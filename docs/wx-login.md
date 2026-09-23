# 小程序登录、会员与额度体系设计 (WeChat Login & Membership Design)

本文档说明 **硬核去水印王** 微信小程序侧的登录、资料、签到、**会员卡**与**每日额度**体系：如何以最小改动寄生在现有 `media-parser` 服务上，以及第二期「小程序虚拟支付」需要提前打好的地基。

---

## 0. 背景与目标

### 0.1 现状（改造前）

小程序目前**没有任何身份概念**：

| 项 | 现状 | 问题 |
| :--- | :--- | :--- |
| 用户身份 | 本地存储里的随机 6 位数字 (`jingji_local_id`) | 清除缓存即换人，无法跨设备，无法在后台看到真实用户 |
| 解析额度 | 客户端本地计数 (`jingji_parse_stats`) | 改 storage 即可重置 |
| 会员状态 | 客户端本地状态 (`utils/member.js`) | 改 storage 即变永久会员 |
| 余额 / 签到 | 本地存储累加 (`jingji_quota_balance`) | 同上，且签到可无限刷 |
| 解析鉴权 | API_KEY 硬编码在小程序包内 | 包一旦被解出，等于站点解析能力对全世界开放 |
| 成本归属 | 配的是**管理员**密钥，而管理员既不扣积分也不判余额耗尽 | 全站零成本控制；全体用户挤在**同一个 20 QPS 桶**里（详见 §6.1） |

### 0.2 目标

1. 引入微信**静默登录**，把身份落到服务端，客户端只持有不可伪造的 token。
2. 把额度、会员、签到**全部搬到服务端计算**，客户端只负责显示。
3. 建立**会员卡体系**：日卡 / 月卡 / 季卡 / 年卡，各自约束到期时间与**每日额度**。
4. `/api/v1/parse` 支持小程序 token 鉴权，从而可以**从包里删掉 API_KEY**。
5. 为第二期虚拟支付铺好地基：`session_key` 服务端留存、订单表幂等、卡片商品可映射到微信侧的 `product_id`。

### 0.3 已确认的前置事实与决定

* **主体**：个人主体，小程序虚拟支付**尚未开通**。因此**本期只做会员模型 + 运营手动开卡，不做支付**（§5）。
* **架构**：选择「扩展现有 `media-parser` 服务」，**不新起服务**。
* **发布状态**：小程序尚未正式发布 → **不存在老用户，无需迁移本地数据**。
* **登录形态**：静默登录。`wx.login()` 不弹任何窗口，用户无感知；"强制登录"在本项目里只能指"强制补全资料"，不是强制授权。
* **新注册不设试用期**：账号永久有效，限制全部落在每日额度上。此项**现有后台已支持**（`default_trial_days = 0` 即不写 `expires_at`，见 §6.5），不需要改代码。
* **API Key 不对用户开放**：门户将来只作内部管理用，用户不知道密钥存在。因此不再为每个用户建密钥（§6.2）。
* **额度模型 = 两层次数（每日额度 + 签到余额），不是单一累计积分**：详见 §1.6。

---

## 1. 架构与数据模型

### 1.1 不新增服务，只新增蓝本与鉴别方式

沿用 `create_app()` 工厂 + Blueprint 的既有结构：

| 文件 | 变更 | 说明 |
| :--- | :--- | :--- |
| `src/api/wx.py` | **新增** | 微信侧全部接口（登录 / 我的 / 资料 / 签到 / 头像） |
| `src/api/access.py` | 扩展 | 新增 `authenticate_wx_token()`，与既有 `authenticate_api_key()` 并列 |
| `src/api/parse.py` | 改鉴别一行为 | 先试 token，再试 API Key（详见 §2.5） |
| `src/db.py` | 加列 + 加表 + 轻量迁移 | 详见 §1.2 ~ §1.4 |
| `app.py` | 注册 `wx_bp` | 与其他蓝本并列，`API_ONLY` 下同样注册（小程序是纯 API 调用方） |
| `src/web/admin.py` | 加开卡入口 | 运营手动开卡的 UI（第二期接支付后由回调替代），见 §6.4 |
| `src/web/portal.py` | **关掉**用户自助建密钥 | `/keys` 那一套不再对用户开放，见 §6.2 |

### 1.2 小程序用户寄生 `users` 表（关键决策）

不自建 `wx_users` 表，而是**复用现有 `users` 表**，靠新增 `openid` 列区分来源。理由是现有表已经把这件事需要的东西全都表达过了：

| 现有字段 | 本项目里的含义 |
| :--- | :--- |
| `credits` | **不再是会员标志**（§2.4），但**是小程序额度的第二层**：每日额度用尽后回退扣它（§1.6）。对既有 Web 客户它仍是按次计费的付费墙（走 API Key 路径）；对小程序用户即"签到余额"，也是后台手动补次数的入口 |
| `expires_at` | **账号有效期**（既有语义，**不是会员到期**）：`user_is_expired()` 用它，过期直接 403 `ACCOUNT_EXPIRED`。微信用户留 `NULL` = 账号不受时限 |
| `qps_limit` | 并发限流（微信用户取 `default_user_qps` = 2） |
| `role` / `active` | `role` 必须是 `'user'`；`active` 是唯一能同时拦住登录与接口的开关（`active = 0` 即封禁） |
| `username` / `password_hash` | 小程序用户填 `wx_<完整 openid>`，配一个不可登录的占位 hash（`username` 是 `NOT NULL UNIQUE`，截断 openid 会有撞库风险，直接用完整值 —— openid 本身唯一，因此天然不冲突） |

> ⚠️ **`credits = -1` 的旧语义「不限次」在本设计里作废**。上一版曾打算复用它表示会员，但会员卡机制是「**每日 N 次**」而不是「不限次」，两者不能混用。新模型下**会员身份**完全由 `member_plan` + `member_expires_at` 表达，`credits` 不参与**会员**判定。
>
> 🔄 **2026-09-22 二次修订**：`credits` 不参与会员判定（本段）与它是额度的第二层（§1.6）**并不矛盾** —— 前者说的是"它不能拿来判断你是不是会员"，后者说的是"会员/非会员都会在每日额度用尽后扣它"。`credits = -1` 仍作为「不限次」保留给管理员与后台勾了"不限积分"的账号，但它**不是**一种会员形态。

**新增列**（全部可空，因此不影响既有 Web 用户）：

```sql
openid             TEXT     -- 微信 openid，唯一（见下）
nickname           TEXT     -- 用户自填昵称
avatar             TEXT     -- 本服务提供的头像路径，非微信临时链接
member_plan        TEXT     -- 当前生效卡种：day / month / quarter / year；NULL = 非会员
member_expires_at  TEXT     -- 会员到期；NULL = 非会员（与 expires_at 是两个概念，见 §2.4）
last_checkin_date  TEXT     -- 最近一次签到日（北京时区 YYYY-MM-DD）
checkin_streak     INTEGER NOT NULL DEFAULT 0
```

`openid` 的唯一性**不能**写在 `ALTER TABLE ADD COLUMN` 里（SQLite 不允许给新增列直接加 `UNIQUE` 约束），改用**部分唯一索引**：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_openid
    ON users(openid) WHERE openid IS NOT NULL;
```

用部分索引而不是普通唯一索引，是为了让所有既有 Web 用户（`openid` 全为 `NULL`）和平共存 —— 普通唯一索引虽也允许多个 `NULL`，但显式写成部分索引意图更清楚，也不给未来留坑。

### 1.3 新增四张表

```sql
CREATE TABLE IF NOT EXISTS wx_sessions (
    token_hash   TEXT PRIMARY KEY,      -- sha256(token)，不存明文 token
    user_id      INTEGER NOT NULL,
    session_key  TEXT NOT NULL,         -- AES-GCM 密文，非明文
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,         -- 默认 30 天
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS member_plans (
    code         TEXT PRIMARY KEY,      -- day / month / quarter / year
    name         TEXT NOT NULL,         -- 日卡 / 月卡 / 季卡 / 年卡
    days         INTEGER NOT NULL,      -- 有效时长
    daily_quota  INTEGER NOT NULL,      -- 该卡种的每日额度
    price_fen    INTEGER,               -- 第二期用；本期为 NULL
    wx_product_id TEXT,                 -- 第二期虚拟支付的商品映射
    active       INTEGER NOT NULL DEFAULT 1,
    sort_order   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_usage (
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,              -- 北京时区 YYYY-MM-DD
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(user_id, day)
);

CREATE TABLE IF NOT EXISTS wx_orders (
    out_trade_no TEXT PRIMARY KEY,      -- 8~32 位，虚拟支付发货回调的幂等键
    user_id      INTEGER NOT NULL,
    plan_code    TEXT NOT NULL,
    amount_fen   INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'created',
    wx_order_id  TEXT,
    created_at   TEXT NOT NULL,
    paid_at      TEXT,
    delivered_at TEXT
);
```

* **`token` 只存哈希**：token 在服务端唯一的用途是「比对」，从不参与任何加密运算，因此没有理由存明文。这样即使数据库备份外泄，也无法凭库里的内容直接冒充用户。
* **`session_key` 必须可还原**，所以是加密而非哈希（见 §1.5）。
* **`member_plans` 把卡种做成数据而不是代码常量**：改每日额度、调价、上下架都不用改代码；`wx_product_id` 是第二期虚拟支付必须的字段（微信侧要求商品预先注册），现在建列比以后加列省事。
* **`daily_usage` 是额度判定的权威来源**，理由见 §1.6。
* **`wx_orders` 本期只建空表、不写入**：`out_trade_no` 主键天然提供发货回调的幂等性（重复推送同一单号只会命中同一行）。虚拟支付的发货推送会重复投递同一 `out_trade_no`，服务端必须幂等 —— 这一点靠主键约束兜住，不靠应用层判断。

初始卡种用 `INSERT OR IGNORE` 播种，数值可后台再改：

| code | 名称 | 时长 | 每日额度 |
| :--- | :--- | :--- | :--- |
| `day` | 日卡 | 24 小时 | 200 |
| `month` | 月卡 | 30 天 | 300 |
| `quarter` | 季卡 | 90 天 | 300 |
| `year` | 年卡 | 365 天 | 500 |

### 1.4 轻量迁移：`init_db()` 目前没有迁移能力

现有 `init_db()` 只执行 `SCHEMA`（全是 `CREATE TABLE IF NOT EXISTS`）+ 一批 `INSERT OR IGNORE` 默认值。**`ALTER TABLE` 永远不会被执行**，所以直接往 `SCHEMA` 里塞新列对已存在的库毫无作用（新库倒是会有，于是开发机与线上表现不一致 —— 这是最坏的一类 bug）。

补一个最小迁移函数，放在 `executescript(SCHEMA)` 之后、默认值写入之前：

```python
def _ensure_column(db, table, column, ddl):
    """SQLite 无 ALTER ... IF NOT EXISTS，只能先查 PRAGMA 再补列。"""
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
```

对 §1.2 的 7 个新列各调一次。该函数**只加列、不改列、不删列**，保持向前兼容；未来若真需要重建表，再引入正式的迁移机制，本期内不做。

### 1.5 `session_key` 必须加密落库

`session_key` 是虚拟支付用户态签名的密钥（`signature = hmac_sha256(sessionKey, signData)`）。它明文躺在磁盘上，等于把用户钱包的钥匙放在数据库里；一旦备份泄露或 SQL 注入，攻击者可以直接伪造支付签名。因此：

* 算法：**AES-GCM**
* 密钥：由 `SECRET_KEY` 经 **HKDF-SHA256** 派生（`SECRET_KEY` 已由 `_load_or_create_secret()` 持久化在 `data/.secret_key`，无需新增密钥管理）
* 依赖：**`pycryptodome==3.20.0` 已在 `requirements.txt` 里**，不用新增依赖
* 成本：约 15 行代码

这是本期唯一一条"现在不做、第二期会痛"的安全项，因此提前做掉。

### 1.6 额度模型：两层（每日额度 + 签到余额）+ 原子预占（本期最核心的改动）

> 🔄 **2026-09-22 修订（第二版）**：本节初版把额度定成**单层**「每日额度」，并把 `credits`
> 降级成「仅作展示的补偿包」。落地后发现那个决定留下一个坏体验：签到每天送 10 次，而小程序
> 用户**永远花不掉** —— 界面上一个只涨不跌的数字。文档 §2.2 当时称它为「运营手动补偿的次数包」，
> 既然是"次数包"，它就该是能花的次数，只是代码里没人花它。
>
> 现修订为**两层次数**：第一层每日额度（会重置），第二层签到余额（`users.credits`，不过期）。
> 每日额度用尽后**回退**扣余额，两层都空才 402。配套改动：`parse.py` 的回退与分层退款、
> `index.js` 的提示口径、`my.wxml` 的余额显示、`verify-theme-c.js` 第 11/12 节
> （次列 2→3、余额相关断言翻案）。
>
> 副产品：`credits` 重新有了消费方，因此**后台的积分列与批量增减不必撤** ——
> 它们的语义从「补偿」变成「给用户补次数」，现在是真的有效的。

额度模型从「累计余额」换成「每日额度」作为**第一层**。三档上限：

```
当日上限 = 会员有效 ? member_plans[member_plan].daily_quota    -- 200 / 300 / 300 / 500
                    : setting('wx_free_daily_quota')            -- 免费档，默认 10
```

**上限按北京时区自然日重置**，判定用 `daily_usage` 表而不是扫描 `request_logs`。

> 📌 **这里推翻我上一版的一个建议**。上一版说「不建计数器表，直接数 `request_logs`」，理由是免于维护一致性。那个理由在**软额度**下成立，但现在额度是**硬上限**，逐条统计有一个可被利用的漏洞：并发的 N 个请求会同时读到「还没超」，然后**全部放行** —— 一个脚本发 100 个并发请求就能在 200 次的卡上跑出 300 次。所以硬上限必须**原子预占**：
>
> ```python
> def consume_daily_quota(user_id, day, cap):
>     """原子预占一次当日额度。True = 已占；False = 当日额度已用尽。"""
>     with transaction(immediate=True) as db:
>         row = db.execute("SELECT count FROM daily_usage WHERE user_id=? AND day=?", (user_id, day)).fetchone()
>         used = row["count"] if row else 0
>         if used >= cap:
>             return False
>         db.execute(
>             "INSERT INTO daily_usage(user_id,day,count) VALUES(?,?,1) "
>             "ON CONFLICT(user_id,day) DO UPDATE SET count=count+1",
>             (user_id, day),
>         )
>         return True
> ```
>
> `BEGIN IMMEDIATE` 让「读当前计数 + 写入」在同一个写事务内完成，并发请求会排队而不是一起放行。这也顺带让 `/wx/me` 读额度变成一次主键查询，不用扫日志表。

**解析失败要退回这一次预占**（`count = count - 1`，下限 0）：用户不该为一次失败的解析消耗额度。这与既有 `refund_user_credit()` 的精神一致，只是对象从余额换成了当日计数。

**第二层：签到余额（`users.credits`）**，不过期。来源有三：签到 `wx_checkin_bonus`（默认 10）、后台批量增减积分、注册初始积分。

扣费顺序**必须先每日额度、后余额**：额度每天北京 0 点重置、余额不过期，先用会过期的那个、把不过期的留作保底；反过来等于白白扔掉当天的免费次数。

判定直接复用既有的 `reserve_user_credit()` —— 它的三态刚好够用：`True` 已扣、`False` 无需扣（admin 或 `credits == -1`，等同不限次）、`None` 不足。所以第二层**不需要新写任何扣费函数**，`parse.py` 只是在额度返回 `False` 时多一次回退调用。

**退款按层退回**：一次请求只占住其中一层，失败时退哪一层由预占标记决定（`quota_reserved` / `credit_reserved`），不会出现"扣了余额却去退每日额度"。两层共用 `credit_committed` 作为唯一的提交开关 —— 只有 200 才置位。

**会员与余额正交**：会员吃卡种额度，额度用尽后同样可以花余额。余额是用户自己攒的，不因会员身份而失效。

> ⚠️ 数值提醒：免费档每日 10 次、签到默认也送 10，余额一旦能花就等于**每天把免费额度翻倍**。生产环境建议下调 `wx_checkin_bonus`（后台可改，无需发版）。

**为什么不用现成的 `SQLiteRateLimiter`**：它的桶是 `(now // window) * window` 算出来的 **UTC 对齐**窗口（`src/api/access.py:32`）。窗口设成 86400 就是 UTC 日界，而北京日界比它早 8 小时 —— 计数会在北京时间的早上 8 点中途归零，白天再攒一次，等于每天给两份额度。subject 里塞北京日期也救不了，因为 `bucket_second` 仍按 UTC 走。所以这个场景必须用自己的表。

**会员到期当天的一个已知后果**：额度按自然日算，若某天已用 250 次（年卡额度 500）而卡在当天 20:00 到期，则该用户 20:00 之后当天剩余时间**不可用**（因为已用 250 > 免费档 10）。这是「接受日卡跨日、各算各的」这个选择的必然结果，可接受，但要在客服话术里写清楚。

### 1.7 头像必须服务端转存

`<button open-type="chooseAvatar">` 返回的是 **约 2 小时有效的临时链接**。若直接把该 URL 存库，两天后所有用户头像都会变成白图。因此：

1. `POST /profile` 收到微信临时地址后，**服务端立即下载**；
2. 校验为图片、限制体积（≤ 2MB）、统一转存为 `data/avatars/<user_id>.jpg`；
3. 库里 `avatar` 存本服务路径，对外经 `GET /api/v1/wx/avatar/<user_id>` 提供。

`data/` 已在 `docker-compose.yml` 中挂载为卷，重启不丢。

### 1.8 一次解析请求的数据流

```
小程序  ──POST /api/v1/parse──▶  parse.py
   Header: X-WX-Token                │
                                     ├─ authenticate_wx_token()  → 查出 user_id
                                     │      ├─ active / expires_at 检查（403）
                                     │      └─ 复用 user:{user_id} 限流主体
                                     ├─ 当日上限 = 会员卡额度 或 免费档额度        ← §1.6
                                     ├─ consume_daily_quota()  ── 用尽 ─┐
                                     │                                 └─▶ reserve_user_credit()
                                     │                                        两层都空 → 402 DAILY_QUOTA_EXCEEDED
                                     ├─ _execute_parse(text, access)
                                     ├─ 失败 → 按层退回这次预占（哪层扣的就退哪层）
                                     └─ record_request(user_id, ...)   ← 仅用于统计，不参与判定
```

---

## 2. 接口契约

### 2.1 蓝本与统一信封

全部挂在 `/api/v1/wx` 下，响应沿用 `make_response(status, message, data, succ, error_code)` 的信封 —— 小程序的 `parser.js` 已经在解析 `succ` / `error_code` / `retdesc` 这套结构，复用意味着客户端不需要新写一层响应解析。

| 方法 | 路径 | 鉴权 | 说明 |
| :--- | :--- | :--- | :--- |
| POST | `/api/v1/wx/login` | 无 | 入参 `{code}`，返回 `{token, expires_at, user}` |
| GET | `/api/v1/wx/me` | `X-WX-Token` | 用户资料 + 额度快照 |
| POST | `/api/v1/wx/profile` | `X-WX-Token` | 入参 `{nickname, avatar}`；`avatar` 为临时路径，服务端转存 |
| POST | `/api/v1/wx/checkin` | `X-WX-Token` | 签到，返回 `{streak, bonus, balance}` |
| GET | `/api/v1/wx/avatar/<user_id>` | 无 | 头像字节流（本身就是公开材料，无需 token） |

### 2.2 响应字段

`POST /wx/login` 与 `GET /wx/me` 返回同一套 `user` 结构，客户端只需要这一个快照，不再自己算任何东西（对应 §3.4）：

```json
{
  "user": {
    "id": 42,
    "nickname": "阿七",
    "avatar": "/api/v1/wx/avatar/42",
    "member": false,
    "member_plan": null,
    "member_plan_name": null,
    "member_expires_at": null,
    "member_days_left": null,
    "daily_quota": 10,
    "used_today": 2,
    "remaining_today": 8,
    "balance": 0
  }
}
```

字段口径：

| 字段 | 含义 |
| :--- | :--- |
| `member` | 会员是否生效（`member_expires_at` 未过期）。**唯一的真相来源**，客户端不得自行推断 |
| `member_plan` / `member_plan_name` | 卡种代号与显示名（`year` / 年卡）；非会员为 `null` |
| `member_days_left` | 剩余天数，供「剩余 N 天」直接渲染 |
| `daily_quota` | **当日上限**：会员取卡种额度，非会员取免费档额度 |
| `remaining_today` | `max(0, daily_quota - used_today)`；**这就是"今天还能用几次"** |
| `balance` | `credits` 余额 = **第二层次数**（签到攒的、后台补的），每日额度用尽后接着扣。`-1` 表示不限次 |

> 这里**有意不提供** `available`（"还能用几次"的合成值）。两层是**两条不同的话**，不是可以相加的两个数：`remaining_today` 是"今天还剩几次免费额度"，`balance` 是"额度用完之后还能垫几次"。合成成一个字段，客户端就再也说不出"额度已用完、但余额还有几次"这句真正有用的话了 —— 那正是额度见底时用户最需要知道的一句。
>
> 🔄 **2026-09-22 二次修订**：本节原先写的是「每日额度模型下不存在不限量，也不存在『额度 + 余额』的叠加」。前半句对（`-1` 只出现在管理员/不限积分账号，不是额度模型的一部分），后半句**已被 §1.6 的两层模型取代** —— 余额现在就是第二层，额度用尽后会真的接着扣。撤掉 `available` 的理由因此从"两层不该混为一谈"变成"两层必须各自说清"，结论相同、理由相反，故保留本节而改其论证。

`avatar` 返回**相对路径**，由客户端拼域名：服务端可能部署在多个入口（内网 / 域名 / 调试），把绝对地址写死会迟早出错。

### 2.3 错误码

沿用信封里的 `error_code` 字段，客户端据它决定动作：

| `retcode` | `error_code` | 客户端应有动作 |
| :---: | :--- | :--- |
| 401 | `WX_TOKEN_INVALID` | 静默重登一次并重试原请求 |
| 401 | `WX_TOKEN_EXPIRED` | 同上 |
| 400 | `WX_CODE_INVALID` | `wx.login` 的 code 已被用过或超时（5 分钟一次性）—— 重新取 code 再登一次 |
| 403 | `ACCOUNT_DISABLED` | `active = 0`（后台停用）。提示"账号已被停用"，**不要重试** |
| 403 | `ACCOUNT_EXPIRED` | `expires_at` 已过期。提示"账号已到期"，**不要重试** |
| 402 | `DAILY_QUOTA_EXCEEDED` | **本期新增，最主要的那个**：今日额度**与签到余额都**已用尽。提示「今日额度已用完，可签到或开通会员获取更多」，并展示开卡/签到引导。⚠️ 只在**两层都空**时出现 —— 余额还有就别报这个错（§1.6） |
| 409 | `ALREADY_CHECKED_IN` | 提示"今天已经签到过了" |
| 503 | `WX_NOT_CONFIGURED` | 服务端未配 `WX_APPID` / `WX_APPSECRET`；**必须是结构化错误，不能是 500** |

`503 WX_NOT_CONFIGURED` 单列一条，是因为开发/自部署环境很容易忘配环境变量，一个能读懂的错误码远好过一个 500 堆栈。

`DAILY_QUOTA_EXCEEDED` 与「余额不足」必须是**两个不同的错误码**：前者是"明天会恢复"，后者是"要充值"，给用户的引导完全不同，合并成一个码就等于把这两种情况都说不清。

**`/wx/login` 也要查 `active` 与 `expires_at`**，与网页端故意不同：网页登录**不查** `expires_at`（`src/auth.py:212-217` 只判密码和 `active`），到期账号仍能登进去看后台、只是解析时被 403 拦住。小程序没有"登进去看看"这种场景 —— 它的登录就是为了解析，所以更该**在登录这一步就把停用/到期说清楚**，而不是让用户每次点解析都收到一个和额度无关的 403。

> ⚠️ 反过来说：`expires_at` **不适合当封禁或会员手段**。它拦不住网页登录（只能拦接口），而真正能同时拦住登录和接口的是 `active`（§6.2）。且它一旦被误填，表现是"能登录、能用免费额度、但每次解析都 403"，很难排查。

### 2.4 额度判定与会员判定

**额度判定**（每一步都必须原子，见 §1.6）：

```
1. 账号检查（照抄密钥路径，见 §2.5）→ 停用/过期直接 403
2. 取当日上限 cap = 会员有效 ? 卡种额度 : 免费档额度
3. consume_daily_quota(user_id, 北京今日, cap)      ← 第一层：每日额度
       占到位 → 放行
       没占到 → 落第二层（不报错）
4. reserve_user_credit(user_id)                     ← 第二层：签到余额
       已扣 / 无需扣（admin 或 credits == -1）→ 放行
       None（真不足）                        → 402 DAILY_QUOTA_EXCEEDED
5. 解析失败 → 退回这次预占（按层退：哪层扣的退哪层）
```

**第 3、4 步的顺序不能反**：每日额度北京 0 点重置、余额不过期，所以先用会过期的那个、把不过期的留作保底。反过来写等于每天白白扔掉一次免费用户当天的额度 —— 用户看不出错，只是余额少得莫名其妙。

**会员判定**必须集中在一个函数 `is_member(user)` 里：`member_expires_at` 非空且未过期，且 `member_plan` 是有效卡种。它有四个调用点 —— 额度上限、`/wx/me` 展示、签到、以及第二期的开卡逻辑。四处各写一遍必然写歪一处。

**开卡与续期的口径**（运营手动开卡与第二期支付回调共用同一个函数）：

* **到期时间叠加**：新到期时间 = `max(now, 原 member_expires_at) + 卡种时长`。续费不会吞掉剩余时间。
* **额度只升不降**：若用户已持有更高档的卡且未到期，`member_plan` 保留原卡种（月卡/季卡同为 300，日卡 200 低于它们）。避免"买了张日卡反而把年卡的 500 降到 200"。
* `days` 到 `member_expires_at` 的换算用北京时区，与额度口径一致。

> 🔴 **必须给会员单开字段（`member_plan` + `member_expires_at`），不能复用 `expires_at`。**
>
> `expires_at` 是**账号有效期**，`user_is_expired()`（`src/auth.py:276`）和 `authenticate_api_key()`（`src/api/access.py:133`）拿它做 **403 `ACCOUNT_EXPIRED` 硬拒绝**；而 `expires_at IS NULL` 在现有语义里是"**账号永久有效**"（`src/auth.py:279-280` 对空值返回 `False`）。若会员判定读它，则会员一过期不是"降级到免费档"，而是**整个账号被拒**；且按 §6.2 微信用户的 `expires_at` 就该留 `NULL`，那样**每个新用户都会被判成永久会员**。这是本次设计里唯一一个"写错就立刻出事、且出在钱上"的点。

**`credits` 在小程序路径上是第二条扣减线**：每日额度用尽后由 `reserve_user_credit()` 接着扣，失败按层退回（§1.6）。因此**不需要**为小程序路径新写一套余额逻辑 —— 直接复用 `reserve_user_credit()` 现成的三态（`True` 已扣 / `False` 无需扣 / `None` 不足），它本来就是为按次计费写的，语义正好对得上。对既有 Web 客户它仍是原有的按次计费付费墙（走 API Key 路径，行为不变）。

> 🔄 **2026-09-22 二次修订**：本段原先写的是「`credits` 在小程序路径上完全不参与判定：既不预扣也不退款」。那是单层额度模型下的正确结论 —— 当时的 `credits` 只是运营补偿包，接进判定反而会让"免费 10 次"变成"免费 20 次"。两层次数模型明确接受这个代价（§1.6 有警告），换取签到真正有意义，故本段改写。

> ⚠️ **这带来一个必须同时处理的陷阱**：`access.py:135` 现在有一道「`credits <= 0` → 402」的前置检查。按 §6.2 给微信用户 `credits = 0`，那么**每个免费用户一进请求就会被 402 拦死**，每日额度逻辑根本没机会跑。所以令牌路径上这道检查必须**换成"今日额度是否用尽"**，而不是叠加。

### 2.5 `/api/v1/parse` 改为双路鉴别

```
X-WX-Token 有效      → 小程序用户，走 §2.4 每日额度 + 记录 user_id
否则 Authorization   → 既有 API Key 路径，行为完全不变（credits 付费墙照旧）
两者都没有           → 维持既有的未鉴权响应
```

两条路**并存**而不是替换，因此其他客户端与微服务模式（`API_ONLY=true`）的表现不受影响，改造可以灰度验证。

令牌路径必须**照抄密钥路径的四道检查**，缺一道就会出现"后台管不住"的漏洞：

| 检查 | 密钥路径的出处 | 漏掉会怎样 |
| :--- | :--- | :--- |
| `active = 1`（用户与账号都未停用） | `access.py:131` | 后台点"停用"对微信用户**无效** |
| 账号未过期（`user_is_expired`） | `access.py:133` | 同上，账号到期形同虚设 |
| ~~余额耗尽前置判定~~ → **换成每日额度判定** | `access.py:135` | 直接照抄会把所有免费用户 402 拦死（§2.4 的陷阱） |
| 限流主体 `user:{user_id}` | `access.py:137` | 见下 |

限流主体**必须逐字复用 `f"user:{user_id}"`**：`SQLiteRateLimiter` 是按 subject 字符串分桶的（`access.py:33`），令牌路径若另起一个 `wx:{user_id}` 之类的名字，同一个用户就会拿到**两个互不相干的桶 = QPS 额度翻倍**。而且这样一来，将来用户既有令牌又有密钥时，两条通道共享同一个桶 —— 这正是"密钥共享用户额度与并发"该有的样子。

**两条路径的额度规则不同，这是有意的**：令牌路径吃**每日额度**（先用，用尽再落 `credits` 余额，见 §1.6），密钥路径**直接**吃 `credits`（按次扣，跳过每日额度）。若运营给某个微信用户也开了一把密钥，他会发现"小程序里先走每日免费额度、用密钥调则直接扣余额" —— 这是已知的不对称，本期接受，因为两条通道服务的是完全不同的使用形态（小程序 vs 程序化对接）。

> ⚠️ **删除小程序包内 API_KEY 是最后一步**，切换顺序见 §6.3。顺序颠倒会导致小程序解析全线 401。

§6 完整说明新的账号体系与现有 API Key 体系之间的关系 —— 这是本次改造真正要动的地方。

---

## 3. 客户端改造

### 3.1 新增 `utils/auth.js`

| 导出 | 职责 |
| :--- | :--- |
| `ensureLogin()` | 有有效 token 直接返回；否则 `wx.login` → `POST /wx/login` → 存 token |
| `request(options)` | 统一请求层：自动带 `X-WX-Token`；收到 401 时静默重登并**只重试一次** |
| `clearToken()` | 仅在明确失效时调用 |

两条必须守住的行为：

1. **"只重试一次"是硬约束**：401 → 重登 → 重试；再 401 就向上抛错。否则一旦服务端配置错误，客户端会陷入无限重登循环，把 `code2Session` 额度打爆。
2. **并发只登录一次**：`ensureLogin()` 在模块内缓存 in-flight 的 Promise，后来者复用它。这不是理论问题 —— `pages/index` 的 `onShow`（刷新额度）与用户点击解析（`startParse`）完全可能同时触发，天真实现会并发发起两次 `wx.login`，而后一个 `code` 会顶掉前一个。

### 3.2 额度与统计改造

| 文件 | 变更 |
| :--- | :--- |
| `utils/quota.js` | `getQuota()` 保留为**同步读缓存**（供首帧直接渲染），新增 `fetchQuota()` 异步取服务端快照并写回缓存；`consumeQuota()` / `addBalance()` 删除（额度与签到奖励都归服务端） |
| `utils/member.js` | 本地假会员状态**整体删除**；会员信息改为 `/wx/me` 返回值。**「不限量」这个概念一并删除** —— 新模型没有不限量，只有每日额度 |
| `utils/stats.js` | 降级为展示层：不再本地累加，数据由 `/wx/me` 提供 |
| `config.js` | `FREE_DAILY_QUOTA` / `ENFORCE_QUOTA` 失去意义（额度判定在服务端），改为仅作为服务端不可达时的**显示兜底**或直接删除 |

渲染策略：**先用缓存渲染一帧，再用服务端结果覆盖**。这样页面不会为了等网络而空着，同时用户看到的数字最终一定以服务端为准。

### 3.3 页面调整

| 页面 | 变更 |
| :--- | :--- |
| `pages/index` | `onShow` 改为 `await fetchQuota()`；`startParse()` **不再本地判额度**，把 `402 DAILY_QUOTA_EXCEEDED` 交给既有的签到引导弹窗处理（文案改为"今日额度已用完"） |
| `pages/my` | 异步刷新；签到改为调 `POST /wx/checkin`；新增「完善资料」入口（`chooseAvatar` + `<input type="nickname">`）；新增**会员卡展示区**（卡种、剩余天数、每日额度）；`ensureLocalId()` 删除，用户号显示服务端 `id` |
| `pages/member` | 沿用现有页面，改为展示服务端下发的四档卡（名称 + 每日额度 + 时长）。本期**只展示、不可购买**（支付在第二期） |
| `pages/result` | **不动** |

### 3.4 铁律

> **客户端不再实现任何额度规则，只显示服务端给的数字。**

客户端保留的唯一"额度逻辑"是显示格式化。任何形如"今天还能用几次"的计算都必须由服务端算好放进 `/wx/me` 的 `remaining_today` 里。这条守不住，本次改造就白做了 —— 本地能算的额度，本地就能改。

---

## 4. 测试与验收

沿用本项目既有的"先红后绿"纪律与自建断言脚本风格。

### 4.1 服务端 `tests/test_wx_login.py`（新增）

`code2Session` 必须**可注入 stub**（绝不真打微信接口），覆盖：

| 用例 | 断言 |
| :--- | :--- |
| 首次登录 | 建出用户行，返回 token |
| **新用户取值** | `credits = 0`（不是列默认的 100）、`role = 'user'`、`expires_at IS NULL`、`member_plan IS NULL` |
| 二次登录（同 openid） | **复用同一个 `user_id`**，不新建行 |
| token 有效 / 过期 / 伪造 | 分别放行 / 401 `WX_TOKEN_EXPIRED` / 401 `WX_TOKEN_INVALID` |
| **免费档边界** | 免费档额度 N、余额 0：第 N 次放行、第 N+1 次 402 `DAILY_QUOTA_EXCEEDED` |
| **并发不越界** | 同一用户并发发起 `cap + 20` 个请求，**放行数恰好 ≤ cap**（这是 `daily_usage` 预占存在的理由，必须有用例守住） |
| **解析失败退回** | 失败后 `daily_usage.count` 回到原值；成功则不退 |
| **会员档边界** | 日卡用户上限 200 而非免费档的 10；跨日后计数归零 |
| **会员到期降级** | `member_expires_at` 过期后上限立刻回落到免费档；**且不会被判成 403 `ACCOUNT_EXPIRED`** |
| **开卡叠加** | 有效卡未到期时开新卡：到期时间叠加而非覆盖；已持年卡时开日卡**不降级**卡种 |
| **`credits = 0` 不被前置拦死** | `credits = 0` 的免费用户**不会**因 `access.py:135` 那道检查被 402 —— 它该走的是每日额度（§2.4 的陷阱） |
| **两层次序**（🔄 新增） | 额度与余额都充足时，**扣的是每日额度、余额纹丝不动**（顺序写反会静默烧掉余额） |
| **落到第二层**（🔄 新增） | 额度用尽、余额 > 0：请求**放行**且余额 -1，而不是 402 |
| **两层都空**（🔄 新增） | 额度用尽、余额 0：402 `DAILY_QUOTA_EXCEEDED` |
| **按层退回**（🔄 新增） | 扣的是余额而解析失败 → 退回的是**余额**，`daily_usage` 不变；扣的是额度则反之 |
| **签到可花**（🔄 新增） | 签到拿到的 bonus **确实能**在额度用尽后垫一次解析（这是本次修订的目的，必须有用例守住） |
| 账号有效期不误伤会员 | `member_expires_at` 过期**不会**让请求变成 403 `ACCOUNT_EXPIRED`；`expires_at` 过期才会 |
| 管理员不扣费 | `role = 'admin'` 的请求不受额度限制 —— **两层都不扣**：额度不记账（`cap` 为 `None`），余额也不动 |
| **限流主体复用** | 令牌路径与密钥路径命中**同一个** `user:{id}` 桶：令牌打满后密钥立刻 429，反之亦然 |
| 后台停用生效 | 把微信用户 `active` 置 0 后，**令牌路径立即 403**（不是 200） |
| 重复签到 | 409 `ALREADY_CHECKED_IN`，且余额不变 |
| 跨日签到 | 隔天可签、`streak` 递增；断签后 `streak` 归 1 |
| 头像转存往返 | 存 `data/avatars/<id>.jpg`，`GET /wx/avatar/<id>` 能取回同字节 |
| 缺 `WX_APPID` / `WX_APPSECRET` | **503 `WX_NOT_CONFIGURED`**，不是 500 |
| 迁移幂等 | 在已有旧库上跑两次 `init_db()`，列不重复、不报错 |
| 卡种播种幂等 | 已有 `member_plans` 行时 `init_db()` 不覆盖运营改过的额度 |

### 4.2 客户端 `scripts/test-auth.js`（新增）

沿用既有的 `[ok]` / `[FAIL]` + 退出码约定，stub 掉 `wx.login` 与 `wx.request`：

| 用例 | 断言 |
| :--- | :--- |
| 已有有效 token | **不调用** `wx.login` |
| 401 → 重登 | 重试**只发生一次**，总请求数 ≤ 2 |
| 并发调用 `ensureLogin()` | **只触发一次** `wx.login`（对应 §3.1 的真实风险） |
| 额度渲染 | 先渲染缓存值、再渲染服务端值，共两帧 |
| 402 分流 | `DAILY_QUOTA_EXCEEDED` 弹"今日额度已用完"引导；**不弹**"余额不足"（两个码不能混） |

### 4.3 `scripts/verify-theme-c.js` 同步更新

该脚本是本项目的活体记录，按源码文本断言。`utils/quota.js` 由同步转异步、以及**「不限量」分支的删除**会打到其中的额度文案断言 —— **先让它红，再改**，并在断言旁写下变更理由，保持这份记录的可信度。

### 4.4 回归

服务端全量 `pytest` 必须全绿，重点看 `/api/v1/parse` 的扣费路径未被双路鉴别改坏 —— **API Key 路径的 credits 付费墙行为必须与改造前逐字一致**。

---

## 5. 分期与待核实项

### 5.1 第一期（本次实施）

* 微信静默登录、资料、签到、头像转存
* **会员模型**：`member_plans` 四档卡 + `is_member()` + 开卡/续期函数 + 运营手动开卡 UI
* **每日额度**：`daily_usage` 原子预占 + 三档上限（免费档 / 卡档）
* 额度服务端化，客户端退化为展示层
* `/api/v1/parse` 双路鉴权、令牌路径复刻四道检查并复用限流主体
* `wx_orders` 建空表、`session_key` 加密落库
* **新注册不设试用期**：后台把 `default_trial_days` 设为 0、`default_initial_credits` 设为 0（§6.5）
* **关掉门户的用户自助建密钥入口**（§6.2）
* 切换完成后停用出过包的管理员密钥（§6.3）

### 5.2 第二期（虚拟支付，本次不做）

`wx.requestVirtualPayment`、`paySig` / `signature` 双签名、发货回调幂等入库（写 `wx_orders` → 调开卡函数）、`/xpay/query_order` 轮询对账、iOS 侧 Apple IAP 配置、以及把 `member_plans` 的四档卡映射到微信侧已注册的 `product_id`。第一期的 `session_key` 留存、`wx_orders` 建表、`member_plans.wx_product_id` 列、以及开卡函数就是为这里准备的前提条件。

### 5.3 已暂定的假设（第二期前必须回头验证）

以下几项**没有查证过**，本期按最省事的假设推进。它们不影响第一期的实施，但第二期动手前必须逐条落实。

| 假设 | 若假设为真 | 若假设为假 |
| :--- | :--- | :--- |
| **虚拟支付支持"时长卡"形态的商品**（2026-09-22 决定：暂按支持推进） | 第二期只需把四档卡映射到微信侧已注册的 `product_id` —— `member_plans.wx_product_id` 这一列已经留好，**零改动** | 第二期要改成"卖次数包"，由服务端把一次购买换算成卡的时长/额度。影响面**仅限第二期**：`member_plans` 的播种数据与发货回调的换算逻辑 —— 第一期一行都不用改，这正是把卡种做成数据表而非代码常量的收益 |
| 个人主体满足虚拟支付的「认证」条件 | 第二期可正常接入 | 虚拟支付这条路走不通，需要换支付渠道或改主体 —— 但**会员模型本身不受影响**，运营手动开卡（§6.4）仍然可用 |
| `getPhoneNumber` 对个人主体开放 | 可作为可选的联系方式补全项 | 本设计不依赖它，无需改动 |

> 本文档中关于虚拟支付的费率、结算周期、生效日期等数字来自二手资料（微信官方文档在撰写时无法直接访问），**仅作方向参考**，实施第二期前必须以后台实际显示为准。

---

## 6. 与现有 API Key 体系的关系

这一节回答一个具体问题：现有系统里「密钥挂在用户下、共享用户的额度与并发」，而小程序现在配的是**管理员的**密钥 —— 这次改造到底要动什么。

### 6.1 现状核查：那把管理员密钥实际做了什么

以下结论均已核对到代码行，不是推测：

| 事实 | 出处 |
| :--- | :--- |
| 密钥**不持有**额度：`api_keys` 只有 `user_id / name / key / active / qps_limit`，余额与账号有效期都在 `users` 行 | `src/db.py:22-31` |
| 因此**同一用户下的所有密钥共享**该用户的余额与账号有效期（鉴权时 JOIN `users`） | `src/api/access.py:115` |
| 并发限制是两层：`user:{user_id}` **必然**生效，密钥自己的 `qps_limit` 非空时**再加一层** | `src/api/access.py:137-139` |
| 所以密钥之间**共享用户那一层的桶**，密钥级限制只是额外收紧 | 同上 |
| 管理员**完全不扣积分**：鉴权层对 `role == 'admin'` 跳过余额耗尽判定 | `src/api/access.py:135` |
| `reserve_user_credit()` 对 admin 直接返回 `False`（不扣） | `src/db.py` |
| 管理员 QPS = **20**，`/setup` 时硬编码，**不取** `default_user_qps` | `src/auth.py:131` |

> 📌 **更正 §0.1 的一处错误**：配管理员密钥时，小程序并**没有**在消耗站点主账号的积分 —— 管理员根本不扣费。真实代价是另外三件事：
>
> 1. **全体用户共享同一个 20 QPS 桶** —— 一个人猛刷，所有人一起吃 429。
> 2. **所有解析都记在管理员名下**（`request_logs.user_id` / `api_key_id` 全是它），后台只能看到一个巨大的"管理员"用户，看不到真实用户，也无从按用户封禁或统计。
> 3. 密钥明文躺在包里（`miniprogram/config.js:11`），拿到包的人**无限量、零成本**白用。
>
> 换句话说：客户端那套"每日额度"是**唯一**的限制，而它改一下 storage 就没了。

### 6.2 新体系下的映射（含密钥的去留）

**每个微信用户 = 一行 `users`**：`role = 'user'`（**绝不能是 admin** —— 那正是当前问题的根因）、`openid` 非空、自己的 `qps_limit`、自己的每日额度；`credits = 0`、`expires_at = NULL`、`member_plan = NULL`（即"非会员的普通用户"）。

> ⚠️ 建行时必须**显式写 `credits = 0`**：`users.credits` 的列默认值是 **100**（`src/db.py:19`），`INSERT` 里省略就等于赠送 100 次。而 openid 可以批量获取 —— 省略这一项就是送刷。
>
> 🔄 **2026-09-22 二次修订**：这一项比初版**更要紧**了。初版里 `credits` 只作展示，漏写最坏是"运营补偿的起点多了 100"；两层次数模型下它是**额度的第二层**（§1.6），漏写等于**每个新用户白送 100 次解析** —— 从"数字难看"升级成"直接的钱"。建行语句必须逐字带上 `credits = 0`。

**API Key 的去留**：一个用户多把密钥的实际收益只有三条，且全部以「用户会自己写代码对接」为前提 —— ①某个集成跑飞了可以单独禁用、单独限速；②`request_logs.api_key_id` 能归因到具体项目；③轮换密钥时新旧并存不中断。

在本项目里这三条**全不成立**：用户不知道密钥存在、门户将来不对用户开放、小程序走 `X-WX-Token` 而非密钥。所以本期决定：

> 🔄 **2026-09-22 三次修订（下方四条已被推翻）**：设计时定的是「**保留** `Authorization` 鉴权路径」，落地时改为「**服务端认证一并停用**」—— 密钥通道整体撤销，`api_keys` 表本身仍在（历史日志归属）。
>
> 推翻的理由：`/register` 是**开放注册**且注册即送 100 积分 + 365 天，与「自助建密钥」相乘 = 任何陌生人免费解析通道（推演见 `docs/api.md` §1.1.1）。设计时把「密钥此后只服务运营自用与白名单客户」当作前提，但代码里并没有任何东西**强制**这一点 —— 前提靠人守，而漏洞靠代码生效。
>
> 本节以下四条按原文保留，作为当时的决策记录；**现状以 `docs/api.md` §1.1 为准**。

* **保留 `api_keys` 表与 `Authorization` 鉴权路径** —— `request_logs.api_key_id` 挂在上面，`docs/api.md` 是公开契约，`API_ONLY` 微服务模式也依赖它。删掉是破坏性变更，收益为零。
* **关掉门户里用户自助创建/管理密钥的那套 UI**（`src/web/portal.py:206` 起，含每用户最多 10 把的上限）。门户不对用户开放后它没有消费者，留着反而是一处无人维护的授权入口。
* **不再为每个用户自动建密钥**。特别是不给微信用户建：令牌 30 天到期、可集中吊销；而密钥是**永久** bearer 凭证，给每个用户建一把等于凭空多一条永不失效、而用户自己都看不到的通道。
* 密钥此后实际只服务两类用途：**运营/管理员自用**，以及**白名单客户的程序化对接**。

**封禁**走 `active = 0`，它是唯一能同时拦住网页登录（`src/auth.py:215`）与接口鉴权（`src/api/access.py:131`）的开关。

### 6.3 切换与密钥下线顺序

按序执行；**第 3、4 步颠倒会让小程序解析全线 401**：

> 🔄 **2026-09-22 三次修订**：本表已执行完毕。第 3 步（删包内 `API_KEY`）已完成（`miniprogram/config.js` 已无该字段，并由 `scripts/test-parser.js` 加了回归守卫）；第 4 步**已无对象** —— 密钥通道整体撤销后，那把管理员密钥连同所有密钥一样不再被任何代码读取。下表按原文保留。

| 步 | 动作 | 怎么确认可以进下一步 |
| :---: | :--- | :--- |
| 1 | 上线令牌路径（双路并存），小程序改用 token | `pytest` 全绿 + 真机解析成功 |
| 2 | 观察请求日志 | 出现真实微信用户 id 的记录，且这些记录 `api_key_id IS NULL` |
| 3 | 小程序包内**删除** `API_KEY`（`miniprogram/config.js:11`） | 真机再现一次解析成功 |
| 4 | 后台**停用**那把管理员密钥 | 停用后日志不再出现该密钥的请求 |

第 4 步想稳妥一点，可以**先把它密钥级的 `qps_limit` 设成 1 当哨兵** —— 万一还有老客户端在用，会立刻在日志里冒出来，而不是等你发现时才 401。

之后建议**直接删除**这把密钥：它已经出过包，不再算秘密。

### 6.4 后台需要的改动

| 改动 | 必要性 | 说明 |
| :--- | :--- | :--- |
| **开卡入口** | 第一期必须 | 运营手动给用户开卡：选卡种 → 调 §2.4 的开卡函数（到期时间叠加、额度只升不降）。没有它，会员只能靠手写 SQL 发放 |
| **卡片管理** | 第一期建议 | `member_plans` 的每日额度/时长/上下架可编辑。有了它，调价调额度不用改代码 |
| 用户列表加「来源」列与筛选 | 建议 | `openid IS NOT NULL` 即微信用户。现有批量操作已支持按筛选条件批改 QPS / 充值 / 停用（`src/web/admin.py:359` 一带），加上筛选后微信用户就能被批量运营 |
| 请求日志「密钥」列 | 建议 | 微信用户的 `api_key_id` 为空，建议显示成「微信」，免得运维以为日志缺字段 |
| **拆开「账号有效期」与「会员到期」** | 第二期前必须 | 见下 |

> ⚠️ 用户编辑页现在那个「到期时间」只写 `expires_at`（账号有效期）。引入会员后这个字段就有了歧义 —— 运营想给某人开一个月会员，填进去的实际是"账号有效期"，结果是**给了他一个永久有效的账号却仍不是会员**，而且不给任何报错。必须拆成两个输入框。

### 6.5 顺带：新注册不设试用期

这项**不需要改代码**，后台已经支持：

* `admin.py:126` 判断 `chk_unlimited_trial == "1"` 或 `default_trial_days == "0"` 时写 `set_setting("default_trial_days", 0)`；
* `auth.py:162-164` 在 `trial_days = 0` 时**不写 `expires_at`**（保持 NULL）→ `user_is_expired()` 对空值返回 `False` → 账号永久有效；
* 配套把 `default_initial_credits` 从 100 改成 0（`db.py:113` 的默认值），否则新用户仍白拿 100 次。

执行方式：**在后台设置页勾选"不限试用期"、把初始积分填 0**。代码零改动。

---

## 7. 变更影响面小结

| 影响 | 说明 |
| :--- | :--- |
| 现有 Web 用户 | **无影响**。新增列全部可空；`openid` 用部分唯一索引；API Key 路径与 credits 付费墙行为不变 |
| 会员语义 | `credits = -1` 的旧「不限次」语义**不再用于表达会员**，会员改为 `member_plan` + `member_expires_at` + 每日额度。既有 `credits = -1` 的 Web 客户行为不变（他们走密钥路径） |
| `API_ONLY=true` 模式 | 需确认生产是否启用。若启用，`/api/v1/parse` 当前免鉴权，双路鉴别改造需同步考虑该模式下的行为 |
| 数据库 | 加 7 列 + 4 表 + 1 索引，全部 `IF NOT EXISTS` 语义，可重复执行 |
| 小程序包 | 切换顺序见 §6.3：先删包内 API_KEY，**再**停用管理员密钥 |
| 那把出过包的管理员密钥 | 停用（建议删除）。它已不是秘密，且它让全站共享一个 20 QPS 桶 —— §6.1 / §6.3 |
| 运营后台 | 获得小程序用户与会员管理能力；需要按 §6.4 补开卡入口，并在第二期前拆开两个到期字段 |
| 客户端 | 「不限量」展示分支删除；额度文案由同步改异步，`scripts/verify-theme-c.js` 的断言需先红后改 |
