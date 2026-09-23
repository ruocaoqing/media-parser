import base64
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

    def test_missing_user_id_is_allowed_and_not_counted(self):
        """非微信 token 路径没有用户，照样放行，且一个字都不往 daily_usage 写。

        这是 consume_daily_quota 文档里写明的契约（user_id 为假值 = 跳过记账）。
        """
        day = "2026-09-22"
        with self.app.app_context():
            for _ in range(5):
                # cap=1：真有额度约束的话第二次就该 False，这里必须一路 True
                self.assertTrue(consume_daily_quota(None, day, 1))
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
            # timeout 是必须的：线程没起来时 BrokenBarrierError 让用例响亮失败，
            # 否则整轮测试会挂死到外部超时。
            barrier.wait(timeout=10)
            with self.app.app_context():
                return consume_daily_quota(self.user_id, day, cap)

        with ThreadPoolExecutor(max_workers=attempts) as pool:
            results = list(pool.map(lambda _index: worker(), range(attempts)))

        self.assertEqual(sum(1 for granted in results if granted), cap)
        with self.app.app_context():
            self.assertEqual(get_daily_usage(self.user_id, day), cap)


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
                "INSERT INTO users(username,password_hash,role,active,credits,openid,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("wx_alice", "x", "user", 1, 0, "openid-alice", utcnow()),
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
            # 原「开卡绝不碰账号有效期」断言（`expires_at IS NULL`）随 users.expires_at 一并删除：
            # 那个字段已经不存在，开卡路径无从碰它。会员与账号开关的隔离现在由
            # `is_member` 只看 member_expires_at 这一条保证（src/db.py:355-366）。
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
            # 不能用 .days > 29：timedelta.days 向下取整，而存下来的到期时间是
            # 「实现里的 now + 30 天」，这里的 now 必然更晚，差值恒为「30 天减一点点」，
            # 于是 .days 恒等于 29，写 > 29 永远不成立（去掉秒级截断也一样）。
            # 改用与上一条用例相同的 delta 比较：仍然抓得住「从过期时间续」的实现
            # （那样只剩 27 天左右，会被 delta=1 判负）。
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

    def test_expired_member_falls_back_to_the_free_tier(self):
        """设计文档 §2.4 的 🔴：会员过期只该降级到免费档，绝不能变成 403 ACCOUNT_EXPIRED。

        原用例名尾的 `without_touching_expires_at` 与其中那条 `expires_at IS NULL` 断言
        随 users.expires_at 删除 —— 账号层面已无「过期」这个状态，会员过期必然是
        「降级」而不是「拒绝」，这正是本条断言钉住的事。
        """
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE users SET member_plan='year', member_expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"), self.user_id),
            )
            db.commit()
            user = self.user_row()
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

    def test_retired_current_plan_does_not_block_the_purchased_card(self):
        """已下架的旧卡种不能靠「额度只升不降」把用户锁在免费档上。

        旧卡种 active=0 时它一分钱额度都兑现不了（is_member 走 get_member_plan 会返回 None），
        此时「保留旧卡种」严格劣于换成刚买的卡。
        """
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE users SET member_plan='year', member_expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds"), self.user_id),
            )
            db.execute("UPDATE member_plans SET active=0 WHERE code='year'")
            db.commit()
            ok, message, _expires = open_member_card(self.user_id, "day")
            self.assertTrue(ok, message)
            user = self.user_row()
            self.assertEqual(user["member_plan"], "day")
            self.assertEqual(daily_quota_cap(user), 200)


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

    def test_tampered_tag_is_rejected(self):
        """GCM 的认证标签必须真的被校验。

        已有的篡改用例只改密文尾部（raw[33..35]），碰不到 tag（raw[12..27]）：
        把 decrypt_and_verify 换成 decrypt 后它仍有约 84% 的概率通过（靠
        UnicodeDecodeError，而它也是 ValueError 的子类）。这里只翻转 tag 的第一个
        字节 —— 校验存在就必须抛 ValueError，不校验就会原样返回明文。
        """
        blob = encrypt_session_key("sk-alice", "secret-a")
        raw = bytearray(base64.b64decode(blob))
        raw[12] ^= 0x01  # nonce 是 12 字节，紧随其后的 16 字节就是 tag
        broken = base64.b64encode(bytes(raw)).decode("ascii")
        with self.assertRaises(ValueError):
            decrypt_session_key(broken, "secret-a")


from unittest.mock import Mock, patch


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
        """没带 code 时必须在**本地**就拒掉，绝不能拿空 code 去打微信。

        只断言 400 + WX_CODE_INVALID 是不够的：把 `if not code` 那段守卫删掉后，
        code2session 被 patch 成返回 {} 时照样走「没有 openid」的路径，同样吐
        400 WX_CODE_INVALID —— 断言恒真。assert_not_called() 才钉住守卫真正的
        可观测效果：这种请求根本没走到微信那一步。
        """
        with patch("src.api.wx.code2session", return_value={}) as fake:
            response = self.client.post("/api/v1/wx/login", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error_code"], "WX_CODE_INVALID")
        fake.assert_not_called()

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

    # 2026-09-22 删除 `test_expired_account_cannot_log_in`：`/wx/login` 曾与网页登录
    # **故意不同**地多查一道 `expires_at`（§2.3）。该字段废弃后小程序登录只剩「停用」一条前置，
    # 就是上面那条 `test_disabled_account_cannot_log_in`。


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

    def test_member_plans_endpoint_lists_the_four_cards(self):
        response = self.client.get("/api/v1/wx/member-plans")
        self.assertEqual(response.status_code, 200)
        plans = response.get_json()["data"]["plans"]
        self.assertEqual([p["code"] for p in plans], ["day", "month", "quarter", "year"])
        self.assertEqual(plans[0]["daily_quota"], 200)


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

    def login_again(self, code="code-second"):
        """再走一次登录，拿到同一用户的**另一枚**令牌（openid 不变，会话行新增一行）。"""
        with patch("src.api.wx.code2session",
                   return_value={"openid": "openid-alice", "session_key": "sk-alice"}):
            return self.client.post("/api/v1/wx/login", json={"code": code}).get_json()["data"]["token"]

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
            # 本用例测的是每日额度，不是限流：把 QPS 放开，别让第 3 次请求先撞上 429
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

    # ---- 两层次数：每日额度 + 签到余额 ----
    #
    # credits 从"仅展示的补偿包"改成可花的第二层。地基本来就在（_user_snapshot 一直
    # 在返回 balance、reserve_user_credit 一直是原子的），缺的只是额度用尽后回退到
    # 余额这一步。下面五个用例钉住的是：顺序、回退、退款归属、签到真的能花、管理员不受影响。

    def set_balance(self, amount):
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET credits=? WHERE id=?", (amount, self.user_id))
            db.commit()

    def balance(self):
        with self.app.app_context():
            return get_db().execute(
                "SELECT credits FROM users WHERE id=?", (self.user_id,)
            ).fetchone()["credits"]

    def free_tier(self, quota, balance, **settings):
        """把免费档额度、余额、QPS 一次配好 —— 本组用例测的都是额度，不是限流。

        `set_setting` 本身不 commit（它只 execute），所以这里的 commit 必须留在
        同一次 app_context 里，否则设置会被丢掉、用例读到的是建库种子值。
        """
        self.set_balance(balance)
        with self.app.app_context():
            set_setting("wx_free_daily_quota", quota)
            for key, value in settings.items():
                set_setting(key, value)
            db = get_db()
            db.execute("UPDATE users SET qps_limit=50 WHERE id=?", (self.user_id,))
            db.commit()

    def test_daily_quota_is_spent_before_the_balance(self):
        """顺序：先用会重置的每日额度，把不过期的余额留作保底。"""
        self.free_tier(quota=2, balance=5)
        self.assertEqual(self.parse(self.token).status_code, 200)
        self.assertEqual(self.parse(self.token).status_code, 200)
        self.assertEqual(self.used_today(), 2)
        self.assertEqual(self.balance(), 5, "每日额度还没用尽，就不该动余额")

    def test_balance_is_spent_after_the_daily_quota_runs_out(self):
        """额度用尽后回退到余额；两层都空了才 402。"""
        self.free_tier(quota=1, balance=2)
        self.assertEqual(self.parse(self.token).status_code, 200, "第 1 次吃每日额度")
        self.assertEqual(self.parse(self.token).status_code, 200, "第 2 次吃余额 2→1")
        self.assertEqual(self.parse(self.token).status_code, 200, "第 3 次吃余额 1→0")
        self.assertEqual(self.used_today(), 1)
        self.assertEqual(self.balance(), 0)
        response = self.parse(self.token)
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.get_json()["error_code"], "DAILY_QUOTA_EXCEEDED")
        self.assertEqual(self.balance(), 0, "402 之后余额不能被扣成负数")

    def test_failed_parse_refunds_the_balance_tier(self):
        """失败退回到**扣的那一层** —— 这次扣的是余额，就不能去退每日额度。"""
        self.free_tier(quota=1, balance=2)
        self.assertEqual(self.parse(self.token).status_code, 200, "先把每日额度用掉")
        with patch("src.api.parse.WebFetcher.fetch_redirect_url",
                   return_value="https://www.douyin.com/video/123"):
            with patch("src.api.parse.ParserFactory.create_parser",
                       side_effect=RuntimeError("解析炸了")):
                response = self.client.get("/api/v1/parse?url=https://example.com/share",
                                           headers={"X-WX-Token": self.token})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.balance(), 2, "失败的这一次必须把余额退回来")
        self.assertEqual(self.used_today(), 1, "每日额度那一层不该被动到")

    def test_checkin_bonus_becomes_spendable_balance(self):
        """签到送的积分真的能花 —— 这正是把积分接成余额的目的。"""
        self.free_tier(quota=1, balance=0, wx_checkin_bonus=3)
        response = self.client.post("/api/v1/wx/checkin", headers={"X-WX-Token": self.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["balance"], 3)
        self.assertEqual(self.parse(self.token).status_code, 200, "第 1 次吃每日额度")
        for i in range(3):
            self.assertEqual(self.parse(self.token).status_code, 200, f"签到余额第 {i + 1} 次")
        self.assertEqual(self.balance(), 0)
        response = self.parse(self.token)
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.get_json()["error_code"], "DAILY_QUOTA_EXCEEDED")

    def test_admin_is_unlimited_on_both_tiers(self):
        """管理员 cap 为 None：每日额度直接放行，且永远不该走到扣余额那一步。"""
        self.free_tier(quota=1, balance=0)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET role='admin' WHERE id=?", (self.user_id,))
            db.commit()
        for _ in range(3):
            self.assertEqual(self.parse(self.token).status_code, 200)
        self.assertEqual(self.used_today(), 0, "管理员不记账")
        self.assertEqual(self.balance(), 0, "管理员不吃余额")

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

    def test_daily_quota_exhausted_falls_back_to_credits(self):
        """2026-09-22 修订：本条原名 `..._does_not_touch_credits`，断言"额度用尽
        硬停到次日、不扣余额" —— 那是旧模型（credits 仅作展示的补偿包）的契约。
        新模型把 credits 接成了第二层，所以额度用尽后**应该**扣余额，且仍然放行。
        旧断言不是回归，是被取代；留着这条改写，让这次翻案在文件里可见。
        """
        with self.app.app_context():
            db = get_db()
            set_setting("wx_free_daily_quota", 1)
            db.execute("UPDATE users SET credits=5 WHERE id=?", (self.user_id,))
            db.commit()
        self.assertEqual(self.parse(self.token).status_code, 200, "第 1 次吃每日额度")
        self.assertEqual(self.parse(self.token).status_code, 200, "第 2 次回退到余额")
        with self.app.app_context():
            user = get_db().execute("SELECT credits FROM users WHERE id=?", (self.user_id,)).fetchone()
            self.assertEqual(user["credits"], 4)

    def test_disabled_account_is_403_not_200(self):
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET active=0 WHERE id=?", (self.user_id,))
            db.commit()
        response = self.parse(self.token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error_code"], "ACCOUNT_DISABLED")

    # 2026-09-22 删除 `test_expired_account_is_403`：`ACCOUNT_EXPIRED` 这个错误码
    # 随 users.expires_at 一并消失，令牌路径的 403 只剩上面那条「停用」。

    def test_unknown_token_is_401(self):
        response = self.parse("bogus-token")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "WX_TOKEN_INVALID")

    def test_admin_token_is_not_quota_limited(self):
        with self.app.app_context():
            db = get_db()
            set_setting("wx_free_daily_quota", 1)
            # 本用例测的是每日额度，不是限流：把 QPS 放开，别让第 3 次请求先撞上 429
            db.execute("UPDATE users SET qps_limit=50 WHERE id=?", (self.user_id,))
            db.execute("UPDATE users SET role='admin' WHERE id=?", (self.user_id,))
            db.commit()
        for _ in range(3):
            self.assertEqual(self.parse(self.token).status_code, 200)
        self.assertEqual(self.used_today(), 0)

    def test_a_second_login_does_not_reset_the_rate_limit_bucket(self):
        """§2.5 的延续：限流主体是 `user:{id}`，**不是**令牌。

        原用例叫 `test_token_and_api_key_share_one_rate_limit_bucket`，比的是
        「令牌腿 vs 密钥腿」两条凭证共用同一个桶。密钥撤销后没有第二种凭证了，
        但**同一个用户可以同时持有多枚令牌** —— 每次 `wx.login()` 都会新发一枚
        （见 `test_each_login_issues_a_new_session_row`）。若把主体名写成 `token:{hash}`，
        用户只要重登一次就白拿一份 QPS 配额，同一个洞原样还在。
        所以本用例换成「两枚令牌、同一个用户」，测的还是那件事。
        """
        # 限流桶是 1 秒定宽窗口，本用例要求几次请求落进**同一个**桶 —— 不冻结时间的话，
        # 秒边界正好切在中间就会把期望的 429 变成 200。
        with patch("src.api.access.time.time", return_value=123456):
            with self.app.app_context():
                db = get_db()
                db.execute("UPDATE users SET qps_limit=2 WHERE id=?", (self.user_id,))
                db.commit()

            self.assertEqual(self.parse(self.token).status_code, 200)
            self.assertEqual(self.parse(self.token).status_code, 200)
            self.assertEqual(self.parse(self.token).status_code, 429, "同一枚令牌的第 3 次该被限流")

            fresh_token = self.login_again()
            # 这半截是**承重**的：没有它，上面的 429 用「令牌桶」也能解释得通，
            # 本用例就退化成了「限流生效」的同义反复。
            self.assertNotEqual(fresh_token, self.token, "重登必须换一枚新令牌，否则下面那条断言是空的")
            self.assertEqual(
                self.parse(fresh_token).status_code, 429,
                "换了新令牌仍该 429 —— 桶按用户算；若这里变成 200，说明主体名退化成按令牌计",
            )

    # 2026-09-22 删除：`test_api_key_path_still_enforces_the_credits_paywall`。
    # 它断言「密钥路径的 credits 付费墙与改造前逐字一致」，而密钥路径本身没了。
    # 「额度耗尽 → 402」这件事在本文档下面那一组两层用例里测得更细
    #（顺序 / 回退 / 按层退款 / 两层都空才 402），错误码从 INSUFFICIENT_CREDITS
    # 换成 DAILY_QUOTA_EXCEEDED —— 因为在新的两层模型里，密钥路径特有的
    # 「只有积分一条线」已经不存在了。

    def test_no_credentials_is_401_token_required(self):
        """**没有** X-WX-Token 头时必须 401。

        2026-09-22：原为 `test_no_credentials_keeps_the_legacy_response`，
        断言的是旧密钥路径的 `API_KEY_REQUIRED`。撤销密钥通道后这一条的份量反而更重：
        parse.py 里那段 `if access is None and not is_api_only: 401` 一旦被删掉，
        /api/v1/parse 就成了完全公开的解析接口 —— 那正是撤销密钥要堵的那个洞。
        错误码单列 REQUIRED 而不是复用 INVALID：「没带凭证」与「凭证是坏的」对调用方是两件事。
        """
        response = self.parse()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "WX_TOKEN_REQUIRED")


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
        # 必须断言守卫真的开了口，不能只看 member_plan 没变：「没变」的成因太多 —— 被
        # admin_required / csrf_protected 拒掉同样是没变。断言 flash 才能把「守卫按预期报错」
        # 与「请求压根没走到守卫」区分开。
        # 反例别拿「删掉守卫」来做：那会让视图抛 TypeError，而 TESTING=True 下 Flask 把它直接
        # 重新抛出，用例在那个 post 调用处就 ERROR，下面几行断言一行都不会执行 —— 那种变异下
        # 红不红与断言本身无关。要验证这条断言，得用「不抛异常但行为错」的变异，例如把
        # open_member_card 的提示文案改掉（已实测：状态码断言照样通过，本行断言变红）。
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

    def test_users_page_renders_the_card_modal_and_plan_options(self):
        # 取代 brief 的手工浏览器验证（本机跑不起 app.py）。没有把 member_plans 传进模板时，
        # 弹窗里的 {% for plan in member_plans %} 会让 Jinja 抛 UndefinedError → 500，
        # 这条是该接线唯一的自动化体检。
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["role"] = "admin"
        response = self.client.get("/admin/users")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("admin-open-card-modal-backdrop", body)
        self.assertIn("年卡", body)


from src.db import record_member_card_grant  # noqa: E402 （beijing_today / utcnow 已在文件上方导入）


class AdminMemberCardsTest(unittest.TestCase):
    """工作流 C：后台看得见「今日用了多少」与「谁买了什么卡」。

    用户的原话是「1 我在后台管理界面 没地方能看到 daily_usage 的数据，也没地方看到用户的
    购买的会员信息，订单信息」—— 这一组就是那句话的验收。

    两件事各自独立：
      * 用户列表补两列（会员卡 / 今日额度），**算**出来的，不新增存储；
      * 开卡记录表 `member_card_grants`，**写**下来的，因为「这一刻开出去的是什么卡」
        从 `users` 的现值里还原不出来（额度只升不降会留下「开的是日卡、生效的是年卡」）。
    """

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
            self.admin_name = db.execute(
                "SELECT username FROM users WHERE id=?", (self.user_id,)
            ).fetchone()["username"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        return "test-csrf"

    def login_admin(self):
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["role"] = "admin"

    def make_customer(self, username):
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,role,active,qps_limit,credits,openid,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (username, "x", "user", 1, 2, 0, username, utcnow()),
            )
            db.commit()
            return cursor.lastrowid

    def grant(self, target_id, plan_code):
        return self.client.post(f"/admin/users/{target_id}/member-card",
                                data={"csrf_token": self.csrf(), "plan_code": plan_code})

    def grants(self):
        with self.app.app_context():
            return get_db().execute(
                "SELECT * FROM member_card_grants ORDER BY id"
            ).fetchall()

    @staticmethod
    def row_html(body, needle):
        """截出 needle 所在的那一行 `<tr>`，供「这一行里长什么样」类断言使用。

        整页断言在这个页面上踩过坑：状态列与行尾「⋯」菜单的启停表单**路由、字段、
        确认文案逐字相同**，拿整页做断言分不出是哪一个在起作用。

        用 `rindex("<tr")` 而不是从 needle 处直接切：needle 若先出现在表格之外
        （比如顶部导航里的当前登录用户名），从那里切出来的「一行」会横跨表头，
        断言就变成了永远为真。传 `>#<id></td>` 这类行内唯一的串最稳妥。
        """
        pos = body.index(needle)
        start = body.rindex("<tr", 0, pos)
        return body[start:body.index("</tr>", pos)]

    @staticmethod
    def select_html(body):
        """截出卡种下拉框那一整段 `<select name="cards_plan">…</select>`。

        卡种名同时会出现在表格正文（开卡记录里的快照名），整页断言分不出
        「筛选项里有没有」和「记录里有没有」。
        """
        start = body.index('<select name="cards_plan"')
        return body[start:body.index("</select>", start)]

    # ---------- 开卡记录表 ----------

    def test_granting_a_card_writes_an_audit_row(self):
        """开卡成功必须留痕 —— 这正是「没地方看到用户的购买的会员信息」的缺口。"""
        self.login_admin()
        target_id = self.make_customer("wx_openid-bob")
        self.grant(target_id, "year")

        rows = self.grants()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["user_id"], target_id)
        self.assertEqual(row["username"], "wx_openid-bob")
        self.assertEqual(row["plan_code"], "year")
        self.assertEqual(row["plan_name"], "年卡")
        self.assertEqual(row["days"], 365)
        self.assertEqual(row["daily_quota"], 500)
        self.assertEqual(row["source"], "admin")
        self.assertEqual(row["granted_by"], self.user_id)
        self.assertEqual(row["granted_by_name"], self.admin_name)
        # 审计行里的到期时间必须与 users 上真正生效的那个**逐字相同**：
        # 两处各算一次时间，就会差出一次 open_member_card 的续期语义。
        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
        self.assertEqual(row["expires_at"], user["member_expires_at"])

    def test_failed_grant_writes_no_row(self):
        """开卡失败不许留痕：记下的每一行都必须对应一张真的开出去的卡。

        这里用的是**已下架**的卡种，不是不存在的卡种。两者路径不同：
        不存在的卡种在 `record_member_card_grant` 里也查不到，写审计的调用就算不给
        `ok` 加守卫，那一行照样写不出来 —— 拿它做用例，守卫拆了也是绿的。
        已下架的卡种则相反：`open_member_card` 带 `active=1` 会拒绝，而审计函数
        **故意不带** `active=1`（见它的 docstring），所以「先写审计、后看 ok」这个顺序
        错误只有在这条路径上才暴露得出来。
        """
        self.login_admin()
        target_id = self.make_customer("wx_openid-bob")
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE member_plans SET active=0 WHERE code='year'")
            db.commit()
        response = self.grant(target_id, "year")
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            flashes = session.get("_flashes") or []
        self.assertIn(("error", "卡种不存在或已下架"), flashes)
        self.assertEqual(self.grants(), [])

    def test_the_audit_row_snapshots_the_plan_at_grant_time(self):
        """改卡种不该改写历史：这一行说的是「当时开出去的是什么」，不是「它现在叫什么」。"""
        self.login_admin()
        target_id = self.make_customer("wx_openid-bob")
        self.grant(target_id, "year")
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE member_plans SET name='包年卡', days=400, daily_quota=999 WHERE code='year'"
            )
            db.commit()
        row = self.grants()[0]
        self.assertEqual(row["plan_name"], "年卡")
        self.assertEqual(row["days"], 365)
        self.assertEqual(row["daily_quota"], 500)

    def test_granting_a_smaller_card_records_what_actually_took_effect(self):
        """额度只升不降：给年卡用户开日卡，开出去的是日卡，生效的仍是年卡。

        只记 plan_code 的话，这一行读起来像「把用户降成了日卡」—— 与事实相反。
        """
        self.login_admin()
        target_id = self.make_customer("wx_openid-bob")
        self.grant(target_id, "year")
        self.grant(target_id, "day")

        rows = self.grants()
        self.assertEqual([r["plan_code"] for r in rows], ["year", "day"])
        self.assertEqual(rows[1]["effective_plan_code"], "year", "生效档位该保留年卡")
        self.assertEqual(rows[0]["effective_plan_code"], "year", "第一张开的就是年卡")

    def test_granting_a_bigger_card_records_it_as_effective(self):
        self.login_admin()
        target_id = self.make_customer("wx_openid-bob")
        self.grant(target_id, "day")
        self.grant(target_id, "year")
        rows = self.grants()
        self.assertEqual(rows[1]["effective_plan_code"], "year")

    def test_record_member_card_grant_gives_up_on_an_unknown_plan(self):
        """写不进去返回 None 而不是抛异常：审计记不上账，不该把已经开出去的卡带翻。"""
        target_id = self.make_customer("wx_openid-bob")
        with self.app.app_context():
            self.assertIsNone(
                record_member_card_grant(target_id, "lifetime", "2030-01-01T00:00:00")
            )
            self.assertIsNone(record_member_card_grant(999999, "year", "2030-01-01T00:00:00"))

    # ---------- 开卡记录页 ----------

    def test_member_cards_page_lists_the_grant(self):
        self.login_admin()
        target_id = self.make_customer("wx_openid-bob")
        self.grant(target_id, "year")

        response = self.client.get("/admin/member-cards")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("wx_openid-bob", body)
        self.assertIn("年卡", body)
        self.assertIn(self.admin_name, body, "开卡人也要能看见")

    def test_member_cards_page_filters_by_plan(self):
        self.login_admin()
        bob = self.make_customer("wx_openid-bob")
        carol = self.make_customer("wx_openid-carol")
        self.grant(bob, "year")
        self.grant(carol, "day")

        body = self.client.get("/admin/member-cards?cards_plan=day").get_data(as_text=True)
        self.assertIn("wx_openid-carol", body)
        # 断言的是用户名而不是「年卡」三个字：卡种名同时出现在筛选下拉的 <option> 里，
        # 拿它做断言等于什么都没测。
        self.assertNotIn("wx_openid-bob", body)

    def test_the_plan_filter_lists_the_live_catalog_before_any_card_is_granted(self):
        """卡种下拉 = 「在架的」∪「记录里出现过的」。

        只用后者（最初的实现）在还没开过任何卡时就是个空框 —— 上线第一天运营打开这一页，
        筛选框里只有「全部卡种」，看着像功能坏了。这就是用户报的「卡种下拉框 目前只有全部卡种」。
        """
        self.login_admin()
        self.assertEqual(self.grants(), [], "这条用例的前提就是记录表还空着")

        body = self.client.get("/admin/member-cards").get_data(as_text=True)
        sel = self.select_html(body)
        for code, name in (("day", "日卡"), ("month", "月卡"),
                           ("quarter", "季卡"), ("year", "年卡")):
            self.assertIn(f'<option value="{code}"', sel, f"在架卡种「{name}」必须能选")
            self.assertIn(f">{name}</option>", sel)

    def test_the_plan_filter_keeps_a_retired_plan_and_prefers_the_live_name(self):
        """两条都不能丢：下架/删掉的卡种靠**记录**留下筛选项，改名后要显示**当前**叫法。

        只用「在架的」会让下架卡种的历史记录永远筛不出来 —— 而那恰恰是最需要对账的时候。
        反过来，同一个 code 两边都有时，得用 `member_plans` 里的当前名称，否则后台改了名字，
        筛选框里还是旧名字，运营对不上。
        """
        self.login_admin()
        bob = self.make_customer("wx_openid-bob")
        self.grant(bob, "year")
        self.grant(bob, "quarter")

        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE member_plans SET name='包年卡' WHERE code='year'")
            db.execute("DELETE FROM member_plans WHERE code='quarter'")
            db.commit()

        sel = self.select_html(self.client.get("/admin/member-cards").get_data(as_text=True))

        self.assertIn("包年卡", sel, "改名后筛选项要用当前名称")
        self.assertNotIn(">年卡</option>", sel, "历史快照名不该盖过当前名称")

        self.assertIn('<option value="quarter"', sel, "记录里出现过的卡种，即使卡种已被删掉也要能筛")
        self.assertIn(">季卡</option>", sel)

    def test_member_cards_page_is_admin_only(self):
        target_id = self.make_customer("wx_openid-bob")
        with self.client.session_transaction() as session:
            session["user_id"] = target_id
        response = self.client.get("/admin/member-cards")
        self.assertIn(response.status_code, (302, 403))
        self.assertNotIn("开卡记录", response.get_data(as_text=True))

    # ---------- 用户列表的两列 ----------

    def test_users_page_shows_todays_usage_against_the_cap(self):
        """三档口径必须各自正确：免费档 / 会员档 / 管理员。

        管理员那一档尤其不能拿免费档数字顶上 —— `access.py` 里 `cap is None` 才是
        「不限」，写成 `0 / 10` 会让运营以为管理员今天只剩 10 次。
        """
        self.login_admin()
        self.make_customer("wx_openid-idle")
        member_id = self.make_customer("wx_openid-member")
        exhausted = self.make_customer("wx_openid-dead")
        self.grant(member_id, "year")

        with self.app.app_context():
            db = get_db()
            day = beijing_today()
            db.execute("INSERT INTO daily_usage(user_id,day,count) VALUES(?,?,?)",
                       (member_id, day, 12))
            db.execute("INSERT INTO daily_usage(user_id,day,count) VALUES(?,?,?)",
                       (exhausted, day, 10))
            db.commit()

        body = self.client.get("/admin/users").get_data(as_text=True)
        self.assertIn("> / 10</span>", body, "免费档：0 / 10")
        self.assertIn("> / 500</span>", body, "年卡：12 / 500")
        self.assertIn(">12</span>", body)
        self.assertIn(">10 / 10</span>", body, "用满的那一行要整格转红")
        self.assertIn(">已满<", body)
        # 下面三条都带上 class 串，不是为了让断言变好看：
        #   「不限」两个字的子串在本页另有出处（批量工具栏的「设为无限制 (不限额)」），
        #   「年卡」在开卡弹窗的 <option> 里也有一份，纯文字断言换一列也能绿。
        # 带上 pill 的 class 才能钉住「这个值出现在这一格」。
        self.assertIn('border-purple-200">不限</span>', body, "管理员是不限，不是某个数字")
        self.assertIn('border-amber-200">年卡</span>', body, "会员卡列")
        self.assertIn('>非会员</span>', body)

    def test_users_page_shows_a_member_but_not_a_lapsed_plan(self):
        """卡种下架后 is_member 为假 —— 会员列必须跟着显示「非会员」，
        否则这里说「年卡」、API 那边却按免费档限流，运营会照着这一列去查为什么客户被拦。"""
        self.login_admin()
        member_id = self.make_customer("wx_openid-member")
        self.grant(member_id, "year")
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE member_plans SET active=0 WHERE code='year'")
            db.commit()

        body = self.client.get("/admin/users").get_data(as_text=True)
        self.assertIn('>非会员</span>', body)
        self.assertNotIn('border-amber-200">年卡</span>', body, "下架的卡种不该再显示成会员")
        self.assertIn("> / 10</span>", body, "判定降回免费档，额度列也要跟着降")

    # ---------- 账号列 / openid 列 / 状态列（2026-09-22 的表头改造） ----------

    def test_users_page_splits_account_from_openid(self):
        """小程序账号的 username 是 `wx_<openid>`，看着像 openid 其实是账号名。

        客服排查要的是**原始 openid**，所以它必须自成一列且可选中复制；
        「客户名称」这个名字也改回「账号」。同时删掉「账号到期」列 ——
        `users.expires_at` 已整体废弃，留着这一列等于给运营看一个永远为空的字段。
        """
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO users(username,password_hash,role,active,qps_limit,credits,openid,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("wx_openid-bob", "x", "user", 1, 2, 0, "openid-bob", utcnow()),
            )
            # 非小程序账号：openid 为空，那一格要有占位符而不是空白
            db.execute(
                "INSERT INTO users(username,password_hash,role,active,qps_limit,credits,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("plainuser", "x", "user", 1, 2, 0, utcnow()),
            )
            db.commit()

        self.login_admin()
        body = self.client.get("/admin/users").get_data(as_text=True)

        self.assertIn('<span class="font-bold text-slate-900">wx_openid-bob</span>', body, "账号列放 username")
        self.assertIn(
            '<span class="font-mono text-[11px] text-slate-600 select-all">openid-bob</span>',
            body, "openid 列放原始 openid，且不带 wx_ 前缀",
        )
        self.assertIn('<span class="font-bold text-slate-900">plainuser</span>', body)

        plain_row = body[body.index("plainuser"):body.index("plainuser") + 600]
        self.assertIn(
            '<span class="text-slate-400">-</span>', plain_row,
            "没有 openid 的账号，openid 那一格是占位符",
        )

        self.assertNotIn("账号到期", body)
        self.assertNotIn("客户名称", body)
        self.assertNotIn("expires_at", body)

    def test_users_page_status_cell_is_a_confirm_gated_toggle(self):
        """状态列是一个开关（switch）：点一下切换，确认框写清对象与动作。

        用户的原话是「状态 字段 看不出来是能切换」—— 只给一个彩色胶囊，看不出它可点。
        所以改成开关（`role="switch"` + 滑块位移），并且右侧另配一个文字标签：
        光看滑块在左还是在右分不出哪个是「正常」（两种约定都有人用），颜色也不能
        作为唯一线索。点击后照旧弹确认框 —— 停用会立刻切断对方的解析。

        管理员那一行不给表单（后端也会拒 `role!='admin'`），只给一个禁用的开关。
        """
        self.login_admin()
        bob = self.make_customer("wx_openid-bob")

        body = self.client.get("/admin/users").get_data(as_text=True)
        self.assertIn('data-confirm="确定要停用客户「wx_openid-bob」吗？"', body)
        self.assertIn('<input type="hidden" name="active" value="0">', body, "启用中 → 点了就是停用")
        self.assertNotIn(
            f'action="/admin/users/{self.user_id}"', body,
            "管理员行不该有启停表单",
        )

        row = self.row_html(body, f">#{bob}</td>")
        self.assertIn('role="switch"', row, "状态列是个开关，不是一个看不出可点的胶囊")
        self.assertIn('aria-checked="true"', row, "启用中 → 开关是打开的样子")
        self.assertIn('translate-x-[18px]', row, "滑块靠右")
        self.assertIn('>正常</span>', row)
        self.assertNotIn('aria-checked="false"', row)

        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET active=0 WHERE id=?", (bob,))
            db.commit()

        body = self.client.get("/admin/users").get_data(as_text=True)
        self.assertIn('data-confirm="确定要启用客户「wx_openid-bob」吗？"', body)
        self.assertIn('<input type="hidden" name="active" value="1">', body, "已停用 → 点了就是启用")

        row = self.row_html(body, f">#{bob}</td>")
        self.assertIn('aria-checked="false"', row, "已停用 → 开关是关上的样子")
        self.assertIn('translate-x-[3px]', row, "滑块靠左")
        self.assertIn('>停用</span>', row)

    def test_the_admin_row_gets_a_dead_switch_not_a_live_one(self):
        """管理员行的开关必须是**禁用的**：同一列里长一样，但点了不该有任何反应。

        后端也拦（`role!='admin'`），所以这不是唯一防线；但一个能点、能弹确认框、
        确认完什么都不发生的开关，比一个明显灰掉的开关糟糕得多。
        """
        self.login_admin()
        body = self.client.get("/admin/users").get_data(as_text=True)

        row = self.row_html(body, f">#{self.user_id}</td>")
        self.assertIn('role="switch"', row)
        self.assertIn("disabled", row, "管理员那一行的开关是禁用的")
        self.assertNotIn("<form", row, "管理员行没有启停表单")

    def test_status_toggle_post_touches_only_active(self):
        """状态列那个按钮只提交 active，守卫必须让它只 UPDATE active。

        没有这条守卫，快捷切换会掉进下面的「完整表单」分支，用表单里缺省的
        `qps_limit=2` 把客户调好的 QPS 一起冲掉。所以这条断言的是
        「积分 999 / QPS 7 原封不动」，而不只是「active 变成了 0」。
        """
        self.login_admin()
        bob = self.make_customer("wx_openid-bob")
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET credits=999, qps_limit=7 WHERE id=?", (bob,))
            db.commit()

        response = self.client.post(
            f"/admin/users/{bob}",
            data={"csrf_token": self.csrf(), "active": "0"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE id=?", (bob,)).fetchone()
        self.assertEqual(user["active"], 0)
        self.assertEqual(user["credits"], 999, "快捷切换不该碰积分")
        self.assertEqual(user["qps_limit"], 7, "快捷切换不该碰 QPS")

    def test_a_post_carrying_credits_is_never_treated_as_a_status_toggle(self):
        """守卫的判据是「这份表单带没带 qps_limit / credits」，而不是「带没带 active」。

        带了 credits 就必须走完整更新分支：调用方明明发了积分、服务端静默丢掉，
        界面上却一切正常 —— 那是最难查的一类 bug。编辑弹窗（active + qps_limit + credits
        三件套）就是这条的活体调用方，这条用例拿掉了它的 qps_limit 字段来逼出这个分支。
        """
        self.login_admin()
        bob = self.make_customer("wx_openid-bob")
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET credits=0, qps_limit=7 WHERE id=?", (bob,))
            db.commit()

        self.client.post(
            f"/admin/users/{bob}",
            data={"csrf_token": self.csrf(), "active": "0", "credits": "555"},
            follow_redirects=True,
        )
        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE id=?", (bob,)).fetchone()
        self.assertEqual(user["credits"], 555, "带了 credits 就该被当成完整表单，不能静默丢弃")
        self.assertEqual(user["active"], 0)


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

    def test_member_plans_page_uses_the_same_card_frame_as_its_siblings(self):
        """这一页原来是一张「贴边」的表：卡片没有内边距、表头也没有统一样式。

        用户的原话是「卡种页面好像跟其他几个页面不太一样，少了外框」。同构的做法在
        客户账号 / 开卡记录 / 支持平台三页是一致的：外层 `section` 白卡（`p-6` 内边距）
        + 内层 `table-wrap` 圆角边框。断言这两个外壳，而不是断言像素。
        """
        self.as_admin()
        html = self.client.get("/admin/member-plans").get_data(as_text=True)

        self.assertIn(
            '<section class="bg-white rounded-2xl border border-slate-200 p-6 shadow-xs">',
            html, "外层白卡与其它后台页同构",
        )
        self.assertIn(
            '<div class="table-wrap overflow-x-auto rounded-xl border border-slate-200">',
            html, "表格另有自己的圆角边框，与其它页一致",
        )
        self.assertNotIn('class="card bg-white', html, "旧的贴边卡片写法应当已消失")

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


class PortalKeyCreationClosedTest(unittest.TestCase):
    """设计文档 §6.2：门户不对用户开放后，自助建密钥是一处无人维护的授权入口。

    2026-09-22 升级：原先把「创建入口已关闭」钉在 405（路径还在、POST 没了）与
    「页面上没有创建表单」两条断言上。密钥通道整体撤销后这两条都过头了 ——
    页面**整个**没了，留着的 keys.html 只会对着一个不存在的功能渲染列表。
    所以断言收紧成 404：路径本身必须消失。这比 405 更紧，也更能防回归 ——
    哪天有人把这段路由加回来，405 那条会绿，404 这条会红。
    """

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
        self.assertEqual(response.status_code, 404)
        with self.app.app_context():
            count = get_db().execute("SELECT COUNT(*) c FROM api_keys WHERE user_id=?",
                                    (self.user_id,)).fetchone()["c"]
            self.assertEqual(count, 0)

    def test_portal_keys_page_is_gone_and_cannot_leak_a_key(self):
        """整个页面必须 404 —— 不只是「没有创建表单」。

        种一个假明文密钥进 session：若哪天有人把那段把 `new_api_key` 渲染出来的模板加回来，
        这条会红。不种的话断言是空的（那段本来就不渲染）。
        """
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["new_api_key"] = "mp_must_not_be_rendered"
        response = self.client.get("/console/keys")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("mp_must_not_be_rendered", response.get_data(as_text=True))

    def test_admin_keys_page_is_gone_too(self):
        """后台那半页同样撤销 —— 管理员后台也没有密钥可管了。"""
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
        self.assertEqual(self.client.get("/admin/keys").status_code, 404)


# ---------------------------------------------------------------------------
# 插件扫码登录（设备码流程）
# ---------------------------------------------------------------------------

import hashlib

from unittest.mock import MagicMock

from src.api.wx import _access_token_state


PAIRING_QR_BYTES = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class WxPairingTest(unittest.TestCase):
    """start / confirm / status 三个端点 + access_token 缓存，每个逻辑分支一条。"""

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
        # 门票缓存是模块级单例，上一个用例留下的票会越过后面的 mock 直接送出
        _access_token_state.update(value=None, good_until=0.0)
        with patch("src.api.wx.code2session",
                   return_value={"openid": "openid-alice", "session_key": "sk-alice"}):
            self.session = self.client.post("/api/v1/wx/login", json={"code": "c"}).get_json()["data"]
        self.token = self.session["token"]

    def tearDown(self):
        _access_token_state.update(value=None, good_until=0.0)
        self.temp_dir.cleanup()

    def start(self, qr=PAIRING_QR_BYTES):
        """生码永远 mock —— 测试绝不真打微信（与 code2session 同一规矩）。"""
        with patch("src.api.wx.fetch_pairing_qrcode", return_value=qr) as fake:
            response = self.client.post("/api/v1/wx/pair/start")
        return response, fake

    def confirm(self, code, headers=None):
        return self.client.post("/api/v1/wx/pair/confirm", json={"pairing_code": code},
                                headers=headers if headers is not None else {"X-WX-Token": self.token})

    def status(self, code):
        return self.client.get("/api/v1/wx/pair/status", query_string={"pairing_code": code})

    def pairing_row(self, code):
        with self.app.app_context():
            return get_db().execute(
                "SELECT * FROM ext_pairings WHERE code_hash=?",
                (hashlib.sha256(code.encode("utf-8")).hexdigest(),),
            ).fetchone()

    # ---------- start ----------

    def test_start_returns_qr_and_registers_a_pending_row(self):
        response, fake = self.start()
        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        code = data["pairing_code"]
        self.assertRegex(code, r"^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{8}$")
        self.assertEqual(base64.b64decode(data["qr_png"]), PAIRING_QR_BYTES)
        self.assertEqual(data["expires_in"], 300)
        # 配对码必须嵌在 scene 里 —— 拿掉这步，扫出来的码永远配不上轮询的码
        fake.assert_called_once_with(f"c={code}")

        row = self.pairing_row(code)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["user_id"], "还没人扫码确认")
        self.assertEqual(row["code_hash"], hashlib.sha256(code.encode()).hexdigest(),
                         "落库的是哈希，明文码绝不进表")

    def test_start_without_wx_config_is_503(self):
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "nocfg.db"),
            "WX_APPID": "",
            "WX_APPSECRET": "",
        })
        with patch("src.api.wx.fetch_pairing_qrcode") as fake:
            response = app.test_client().post("/api/v1/wx/pair/start")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error_code"], "WX_NOT_CONFIGURED")
        fake.assert_not_called()

    def test_start_qr_failure_is_502_and_writes_nothing(self):
        with patch("src.api.wx.fetch_pairing_qrcode", side_effect=RuntimeError("微信挂了")):
            response = self.client.post("/api/v1/wx/pair/start")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error_code"], "WX_QR_FAILED")
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) c FROM ext_pairings").fetchone()["c"], 0)

    # ---------- confirm ----------

    def test_confirm_requires_a_wx_token(self):
        code = self.start()[0].get_json()["data"]["pairing_code"]
        response = self.confirm(code, headers={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.pairing_row(code)["status"], "pending",
                         "被拒的确认不许改变配对状态")

    def test_confirm_mints_an_independent_session_row(self):
        code = self.start()[0].get_json()["data"]["pairing_code"]
        response = self.confirm(code)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["status"], "confirmed")

        with self.app.app_context():
            rows = get_db().execute(
                "SELECT * FROM wx_sessions ORDER BY created_at").fetchall()
        self.assertEqual(len(rows), 2, "登录的 1 行 + 插件的 1 行")
        ext = [r for r in rows if r["token_hash"] != hashlib.sha256(
            self.token.encode()).hexdigest()][0]
        self.assertEqual(ext["user_id"], self.session["user"]["id"])
        self.assertEqual(ext["session_key"], "", "插件会话没有 session_key，存空串")
        self.assertNotEqual(ext["token_hash"], hashlib.sha256(self.token.encode()).hexdigest(),
                            "必须是独立令牌，不能复用小程序自己的那枚")

    def test_confirm_twice_is_409(self):
        code = self.start()[0].get_json()["data"]["pairing_code"]
        self.confirm(code)
        response = self.confirm(code)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error_code"], "PAIRING_ALREADY_CONFIRMED")

    def test_confirm_unknown_code_is_404(self):
        response = self.confirm("ZZZZZZZZ")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error_code"], "PAIRING_NOT_FOUND")

    # ---------- status ----------

    def test_status_pending_then_delivers_the_token_exactly_once(self):
        code = self.start()[0].get_json()["data"]["pairing_code"]

        self.assertEqual(self.status(code).get_json()["data"]["status"], "pending")
        self.confirm(code)

        first = self.status(code)
        self.assertEqual(first.status_code, 200)
        data = first.get_json()["data"]
        self.assertEqual(data["status"], "confirmed")
        self.assertTrue(data["token"])
        self.assertEqual(data["user"]["id"], self.session["user"]["id"])
        # 发出去的令牌必须真的能用 —— 只断非空等于没验货
        me = self.client.get("/api/v1/wx/me", headers={"X-WX-Token": data["token"]})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.get_json()["data"]["user"]["id"], self.session["user"]["id"])

        second = self.status(code).get_json()["data"]
        self.assertEqual(second["status"], "consumed", "令牌只发一次")
        self.assertNotIn("token", second)

    def test_status_expired_is_410(self):
        code = self.start()[0].get_json()["data"]["pairing_code"]
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE ext_pairings SET expires_at=?", ("2020-01-01T00:00:00+00:00",))
            db.commit()
        response = self.status(code)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.get_json()["error_code"], "PAIRING_EXPIRED")

    # ---------- access_token 缓存 ----------

    def test_access_token_is_cached_until_forced(self):
        with patch("src.api.wx._request_access_token",
                   side_effect=[{"access_token": "t1", "expires_in": 7200},
                                {"access_token": "t2", "expires_in": 7200}]) as fake:
            from src.api.wx import get_access_token
            self.assertEqual(get_access_token(), "t1")
            self.assertEqual(get_access_token(), "t1", "有效期内必须复用缓存")
            fake.assert_called_once()
            self.assertEqual(get_access_token(force_refresh=True), "t2",
                             "强制刷新要真去换新票（旧票已被微信顶掉的场景）")
            self.assertEqual(fake.call_count, 2)

    def test_qrcode_fetch_retries_once_on_an_invalid_ticket(self):
        def wx_response(payload_bytes):
            # 代码里是 `with urlopen(...) as response: response.read()` ——
            # __enter__ 必须回到自己身上，配置好的 read 才能被读到
            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.read.return_value = payload_bytes
            return resp

        json_resp = wx_response(b'{"errcode": 40001, "errmsg": "invalid credential"}')
        png_resp = wx_response(PAIRING_QR_BYTES)
        with patch("src.api.wx._request_access_token",
                   side_effect=[{"access_token": "t1", "expires_in": 7200},
                                {"access_token": "t2", "expires_in": 7200}]):
            with patch("src.api.wx.urllib.request.urlopen",
                       side_effect=[json_resp, png_resp]) as urlopen:
                from src.api.wx import fetch_pairing_qrcode
                with self.app.app_context():
                    payload = fetch_pairing_qrcode("c=AB23CD45")
        self.assertEqual(payload, PAIRING_QR_BYTES)
        self.assertEqual(urlopen.call_count, 2, "40001 要强制换票重试一次，而不是直接失败")
        # 第二次请求必须带着新票去 —— 拿旧票重试等于没重试
        self.assertIn("t2", urlopen.call_args_list[1][0][0].full_url)
