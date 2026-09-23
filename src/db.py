import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin', 'user')),
    active INTEGER NOT NULL DEFAULT 1,
    qps_limit INTEGER NOT NULL DEFAULT 2,
    credits INTEGER NOT NULL DEFAULT 100,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    qps_limit INTEGER,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS platform_settings (
    platform TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    qps_limit INTEGER
);
CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    api_key_id INTEGER,
    platform TEXT,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    error_code TEXT,
    duration_ms INTEGER NOT NULL,
    input_url TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    subject TEXT NOT NULL,
    bucket_second INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY(subject, bucket_second)
);
CREATE INDEX IF NOT EXISTS idx_logs_created_at ON request_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_user_id ON request_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_time ON rate_limit_buckets(bucket_second);
"""


DEFAULT_FREE_DAILY_QUOTA = 10
DEFAULT_CHECKIN_BONUS = 10


# 新表与新索引单独成段：它们要在 users 的新列补齐之后才能建
# （idx_users_openid 引用的 openid 列由 _ensure_column 添加，不是 SCHEMA 自带的）
SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS wx_sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    session_key  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member_plans (
    code          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    days          INTEGER NOT NULL,
    daily_quota   INTEGER NOT NULL,
    price_fen     INTEGER,
    wx_product_id TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    sort_order    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS daily_usage (
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id, day)
);
CREATE TABLE IF NOT EXISTS wx_orders (
    out_trade_no TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    plan_code    TEXT NOT NULL,
    amount_fen   INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'created',
    wx_order_id  TEXT,
    created_at   TEXT NOT NULL,
    paid_at      TEXT,
    delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS member_card_grants (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    username            TEXT NOT NULL,
    plan_code           TEXT NOT NULL,
    plan_name           TEXT NOT NULL,
    days                INTEGER NOT NULL,
    daily_quota         INTEGER NOT NULL,
    effective_plan_code TEXT,
    expires_at          TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'admin',
    granted_by          INTEGER,
    granted_by_name     TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_card_grants_user ON member_card_grants(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_card_grants_created_at ON member_card_grants(created_at DESC);
CREATE TABLE IF NOT EXISTS ext_pairings (
    code_hash   TEXT PRIMARY KEY,   -- 配对码的 sha256，明文码绝不落库
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending=等扫码 confirmed=已确认待发放 consumed=已发放
    user_id     INTEGER,            -- 确认人（配对成功前为 NULL）
    token_hash  TEXT,               -- 发放令牌在 wx_sessions 里的 hash，对账用
    token_enc   TEXT,               -- 令牌明文的 AES-GCM 密文：status 轮询要把它原样发给插件，
                                    -- 而 DB 里只存哈希的规矩不能破（§1.3），所以短存 5 分钟密文
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_openid ON users(openid) WHERE openid IS NOT NULL;
"""


# 设计文档 §1.4：这 7 列**故意不写进 SCHEMA**。SQLite 的 CREATE TABLE IF NOT EXISTS
# 对已存在的表不生效，写进去只会让新库有线、旧库没线 —— 开发机能跑、线上报错。
# 统一由 _ensure_column 补，保证新旧库的来源一致。
NEW_USER_COLUMNS = (
    ("openid", "TEXT"),
    ("nickname", "TEXT"),
    ("avatar", "TEXT"),
    ("member_plan", "TEXT"),
    ("member_expires_at", "TEXT"),
    ("last_checkin_date", "TEXT"),
    ("checkin_streak", "INTEGER NOT NULL DEFAULT 0"),
)

MEMBER_PLAN_SEED = (
    ("day", "日卡", 1, 200, 0),
    ("month", "月卡", 30, 300, 1),
    ("quarter", "季卡", 90, 300, 2),
    ("year", "年卡", 365, 500, 3),
)


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def beijing_now():
    """返回当前北京时间，不依赖部署服务器的时区配置。"""
    return datetime.now(timezone.utc).astimezone(BEIJING_TZ)


def beijing_period_start_utc(days):
    """返回“近 N 个北京时间自然日”的起始时刻（UTC）。"""
    start_date = beijing_now().date() - timedelta(days=max(1, days) - 1)
    return datetime.combine(start_date, time.min, tzinfo=BEIJING_TZ).astimezone(timezone.utc).isoformat(timespec="seconds")


def get_db():
    if "db" not in g:
        database = current_app.config["DATABASE"]
        g.db = sqlite3.connect(database, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 10000")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA synchronous = NORMAL")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _ensure_column(db, table, column, ddl):
    """SQLite 没有 ALTER ... ADD COLUMN IF NOT EXISTS，只能先查 PRAGMA 再补列。

    只加列，不改列、不删列 —— 保持向前兼容（设计文档 §1.4）。
    """
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    db = get_db()
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.executescript(SCHEMA)
    for column, ddl in NEW_USER_COLUMNS:
        _ensure_column(db, "users", column, ddl)
    db.executescript(SCHEMA_EXTRA)
    defaults = {
        "global_api_enabled": "1",
        "homepage_enabled": "1",
        "demo_enabled": "1",
        "registration_enabled": "1",
        "default_user_qps": "2",
        "default_initial_credits": "100",
        "wx_free_daily_quota": str(DEFAULT_FREE_DAILY_QUOTA),
        "wx_checkin_bonus": str(DEFAULT_CHECKIN_BONUS),
        "api_tip_enabled": "1",
        "api_tip_author": "ucmao",
        "api_tip_website": "https://github.com/ucmao/media-parser",
        "api_tip_notice": "本接口由开源项目 media-parser 提供服务",
    }
    db.executemany(
        "INSERT OR IGNORE INTO system_settings(key, value) VALUES (?, ?)",
        defaults.items(),
    )
    # INSERT OR IGNORE：运营在后台改过的额度不会被重启覆盖
    db.executemany(
        "INSERT OR IGNORE INTO member_plans(code, name, days, daily_quota, sort_order) VALUES (?, ?, ?, ?, ?)",
        MEMBER_PLAN_SEED,
    )
    db.commit()


def reserve_user_credit(user_id):
    """原子预扣一次签到余额；True 表示已扣，False 表示无需扣，None 表示余额不足。

    2026-09-22 起它的唯一调用方是小程序路径的**第二层**（每日额度用尽后才轮到它，
    见 src/api/parse.py 与 docs/wx-login.md §1.6）。旧 API Key 路径的同名调用已随
    密钥通道一并撤销，但函数本身留下的理由更充分了：它的三态（已扣 / 无需扣 / 不足）
    正好是两层回退需要的三态，且 `BEGIN IMMEDIATE` 里那句
    `UPDATE ... WHERE credits>0` 是并发下唯一能保证余额不被扣穿的写法。
    """
    if not user_id:
        return False
    with transaction(immediate=True) as db:
        user = db.execute(
            "SELECT role, credits FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not user:
            return None
        if user["role"] == "admin" or user["credits"] == -1:
            return False
        cursor = db.execute(
            "UPDATE users SET credits=credits-1 WHERE id=? AND credits>0",
            (user_id,),
        )
        return True if cursor.rowcount == 1 else None


def refund_user_credit(user_id):
    """退回一次已预扣的普通用户积分。"""
    if not user_id:
        return
    with transaction(immediate=True) as db:
        db.execute(
            "UPDATE users SET credits=credits+1 "
            "WHERE id=? AND role!='admin' AND credits!=-1",
            (user_id,),
        )


def beijing_today():
    """北京时区自然日的日期串（YYYY-MM-DD）。额度计数的分桶键。"""
    return beijing_now().date().isoformat()


def consume_daily_quota(user_id, day, cap):
    """原子预占一次当日额度。

    返回 True = 已占；False = 当日额度已用尽。
    cap 为 None 表示不限（管理员），不计数、不写表。
    user_id 为假值同样直接放行、完全不记账 —— 这是给非微信的 token 接口
    留的口子（那条路径本来就没有用户可记）。因此调用方**必须先确认已登录**
    再调本函数，否则等于把每日硬上限变成不限。注意本函数与
    reserve_user_credit() 的约定相反：那边 user_id 为假值返回的是 False。
    """
    if not user_id or cap is None:
        # 前半段是非微信 token 路径：没有用户就不记账、直接放行。
        # 与 reserve_user_credit() 相反（那边假 user_id 返回 False），不是笔误。
        return True
    with transaction(immediate=True) as db:
        row = db.execute(
            "SELECT count FROM daily_usage WHERE user_id=? AND day=?", (user_id, day)
        ).fetchone()
        used = row["count"] if row else 0
        if used >= cap:
            return False
        db.execute(
            "INSERT INTO daily_usage(user_id,day,count) VALUES(?,?,1) "
            "ON CONFLICT(user_id,day) DO UPDATE SET count=count+1",
            (user_id, day),
        )
    return True


def refund_daily_quota(user_id, day):
    """退回一次已预占的当日额度（解析失败时调用）。下限 0，不会把计数压成负数。"""
    if not user_id:
        return
    with transaction(immediate=True) as db:
        db.execute(
            "UPDATE daily_usage SET count=count-1 WHERE user_id=? AND day=? AND count>0",
            (user_id, day),
        )


def get_daily_usage(user_id, day):
    row = get_db().execute(
        "SELECT count FROM daily_usage WHERE user_id=? AND day=?", (user_id, day)
    ).fetchone()
    return row["count"] if row else 0


def get_daily_usage_map(user_ids, day):
    """一次取多个用户的当日已用次数 → {user_id: count}。

    给后台用户列表用：那一页 20 行，逐行调 get_daily_usage 就是 20 条 SQL。
    没记录的用户**不出现在返回的字典里**，调用方用 .get(uid, 0) 取。
    """
    ids = [uid for uid in user_ids if uid]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = get_db().execute(
        f"SELECT user_id, count FROM daily_usage WHERE day=? AND user_id IN ({placeholders})",
        [day, *ids],
    ).fetchall()
    return {row["user_id"]: row["count"] for row in rows}


def parse_utc(value):
    """宽松解析 ISO 时间串；无法解析返回 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def get_member_plan(code):
    """返回生效中的卡种行；卡种不存在或已下架返回 None。"""
    if not code:
        return None
    row = get_db().execute("SELECT * FROM member_plans WHERE code=?", (code,)).fetchone()
    return row if row is not None and row["active"] else None


def is_member(user):
    """会员是否生效：卡种有效且 member_expires_at 未过期。**会员判定的唯一真相来源**。

    四个调用点：额度上限、/wx/me 展示、签到、开卡逻辑（设计文档 §2.4）。
    只看 member_expires_at —— 账号本身没有有效期概念（2026-09-22 起 users.expires_at 已废弃，
    账号的开关由 active 一个字段表达）。
    """
    if not user:
        return False
    keys = user.keys()
    plan_code = user["member_plan"] if "member_plan" in keys else None
    expires = parse_utc(user["member_expires_at"]) if "member_expires_at" in keys else None
    if not plan_code or expires is None:
        return False
    if get_member_plan(plan_code) is None:
        return False
    return expires > datetime.now(timezone.utc)


def daily_quota_cap(user, plans_by_code=None, free_quota=None):
    """当日上限：会员取卡种额度，非会员取免费档。始终返回整数。

    `plans_by_code` / `free_quota` 是给后台用户列表用的**预加载缓存**：那一页 20 行，
    每行都调本函数就会各查一次卡种表与 system_settings。列表页是最常刷新的页面，
    值得省掉。**不传就是老行为**（各查一次），单用户路径不必关心。

    缓存只省查询、不改判定：`plans_by_code` 按调用方的约定只装**生效中**的卡种，
    键不存在就照旧落回 get_member_plan()。管理员无上限那件事由调用方负责
    （access.py 传的是 `None if role=='admin'`），本函数不认角色。
    """
    if is_member(user):
        code = user["member_plan"]
        if plans_by_code is not None and code in plans_by_code:
            return int(plans_by_code[code])
        plan = get_member_plan(code)
        if plan is not None:
            return int(plan["daily_quota"])
    raw = free_quota if free_quota is not None else setting("wx_free_daily_quota", DEFAULT_FREE_DAILY_QUOTA)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_FREE_DAILY_QUOTA


def open_member_card(user_id, plan_code):
    """开卡/续期。返回 (是否成功, 提示文案, 新到期时间)。

    运营手动开卡与第二期支付回调共用这一个函数（设计文档 §2.4）：
      · 到期时间叠加：新到期 = max(now, 原到期) + 卡种时长，续费不吞剩余时间
      · 额度只升不降：已持更高档且未到期时保留原卡种
    """
    with transaction(immediate=True) as db:
        plan = db.execute(
            "SELECT * FROM member_plans WHERE code=? AND active=1", (plan_code,)
        ).fetchone()
        if plan is None:
            return False, "卡种不存在或已下架", None

        user = db.execute(
            "SELECT id, member_plan, member_expires_at FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if user is None:
            return False, "用户不存在", None

        now = datetime.now(timezone.utc)
        current_expires = parse_utc(user["member_expires_at"])
        base = current_expires if current_expires and current_expires > now else now
        new_expires = base + timedelta(days=int(plan["days"]))

        # 必须带 active=1：与上面取新卡种用的是同一个「生效」定义。已下架的旧卡种
        # 兑现不了任何额度（is_member 走 get_member_plan 会返回 None），拿它的额度去
        # 比大小只会把用户锁死在免费档 —— 保留它严格劣于换成刚买的卡。
        current_plan = db.execute(
            "SELECT daily_quota FROM member_plans WHERE code=? AND active=1", (user["member_plan"],)
        ).fetchone() if user["member_plan"] else None
        keep_current = (
            current_expires is not None
            and current_expires > now
            and current_plan is not None
            and int(current_plan["daily_quota"]) > int(plan["daily_quota"])
        )
        stored_plan = user["member_plan"] if keep_current else plan["code"]

        db.execute(
            "UPDATE users SET member_plan=?, member_expires_at=? WHERE id=?",
            (stored_plan, new_expires.isoformat(timespec="seconds"), user_id),
        )
        return True, f"已开通{plan['name']}", new_expires.isoformat(timespec="seconds")


def record_member_card_grant(user_id, plan_code, expires_at, granted_by=None, source="admin"):
    """写一条开卡记录，返回新行 id；写不进去返回 None。**开卡成功后**由调用方调。

    这里记的是「**这一次开出的卡**」，不是「用户现在持有什么」—— 两者在
    「已持更高档」时并不相同（open_member_card 的额度只升不降会把原卡种留下），
    所以 effective_plan_code 单独一列，免得日后照这条记录误判用户档位。

    卡种名称 / 天数 / 额度都是**快照**：卡种表可改可下架，不存快照的话，
    运营改一次价，历史记录的含义就跟着变了 —— 那正是「对不上账」的来源。
    下面查卡种**故意不带 active=1**：记的是刚刚开出去的那张卡，运营下一秒把它
    下架，这条记录也必须还能叫出它的名字。

    审计写失败**不回滚开卡**：卡已经开出并提交了，为了记不上账而把它撤销，
    比少一行记录更糟。调用方拿到 None 记一条日志即可。
    """
    db = get_db()
    user = db.execute(
        "SELECT username, member_plan FROM users WHERE id=?", (user_id,)
    ).fetchone()
    plan = db.execute(
        "SELECT name, days, daily_quota FROM member_plans WHERE code=?", (plan_code,)
    ).fetchone()
    if user is None or plan is None:
        return None
    operator = db.execute(
        "SELECT username FROM users WHERE id=?", (granted_by,)
    ).fetchone() if granted_by else None
    cursor = db.execute(
        "INSERT INTO member_card_grants(user_id,username,plan_code,plan_name,days,daily_quota,"
        "effective_plan_code,expires_at,source,granted_by,granted_by_name,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            user_id, user["username"], plan_code, plan["name"],
            int(plan["days"]), int(plan["daily_quota"]), user["member_plan"],
            expires_at, source, granted_by,
            operator["username"] if operator else None, utcnow(),
        ),
    )
    db.commit()
    return cursor.lastrowid


def setting(name, default=None):
    row = get_db().execute(
        "SELECT value FROM system_settings WHERE key = ?", (name,)
    ).fetchone()
    return row["value"] if row else default


def set_setting(name, value):
    get_db().execute(
        "INSERT INTO system_settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (name, str(value)),
    )


@contextmanager
def transaction(immediate=False):
    db = get_db()
    db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()


def get_daily_trend(user_id=None, days=7):
    db = get_db()
    today = beijing_now().date()
    
    # 确定实际天数 (days=0 表示全周期)
    if days == 0:
        sql_min = "SELECT MIN(date(datetime(created_at, '+8 hours'))) as min_date FROM request_logs "
        params_min = []
        if user_id is not None:
            sql_min += "WHERE user_id = ? "
            params_min.append(user_id)
        row_min = db.execute(sql_min, params_min).fetchone()
        if row_min and row_min["min_date"]:
            try:
                min_date = datetime.strptime(row_min["min_date"], "%Y-%m-%d").date()
                total_days = max(7, (today - min_date).days + 1)
            except ValueError:
                total_days = 7
        else:
            total_days = 7
    else:
        total_days = max(1, days)

    sql = (
        "SELECT date(datetime(created_at, '+8 hours')) as day, "
        "COUNT(*) as calls, "
        "SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) as successes "
        "FROM request_logs "
    )
    params = []
    where_clauses = []
    if days > 0:
        where_clauses.append("created_at >= ?")
        params.append(beijing_period_start_utc(total_days))
    if user_id is not None:
        where_clauses.append("user_id = ?")
        params.append(user_id)

    if where_clauses:
        sql += "WHERE " + " AND ".join(where_clauses) + " "
    sql += "GROUP BY day ORDER BY day ASC"

    rows = db.execute(sql, params).fetchall()
    row_map = {row["day"]: row for row in rows if row["day"]}

    daily_stats = []
    max_val = 0
    for i in range(total_days - 1, -1, -1):
        day_date = today - timedelta(days=i)
        day_str = day_date.strftime("%Y-%m-%d")
        short_date = day_date.strftime("%m-%d")
        item = row_map.get(day_str)
        calls = item["calls"] if item and item["calls"] else 0
        successes = item["successes"] if item and item["successes"] else 0
        if calls > max_val:
            max_val = calls
        daily_stats.append({
            "date": day_str,
            "short_date": short_date,
            "calls": calls,
            "successes": successes,
            "failures": max(0, calls - successes),
        })

    num_points = len(daily_stats)
    # 计算 X 轴文字抽样频率
    if num_points <= 10:
        step = 1
    elif num_points <= 31:
        step = 5
    elif num_points <= 90:
        step = 15
    elif num_points <= 180:
        step = 30
    else:
        step = 60

    for idx, item in enumerate(daily_stats):
        item["show_label"] = (idx % step == 0) or (idx == num_points - 1)

    def _calc_nice_ticks(mv, top_m=20, h=160):
        if mv <= 0:
            ym = 10
            raw_ticks = [10, 8, 6, 4, 2, 0]
        elif mv <= 3:
            ym = 3
            raw_ticks = [3, 2, 1, 0]
        elif mv <= 5:
            ym = 5
            raw_ticks = [5, 4, 3, 2, 1, 0]
        elif mv <= 8:
            ym = 8
            raw_ticks = [8, 6, 4, 2, 0]
        elif mv <= 12:
            ym = 12
            raw_ticks = [12, 9, 6, 3, 0]
        elif mv <= 15:
            ym = 15
            raw_ticks = [15, 12, 9, 6, 3, 0]
        elif mv <= 20:
            ym = 20
            raw_ticks = [20, 15, 10, 5, 0]
        elif mv <= 30:
            ym = 30
            raw_ticks = [30, 24, 18, 12, 6, 0]
        elif mv <= 50:
            ym = math.ceil(mv / 10) * 10
            step = 10 if ym <= 30 else (ym // 5)
            raw_ticks = list(range(ym, -1, -step))
            if raw_ticks[-1] != 0:
                raw_ticks.append(0)
        elif mv <= 100:
            ym = math.ceil(mv / 10) * 10
            step = max(10, ym // 5)
            raw_ticks = list(range(ym, -1, -step))
            if raw_ticks[-1] != 0:
                raw_ticks.append(0)
        else:
            mag = 10 ** math.floor(math.log10(mv))
            norm = mv / mag
            if norm <= 1.5:
                step = int(0.25 * mag) if mag >= 10 else 1
                ym = int(1.5 * mag)
            elif norm <= 2.0:
                step = int(0.4 * mag) if mag >= 10 else 1
                ym = int(2.0 * mag)
            elif norm <= 5.0:
                step = int(0.5 * mag) if mag >= 10 else 1
                ym = int(math.ceil(norm) * mag)
            else:
                step = int(1.0 * mag) if mag >= 10 else 1
                ym = int(math.ceil(norm / 2) * 2 * mag)
            raw_ticks = list(range(ym, -1, -step))
            if raw_ticks[-1] != 0:
                raw_ticks.append(0)

        ticks = []
        for v in raw_ticks:
            y_pos = round(top_m + h * (1 - v / ym), 1)
            ticks.append({
                "val": f"{v:,}",
                "raw_val": v,
                "y": y_pos,
            })
        return ym, ticks

    def _points_to_bezier_path(coords, min_y=20, max_y=180, tension=0.2):
        if not coords:
            return ""
        if len(coords) == 1:
            return f"M {coords[0][0]:.1f} {coords[0][1]:.1f}"
        if len(coords) == 2:
            return f"M {coords[0][0]:.1f} {coords[0][1]:.1f} L {coords[1][0]:.1f} {coords[1][1]:.1f}"

        path = [f"M {coords[0][0]:.1f} {coords[0][1]:.1f}"]
        n = len(coords)
        for i in range(n - 1):
            p0 = coords[max(0, i - 1)]
            p1 = coords[i]
            p2 = coords[i + 1]
            p3 = coords[min(n - 1, i + 2)]

            if p1[1] == max_y and p2[1] == max_y:
                path.append(f"L {p2[0]:.1f} {p2[1]:.1f}")
                continue

            cp1x = p1[0] + (p2[0] - p0[0]) * tension
            cp1y = max(min_y, min(max_y, p1[1] + (p2[1] - p0[1]) * tension))
            cp2x = p2[0] - (p3[0] - p1[0]) * tension
            cp2y = max(min_y, min(max_y, p2[1] - (p3[1] - p1[1]) * tension))

            path.append(f"C {cp1x:.1f} {cp1y:.1f}, {cp2x:.1f} {cp2y:.1f}, {p2[0]:.1f} {p2[1]:.1f}")
        return " ".join(path)

    width = 620
    height = 160
    left_margin = 50
    top_margin = 20
    bottom_y = top_margin + height

    y_max, y_ticks = _calc_nice_ticks(max_val, top_margin, height)

    coords_calls = []
    coords_successes = []
    points_calls = []
    points_successes = []

    for i, item in enumerate(daily_stats):
        x = round(left_margin + i * (width / max(1, num_points - 1)), 1)
        y_c = round(top_margin + height * (1 - item["calls"] / y_max), 1)
        y_s = round(top_margin + height * (1 - item["successes"] / y_max), 1)
        item["x"] = x
        item["y_calls"] = y_c
        item["y_successes"] = y_s
        coords_calls.append((x, y_c))
        coords_successes.append((x, y_s))
        points_calls.append(f"{x},{y_c}")
        points_successes.append(f"{x},{y_s}")

    calls_path = _points_to_bezier_path(coords_calls, min_y=top_margin, max_y=bottom_y)
    successes_path = _points_to_bezier_path(coords_successes, min_y=top_margin, max_y=bottom_y)

    first_x = daily_stats[0]["x"]
    last_x = daily_stats[-1]["x"]

    calls_area_path = f"{calls_path} L {last_x:.1f} {bottom_y:.1f} L {first_x:.1f} {bottom_y:.1f} Z"
    successes_area_path = f"{successes_path} L {last_x:.1f} {bottom_y:.1f} L {first_x:.1f} {bottom_y:.1f} Z"

    calls_line = " ".join(points_calls)
    successes_line = " ".join(points_successes)
    calls_area = f"{first_x},{bottom_y} {calls_line} {last_x},{bottom_y}"
    successes_area = f"{first_x},{bottom_y} {successes_line} {last_x},{bottom_y}"

    return {
        "trend": daily_stats,
        "max_val": max_val,
        "y_max": y_max,
        "calls_path": calls_path,
        "successes_path": successes_path,
        "calls_area_path": calls_area_path,
        "successes_area_path": successes_area_path,
        "calls_line": calls_line,
        "successes_line": successes_line,
        "calls_area": calls_area,
        "successes_area": successes_area,
        "y_ticks": y_ticks,
    }


def get_platform_distribution(user_id=None, days=7):
    db = get_db()
    sql = (
        "SELECT COALESCE(NULLIF(platform, ''), '未知平台') as platform_name, "
        "COUNT(*) as calls, "
        "SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) as successes "
        "FROM request_logs "
    )
    params = []
    where_clauses = []
    if days > 0:
        where_clauses.append("created_at >= ?")
        params.append(beijing_period_start_utc(days))
    if user_id is not None:
        where_clauses.append("user_id = ?")
        params.append(user_id)

    if where_clauses:
        sql += "WHERE " + " AND ".join(where_clauses) + " "
    sql += "GROUP BY platform_name ORDER BY calls DESC"

    rows = db.execute(sql, params).fetchall()
    total_calls = sum(r["calls"] for r in rows) if rows else 0

    PALETTE = [
        "#4f46e5", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6",
        "#06b6d4", "#ef4444", "#3b82f6", "#14b8a6", "#f97316",
        "#6366f1", "#84cc16", "#d946ef", "#0284c7", "#e11d48",
        "#7c3aed", "#059669", "#d97706", "#2563eb", "#db2777",
        "#0891b2", "#ea580c", "#475569", "#65a30d", "#9333ea",
        "#0d9488", "#c026d3", "#4338ca", "#16a34a", "#ca8a04",
        "#be123c", "#1d4ed8", "#b91c1c", "#6d28d9", "#0f766e",
        "#c2410c", "#334155", "#4d7c0f", "#86198f", "#1e40af",
        "#991b1b", "#581c87", "#115e59", "#9a3412", "#1e293b",
        "#3f6212", "#701a75", "#1e3a8a", "#831843", "#312e81",
        "#064e3b", "#78350f", "#0f172a", "#365314", "#4a044e"
    ]

    items = []
    if total_calls > 0:
        cx, cy = 100, 100
        r_out, r_in = 88, 64
        current_angle = 0.0  # radians

        for idx, row in enumerate(rows):
            name = row["platform_name"]
            calls = row["calls"]
            successes = row["successes"] or 0
            failures = max(0, calls - successes)
            success_rate = round((successes / calls) * 100, 1) if calls > 0 else 0.0
            percentage = round((calls / total_calls) * 100, 1)
            color = PALETTE[idx % len(PALETTE)]

            slice_angle = (calls / total_calls) * 2 * math.pi
            # 防止刚好 360 度圆环闭合异常
            if slice_angle >= 2 * math.pi - 1e-4:
                slice_angle = 2 * math.pi - 1e-4

            start_angle = current_angle
            end_angle = current_angle + slice_angle
            current_angle = end_angle

            x1_out = cx + r_out * math.sin(start_angle)
            y1_out = cy - r_out * math.cos(start_angle)
            x2_out = cx + r_out * math.sin(end_angle)
            y2_out = cy - r_out * math.cos(end_angle)

            x2_in = cx + r_in * math.sin(end_angle)
            y2_in = cy - r_in * math.cos(end_angle)
            x1_in = cx + r_in * math.sin(start_angle)
            y1_in = cy - r_in * math.cos(start_angle)

            large_arc = 1 if slice_angle > math.pi else 0

            path_d = (
                f"M {x1_out:.2f} {y1_out:.2f} "
                f"A {r_out} {r_out} 0 {large_arc} 1 {x2_out:.2f} {y2_out:.2f} "
                f"L {x2_in:.2f} {y2_in:.2f} "
                f"A {r_in} {r_in} 0 {large_arc} 0 {x1_in:.2f} {y1_in:.2f} Z"
            )

            items.append({
                "platform": name,
                "calls": calls,
                "calls_formatted": f"{calls:,}",
                "successes": successes,
                "successes_formatted": f"{successes:,}",
                "failures": failures,
                "failures_formatted": f"{failures:,}",
                "success_rate": success_rate,
                "percentage": percentage,
                "color": color,
                "path_d": path_d,
            })

    total_successes = sum(r["successes"] or 0 for r in rows) if rows else 0
    total_failures = max(0, total_calls - total_successes)
    total_success_rate = round((total_successes / total_calls) * 100, 1) if total_calls > 0 else 0.0

    return {
        "total_calls": total_calls,
        "total_calls_formatted": f"{total_calls:,}",
        "total_successes": total_successes,
        "total_successes_formatted": f"{total_successes:,}",
        "total_failures": total_failures,
        "total_failures_formatted": f"{total_failures:,}",
        "total_success_rate": total_success_rate,
        "platform_count": len(rows),
        "items": items,
    }


def get_top_users(days=7, limit=10):
    db = get_db()
    sql = (
        "SELECT u.username, COUNT(l.id) as calls "
        "FROM request_logs l "
        "JOIN users u ON u.id = l.user_id "
    )
    params = []
    if days > 0:
        sql += "WHERE l.created_at >= ? "
        params.append(beijing_period_start_utc(days))
    sql += "GROUP BY u.id ORDER BY calls DESC LIMIT ?"
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    max_calls = max((r["calls"] for r in rows), default=1)
    return [
        {
            "username": r["username"],
            "calls": r["calls"],
            "pct": round((r["calls"] / max_calls) * 100, 1),
        }
        for r in rows
    ]


