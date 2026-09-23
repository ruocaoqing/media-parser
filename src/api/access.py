import hashlib
import random
import re
import sys
import time

from flask import current_app, has_app_context, request

from src.db import daily_quota_cap, get_db, setting, transaction, utcnow


class SQLiteRateLimiter:
    """使用 SQLite 原子事务在所有 Gunicorn worker 之间共享限流状态。"""

    def consume_many(self, limits):
        normalized = []
        for subject, limit, window_seconds in limits:
            try:
                parsed_limit = int(limit)
                parsed_window = max(1, int(window_seconds))
            except (TypeError, ValueError):
                continue
            if parsed_limit > 0:
                normalized.append((str(subject), parsed_limit, parsed_window))
        if not normalized:
            return None

        now_time = int(time.time())
        exceeded = None
        with transaction(immediate=True) as db:
            for subject, limit, window_seconds in normalized:
                bucket_start = (now_time // window_seconds) * window_seconds
                bucket_subject = f"{subject}:{window_seconds}"
                db.execute(
                    "INSERT INTO rate_limit_buckets(subject,bucket_second,count) VALUES(?,?,1) "
                    "ON CONFLICT(subject,bucket_second) DO UPDATE SET count=count+1",
                    (bucket_subject, bucket_start),
                )
                count = db.execute(
                    "SELECT count FROM rate_limit_buckets WHERE subject=? AND bucket_second=?",
                    (bucket_subject, bucket_start),
                ).fetchone()["count"]
                if exceeded is None and count > limit:
                    exceeded = (subject, limit)

            if random.randint(1, 100) == 1:
                db.execute(
                    "DELETE FROM rate_limit_buckets WHERE bucket_second < ?",
                    (now_time - 86400,),
                )
        return exceeded

    def consume(self, subject, limit, window_seconds=1):
        return self.consume_many([(subject, limit, window_seconds)]) is None

    def reset(self):
        if has_app_context():
            db = get_db()
            db.execute("DELETE FROM rate_limit_buckets")
            db.commit()


rate_limiter = SQLiteRateLimiter()


_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def sanitize_log_url(value):
    """提取用户提交的原始链接，供运行日志持久化。"""
    if not isinstance(value, str):
        return None
    match = _URL_PATTERN.search(value.strip())
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?)]}，。；：！？）】》")[:4096]


def _request_log_url():
    value = request.args.get("url") or request.args.get("text")
    if value is None and request.form:
        value = request.form.get("url") or request.form.get("text")
    if value is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            value = payload.get("url") or payload.get("text")
    return sanitize_log_url(value)


def consume_rate_limit(subject, limit, window_seconds=1):
    return rate_limiter.consume(subject, limit, window_seconds)


def consume_rate_limits(limits):
    return rate_limiter.consume_many(limits)


def get_client_ip():
    if current_app.config.get("TRUST_PROXY_HEADERS"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


# 2026-09-22 移除：`authenticate_api_key()` 整体撤销（用户决定「服务端认证一并停用」）。
# 原实现按 Authorization: Bearer <API Key>（或 ?key=）查 api_keys 行，再查停用/有效期/积分三态。
# 撤销理由与善后：
#   * 这套凭证是「注册即可自取、客户自己管理」的形态，而注册送 100 积分 + 365 天试用，
#     等于任何陌生人都能拿到一条免费解析通道（docs/wx-login.md §6.2 称之为无人维护的授权入口）。
#   * 小程序从头到尾走 X-WX-Token，从不使用它 —— 全库仅 1 把密钥（管理员自用测试），
#     所以撤销不打断任何外部客户。
#   * `api_keys` 表**保留不删**：request_logs 里 1641 行历史记录的 api_key_id 指着它，
#     删表会让历史日志失去归属。保留的意思是「不再读写不再展示」，不是「数据还存在意义」。
# 若日后确实需要给白名单客户开程序化通道，正确形态是重做一套**发证制**凭证，
# 而不是把这套自助注册的密钥复活。


def authenticate_wx_token():
    """校验小程序登录令牌。

    返回三态：
      (access, None) —— 令牌合法，access 形如 {"user_id": int, "id": None, "wx": True, "quota_cap": int|None}
      (None, error)  —— 令牌存在但不合法（调用方直接返回该错误）
      (None, None)   —— **没有 X-WX-Token 头**。这不是"换条路试试"，而是"没出示凭证"；
                        撤销密钥通道后调用方应据此返回 401（API_ONLY 模式除外）
    """
    token = request.headers.get("X-WX-Token", "").strip()
    if not token:
        return None, None

    db = get_db()
    row = db.execute(
        "SELECT s.token_hash, s.expires_at AS session_expires_at, u.* "
        "FROM wx_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
        (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
    ).fetchone()
    if row is None:
        return None, (401, "登录状态无效，请重新登录", "WX_TOKEN_INVALID")
    if str(row["session_expires_at"]) <= utcnow():
        return None, (401, "登录状态已过期，请重新登录", "WX_TOKEN_EXPIRED")
    # 三道检查（§2.5）：停用必须查
    if not row["active"]:
        return None, (403, "账号已被停用", "ACCOUNT_DISABLED")
    # 第二道检查换成每日额度 + 签到余额两层 —— 在 _execute_parse 里做（那里才知道今天用量）
    # 第四道：限流主体是 user:{id}。撤销密钥通道后不再有第二个主体，
    # 但**主体名逐字保持不变** —— 改名的代价是限流桶换键，老桶里的计数当场失效，
    # 而原名没有任何坏处。
    limits = [(f"user:{row['id']}", row["qps_limit"], 1)]
    exceeded = consume_rate_limits(limits)
    if exceeded:
        return None, (429, f"请求过于频繁，当前账号限制为 {row['qps_limit']} QPS", "RATE_LIMITED")

    db.execute("UPDATE wx_sessions SET last_seen_at=? WHERE token_hash=?",
               (utcnow(), row["token_hash"]))
    db.commit()
    return {
        "user_id": row["id"],
        "id": None,                      # 小程序用户没有密钥行，日志里 api_key_id 恒为 NULL
        "wx": True,
        "quota_cap": None if row["role"] == "admin" else daily_quota_cap(row),
    }, None


def platform_access(platform):
    row = get_db().execute(
        "SELECT enabled,qps_limit FROM platform_settings WHERE platform=?", (platform,)
    ).fetchone()
    if row is not None and not row["enabled"]:
        return 503, f"{platform} 接口维护中", "PLATFORM_DISABLED"
    if row is not None and row["qps_limit"]:
        if not consume_rate_limit(f"platform:{platform}", row["qps_limit"]):
            return 429, f"{platform} 接口请求过于频繁", "PLATFORM_RATE_LIMITED"
    return None


def global_api_enabled():
    return setting("global_api_enabled", "1") == "1"


def demo_enabled():
    return setting("demo_enabled", "1") == "1"


def record_request(access, platform, path, status_code, error_code, duration_ms):
    if (current_app and (current_app.testing or current_app.config.get("TESTING") or current_app.config.get("API_ONLY"))) or "unittest" in sys.modules or "pytest" in sys.modules:
        return
    db = get_db()
    db.execute(
        "INSERT INTO request_logs(user_id,api_key_id,platform,path,status_code,error_code,duration_ms,input_url,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            access["user_id"] if access else None,
            access["id"] if access else None,
            platform,
            path,
            status_code,
            error_code,
            duration_ms,
            _request_log_url(),
            utcnow(),
        ),
    )
    db.commit()
