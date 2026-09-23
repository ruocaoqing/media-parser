import csv
from datetime import datetime, time, timedelta, timezone
import io
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from werkzeug.security import generate_password_hash

from configs.general_constants import DOMAIN_TO_NAME
from src.auth import admin_required, csrf_protected, format_log_time
from src.db import (
    DEFAULT_CHECKIN_BONUS,
    DEFAULT_FREE_DAILY_QUOTA,
    beijing_today,
    daily_quota_cap,
    get_daily_trend,
    get_daily_usage_map,
    get_db,
    get_platform_distribution,
    get_top_users,
    is_member,
    open_member_card,
    record_member_card_grant,
    set_setting,
    setting,
    utcnow,
)
from src.utils.table_query import paginate_memory_list, query_paginated_table


bp = Blueprint("admin", __name__, url_prefix="/admin")


def _positive_int(value, default=1, maximum=1000):
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _positive_page(value):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _parse_shanghai_to_utc_iso(val: str, is_end: bool = False) -> str | None:
    if not val:
        return None
    val = val.strip()
    if not val:
        return None
    if len(val) == 10 and val.count("-") == 2:
        try:
            d = datetime.strptime(val, "%Y-%m-%d").date()
            t = time.max if is_end else time.min
            dt = datetime.combine(d, t, tzinfo=ZoneInfo("Asia/Shanghai"))
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            return None
    clean_val = val.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(clean_val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


# ----------------------------------------------------------------------
# 1. 系统概览 (Overview)
# ----------------------------------------------------------------------

@bp.get("", endpoint="dashboard")
@bp.get("/overview", endpoint="overview")
@admin_required
def overview():
    try:
        days = int(request.args.get("days", 7))
        if days not in (7, 30, 90, 180, 0):
            days = 7
    except (TypeError, ValueError):
        days = 7

    db = get_db()
    stats = db.execute(
        "SELECT COUNT(*) calls, SUM(status_code < 400) successes, COUNT(DISTINCT user_id) users "
        "FROM request_logs WHERE datetime(created_at) >= datetime('now','-1 day')"
    ).fetchone()
    settings = {row["key"]: row["value"] for row in db.execute("SELECT * FROM system_settings")}
    chart_data = get_daily_trend(user_id=None, days=days)
    pie_data = get_platform_distribution(user_id=None, days=days)
    top_users = get_top_users(days=days, limit=10)

    return render_template(
        "admin/overview.html",
        stats=stats,
        settings=settings,
        chart_data=chart_data,
        pie_data=pie_data,
        top_users=top_users,
        current_days=days,
        active_nav="overview",
    )


# ----------------------------------------------------------------------
# 2. 系统设置 (Settings)
# ----------------------------------------------------------------------

@bp.get("/settings")
@admin_required
def settings():
    db = get_db()
    settings = {row["key"]: row["value"] for row in db.execute("SELECT * FROM system_settings")}
    return render_template(
        "admin/settings.html",
        settings=settings,
        active_nav="settings",
    )


@bp.post("/settings")
@admin_required
@csrf_protected
def update_settings():
    db = get_db()
    for name in ("global_api_enabled", "homepage_enabled", "demo_enabled", "registration_enabled", "api_tip_enabled"):
        set_setting(name, "1" if request.form.get(name) else "0")
    set_setting("default_user_qps", _positive_int(request.form.get("default_user_qps"), 2))
    set_setting("wx_free_daily_quota", _positive_int(request.form.get("wx_free_daily_quota"), DEFAULT_FREE_DAILY_QUOTA, 100000))
    set_setting("wx_checkin_bonus", _positive_int(request.form.get("wx_checkin_bonus"), DEFAULT_CHECKIN_BONUS, 100000))

    if request.form.get("chk_unlimited_credits") == "1" or request.form.get("default_initial_credits") == "-1":
        set_setting("default_initial_credits", -1)
    else:
        try:
            raw_val = request.form.get("default_initial_credits")
            if raw_val is None or str(raw_val).strip() == "":
                init_cred = 100
            else:
                raw_cred = int(str(raw_val).replace(",", "").strip())
                init_cred = -1 if raw_cred == -1 else max(0, min(raw_cred, 99999999))
        except (TypeError, ValueError):
            init_cred = 100
        set_setting("default_initial_credits", init_cred)

    set_setting("api_tip_author", (request.form.get("api_tip_author") or "").strip() or "ucmao")
    set_setting("api_tip_website", (request.form.get("api_tip_website") or "").strip() or "https://github.com/ucmao/media-parser")
    set_setting("api_tip_notice", (request.form.get("api_tip_notice") or "").strip() or "本接口由开源项目 media-parser 提供服务")

    db.commit()

    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or "application/json" in request.headers.get("Accept", "")
    if is_ajax:
        return jsonify({"succ": True, "message": "系统设置已自动保存"})

    flash("系统设置已保存", "success")
    return redirect(url_for("admin.settings"))


# ----------------------------------------------------------------------
# 3. 客户账号 (Users)
# ----------------------------------------------------------------------

@bp.get("/users")
@admin_required
def users():
    db = get_db()
    users_where, users_params = [], []
    if u_q := request.args.get("users_q", "").strip():
        users_where.append("u.username LIKE ?")
        users_params.append(f"%{u_q}%")
    if u_role := request.args.get("users_role", "").strip():
        users_where.append("u.role = ?")
        users_params.append(u_role)
    if u_active := request.args.get("users_active", "").strip():
        try:
            val = int(u_active)
            users_where.append("u.active = ?")
            users_params.append(val)
        except ValueError:
            pass

    users_allowed_sorts = {
        "id": "u.id",
        "username": "u.username",
        "role": "u.role",
        "created_at": "u.created_at",
        "qps_limit": "u.qps_limit",
        "credits": "u.credits",
    }
    users_table = query_paginated_table(
        db,
        base_from_sql="users u",
        select_fields="u.*",
        allowed_sorts=users_allowed_sorts,
        default_sort="id",
        default_order="desc",
        where_clauses=users_where,
        where_params=users_params,
        request_args=request.args,
        default_page_size=20,
        prefix="users_",
    )

    member_plans = db.execute(
        "SELECT * FROM member_plans WHERE active=1 ORDER BY sort_order, code"
    ).fetchall()

    # 「会员」列与「今日额度」列所需的每行数据。
    # 卡种与免费额度**预加载一次**：不预加载的话，20 行 × 每行 2 次查询（卡种表 +
    # system_settings）就是 40 条只为渲染一页的 SQL。用量则一次性按 IN 取回。
    plans_by_code = {plan["code"]: int(plan["daily_quota"]) for plan in member_plans}
    plan_names = {plan["code"]: plan["name"] for plan in member_plans}
    free_quota = setting("wx_free_daily_quota", DEFAULT_FREE_DAILY_QUOTA)
    usage_map = get_daily_usage_map([u["id"] for u in users_table["items"]], beijing_today())
    quota_info = {}
    for u in users_table["items"]:
        if u["role"] == "admin":
            # 管理员 cap 为 None = 不限，与 access.py 的 `quota_cap = None if role=='admin'`
            # 同一口径：consume_daily_quota 拿到 None 既不拦也不记账。所以后台**不能**
            # 按免费档给管理员显示「0 / 10」—— 那是个并不存在的额度。
            quota_info[u["id"]] = {"used": 0, "cap": None, "member": None, "plan_name": None}
            continue
        # 会员判定用 is_member()（唯一真相来源），**不是**「member_plan 非空」：
        # 卡种下架后 member_plan 还留着，但额度已经掉回免费档。两列必须同口径，
        # 否则会出现「显示年卡、额度却是 10」这种自相矛盾的行。
        member = is_member(u)
        quota_info[u["id"]] = {
            "used": usage_map.get(u["id"], 0),
            "cap": daily_quota_cap(u, plans_by_code, free_quota),
            "member": member,
            "plan_name": plan_names.get(u["member_plan"]) if member else None,
        }

    return render_template(
        "admin/users.html",
        users=users_table["items"],
        users_table=users_table,
        member_plans=member_plans,
        quota_info=quota_info,
        active_nav="users",
    )


# ----------------------------------------------------------------------
# 3.1 会员卡种 (Member Plans)
# ----------------------------------------------------------------------

@bp.get("/member-plans")
@admin_required
def member_plans():
    db = get_db()
    plans = db.execute("SELECT * FROM member_plans ORDER BY sort_order, code").fetchall()
    return render_template("admin/member_plans.html", plans=plans, active_nav="member_plans")


@bp.post("/member-plans/<code>")
@admin_required
@csrf_protected
def update_member_plan(code):
    db = get_db()
    exists = db.execute("SELECT code FROM member_plans WHERE code=?", (code,)).fetchone()
    if not exists:
        flash("卡种不存在", "error")
        return redirect(url_for("admin.member_plans"))
    db.execute(
        "UPDATE member_plans SET name=?, days=?, daily_quota=?, active=? WHERE code=?",
        (
            (request.form.get("name") or "").strip()[:20] or code,
            _positive_int(request.form.get("days"), 1, 3650),
            _positive_int(request.form.get("daily_quota"), 1, 100000),
            1 if request.form.get("active") else 0,
            code,
        ),
    )
    db.commit()
    flash("卡种已保存", "success")
    return redirect(url_for("admin.member_plans"))


@bp.post("/users/<int:user_id>")
@admin_required
@csrf_protected
def update_user(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user["role"] == "admin":
        flash("无法操作该用户账号", "error")
        return redirect(url_for("admin.users"))

    # 快捷切换启用/停用状态
    # 守卫的三项（qps_limit / credits）是「这是编辑弹窗的完整表单」的判据：
    # 客户账号页「状态」列那个按钮只提交 active，走这条分支，不碰积分和 QPS。
    if "active" in request.form and "qps_limit" not in request.form and "credits" not in request.form:
        active_val = 1 if request.form.get("active") in ("1", "true", "on") else 0
        db.execute("UPDATE users SET active=? WHERE id=? AND role!='admin'", (active_val, user_id))
        db.commit()
        status_text = "已启用" if active_val else "已停用"
        flash(f"已成功{status_text}客户「{user['username']}」", "success")
        return redirect(url_for("admin.users"))

    credits_raw = request.form.get("credits", "").replace(",", "").strip()
    try:
        user_credits = int(credits_raw) if credits_raw else None
    except ValueError:
        user_credits = None

    qps = _positive_int(request.form.get("qps_limit"), 2)

    if "active" in request.form:
        active_val = 1 if request.form.get("active") in ("1", "true", "on") else 0
        if user_credits is not None:
            db.execute(
                "UPDATE users SET active=?, qps_limit=?, credits=? WHERE id=? AND role!='admin'",
                (active_val, qps, user_credits, user_id),
            )
        else:
            db.execute(
                "UPDATE users SET active=?, qps_limit=? WHERE id=? AND role!='admin'",
                (active_val, qps, user_id),
            )
    else:
        # 编辑弹窗保存（不修改现有启用/停用状态）
        if user_credits is not None:
            db.execute(
                "UPDATE users SET qps_limit=?, credits=? WHERE id=? AND role!='admin'",
                (qps, user_credits, user_id),
            )
        else:
            db.execute(
                "UPDATE users SET qps_limit=? WHERE id=? AND role!='admin'",
                (qps, user_id),
            )

    db.commit()
    flash("客户设置已保存", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:user_id>/member-card")
@admin_required
@csrf_protected
def grant_member_card(user_id):
    """运营手动开卡。第二期接入虚拟支付后，支付回调走同一个 open_member_card()。"""
    plan_code = (request.form.get("plan_code") or "").strip()
    ok, message, expires = open_member_card(user_id, plan_code)
    if ok and record_member_card_grant(user_id, plan_code, expires, granted_by=g.user["id"]) is None:
        # 只记日志、不回滚开卡：卡已经开出并提交了，为了记不上账把它撤销更糟。
        current_app.logger.warning(
            f"开卡记录写入失败：user_id={user_id} plan={plan_code}（卡已开通，仅少了审计行）"
        )
    flash(message, "success" if ok else "error")
    return redirect(url_for("admin.users"))


@bp.get("/member-cards")
@admin_required
def member_cards():
    """开卡记录：谁、什么时候、给谁开过哪张卡。"""
    db = get_db()
    where_clauses, params = [], []
    if c_q := request.args.get("cards_q", "").strip():
        where_clauses.append("(g.username LIKE ? OR g.granted_by_name LIKE ?)")
        params.extend([f"%{c_q}%", f"%{c_q}%"])
    if c_plan := request.args.get("cards_plan", "").strip():
        where_clauses.append("g.plan_code = ?")
        params.append(c_plan)

    cards_table = query_paginated_table(
        db,
        base_from_sql="member_card_grants g",
        select_fields="g.*",
        allowed_sorts={
            "id": "g.id",
            "username": "g.username",
            "plan_code": "g.plan_code",
            "expires_at": "g.expires_at",
            "created_at": "g.created_at",
        },
        default_sort="id",
        default_order="desc",
        where_clauses=where_clauses,
        where_params=params,
        request_args=request.args,
        default_page_size=20,
        prefix="cards_",
    )

    plans = db.execute("SELECT code, name FROM member_plans ORDER BY sort_order, code").fetchall()
    # 筛选下拉的卡种 = 「在架的」∪「记录里出现过的」，两边缺一不可：
    #   * 只用记录里的（最初的实现）：一条记录都还没有时是个空框 —— 上线第一天运营打开
    #     这一页，筛选框里只有「全部卡种」，看着像坏了；
    #   * 只用在架的：卡种下架或删掉之后，它开出去的卡就再也筛不出来，而那恰恰是最需要
    #     对账的时候。
    # 同一个 code 两边都有时以 member_plans 的名称为准：后台改了名字，筛选项要跟着改，
    # 否则运营按新名字找不到、按旧名字才找得到。dict 保留插入顺序 —— 在架卡种按
    # sort_order 排在前面，只剩历史记录的卡种缀在后面。
    plan_names = {row["code"]: row["name"] for row in plans}
    for row in db.execute(
        "SELECT DISTINCT plan_code, plan_name FROM member_card_grants ORDER BY plan_name"
    ):
        plan_names.setdefault(row["plan_code"], row["plan_name"])
    all_plans = [
        {"plan_code": code, "plan_name": name} for code, name in plan_names.items()
    ]

    return render_template(
        "admin/member_cards.html",
        cards=cards_table["items"],
        cards_table=cards_table,
        plans=plans,
        all_plans=all_plans,
        active_nav="member_cards",
    )


@bp.post("/users/<int:user_id>/reset-password")
@admin_required
@csrf_protected
def reset_password(user_id):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user or user["role"] == "admin":
        flash("无法操作该用户账号", "error")
        return redirect(url_for("admin.users"))

    new_password = request.form.get("new_password", "")
    if len(new_password) < 8:
        flash("重置密码至少需要 8 位", "error")
        return redirect(url_for("admin.users"))

    db.execute(
        "UPDATE users SET password_hash=? WHERE id=?",
        (generate_password_hash(new_password), user_id),
    )
    db.commit()
    flash(f"已成功重置客户「{user['username']}」的密码", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/batch")
@admin_required
@csrf_protected
def batch_users():
    db = get_db()
    action = request.form.get("action", "").strip()
    select_mode = request.form.get("select_mode", "page").strip()

    if not action:
        flash("未指定批量操作类型", "error")
        return redirect(url_for("admin.users"))

    if select_mode == "all":
        users_where, users_params = [], []
        if u_q := (request.form.get("users_q") or request.form.get("q") or "").strip():
            users_where.append("u.username LIKE ?")
            users_params.append(f"%{u_q}%")
        if u_role := (request.form.get("users_role") or request.form.get("role") or "").strip():
            users_where.append("u.role = ?")
            users_params.append(u_role)
        if u_active := (request.form.get("users_active") or request.form.get("active") or "").strip():
            try:
                val = int(u_active)
                users_where.append("u.active = ?")
                users_params.append(val)
            except ValueError:
                pass
        users_where.append("u.role != 'admin'")

        where_sql = " WHERE " + " AND ".join(users_where)
        subquery = f"SELECT u.id FROM users u{where_sql}"
        matched_count = db.execute(f"SELECT COUNT(*) as c FROM users WHERE id IN ({subquery})", users_params).fetchone()["c"]

        if action == "enable":
            db.execute(f"UPDATE users SET active=1 WHERE id IN ({subquery})", users_params)
            flash(f"已批量启用符合筛选条件的全部 {matched_count} 个客户账号", "success")
        elif action == "disable":
            db.execute(f"UPDATE users SET active=0 WHERE id IN ({subquery})", users_params)
            flash(f"已批量停用符合筛选条件的全部 {matched_count} 个客户账号", "success")
        elif action == "adjust_credits":
            mode = request.form.get("credits_mode", "add")
            try:
                amount_raw = str(request.form.get("credits_amount", 0)).replace(",", "").strip()
                amount = int(amount_raw)
            except ValueError:
                amount = 0

            if mode == "set":
                db.execute(f"UPDATE users SET credits=? WHERE id IN ({subquery})", [amount] + users_params)
            elif mode == "add":
                db.execute(f"UPDATE users SET credits=credits+? WHERE id IN ({subquery}) AND credits!=-1", [amount] + users_params)
            elif mode == "deduct":
                db.execute(f"UPDATE users SET credits=MAX(0, credits-?) WHERE id IN ({subquery}) AND credits!=-1", [amount] + users_params)
            flash(f"已批量更新符合筛选条件的全部 {matched_count} 个客户账号积分", "success")
        elif action == "set_qps":
            try:
                qps = _positive_int(request.form.get("qps_limit"), 2)
                db.execute(f"UPDATE users SET qps_limit=? WHERE id IN ({subquery})", [qps] + users_params)
                flash(f"已批量设置符合筛选条件的全部 {matched_count} 个客户并发 QPS 为 {qps}", "success")
            except ValueError:
                flash("QPS 格式无效", "error")
        else:
            flash("不支持的批量操作类型", "error")
            return redirect(url_for("admin.users"))

        db.commit()
        return redirect(url_for("admin.users"))

    ids_raw = request.form.get("ids", "").strip()
    if not ids_raw:
        flash("请先勾选需要批量操作的客户账号", "error")
        return redirect(url_for("admin.users"))

    try:
        user_ids = [int(i.strip()) for i in ids_raw.split(",") if i.strip().isdigit()]
    except ValueError:
        user_ids = []

    if not user_ids:
        flash("未获取到有效的用户 ID 列表", "error")
        return redirect(url_for("admin.users"))

    placeholders = ",".join(["?"] * len(user_ids))

    if action == "enable":
        db.execute(f"UPDATE users SET active=1 WHERE id IN ({placeholders}) AND role!='admin'", user_ids)
        flash(f"已批量启用选中的 {len(user_ids)} 个客户账号", "success")
    elif action == "disable":
        db.execute(f"UPDATE users SET active=0 WHERE id IN ({placeholders}) AND role!='admin'", user_ids)
        flash(f"已批量停用选中的 {len(user_ids)} 个客户账号", "success")
    elif action == "adjust_credits":
        mode = request.form.get("credits_mode", "add")
        try:
            amount = int(request.form.get("credits_amount", 0))
        except ValueError:
            amount = 0

        if mode == "set":
            db.execute(f"UPDATE users SET credits=? WHERE id IN ({placeholders}) AND role!='admin'", [amount] + user_ids)
        elif mode == "add":
            db.execute(f"UPDATE users SET credits=credits+? WHERE id IN ({placeholders}) AND role!='admin' AND credits!=-1", [amount] + user_ids)
        elif mode == "deduct":
            db.execute(f"UPDATE users SET credits=MAX(0, credits-?) WHERE id IN ({placeholders}) AND role!='admin' AND credits!=-1", [amount] + user_ids)
        flash(f"已批量更新选中的 {len(user_ids)} 个客户账号积分", "success")
    elif action == "set_qps":
        try:
            qps = _positive_int(request.form.get("qps_limit"), 2)
            db.execute(f"UPDATE users SET qps_limit=? WHERE id IN ({placeholders}) AND role!='admin'", [qps] + user_ids)
            flash(f"已批量设置选中的 {len(user_ids)} 个客户并发 QPS 为 {qps}", "success")
        except ValueError:
            flash("QPS 格式无效", "error")
    else:
        flash("不支持的批量操作类型", "error")
        return redirect(url_for("admin.users"))

    db.commit()
    return redirect(url_for("admin.users"))


# ----------------------------------------------------------------------
# 5. 支持平台 (Platforms)
# ----------------------------------------------------------------------

@bp.get("/platforms")
@admin_required
def platforms():
    db = get_db()
    configured = {row["platform"]: row for row in db.execute("SELECT * FROM platform_settings")}

    # 平台与域名映射
    platform_to_domains = {}
    for domain, pname in DOMAIN_TO_NAME.items():
        platform_to_domains.setdefault(pname, []).append(domain)
    for pname in platform_to_domains:
        platform_to_domains[pname].sort()

    # 近 24 小时各平台解析调用统计
    stats_rows = db.execute(
        "SELECT platform, COUNT(*) as calls, SUM(status_code < 400) as successes, AVG(duration_ms) as avg_duration "
        "FROM request_logs "
        "WHERE datetime(created_at) >= datetime('now', '-1 day') "
        "GROUP BY platform"
    ).fetchall()
    stats_by_platform = {}
    for r in stats_rows:
        p = r["platform"]
        calls = r["calls"] or 0
        successes = r["successes"] or 0
        avg_dur = round(r["avg_duration"]) if r["avg_duration"] else 0
        stats_by_platform[p] = {
            "calls": calls,
            "successes": successes,
            "avg_duration": avg_dur,
            "success_rate": round(successes * 100.0 / calls, 1) if calls > 0 else 0,
        }

    platforms_list = []
    for name in sorted(set(DOMAIN_TO_NAME.values())):
        row = configured.get(name)
        p_stats = stats_by_platform.get(name, {"calls": 0, "successes": 0, "avg_duration": 0, "success_rate": 0})
        platforms_list.append({
            "name": name,
            "enabled": True if row is None else bool(row["enabled"]),
            "qps_limit": None if row is None else row["qps_limit"],
            "domains": platform_to_domains.get(name, []),
            "domain_count": len(platform_to_domains.get(name, [])),
            "calls_24h": p_stats["calls"],
            "successes_24h": p_stats["successes"],
            "avg_duration_24h": p_stats["avg_duration"],
            "success_rate_24h": p_stats["success_rate"],
        })

    # 顶部统计概览
    total_count = len(platforms_list)
    enabled_count = sum(1 for p in platforms_list if p["enabled"])
    disabled_count = total_count - enabled_count
    limited_count = sum(1 for p in platforms_list if p["qps_limit"])
    total_calls_24h = sum(p["calls_24h"] for p in platforms_list)
    total_successes_24h = sum(p["successes_24h"] for p in platforms_list)
    overall_success_rate = round(total_successes_24h * 100.0 / total_calls_24h, 1) if total_calls_24h > 0 else 100.0

    summary_stats = {
        "total_count": total_count,
        "enabled_count": enabled_count,
        "disabled_count": disabled_count,
        "limited_count": limited_count,
        "total_calls_24h": total_calls_24h,
        "overall_success_rate": overall_success_rate,
    }

    # 过滤条件筛选
    p_q = request.args.get("platforms_q", "").strip().lower()
    p_status = request.args.get("platforms_status", "").strip()

    filtered_platforms = []
    for p in platforms_list:
        if p_q:
            match_name = p_q in p["name"].lower()
            match_domains = any(p_q in d.lower() for d in p["domains"])
            if not (match_name or match_domains):
                continue
        if p_status == "enabled" and not p["enabled"]:
            continue
        elif p_status == "disabled" and p["enabled"]:
            continue
        elif p_status == "limited" and not p["qps_limit"]:
            continue
        filtered_platforms.append(p)

    platforms_allowed_sorts = {
        "name": "name",
        "domains": "domain_count",
        "calls": "calls_24h",
        "rate": "success_rate_24h",
        "success_rate": "success_rate_24h",
        "qps": lambda x: (x["qps_limit"] is None, x["qps_limit"] or 0),
        "status": lambda x: (not x["enabled"], x["name"]),
    }

    platforms_table = paginate_memory_list(
        filtered_platforms,
        allowed_sorts=platforms_allowed_sorts,
        default_sort="name",
        default_order="asc",
        request_args=request.args,
        default_page_size=50,
        prefix="platforms_",
    )

    return render_template(
        "admin/platforms.html",
        platforms=platforms_table["items"],
        platforms_table=platforms_table,
        summary_stats=summary_stats,
        active_nav="platforms",
    )


@bp.post("/platforms/<path:platform>")
@admin_required
@csrf_protected
def update_platform(platform):
    if platform not in set(DOMAIN_TO_NAME.values()):
        flash("未知平台", "error")
        return redirect(url_for("admin.platforms"))
    qps_raw = request.form.get("qps_limit", "").strip()
    qps = _positive_int(qps_raw) if qps_raw else None
    db = get_db()
    db.execute(
        "INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,?,?) "
        "ON CONFLICT(platform) DO UPDATE SET enabled=excluded.enabled,qps_limit=excluded.qps_limit",
        (platform, 1 if request.form.get("enabled") else 0, qps),
    )
    db.commit()
    flash(f"{platform} 设置已保存", "success")
    return redirect(url_for("admin.platforms"))


@bp.post("/platforms/batch")
@admin_required
@csrf_protected
def batch_platforms():
    db = get_db()
    action = request.form.get("action", "").strip()
    select_mode = request.form.get("select_mode", "page").strip()

    if not action:
        flash("未指定批量操作类型", "error")
        return redirect(url_for("admin.platforms"))

    if select_mode == "all":
        configured = {row["platform"]: row for row in db.execute("SELECT * FROM platform_settings")}
        platform_to_domains = {}
        for domain, pname in DOMAIN_TO_NAME.items():
            platform_to_domains.setdefault(pname, []).append(domain)

        platforms_list = []
        for name in sorted(set(DOMAIN_TO_NAME.values())):
            row = configured.get(name)
            enabled = True if row is None else bool(row["enabled"])
            qps_limit = None if row is None else row["qps_limit"]
            platforms_list.append({
                "name": name,
                "enabled": enabled,
                "qps_limit": qps_limit,
                "domains": platform_to_domains.get(name, []),
            })

        p_q = (request.form.get("platforms_q") or request.form.get("q") or "").strip().lower()
        p_status = (request.form.get("platforms_status") or request.form.get("status") or "").strip()

        filtered_platforms = []
        for p in platforms_list:
            if p_q:
                match_name = p_q in p["name"].lower()
                match_domains = any(p_q in d.lower() for d in p["domains"])
                if not (match_name or match_domains):
                    continue
            if p_status == "enabled" and not p["enabled"]:
                continue
            elif p_status == "disabled" and p["enabled"]:
                continue
            elif p_status == "limited" and not p["qps_limit"]:
                continue
            filtered_platforms.append(p)

        platform_names = [p["name"] for p in filtered_platforms]
        if not platform_names:
            flash("未找到符合筛选条件的支持平台", "error")
            return redirect(url_for("admin.platforms"))

        qps_raw = request.form.get("qps_limit", "").strip()
        qps = _positive_int(qps_raw) if qps_raw else None

        for name in platform_names:
            if action == "enable":
                db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,1,NULL) ON CONFLICT(platform) DO UPDATE SET enabled=1", (name,))
            elif action == "disable":
                db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,0,NULL) ON CONFLICT(platform) DO UPDATE SET enabled=0", (name,))
            elif action == "set_qps":
                db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,1,?) ON CONFLICT(platform) DO UPDATE SET qps_limit=excluded.qps_limit", (name, qps))

        db.commit()
        flash(f"已成功批量更新符合筛选条件的全部 {len(platform_names)} 个支持平台配置", "success")
        return redirect(url_for("admin.platforms"))

    names_raw = request.form.get("names", "").strip()
    if not names_raw:
        flash("请先勾选需要批量操作的支持平台", "error")
        return redirect(url_for("admin.platforms"))

    platform_names = [n.strip() for n in names_raw.split(",") if n.strip() in set(DOMAIN_TO_NAME.values())]

    if not platform_names:
        flash("未获取到有效的支持平台列表", "error")
        return redirect(url_for("admin.platforms"))

    qps_raw = request.form.get("qps_limit", "").strip()
    qps = _positive_int(qps_raw) if qps_raw else None

    for name in platform_names:
        if action == "enable":
            db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,1,NULL) ON CONFLICT(platform) DO UPDATE SET enabled=1", (name,))
        elif action == "disable":
            db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,0,NULL) ON CONFLICT(platform) DO UPDATE SET enabled=0", (name,))
        elif action == "set_qps":
            db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES(?,1,?) ON CONFLICT(platform) DO UPDATE SET qps_limit=excluded.qps_limit", (name, qps))

    db.commit()
    flash(f"已成功批量更新 {len(platform_names)} 个支持平台配置", "success")
    return redirect(url_for("admin.platforms"))


# ----------------------------------------------------------------------
# 7. 运行日志 (Logs)
# ----------------------------------------------------------------------

@bp.get("/logs")
@admin_required
def logs():
    db = get_db()
    logs_where, logs_params = [], []
    if l_q := request.args.get("logs_q", request.args.get("q", "")).strip():
        logs_where.append("(l.input_url LIKE ? OR u.username LIKE ? OR l.error_code LIKE ?)")
        logs_params.extend([f"%{l_q}%", f"%{l_q}%", f"%{l_q}%"])
    if l_status := request.args.get("logs_status", request.args.get("status_code", "")).strip():
        if l_status == "200":
            logs_where.append("l.status_code < 400")
        elif l_status == "error":
            logs_where.append("l.status_code >= 400")
        elif l_status.isdigit():
            logs_where.append("l.status_code = ?")
            logs_params.append(int(l_status))
    if l_platform := request.args.get("logs_platform", request.args.get("platform", "")).strip():
        logs_where.append("l.platform = ?")
        logs_params.append(l_platform)
    if utc_start := _parse_shanghai_to_utc_iso(request.args.get("logs_start_date", request.args.get("start_date", "")), is_end=False):
        logs_where.append("l.created_at >= ?")
        logs_params.append(utc_start)
    if utc_end := _parse_shanghai_to_utc_iso(request.args.get("logs_end_date", request.args.get("end_date", "")), is_end=True):
        logs_where.append("l.created_at <= ?")
        logs_params.append(utc_end)

    logs_allowed_sorts = {
        "id": "l.id",
        "created_at": "l.created_at",
        "duration_ms": "l.duration_ms",
        "status_code": "l.status_code",
        "username": "u.username",
        "platform": "l.platform",
    }

    req_args = dict(request.args)
    if "page" in req_args and "logs_page" not in req_args:
        req_args["logs_page"] = req_args["page"]

    logs_table = query_paginated_table(
        db,
        base_from_sql="request_logs l LEFT JOIN users u ON u.id=l.user_id",
        select_fields="l.*, u.username",
        allowed_sorts=logs_allowed_sorts,
        default_sort="id",
        default_order="desc",
        where_clauses=logs_where,
        where_params=logs_params,
        request_args=req_args,
        prefix="logs_",
    )

    configured = {row["platform"]: row for row in db.execute("SELECT * FROM platform_settings")}
    platforms_list = []
    for name in sorted(set(DOMAIN_TO_NAME.values())):
        row = configured.get(name)
        platforms_list.append({
            "name": name,
            "enabled": True if row is None else bool(row["enabled"]),
            "qps_limit": None if row is None else row["qps_limit"],
        })

    return render_template(
        "admin/logs.html",
        logs=logs_table["items"],
        logs_table=logs_table,
        platforms=platforms_list,
        active_nav="logs",
    )


def _safe_csv_cell(value):
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


@bp.get("/logs/export.csv")
@admin_required
def export_logs():
    db = get_db()
    export_type = request.args.get("export_type", "all")
    ids_raw = request.args.get("ids", "").strip()

    where_clauses, params = [], []

    if export_type == "selected" and ids_raw:
        try:
            id_list = [int(i.strip()) for i in ids_raw.split(",") if i.strip().isdigit()]
            if id_list:
                placeholders = ",".join(["?"] * len(id_list))
                where_clauses.append(f"l.id IN ({placeholders})")
                params.extend(id_list)
        except ValueError:
            pass
    else:
        if l_q := request.args.get("logs_q", request.args.get("q", "")).strip():
            where_clauses.append("(l.input_url LIKE ? OR u.username LIKE ? OR l.error_code LIKE ?)")
            params.extend([f"%{l_q}%", f"%{l_q}%", f"%{l_q}%"])
        if l_status := request.args.get("logs_status", request.args.get("status_code", "")).strip():
            if l_status == "200":
                where_clauses.append("l.status_code < 400")
            elif l_status == "error":
                where_clauses.append("l.status_code >= 400")
            elif l_status.isdigit():
                where_clauses.append("l.status_code = ?")
                params.append(int(l_status))
        if l_platform := request.args.get("logs_platform", request.args.get("platform", "")).strip():
            where_clauses.append("l.platform = ?")
            params.append(l_platform)
        if utc_start := _parse_shanghai_to_utc_iso(request.args.get("logs_start_date", request.args.get("start_date", "")), is_end=False):
            where_clauses.append("l.created_at >= ?")
            params.append(utc_start)
        if utc_end := _parse_shanghai_to_utc_iso(request.args.get("logs_end_date", request.args.get("end_date", "")), is_end=True):
            where_clauses.append("l.created_at <= ?")
            params.append(utc_end)

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(("时间", "客户", "平台", "请求路径", "请求 URL", "状态码", "耗时（毫秒）", "错误码"))

    cursor = db.execute(
        f"SELECT l.*, u.username FROM request_logs l "
        f"LEFT JOIN users u ON u.id=l.user_id "
        f"{where_sql} ORDER BY l.id DESC",
        params,
    )
    while rows := cursor.fetchmany(1000):
        for row in rows:
            writer.writerow((
                _safe_csv_cell(format_log_time(row["created_at"])),
                _safe_csv_cell(row["username"] or "在线体验"),
                _safe_csv_cell(row["platform"] or ""),
                _safe_csv_cell(row["path"]),
                _safe_csv_cell(row["input_url"] or ""),
                _safe_csv_cell(row["status_code"]),
                _safe_csv_cell(row["duration_ms"]),
                _safe_csv_cell(row["error_code"] or ""),
            ))

    csv_bytes = output.getvalue().encode("utf-8")
    filename = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("request-logs-%Y%m%d-%H%M%S.csv")
    return Response(
        csv_bytes,
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(csv_bytes)),
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@bp.post("/logs/<int:log_id>/delete")
@admin_required
@csrf_protected
def delete_log(log_id):
    db = get_db()
    db.execute("DELETE FROM request_logs WHERE id=?", (log_id,))
    db.commit()
    flash(f"已删除日志记录 #{log_id}", "success")
    return redirect(url_for("admin.logs"))


@bp.post("/logs/batch")
@admin_required
@csrf_protected
def batch_logs():
    db = get_db()
    action = request.form.get("action", "").strip()
    select_mode = request.form.get("select_mode", "page").strip()

    if not action:
        flash("未指定批量操作类型", "error")
        return redirect(url_for("admin.logs"))

    if select_mode == "all":
        where_clauses, params = [], []
        if l_q := (request.form.get("logs_q") or request.form.get("q") or "").strip():
            where_clauses.append("(l.input_url LIKE ? OR u.username LIKE ? OR l.error_code LIKE ?)")
            params.extend([f"%{l_q}%", f"%{l_q}%", f"%{l_q}%"])
        if l_status := (request.form.get("logs_status") or request.form.get("status_code") or "").strip():
            if l_status == "200":
                where_clauses.append("l.status_code < 400")
            elif l_status == "error":
                where_clauses.append("l.status_code >= 400")
            elif l_status.isdigit():
                where_clauses.append("l.status_code = ?")
                params.append(int(l_status))
        if l_platform := (request.form.get("logs_platform") or request.form.get("platform") or "").strip():
            where_clauses.append("l.platform = ?")
            params.append(l_platform)
        if utc_start := _parse_shanghai_to_utc_iso(request.form.get("logs_start_date") or request.form.get("start_date") or "", is_end=False):
            where_clauses.append("l.created_at >= ?")
            params.append(utc_start)
        if utc_end := _parse_shanghai_to_utc_iso(request.form.get("logs_end_date") or request.form.get("end_date") or "", is_end=True):
            where_clauses.append("l.created_at <= ?")
            params.append(utc_end)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        subquery = f"SELECT l.id FROM request_logs l LEFT JOIN users u ON u.id=l.user_id{where_sql}"
        matched_count = db.execute(f"SELECT COUNT(*) as c FROM request_logs WHERE id IN ({subquery})", params).fetchone()["c"]

        if action == "delete":
            db.execute(f"DELETE FROM request_logs WHERE id IN ({subquery})", params)
            db.commit()
            flash(f"已批量删除符合筛选条件的全部 {matched_count} 条日志记录", "success")
        else:
            flash("不支持的批量操作类型", "error")

        return redirect(url_for("admin.logs"))

    ids_raw = request.form.get("ids", "").strip()
    if not ids_raw:
        flash("请先勾选需要批量操作的日志条目", "error")
        return redirect(url_for("admin.logs"))

    try:
        log_ids = [int(i.strip()) for i in ids_raw.split(",") if i.strip().isdigit()]
    except ValueError:
        log_ids = []

    if not log_ids:
        flash("未获取到有效的日志 ID 列表", "error")
        return redirect(url_for("admin.logs"))

    placeholders = ",".join(["?"] * len(log_ids))

    if action == "delete":
        db.execute(f"DELETE FROM request_logs WHERE id IN ({placeholders})", log_ids)
        flash(f"已批量删除选中的 {len(log_ids)} 条日志记录", "success")
    else:
        flash("不支持的批量操作类型", "error")

    db.commit()
    return redirect(url_for("admin.logs"))
