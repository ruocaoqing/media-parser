# 微信登录 / 会员 / 每日额度 实施计划 (WeChat Login & Membership Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让小程序用户通过微信静默登录获得各自的服务端账号，额度从「客户端本地计数 + 一把公开的管理员密钥」换成「服务端每日额度硬上限 + `X-WX-Token`」，并落地四档会员卡（日/月/季/年）与运营手动开卡能力。

**Architecture:** 不新增服务。`users` 表加 7 列寄生微信用户（`openid` 唯一），新增 `wx_sessions` / `member_plans` / `daily_usage` / `wx_orders` 四表；`src/api/wx.py` 提供登录/资料/签到/头像，`src/api/access.py` 增加与 API Key 并列的 `authenticate_wx_token()`，`/api/v1/parse` 变为「先试令牌、再试密钥」的双路鉴别；额度判定用 `BEGIN IMMEDIATE` 内的原子预占（新表 `daily_usage`），失败退回。客户端退化为纯展示层：所有数字由 `/wx/me` 下发，本地不再实现任何额度规则。

**Tech Stack:** Flask 3 + Blueprint 工厂、SQLite（WAL）、pycryptodome 3.20.0（AES-GCM + HKDF-SHA256）、微信小程序基础库 3.10.1（`wx.login` / `chooseAvatar` / `type=nickname`）。

**Spec:** `docs/wx-login.md`

---

## Global Constraints

以下约束来自设计文档，**每个任务的要求都隐含包含本节**，不要因为任务描述没重复就略过。

1. **客户端不再实现任何额度规则，只显示服务端给的数字**（§3.4）。客户端保留的唯一"额度逻辑"是显示格式化；任何形如"今天还能用几次"的计算都必须由服务端算好放进 `/wx/me` 的 `remaining_today`。
2. **令牌路径必须照抄密钥路径的四道检查**（§2.5）：`active = 1`、账号未过期（`user_is_expired`）、~~`credits <= 0` 前置判定~~ → **换成每日额度判定**（不是叠加）、限流。
3. **限流主体逐字复用 `f"user:{user_id}"`**（§2.5）。不要另起 `wx:{user_id}` —— `SQLiteRateLimiter` 按 subject 字符串分桶，换个名字等于 QPS 额度翻倍。
4. **🔴 不得用 `expires_at` 表达会员到期**（§2.4）。`expires_at` 是**账号有效期**，`user_is_expired()` 与 `access.py:133` 拿它做 403 `ACCOUNT_EXPIRED` 硬拒绝，且 `NULL` = "账号永久有效"。会员必须走 `member_plan` + `member_expires_at`。
5. **建微信用户行必须显式写 `credits = 0`**（§6.2）。`users.credits` 的列默认值是 100，`INSERT` 里省略就是白送 100 次。
6. **额度判定必须原子**（§1.6）：读计数与写计数在同一个 `BEGIN IMMEDIATE` 事务内。不要为了"省一张表"去数 `request_logs`。
7. **`API Key` 路径的 credits 付费墙行为必须与改造前逐字一致**（§4.4）。既有 Web 客户、`API_ONLY=true` 微服务模式、`docs/api.md` 的公开契约都不动。
8. **`DAILY_QUOTA_EXCEEDED` 与余额不足是两个不同的错误码**（§2.3）：前者"明天会恢复"，后者"要充值"。文案与引导都不得合并。
9. **本项目沿用「先红后绿」的测试纪律**（§4）：`tests/` 用 `unittest` + `create_app({...})` 临时库；`scripts/*.js` 用 `[ok]` / `[FAIL]` 打印并以退出码 0 / 1 结束。`scripts/verify-theme-c.js` 是项目的活体记录，按源码文本断言 —— 现实变了就**先让它红、再改断言**，并在断言旁写下变更理由。
10. **提交纪律**：本计划每个任务末尾都有一 `git commit` 步骤。`media-parser` 是 git 仓库，但**只在用户明确授权提交时执行**；未获授权时跳过该步骤并在任务末尾标注「未提交」。绝不使用 `--no-verify`。
    **另外：`D:\ClaudeCodeProject\DeMark` 不是 git 仓库**（在那里跑 `git rev-parse` 会直接失败）。所以客户端侧 ——
    凡是在 DeMark 里动手的步骤 —— 里面的 `git add` / `git rm` / `git commit` **一律跳过**，报告里写「未提交」；
    删文件用 `Remove-Item`（或 `rm`），没有 `git rm`。客户端改动的记录方式是**文件清单 + md5**（DeMark 没有版本历史，
    所以「改了什么」只能这么记），与 `media-parser` 侧记 tree sha 的做法对应。
11. **`media-parser` 里不新增第三方依赖**：AES-GCM 与 HKDF 用已在 `requirements.txt` 的 `pycryptodome==3.20.0`（`from Crypto.…`），不要引入 `cryptography`。

---

## 计划补充与偏离说明

写计划过程中发现两处设计文档没说清或说不通的地方。**都没有偷偷改**，列在这里请你确认：

| # | 设计文档原文 | 本计划的做法 | 为什么 |
| :--- | :--- | :--- | :--- |
| A | §3.2「`utils/stats.js` 降级为展示层：不再本地累加，数据由 `/wx/me` 提供」 | `stats.js` **保留**本地累加（历史页与我的页的累计/本周统计仍用它），但 `quota.js` 不再读它；我的页「今日已用」改读服务端的 `used_today` | §2.2 的 `/wx/me` 快照里**只有 `used_today`，没有累计次数与本周次数**。文档要求的"数据由 /wx/me 提供"缺字段。两个选择：(1) 累计/周统计留在本地（本计划采用，它是纯展示，不参与任何判定）；(2) 给 `/wx/me` 加 `total_count` / `week_count` 两个字段（要扫 `request_logs`，离开本次范围）。若你要 (2)，说一声，我给 Task 6 加字段与用例 |
| B | §6.2「关掉门户里用户自助创建/管理密钥的那套 UI」 | 只关**创建**（含每用户 10 把的上限与创建表单），**保留**密钥列表与批量启用/停用/删除 | 你在需求里选的是「保留表，关掉用户自助**创建**」。批量管理不产生新授权入口，且删掉它会连带作废既有的 `tests/test_management.py::test_portal_batch_keys_select_all`（`/console/keys/batch`）。要一并关掉的话说一声，我把那条测试改成断言 404 |
| C | 设计文档没提 `/wx/login` 的滥用防护 | `/wx/login` 加一条按 IP 的限流（10 次/分钟） | `code2Session` 在微信侧有调用额度，一个空转的客户端能把它打爆 —— §3.1 提示过同一个风险，只是只防了客户端侧。这一条**超出设计文档**，你可以直接让我删掉 |

---

## File Structure

**服务端（`D:\ClaudeCodeProject\media-parser`）**

| 文件 | 动作 | 职责 |
| :--- | :--- | :--- |
| `src/db.py` | 修改 | 加 7 列 + 4 表 + 1 部分唯一索引、轻量迁移 `_ensure_column`、卡种播种；新增 `beijing_today` / `parse_utc` / `consume_daily_quota` / `refund_daily_quota` / `get_daily_usage` / `get_member_plan` / `is_member` / `daily_quota_cap` / `open_member_card` |
| `src/wx_crypto.py` | **新增** | HKDF-SHA256 派生 + AES-GCM 加解密 `session_key` |
| `src/api/wx.py` | **新增** | 微信侧全部路由：`/login` `/me` `/profile` `/checkin` `/avatar/<id>`，以及 `code2session()` 与响应快照 `_user_snapshot()` |
| `src/api/access.py` | 修改 | 新增 `authenticate_wx_token()`（与 `authenticate_api_key()` 并列） |
| `src/api/parse.py` | 修改 | `/v1/parse` 双路鉴别；`_execute_parse` 的额度预占分支 |
| `app.py` | 修改 | 读 `WX_APPID` / `WX_APPSECRET` 环境变量；注册 `wx_bp`（**在 `API_ONLY` 判断之外**） |
| `docker-compose.yml` | 修改 | 把 `WX_APPID` / `WX_APPSECRET` 加进 `environment:` —— 该文件**没有 `env_file:`**，是逐项白名单，漏了就永远 503 |
| `.env.example` | 修改 | 记录上面两个变量及取值来源 |
| `src/web/admin.py` | 修改 | 开卡路由、卡种管理路由、`wx_free_daily_quota` / `wx_checkin_bonus` 设置项 |
| `src/web/portal.py` | 修改 | 关掉自助建密钥路由 |
| `templates/admin/users.html` | 修改 | 开卡弹窗 + 行内「开卡」入口 |
| `templates/admin/member_plans.html` | **新增** | 四档卡编辑页 |
| `templates/admin/settings.html` | 修改 | 免费档每日额度、签到奖励输入框 |
| `templates/base_console.html` | 修改 | 侧栏加「会员卡」入口 |
| `templates/portal/keys.html` | 修改 | 去掉创建入口（按钮 `:63` / 弹窗 `:185` / JS `:251` 三件套） |
| `tests/test_wx_login.py` | **新增** | 本期全部服务端用例（各任务按序追加） |

**客户端（`D:\ClaudeCodeProject\DeMark`）**

| 文件 | 动作 | 职责 |
| :--- | :--- | :--- |
| `miniprogram/utils/errors.js` | **新增** | `ERROR_MESSAGES` 错误码文案表（从 `parser.js` 原样搬出）—— 单独成模块是为了断开 `auth.js` ↔ `parser.js` 的循环 require |
| `miniprogram/utils/auth.js` | **新增** | `ensureLogin()` / `request()` / `clearToken()` / `readToken()`；`ERROR_MESSAGES` 取自 `utils/errors.js`，**不得** require `parser.js` |
| `miniprogram/utils/parser.js` | 修改 | `parseShareLink` 改走 `auth.request`；补 `DAILY_QUOTA_EXCEEDED` 文案；改从 `./errors` 取 `ERROR_MESSAGES` 并原样转出 |
| `miniprogram/utils/quota.js` | 重写 | `getQuota()` 同步读缓存 + `fetchQuota()` 异步取服务端快照 |
| `miniprogram/utils/member.js` | **删除** | 本地假会员状态整体移除，会员改由 `/wx/me` 提供 |
| `miniprogram/utils/stats.js` | 不动 | 见「计划补充与偏离说明 A」 |
| `miniprogram/config.js` | 修改 | 删 `ENFORCE_QUOTA`；`FREE_DAILY_QUOTA` / `SIGN_IN_BONUS` 降级为「服务端不可达时的显示兜底」；最后一步删 `API_KEY` |
| `miniprogram/pages/index/index.js` | 修改 | `onShow` 走 `fetchQuota()`；去掉本地额度拦截；`402` 交给引导弹窗 |
| `miniprogram/pages/my/my.js` + `.wxml` | 修改 | 服务端快照、服务端签到、完善资料（头像+昵称）、会员区 |
| `miniprogram/pages/member/member.js` + `.wxml` | 修改 | 展示服务端四档卡（只展示、不可购买） |
| `scripts/test-auth.js` | **新增** | `auth.js` 的行为用例（stub `wx.login` / `wx.request`） |
| `scripts/verify-theme-c.js` | 修改 | 额度与页面断言先红后改 |

任务分两批，**服务端（Task 1–11）自成一批**：跑完 Task 11，服务端全量 `pytest` 应当全绿，此时旧的 API Key 通道一行未坏，可以安全上线观察。

---

## Task 1: 轻量迁移基础设施 + 四张新表 + 卡种播种

**Files:**
- Modify: `src/db.py:10-62`（`SCHEMA` 之后追加 `SCHEMA_EXTRA`）、`src/db.py:101-123`（`init_db`）
- Test: `tests/test_wx_login.py`（新建）

**Interfaces:**
- Produces:
  - `SCHEMA_EXTRA: str` —— 4 张新表 + `idx_users_openid` 部分唯一索引
  - `NEW_USER_COLUMNS: tuple[tuple[str, str], ...]` —— 7 个新列的 `(列名, DDL)` 二元组
  - `MEMBER_PLAN_SEED: tuple[tuple[str, str, int, int, int], ...]` —— `(code, name, days, daily_quota, sort_order)`
  - `DEFAULT_FREE_DAILY_QUOTA = 10`、`DEFAULT_CHECKIN_BONUS = 10`
  - `_ensure_column(db, table, column, ddl) -> None`
  - `init_db()` 执行顺序保证：`SCHEMA` → `_ensure_column` × 7 → `SCHEMA_EXTRA` → 默认值 → 卡种播种

- [ ] **Step 1: 写失败测试**

新建 `tests/test_wx_login.py`：

```python
import os
import tempfile
import unittest

from app import create_app
from src.db import get_db, init_db


class WxMigrationTest(unittest.TestCase):
    """设计文档 §1.2–§1.4：加列、加表、加索引都必须可重复执行。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_app(self):
        return create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": self.db_path,
        })

    def test_init_db_adds_columns_tables_and_index(self):
        app = self.make_app()
        with app.app_context():
            init_db()
            columns = {row["name"] for row in get_db().execute("PRAGMA table_info(users)")}
            for name in ("openid", "nickname", "avatar", "member_plan",
                         "member_expires_at", "last_checkin_date", "checkin_streak"):
                self.assertIn(name, columns)

            tables = {row["name"] for row in get_db().execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for name in ("wx_sessions", "member_plans", "daily_usage", "wx_orders"):
                self.assertIn(name, tables)

            indexes = {row["name"] for row in get_db().execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("idx_users_openid", indexes)

    def test_init_db_is_idempotent_on_existing_database(self):
        app = self.make_app()
        with app.app_context():
            init_db()
            init_db()
            init_db()  # 第三次也不该抛「duplicate column name」

    def test_member_plan_seed_does_not_overwrite_edited_values(self):
        app = self.make_app()
        with app.app_context():
            init_db()
            db = get_db()
            db.execute("UPDATE member_plans SET daily_quota=999 WHERE code='day'")
            db.commit()
            init_db()
            row = db.execute("SELECT daily_quota FROM member_plans WHERE code='day'").fetchone()
            self.assertEqual(row["daily_quota"], 999)

    def test_seeded_plans_match_the_spec_table(self):
        app = self.make_app()
        with app.app_context():
            init_db()
            rows = {row["code"]: row for row in get_db().execute("SELECT * FROM member_plans")}
            self.assertEqual(sorted(rows), ["day", "month", "quarter", "year"])
            self.assertEqual((rows["day"]["name"], rows["day"]["days"], rows["day"]["daily_quota"]),
                             ("日卡", 1, 200))
            self.assertEqual((rows["month"]["name"], rows["month"]["days"], rows["month"]["daily_quota"]),
                             ("月卡", 30, 300))
            self.assertEqual((rows["quarter"]["name"], rows["quarter"]["days"], rows["quarter"]["daily_quota"]),
                             ("季卡", 90, 300))
            self.assertEqual((rows["year"]["name"], rows["year"]["days"], rows["year"]["daily_quota"]),
                             ("年卡", 365, 500))

    def test_openid_unique_index_blocks_duplicates_but_allows_nulls(self):
        app = self.make_app()
        with app.app_context():
            init_db()
            db = get_db()
            insert_wx = ("INSERT INTO users(username,password_hash,role,active,credits,openid,created_at) "
                         "VALUES(?,?,?,?,?,?,?)")
            db.execute(insert_wx, ("wx_a", "x", "user", 1, 0, "openid-1", "2026-01-01T00:00:00+00:00"))
            db.commit()
            with self.assertRaises(Exception):
                db.execute(insert_wx, ("wx_b", "x", "user", 1, 0, "openid-1", "2026-01-01T00:00:00+00:00"))
                db.commit()
            db.rollback()

            insert_web = ("INSERT INTO users(username,password_hash,role,active,credits,created_at) "
                          "VALUES(?,?,?,?,?,?)")
            db.execute(insert_web, ("web_1", "x", "user", 1, 0, "2026-01-01T00:00:00+00:00"))
            db.execute(insert_web, ("web_2", "x", "user", 1, 0, "2026-01-01T00:00:00+00:00"))
            db.commit()
            count = db.execute("SELECT COUNT(*) c FROM users WHERE openid IS NULL").fetchone()["c"]
            self.assertEqual(count, 2)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py -v`
Expected: FAIL —— `openid` 不在 `PRAGMA table_info(users)` 里（`KeyError` 不会出现，是 `assertIn` 失败）；`wx_sessions` 等 4 张表也不存在。

- [ ] **Step 3: 写实现**

在 `src/db.py` 的 `BEIJING_TZ = ZoneInfo("Asia/Shanghai")` **之前**插入：

```python
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
```

在 `src/db.py` 里 `init_db()` 之前加迁移函数：

```python
def _ensure_column(db, table, column, ddl):
    """SQLite 没有 ALTER ... ADD COLUMN IF NOT EXISTS，只能先查 PRAGMA 再补列。

    只加列，不改列、不删列 —— 保持向前兼容（设计文档 §1.4）。
    """
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
```

把 `init_db()` 改成：

```python
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
        "default_trial_days": "365",
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 跑全量回归**

Run: `python -m pytest -q`
Expected: 全绿。既有用例建库时走的是同一个 `init_db()`，新列全可空，不应影响任何断言。

- [ ] **Step 6: Commit**

```bash
git add src/db.py tests/test_wx_login.py
git commit -m "feat(db): add wechat columns, tables and member plan seed

设计文档 §1.2-§1.4：users 加 7 列（openid 唯一）、新增 wx_sessions /
member_plans / daily_usage / wx_orders，以及可重复执行的轻量迁移。"
```

---

## Task 2: 每日额度原子预占与退回

**Files:**
- Modify: `src/db.py`（在 `refund_user_credit` 之后追加）
- Test: `tests/test_wx_login.py`（追加 `DailyQuotaTest`）

**Interfaces:**
- Consumes: `transaction(immediate=True)`（已有）、`daily_usage` 表（Task 1）、`utcnow()`（已有）
- Produces:
  - `beijing_today() -> str` —— 北京自然日的 `YYYY-MM-DD`
  - `consume_daily_quota(user_id, day, cap) -> bool` —— `True` 已占；`False` 当日已用尽；`cap is None` 视为不限，直接 `True`
  - `refund_daily_quota(user_id, day) -> None` —— 计数减一，下限 0
  - `get_daily_usage(user_id, day) -> int`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from zoneinfo import ZoneInfo

from src.db import (
    beijing_now,
    beijing_today,
    consume_daily_quota,
    get_daily_usage,
    refund_daily_quota,
    utcnow,
)


class DailyQuotaTest(unittest.TestCase):
    """设计文档 §1.6：硬上限必须原子预占，否则并发请求会一起放行。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
        })
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,role,active,credits,openid,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("wx_alice", "x", "user", 1, 0, "openid-alice", utcnow()),
            )
            self.user_id = cursor.lastrowid
            db.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cap_is_enforced_exactly_at_the_boundary(self):
        day = "2026-09-22"
        with self.app.app_context():
            for _ in range(3):
                self.assertTrue(consume_daily_quota(self.user_id, day, 3))
            self.assertFalse(consume_daily_quota(self.user_id, day, 3))
            self.assertFalse(consume_daily_quota(self.user_id, day, 3))
            self.assertEqual(get_daily_usage(self.user_id, day), 3)

    def test_none_cap_means_unlimited_and_does_not_write(self):
        day = "2026-09-22"
        with self.app.app_context():
            for _ in range(5):
                self.assertTrue(consume_daily_quota(self.user_id, day, None))
            self.assertEqual(get_daily_usage(self.user_id, day), 0)

    def test_days_are_independent(self):
        with self.app.app_context():
            self.assertTrue(consume_daily_quota(self.user_id, "2026-09-22", 1))
            self.assertFalse(consume_daily_quota(self.user_id, "2026-09-22", 1))
            self.assertTrue(consume_daily_quota(self.user_id, "2026-09-23", 1))

    def test_refund_returns_the_reservation_and_never_goes_negative(self):
        day = "2026-09-22"
        with self.app.app_context():
            consume_daily_quota(self.user_id, day, 5)
            refund_daily_quota(self.user_id, day)
            self.assertEqual(get_daily_usage(self.user_id, day), 0)
            refund_daily_quota(self.user_id, day)
            self.assertEqual(get_daily_usage(self.user_id, day), 0)

    def test_beijing_today_is_a_beijing_calendar_day(self):
        with self.app.app_context():
            today = beijing_today()
            self.assertRegex(today, r"^\d{4}-\d{2}-\d{2}$")
            # 期望值必须独立算：写成 beijing_now().date().isoformat() 就是实现本身，
            # 断言恒真、等于没测。这里自己按 Asia/Shanghai 推一次 —— 北京时间
            # 00:00-08:00 时 UTC 还停在前一天，时区写错这条就会被抓住。
            expected = datetime.now(timezone.utc).astimezone(
                ZoneInfo("Asia/Shanghai")).date().isoformat()
            self.assertEqual(today, expected)

    def test_concurrent_requests_never_exceed_the_cap(self):
        """cap=20 时并发 40 个请求，放行数必须恰好是 20。

        这是 daily_usage 预占存在的**唯一理由** —— 换成「数 request_logs」，
        40 个并发会一起读到「还没超」，然后全部放行。
        """
        day = "2026-09-22"
        cap = 20
        attempts = cap + 20
        barrier = Barrier(attempts)

        def worker():
            barrier.wait()
            with self.app.app_context():
                return consume_daily_quota(self.user_id, day, cap)

        with ThreadPoolExecutor(max_workers=attempts) as pool:
            results = list(pool.map(lambda _index: worker(), range(attempts)))

        self.assertEqual(sum(1 for granted in results if granted), cap)
        with self.app.app_context():
            self.assertEqual(get_daily_usage(self.user_id, day), cap)
```

`beijing_now` 是 `src/db.py:139` 已有的函数，已含在上面的 import 块里；`datetime` /
`timezone` / `ZoneInfo` 也是给 `test_beijing_today_is_a_beijing_calendar_day` 用的，
不要因为它们"看起来没用上"就删掉。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::DailyQuotaTest -v`
Expected: FAIL —— `ImportError: cannot import name 'beijing_today' from 'src.db'`。

- [ ] **Step 3: 写实现**

在 `src/db.py` 的 `refund_user_credit()` 之后追加：

```python
def beijing_today():
    """北京时区自然日的日期串（YYYY-MM-DD）。额度计数的分桶键。"""
    return beijing_now().date().isoformat()


def consume_daily_quota(user_id, day, cap):
    """原子预占一次当日额度。

    返回 True = 已占；False = 当日额度已用尽。
    cap 为 None 表示不限（管理员），不计数、不写表。
    """
    if not user_id or cap is None:
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py::DailyQuotaTest -v`
Expected: PASS（6 passed）

> 若 `test_concurrent_requests_never_exceed_the_cap` 报 `database is locked`：这是 `busy_timeout` 不够，**不要靠降低并发数来"修"它** —— 那等于把这条用例要守的约束绕过去了。把 `get_db()` 的 `busy_timeout` 调大，或在报告里说明并停下来讨论。

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_wx_login.py
git commit -m "feat(db): add atomic daily quota reservation

设计文档 §1.6：BEGIN IMMEDIATE 内完成「读计数 + 写计数」，
并发请求排队而不是一起放行；解析失败可退回。"
```

---

## Task 3: 会员判定、当日上限、开卡与续期

**Files:**
- Modify: `src/db.py`（在 Task 2 的函数之后追加；`daily_quota_cap` 需要 `setting`，定义位置放在 `setting()` 之后即可 —— Python 是运行时解析，函数体里的前向引用没有约束）
- Test: `tests/test_wx_login.py`（追加 `MemberPlanTest`）

**Interfaces:**
- Consumes: `member_plans` 表、`setting` / `set_setting`、`utcnow`、`transaction`
- Produces:
  - `parse_utc(value) -> datetime | None` —— 宽松解析库里的 ISO 时间串，带 `+00:00` 或裸串都能吃
  - `get_member_plan(code) -> sqlite3.Row | None` —— 只返回 `active = 1` 的卡种
  - `is_member(user) -> bool` —— **唯一真相来源**。`user` 是 `users` 行或 dict
  - `daily_quota_cap(user) -> int` —— 始终返回整数（会员卡额度，否则免费档）
  - `open_member_card(user_id, plan_code) -> tuple[bool, str, str | None]` —— `(成功, 提示文案, 新到期时间)`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
from datetime import datetime, timedelta, timezone

from src.db import (
    daily_quota_cap,
    get_member_plan,
    is_member,
    open_member_card,
    parse_utc,
    set_setting,
    setting,
)


class MemberPlanTest(unittest.TestCase):
    """设计文档 §2.4：会员判定集中在一个函数里，开卡口径统一。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
        })
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,role,active,credits,openid,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("wx_alice", "x", "user", 1, 0, "openid-alice", None, utcnow()),
            )
            self.user_id = cursor.lastrowid
            db.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def user_row(self):
        return get_db().execute("SELECT * FROM users WHERE id=?", (self.user_id,)).fetchone()

    def test_free_user_gets_the_free_tier_cap(self):
        with self.app.app_context():
            db = get_db()
            set_setting("wx_free_daily_quota", 7)
            db.commit()
            self.assertFalse(is_member(self.user_row()))
            self.assertEqual(daily_quota_cap(self.user_row()), 7)

    def test_free_tier_falls_back_to_default_when_setting_is_junk(self):
        with self.app.app_context():
            db = get_db()
            set_setting("wx_free_daily_quota", "abc")
            db.commit()
            self.assertEqual(daily_quota_cap(self.user_row()), 10)

    def test_open_day_card_raises_the_cap_to_200(self):
        with self.app.app_context():
            ok, message, expires = open_member_card(self.user_id, "day")
            self.assertTrue(ok, message)
            user = self.user_row()
            self.assertTrue(is_member(user))
            self.assertEqual(user["member_plan"], "day")
            self.assertEqual(daily_quota_cap(user), 200)
            self.assertIsNotNone(parse_utc(expires))

    def test_unknown_plan_is_rejected(self):
        with self.app.app_context():
            ok, message, expires = open_member_card(self.user_id, "lifetime")
            self.assertFalse(ok)
            self.assertIn("卡种", message)
            self.assertIsNone(expires)
            self.assertFalse(is_member(self.user_row()))

    def test_renewal_extends_instead_of_overwriting(self):
        with self.app.app_context():
            open_member_card(self.user_id, "month")
            first = parse_utc(self.user_row()["member_expires_at"])
            open_member_card(self.user_id, "month")
            second = parse_utc(self.user_row()["member_expires_at"])
            self.assertAlmostEqual((second - first).days, 30, delta=1)

    def test_expired_card_restarts_from_now(self):
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE users SET member_plan='month', member_expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds"), self.user_id),
            )
            db.commit()
            self.assertFalse(is_member(self.user_row()))
            open_member_card(self.user_id, "month")
            expires = parse_utc(self.user_row()["member_expires_at"])
            # 不能写 assertGreater(..., 29)：open_member_card 存的是 now + 30 天，而这里量的是
            # 一个更晚的 now，差值恒为 30 天差一点点，timedelta.days 向下取整后**恒等于 29**，
            # 断言永远不会成立。用同类的 delta 写法即可，仍能抓住「从旧到期日续」这个真 bug
            # （那样算出来是 26 天，落在 delta 之外）。
            self.assertAlmostEqual((expires - datetime.now(timezone.utc)).days, 30, delta=1)

    def test_buying_a_smaller_card_does_not_downgrade_an_active_bigger_card(self):
        """日卡 200 < 月卡 300：已持有效月卡时买日卡，卡种保持 month（额度只升不降）。"""
        with self.app.app_context():
            open_member_card(self.user_id, "month")
            open_member_card(self.user_id, "day")
            user = self.user_row()
            self.assertEqual(user["member_plan"], "month")
            self.assertEqual(daily_quota_cap(user), 300)

    def test_upgrading_from_a_smaller_card_switches_the_plan(self):
        with self.app.app_context():
            open_member_card(self.user_id, "day")
            open_member_card(self.user_id, "year")
            user = self.user_row()
            self.assertEqual(user["member_plan"], "year")
            self.assertEqual(daily_quota_cap(user), 500)

    def test_expired_member_falls_back_to_the_free_tier_without_touching_expires_at(self):
        """设计文档 §2.4 的 🔴：会员过期只该降级到免费档，绝不能变成 403 ACCOUNT_EXPIRED。

        用户的 expires_at 是 NULL（账号永久有效），member_expires_at 过期 —— 两者必须互不影响。
        """
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE users SET member_plan='year', member_expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"), self.user_id),
            )
            db.commit()
            user = self.user_row()
            self.assertIsNone(user["expires_at"])
            self.assertFalse(is_member(user))
            self.assertEqual(daily_quota_cap(user), 10)

    def test_admin_cap_is_still_an_integer_for_display(self):
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET role='admin' WHERE id=?", (self.user_id,))
            db.commit()
            # daily_quota_cap 本身不认识 admin；「管理员不扣费」由 authenticate_wx_token 交给
            # consume_daily_quota 的 cap=None 表达（Task 8）
            self.assertEqual(daily_quota_cap(self.user_row()), 10)

    def test_inactive_plan_is_not_a_member(self):
        with self.app.app_context():
            open_member_card(self.user_id, "day")
            db = get_db()
            db.execute("UPDATE member_plans SET active=0 WHERE code='day'")
            db.commit()
            self.assertIsNone(get_member_plan("day"))
            self.assertFalse(is_member(self.user_row()))
            self.assertEqual(daily_quota_cap(self.user_row()), 10)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::MemberPlanTest -v`
Expected: FAIL —— `ImportError: cannot import name 'daily_quota_cap' from 'src.db'`。

- [ ] **Step 3: 写实现**

在 `src/db.py` 追加：

```python
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
    绝不读 expires_at —— 那是账号有效期，语义完全不同。
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


def daily_quota_cap(user):
    """当日上限：会员取卡种额度，非会员取免费档。始终返回整数。"""
    if is_member(user):
        plan = get_member_plan(user["member_plan"])
        if plan is not None:
            return int(plan["daily_quota"])
    try:
        return max(1, int(setting("wx_free_daily_quota", DEFAULT_FREE_DAILY_QUOTA)))
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

        current_plan = db.execute(
            "SELECT daily_quota FROM member_plans WHERE code=?", (user["member_plan"],)
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
```

`src/db.py` 顶部的 `from datetime import datetime, time, timedelta, timezone` 已经齐了，不用改 import。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py -v`
Expected: PASS（Task 1–3 共 24 passed —— 5 + 7 + 12）

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_wx_login.py
git commit -m "feat(db): add membership detection, quota cap and card issuance"
```

---

## Task 4: `session_key` 加密落库（AES-GCM + HKDF-SHA256）

**Files:**
- Create: `src/wx_crypto.py`
- Test: `tests/test_wx_login.py`（追加 `WxCryptoTests`）

**Interfaces:**
- Produces:
  - `encrypt_session_key(session_key: str, secret_key: str) -> str` —— base64(nonce ‖ tag ‖ 密文)
  - `decrypt_session_key(blob: str, secret_key: str) -> str` —— 篡改或换密钥时抛 `ValueError`

**为什么现在做**（§1.5）：`session_key` 是第二期虚拟支付 `signature = hmac_sha256(sessionKey, signData)` 的密钥。明文躺在库里等于把用户钱包的钥匙放在备份文件中。这是本期唯一一条"现在不做、第二期会痛"的安全项。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
from src.wx_crypto import decrypt_session_key, encrypt_session_key


class WxCryptoTests(unittest.TestCase):
    """设计文档 §1.5：session_key 必须可还原，所以是加密而非哈希。"""

    def test_round_trip(self):
        blob = encrypt_session_key("sk-alice-123456", "secret-a")
        self.assertNotEqual(blob, "sk-alice-123456")
        self.assertNotIn("sk-alice", blob)
        self.assertEqual(decrypt_session_key(blob, "secret-a"), "sk-alice-123456")

    def test_ciphertext_differs_each_time(self):
        first = encrypt_session_key("sk-alice", "secret-a")
        second = encrypt_session_key("sk-alice", "secret-a")
        self.assertNotEqual(first, second)  # 随机 nonce

    def test_tampered_ciphertext_is_rejected(self):
        blob = encrypt_session_key("sk-alice", "secret-a")
        broken = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
        with self.assertRaises(ValueError):
            decrypt_session_key(broken, "secret-a")

    def test_wrong_secret_key_is_rejected(self):
        blob = encrypt_session_key("sk-alice", "secret-a")
        with self.assertRaises(ValueError):
            decrypt_session_key(blob, "secret-b")

    def test_empty_session_key_stays_empty(self):
        self.assertEqual(encrypt_session_key("", "secret-a"), "")
        self.assertEqual(decrypt_session_key("", "secret-a"), "")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::WxCryptoTests -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'src.wx_crypto'`。

- [ ] **Step 3: 写实现**

新建 `src/wx_crypto.py`：

```python
import base64

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.Random import get_random_bytes

# HKDF 的 context：把派生出密钥的用途钉死，将来需要别的密钥时加一条新 context 即可，
# 不会和这一条互相干扰
_HKDF_CONTEXT = b"media-parser wx session_key v1"
_KEY_LENGTH = 32
_NONCE_LENGTH = 12
_TAG_LENGTH = 16


def _derive_key(secret_key):
    """由 SECRET_KEY 派生一把专属密钥 —— SECRET_KEY 已由 _load_or_create_secret 持久化。"""
    if not secret_key:
        raise RuntimeError("SECRET_KEY 未配置，无法加密 session_key")
    return HKDF(
        master=secret_key.encode("utf-8"),
        key_len=_KEY_LENGTH,
        salt=None,
        hashmod=SHA256,
        num_keys=1,
        context=_HKDF_CONTEXT,
    )


def encrypt_session_key(session_key, secret_key):
    """AES-GCM 加密，返回 base64(nonce ‖ tag ‖ 密文)。"""
    if not session_key:
        return ""
    cipher = AES.new(_derive_key(secret_key), AES.MODE_GCM, nonce=get_random_bytes(_NONCE_LENGTH))
    ciphertext, tag = cipher.encrypt_and_digest(str(session_key).encode("utf-8"))
    return base64.b64encode(cipher.nonce + tag + ciphertext).decode("ascii")


def decrypt_session_key(blob, secret_key):
    """还原 session_key。密文被篡改或 SECRET_KEY 变了都会抛 ValueError（GCM 认证失败）。"""
    if not blob:
        return ""
    raw = base64.b64decode(blob)
    nonce = raw[:_NONCE_LENGTH]
    tag = raw[_NONCE_LENGTH:_NONCE_LENGTH + _TAG_LENGTH]
    ciphertext = raw[_NONCE_LENGTH + _TAG_LENGTH:]
    cipher = AES.new(_derive_key(secret_key), AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py::WxCryptoTests -v`
Expected: PASS（6 passed —— 原文写 5；实施时补了一条「只翻转 tag 字节」的用例，因为原有的两条篡改用例碰不到 tag，把 `decrypt_and_verify` 换成 `decrypt` 后仍能通过）

- [ ] **Step 5: Commit**

```bash
git add src/wx_crypto.py tests/test_wx_login.py
git commit -m "feat: encrypt wechat session_key with AES-GCM + HKDF-SHA256

设计文档 §1.5：session_key 是第二期虚拟支付的签名密钥，不能明文落库。"
```

---

## Task 5: `wx` 蓝本、`code2Session` 客户端与 `/api/v1/wx/login`

**Files:**
- Create: `src/api/wx.py`
- Modify: `app.py:38-74`（环境变量 + 注册蓝本）
- Test: `tests/test_wx_login.py`（追加 `WxLoginTest`）

**Interfaces:**
- Consumes: `_ensure_column` 的 7 列、`wx_sessions` 表、`encrypt_session_key`（Task 4）、`is_member` / `daily_quota_cap` / `get_daily_usage` / `get_member_plan`（Task 3）、`authenticate_wx_token()`（**本任务先写它** —— 实现逐字取自 Task 8 第 3 步；Task 8 只做核对与接线）
- Produces:
  - `bp` —— 蓝本，路由都带 `/v1/wx` 前缀（注册时 `url_prefix='/api'`）
  - `code2session(code) -> dict` —— **可被测试 patch**（`patch("src.api.wx.code2session", ...)`）
  - `wx_configured() -> bool`
  - `_hash_token(token) -> str`
  - `_user_snapshot(user) -> dict` —— 与 §2.2 字段表逐字对应，`/wx/login`、`/wx/me`、`/wx/profile` 共用；其中 `avatar` **只在头像文件存在时**给地址，否则为空串（没传过头像的用户不该拿到一个 404 的图）
  - 响应字段：`{token, expires_at, user}`，`user` 见 §2.2

- [ ] **Step 0: 先写 `authenticate_wx_token()`（内容就是 Task 8 第 3 步）**

本任务的 `src/api/wx.py` 在**模块级** `from src.api.access import authenticate_wx_token`，而它的实现在
Task 8 才给出 —— 照任务顺序执行，本任务的测试会因为 `ImportError` 全红，与本任务写得对不对无关。
所以先照 **Task 8 第 3 步**的代码把它追加进 `src/api/access.py`（签名与实现逐字照抄，别改写）。

这一步**不写测试**：它的用例属于 Task 8，Task 8 负责核对它、把 `quota_cap` 接进 `/api/v1/parse`，
并补上 `WxParseAuthTest`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
from unittest.mock import patch


class WxLoginTest(unittest.TestCase):
    """设计文档 §2.1 / §2.2 / §4.1。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
            "WX_APPID": "wx-test-appid",
            "WX_APPSECRET": "wx-test-appsecret",
            # 头像目录必须指到临时目录：默认是 <仓库>/data/avatars，会让上一个用例
            # （或上一次运行）留下的 1.jpg 被这个用例看到
            "AVATAR_DIR": os.path.join(self.temp_dir.name, "avatars"),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def login(self, code="code-1", openid="openid-alice", session_key="sk-alice"):
        with patch("src.api.wx.code2session",
                   return_value={"openid": openid, "session_key": session_key}):
            return self.client.post("/api/v1/wx/login", json={"code": code})

    def test_first_login_creates_a_user_with_the_expected_values(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["succ"])
        self.assertTrue(payload["data"]["token"])

        with self.app.app_context():
            user = get_db().execute(
                "SELECT * FROM users WHERE openid=?", ("openid-alice",)
            ).fetchone()
            self.assertIsNotNone(user)
            self.assertEqual(user["role"], "user")
            self.assertEqual(user["credits"], 0)          # 不是列默认的 100
            self.assertIsNone(user["expires_at"])         # 账号永久有效
            self.assertIsNone(user["member_plan"])
            self.assertEqual(user["username"], "wx_openid-alice")
            self.assertTrue(user["active"])

    def test_login_response_matches_the_field_contract(self):
        user = self.login().get_json()["data"]["user"]
        self.assertEqual(sorted(user), [
            "avatar", "balance", "daily_quota", "id", "member", "member_days_left",
            "member_expires_at", "member_plan", "member_plan_name", "nickname",
            "remaining_today", "used_today",
        ])
        self.assertFalse(user["member"])
        self.assertIsNone(user["member_plan"])
        self.assertEqual(user["daily_quota"], 10)
        self.assertEqual(user["used_today"], 0)
        self.assertEqual(user["remaining_today"], 10)
        self.assertEqual(user["balance"], 0)
        # 刚登录、还没传过头像：给空串，客户端才会走 wx:else 的默认头像（§3.4）
        self.assertEqual(user["avatar"], "")
        self.assertNotIn("available", user)      # §2.2：有意不提供合成值
        self.assertNotIn("unlimited", user)      # 新模型不存在「不限量」

    def test_second_login_reuses_the_same_user_row(self):
        first = self.login(code="code-1").get_json()["data"]["user"]["id"]
        second = self.login(code="code-2").get_json()["data"]["user"]["id"]
        self.assertEqual(first, second)
        with self.app.app_context():
            count = get_db().execute(
                "SELECT COUNT(*) c FROM users WHERE openid=?", ("openid-alice",)
            ).fetchone()["c"]
            self.assertEqual(count, 1)

    def test_each_login_issues_a_new_session_row(self):
        self.login(code="code-1")
        self.login(code="code-2")
        with self.app.app_context():
            count = get_db().execute("SELECT COUNT(*) c FROM wx_sessions").fetchone()["c"]
            self.assertEqual(count, 2)

    def test_session_key_is_encrypted_at_rest(self):
        self.login()
        with self.app.app_context():
            row = get_db().execute("SELECT session_key FROM wx_sessions").fetchone()
            self.assertNotEqual(row["session_key"], "sk-alice")
            self.assertNotIn("sk-alice", row["session_key"])
            self.assertEqual(
                decrypt_session_key(row["session_key"], "test-secret"), "sk-alice"
            )

    def test_missing_code_is_rejected(self):
        response = self.client.post("/api/v1/wx/login", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "WX_CODE_INVALID")

    def test_wechat_rejecting_the_code_is_400_not_500(self):
        with patch("src.api.wx.code2session", return_value={"errcode": 40029, "errmsg": "invalid code"}):
            response = self.client.post("/api/v1/wx/login", json={"code": "used-code"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "WX_CODE_INVALID")

    def test_network_failure_is_400_not_500(self):
        with patch("src.api.wx.code2session", side_effect=OSError("boom")):
            response = self.client.post("/api/v1/wx/login", json={"code": "code-1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "WX_CODE_INVALID")

    def test_unconfigured_appid_returns_503(self):
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "nocfg.db"),
            "WX_APPID": "",
            "WX_APPSECRET": "",
        })
        response = app.test_client().post("/api/v1/wx/login", json={"code": "code-1"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error_code"], "WX_NOT_CONFIGURED")

    def test_disabled_account_cannot_log_in(self):
        user_id = self.login().get_json()["data"]["user"]["id"]
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
            db.commit()
        response = self.login(code="code-2")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error_code"], "ACCOUNT_DISABLED")

    def test_expired_account_cannot_log_in(self):
        """/wx/login 与网页登录**故意不同**：这里要查 expires_at（§2.3）。"""
        user_id = self.login().get_json()["data"]["user"]["id"]
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET expires_at=? WHERE id=?",
                       ("2020-01-01T00:00:00+00:00", user_id))
            db.commit()
        response = self.login(code="code-2")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error_code"], "ACCOUNT_EXPIRED")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::WxLoginTest -v`
Expected: FAIL —— `404`（蓝本没注册）或 `ModuleNotFoundError: No module named 'src.api.wx'`。

- [ ] **Step 3: 写实现**

新建 `src/api/wx.py`：

```python
import hashlib
import json
import math
import os
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, request, send_file
from configs.logging_config import get_logger

from src.api.access import authenticate_wx_token, consume_rate_limit, get_client_ip
from src.api.response import make_response
from src.db import (
    beijing_today,
    daily_quota_cap,
    get_daily_usage,
    get_db,
    get_member_plan,
    is_member,
    parse_utc,
    setting,
    transaction,
    utcnow,
)
from src.auth import user_is_expired
from src.wx_crypto import encrypt_session_key

bp = Blueprint('wx', __name__)
logger = get_logger(__name__)

CODE2SESSION_URL = 'https://api.weixin.qq.com/sns/jscode2session'
TOKEN_TTL_DAYS = 30
AVATAR_MAX_BYTES = 2 * 1024 * 1024
LOGIN_RATE_LIMIT = 10
# 不可登录的占位 hash：username 是 NOT NULL，但小程序用户永远不走密码登录
UNUSABLE_PASSWORD_HASH = '!wx-silent-login-no-password'
IMAGE_MAGIC = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'RIFF', 'image/webp'),
)


def wx_configured():
    return bool(current_app.config.get('WX_APPID')) and bool(current_app.config.get('WX_APPSECRET'))


def _hash_token(token):
    """token 在服务端只用于比对，从不参与加密运算，所以只存哈希（§1.3）。"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def code2session(code):
    """用 code 换 openid / session_key。独立成函数，测试里 patch 它，绝不真打微信接口。"""
    query = urllib.parse.urlencode({
        'appid': current_app.config.get('WX_APPID'),
        'secret': current_app.config.get('WX_APPSECRET'),
        'js_code': code,
        'grant_type': 'authorization_code',
    })
    with urllib.request.urlopen(f'{CODE2SESSION_URL}?{query}', timeout=5) as response:
        return json.loads(response.read().decode('utf-8'))


def _default_qps():
    try:
        return max(1, int(setting('default_user_qps', 2)))
    except (TypeError, ValueError):
        return 2


def _sniff_image_mime(payload):
    if payload[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if payload[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if payload[:4] == b'RIFF' and payload[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _avatar_dir():
    """头像目录。默认 `app.instance_path/avatars`，测试用 `AVATAR_DIR` 指到临时目录。

    不能直接写 `current_app.instance_path`：它对**每个** app 都是 `<仓库>/data`
    （app.py 的 `data_dir` 是按 `__file__` 算出来的绝对路径，测试传的临时 DATABASE 改不了它）。
    临时库的用户 id 每次从 1 开始，于是 `data/avatars/1.jpg` 会跨用例、跨运行残留，
    让「没传过头像 → avatar 为空串」这条断言在第二次运行时就变红。
    """
    return current_app.config.get('AVATAR_DIR') or os.path.join(current_app.instance_path, 'avatars')


def _user_snapshot(user):
    """客户端唯一的数据来源（§3.4）。服务端把一切算好，客户端只渲染。"""
    today = beijing_today()
    member = is_member(user)
    plan = get_member_plan(user['member_plan']) if member else None
    cap = daily_quota_cap(user)
    used = get_daily_usage(user['id'], today)
    expires = parse_utc(user['member_expires_at']) if member else None
    days_left = None
    if expires is not None:
        days_left = max(0, math.ceil((expires - datetime.now(timezone.utc)).total_seconds() / 86400))
    # 头像是「有文件才给地址」：/wx/avatar/<id> 没落过盘时返回 404，小程序 <image>
    # 拿到 404 只会显示一片空白，wxml 里 wx:else 的默认头像分支反而永远走不到。
    # 按文件是否存在判断（而不是看 users.avatar 列）：换机器/删 instance 目录后
    # 列里可能还留着旧地址，那时按列判断就会又白屏一次。
    avatar_path = os.path.join(_avatar_dir(), f"{user['id']}.jpg")
    return {
        'id': user['id'],
        'nickname': user['nickname'] or '',
        'avatar': f"/api/v1/wx/avatar/{user['id']}" if os.path.exists(avatar_path) else '',
        'member': member,
        'member_plan': plan['code'] if plan else None,
        'member_plan_name': plan['name'] if plan else None,
        'member_expires_at': user['member_expires_at'] if plan else None,
        'member_days_left': days_left,
        'daily_quota': cap,
        'used_today': used,
        'remaining_today': max(0, cap - used),
        'balance': user['credits'],
    }


@bp.post('/v1/wx/login')
def login():
    if not wx_configured():
        return make_response(503, '服务端未配置微信登录凭证', None, False, 'WX_NOT_CONFIGURED'), 503
    # 登录接口本身不限流就会变成 code2Session 额度的放大器（§3.1 的同一风险）
    if not consume_rate_limit(f"wx_login:{get_client_ip()}", LOGIN_RATE_LIMIT, window_seconds=60):
        return make_response(429, '登录过于频繁，请稍后重试', None, False, 'RATE_LIMITED'), 429

    payload = request.get_json(silent=True)
    code = str((payload or {}).get('code', '')).strip() if isinstance(payload, dict) else ''
    if not code:
        return make_response(400, '缺少 code 参数', None, False, 'WX_CODE_INVALID'), 400

    try:
        session = code2session(code)
    except Exception:
        logger.exception('code2session 请求失败')
        return make_response(400, '微信登录凭证校验失败，请重试', None, False, 'WX_CODE_INVALID'), 400

    openid = (session or {}).get('openid')
    if not openid:
        logger.warning(f'code2session 未返回 openid: {session}')
        return make_response(400, '微信登录凭证无效或已过期，请重试', None, False, 'WX_CODE_INVALID'), 400

    now = utcnow()
    with transaction(immediate=True) as db:
        user = db.execute('SELECT * FROM users WHERE openid=?', (openid,)).fetchone()
        if user is None:
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,role,active,expires_at,qps_limit,credits,openid,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (f'wx_{openid}', UNUSABLE_PASSWORD_HASH, 'user', 1, None,
                 _default_qps(), 0, openid, now),
            )
            user = db.execute('SELECT * FROM users WHERE id=?', (cursor.lastrowid,)).fetchone()

        # 与网页登录故意不同：小程序没有「登进去看看」的场景，停用/到期要在这一步就说清（§2.3）
        if not user['active']:
            return make_response(403, '账号已被停用，请联系客服', None, False, 'ACCOUNT_DISABLED'), 403
        if user_is_expired(user):
            return make_response(403, '账号已到期，请联系客服', None, False, 'ACCOUNT_EXPIRED'), 403

        raw_token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=TOKEN_TTL_DAYS)).isoformat(timespec='seconds')
        db.execute(
            "INSERT INTO wx_sessions(token_hash,user_id,session_key,created_at,expires_at,last_seen_at) "
            "VALUES(?,?,?,?,?,?)",
            (_hash_token(raw_token),
             user['id'],
             encrypt_session_key((session or {}).get('session_key', ''), current_app.config['SECRET_KEY']),
             now, expires_at, now),
        )
        db.execute('DELETE FROM wx_sessions WHERE expires_at < ?', (now,))
        snapshot = _user_snapshot(user)

    logger.info(f'微信登录成功 user_id={snapshot["id"]}')
    return make_response(200, '成功', {'token': raw_token, 'expires_at': expires_at, 'user': snapshot}, True), 200
```

`app.py` 的改动：

```python
from src.api.wx import bp as wx_bp
```
（放在 `from src.api.parse import bp as api_bp` 之后）

在 `app.config['API_ONLY'] = ...` 那段之后加：

```python
    app.config['WX_APPID'] = os.getenv('WX_APPID', '').strip()
    app.config['WX_APPSECRET'] = os.getenv('WX_APPSECRET', '').strip()
```

注册蓝本处改成：

```python
    # 注册蓝图
    app.register_blueprint(api_bp, url_prefix='/api')
    # wx_bp 必须在外层：小程序是**纯 API 调用方**，API_ONLY=true 时它照样要能登录（§1.1）
    app.register_blueprint(wx_bp, url_prefix='/api')
    if not app.config.get('API_ONLY'):
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py::WxLoginTest -v`
Expected: PASS（11 passed）

- [ ] **Step 5: Commit**

```bash
# Step 0 改了 src/api/access.py（authenticate_wx_token + 两个 import），漏了它这次提交就只有半成品
git add src/api/wx.py src/api/access.py app.py tests/test_wx_login.py
git commit -m "feat(wx): add silent login endpoint and wx blueprint

设计文档 §2.1-§2.3：code2Session 可注入、新用户显式 credits=0、
停用与到期在登录这一步就说清、session_key 加密落库。"
```

---

## Task 6: `/wx/me` 与 `/wx/checkin`

**Files:**
- Modify: `src/api/wx.py`（追加路由）
- Test: `tests/test_wx_login.py`（追加 `WxMeTest`、`WxCheckinTest`）

**Interfaces:**
- Consumes: `authenticate_wx_token()`（**Task 5 第 0 步已写好** —— 它的实现逐字取自 Task 8 第 3 步。本任务**只直接调用**，不要重新定义它；Task 8 负责核对签名并接进 `/api/v1/parse`）

> ⚠️ **不要重复实现它**：本任务与 Task 7 的路由都要 `authenticate_wx_token()`，而它已在 **Task 5 第 0 步**
> 写进 `src/api/access.py`（`src/api/wx.py` 也在模块级导入了它）。本任务只需调用 —— 再写一遍不会报错，
> 只会静静地覆盖前一个定义。核对签名与接进 `/api/v1/parse` 是 Task 8 的事。

- Produces:
  - `GET /api/v1/wx/me` → `{user: <§2.2 快照>}`
  - `POST /api/v1/wx/checkin` → `{streak, bonus, balance}`；重复签到 409 `ALREADY_CHECKED_IN`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
def auth_header(token):
    return {"X-WX-Token": token}


class WxMeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
            "WX_APPID": "wx-test-appid",
            "WX_APPSECRET": "wx-test-appsecret",
        })
        self.client = self.app.test_client()
        with patch("src.api.wx.code2session",
                   return_value={"openid": "openid-alice", "session_key": "sk-alice"}):
            self.session = self.client.post("/api/v1/wx/login", json={"code": "c"}).get_json()["data"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_me_returns_the_snapshot(self):
        response = self.client.get("/api/v1/wx/me", headers=auth_header(self.session["token"]))
        self.assertEqual(response.status_code, 200)
        user = response.get_json()["data"]["user"]
        self.assertEqual(user["id"], self.session["user"]["id"])
        self.assertEqual(user["remaining_today"], 10)

    def test_me_reflects_a_member_card(self):
        with self.app.app_context():
            open_member_card(self.session["user"]["id"], "year")
        response = self.client.get("/api/v1/wx/me", headers=auth_header(self.session["token"]))
        user = response.get_json()["data"]["user"]
        self.assertTrue(user["member"])
        self.assertEqual(user["member_plan"], "year")
        self.assertEqual(user["member_plan_name"], "年卡")
        self.assertEqual(user["daily_quota"], 500)
        self.assertEqual(user["remaining_today"], 500)
        self.assertGreater(user["member_days_left"], 360)

    def test_me_without_token_is_401(self):
        response = self.client.get("/api/v1/wx/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "WX_TOKEN_INVALID")

    def test_me_with_unknown_token_is_401(self):
        response = self.client.get("/api/v1/wx/me", headers=auth_header("not-a-real-token"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "WX_TOKEN_INVALID")

    def test_me_with_expired_session_is_401_expired(self):
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE wx_sessions SET expires_at=?", ("2020-01-01T00:00:00+00:00",))
            db.commit()
        response = self.client.get("/api/v1/wx/me", headers=auth_header(self.session["token"]))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "WX_TOKEN_EXPIRED")

    def test_me_reports_the_used_count(self):
        user_id = self.session["user"]["id"]
        with self.app.app_context():
            consume_daily_quota(user_id, beijing_today(), 10)
            consume_daily_quota(user_id, beijing_today(), 10)
        response = self.client.get("/api/v1/wx/me", headers=auth_header(self.session["token"]))
        user = response.get_json()["data"]["user"]
        self.assertEqual(user["used_today"], 2)
        self.assertEqual(user["remaining_today"], 8)


class WxCheckinTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
            "WX_APPID": "wx-test-appid",
            "WX_APPSECRET": "wx-test-appsecret",
        })
        self.client = self.app.test_client()
        with patch("src.api.wx.code2session",
                   return_value={"openid": "openid-alice", "session_key": "sk-alice"}):
            self.session = self.client.post("/api/v1/wx/login", json={"code": "c"}).get_json()["data"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def checkin(self):
        return self.client.post("/api/v1/wx/checkin", headers=auth_header(self.session["token"]))

    def test_first_checkin_grants_the_bonus(self):
        response = self.checkin()
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["streak"], 1)
        self.assertEqual(data["bonus"], 10)
        self.assertEqual(data["balance"], 10)

    def test_second_checkin_same_day_is_409_and_does_not_pay_again(self):
        self.checkin()
        response = self.checkin()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error_code"], "ALREADY_CHECKED_IN")
        with self.app.app_context():
            user = get_db().execute("SELECT credits FROM users WHERE id=?",
                                    (self.session["user"]["id"],)).fetchone()
            self.assertEqual(user["credits"], 10)

    def test_consecutive_days_increase_the_streak(self):
        self.checkin()
        yesterday = (beijing_now().date() - timedelta(days=1)).isoformat()
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET last_checkin_date=? WHERE id=?",
                       (yesterday, self.session["user"]["id"]))
            db.commit()
        self.assertEqual(self.checkin().get_json()["data"]["streak"], 2)

    def test_a_broken_streak_restarts_at_one(self):
        self.checkin()
        three_days_ago = (beijing_now().date() - timedelta(days=3)).isoformat()
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET last_checkin_date=? WHERE id=?",
                       (three_days_ago, self.session["user"]["id"]))
            db.commit()
        self.assertEqual(self.checkin().get_json()["data"]["streak"], 1)

    def test_checkin_without_token_is_401(self):
        response = self.client.post("/api/v1/wx/checkin")
        self.assertEqual(response.status_code, 401)
```

`timedelta` / `beijing_now` / `open_member_card` / `consume_daily_quota` / `beijing_today` 都已在前面任务的 import 里，不用重复引入。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::WxMeTest tests/test_wx_login.py::WxCheckinTest -v`
Expected: FAIL —— 两个路由都是 404。

- [ ] **Step 3: 写实现**

追加到 `src/api/wx.py`：

```python
def _auth_or_error():
    """共用的鉴权前置：返回 (access, None) 或 (None, response)。"""
    access, error = authenticate_wx_token()
    if error:
        status, message, code = error
        return None, (make_response(status, message, None, False, code), status)
    if access is None:
        return None, (make_response(401, '缺少登录令牌', None, False, 'WX_TOKEN_INVALID'), 401)
    return access, None


@bp.get('/v1/wx/me')
def me():
    access, failure = _auth_or_error()
    if failure:
        return failure
    user = get_db().execute('SELECT * FROM users WHERE id=?', (access['user_id'],)).fetchone()
    return make_response(200, '成功', {'user': _user_snapshot(user)}, True), 200


@bp.post('/v1/wx/checkin')
def checkin():
    access, failure = _auth_or_error()
    if failure:
        return failure

    today = beijing_today()
    yesterday = (beijing_now().date() - timedelta(days=1)).isoformat()
    try:
        bonus = max(1, int(setting('wx_checkin_bonus', DEFAULT_CHECKIN_BONUS)))
    except (TypeError, ValueError):
        bonus = DEFAULT_CHECKIN_BONUS

    with transaction(immediate=True) as db:
        user = db.execute('SELECT * FROM users WHERE id=?', (access['user_id'],)).fetchone()
        if user['last_checkin_date'] == today:
            return make_response(409, '今天已经签到过了', None, False, 'ALREADY_CHECKED_IN'), 409
        streak = (int(user['checkin_streak'] or 0) + 1) if user['last_checkin_date'] == yesterday else 1
        db.execute(
            'UPDATE users SET last_checkin_date=?, checkin_streak=?, credits=credits+? WHERE id=?',
            (today, streak, bonus, user['id']),
        )
        balance = db.execute('SELECT credits FROM users WHERE id=?', (user['id'],)).fetchone()['credits']

    return make_response(200, '签到成功',
                         {'streak': streak, 'bonus': bonus, 'balance': balance}, True), 200
```

`beijing_now` 与 `DEFAULT_CHECKIN_BONUS` 要补进 `src/api/wx.py` 的 import。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/api/wx.py tests/test_wx_login.py
git commit -m "feat(wx): add /wx/me snapshot and /wx/checkin"
```

---

## Task 7: `/wx/profile`（头像服务端转存）与 `/wx/avatar/<user_id>`

**Files:**
- Modify: `src/api/wx.py`（追加路由与 `_store_avatar`）
- Test: `tests/test_wx_login.py`（追加 `WxProfileTest`）

**Interfaces:**
- Consumes: `_avatar_dir()` —— **Task 5 已经写好的那个解析函数**（读 `AVATAR_DIR` 配置，缺省 `app.instance_path/avatars`）。头像的读写都要走它：直接写 `current_app.instance_path` 会让文件落到 `<仓库>/data`，测试之间互相污染，而且换机器/删目录后行为不一致
- Consumes: 还有两个 **Task 6 已经写好的名字** —— 直接调用，**不要重新定义**：
  - `_auth_or_error()`（`src/api/wx.py` 模块级）—— 本任务的路由用它做鉴权前置，返回 `(access, failure)`
  - `auth_header(token)`（`tests/test_wx_login.py` 模块级）—— 本任务的用例用它构造 `X-WX-Token` 头。
    再写一遍不会报错，只会静静地覆盖前一个定义
- Produces:
  - `POST /api/v1/wx/profile` → `{user: <快照>}`；接受 `multipart/form-data`（`nickname` 文本字段 + `avatar` 文件字段）或 JSON（只改昵称）
  - `GET /api/v1/wx/avatar/<int:user_id>` → 图片字节流，无需 token
  - `_store_avatar(user_id, file_storage) -> tuple[bool, str]`
  - `AVATAR_MAX_BYTES = 2 * 1024 * 1024`
- Modifies: `app.py` 的 `MAX_CONTENT_LENGTH`（Task 5 的改动之后已下移到第 47 行，按符号定位别按行号）—— **32KB 装不下一张手机头像，必须提到 4MB**

**为什么是「客户端上传字节」而不是「客户端给个 URL」**（§1.7）：`chooseAvatar` 拿到的临时地址约 2 小时失效，所以头像必须落盘到我们自己的存储 —— 这一点设计文档写对了。但**服务端下载不到它**：`chooseAvatar` 交给客户端的是 `wxfile://` **本机临时路径**，只在用户那台手机上有效，服务端拿到这个字符串什么也做不了（https 形式的临时地址是已废弃的 `getUserProfile` 时代的产物；即便有，让服务端去拉也比客户端直传多一次往返、多一个 SSRF 出口）。

所以：**客户端用 `wx.uploadFile` 把字节传上来，服务端存盘**。这不是取舍，是平台约束 —— 设计文档 §2.3 里「`avatar` 是临时路径字符串」那一句按字面实现跑不通，本计划按上传实现。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
import io

# 伪造的头像字节：只要求前缀能过 _sniff_image_mime，其余字节无所谓
PNG_BYTES = (b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
JPEG_BYTES = (b'\xff\xd8\xff\xe0' + b'\x00' * 64)


class WxProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
            "WX_APPID": "wx-test-appid",
            "WX_APPSECRET": "wx-test-appsecret",
            # 头像目录指到临时目录：默认的 <仓库>/data/avatars 会让本类写下的 1.jpg
            # 留在磁盘上，下一次运行时先跑的 WxLoginTest 会看到它、以为用户传过头像
            "AVATAR_DIR": os.path.join(self.temp_dir.name, "avatars"),
        })
        self.client = self.app.test_client()
        with patch("src.api.wx.code2session",
                   return_value={"openid": "openid-alice", "session_key": "sk-alice"}):
            self.session = self.client.post("/api/v1/wx/login", json={"code": "c"}).get_json()["data"]
        self.user_id = self.session["user"]["id"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def stored_path(self):
        """上传后文件应该落在这里 —— 读同一个配置，别自己拼 instance_path。"""
        return os.path.join(self.app.config["AVATAR_DIR"], f"{self.user_id}.jpg")

    def profile(self, **body):
        return self.client.post("/api/v1/wx/profile", json=body,
                                headers=auth_header(self.session["token"]))

    def upload(self, payload, filename="a.jpg"):
        """客户端用 wx.uploadFile 提交，服务端收到的是 multipart —— 测试必须照这个形状发。"""
        return self.client.post(
            "/api/v1/wx/profile",
            data={"avatar": (io.BytesIO(payload), filename)},
            content_type="multipart/form-data",
            headers=auth_header(self.session["token"]),
        )

    def test_nickname_is_saved_and_returned(self):
        response = self.profile(nickname="阿七")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["user"]["nickname"], "阿七")
        self.assertEqual(
            self.client.get("/api/v1/wx/me", headers=auth_header(self.session["token"]))
                .get_json()["data"]["user"]["nickname"], "阿七")

    def test_nickname_is_truncated_to_32_chars(self):
        response = self.profile(nickname="阿" * 50)
        self.assertEqual(len(response.get_json()["data"]["user"]["nickname"]), 32)

    def test_avatar_upload_round_trips_byte_for_byte(self):
        response = self.upload(JPEG_BYTES)
        self.assertEqual(response.status_code, 200)
        avatar_path = response.get_json()["data"]["user"]["avatar"]
        self.assertEqual(avatar_path, f"/api/v1/wx/avatar/{self.user_id}")

        self.assertTrue(os.path.exists(self.stored_path()), f"头像未落盘：{self.stored_path()}")
        with open(self.stored_path(), "rb") as handle:
            self.assertEqual(handle.read(), JPEG_BYTES)

        served = self.client.get(avatar_path)
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.data, JPEG_BYTES)
        self.assertEqual(served.headers["Content-Type"].split(";")[0], "image/jpeg")

    def test_avatar_content_type_is_sniffed_from_bytes_not_the_filename(self):
        """文件名一律 .jpg（§1.7 的既定命名），但 PNG 字节必须按 image/png 提供。"""
        response = self.upload(PNG_BYTES, filename="avatar.jpg")
        self.assertEqual(response.status_code, 200)
        served = self.client.get(f"/api/v1/wx/avatar/{self.user_id}")
        self.assertEqual(served.headers["Content-Type"].split(";")[0], "image/png")

    def test_a_real_sized_avatar_is_not_blocked_by_the_body_limit(self):
        """app.py 的 MAX_CONTENT_LENGTH 原来是 32 * 1024 —— 连一张手机头像都装不下。

        这条用例就是那个陷阱的哨兵：1.5MB 的上传必须能通过，而不是 413。
        """
        response = self.upload(b'\xff\xd8\xff\xe0' + b'\x00' * (1536 * 1024))
        self.assertEqual(response.status_code, 200)

    def test_oversized_avatar_is_rejected_with_400(self):
        oversized = b'\xff\xd8\xff\xe0' + b'\x00' * (2 * 1024 * 1024 + 10)
        response = self.upload(oversized)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "AVATAR_SAVE_FAILED")
        self.assertFalse(os.path.exists(self.stored_path()))

    def test_non_image_upload_is_rejected_and_nothing_is_written(self):
        response = self.upload(b'{"errcode":40001}')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "AVATAR_SAVE_FAILED")
        self.assertFalse(os.path.exists(self.stored_path()))

    def test_empty_upload_is_rejected(self):
        response = self.upload(b'')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "AVATAR_SAVE_FAILED")
        self.assertFalse(os.path.exists(self.stored_path()))

    def test_avatar_can_be_updated_twice(self):
        self.upload(JPEG_BYTES)
        self.upload(PNG_BYTES)
        with open(self.stored_path(), "rb") as handle:
            self.assertEqual(handle.read(), PNG_BYTES)

    def test_missing_avatar_file_is_404(self):
        response = self.client.get(f"/api/v1/wx/avatar/{self.user_id}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error_code"], "AVATAR_NOT_FOUND")

    def test_profile_requires_a_token(self):
        response = self.client.post("/api/v1/wx/profile", json={"nickname": "x"})
        self.assertEqual(response.status_code, 401)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::WxProfileTest -v`
Expected: FAIL —— 404。

- [ ] **Step 3: 写实现**

追加到 `src/api/wx.py`：

```python
def _store_avatar(user_id, file_storage):
    """保存客户端上传的头像字节（§1.7）。

    注意：收的是**文件字节**，不是 URL —— chooseAvatar 交给客户端的是 wxfile://
    本机临时路径，服务端下载不了它。
    """
    payload = file_storage.read(AVATAR_MAX_BYTES + 1)
    if not payload:
        return False, '头像文件为空'
    if len(payload) > AVATAR_MAX_BYTES:
        return False, '头像文件超过 2MB'
    if _sniff_image_mime(payload) is None:
        return False, '请上传 jpg / png / webp 格式的图片'

    directory = _avatar_dir()
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f'{user_id}.jpg'), 'wb') as handle:
        handle.write(payload)

    db = get_db()
    db.execute('UPDATE users SET avatar=? WHERE id=?', (f'/api/v1/wx/avatar/{user_id}', user_id))
    db.commit()
    return True, 'ok'


@bp.post('/v1/wx/profile')
def profile():
    """更新昵称与头像。multipart（带头像）与 JSON（只改昵称）两种提交都收。"""
    access, failure = _auth_or_error()
    if failure:
        return failure

    if request.is_json:
        payload = request.get_json(silent=True) or {}
        nickname = str(payload.get('nickname') or '')
        upload = None
    else:
        nickname = str(request.form.get('nickname') or '')
        upload = request.files.get('avatar')

    user_id = access['user_id']
    nickname = nickname.strip()[:32]
    if nickname:
        db = get_db()
        db.execute('UPDATE users SET nickname=? WHERE id=?', (nickname, user_id))
        db.commit()

    if upload is not None:
        ok, message = _store_avatar(user_id, upload)
        if not ok:
            return make_response(400, message, None, False, 'AVATAR_SAVE_FAILED'), 400

    user = get_db().execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    return make_response(200, '成功', {'user': _user_snapshot(user)}, True), 200


@bp.get('/v1/wx/avatar/<int:user_id>')
def avatar(user_id):
    """头像字节流。本身就是公开材料，无需 token（§2.1）。"""
    path = os.path.join(_avatar_dir(), f'{user_id}.jpg')
    if not os.path.exists(path):
        return make_response(404, '头像不存在', None, False, 'AVATAR_NOT_FOUND'), 404
    with open(path, 'rb') as handle:
        head = handle.read(16)
    return send_file(path, mimetype=_sniff_image_mime(head) or 'application/octet-stream')
```

最后改 `app.py` —— **这一步不能漏，漏了头像上传会得到 413 而不是 200**：

```python
    # 原来是 32 * 1024。小程序头像走 multipart 直传，一张手机头像轻松超过 32KB，
    # 保持 32KB 会让 /api/v1/wx/profile 直接 413（docs/wx-login.md §1.7）。
    # 4MB 只用于让请求体进得来：各接口自己的校验（parse 的 2048 字上限、
    # 头像的 AVATAR_MAX_BYTES = 2MB）才是真正的边界，放宽的只是那个 413 闸门。
    app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024
```

> 放宽全局上限的代价要说清楚：任何 POST 请求体的读取闸门都从 32KB 变成 4MB。真正读 body 的公开入口只有 `/api/v1/parse`（读 body 前已过鉴权）与 `/api/v1/wx/login`（读 body 前已过 IP 限流，Task 5），其余都要登录会话。若你更想守住 32KB，替代方案是给 wx 蓝本挂一个 `@bp.before_request` 单独调高 `request.max_content_length` —— 但那条路依赖 Flask 的按请求覆盖语义，本计划不拿它当主路径。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py::WxProfileTest -v`
Expected: PASS（11 passed）

- [ ] **Step 5: 手工验证一次上传**

Run: `python app.py`，用 `curl` 模拟一次真实上传（用任意一张 jpg）：

```bash
curl -i -X POST http://127.0.0.1:8051/api/v1/wx/profile \
  -H "X-WX-Token: <上一步登录拿到的 token>" \
  -F "avatar=@一张手机拍的.jpg"
```
Expected: `HTTP/1.1 200`，且 `data/avatars/<user_id>.jpg` 出现、浏览器打开 `/api/v1/wx/avatar/<user_id>` 能看到那张图。**若返回 413，就是 Step 3 末尾那段 `MAX_CONTENT_LENGTH` 没改。**

- [ ] **Step 6: Commit**

```bash
git add src/api/wx.py app.py tests/test_wx_login.py
git commit -m "feat(wx): accept avatar uploads and raise the body size limit

设计文档 §1.7：头像临时链接约 2 小时失效，必须落盘后由本服务提供；
chooseAvatar 给的是本机临时路径，所以由客户端 uploadFile 直传字节。
app.py 的 MAX_CONTENT_LENGTH 从 32KB 提到 4MB —— 32KB 连一张头像都装不下。"
```

---

## Task 8: `authenticate_wx_token()` 与 `/api/v1/parse` 双路鉴别

**Files:**
- Modify: `src/api/access.py`（追加函数）
- Modify: `src/api/parse.py:71-97`（`simple_parse`）、`src/api/parse.py:100-283`（`_execute_parse`）
- Test: `tests/test_wx_login.py`（追加 `WxParseAuthTest`）

**Interfaces:**
- Consumes: `wx_sessions` 表、`daily_quota_cap`、`consume_rate_limits`、`user_is_expired`、`authenticate_wx_token()`（**已在 Task 5 的第 0 步写好** —— 本任务核对签名，并把它接进 `parse.py` 的双路鉴别）
- Produces:
  - `authenticate_wx_token() -> tuple[dict | None, tuple | None]`
    - 有令牌且合法 → `({"user_id": int, "id": None, "wx": True, "quota_cap": int | None}, None)`
    - 令牌存在但无效/过期/账号停用/过期 → `(None, (status, message, error_code))`
    - **没有 `X-WX-Token` 头 → `(None, None)`**（表示"不是我这条通道"，调用方继续试 API Key）
  - `quota_cap is None` 表示管理员不限次；`_execute_parse` 据此跳过预占

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
class WxParseAuthTest(unittest.TestCase):
    """设计文档 §2.5 / §4.1：令牌路径必须照抄密钥路径的四道检查。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
            "WX_APPID": "wx-test-appid",
            "WX_APPSECRET": "wx-test-appsecret",
        })
        self.client = self.app.test_client()
        with patch("src.api.wx.code2session",
                   return_value={"openid": "openid-alice", "session_key": "sk-alice"}):
            self.session = self.client.post("/api/v1/wx/login", json={"code": "c"}).get_json()["data"]
        self.token = self.session["token"]
        self.user_id = self.session["user"]["id"]

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def parser():
        parser = Mock()
        parser.get_title_content.return_value = "测试"
        parser.get_real_video_url.return_value = "https://example.com/a.mp4"
        parser.get_video_list.return_value = []
        parser.get_cover_photo_url.return_value = None
        parser.get_author_info.return_value = None
        parser.get_image_list.return_value = []
        parser.get_audio_url.return_value = None
        parser.get_subtitles.return_value = None
        parser.get_description.return_value = None
        return parser

    def parse(self, token=None):
        headers = {"X-WX-Token": token} if token else {}
        with patch("src.api.parse.WebFetcher.fetch_redirect_url",
                   return_value="https://www.douyin.com/video/123"):
            with patch("src.api.parse.ParserFactory.create_parser", return_value=self.parser()):
                return self.client.get("/api/v1/parse?url=https://example.com/share", headers=headers)

    def used_today(self):
        with self.app.app_context():
            return get_daily_usage(self.user_id, beijing_today())

    def test_valid_token_parses_and_counts_one(self):
        response = self.parse(self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.used_today(), 1)

    def test_free_tier_boundary_is_enforced_with_the_new_error_code(self):
        with self.app.app_context():
            db = get_db()
            set_setting("wx_free_daily_quota", 2)
            # 本用例测的是每日额度，不是限流：把 QPS 放开，别让第 3 次请求先撞上 429
            db.execute("UPDATE users SET qps_limit=50 WHERE id=?", (self.user_id,))
            db.commit()
        self.assertEqual(self.parse(self.token).status_code, 200)
        self.assertEqual(self.parse(self.token).status_code, 200)
        response = self.parse(self.token)
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.get_json()["error_code"], "DAILY_QUOTA_EXCEEDED")
        self.assertNotEqual(response.get_json()["error_code"], "INSUFFICIENT_CREDITS")
        self.assertEqual(self.used_today(), 2)

    def test_free_user_with_zero_credits_is_not_blocked_by_the_legacy_check(self):
        """§2.4 的陷阱：access.py:135 的 credits<=0 前置判定必须换成每日额度判定。"""
        with self.app.app_context():
            user = get_db().execute("SELECT credits FROM users WHERE id=?", (self.user_id,)).fetchone()
            self.assertEqual(user["credits"], 0)
        self.assertEqual(self.parse(self.token).status_code, 200)

    def test_member_gets_the_card_quota(self):
        with self.app.app_context():
            open_member_card(self.user_id, "day")
            db = get_db()
            set_setting("wx_free_daily_quota", 2)
            # 本用例测的是每日额度，不是限流：连发 5 次要放行
            db.execute("UPDATE users SET qps_limit=50 WHERE id=?", (self.user_id,))
            db.commit()
        for _ in range(5):
            self.assertEqual(self.parse(self.token).status_code, 200)
        self.assertEqual(self.used_today(), 5)

    def test_expired_member_falls_back_to_the_free_tier_not_403(self):
        with self.app.app_context():
            db = get_db()
            set_setting("wx_free_daily_quota", 1)
            db.execute("UPDATE users SET member_plan='year', member_expires_at=? WHERE id=?",
                       ("2020-01-01T00:00:00+00:00", self.user_id))
            db.commit()
        self.assertEqual(self.parse(self.token).status_code, 200)
        response = self.parse(self.token)
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.get_json()["error_code"], "DAILY_QUOTA_EXCEEDED")

    def test_failed_parse_refunds_the_reservation(self):
        with patch("src.api.parse.WebFetcher.fetch_redirect_url",
                   return_value="https://www.douyin.com/video/123"):
            with patch("src.api.parse.ParserFactory.create_parser",
                       side_effect=RuntimeError("解析炸了")):
                response = self.client.get("/api/v1/parse?url=https://example.com/share",
                                           headers={"X-WX-Token": self.token})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.used_today(), 0)

    @staticmethod
    def empty_parser():
        """链接有效、平台支持，但内容取不到 —— 业务层面的失败。

        和 `parser()` 的唯一区别是所有媒体字段都为空：走完预占、走进
        `MEDIA_NOT_FOUND` 那条分支，才谈得上"退回"。
        """
        parser = Mock()
        parser.get_title_content.return_value = None
        parser.get_real_video_url.return_value = None
        parser.get_video_list.return_value = []
        parser.get_cover_photo_url.return_value = None
        parser.get_author_info.return_value = None
        parser.get_image_list.return_value = []
        parser.get_audio_url.return_value = None
        parser.get_subtitles.return_value = None
        parser.get_description.return_value = None
        return parser

    def test_business_failure_also_refunds(self):
        """业务失败（能识别链接与平台，但媒体取不到）也要退回预占的额度。

        两层都必须 patch：不 patch 重定向就会真去请求 example.com，而
        `example.com/share` 在 URL 解析那步是**通过**的（`UrlParser.get_url`
        匹配任意 http(s) URL），断言就会跟着网络走。
        """
        with patch("src.api.parse.WebFetcher.fetch_redirect_url",
                   return_value="https://www.douyin.com/video/123"):
            with patch("src.api.parse.ParserFactory.create_parser",
                       return_value=self.empty_parser()):
                response = self.client.get("/api/v1/parse?url=https://example.com/share",
                                           headers={"X-WX-Token": self.token})
        self.assertEqual(response.status_code, 400)
        # 断到具体错误码，这一条同时证明"预占确实发生过"：MEDIA_NOT_FOUND 在
        # src/api/parse.py:198 抛出，而预占在 :146 —— 它在上游。少了这行断言，
        # used_today()==0 无论退没退都是 0（失败发生得更早，压根没预占），
        # 这条用例就白测了。
        self.assertEqual(response.get_json()["error_code"], "MEDIA_NOT_FOUND")
        self.assertEqual(self.used_today(), 0)

    def test_daily_quota_exhausted_does_not_touch_credits(self):
        with self.app.app_context():
            db = get_db()
            set_setting("wx_free_daily_quota", 1)
            db.execute("UPDATE users SET credits=5 WHERE id=?", (self.user_id,))
            db.commit()
        self.parse(self.token)
        self.assertEqual(self.parse(self.token).status_code, 402)
        with self.app.app_context():
            user = get_db().execute("SELECT credits FROM users WHERE id=?", (self.user_id,)).fetchone()
            self.assertEqual(user["credits"], 5)   # 硬停到次日，不回落、不扣余额

    def test_disabled_account_is_403_not_200(self):
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET active=0 WHERE id=?", (self.user_id,))
            db.commit()
        response = self.parse(self.token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error_code"], "ACCOUNT_DISABLED")

    def test_expired_account_is_403(self):
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET expires_at=? WHERE id=?",
                       ("2020-01-01T00:00:00+00:00", self.user_id))
            db.commit()
        response = self.parse(self.token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error_code"], "ACCOUNT_EXPIRED")

    def test_unknown_token_is_401(self):
        response = self.parse("bogus-token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "WX_TOKEN_INVALID")

    def test_admin_token_is_not_quota_limited(self):
        with self.app.app_context():
            db = get_db()
            set_setting("wx_free_daily_quota", 1)
            db.execute("UPDATE users SET role='admin' WHERE id=?", (self.user_id,))
            # 本用例测的是"管理员不扣费"，不是限流：连发 3 次要放行
            db.execute("UPDATE users SET qps_limit=50 WHERE id=?", (self.user_id,))
            db.commit()
        for _ in range(3):
            self.assertEqual(self.parse(self.token).status_code, 200)
        self.assertEqual(self.used_today(), 0)

    def test_token_and_api_key_share_one_rate_limit_bucket(self):
        """§2.5：令牌路径若另起一个 subject，同一用户就有两个互不相干的桶 = QPS 翻倍。"""
        # 限流桶是 1 秒定宽窗口，而本用例要求 3 次请求落进同一个桶 —— 不冻结时间的话，
        # 秒边界正好切在这 3 次之间就会把期望的 429 变成 200。test_management.py 里
        # 同一个做法用了 9 次（patch("src.api.access.time.time", ...)）。
        with patch("src.api.access.time.time", return_value=123456):
            raw_key = "mp_shared_bucket_key_123456"
            with self.app.app_context():
                db = get_db()
                # 这条腿走密钥路径，而密钥路径有积分墙（小程序路径故意没有，§4.4）——
                # 先把积分补上，否则它 402 INSUFFICIENT_CREDITS，等不到 200
                db.execute("UPDATE users SET qps_limit=2, credits=1 WHERE id=?", (self.user_id,))
                db.execute("INSERT INTO api_keys(user_id,name,key,created_at) VALUES(?,?,?,?)",
                           (self.user_id, "shared", raw_key, utcnow()))
                db.commit()

            for _ in range(2):
                self.assertEqual(self.parse(self.token).status_code, 200)
            token_blocked = self.parse(self.token).status_code
            self.assertEqual(token_blocked, 429)

            with self.app.app_context():
                db = get_db()
                db.execute("DELETE FROM rate_limit_buckets")   # 清桶，只验证"共用同一个桶"
                db.commit()
            # 两层都必须 patch：不 patch 就会真去请求 example.com（`UrlParser.get_url` 匹配任意
            # http(s) URL，URL 解析那步是**通过**的），这条腿永远到不了 200
            with patch("src.api.parse.WebFetcher.fetch_redirect_url",
                       return_value="https://www.douyin.com/video/123"):
                with patch("src.api.parse.ParserFactory.create_parser", return_value=self.parser()):
                    key_response = self.client.get("/api/v1/parse?url=https://example.com/share",
                                                   headers={"Authorization": f"Bearer {raw_key}"})
            self.assertEqual(key_response.status_code, 200)
            self.assertEqual(self.parse(self.token).status_code, 200)
            self.assertEqual(self.parse(self.token).status_code, 429)

    def test_api_key_path_still_enforces_the_credits_paywall(self):
        """§4.4：API Key 路径的 credits 付费墙行为必须与改造前逐字一致。"""
        raw_key = "mp_credits_paywall_key_12"
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET credits=1 WHERE id=?", (self.user_id,))
            db.execute("INSERT INTO api_keys(user_id,name,key,created_at) VALUES(?,?,?,?)",
                       (self.user_id, "paywall", raw_key, utcnow()))
            db.commit()

        with patch("src.api.parse.WebFetcher.fetch_redirect_url",
                   return_value="https://www.douyin.com/video/123"):
            with patch("src.api.parse.ParserFactory.create_parser", return_value=self.parser()):
                first = self.client.get("/api/v1/parse?url=https://example.com/share",
                                        headers={"Authorization": f"Bearer {raw_key}"})
                second = self.client.get("/api/v1/parse?url=https://example.com/share",
                                         headers={"Authorization": f"Bearer {raw_key}"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 402)
        self.assertEqual(second.get_json()["error_code"], "INSUFFICIENT_CREDITS")

    def test_no_credentials_keeps_the_legacy_response(self):
        response = self.parse()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "API_KEY_REQUIRED")
```

文件顶部补 `from unittest.mock import Mock, patch`（`patch` 已在 Task 5 引入，把 `Mock` 补上）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::WxParseAuthTest -v`
Expected: FAIL —— 令牌路径没接，`X-WX-Token` 被忽略，返回 401 `API_KEY_REQUIRED`。

- [ ] **Step 3: 写实现**

`src/api/access.py` 顶部的 import **Task 5 第 0 步已经改好了**（`hashlib` 与 `daily_quota_cap` 都在里面）。
核对下面这段与文件现状一致即可，**确认一致就不要再动**：

```python
import hashlib
import random
import re
import sys
import time

from flask import current_app, has_app_context, request, session

from src.auth import hash_api_key, legacy_hash_api_key, user_is_expired
from src.db import daily_quota_cap, get_db, setting, transaction, utcnow
```

`authenticate_wx_token()` **Task 5 第 0 步已经写进这个文件了**（`src/api/access.py:151`，紧跟在
`authenticate_api_key()` 之后 —— 也就是下面这段要你放的位置）。核对签名与文件现状一致即可，
**确认一致就不要再追加一遍**：再写一份不会报错，只会静静地覆盖前一个定义。

作为参考，文件里现在是这个（**不要复制粘贴**，只用来比对）：

```python
def authenticate_wx_token():
    """校验小程序登录令牌。

    返回三态：
      (access, None) —— 令牌合法，access 形如 {"user_id": int, "id": None, "wx": True, "quota_cap": int|None}
      (None, error)  —— 令牌存在但不合法（调用方直接返回该错误）
      (None, None)   —— **没有 X-WX-Token 头**，这不是错误，调用方继续走 API Key 路径
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
    # 照抄密钥路径的四道检查（§2.5）：停用与账号有效期必须查
    if not row["active"]:
        return None, (403, "账号已被停用", "ACCOUNT_DISABLED")
    if user_is_expired(row):
        return None, (403, "账号已到期", "ACCOUNT_EXPIRED")
    # 第三道检查换成每日额度 —— 在 _execute_parse 里做（那里才知道今天的用量）
    # 第四道：限流主体逐字复用 user:{id}，与密钥路径共享同一个桶
    limits = [(f"user:{row['id']}", row["qps_limit"], 1)]
    exceeded = consume_rate_limits(limits)
    if exceeded:
        return None, (429, f"请求过于频繁，当前账号限制为 {row['qps_limit']} QPS", "RATE_LIMITED")

    db.execute("UPDATE wx_sessions SET last_seen_at=? WHERE token_hash=?",
               (utcnow(), row["token_hash"]))
    db.commit()
    return {
        "user_id": row["id"],
        "id": None,                      # 小程序用户没有密钥行，日志里 api_key_id 为 NULL
        "wx": True,
        "quota_cap": None if row["role"] == "admin" else daily_quota_cap(row),
    }, None
```

`src/api/parse.py` 的 import 改成：

```python
from src.db import (
    beijing_today,
    consume_daily_quota,
    refund_daily_quota,
    refund_user_credit,
    reserve_user_credit,
)
from src.api.access import (
    authenticate_api_key,
    authenticate_wx_token,
    consume_rate_limit,
    demo_enabled,
    get_client_ip,
    global_api_enabled,
    platform_access,
    record_request,
)
```

`simple_parse()`（`src/api/parse.py:71-97`）的鉴权段改成：

```python
@bp.route('/v1/parse', methods=['GET', 'POST'])
def simple_parse():
    """面向客户的解析接口，支持 GET 与 POST。"""
    if not global_api_enabled():
        return make_response(503, 'API 服务已暂停', None, False, 'API_DISABLED'), 503
    is_api_only = bool(current_app and current_app.config.get('API_ONLY'))
    # 令牌路径先试，且**不受 API_ONLY 影响** —— 小程序是纯 API 调用方（§1.1）
    access, error = authenticate_wx_token()
    if error:
        status, message, code = error
        return make_response(status, message, None, False, code), status
    if access is None and not is_api_only:
        access, error = authenticate_api_key()
        if error:
            status, message, code = error
            return make_response(status, message, None, False, code), status
    # …以下读参数的部分保持原样
```

`_execute_parse()` 的开头（局部变量）加一个 `quota_reserved = False`、`quota_day = None`：

```python
def _execute_parse(text, access):
    started = time.monotonic()
    platform = None
    response = None
    status = 500
    credit_reserved = False
    credit_committed = False
    # 变量名沿用 credit_*：它现在同时表示"本次请求已预占额度"，成功时才提交
    quota_reserved = False
    quota_day = None
```

把 `if access and access["user_id"]:` 那段（`src/api/parse.py:146-157`）换成：

```python
        # 用 isinstance 判定通道：令牌路径给的是 dict，密钥路径给的是 sqlite3.Row，
        # 而 Row 没有 .get()，直接写 access.get('wx') 会在密钥路径上抛 AttributeError
        if isinstance(access, dict) and access.get('wx'):
            # 小程序用户：每日额度硬上限，原子预占；管理员（cap is None）不限次
            quota_day = beijing_today()
            if not consume_daily_quota(access["user_id"], quota_day, access.get("quota_cap")):
                response, status = make_response(
                    402,
                    '今日解析额度已用完，明日自动恢复',
                    None,
                    False,
                    'DAILY_QUOTA_EXCEEDED',
                ), 402
                return response, status
            quota_reserved = access.get("quota_cap") is not None
        elif access and access["user_id"]:
            # 既有 API Key 路径：credits 按次预扣，行为与改造前一致（§4.4）
            reservation = reserve_user_credit(access["user_id"])
            if reservation is None:
                response, status = make_response(
                    402,
                    '账号解析积分已耗尽，请联系管理员充值',
                    None,
                    False,
                    'INSUFFICIENT_CREDITS',
                ), 402
                return response, status
            credit_reserved = reservation
```

`finally` 段（`src/api/parse.py:265-270`）在退款之前插入：

```python
        if quota_reserved and not credit_committed:
            try:
                refund_daily_quota(access["user_id"], quota_day or beijing_today())
            except Exception:
                logger.exception("Reserved daily quota refund failed")
        if credit_reserved and not credit_committed:
```

> `quota_day` 用预占时记下的那一个，不在退款时重算 —— 请求若跨过北京零点，重算会退到隔天那一格，等于没退。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py -v`
Expected: PASS

- [ ] **Step 5: 跑全量回归（这一步是本计划最重要的一次回归）**

Run: `python -m pytest -q`
Expected: 全绿。**重点确认 `tests/test_management.py` 与 `tests/test_api_contract.py` 一条不红** —— 它们守着 API Key 路径的既有行为。

- [ ] **Step 6: Commit**

```bash
git add src/api/access.py src/api/parse.py tests/test_wx_login.py
git commit -m "feat(api): dual-path auth on /api/v1/parse with daily quota

设计文档 §2.4-§2.5：令牌路径复刻密钥路径的四道检查（第三道换成
每日额度判定），限流主体逐字复用 user:{id}；API Key 路径行为不变。"
```

---

## Task 9: 后台开卡入口

**Files:**
- Modify: `src/web/admin.py`（在 `update_user` 之后追加路由）
- Modify: `templates/admin/users.html`（行内下拉加「开卡」项、新增开卡弹窗、`openAdminOpenCardModal`）
- Test: `tests/test_wx_login.py`（追加 `AdminCardTest`）
- Modify: `docker-compose.yml`（`environment:` 白名单加 `WX_APPID` / `WX_APPSECRET`）
- Modify: `.env.example`（记录这两个变量）

**Interfaces:**
- Consumes: `open_member_card(user_id, plan_code)`（Task 3）
- Produces: `POST /admin/users/<int:user_id>/member-card`，表单字段 `plan_code`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
class AdminCardTest(unittest.TestCase):
    """设计文档 §6.4：没有开卡入口，会员就只能靠手写 SQL 发放。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
            "WX_APPID": "wx-test-appid",
            "WX_APPSECRET": "wx-test-appsecret",
        })
        self.client = self.app.test_client()
        with patch("src.api.wx.code2session",
                   return_value={"openid": "openid-alice", "session_key": "sk-alice"}):
            self.session = self.client.post("/api/v1/wx/login", json={"code": "c"}).get_json()["data"]
        self.user_id = self.session["user"]["id"]
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET role='admin', password_hash='unused' WHERE id=?",
                       (self.user_id,))
            db.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        return "test-csrf"

    def test_admin_can_grant_a_card(self):
        # 直接用会话登录管理员：setUp 给这个账号写的 password_hash 是占位串，走 /auth/login
        # 必然失败。那次调用不该留在用例里 —— 它的结果无人断言，读起来却像"管理员身份是登录
        # 得来的"，而真正授权的是下面这两行 session 直插。
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["role"] = "admin"

        # 目标用户另建一个普通微信用户
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,role,active,qps_limit,credits,openid,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("wx_openid-bob", "x", "user", 1, 2, 0, "openid-bob", utcnow()),
            )
            target_id = cursor.lastrowid
            db.commit()

        response = self.client.post(
            f"/admin/users/{target_id}/member-card",
            data={"csrf_token": self.csrf(), "plan_code": "year"},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
            self.assertEqual(user["member_plan"], "year")
            self.assertIsNotNone(user["member_expires_at"])
            self.assertTrue(is_member(user))

    def test_unknown_plan_flashes_an_error_without_changing_anything(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["role"] = "admin"
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,role,active,qps_limit,credits,openid,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("wx_openid-carol", "x", "user", 1, 2, 0, "openid-carol", utcnow()),
            )
            target_id = cursor.lastrowid
            db.commit()
        response = self.client.post(f"/admin/users/{target_id}/member-card",
                                    data={"csrf_token": self.csrf(), "plan_code": "lifetime"})
        # 必须断言守卫真的开了口，不能只看 member_plan 没变：删掉 open_member_card 里那个
        # `if plan is None` 守卫，下一行 plan["days"] 就抛 TypeError → 500，而 transaction()
        # 会回滚 —— member_plan 照样是 None。只断言 None 的话，守卫被删掉这条用例仍然全绿。
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            flashes = session.get("_flashes") or []
        self.assertIn(("error", "卡种不存在或已下架"), flashes)
        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
            self.assertIsNone(user["member_plan"])

    def test_non_admin_is_rejected(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 999
        response = self.client.post(
            f"/admin/users/{self.user_id}/member-card",
            data={"csrf_token": self.csrf(), "plan_code": "day"},
        )
        self.assertIn(response.status_code, (302, 403))
        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE id=?", (self.user_id,)).fetchone()
            self.assertIsNone(user["member_plan"])
```

> `test_admin_can_grant_a_card` 里**不要**再写 `/auth/login`：`setUp` 写的密码是占位 hash，那次登录必然失败，
> 而它的失败无人断言 —— 留着只会让人以为管理员身份是登录来的。授权靠的是直插 `session["user_id"]`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::AdminCardTest -v`
Expected: FAIL —— 404（路由不存在）。

- [ ] **Step 3: 写实现**

`src/web/admin.py` 的 db import 补上 `open_member_card`：

```python
from src.db import get_db, open_member_card, set_setting, utcnow
```
（按该文件现有的 import 行补齐，不要新增重复的 import 语句。）

在 `update_user` 之后追加：

```python
@bp.post("/users/<int:user_id>/member-card")
@admin_required
@csrf_protected
def grant_member_card(user_id):
    """运营手动开卡。第二期接入虚拟支付后，支付回调走同一个 open_member_card()。"""
    plan_code = (request.form.get("plan_code") or "").strip()
    ok, message, _expires = open_member_card(user_id, plan_code)
    flash(message, "success" if ok else "error")
    return redirect(url_for("admin.users"))
```

`templates/admin/users.html`：

(a) 行内下拉里，在「重置密码」按钮之前插入一个「开卡」按钮：

```html
                                            <button type="button" class="action-dropdown-item w-full flex items-center gap-2 px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
                                                onclick="closeAllActionDropdowns(); openAdminOpenCardModal({{ user['id'] }}, '{{ user['username']|e }}')">
                                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>
                                                <span>会员开卡</span>
                                            </button>
```

(b) 在「重置客户密码弹窗」之前插入开卡弹窗（结构照抄既有弹窗，样式类全部复用）：

```html
    <!-- 会员开卡弹窗 -->
    <div id="admin-open-card-modal-backdrop" class="modal-backdrop fixed inset-0 z-50 bg-slate-900/40 backdrop-blur-xs flex items-center justify-center p-4" style="display:none" aria-hidden="true">
        <div class="modal-card bg-white rounded-2xl border border-slate-200 shadow-2xl max-w-md w-full p-6" role="dialog" aria-modal="true">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-base font-bold text-slate-900 m-0">会员开卡</h3>
                <button type="button" class="text-slate-400 hover:text-slate-700 text-lg leading-none" onclick="closeAdminOpenCardModal()" aria-label="关闭">&times;</button>
            </div>
            <form id="admin-open-card-form" method="post" action="" class="space-y-4">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <p class="text-xs text-slate-500">正在为客户账号「<strong id="admin-open-card-username" class="text-slate-900 font-bold"></strong>」开通会员。</p>
                <div class="space-y-1">
                    <label class="block text-xs font-semibold text-slate-700">卡种</label>
                    <select id="admin-open-card-plan" name="plan_code" class="w-full px-3.5 py-2 text-sm rounded-lg border border-slate-200 bg-white">
                        {% for plan in member_plans %}
                        <option value="{{ plan['code'] }}">{{ plan['name'] }} · {{ plan['days'] }} 天 · 每日 {{ plan['daily_quota'] }} 次</option>
                        {% endfor %}
                    </select>
                </div>
                <p class="text-xs text-slate-500 leading-relaxed">已持有会员时会**叠加**到期时间；若原卡种每日额度更高，卡种保持不变（额度只升不降）。</p>
                <div class="flex items-center justify-end gap-3 pt-4 border-t border-slate-100">
                    <button type="button" class="px-4 py-2 rounded-lg text-sm font-semibold border border-slate-200 bg-white text-slate-700 hover:bg-slate-50" onclick="closeAdminOpenCardModal()">取消</button>
                    <button type="submit" class="px-4 py-2 rounded-lg text-sm font-semibold text-white bg-brand hover:bg-indigo-700 shadow-xs">确认开卡</button>
                </div>
            </form>
        </div>
    </div>
```

把那段说明里多余的 `**…**` 去掉（HTML 里不渲染 Markdown）：`已持有会员时会叠加到期时间；若原卡种每日额度更高，卡种保持不变（额度只升不降）。`

(c) 脚本里加 JS（放在 `closeAdminResetPasswordModal` 附近）：

```js
function openAdminOpenCardModal(userId, username) {
    const m = document.getElementById('admin-open-card-modal-backdrop');
    const form = document.getElementById('admin-open-card-form');
    if (!m || !form) return;
    form.action = '/admin/users/' + userId + '/member-card';
    document.getElementById('admin-open-card-username').value = username;
    m.style.display = 'flex';
    void m.offsetHeight;
    m.classList.add('modal-open');
}

function closeAdminOpenCardModal() {
    const m = document.getElementById('admin-open-card-modal-backdrop');
    if (!m) return;
    m.classList.remove('modal-open');
    setTimeout(() => { m.style.display = 'none'; }, 180);
}
```

(d) `admin.users` 视图要把 `member_plans` 传给模板。`src/web/admin.py` 的 `users()` 里 `render_template` 之前加：

```python
    member_plans = db.execute(
        "SELECT * FROM member_plans WHERE active=1 ORDER BY sort_order, code"
    ).fetchall()
```
并把 `member_plans=member_plans` 加进 `render_template(...)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py::AdminCardTest -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 手工验证一次页面**

Run: `python app.py`，浏览器打开 `http://127.0.0.1:8051/admin/users`，用管理员登录，对任一普通用户点「⋯ → 会员开卡」，选「月卡」，确认后该用户行的到期日期不显示、但 `/admin/users` 页面上该客户不再报错，且数据库里 `member_plans` 的额度生效（可在请求日志或直接查库确认）。

- [ ] **Step 6: 把微信凭据接进部署（漏了这步，容器里的登录会永远 503）**

`docker-compose.yml` 的 `environment:` 是**逐项白名单**（没有 `env_file:`），宿主机 `.env` 里的
`WX_APPID` / `WX_APPSECRET` **不会**自动进容器。不加这两行，`wx_configured()` 恒为 False，
`/api/v1/wx/login` 永远返回 503，真机根本登不进来。

1. 在 `docker-compose.yml` 的 `environment:` 里、紧跟 `API_ONLY=${API_ONLY:-false}` 之后加两行：
   - `WX_APPID=${WX_APPID:-}`
   - `WX_APPSECRET=${WX_APPSECRET:-}`
2. 在 `.env.example` 里补上这两个变量，注一句取值来源（微信公众平台 → 开发管理 → 开发设置）。

Run:
```bash
docker compose up -d --build
docker compose exec web env | grep WX_          # 键必须在，值可以是空的
curl -s -o /dev/null -w '%{http_code}
' -X POST http://localhost:8051/api/v1/wx/login   -H 'Content-Type: application/json' -d '{"code":"x"}'
```

Expected: `env` 里能看到 `WX_APPID=` / `WX_APPSECRET=`；`curl` 返回 **400**（code 无效）
而**不是 503**（未配置）。503 就说明这两行没生效 —— 服务名是 `web`，端口是 `8051`，都在
`docker-compose.yml` 里核实过。

- [ ] **Step 7: Commit**

```bash
git add src/web/admin.py templates/admin/users.html tests/test_wx_login.py
git commit -m "feat(admin): add manual member card issuance"
```

---

## Task 10: 卡种管理页 + 免费档额度与签到奖励设置

**Files:**
- Modify: `src/web/admin.py`（设置项 + 新路由）
- Create: `templates/admin/member_plans.html`
- Modify: `templates/base_console.html:66-72`（侧栏项 + 面包屑）
- Modify: `templates/admin/settings.html`（两个输入框）
- Test: `tests/test_wx_login.py`（追加 `AdminMemberPlanSettingsTest`）

**Interfaces:**
- Consumes: `member_plans` 表、`set_setting`
- Produces:
  - `GET /admin/member-plans`、`POST /admin/member-plans/<code>`
  - 设置项 `wx_free_daily_quota`、`wx_checkin_bonus`（写 `system_settings`）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
class AdminMemberPlanSettingsTest(unittest.TestCase):
    """设计文档 §6.4：把卡种做成数据而不是代码常量，改额度不改代码。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
        })
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?)",
                ("boss", "unused", "admin", 1, utcnow()),
            )
            self.admin_id = cursor.lastrowid
            db.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        return "test-csrf"

    def as_admin(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.admin_id
            session["role"] = "admin"

    def test_member_plans_page_loads(self):
        self.as_admin()
        response = self.client.get("/admin/member-plans")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for name in ("日卡", "月卡", "季卡", "年卡"):
            self.assertIn(name, html)
        # 顺带钉住行内表单的写法：控件必须用 form="plan-…" 按 id 关联到同一个 <form>，而不是被
        # 一个 form 包住 —— form 包 <td> 是非法的，浏览器会把它提出表格，保存按钮随之失效。
        # 旧的非法写法下这两条字符串都不出现，所以这两行不是摆设。
        self.assertIn('id="plan-day"', html)
        self.assertIn('form="plan-day"', html)

    def test_editing_a_plan_changes_the_effective_cap(self):
        self.as_admin()
        response = self.client.post("/admin/member-plans/day",
                                    data={"csrf_token": self.csrf(), "name": "日卡",
                                          "days": "1", "daily_quota": "150", "active": "1"})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            plan = get_db().execute("SELECT * FROM member_plans WHERE code='day'").fetchone()
            self.assertEqual(plan["daily_quota"], 150)
            cursor = get_db().execute(
                "INSERT INTO users(username,password_hash,role,active,qps_limit,credits,openid,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("wx_x", "x", "user", 1, 2, 0, "openid-x", utcnow()),
            )
            get_db().commit()
            open_member_card(cursor.lastrowid, "day")
            user = get_db().execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
            self.assertEqual(daily_quota_cap(user), 150)

    def test_reactivating_a_plan(self):
        self.as_admin()
        self.client.post("/admin/member-plans/day",
                         data={"csrf_token": self.csrf(), "name": "日卡", "days": "1",
                               "daily_quota": "200"})   # 不勾 active => 下架
        with self.app.app_context():
            self.assertIsNone(get_member_plan("day"))

    def test_settings_page_and_save_expose_the_free_tier_quota(self):
        self.as_admin()
        html = self.client.get("/admin/settings").get_data(as_text=True)
        self.assertIn('name="wx_free_daily_quota"', html)
        self.assertIn('name="wx_checkin_bonus"', html)

        response = self.client.post("/admin/settings", data={
            "csrf_token": self.csrf(),
            "wx_free_daily_quota": "5",
            "wx_checkin_bonus": "20",
        })
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(setting("wx_free_daily_quota"), "5")
            self.assertEqual(setting("wx_checkin_bonus"), "20")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::AdminMemberPlanSettingsTest -v`
Expected: FAIL —— 页面 404；settings 的 HTML 里没有那两个 input。

- [ ] **Step 3: 写实现**

`src/web/admin.py`：

在 `update_settings` 里 `set_setting("default_user_qps", …)` 之后插入：

```python
    set_setting("wx_free_daily_quota", _positive_int(request.form.get("wx_free_daily_quota"), DEFAULT_FREE_DAILY_QUOTA, 100000))
    set_setting("wx_checkin_bonus", _positive_int(request.form.get("wx_checkin_bonus"), DEFAULT_CHECKIN_BONUS, 100000))
```

db import 补 `DEFAULT_CHECKIN_BONUS`、`DEFAULT_FREE_DAILY_QUOTA`、`get_member_plan`（若用到）。

在 `users()` 附近追加卡种管理路由：

```python
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
```

新建 `templates/admin/member_plans.html`（`{% extends %}` 与 `active_nav` 的关键字照抄 `templates/admin/users.html` 的头部，样式类全部复用）：

```html
{% extends "base_console.html" %}
{% block title %}会员卡种 · 管理员后台{% endblock %}
{% block page_title %}会员卡种{% endblock %}
{% block console_content %}
<main class="p-6">
    <div class="mb-5">
        <h1 class="text-lg font-bold text-slate-900 m-0">会员卡种</h1>
        <p class="text-xs text-slate-500 mt-1">每日额度与时长改动后立即对新开的卡生效；已开出的卡按开卡时的天数固化在到期时间上。</p>
    </div>

    <div class="card bg-white rounded-2xl border border-slate-200 shadow-xs overflow-hidden">
        <table class="w-full text-sm">
            <thead class="bg-slate-50 text-slate-500 text-xs">
                <tr>
                    <th class="px-4 py-3 text-left">代号</th>
                    <th class="px-4 py-3 text-left">名称</th>
                    <th class="px-4 py-3 text-left">时长（天）</th>
                    <th class="px-4 py-3 text-left">每日额度</th>
                    <th class="px-4 py-3 text-left">上架</th>
                    <th class="px-4 py-3 text-right">操作</th>
                </tr>
            </thead>
            <tbody>
                {% for plan in plans %}
                <tr class="border-t border-slate-100">
                    {# <form> 不能包住 <td>：<tr> 的内容模型只允许单元格，浏览器会把提不进去的
                       <form> 提到表格外，里面的控件随之失去 form owner —— 保存按钮点了没反应。
                       所以 form 只放在代号那个 <td> 里（带 csrf 隐藏域），其余控件一律用
                       form="plan-<code>" 按 id 关联回来。 #}
                    <td class="px-4 py-3 font-mono text-xs text-slate-500">
                        <form id="plan-{{ plan['code'] }}" method="post" action="{{ url_for('admin.update_member_plan', code=plan['code']) }}" class="m-0">
                            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                        </form>
                        {{ plan['code'] }}
                    </td>
                    <td class="px-4 py-3">
                        <input form="plan-{{ plan['code'] }}" type="text" name="name" value="{{ plan['name'] }}" maxlength="20" class="w-32 px-3 py-1.5 text-sm rounded-lg border border-slate-200">
                    </td>
                    <td class="px-4 py-3">
                        <input form="plan-{{ plan['code'] }}" type="number" name="days" value="{{ plan['days'] }}" min="1" max="3650" class="w-24 px-3 py-1.5 text-sm rounded-lg border border-slate-200">
                    </td>
                    <td class="px-4 py-3">
                        <input form="plan-{{ plan['code'] }}" type="number" name="daily_quota" value="{{ plan['daily_quota'] }}" min="1" max="100000" class="w-28 px-3 py-1.5 text-sm rounded-lg border border-slate-200">
                    </td>
                    <td class="px-4 py-3">
                        <input form="plan-{{ plan['code'] }}" type="checkbox" name="active" value="1" {{ 'checked' if plan['active'] }} class="rounded border-slate-300 text-brand">
                    </td>
                    <td class="px-4 py-3 text-right">
                        <button form="plan-{{ plan['code'] }}" type="submit" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-white bg-brand hover:bg-indigo-700">保存</button>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</main>
{% endblock %}
```

> 上面这段的两个写法都已核实，**照着写，不要改**：
> - block 名是 **`console_content`**，不是 `content`。`templates/base_console.html` 只声明了
>   `title`、`body`、`page_title`、`console_content` 四个 block，`templates/admin/` 下七个模板
>   全部用 `console_content`。子模板声明一个父模板没有的 block，**Jinja 不报错，直接不渲染** ——
>   页面会返回 200 但正文空白。`test_member_plans_page_loads` 断言四个卡种名在 HTML 里，正好兜住。
> - 行内表单用 `form="plan-<code>"` 按 id 关联，`<form>` 自己只放在代号那个 `<td>` 里。
>   **不要**照抄 `users.html`：那个文件每行的 `<form>` 之所以能包在 `<td>` 里，是因为它需要的
>   控件（csrf、active、提交按钮）本来就都在同一个 `<td>` 内；本页的输入框分布在六个单元格里，
>   照抄就得让 form 去包 `<td>` —— 那正是浏览器会把 form 提到表格外、保存按钮失效的写法。
> - 这条别指望用浏览器试：本机 `python app.py` 起不来（`app.py:3` 的 `import fcntl` 是 Unix-only）。
>   结构是否写对，由用例里那两条 `assertIn('form="plan-day"', html)` 兜住。

侧栏 `templates/base_console.html`：在「客户账号」那一行之后加：

```html
            <a href="{{ url_for('admin.member_plans') }}" class="nav-item flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors {{ 'bg-indigo-50/80 text-brand font-semibold shadow-2xs' if active_nav == 'member_plans' else 'text-slate-600 hover:bg-slate-50 hover:text-slate-900' }}">
                <span class="w-5 h-5 flex items-center justify-center shrink-0">{{ icon('layers', 'w-4 h-4') }}</span><span>会员卡种</span>
            </a>
```

并把第 168 行的面包屑分支从 `{%- elif active_nav in ['users', 'keys', 'platforms'] -%}` 改成 `{%- elif active_nav in ['users', 'keys', 'platforms', 'member_plans'] -%}`。

`templates/admin/settings.html`：在「初始积分」那一块之后加两个输入框（照抄相邻 `input_default_user_qps` 的写法，含 `id`、`name`、`value`、`class`）：

```html
                                            <input type="number" min="1" max="100000"
                                                id="input_wx_free_daily_quota" name="wx_free_daily_quota"
                                                value="{{ settings.get('wx_free_daily_quota', '10') }}"
                                                class="w-full px-3.5 py-2 text-sm rounded-lg border border-slate-200 focus:outline-none focus:border-brand">
```

（标签文案：`免费用户每日额度`；紧接着同样加一个 `input_wx_checkin_bonus` / `name="wx_checkin_bonus"`，标签 `每日签到奖励次数`，默认值 `10`。）

同时把该文件脚本里那条「这几个字段改动即自动保存」的白名单（第 467 行）补上新字段：

```js
        if (name === 'default_user_qps' || name === 'default_trial_days' || name === 'chk_unlimited_trial' || name === 'default_initial_credits' || name === 'chk_unlimited_credits' || name === 'wx_free_daily_quota' || name === 'wx_checkin_bonus') {
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py::AdminMemberPlanSettingsTest -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 手工验证卡种页**

Run: `python app.py` → `/admin/member-plans`，把日卡额度改成 150 并保存，再改回 200。确认列表刷新后显示的是新值。

- [ ] **Step 6: Commit**

```bash
git add src/web/admin.py templates/admin/member_plans.html templates/admin/settings.html templates/base_console.html tests/test_wx_login.py
git commit -m "feat(admin): manage member plans and free-tier quota from the console"
```

---

## Task 11: 关掉门户的用户自助建密钥入口

**Files:**
- Modify: `src/web/portal.py:206-230`（删除 `create_key` 视图；紧跟其后的 `@bp.post("/keys/batch")` 起是保留区）
- Modify: `templates/portal/keys.html`（删除创建入口三件套：`:63` 的「+ 创建 API Key」按钮、`:185` 的创建弹窗、`:251` 的 `openPortalCreateKeyModal` JS）
- Modify: `src/web/portal.py:123`（`create_key` 删掉后没人再写 `session["new_api_key"]`，这行 `new_api_key=session.pop(...)` 成为死代码，一并去掉）
- Test: `tests/test_wx_login.py`（追加 `PortalKeyCreationClosedTest`）

**Interfaces:**
- 关闭：`POST /console/keys`（创建）、每用户 10 把的上限判断
- 保留：`GET /console/keys`（列表）、`POST /console/keys/batch`（批量启用/停用/删除）、`POST /console/keys/<id>/toggle`
- 理由见「计划补充与偏离说明 B」

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_wx_login.py`：

```python
class PortalKeyCreationClosedTest(unittest.TestCase):
    """设计文档 §6.2：门户不对用户开放后，自助建密钥是一处无人维护的授权入口。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
        })
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,role,active,credits,created_at) VALUES(?,?,?,?,?,?)",
                ("portal_user", "unused", "user", 1, 100, utcnow()),
            )
            self.user_id = cursor.lastrowid
            db.commit()

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        return "test-csrf"

    def test_portal_can_no_longer_create_keys(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        response = self.client.post("/console/keys",
                                    data={"csrf_token": self.csrf(), "name": "自己建的"})
        # 期望是 405，不是 404：本任务**保留**了同路径的 GET /console/keys 列表页（portal.py:81），
        # 路径还在、只是 POST 方法没了 —— Werkzeug 只在整条规则消失时才给 404，这里必然是 405
        # （Method Not Allowed）。405 同样证到「创建入口已关闭」，比放宽成 assertIn(..., (404, 405)) 更紧。
        self.assertEqual(response.status_code, 405)
        with self.app.app_context():
            count = get_db().execute("SELECT COUNT(*) c FROM api_keys WHERE user_id=?",
                                    (self.user_id,)).fetchone()["c"]
            self.assertEqual(count, 0)

    def test_portal_keys_page_has_no_creation_form(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            # 种一个假密钥：若「创建成功」那段还在渲染，它必然把明文打到页面上（该片段 :20 的
            # {{ new_api_key }}）。不种的话那段根本不渲染，下面那条断言就是空的。
            session["new_api_key"] = "mp_must_not_be_rendered"
        html = self.client.get("/console/keys").get_data(as_text=True)
        # 创建入口是「按钮(:63) + 弹窗(:185) + JS(:251)」三件套，两个断言合起来才盖得住：
        #   openPortalCreateKeyModal 只出现在 :63 的 onclick 与 :251 的函数定义（共 2 处），
        #   弹窗 :185 靠它自己的注释 <!-- 创建 API Key 弹窗 --> 被「创建 API Key」钉住。
        # 单用任何一个都会漏：删了按钮和 JS 却留着弹窗时，第一条过、第二条红；只删 JS 时第一条红。
        # 注意别用「创建密钥」或「一键复制」当断言：前者在本页出现 **0 次**（只在
        # components/api_docs.html:523 的排查文案与已删视图的 flash 里），恒真；后者在本页 2 次 ——
        # :18 在要删的成功提示里，:61 在**保留下来的**列表说明里（「可通过小眼睛切换明文或一键复制」），
        # 所以它永远不可能通过 —— 恒假。两条都不能用。
        self.assertNotIn("openPortalCreateKeyModal", html)
        self.assertNotIn("创建 API Key", html)
        self.assertNotIn("mp_must_not_be_rendered", html)
        # 列表与批量管理必须留着：本任务只关创建，别把整页删空
        self.assertIn("API 密钥", html)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_wx_login.py::PortalKeyCreationClosedTest -v`
Expected: FAIL —— `POST /console/keys` 目前返回 302（创建成功）。

- [ ] **Step 3: 写实现**

删除 `src/web/portal.py` 中整个 `create_key` 视图（含 `@bp.post("/keys")` 三个装饰器，约 `portal.py:206-232`），并在原处留一条注释说明为什么删：

```python
# 2026-09-22 移除：门户不对用户开放后，自助建密钥是一处无人维护的授权入口（docs/wx-login.md §6.2）。
# 密钥此后只服务「运营自用」与「白名单客户程序化对接」两类用途，由管理员在 /admin/keys 发放。
# 小程序走 X-WX-Token，不再需要任何密钥。
```

`templates/portal/keys.html`（**注意是 `portal/`，不是 `console/`** —— 仓库里没有 `templates/console/` 这个目录）：删掉创建入口的**三处**，它们缺一不可：

1. `:63` 的按钮 `<button … onclick="openPortalCreateKeyModal()">+ 创建 API Key</button>`
2. `:185` 起整个创建弹窗（`<!-- 创建 API Key 弹窗 -->`，含 `:192` 那个 `action="{{ url_for('portal.create_key') }}"` 的表单，一直到该弹窗的收尾）
3. `:251` 的 `function openPortalCreateKeyModal()`（及它绑定的按钮/listener）

同时删掉 `:8-52` 的「密钥创建成功」弹窗（`{% if new_api_key %}` 那整段）。**保留**列表、筛选、分页与批量操作区。

`src/web/portal.py:123`：`create_key` 没了之后没有任何代码再写 `session["new_api_key"]`，把这行 `new_api_key=session.pop("new_api_key", None),` 去掉。**但不要把 `new_api_key` 从别处一起铲掉** —— `templates/components/api_docs.html`（经 `templates/portal/docs.html` 渲染）仍然读它。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_wx_login.py::PortalKeyCreationClosedTest -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 跑全量回归**

Run: `python -m pytest -q`
Expected: 全绿。**特别确认 `tests/test_management.py::ManagementTest::test_portal_batch_keys_select_all` 仍然通过** —— 它测的 `/console/keys/batch` 是保留下来的批量管理，不是创建。

- [ ] **Step 6: Commit**

```bash
git add src/web/portal.py templates/portal/keys.html tests/test_wx_login.py
git commit -m "feat(portal): remove self-service api key creation

设计文档 §6.2：门户不对用户开放，自助建密钥入口失去消费者。
列表与批量管理保留（运营仍需要）。"
```

**✅ 服务端批次到此结束。** 跑 `python -m pytest -q` 全绿、真机验证一次小程序解析成功后，本批次可以独立上线观察（设计文档 §6.3 第 1、2 步）。客户端任务（Task 12 起）在客户端仓库里进行。

---

## Task 12: 客户端 `utils/auth.js`（登录与统一请求层）

**Files:**
- Create: `D:\ClaudeCodeProject\DeMark\miniprogram\utils\errors.js`（`ERROR_MESSAGES` 表从 `parser.js` 原样搬出来，单独立模块）
- Create: `D:\ClaudeCodeProject\DeMark\miniprogram\utils\auth.js`
- Create: `D:\ClaudeCodeProject\DeMark\scripts\test-auth.js`
- Modify: `D:\ClaudeCodeProject\DeMark\miniprogram\utils\parser.js`（补 `DAILY_QUOTA_EXCEEDED` 文案；改从 `./errors` 取 `ERROR_MESSAGES`，并原样转出）

**Interfaces:**
- Consumes: `config.js` 的 `API_BASE_URL`
- Produces:
  - `ensureLogin() -> Promise<{token, ...}>` —— 有有效 token 直接返回，否则 `wx.login` → login 接口；**并发只登一次**
  - `request({path, method, data}) -> Promise<data>` —— 自动带 `X-WX-Token`；收到 401 令牌失效时**清 token 重登并只重试一次**
  - `readToken() -> string`、`clearToken() -> void`
  - `utils/errors.js` 导出 `ERROR_MESSAGES`；`auth.js` 与 `parser.js` 都从这里取（`parser.js` 另外原样转出，保持既有对外形态）

- [ ] **Step 1: 写失败测试**

新建 `scripts/test-auth.js`（沿用项目既有 `[ok]` / `[FAIL]` + 退出码约定）：整份文件是一个 `main()` async 函数，顺序 `await`，末尾按 `failures` 收退出码；六组用例全程 stub 掉 `wx.login` / `wx.request` / `wx.uploadFile`，不联网、不消耗 `code2Session` 额度。


```js
// 微信登录与统一请求层验证
// 用法：node scripts/test-auth.js
//
// 全程 stub 掉 wx.login 与 wx.request，不联网、不消耗 code2Session 额度。
const path = require('path')

let failures = 0
const fail = (msg) => { failures++; console.log('  [FAIL] ' + msg) }
const ok = (msg) => console.log('  [ok] ' + msg)

const ROOT = path.join(__dirname, '..', 'miniprogram')

// auth.js 模块内有 in-flight Promise 缓存，用例之间必须清 require.cache 才不串味
function freshAuth(wxStub) {
  Object.keys(require.cache).forEach((key) => {
    if (key.indexOf(path.join(ROOT, 'utils')) >= 0 || key.indexOf(path.join(ROOT, 'config.js')) >= 0) {
      delete require.cache[key]
    }
  })
  global.wx = wxStub
  return require(path.join(ROOT, 'utils', 'auth.js'))
}

function makeWx(overrides) {
  const store = (overrides && overrides.store) || {}
  const base = {
    getStorageSync: (k) => (k in store ? store[k] : ''),
    setStorageSync: (k, v) => { store[k] = v },
    removeStorageSync: (k) => { delete store[k] },
    login: (opts) => opts.success({ code: 'code-1' }),
    request: () => { throw new Error('本用例不该发起请求') },
    uploadFile: () => { throw new Error('本用例不该上传文件') }
  }
  return Object.assign(base, overrides || {})
}

function loginSuccess(token, expiresAt) {
  return {
    statusCode: 200,
    data: { retcode: 200, retdesc: '成功', succ: true, data: { token, expires_at: expiresAt, user: { id: 42 } } }
  }
}

function failBody(errorCode, message) {
  return { statusCode: 402, data: { retcode: 402, retdesc: message, succ: false, error_code: errorCode, data: null } }
}

const TOKEN_KEY = 'jingji_wx_token'
const EXPIRES_KEY = 'jingji_wx_token_expires'
const FUTURE_ISO = new Date(Date.now() + 20 * 86400000).toISOString()

async function main() {
  // ---------- 1. 已有有效 token 时完全不调用 wx.login ----------
  console.log('\n== 1. token 缓存 ==')
  {
    let loginCalls = 0
    const store = { [TOKEN_KEY]: 'cached-token', [EXPIRES_KEY]: Date.parse(FUTURE_ISO) }
    const auth = freshAuth(makeWx({
      store,
      login: () => { loginCalls++; throw new Error('不该调用 wx.login') }
    }))
    const session = await auth.ensureLogin()
    if (loginCalls === 0 && session.token === 'cached-token') ok('已有有效 token 不调用 wx.login')
    else fail(`应复用缓存 token，实际 loginCalls=${loginCalls} token=${session.token}`)
  }

  // ---------- 2. 401 只重试一次 ----------
  console.log('\n== 2. 401 静默重登 ==')
  {
    // 必须带一个有效 token 进这个用例。store 为空时 request() 得先 ensureLogin，于是第 1 次
    // wx.request 是登录 POST：stub 的 `requestCalls === 1` 分支只对第 1 次请求返回 401，而这一分支
    // 现在被登录吃掉了 —— 真实请求拿到的是 200，401 一次都不会出现。用例名叫「401 静默重登」却
    // 从未见过 401，删掉整段重登逻辑它照样全绿。种上 token，第 1 次请求才是真实请求。
    const store = { [TOKEN_KEY]: 'cached-token', [EXPIRES_KEY]: Date.parse(FUTURE_ISO) }
    let loginCalls = 0
    let requestCalls = 0
    const auth = freshAuth(makeWx({
      store,
      login: (opts) => { loginCalls++; opts.success({ code: `code-${loginCalls}` }) },
      request: (opts) => {
        requestCalls++
        if (requestCalls === 1) {
          if (opts.url.indexOf('/wx/login') >= 0) opts.success(loginSuccess('t1', FUTURE_ISO))
          else opts.success({ statusCode: 401, data: { succ: false, error_code: 'WX_TOKEN_EXPIRED', retdesc: '过期' } })
          return
        }
        if (opts.url.indexOf('/wx/login') >= 0) opts.success(loginSuccess('t2', FUTURE_ISO))
        else opts.success({ statusCode: 200, data: { succ: true, retcode: 200, data: { ok: true } } })
      }
    }))
    const result = await auth.request({ path: '/api/v1/wx/me' })
    // 种了 token 之后真实计数是 3：请求①带旧 token 拿 401 → 重登（请求②）→ 重试（请求③）。
    // 原来写的 `<= 2` 既放过了空 store 的空转版本，也分不出「重试了一次」和「压根没重试」。
    if (result && result.ok === true && requestCalls === 3) ok(`401 后重登并重试一次（总请求数 ${requestCalls}）`)
    else fail(`期望重试一次后成功，实际 requestCalls=${requestCalls} result=${JSON.stringify(result)}`)
  }

  // ---------- 3. 连续 401 不再无限重登 ----------
  console.log('\n== 3. 连续 401 必须放弃 ==')
  {
    // 同样必须种 token：store 为空时登录会先占掉第 1 次请求，整个计数变成 4，本用例的 === 3 直接失败
    // （而失败原因看着像「放弃逻辑写错了」，很容易被改成放宽断言而不是修好前置条件）。
    const store = { [TOKEN_KEY]: 'cached-token', [EXPIRES_KEY]: Date.parse(FUTURE_ISO) }
    let requestCalls = 0
    const auth = freshAuth(makeWx({
      store,
      login: (opts) => opts.success({ code: 'code' }),
      request: (opts) => {
        requestCalls++
        if (opts.url.indexOf('/wx/login') >= 0) opts.success(loginSuccess(`t${requestCalls}`, FUTURE_ISO))
        else opts.success({ statusCode: 401, data: { succ: false, error_code: 'WX_TOKEN_INVALID', retdesc: '无效' } })
      }
    }))
    let threw = null
    try { await auth.request({ path: '/api/v1/wx/me' }) } catch (error) { threw = error }
    // 真实请求 + 一次登录 + 一次重试 = 3；再多就是循环了。
    // （这个 3 成立的**前提**是上面种了 token，让第 1 次 wx.request 就是真实请求；store 为空时是 4。）
    if (threw && threw.code === 'WX_TOKEN_INVALID' && requestCalls === 3) ok(`连续 401 在第 ${requestCalls} 次请求后放弃并上抛`)
    else fail(`应上抛 WX_TOKEN_INVALID 且总请求数为 3，实际 requestCalls=${requestCalls} threw=${threw && threw.code}`)
  }

  // ---------- 4. 并发 ensureLogin 只触发一次 wx.login ----------
  console.log('\n== 4. 并发只登录一次 ==')
  {
    const store = {}
    let loginCalls = 0
    const auth = freshAuth(makeWx({
      store,
      login: (opts) => { loginCalls++; setTimeout(() => opts.success({ code: 'code-1' }), 5) },
      request: (opts) => setTimeout(() => opts.success(loginSuccess('t-concurrent', FUTURE_ISO)), 5)
    }))
    const results = await Promise.all([auth.ensureLogin(), auth.ensureLogin(), auth.ensureLogin()])
    if (loginCalls === 1 && results.every((r) => r.token === 't-concurrent')) ok('三个并发 ensureLogin 只触发一次 wx.login')
    else fail(`期望 loginCalls=1，实际 ${loginCalls}`)
  }

  // ---------- 5. 402 分流：额度用尽与余额不足是两个码 ----------
  console.log('\n== 5. 402 分流 ==')
  {
    const store = { [TOKEN_KEY]: 'tok', [EXPIRES_KEY]: Date.parse(FUTURE_ISO) }
    // 服务端 retdesc 故意写成一句和客户端文案**不一样**的话，而且故意听着像余额问题。
    // 若照抄同一句，即使 errors.js 里根本没有 DAILY_QUOTA_EXCEEDED，toError 的
    // `|| payload.retdesc` 兜底也会吐出同一句话，下面两条断言照样全绿 —— 表在不在都测不出来。
    // 现在：只有真从表里取到文案，第一条才成立；只有表里那句自己不提余额，第二条才成立。
    const auth = freshAuth(makeWx({
      store,
      request: (opts) => opts.success(failBody('DAILY_QUOTA_EXCEEDED', '余额不足，请充值后重试'))
    }))
    const { ERROR_MESSAGES } = require(path.join(ROOT, 'utils', 'errors.js'))
    let threw = null
    try { await auth.request({ path: '/api/v1/parse' }) } catch (error) { threw = error }
    // 比的是**表里那句**，不是「像不像今日已用完」：这样断言才真的依赖 errors.js 被读到。
    if (threw && threw.code === 'DAILY_QUOTA_EXCEEDED' && threw.message === ERROR_MESSAGES.DAILY_QUOTA_EXCEEDED) {
      ok(`DAILY_QUOTA_EXCEEDED → 「${threw.message}」（取自 errors.js）`)
    } else {
      fail(`402 分流不对：code=${threw && threw.code} message=${threw && threw.message} 期望=${ERROR_MESSAGES.DAILY_QUOTA_EXCEEDED}`)
    }
    if (threw && threw.message.indexOf('积分') < 0 && threw.message.indexOf('余额') < 0) {
      ok('额度用尽的文案没有混入「余额不足」的说法')
    } else {
      fail(`额度用尽被说成了余额问题：「${threw && threw.message}」`)
    }
  }

  // ---------- 6. 头像上传带上令牌，并把 uploadFile 的字符串 body 解析掉 ----------
  console.log('\n== 6. 文件上传 ==')
  {
    const store = { [TOKEN_KEY]: 'tok', [EXPIRES_KEY]: Date.parse(FUTURE_ISO) }
    let uploadCalls = 0
    const auth = freshAuth(makeWx({
      store,
      // uploadFile 的 data 是字符串，不是解析好的对象 —— 这里必须照真实形状 stub
      uploadFile: (opts) => {
        uploadCalls++
        if (opts.header['X-WX-Token'] !== 'tok') {
          opts.success({ statusCode: 401, data: JSON.stringify({ succ: false, error_code: 'WX_TOKEN_INVALID' }) })
          return
        }
        opts.success({ statusCode: 200, data: JSON.stringify({ succ: true, retcode: 200, data: { user: { id: 42 } } }) })
      }
    }))
    const result = await auth.upload({
      path: '/api/v1/wx/profile', filePath: 'wxfile://tmp_abc.jpg', name: 'avatar'
    })
    if (result && result.user && result.user.id === 42 && uploadCalls === 1) {
      ok('上传带 X-WX-Token，且字符串 body 被正确解析')
    } else {
      fail(`上传路径不对：uploadCalls=${uploadCalls} result=${JSON.stringify(result)}`)
    }
  }

  console.log(failures === 0 ? '\n全部检查通过 ✔' : `\n${failures} 项未通过 ✘`)
  process.exit(failures === 0 ? 0 : 1)
}

main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:\ClaudeCodeProject\DeMark && node scripts/test-auth.js`
Expected: FAIL —— `Cannot find module '...miniprogram/utils/auth.js'`。

- [ ] **Step 3: 写实现**

先建 `miniprogram/utils/errors.js`：把 `ERROR_MESSAGES` 表**整段剪切**过去 —— 源区间是
`miniprogram/utils/parser.js:3-23`（表上方那三条 `//` 注释 + `const ERROR_MESSAGES = {` 到它那一行的 `}`，含表内
`// 以下为运营方故障` 那条），**一个字都不改**，末尾补一行 `module.exports = { ERROR_MESSAGES }`；`parser.js` 里这一段
随之删掉（改由 `require('./errors')` 取，见本步末尾）。搬完之后在这个新表里补上本任务新增的错误码
`DAILY_QUOTA_EXCEEDED: '今日解析额度已用完，明日自动恢复'`（放在 `RATE_LIMITED` 之后）。
**不要**在新文件里重抄文案 —— 直接剪切，避免两份文案日后各自漂移。

> 这张表为什么必须单独立模块：`auth.js` 需要它，而 Task 13 会让 `parser.js` require `auth.js`。表若留在
> `parser.js`，两个模块就互相 require，先被 require 完的一方拿到的是对方还没填完的 `module.exports`，解构出来的
> `ERROR_MESSAGES` / `request` 就是 `undefined` —— 而且只在特定加载顺序下才炸，两侧的单测都挡不住。

然后新建 `miniprogram/utils/auth.js`：

```js
const { API_BASE_URL, REQUEST_TIMEOUT, LOGIN_PATH } = require('../config')
// ERROR_MESSAGES 从 ./errors 取，**不要** require ./parser：Task 13 会让 parser.js require 本模块，
// 两边互 require 就是循环依赖 —— 先被加载完的一方拿到的是对方没填完的 module.exports，解构出来是
// undefined，而且只在特定加载顺序下才炸（两边各自的单测都挡不住）。
const { ERROR_MESSAGES } = require('./errors')

const TOKEN_KEY = 'jingji_wx_token'
const TOKEN_EXPIRES_KEY = 'jingji_wx_token_expires'
// 提前 5 分钟判过期：卡在过期瞬间发出的请求只会白跑一次 401
const EXPIRY_SKEW_MS = 5 * 60 * 1000
// 兜底有效期：服务端没给 expires_at 时按 1 天算（正常路径用不到）
const FALLBACK_TTL_MS = 24 * 60 * 60 * 1000
const TOKEN_INVALID_CODES = ['WX_TOKEN_INVALID', 'WX_TOKEN_EXPIRED']

// 模块内的 in-flight Promise：onShow 的刷新与用户点击解析完全可能同时触发，
// 天真实现会并发发起两次 wx.login，后一个 code 会顶掉前一个（设计文档 §3.1）
let inFlightLogin = null

function readToken() {
  const token = wx.getStorageSync(TOKEN_KEY)
  const expires = Number(wx.getStorageSync(TOKEN_EXPIRES_KEY)) || 0
  if (!token || !expires) return ''
  return expires - EXPIRY_SKEW_MS > Date.now() ? token : ''
}

function saveSession(token, expiresAt) {
  const parsed = Date.parse(expiresAt)
  wx.setStorageSync(TOKEN_KEY, token)
  wx.setStorageSync(TOKEN_EXPIRES_KEY, Number.isFinite(parsed) ? parsed : Date.now() + FALLBACK_TTL_MS)
}

function clearToken() {
  wx.removeStorageSync(TOKEN_KEY)
  wx.removeStorageSync(TOKEN_EXPIRES_KEY)
}

function toError(payload, fallback) {
  const code = (payload && payload.error_code) || ''
  const error = new Error(ERROR_MESSAGES[code] || (payload && payload.retdesc) || fallback)
  error.code = code
  error.retcode = payload && payload.retcode
  return error
}

// 网络层失败的用户提示。这两句是从 parser.js 现在的实现里原样搬过来的（Task 13 会把那段删掉）：
// 「request 合法域名没配」是小程序最常见的部署故障，旧实现在这里明确告诉用户去公众平台加域名，
// 搬过来之前若图省事统一成一句笼统的「网络连接失败」，就是同一次改造里凭空丢掉了这条诊断。
function networkError(error) {
  const host = API_BASE_URL.replace(/^https?:\/\//, '')
  return new Error(
    /timeout/i.test((error && error.errMsg) || '')
      ? '请求超时，请稍后重试'
      : `网络连接失败，请检查手机网络，以及微信公众平台是否已把 ${host} 加入 request 合法域名`
  )
}

function loginCode() {
  return new Promise((resolve, reject) => {
    wx.login({
      success: ({ code }) => (code ? resolve(code) : reject(new Error('微信登录失败，请重试'))),
      fail: () => reject(new Error('微信登录失败，请重试'))
    })
  })
}

function login() {
  return loginCode().then((code) => new Promise((resolve, reject) => {
    wx.request({
      url: `${API_BASE_URL}${LOGIN_PATH}`,
      method: 'POST',
      data: { code },
      timeout: REQUEST_TIMEOUT,
      header: { 'Content-Type': 'application/json' },
      success(response) {
        const payload = response.data
        if (payload && payload.succ === true && payload.data && payload.data.token) {
          saveSession(payload.data.token, payload.data.expires_at)
          resolve(payload.data)
          return
        }
        reject(toError(payload, '登录失败，请稍后重试'))
      },
      fail(error) {
        reject(networkError(error))
      }
    })
  }))
}

function ensureLogin() {
  const token = readToken()
  if (token) return Promise.resolve({ token })
  if (!inFlightLogin) {
    inFlightLogin = login().then(
      (result) => { inFlightLogin = null; return result },
      (error) => { inFlightLogin = null; throw error }
    )
  }
  return inFlightLogin
}

function send(path, options, session) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${API_BASE_URL}${path}`,
      method: options.method || 'GET',
      data: options.data,
      timeout: REQUEST_TIMEOUT,
      header: Object.assign(
        { Accept: 'application/json', 'X-WX-Token': session.token },
        options.header || {}
      ),
      success(response) {
        const payload = response.data
        if (payload && typeof payload === 'object' && payload.succ === true) {
          resolve(payload.data)
          return
        }
        reject(toError(payload, `请求失败（HTTP ${response.statusCode}）`))
      },
      fail(error) {
        reject(networkError(error))
      }
    })
  })
}

// 令牌失效时清掉本地令牌、静默重登并重试一次。
// 「只重试一次」是硬约束：否则服务端配置一错，客户端就无限重登，把 code2Session 额度打爆（§3.1）
function request(options) {
  return ensureLogin()
    .then((session) => send(options.path, options, session))
    .catch((error) => {
      if (options.retried || TOKEN_INVALID_CODES.indexOf(error.code) < 0) throw error
      clearToken()
      return ensureLogin()
        .then((session) => send(options.path, Object.assign({}, options, { retried: true }), session))
    })
}

// 文件上传走 wx.uploadFile：它是独立 API，跟 wx.request 不共用参数形状，
// 唯一需要在这里做的是把令牌带上、把成功/失败判成同一种错误（§1.7 的头像上传用它）
function uploadFile(path, options, session) {
  return new Promise((resolve, reject) => {
    wx.uploadFile({
      url: `${API_BASE_URL}${path}`,
      filePath: options.filePath,
      name: options.name,
      formData: options.formData || {},
      timeout: REQUEST_TIMEOUT,
      header: { 'X-WX-Token': session.token },
      success(response) {
        // 与 wx.request 不同，uploadFile 的 data 是**字符串**，要自己解析
        let payload = null
        try { payload = JSON.parse(response.data) } catch (error) { payload = null }
        if (payload && payload.succ === true) {
          resolve(payload.data)
          return
        }
        reject(toError(payload, `上传失败（HTTP ${response.statusCode}）`))
      },
      fail(error) {
        reject(networkError(error))
      }
    })
  })
}

// 与 request 同一条「只重试一次」规则：上传也不能无限重登
function upload(path, options) {
  const payload = Object.assign({}, options, { path })
  return ensureLogin()
    .then((session) => uploadFile(path, payload, session))
    .catch((error) => {
      if (payload.retried || TOKEN_INVALID_CODES.indexOf(error.code) < 0) throw error
      clearToken()
      return ensureLogin()
        .then((session) => uploadFile(path, Object.assign({}, payload, { retried: true }), session))
    })
}

module.exports = { clearToken, ensureLogin, readToken, request, upload }
```

`miniprogram/config.js` 补 `LOGIN_PATH`（`API_KEY` 与 `ENFORCE_QUOTA` 的删除放在 Task 14 / Task 18）：

```js
  API_PATH: '/api/v1/parse',
  LOGIN_PATH: '/api/v1/wx/login',
```

`miniprogram/utils/parser.js`：

(a) 顶部加一行 `require`（接在 `const { API_BASE_URL, API_PATH, API_KEY, REQUEST_TIMEOUT } = require('../config')` 那行之后）：

```js
const { ERROR_MESSAGES } = require('./errors')
```

(b) 文件末尾的导出块**补上一行 `ERROR_MESSAGES`**（块内按字典序，排在 `extractFirstUrl` 之前）。
本任务改造前的导出块只有 5 项、**并没有导出 `ERROR_MESSAGES`** —— 表搬去 `errors.js` 之后由
`parser.js` 原样转出，既有对外形态才不变。这一步是**新增一行导出**，不是「原样不动」：

```js
module.exports = {
  ERROR_MESSAGES,
  extractFirstUrl,
  normalizeResponse,
  normalizeResult,
  parseShareLink,
  typeLabelFor
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node scripts/test-auth.js`
Expected: 6 组 `[ok]`，末尾 `全部检查通过 ✔`，退出码 0。

> 注意 `parseShareLink` 此刻仍然走老路（`Authorization: ${API_KEY}`），Task 13 才切到 `auth.request`。本任务的用例全部直接测 `auth.request`，不经过 `parser.js`。

- [ ] **Step 5: Commit**

```bash
git add miniprogram/utils/errors.js miniprogram/utils/auth.js miniprogram/config.js miniprogram/utils/parser.js scripts/test-auth.js
git commit -m "feat(client): add wechat silent login and unified request layer"
```

---

## Task 13: 解析请求改走令牌路径

**Files:**
- Modify: `miniprogram/utils/parser.js:1`（import）、`miniprogram/utils/parser.js:127-165`（`parseShareLink`）
- Modify: `scripts/test-parser.js`（补一条断言，确认解析请求带的是令牌而不是密钥）

**Interfaces:**
- Consumes: `auth.request({path, method, data})`（Task 12）
- Produces: `parseShareLink(sourceUrl)` 行为不变（同样的 `normalizeResponse` 结果与错误对象），只是传输层换成令牌

- [ ] **Step 1: 写失败测试**

打开 `scripts/test-parser.js`，把下面两段追加在第 5 节那个 `} else { … }` 块的**后面**、
`function finish()` 声明**之前**（该文件已有 `fail` / `ok` 两个辅助函数；现在是 207 行，`function finish()`
在 202 行）。

> **别追加到文件最末尾**：末行是 `if (!SHARE) finish()`，而 `finish()` 会 `process.exit()`。不加参数跑时
> 进程在那一行就退出了，加在它后面的整段代码**一行都不会执行** —— `node scripts/test-parser.js` 照样打印
> 「全部检查通过 ✔」、退出码 0，而四条新断言一条都没跑过：看着全绿，其实从没存在过（P19/P25/P27/P29 那一类
> 「永远不会失败的断言」）。而且 Step 2 期望的「FAIL 四条」也永远等不到 —— 死代码只会给出一片绿。
> 这一点是**实测**的：把一段打印标记的代码追加到文件真正的末尾、不加参数运行，标记没有出现在输出里，退出码 0。

```js
// ---------- 6. 请求层走 X-WX-Token，不再使用包内 API_KEY ----------
console.log('\n== 6. 请求层 = 令牌路径 ==')
const parserSource = fs.readFileSync(path.join(__dirname, '..', 'miniprogram', 'utils', 'parser.js'), 'utf8')
// 本节是**文本扫描**：扫源码，不是跑行为。所以每条断言都得挑「源码里真的会被执行的东西」：
//   别拿「源码里出现 X-WX-Token」当「走了令牌通道」的证据 —— 令牌头是 auth.js 加的，parser.js 里只有
//   Step 3 那段注释会出现这个串；注释留着、旧请求路径也留着，照样能过（P29）。
//   承重的是下面三条「不该再有」：旧通道的三个特征串只要还在，就说明请求没真交给 auth.js。
if (parserSource.indexOf("require('./auth')") >= 0) ok('parseShareLink 已把传输层交给 auth.js')
else fail('parser.js 没有 require ./auth —— 请求仍在自己发')
if (parserSource.indexOf('wx.request') < 0) ok('parser.js 里已经没有自己发起的 wx.request')
else fail('parser.js 还在自己调 wx.request（应交给 auth.request）')
if (parserSource.indexOf('Authorization') < 0) ok('parser.js 里已经没有 Authorization 头')
else fail('parser.js 仍在用旧的 Authorization: Bearer 通道')
if (parserSource.indexOf('API_KEY') < 0) ok('parser.js 里已经没有 API_KEY 的引用')
else fail('parser.js 仍在引用 API_KEY')

// ---------- 7. 模块依赖不成环 ----------
console.log('\n== 7. 模块依赖不成环 ==')
const authSource = fs.readFileSync(path.join(__dirname, '..', 'miniprogram', 'utils', 'auth.js'), 'utf8')
// parser.js 从本任务起 require auth.js；auth.js 若反过来 require parser.js，两边就互 require：先被
// require 完的一方拿到的是对方还没填完的 module.exports，解构出来的 ERROR_MESSAGES / request 是
// undefined，而且只在特定加载顺序下才炸 —— 两个测试脚本都挡不住（P2）。
// 用带引号的正则，别写成「裸字符串 indexOf」：auth.js 的注释里就有「不要 require ./parser」这句提醒，
// 裸字符串会把那句注释也当成命中，这条断言就永远是红的（或反过来永远绿）。
if (!/require\(['"]\.\/parser['"]\)/.test(authSource)) ok('auth.js 没有反向 require parser.js（不成环）')
else fail('auth.js 又 require 回 parser.js 了 —— 会和 parser.js 的 require("./auth") 构成循环依赖')
```

同时在 `scripts/test-parser.js` 顶部确认 `fs` 与 `path` 都已引入：`path` 已经在第 7 行，
`fs` **没有**，加上 `const fs = require('fs')`（第 6、7 两节用的是 `fs.readFileSync` 与 `path.join`）。

最后，第 5 节那个垫片**必须**补上存储与登录两个 API —— 本任务之后 `parseShareLink` 走 `auth.request`，
而它第一步就是 `wx.getStorageSync` 读缓存令牌。把那段 `global.wx = { … }` 换成：

```js
  global.wx = {
    // Task 13 起 parseShareLink 走 auth.js：它先读缓存令牌、必要时 wx.login，所以垫片也得提供这两个 API。
    // 只留一个 request 垫片的话，auth.request 会**同步**抛 TypeError: wx.getStorageSync is not a function
    // ——不是 reject，是 throw，于是带链接跑直接崩在栈上，而不是给出一句能看懂的话。
    getStorageSync: (key) => {
      if (key === 'jingji_wx_token') return process.env.WX_TOKEN || ''
      if (key === 'jingji_wx_token_expires') return Date.now() + 24 * 3600 * 1000
      return ''
    },
    setStorageSync: () => {},
    removeStorageSync: () => {},
    // 本脚本拿不到微信登录 code（那要小程序宿主），所以没令牌时把话说清楚，而不是崩栈
    login: (opts) => opts.fail({ errMsg: 'login:fail 本脚本无法取得微信登录 code，请用 WX_TOKEN 传入令牌' }),
    request(options) {
      const qs = new URLSearchParams(options.data).toString()
      fetch(`${options.url}?${qs}`, { method: options.method, headers: options.header })
        .then(async (res) => {
          const text = await res.text()
          let data
          try { data = JSON.parse(text) } catch (e) { data = text }
          options.success({ statusCode: res.status, data })
        })
        .catch((err) => options.fail({ errMsg: 'request:fail ' + err.message }))
    }
  }
```

令牌取自开发者工具的 Storage 面板（键名 `jingji_wx_token`），从环境变量传进去：

```bash
WX_TOKEN=<令牌> node scripts/test-parser.js <分享链接>
```

（PowerShell 下写 `$env:WX_TOKEN='<令牌>'; node scripts/test-parser.js <分享链接>`。）

文件头「用法」那几行同时改掉 —— 原来那句「带链接 → 额外走一次真实接口，端到端验证」在本任务之后
需要令牌才成立，不改就是一句空话：

```js
// 用法：node scripts/test-parser.js [分享链接]
//   不带参数 → 只跑离线样本（用实测返回的固定 JSON，不消耗接口积分）
//   带链接   → 额外走一次真实接口，端到端验证。2026-09-22 起请求走 X-WX-Token 通道，需要令牌
//              （开发者工具 Storage 面板里的 jingji_wx_token）：
//              WX_TOKEN=<令牌> node scripts/test-parser.js <分享链接>
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node scripts/test-parser.js`
Expected: FAIL 四条 —— 第 6 节里 `require('./auth')` 还没有、而 `wx.request` / `Authorization` / `API_KEY`
三个旧通道特征串都还在。第 7 节两条此时应当是 `[ok]`：它是模块依赖的守门断言，不是本任务要实现的行为，一上来就是绿的，
别把它当成「不会失败的测试」。

- [ ] **Step 3: 写实现**

`miniprogram/utils/parser.js` 第 1 行的 import 改成：

```js
const { API_BASE_URL, API_PATH } = require('../config')
const { request } = require('./auth')
```

> `API_BASE_URL` 在 `normalizeResult` 之后没有再被用到 —— 检查一遍，若只剩 `parseShareLink` 用过它，改完后把 `API_BASE_URL` 从解构里去掉，避免留下一个未使用的变量。

`parseShareLink` 整个函数体换成：

```js
function parseShareLink(sourceUrl) {
  // 传输层交给 auth.js：自动带 X-WX-Token、401 静默重登并只重试一次（设计文档 §3.1）
  return request({ path: API_PATH, method: 'GET', data: { url: sourceUrl } })
    .then((data) => normalizeResult(data, sourceUrl))
}
```

`normalizeResponse` 保留（离线样本测试仍要用它解析固定 JSON），`module.exports` 不动。

- [ ] **Step 4: 跑测试确认通过**

Run: `node scripts/test-parser.js`
Expected: 离线样本全 `[ok]`，第 6 节四条 + 第 7 节两条也都是 `[ok]`。**不带参数**运行，不消耗接口额度。

- [ ] **Step 5: Commit**

```bash
git add miniprogram/utils/parser.js scripts/test-parser.js
git commit -m "feat(client): route parse requests through the wx token path"
```

---

## Task 14: 额度与会员状态改为服务端快照

**Files:**
- Modify: `miniprogram/config.js`（删 `ENFORCE_QUOTA`，两处常量降级为兜底）
- Rewrite: `miniprogram/utils/quota.js`
- Delete: `miniprogram/utils/member.js`（**DeMark 不是 git 仓库**：用 `Remove-Item miniprogram\utils\member.js`
  删文件即可，没有 `git rm`。本计划里 DeMark 侧的 `git commit` 步骤一律跳过，并在报告里写「未提交」）
- Modify: `scripts/verify-theme-c.js`（额度断言先红后改）

**Interfaces:**
- Consumes: `auth.request({path: '/api/v1/wx/me'})`（Task 12）
- Produces:
  - `getQuota() -> snapshot` —— **同步**读缓存，形状：
    `{ready, member, memberPlanName, memberDaysLeft, dailyQuota, usedToday, remainingToday, balance, nickname, avatar, userId}`
  - `fetchQuota() -> Promise<snapshot>` —— 异步取服务端快照，写回缓存后返回
  - `SNAPSHOT_KEY = 'jingji_quota_snapshot'`
  - `clearLegacyStorage() -> void` —— 清掉 `LEGACY_KEYS` 那三个 2026-09 之前的本地键。
    本文件的代码块里定义了并导出了它，**Task 15 和 Task 16 都要 require 它**（Task 15 两处调用、
    Task 16 的 Interfaces 行也点了名）—— 所以它是本任务的对外接口，不是顺手多写的死代码，别当死代码删掉。
  - `consumeQuota()` / `addBalance()` / `BALANCE_KEY` / `utils/member.js` **全部删除**

- [ ] **Step 1: 写失败测试（让 verify-theme-c 先红）**

Run: `cd D:\ClaudeCodeProject\DeMark && node scripts/verify-theme-c.js`
Expected: 现在应当仍是全绿（基线）。**先记下这一次的输出**，改完之后要能说清哪几条是刻意改红的。

- [ ] **Step 2: 改 `config.js`**

```js
  VERSION: '0.1.0',
  // 免费用户每日可用次数，以及签到赠送的账户余额次数。
  // 2026-09-22：额度判定已移到服务端（docs/wx-login.md §1.6），这两个值只剩一个用途 ——
  // 服务端快照还没到手时，先按它们渲染首帧，不让页面空着（§3.2）。数值改后台设置即可，
  // 这里改了只影响首帧那几百毫秒。
  FREE_DAILY_QUOTA: 10,
  SIGN_IN_BONUS: 10
```

`ENFORCE_QUOTA` 整条删除（额度拦截已由服务端 `402 DAILY_QUOTA_EXCEEDED` 决定）。

- [ ] **Step 3: 重写 `miniprogram/utils/quota.js`**

整个文件替换为：

```js
const { FREE_DAILY_QUOTA } = require('../config')
const { request } = require('./auth')

const SNAPSHOT_KEY = 'jingji_quota_snapshot'
// 2026-09 之前的本地额度实现留下的键。会员状态曾经完全存在本地，
// 留着不但没用，还会在排查问题时误导人 —— 顺手清掉
const LEGACY_KEYS = ['jingji_quota_balance', 'jingji_member', 'jingji_checkin']

function fallbackSnapshot() {
  return {
    ready: false,
    userId: 0,
    nickname: '',
    avatar: '',
    member: false,
    memberPlanName: '',
    memberDaysLeft: 0,
    dailyQuota: FREE_DAILY_QUOTA,
    usedToday: 0,
    remainingToday: FREE_DAILY_QUOTA,
    balance: 0
  }
}

function normalize(serverUser) {
  const user = serverUser || {}
  return {
    ready: true,
    userId: Number(user.id) || 0,
    nickname: user.nickname || '',
    avatar: user.avatar || '',
    member: user.member === true,
    memberPlanName: user.member_plan_name || '',
    memberDaysLeft: Number(user.member_days_left) || 0,
    dailyQuota: Number(user.daily_quota) || 0,
    usedToday: Number(user.used_today) || 0,
    remainingToday: Number(user.remaining_today) || 0,
    balance: Number(user.balance) || 0
  }
}

// 同步读缓存：供页面首帧直接渲染，不让页面为了等网络而空着（§3.2）
function getQuota() {
  const cached = wx.getStorageSync(SNAPSHOT_KEY)
  if (cached && typeof cached === 'object' && cached.ready) return Object.assign(fallbackSnapshot(), cached)
  return fallbackSnapshot()
}

// 异步取服务端快照并写回缓存。失败时保留上一次的缓存值 —— 展示层不该因为一次网络抖动而归零
function fetchQuota() {
  return request({ path: '/api/v1/wx/me' }).then((data) => {
    const snapshot = normalize(data && data.user)
    wx.setStorageSync(SNAPSHOT_KEY, snapshot)
    return snapshot
  })
}

function clearLegacyStorage() {
  LEGACY_KEYS.forEach((key) => wx.removeStorageSync(key))
}

module.exports = { SNAPSHOT_KEY, clearLegacyStorage, fetchQuota, getQuota }
```

- [ ] **Step 4: 删除 `miniprogram/utils/member.js`**

```bash
rm miniprogram/utils/member.js
```

（Windows 下用 `Remove-Item miniprogram\utils\member.js`。）**先确认没有别的文件还在 require 它**：

```bash
grep -rn "utils/member" miniprogram scripts
```
除 `pages/member/member.js` 与 `pages/my/my.js`（Task 15 / 16 会改）之外不该有别的引用。

- [ ] **Step 5: 改 `scripts/verify-theme-c.js` 的额度断言**

第 12 节（`== 12. 剩余次数提示分支 ==`）整段替换为：

```js
// ---------- 12. 剩余次数提示分支（跑真实的 refreshQuota，而非静态字符串） ----------
//
// 2026-09-22 变更理由：额度判定整体移到了服务端（docs/wx-login.md §1.6）。
// 本地不再有「账户余额」这一档，「不限量」这个概念在新模型里也不存在 ——
// 每日额度模型下人人都有上限。所以这一节从「本地算 available」改成「渲染服务端快照」，
// 断言值随之改写，而不是把旧断言删掉留个空洞。
console.log('\n== 12. 剩余次数提示分支 ==')
const store = {}
global.wx = {
  getStorageSync: (k) => (k in store ? store[k] : ''),
  setStorageSync: (k, v) => { store[k] = v },
  removeStorageSync: (k) => { delete store[k] },
  request: () => { throw new Error('本节的断言不该发起请求') }
}
let indexPage = null
global.Page = (obj) => { indexPage = obj }
require(path.join(ROOT, 'pages', 'index', 'index.js'))
const quotaMod = require(path.join(ROOT, 'utils', 'quota.js'))
const DAILY = require(path.join(ROOT, 'config.js')).FREE_DAILY_QUOTA
const clearStore = () => Object.keys(store).forEach((k) => delete store[k])
const readHint = () => {
  const fake = { data: {}, setData(obj) { Object.assign(this.data, obj) } }
  indexPage.refreshQuota.call(fake)
  return fake.data
}
const seed = (patch) => {
  quotaMod.__seedForTest
    ? quotaMod.__seedForTest(patch)
    : wx.setStorageSync(quotaMod.SNAPSHOT_KEY, patch)
}
const expectHint = (setup, wantHint, wantLow, label) => {
  clearStore()
  if (setup) setup()
  const d = readHint()
  if (d.quotaHint === wantHint && d.quotaLow === wantLow) ok(`${label} → 「${d.quotaHint}」`)
  else fail(`${label}：得到「${d.quotaHint}」(low=${d.quotaLow})，期望「${wantHint}」(low=${wantLow})`)
}
expectHint(null, `今日还可解析 ${DAILY} 次`, false, '无缓存（首帧兜底）')
expectHint(() => seed({ ready: true, member: false, dailyQuota: 10, usedToday: 0, remainingToday: 10 }),
  '今日还可解析 10 次', false, '普通用户满额')
expectHint(() => seed({ ready: true, member: false, dailyQuota: 10, usedToday: 8, remainingToday: 2 }),
  '今日还可解析 2 次', true, '剩 2 次（转预警色）')
expectHint(() => seed({ ready: true, member: false, dailyQuota: 10, usedToday: 10, remainingToday: 0 }),
  '今日额度已用完，明日恢复', false, '普通用户额度用尽')
// 这条是本次改写的重点：旧实现里余额还能继续解析，所以提示要说「可用余额 N 次」。
// 新模型下余额不参与额度判定（§2.2），因此提示必须与余额无关
expectHint(() => seed({ ready: true, member: false, dailyQuota: 10, usedToday: 10, remainingToday: 0, balance: 5 }),
  '今日额度已用完，明日恢复', false, '额度用尽但有余额（余额不再参与判定）')
expectHint(() => seed({ ready: true, member: true, memberPlanName: '年卡', dailyQuota: 500, usedToday: 2, remainingToday: 498 }),
  '会员每日 500 次 · 今日还剩 498 次', false, '会员')
```

随后第 10 节里那两条断言也要跟着改（它们在文件更靠前的位置）：

第 360 行附近：

```js
// 展示值必须与服务端快照同源（改造前是与首页拦截同源）—— docs/wx-login.md §3.4
if (myJs.includes('todayQuota: quota.remainingToday') && myJs.includes('dailyQuota: quota.dailyQuota')) ok('我的页数值取自 quota.js（与服务端快照同源）')
else fail('我的页数值未与 quota.js 绑定，可能与首页显示不一致')
```

第 373 行附近那条 `quota.available` 的断言替换为：

```js
// 2026-09-22 变更理由：本地额度拦截整体删除，改成由服务端 402 DAILY_QUOTA_EXCEEDED 决定（§3.3）。
// 因此这里反过来断言 available 这个合成值**不再存在**，并确认 402 分支被接住
if (!idxJs.includes('quota.available')) ok('首页不再有本地的 available 合成值（额度判定在服务端）')
else fail('首页仍在本地合成可用次数 —— 本地能算的额度，本地就能改')
if (idxJs.includes('DAILY_QUOTA_EXCEEDED')) ok('首页接住了 402 DAILY_QUOTA_EXCEEDED')
else fail('首页没有处理 DAILY_QUOTA_EXCEEDED，额度用尽时会只剩一句通用报错')
```

第 354 行附近那条 `今日剩余次数` 的模板断言保留，但把 `quotaUnlimited` 相关判断改掉（见 Task 15 的模板改写）。

- [ ] **Step 6: 跑测试（此时应当是红的）**

Run: `node scripts/verify-theme-c.js`
Expected: FAIL —— 第 10、12 节的多条断言红。**这是刻意的**：`pages/index/index.js` 与 `pages/my/my.js` 还没改。把红的条目记下来，Task 15 / 16 完成后必须全部转绿。

- [ ] **Step 7: Commit**

```bash
git add miniprogram/config.js miniprogram/utils/quota.js scripts/verify-theme-c.js
git rm miniprogram/utils/member.js
git commit -m "refactor(client): quota becomes a server snapshot cache

设计文档 §3.2：额度判定与会员状态全部移到服务端，本地只保留
「读缓存渲染首帧 + 用服务端结果覆盖」的展示职责。"
```

---

## Task 15: 首页改为异步额度 + 402 引导

**Files:**
- Modify: `miniprogram/pages/index/index.js:60-140`
- Modify: `miniprogram/pages/index/index.wxml`（额度提示区，`quotaUnlimited` 相关节点）
- Modify: `scripts/verify-theme-c.js`（第 10 节 :368 那条重算断言随 `syncQuota()` 改写）
- Test: `scripts/verify-theme-c.js`（第 10、12 节的断言转绿）

**Interfaces:**
- Consumes: `getQuota()` / `fetchQuota()`（Task 14）、`DAILY_QUOTA_EXCEEDED` 文案（Task 12）
- Produces: `refreshQuota()` 仍是同步渲染（供 `verify-theme-c.js` 直接调用），另加 `syncQuota()` 异步刷新

- [ ] **Step 1: 写失败测试**

Run: `node scripts/verify-theme-c.js`
Expected: 第 10、12 节红（Task 14 留下的红）。

- [ ] **Step 2: 改 `pages/index/index.js`**

顶部 import 改成：

```js
const { extractFirstUrl, parseShareLink, typeLabelFor } = require('../../utils/parser')
const { recordParse } = require('../../utils/stats')
const { clearLegacyStorage, fetchQuota, getQuota } = require('../../utils/quota')
const { AUTO_PLATFORM, PLATFORMS, findPlatformByName } = require('../../utils/platforms')
const { MAX_HISTORY_ITEMS } = require('../../config')
```

`onShow` 改成：

```js
  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 0 })
    clearLegacyStorage()
    this.refreshQuota()          // 先用缓存渲染一帧，页面不为等网络而空着
    this.syncQuota()             // 再取服务端快照覆盖
    const pendingUrl = wx.getStorageSync('jingji_pending_url')
    if (pendingUrl) {
      wx.removeStorageSync('jingji_pending_url')
      this.setData({ shareText: pendingUrl, inputLength: pendingUrl.length })
      this.startParse()
    }
  },

  // 异步刷新：失败时静默保留缓存值 —— 展示层不该因为一次网络抖动而归零（§3.2）
  syncQuota() {
    return fetchQuota().then(() => this.refreshQuota()).catch(() => {})
  },
```

`refreshQuota()` 改成（**模板里用到的字段都在这里算好，模板不做三元嵌套**）：

```js
  // 剩余次数提示：数值全部来自服务端快照，本地只做显示格式化（§3.4）
  refreshQuota() {
    const quota = getQuota()
    let hint = `今日还可解析 ${quota.remainingToday} 次`
    if (quota.member) hint = `会员每日 ${quota.dailyQuota} 次 · 今日还剩 ${quota.remainingToday} 次`
    else if (quota.remainingToday <= 0) hint = '今日额度已用完，明日恢复'
    this.setData({
      quotaHint: hint,
      quotaLow: quota.remainingToday > 0 && quota.remainingToday <= 2,
      quotaReady: quota.ready
    })
  },
```

`startParse()` 的额度拦截段（原 `if (ENFORCE_QUOTA) { … }` 整块）**删掉**，换成由服务端 402 决定。新的 `startParse`：

```js
  async startParse() {
    if (this.data.loading) return
    const sourceUrl = extractFirstUrl(this.data.shareText)
    if (!sourceUrl) {
      this.setData({ errorMessage: '请粘贴包含 http 或 https 的有效分享链接' })
      return
    }

    // 本地不再判额度（§3.3）：能不能解析由服务端的每日额度决定，
    // 服务端说不行会返回 402 DAILY_QUOTA_EXCEEDED
    this.setData({ loading: true, errorMessage: '' })

    try {
      // 解析结果不落在首页任何节点上（展示与操作全在结果页），拿到手只为了写历史、跳页
      const result = await parseShareLink(sourceUrl)
      const historyId = this.addHistory(result)
      recordParse()
      wx.showToast({ title: '解析成功', icon: 'success' })
      wx.navigateTo({ url: `/pages/result/result?id=${historyId}` })
    } catch (error) {
      if (error.code === 'DAILY_QUOTA_EXCEEDED') this.showQuotaGuide(error.message)
      else this.setData({ errorMessage: error.message || '解析失败，请稍后重试' })
    } finally {
      this.setData({ loading: false })
      // 解析成功已消耗额度，失败则数字不变，两种情况都重算一次最省心
      this.syncQuota()
    }
  },

  // 额度用尽的引导：签到与开卡是两条不同的路，文案不能和「余额不足」混（§2.3）
  showQuotaGuide(message) {
    wx.showModal({
      title: '今日额度已用完',
      content: `${message || '今日额度已用完，明日恢复'}。可先签到领取次数，或开通会员提升每日额度。`,
      confirmText: '去签到',
      cancelText: '知道了',
      success: (confirmResult) => {
        if (confirmResult.confirm) wx.switchTab({ url: '/pages/my/my' })
      }
    })
  },
```

`data` 里把 `quotaLow` 保留、加 `quotaReady: false`。

- [ ] **Step 3: 改 `pages/index/index.wxml`**

找到展示 `quotaHint` 的那一处，确认它没有依赖 `quotaUnlimited`（首页 WXML 只用 `quotaHint` 与 `quotaLow`，按 `verify-theme-c.js` 的断言如此）。若有任何 `不限` 字样的分支，删掉 —— 新模型没有不限量。

- [ ] **Step 4: 改 `scripts/verify-theme-c.js` 第 368 行附近那条重算断言**

那条断言原本要求 `finally { … refreshQuota() }`。本任务起解析结束后的重算改走 `syncQuota()`
（先取服务端快照再渲染），照原样匹配就永远是红的 —— 而它的**本意**（「解析完数字要立刻跟着变，
不能滞后一次」）在新实现里依然成立，所以要改写而不是删掉：

```js
// 2026-09-22 变更理由：解析结束后的重算改走 syncQuota()（先取服务端快照再渲染，§3.2）。
// 只把匹配放宽成 (?:sync|refresh)Quota() 会留下一个空洞 —— syncQuota() 完全可以只取快照、
// 不重渲染，数字照样滞后一次。所以下面补一条断言，把 syncQuota() 自己的落点钉死。
if (/finally\s*\{[\s\S]{0,200}?(?:sync|refresh)Quota\(\)/.test(idxJs)) ok('首页解析结束后重算（成功后数字立即递减，不滞后）')
else fail('首页解析结束后未重算剩余次数，数字会滞后一次')
// 要求的是 this.refreshQuota() 这个**调用**写法：方法定义那行是 `refreshQuota() {`，不带 `this.`，
// 所以匹配不上 —— 否则一条「只取快照不渲染」的 syncQuota() 会被紧随其后的方法定义蒙混过关。
if (/syncQuota\s*\(\s*\)\s*\{[\s\S]{0,200}?this\.refreshQuota\s*\(\s*\)/.test(idxJs)) ok('syncQuota() 取回快照后确实重渲染')
else fail('syncQuota() 没有落到 this.refreshQuota()，拿到服务端快照也不会更新界面')
```

- [ ] **Step 5: 跑测试确认通过**

Run: `node scripts/verify-theme-c.js`
Expected: 第 10、12 节转绿；第 10 节里只剩 `my.js` 那条数值断言（第 360 行附近）仍红（Task 16 修）。

- [ ] **Step 6: 手工验证一次首页**

微信开发者工具打开项目（`compileHotReLoad` 已关，改完记得手动编译一次），在首页看额度提示是否为「今日还可解析 N 次」，下拉或切页回来数字是否刷新。

- [ ] **Step 7: Commit**

```bash
git add miniprogram/pages/index/index.js miniprogram/pages/index/index.wxml scripts/verify-theme-c.js
git commit -m "feat(index): render quota from the server snapshot, guide on 402"
```

---

## Task 16: 我的页接服务端快照、服务端签到、完善资料、会员区

**Files:**
- Modify: `miniprogram/pages/my/my.js`
- Modify: `miniprogram/pages/my/my.wxml`
- Modify: `miniprogram/pages/my/my.wxss`（**补 3 个类名规则**：`profile-avatar-btn` / `profile-avatar` /
  `profile-name-input` —— 见 Step 3(d) 与 Step 4。2026-09-22 实测：这三条规则在 `my.wxss` 与 `app.wxss` 里
  都不存在，而第 3 节的静态类名扫描会逐个报红，本任务不改 wxss 就结束不了；
  `profile-name-placeholder` 虽然只从 `placeholder-class=` 出现，但第 3 节的扫描照样看得到它 ——
  那是正则 `/class="([^"]*)"/g`，没有词边界，`placeholder-class="x"` 里的 `class="x"` 一样命中
  （2026-09-22 修正 P38 的旧说法，别再以为这种写法是隐形的），所以同样要补规则）
- Test: `scripts/verify-theme-c.js`（第 10 节剩余断言转绿）

**Interfaces:**
- Consumes: `getQuota()` / `fetchQuota()` / `clearLegacyStorage()`（Task 14）、`auth.request` / `auth.upload`（Task 12）
- Produces:
  - `refresh()` 同步渲染缓存（供 `verify-theme-c.js` 断言）
  - `sync()` 异步取 `/api/v1/wx/me` 后重渲染
  - `signIn()` 调 `POST /api/v1/wx/checkin`
  - `chooseAvatar(event)` → `upload()` 把**字节**传到 `POST /api/v1/wx/profile`
  - `onNicknameChange(event)` → `request()` 把昵称存到 `POST /api/v1/wx/profile`
  - `openMember()` → `navigateTo` 会员页（`pages/member/member` 不在 tabBar 里）
  - `ensureLocalId()` 与 `LOCAL_ID_KEY` **删除**（用户号显示服务端 `id`）

- [ ] **Step 1: 写失败测试**

Run: `node scripts/verify-theme-c.js`
Expected: 第 10 节的两条 `my.js` 断言仍红。

- [ ] **Step 2: 重写 `pages/my/my.js`**

```js
const { getStats } = require('../../utils/stats')
const { clearLegacyStorage, fetchQuota, getQuota } = require('../../utils/quota')
// request 走 JSON（签到、昵称），upload 走 multipart（头像字节）
const { request, upload } = require('../../utils/auth')
const { API_BASE_URL, SIGN_IN_BONUS, VERSION } = require('../../config')

// 本地只记「今天签过没有」用于展示（§3.2）——服务端快照里没有签到字段（实测 `_user_snapshot`
// 只返回额度/会员/头像/昵称），这里没有别的来源可推。
// 键名**不能**叫 'jingji_checkin'：`utils/quota.js` 的 LEGACY_KEYS 里有同名键，
// clearLegacyStorage() 每次进页面都会先把它删掉，refresh() 再读就永远是空的
// （2026-09-22 实测：签过到的人每次进页面都显示「签到」）。
const CHECKIN_KEY = 'jingji_checkin_mark'

Page({
  data: {
    version: VERSION,
    userId: '',
    nickname: '硬核用户',
    avatar: '',
    levelLabel: '普通用户',
    totalCount: 0,
    todayCount: 0,
    weekCount: 0,
    // 额度相关：数值全部来自服务端快照（§3.4），本地只格式化
    todayQuota: 0,
    dailyQuota: 0,
    balanceCount: 0,
    checkedIn: false,
    streak: 0,
    levelActive: false,
    memberPlanName: '',
    memberSub: '',
    memberAction: '去开通',
    // 仅用于服务端不可达时的显示兜底；真实奖励数以 /wx/checkin 返回的 bonus 为准
    signInBonus: SIGN_IN_BONUS
  },

  onShow() {
    if (typeof this.getTabBar === 'function' && this.getTabBar()) this.getTabBar().setData({ selected: 2 })
    clearLegacyStorage()
    this.refresh()      // 先用缓存渲染一帧
    this.sync()         // 再取服务端快照覆盖
  },

  sync() {
    return fetchQuota().then(() => this.refresh()).catch(() => {})
  },

  refresh() {
    const stats = getStats()
    const checkin = wx.getStorageSync(CHECKIN_KEY) || {}
    const quota = getQuota()
    const today = new Date()
    const checkedIn = checkin.lastDate === this.dayKey(today)
    this.setData({
      userId: quota.userId || '',
      nickname: quota.nickname || '硬核用户',
      // 服务端给的是相对路径（§2.2），<image src> 需要绝对地址
      avatar: quota.avatar ? API_BASE_URL + quota.avatar : '',
      totalCount: stats.total,
      todayCount: quota.usedToday,
      weekCount: stats.week,
      todayQuota: quota.remainingToday,
      dailyQuota: quota.dailyQuota,
      balanceCount: quota.balance,
      checkedIn,
      streak: checkedIn ? Number(checkin.streak) || 1 : 0,
      levelLabel: quota.member ? '尊享会员' : '普通用户',
      levelActive: quota.member,
      memberPlanName: quota.memberPlanName,
      memberSub: quota.member
        ? `${quota.memberPlanName} · 剩余 ${quota.memberDaysLeft} 天 · 每日 ${quota.dailyQuota} 次`
        : '开通会员，提升每日解析额度',
      memberAction: quota.member ? '会员中心' : '去开通'
    })
  },

  dayKey(date) {
    const pad = (value) => String(value).padStart(2, '0')
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  },

  // 签到完全归服务端：本地只记「今天签过没有」用于展示（§3.2）
  signIn() {
    if (this.data.checkedIn) {
      wx.showToast({ title: '今天已经签到过了', icon: 'none' })
      return
    }
    request({ path: '/api/v1/wx/checkin', method: 'POST' })
      .then((data) => {
        wx.setStorageSync(CHECKIN_KEY, { lastDate: this.dayKey(new Date()), streak: data.streak })
        this.refresh()
        this.sync()
        wx.showToast({ title: `签到成功，余额 +${data.bonus}`, icon: 'none' })
      })
      .catch((error) => {
        if (error.code === 'ALREADY_CHECKED_IN') {
          wx.setStorageSync(CHECKIN_KEY, { lastDate: this.dayKey(new Date()), streak: this.data.streak })
          this.refresh()
          wx.showToast({ title: '今天已经签到过了', icon: 'none' })
          return
        }
        wx.showToast({ title: error.message || '签到失败，请稍后重试', icon: 'none' })
      })
  },

  // 头像：chooseAvatar 交给客户端的是 wxfile:// 本机临时路径，服务端下载不了它，
  // 所以由客户端把字节直接传上去（§1.7）
  chooseAvatar(event) {
    const tempPath = event.detail.avatarUrl
    if (!tempPath) return
    upload({ path: '/api/v1/wx/profile', filePath: tempPath, name: 'avatar' })
      .then((data) => {
        const user = (data && data.user) || {}
        if (user.avatar) this.setData({ avatar: API_BASE_URL + user.avatar })
        wx.showToast({ title: '头像已更新', icon: 'none' })
      })
      .catch((error) => wx.showToast({ title: error.message || '头像上传失败，请重试', icon: 'none' }))
  },

  // 昵称用 <input type="nickname">，微信把用户选定的昵称放在 event.detail.value
  onNicknameChange(event) {
    const nickname = (event.detail.value || '').trim()
    if (!nickname || nickname === this.data.nickname) return
    request({ path: '/api/v1/wx/profile', method: 'POST', data: { nickname } })
      .then((data) => {
        const user = (data && data.user) || {}
        this.setData({ nickname: user.nickname || nickname })
      })
      .catch(() => wx.showToast({ title: '昵称保存失败，请重试', icon: 'none' }))
  },

  openMember() {
    wx.navigateTo({ url: '/pages/member/member' })
  },

  // ↓ 原文件里的这 8 个方法一行都不用改，接在 openMember 后面即可：
  //   showPlatforms / showHelp / openHistory / showFeedback
  //   openPermissions / showPrivacy / showAgreement / onShareAppMessage
})
```

> 头像的完整链路是「客户端 `wx.uploadFile` 上传字节 → 服务端 `_store_avatar` 落盘 → `/wx/avatar/<id>` 提供」，两头分别由 Task 12 的 `auth.upload` 与 Task 7 实现。**服务端返回的 `avatar` 是相对路径**（§2.2），所以这里必须拼上 `API_BASE_URL` 才能给 `<image src>` 用 —— 漏了这一步，真机上头像永远显示不出来，而开发者工具里可能照常显示（工具对相对路径更宽容）。

> 这个文件是一次**整体重写**，除上面列出的方法外，其余内容按这四条处理，不要留旧代码：
> 1. 模块级的 `pad(value)` 与 `dayKey(date)` **删掉** —— 新版把 `dayKey` 收进 `Page` 当方法（`this.dayKey`），`pad` 是它的内联实现，重复留着会有一个永远不被调用的死函数。
> 2. 模块级常量 `LOCAL_ID_KEY` 与 `ensureLocalId()` **整个删掉**；用户号改成显示服务端快照里的 `quota.userId`。
> 3. `require('../../utils/member')`（`getMemberState`）与 `utils/quota` 的 `addBalance` **都不再引入** —— 会员状态与余额全部来自服务端快照（§3.4）。注意 `utils/member.js` 在 Task 14 已被删除，留着这个 require 会直接报模块找不到。
> 4. `data` 里的 `quotaUnlimited` / `availableCount` 两个键**删掉** —— 新模型没有「不限量」这个概念，wxml 里对应的那个分支在 Step 3(b) 一并去掉。

- [ ] **Step 3: 改 `pages/my/my.wxml`**

(a) 顶部资料区改成可编辑（昵称 + 头像），并把「ID {{userId}}」改成服务端 id：

```html
  <view class="profile">
    <button class="profile-avatar-btn" open-type="chooseAvatar" bindchooseavatar="chooseAvatar">
      <image wx:if="{{avatar}}" class="profile-avatar" src="{{avatar}}" mode="aspectFill"></image>
      <view wx:else class="profile-mark"><view></view><view></view><view></view></view>
    </button>
    <view class="profile-copy">
      <view class="profile-line">
        <input class="profile-name-input" type="nickname" value="{{nickname}}" placeholder="点击设置昵称"
               placeholder-class="profile-name-placeholder" bindchange="onNicknameChange" />
        <text class="level-chip {{levelActive ? 'level-chip-active' : ''}}">{{levelLabel}}</text>
      </view>
      <text class="profile-id">ID {{userId}}</text>
    </view>
  </view>
```

(b) 把顶部那张「今日剩余次数」卡片里的 `quotaUnlimited` 分支去掉（新模型没有不限量）。
> 2026-09-22 实测：这张卡是**活的**（`my.wxml:13-25`），**不是**注释里的那块 —— 注释掉的是下面
> `:27-58` 那一整段（「暂时隐藏：次数统计 / 会员 / 每日签到」）。别去改下面那块来交这一步的差。

```html
  <!-- 今日剩余次数：数值取自 utils/quota.js 的**服务端快照**，本地不做任何额度运算（docs/wx-login.md §3.4） -->
  <view class="card stat-card">
    <view class="stat-main">
      <text class="stat-label">今日剩余次数</text>
      <view class="stat-value">
        <text class="stat-number {{todayQuota <= 2 ? 'stat-number-low' : ''}}">{{todayQuota}}</text>
        <text class="stat-unit">次</text>
      </view>
    </view>
    <view class="stat-line"></view>
    <view class="stat-side"><text class="stat-side-label">每日额度</text><view class="stat-value"><text class="stat-side-value">{{dailyQuota}}</text></view></view>
    <view class="stat-side"><text class="stat-side-label">今日已用</text><view class="stat-value"><text class="stat-side-value">{{todayCount}}</text></view></view>
  </view>
```

(c) 把「暂时隐藏」的会员卡与签到两块**启用**（去掉 `<!-- -->` 包裹），并按新模型改文案：
> 2026-09-22 实测：那个 `<!-- -->`（`my.wxml:27-58`）包着**三**块 —— 次数统计 / 会员卡 / 每日签到。
> 要启用的是后两块；**次数统计那块整块删掉**，不要跟着放出来：它读的是 `quotaUnlimited` 与
> `availableCount`，而 Step 2 第 4 条已经把这两个键从 `data` 里删了，放出来只会渲染成 `undefined`；
> 何况上面那张活卡片已经承担了「今日剩余次数」的展示，重复。

```html
  <view class="vip-card {{levelActive ? 'vip-card-active' : ''}}" bindtap="openMember">
    <view class="vip-icon icon-crown"></view>
    <view class="vip-copy">
      <text class="vip-title">{{levelActive ? memberPlanName : '开通会员'}}</text>
      <text class="vip-sub">{{memberSub}}</text>
    </view>
    <view class="vip-action {{levelActive ? 'vip-action-done' : ''}}">{{memberAction}}</view>
  </view>

  <view class="card checkin-card">
    <view class="checkin-box"><view class="checkin-icon icon-gift"></view></view>
    <view class="checkin-copy">
      <text class="checkin-title">每日签到</text>
      <text class="checkin-sub">{{checkedIn ? '已连续签到 ' + streak + ' 天' : '签到领取 ' + signInBonus + ' 次账户余额'}}</text>
    </view>
    <button class="checkin-button {{checkedIn ? 'checkin-button-done' : ''}}" bindtap="signIn">{{checkedIn ? '已签到' : '签到'}}</button>
  </view>
```

（`signInBonus` 要加回 `data`：`signInBonus: SIGN_IN_BONUS` —— 它是服务端不可达时的显示兜底，真实奖励数以 `/wx/checkin` 返回的 `bonus` 为准。）

(d) 检查 `my.wxss` 里 `.stat-number-low`、`.vip-card`、`.checkin-card` 的规则都还在（它们本来就有，只是模板被注释了）；`.profile-name-input` / `.profile-name-placeholder` 大概率没有，按 `.profile-name` 的字号字重补上。

- [ ] **Step 4: 跑测试确认通过**

Run: `node scripts/verify-theme-c.js`
Expected: 第 10、12 节全绿。第 3 节的「静态类名扫描」会报 **3 个**类名没有样式：`.profile-avatar-btn` / `.profile-avatar` / `.profile-name-input`（**不是两个**，第三个不是你的 bug），在 `my.wxss` 里补上：尺寸复用 `.profile-mark`，字号字重照 `.profile-name`；`profile-name-placeholder` 扫描看不到，一并补上。

- [ ] **Step 5: 手工验证**

开发者工具里点「签到」，确认弹出「签到成功，余额 +10」且「今日已用」与首页提示一致；把 `daily_quota` 在后台调小后重新进入我的页，数字应跟着服务端变。

- [ ] **Step 6: Commit**

```bash
git add miniprogram/pages/my/my.js miniprogram/pages/my/my.wxml miniprogram/pages/my/my.wxss
git commit -m "feat(my): server-driven quota, checkin, profile and membership card"
```

---

## Task 17: 会员页改为展示服务端四档卡

**Files:**
- Modify: `miniprogram/pages/member/member.js`
- Modify: `miniprogram/pages/member/member.wxml`
- Modify: `miniprogram/pages/member/member.wxss`（**补 `.plan-quota` 规则** —— 见 Step 4(b)。2026-09-22 实测：
  Step 4(b) 的新模板引入了 `class="plan-quota"`，而这条规则在 `member.wxss` 与 `app.wxss` 里都不存在，
  第 3 节的静态类名扫描会报红，本任务不改 wxss 就结束不了。顺带：`plan-icon` / `plan-price` /
  `plan-symbol` / `plan-number` 改完后没人用了，**留着别删** —— 扫描只查「模板里的类名有没有规则」，
  不查反方向）
- Test: `scripts/verify-theme-c.js`（会员页断言，若无对应断言则跳过）
- Modify: `src/api/wx.py`（Step 1 追加只读路由，在 `media-parser` 仓库里）
- Modify: `tests/test_wx_login.py`（Step 1 追加用例，同样在 `media-parser` 里）

**Interfaces:**
- Consumes: `getQuota()` / `fetchQuota()`（Task 14）
- Produces: 页面 `data`：`state`（会员状态快照）、`plans`（**服务端下发的四档卡，只展示、不可购买**）
- 删除：**页面** `pages/member/member.js` 里对 `PLANS` / `redeemCode` / `activate` 的引用，以及卡密兑换整块 UI（本地卡密是演示实现，服务端没有对应接口）。2026-09-22 实测：`miniprogram/utils/member.js` **已经在 Task 14 整文件删除**了，所以本任务没有「删这个 util」这一步 —— 要动的只有页面文件，Step 3 整文件重写已经覆盖

**四档卡从哪来**：`/wx/me` 的快照里只有当前卡种，没有「全部卡种列表」。本任务用 `member_plans` 的数据需要一个新接口。两个选择：

**(甲) 新增只读接口** `GET /api/v1/wx/member-plans`（无需 token，返回 `[{code, name, days, daily_quota}]`）—— 需要回到服务端加一个路由 + 用例（约 20 行）。

**(乙) 客户端写死四档卡的展示数据** —— 与「卡种做成数据而不是代码常量」的初衷相悖（§1.3），后台改了额度前端不会跟着变。

**本计划选 (甲)**，因为它才是设计文档 §3.3「展示服务端下发的四档卡」的字面要求。

- [ ] **Step 1: 服务端补接口（在 `media-parser` 里做）**

追加到 `src/api/wx.py`：

```python
@bp.get('/v1/wx/member-plans')
def member_plans():
    """四档卡的展示数据。不含价格与任何敏感信息，无需 token（§3.3）。"""
    rows = get_db().execute(
        "SELECT code, name, days, daily_quota FROM member_plans WHERE active=1 ORDER BY sort_order, code"
    ).fetchall()
    return make_response(200, '成功', {
        'plans': [dict(row) for row in rows]
    }, True), 200
```

用例追加到 `tests/test_wx_login.py` 的 **`WxMeTest`** 类里 —— 它有 `self.client`，
`WX_APPID` / `WX_APPSECRET` 也已配好。接口本身不需要 token（Step 1 的路由没有走 `_auth_or_error()`），
所以 `setUp` 里那次登录对本用例没有影响。

> **别放进 `MemberPlanTest`**。它的名字最像，但那个类是**纯单元测试**：只建 app、直接 `get_db()` 查库，
> **从来没有 `self.client = self.app.test_client()`**，`self.client.get(...)` 会以 `AttributeError` 收场。
> （2026-09-22 实测：该文件 12 个测试类里，`self.client` 出现在 `WxLoginTest` / `WxMeTest` / `WxCheckinTest` /
> `WxProfileTest` / `WxParseAuthTest` / `AdminCardTest` / `AdminMemberPlanSettingsTest` /
> `PortalKeyCreationClosedTest` 八个类里，`MemberPlanTest` 不在其中。）

```python
    def test_member_plans_endpoint_lists_the_four_cards(self):
        response = self.client.get("/api/v1/wx/member-plans")
        self.assertEqual(response.status_code, 200)
        plans = response.get_json()["data"]["plans"]
        self.assertEqual([p["code"] for p in plans], ["day", "month", "quarter", "year"])
        self.assertEqual(plans[0]["daily_quota"], 200)
```

Run: `python -m pytest tests/test_wx_login.py -v` → 全绿；`git commit -m "feat(wx): expose member plan list for the client"`

- [ ] **Step 2: 写失败测试**

Run: `node scripts/verify-theme-c.js`
Expected: 记录基线 —— **195 ok / 0 FAIL / exit 0**。

> 2026-09-22 实测：这个脚本里**没有任何会员页断言**。全文件的 `member` 只出现在文件清单（`:16` / `:35` /
> `:68`）、wxss 映射（`:163`），以及第 12 节首页额度提示的用例（`:702-712`，断的是**首页**那句提示，不是
> 会员页）。它也是按源码文本断言的，所以 `member.js` 里那句还没换掉的 `require('../../utils/member')`
> （Task 14 有意留下的过渡态）**不会**让任何一条变红。
>
> 因此本任务是「基线即绿、做完仍绿」：`grep -c '\[ok\]'` 前后都该是 195。别去找那个红，更别为了让某条
> 转绿去改断言 —— 这个任务真正的验证在别处：Step 1 的服务端用例、以及第 3 节静态类名扫描不许报红。

- [ ] **Step 3: 重写 `pages/member/member.js`**

```js
const { fetchQuota, getQuota } = require('../../utils/quota')
const { request } = require('../../utils/auth')

Page({
  data: {
    state: { active: false, planLabel: '', daysLeft: 0, dailyQuota: 0 },
    plans: [],
    benefits: [
      { key: 'quota', text: '更高的每日解析额度' },
      { key: 'badge', text: '会员身份标识' }
    ]
  },

  onShow() {
    this.refresh()
    this.loadPlans()
  },

  refresh() {
    const quota = getQuota()
    this.setData({
      state: {
        active: quota.member,
        planLabel: quota.memberPlanName,
        daysLeft: quota.memberDaysLeft,
        dailyQuota: quota.dailyQuota
      }
    })
    fetchQuota().then(() => {
      const latest = getQuota()
      this.setData({
        state: {
          active: latest.member,
          planLabel: latest.memberPlanName,
          daysLeft: latest.memberDaysLeft,
          dailyQuota: latest.dailyQuota
        }
      })
    }).catch(() => {})
  },

  // 四档卡由服务端下发：后台改额度、上下架，客户端不用重新发版（§1.3）
  loadPlans() {
    request({ path: '/api/v1/wx/member-plans' })
      .then((data) => this.setData({ plans: (data && data.plans) || [] }))
      .catch(() => {})
  },

  buyPlan(event) {
    const plan = this.data.plans.find((item) => item.code === event.currentTarget.dataset.code)
    if (!plan) return
    wx.showModal({
      title: plan.name,
      content: `当前版本尚未接入微信支付，暂时无法在线开通。可联系客服人工开通${plan.name}（每日 ${plan.daily_quota} 次）。`,
      showCancel: false,
      confirmText: '知道了'
    })
  }
})
```

- [ ] **Step 4: 改 `pages/member/member.wxml`**

(a) 状态卡文案改用新的 `state` 字段（`state.active` / `state.planLabel` / `state.daysLeft` / `state.dailyQuota`），去掉 `state.forever`（新模型没有永久卡）：

```html
        <text class="status-sub">{{state.active ? state.planLabel + ' · 剩余 ' + state.daysLeft + ' 天 · 每日 ' + state.dailyQuota + ' 次' : '开通后立即提升每日解析额度'}}</text>
```

(b) 套餐列表改成服务端数据、按 daily_quota 展示：

```html
  <view class="plan-list">
    <view class="plan-row" wx:for="{{plans}}" wx:key="code">
      <view class="plan-copy">
        <text class="plan-name">{{item.name}}</text>
        <text class="plan-unit">{{item.days}} 天有效</text>
      </view>
      <view class="plan-quota">每日 {{item.daily_quota}} 次</view>
      <button class="plan-buy" data-code="{{item.code}}" bindtap="buyPlan">开通</button>
    </view>
  </view>
```

(c) **删掉整块卡密兑换卡**（`<view class="card redeem-card">…</view>`）与底部那句「可先通过卡密兑换开通」，改成：

```html
  <view class="member-note">当前版本暂未接入在线支付，如需开通会员请联系客服。</view>
```

（`redeem-card` / `redeem-*` 的样式规则留着不动 —— `verify-theme-c.js` 的 box-sizing 那一条覆盖全项目的「高度+内边距」规则，删样式反而会碰到它。2026-09-22 实测：脚本里**没有**「有样式规则没被任何模板用到」这条断言（全文件的 `未使用` / `未用到` 只出现在 `config.API_PATH` 与 `<audio>` 两条断言上），所以这些规则留着不会报红，也没有「按脚本提示处理」这回事。）

- [ ] **Step 5: 跑测试确认通过**

Run: `node scripts/verify-theme-c.js`
Expected: **195 ok / 0 FAIL / exit 0**，与第 2 步记下的基线逐字相同（原因见第 2 步的实测说明：这套件不看
会员页的渲染）。它对本任务唯一的约束是第 3 节的静态类名扫描不许报红 —— 即 Step 4(b) 引入的
`plan-quota` 必须有样式规则，那一步已经在 Files 与 Step 4 里交代过。

- [ ] **Step 6: Commit**

```bash
git add miniprogram/pages/member/member.js miniprogram/pages/member/member.wxml
git commit -m "feat(member): render the four server-driven cards, display only"
```

---

## Task 18: 切换收尾（第 3、4 步，需要人工确认后才能执行）

**Files:**
- Modify: `D:\ClaudeCodeProject\DeMark\miniprogram\config.js:11`（删除 `API_KEY`）
- Modify: `D:\ClaudeCodeProject\DeMark\miniprogram\README.md`（更新配置说明）
- Modify: `D:\ClaudeCodeProject\DeMark\scripts\probe-parse.js`（原始响应探针改用令牌；见 Step 2 末段）

**这是一次不可逆的切换**，顺序颠倒会让小程序解析全线 401（设计文档 §6.3）。**动手前必须满足以下全部条件**：

1. 服务端已上线并稳定运行 ≥ 1 天；
2. 请求日志里**已经出现真实的微信用户 id** 记录，且这些记录 `api_key_id IS NULL`（§6.3 第 2 步）；
3. 真机在小程序里**至少成功解析过一次**（不是开发者工具）。
4. 容器的 `environment:` 里**确实有** `WX_APPID` / `WX_APPSECRET`，且 `/api/v1/wx/login` 对真机返回
   400（code 无效）而不是 503（未配置）—— 第 2 条前提（日志里有真实微信用户 id）本来就要求这一点，
   这里把它显式列出来，省得切流当天才发现登录从来没通过。

- [ ] **Step 1: 核对上面三条**

在后台 `/admin/logs` 按 `api_key_id IS NULL` 筛一遍，确认有微信用户记录。**没有就别往下做。**

- [ ] **Step 2: 删掉包内的 `API_KEY`**

`miniprogram/config.js` 里删掉这两行（含上面的注释块）：

```js
  // 危险：小程序包可以被下载反编译，写在这里的 key 等同于公开，
  // 别人拿去刷解析会直接扣光账号积分（接口按量计费，有 INSUFFICIENT_CREDITS 错误码）。
  // 仅限开发自测；发布前必须挪到云函数或自有后端，由服务端携带 Authorization 调用。
  API_KEY: 'mp-q6QQ9oyX9wCtXojH7WEDYyvb',
```

并在文件顶部加一句注释解释这段历史：

```js
  // 2026-09-22：这里曾经硬编码过一把**管理员** API Key。它的实际代价不是扣分（管理员不扣费），
  // 而是全体用户共享同一个 20 QPS 桶、所有解析都记在管理员名下、以及密钥随包公开。
  // 现已改为微信静默登录 + X-WX-Token（见 docs/wx-login.md §6.1）。
```

**还有一处真的在用这把密钥**：`scripts/probe-parse.js`（原始响应探针 —— 排查「某个字段到底长什么样」
时用，它打印的是**未归一化**的原始返回，这一点 `test-parser.js` 替代不了：那边的带链接模式会把 body 交给
`normalizeResponse`，失败响应会直接抛错）。它现在解构 `API_KEY` 并发 `Authorization: Bearer`；`config.js`
里那行一删，它就会发 `Bearer undefined`，也就是坏掉。同步改掉它（3 行）：

```js
const { API_BASE_URL, API_PATH } = require('../miniprogram/config')

// 2026-09-22：包内 API_KEY 已随 X-WX-Token 通道下线（docs/wx-login.md §6.1），探针改用令牌。
// 令牌取自开发者工具的 Storage 面板（键名 jingji_wx_token）：
//   PowerShell: $env:WX_TOKEN='<令牌>'; node scripts/probe-parse.js "<分享链接>"
//   bash:       WX_TOKEN=<令牌> node scripts/probe-parse.js "<分享链接>"
const TOKEN = process.env.WX_TOKEN || ''
if (!TOKEN) {
  console.log('提示：未设置 WX_TOKEN，下面很可能拿到 401 —— 令牌见开发者工具 Storage 面板的 jingji_wx_token')
}
```

请求头那一行改成：

```js
  headers: { Accept: 'application/json', 'X-WX-Token': TOKEN }
```

- [ ] **Step 3: 确认没有残留引用**

**真正要证明的是「这把密钥本身没了」**，所以主检查盯的是密钥值，不是 `API_KEY` 这个**名字**
（名字还会出现在服务端错误码、断言文本和历史说明里）：

Run: `cd D:\ClaudeCodeProject\DeMark && grep -rn "mp-q6QQ9oyX9wCtXojH7WEDYyvb" miniprogram scripts`
Expected: 无输出。

再跑一次名字检查，确认剩下的命中**全部**是下面这些，一条都不能多：

Run: `cd D:\ClaudeCodeProject\DeMark && grep -rn "API_KEY" miniprogram scripts`
Expected: 只有三类命中，逐条核对：

1. `miniprogram/utils/errors.js` 的 `API_KEY_REQUIRED` / `INVALID_API_KEY` / `API_KEY_DISABLED` ——
   这是**服务端错误码的名字**，不是这把密钥。**不许删**：删了用户就看不到那三句提示了。
2. `scripts/test-parser.js` 第 6 节的断言（`indexOf('API_KEY') < 0`）和它的 `INVALID_API_KEY` 样本 ——
   那是 Task 13 刚加的守门断言，**不许删**：删了「parser.js 不再引用密钥」就没人看着了。
3. `miniprogram/README.md` 的历史说明 —— 按 Step 2 的要求改过之后，它只能作为**历史**出现，
   不能再写成一份可用的配置项。

> 2026-09-22 修订理由：原文写的是「Expected: 无输出」。这句话**做不到** —— 上面三类命中里有两类是
> 故意要留的，而要用「无输出」交差，最短的路是删掉 `errors.js` 的三个错误码和 Task 13 的断言，也就是
> 用拆掉两处守卫来换一次绿色。所以主检查改成 grep 密钥值（它能精确回答「密钥还在不在」，既不会假绿
> 也不会被错误码名字搅乱），名字检查则把允许的命中逐条写明。

- [ ] **Step 4: 跑全部客户端校验**

```bash
node scripts/test-auth.js
node scripts/test-parser.js
node scripts/verify-theme-c.js
```
Expected: 三个脚本全部退出码 0。

- [ ] **Step 5: 真机再现一次解析成功**

开发者工具上传新版本 → 真机预览 → 粘贴一条分享链接 → 解析成功。**失败就回滚这一步**（把 `API_KEY` 加回去），先查服务端日志。

- [ ] **Step 6: 人工在后台停用那把管理员密钥**

`/admin/keys` 找到 `mp-q6QQ9oyX9wCtXojH7WEDYyvb`，**先把它密钥级的 `qps_limit` 设成 1 当哨兵**（万一还有老客户端在用，会立刻在日志里冒出来），观察一天无异常后再停用，最后删除（它已经出过包，不再算秘密）。

- [ ] **Step 7: Commit（跳过）**

> **DeMark 不是 git 仓库**（`git rev-parse` 会失败），所以这一条**跳过**，报告里写「未提交」。本次改动以
> 「文件清单 + md5」记录，和 Task 12–17 一样。下面这段命令留作将来 DeMark 纳入版本管理后的参考。

```bash
git add miniprogram/config.js miniprogram/README.md
git commit -m "feat: drop the bundled api key now that wechat login is live

设计文档 §6.3 第 3 步。包内密钥等同于公开，且它让全站共享一个 20 QPS 桶。"
```

---

## 交付后的人工操作（不是代码任务）

按设计文档 §6.3 / §6.5，上线后还有三件只能人工在后台做的事：

| # | 操作 | 位置 | 为什么 |
| :--- | :--- | :--- | :--- |
| 1 | 勾选「不限试用期」；`初始积分` 填 **0**，**且千万不勾旁边的「无限积分」** | `/admin/settings` | 新注册不白送 100 次（§6.5，代码零改动）。⚠️ 陷阱：`chk_unlimited_credits` 一勾会写 `-1`（无限），`default_initial_credits` 输入框同时被禁用（`templates/admin/settings.html:175,186`）—— 填 0 也没用，等于把积分付费墙整个关掉 |
| 2 | 确认免费档每日额度 | `/admin/settings` → `免费用户每日额度` | 默认 10，与客户端首帧兜底一致 |
| 3 | 确认生产是否 `API_ONLY=true` | 部署环境变量（**不是**后台那个「API 服务总开关」） | 若开启，`/api/v1/parse` 免鉴权；`wx_bp` 已在 `API_ONLY` 之外注册，登录不受影响（§7）。两者别混：后台的 `global_api_enabled` 关掉会让解析接口一律 503，**小程序也会一起停**（`src/api/parse.py:33,74`） |

---

## Self-Review

**1. 覆盖设计文档的每一节**

| 设计文档 | 落在哪个任务 |
| :--- | :--- |
| §1.2 寄生 `users` 表 + 7 列 + 部分唯一索引 | Task 1 |
| §1.3 四张新表 + 卡种播种 | Task 1 |
| §1.4 轻量迁移 `_ensure_column` | Task 1 |
| §1.5 `session_key` 加密 | Task 4（落库在 Task 5） |
| §1.6 每日额度 + 原子预占 + 失败退回 | Task 2（消费）、Task 8（集成） |
| §1.7 头像服务端转存 | Task 7（服务端收字节 + `MAX_CONTENT_LENGTH` 提到 4MB）、Task 12 的 `auth.upload`、Task 16 的 `chooseAvatar` |
| §1.8 数据流 | Task 8 |
| §2.1 蓝本与信封 | Task 5 |
| §2.2 响应字段（含"不提供 available / unlimited"） | Task 5（断言逐字核对字段表） |
| §2.3 错误码 | Task 5 / 6 / 8（每个码都有用例） |
| §2.4 额度与会员判定、开卡口径、🔴 不复用 `expires_at` | Task 3（含"会员过期不产生 403"用例）、Task 8 |
| §2.5 双路鉴别 + 四道检查 + 限流主体复用 | Task 8 |
| §3.1 `utils/auth.js` 三条导出 + 只重试一次 + 并发只登一次 | Task 12（三条行为都有用例） |
| §3.2 额度/会员/统计/config 改造 | Task 14（`stats.js` 另有说明，见偏离说明 A） |
| §3.3 四个页面调整 | Task 15 / 16 / 17（`pages/result` 不动 ✅） |
| §3.4 铁律 | Global Constraints 1 + Task 15 的 `refreshQuota` 实现 |
| §4.1 服务端用例表 | Task 1–11 的测试代码（逐条对应，见下） |
| §4.2 客户端用例表 | Task 12 |
| §4.3 `verify-theme-c.js` 先红后改 | Task 14 / 15 / 16 |
| §4.4 回归 | Task 1 / 8 / 11 各有一个全量回归步骤 |
| §5.1 第一期清单 | 全覆盖；「关掉门户自助建密钥」= Task 11；「新注册不设试用期」= 交付后人工操作 1 |
| §5.2 第二期 | **不在本计划**（`wx_orders` 建空表 + `member_plans.wx_product_id` 列 + 开卡函数已在 Task 1 / 3 备好） |
| §6.1 现状核查 | 无代码改动（是核查结论） |
| §6.2 映射 + 密钥去留 + `credits = 0` | Task 5（建行）、Task 11（关创建） |
| §6.3 切换顺序 | Task 18（第 3、4 步）+ 前置条件 |
| §6.4 后台改动 | Task 9（开卡，必须）、Task 10（卡种管理 + 设置，建议）。**「拆开账号有效期与会员到期两个输入框」按文档是第二期前必须，本计划不做** |
| §6.5 新注册不设试用期 | 交付后人工操作 1 |
| §7 影响面小结 | Global Constraints 7 / 11 + Task 8 / 11 的回归 |

**§4.1 用例表逐条对照**（设计文档左列 → 本计划的测试方法）：

| 设计文档用例 | 落点 |
| :--- | :--- |
| 首次登录建行返回 token | `test_first_login_creates_a_user_with_the_expected_values` |
| 新用户取值 `credits=0` / `role=user` / `expires_at IS NULL` / `member_plan IS NULL` | 同上（四条逐个 assert） |
| 二次登录复用 `user_id` | `test_second_login_reuses_the_same_user_row` |
| token 有效 / 过期 / 伪造 | `test_valid_token_parses_and_counts_one` / `test_me_with_expired_session_is_401_expired` / `test_me_with_unknown_token_is_401` |
| 免费档边界第 N+1 次 402 | `test_free_tier_boundary_is_enforced_with_the_new_error_code` |
| 并发不越界 | `test_concurrent_requests_never_exceed_the_cap` |
| 解析失败退回 | `test_failed_parse_refunds_the_reservation` + `test_business_failure_also_refunds` |
| 会员档边界（日卡 200、跨日归零） | `test_member_gets_the_card_quota` + `test_days_are_independent` |
| 会员到期降级且不 403 | `test_expired_member_falls_back_to_the_free_tier_not_403` + `test_expired_member_falls_back_to_the_free_tier_without_touching_expires_at` |
| 开卡叠加 / 不降级 | `test_renewal_extends_instead_of_overwriting` + `test_buying_a_smaller_card_does_not_downgrade_an_active_bigger_card` |
| `credits = 0` 不被 402 | `test_free_user_with_zero_credits_is_not_blocked_by_the_legacy_check` |
| `member_expires_at` 过期不 403、`expires_at` 过期才 403 | 上面会员那条 + `test_expired_account_is_403` |
| 管理员不扣费 | `test_admin_token_is_not_quota_limited` |
| 限流主体复用 | `test_token_and_api_key_share_one_rate_limit_bucket` |
| 后台停用生效 | `test_disabled_account_is_403_not_200` |
| 重复签到 409 且余额不变 | `test_second_checkin_same_day_is_409_and_does_not_pay_again` |
| 跨日签到 / 断签 | `test_consecutive_days_increase_the_streak` + `test_a_broken_streak_restarts_at_one` |
| 头像转存往返 | `test_avatar_upload_round_trips_byte_for_byte`（落盘字节一致 + 取回 Content-Type 与字节都一致） |
| 缺 WX_APPID → 503 | `test_unconfigured_appid_returns_503` |
| 迁移幂等 | `test_init_db_is_idempotent_on_existing_database` |
| 卡种播种幂等 | `test_member_plan_seed_does_not_overwrite_edited_values` |
| — | 额外补了：`test_avatar_content_type_is_sniffed_from_bytes_not_the_filename`、`test_non_image_upload_is_rejected_and_nothing_is_written`、`test_empty_upload_is_rejected`、`test_oversized_avatar_is_rejected_with_400`、`test_a_real_sized_avatar_is_not_blocked_by_the_body_limit`、`test_missing_avatar_file_is_404`、`test_api_key_path_still_enforces_the_credits_paywall`、`test_no_credentials_keeps_the_legacy_response`、`test_daily_quota_exhausted_does_not_touch_credits` |

**2. 占位符扫描**

逐节检查过：没有 TBD / TODO / "类似 Task N" / "添加适当的错误处理" / 无代码的步骤。每个代码步骤都给了可直接粘贴的完整实现。第一版里 Task 16 曾留过一处「停一下，等你选头像方案」—— 写完后我把它落成了完整代码（`wx.uploadFile` + `app.py` 的限额调整），因为 `wxfile://` 是本机临时路径、服务端无法访问，答案由平台决定而非由偏好决定，留在计划里当选择题是错的。

**3. 类型与命名一致性**

跨任务核对过的名字：

- `consume_daily_quota(user_id, day, cap)` —— 定义 Task 2，调用 Task 8 ✅
- `refund_daily_quota(user_id, day)` —— 定义 Task 2，调用 Task 8 ✅
- `get_daily_usage(user_id, day)` —— 定义 Task 2，调用 Task 5（`_user_snapshot`）、Task 6 用例 ✅
- `is_member(user)` / `daily_quota_cap(user)` / `get_member_plan(code)` / `open_member_card(user_id, plan_code)` —— 定义 Task 3，调用 Task 5 / 6 / 8 / 9 / 10 ✅
- `parse_utc(value)` —— 定义 Task 3，调用 Task 5 ✅
- `_user_snapshot(user)` —— 定义 Task 5，调用 Task 6 / 7 ✅
- `_auth_or_error()` —— 定义 Task 6，调用 Task 7 ✅
- `encrypt_session_key` / `decrypt_session_key` —— 定义 Task 4，调用 Task 5 / 用例 ✅
- `authenticate_wx_token() -> (access|None, error|None)` —— 定义 Task 5 第 0 步（代码由 Task 8 第 3 步给出），调用 Task 6 / 7 / 8；access 的键 `user_id` / `id` / `wx` / `quota_cap` 在 Task 8 的 parse.py 里逐字使用 ✅
- 客户端 `getQuota()` 返回的键：`ready` / `member` / `memberPlanName` / `memberDaysLeft` / `dailyQuota` / `usedToday` / `remainingToday` / `balance` / `nickname` / `avatar` / `userId` —— 定义 Task 14，消费于 Task 15（`remainingToday` / `member` / `dailyQuota` / `ready`）与 Task 16（全部）✅
- 客户端页面 `data` 键 `todayQuota` / `dailyQuota` 与 `verify-theme-c.js` 第 10 节的断言逐字一致 ✅
- `upload(path, options)` —— 定义 Task 12，调用 Task 16 的 `chooseAvatar`；选项键 `filePath` / `name` 与 `auth.js` 实现逐字一致 ✅
- `_store_avatar(user_id, file_storage)` —— 定义并调用都在 Task 7；`AVATAR_MAX_BYTES` 在 Task 7 内定义、被上传与测试引用 ✅
- `banned` 命名冲突已避开：Task 9 的路由叫 `grant_member_card`，不叫 `open_member_card`（后者是 `src/db.py` 的函数）✅
- `member_plans` 这个名字在三处出现、含义不同，特此备注：`src/db.py` 的表、`src/web/admin.py` 的 `member_plans()` 视图（卡种管理页）、`src/api/wx.py` 的 `member_plans()` 视图（列表接口）。同名前缀但分属三个文件、三个蓝本，不会互相遮蔽 ✅

**4. 计划自身的两个已知缺口（需要你决定）**

1. ~~Task 16 的头像上传方式~~ —— **已按 (甲) 落地**：客户端 `wx.uploadFile` 直传字节，服务端 `app.py` 的 `MAX_CONTENT_LENGTH` 从 32KB 提到 4MB（Task 7）。之所以不留给执行者选：`wxfile://` 是用户手机上的临时路径，服务端没有下载它的可能，这不是偏好问题。
2. **`/api/v1/wx/member-plans`** 是 Task 17 才追加的接口，设计文档 §2.1 的接口表里没有它。它只读、不含敏感字段、无需 token，加它是为了让会员页真的"展示服务端下发的卡"而不是写死四档卡。如果你更希望客户端写死，把 Task 17 第 1 步整段跳过即可（我不推荐）。
