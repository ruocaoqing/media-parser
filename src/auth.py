import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from src.db import get_db, setting, utcnow


bp = Blueprint("auth", __name__, url_prefix="/auth")


# 2026-09-22 移除：`hash_api_key()` / `legacy_hash_api_key()` / `generate_api_key()`
# 随 API Key 通道一并撤销（见 src/api/access.py 顶部说明）。
#
# 撤销前它们就已经不是「在用」状态：
#   * `authenticate_api_key()` 实际是拿明文 `k.key = ?` 去比对的，这两个哈希函数无人调用；
#   * `generate_api_key()` 只被后台的「新建密钥」表单用到，而那份路由已删。
# 也就是说，这三个函数是**已经死掉但还没埋**的代码 —— 撤销密钥通道只是让它们显形。
#
# `api_keys` 表本身保留（历史日志的 api_key_id 指着它），但不再有任何代码写它：
# 表在、数据在、可查，只是没有入口。若日后要开程序化通道，请连函数一起重新设计。


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    return session["csrf_token"]


def validate_csrf():
    supplied = request.form.get("csrf_token")
    expected = session.get("csrf_token")
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied, expected)


def auth_rate_limited(action, username=""):
    """限制管理员初始化、注册和登录尝试，避免批量注册与密码爆破。"""
    from src.api.access import consume_rate_limits, get_client_ip

    client_ip = get_client_ip()
    if action == "setup":
        limits = [(f"auth:setup:ip:{client_ip}", 10, 900)]
    elif action == "register":
        limits = [(f"auth:register:ip:{client_ip}", 5, 3600)]
    else:
        username_digest = hashlib.sha256(username.casefold().encode()).hexdigest()
        limits = [
            (f"auth:login:ip:{client_ip}", 30, 300),
            (f"auth:login:user:{username_digest}", 10, 300),
        ]
    return consume_rate_limits(limits) is not None


def csrf_protected(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not validate_csrf():
            flash("页面已过期，请重试", "error")
            return redirect(request.referrer or url_for("web.index"))
        return view(*args, **kwargs)
    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        if g.user["role"] != "admin":
            flash("需要管理员权限", "error")
            return redirect(url_for("portal.dashboard"))
        return view(*args, **kwargs)
    return wrapped


@bp.before_app_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = None if user_id is None else get_db().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()


@bp.route("/setup", methods=("GET", "POST"))
def setup():
    if get_db().execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if not validate_csrf():
            flash("页面已过期，请重试", "error")
        elif auth_rate_limited("setup"):
            flash("初始化尝试过于频繁，请稍后再试", "error")
            return render_template("auth/setup.html"), 429
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            if len(username) < 3 or len(password) < 8:
                flash("用户名至少 3 位，密码至少 8 位", "error")
            elif password != confirm_password:
                flash("两次输入的密码不一致", "error")
            else:
                db = get_db()
                db.execute(
                    "INSERT INTO users(username,password_hash,role,qps_limit,created_at) VALUES(?,?,?,?,?)",
                    (username, generate_password_hash(password), "admin", 20, utcnow()),
                )
                db.commit()
                flash("管理员创建成功，请登录", "success")
                return redirect(url_for("auth.login"))
    return render_template("auth/setup.html")


@bp.route("/register", methods=("GET", "POST"))
def register():
    if setting("registration_enabled", "1") != "1":
        flash("当前未开放注册", "error")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        if not validate_csrf():
            flash("页面已过期，请重试", "error")
        elif auth_rate_limited("register"):
            flash("注册尝试过于频繁，请稍后再试", "error")
            return render_template("auth/register.html"), 429
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            if len(username) < 3 or len(password) < 8:
                flash("用户名至少 3 位，密码至少 8 位", "error")
            elif password != confirm_password:
                flash("两次输入的密码不一致", "error")
            else:
                try:
                    initial_credits = int(setting("default_initial_credits", "100"))
                    db = get_db()
                    db.execute(
                        "INSERT INTO users(username,password_hash,qps_limit,credits,created_at) VALUES(?,?,?,?,?)",
                        (username, generate_password_hash(password), int(setting("default_user_qps", "2")), initial_credits, utcnow()),
                    )
                    db.commit()
                    if initial_credits == -1:
                        msg = "注册成功！账号已开通并享有无限解析额度，请登录"
                    elif initial_credits > 0:
                        msg = f"注册成功！账号已开通并赠送 {initial_credits} 积分，请登录"
                    else:
                        msg = "注册成功！账号已开通，请登录"
                    flash(msg, "success")
                    return redirect(url_for("auth.login"))
                except Exception as exc:
                    if "UNIQUE" not in str(exc).upper():
                        raise
                    flash("用户名已存在", "error")
    return render_template("auth/register.html")


@bp.route("/login", methods=("GET", "POST"))
def login():
    has_admin = bool(get_db().execute("SELECT 1 FROM users WHERE role='admin'").fetchone())
    homepage_enabled = (setting("homepage_enabled", "1") == "1")
    registration_enabled = (setting("registration_enabled", "1") == "1")
    if request.method == "POST":
        if not validate_csrf():
            flash("页面已过期，请重试", "error")
        else:
            username = request.form.get("username", "").strip()
            if auth_rate_limited("login", username):
                flash("登录尝试过于频繁，请稍后再试", "error")
                return render_template(
                    "auth/login.html",
                    has_admin=has_admin,
                    homepage_enabled=homepage_enabled,
                    registration_enabled=registration_enabled,
                ), 429
            user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if user is None or not check_password_hash(user["password_hash"], request.form.get("password", "")):
                flash("用户名或密码错误", "error")
            elif not user["active"]:
                flash("账号已被停用", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                session.permanent = request.form.get("remember_me") in {"1", "on", "true"}
                target = request.args.get("next", "")
                if (
                    not target.startswith("/")
                    or target.startswith("//")
                    or target.startswith("/auth/")
                    or (user["role"] != "admin" and target.startswith("/admin"))
                ):
                    target = url_for("admin.dashboard") if user["role"] == "admin" else url_for("portal.dashboard")
                return redirect(target)
    return render_template(
        "auth/login.html",
        has_admin=has_admin,
        homepage_enabled=homepage_enabled,
        registration_enabled=registration_enabled,
    )


@bp.post("/logout")
@csrf_protected
def logout():
    session.clear()
    return redirect(url_for("web.index"))


@bp.post("/change-password")
@login_required
@csrf_protected
def change_password():
    old_password = request.form.get("old_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not check_password_hash(g.user["password_hash"], old_password):
        flash("原密码错误", "error")
    elif len(new_password) < 8:
        flash("新密码至少需要 8 位", "error")
    elif new_password != confirm_password:
        flash("两次输入的新密码不一致", "error")
    else:
        db = get_db()
        db.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(new_password), g.user["id"]),
        )
        db.commit()
        flash("密码修改成功", "success")

    target = request.referrer
    if not target:
        target = url_for("admin.dashboard") if g.user["role"] == "admin" else url_for("portal.dashboard")
    return redirect(target)




def format_log_time(value):
    """将数据库中以 UTC 保存的时间展示为北京时间。"""
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)[:19].replace("T", " ")


def format_local_date(value):
    """将 UTC 时间转换为北京时间日期，供日期型表单和状态使用。"""
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except ValueError:
        return str(value)[:10]


def api_base_url():
    """获取对外 API 基础根地址，智能识别反向代理协议（如 Nginx X-Forwarded-Proto）并自适应环境。"""
    import os
    env_base = os.getenv("API_BASE_URL") or os.getenv("BASE_URL")
    if env_base:
        return env_base.rstrip("/") + "/"
    try:
        if not request:
            return "http://localhost:8051/"
        proto = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        return f"{proto}://{host}/"
    except RuntimeError:
        return "http://localhost:8051/"


def is_api_enabled():
    """检查全局 API 服务开关是否开启。"""
    return setting("global_api_enabled", "1") == "1"


def render_icon(name, class_name="w-4 h-4"):
    from markupsafe import Markup
    return Markup(f'<svg class="{class_name}" aria-hidden="true" focusable="false"><use href="/static/icons.svg#icon-{name}"></use></svg>')


def register_template_helpers(app):
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["format_log_time"] = format_log_time
    app.jinja_env.globals["format_local_date"] = format_local_date
    app.jinja_env.globals["api_base_url"] = api_base_url
    app.jinja_env.globals["is_api_enabled"] = is_api_enabled
    app.jinja_env.globals["icon"] = render_icon

