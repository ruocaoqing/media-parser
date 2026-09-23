import hashlib
import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from app import create_app
from src.api.access import SQLiteRateLimiter, get_client_ip, sanitize_log_url
from src.db import get_db, reserve_user_credit, utcnow


class ManagementTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": os.path.join(self.temp_dir.name, "test.db"),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def csrf(self):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        return "test-csrf"

    def create_wx_customer(self, token="wx-test-token-1", username="customer", qps=2, credits=0):
        """建一个能通过 `X-WX-Token` 鉴权的用户，返回 (token, user_id)。

        2026-09-22 改写：原方法 `create_customer_and_key` 建的是 users + api_keys 两行，
        凭证是一把明文密钥。密钥通道整体撤销后令牌成了唯一凭证，故改为建 users + wx_sessions
        两行 —— 令牌本身不落库，库里存的是它的 sha256（access.py 就是这么查的）。

        同日又删掉 `expires_in_days` 形参：`users.expires_at` 已整体废弃，
        账号的开关只剩 `active` 一个字段，不再有「造一个过期账号」这种状态可造。
        `credits` 默认 0：新的免费档用户余额就是 0（登录时不再送 100），
        需要测第二层的用例自己传数。
        """
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO users(username,password_hash,qps_limit,credits,created_at) VALUES(?,?,?,?,?)",
                (username, "unused", qps, credits, utcnow()),
            )
            user_id = cursor.lastrowid
            db.execute(
                "INSERT INTO wx_sessions(token_hash,user_id,session_key,created_at,expires_at,last_seen_at) VALUES(?,?,?,?,?,?)",
                (
                    hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    user_id,
                    "",
                    utcnow(),
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds"),
                    utcnow(),
                ),
            )
            db.commit()
        return token, user_id

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
        return parser

    def test_first_admin_setup_and_login(self):
        response = self.client.post(
            "/auth/setup",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("管理员创建成功", response.get_data(as_text=True))
        response = self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin"))

    def test_login_remember_me_controls_session_permanence(self):
        self.client.post(
            "/auth/setup",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"},
        )
        # Without remember_me
        with self.client:
            self.client.post(
                "/auth/login",
                data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"},
            )
            from flask import session
            self.assertFalse(session.permanent)

        # With remember_me
        with self.client:
            self.client.post(
                "/auth/login",
                data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "remember_me": "1"},
            )
            from flask import session
            self.assertTrue(session.permanent)

    def test_setup_rejects_missing_csrf_token(self):
        response = self.client.post(
            "/auth/setup",
            data={"username": "attacker", "password": "password123", "confirm_password": "password123"},
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            admin = get_db().execute("SELECT 1 FROM users WHERE role='admin'").fetchone()
        self.assertIsNone(admin)

    def test_authenticated_management_rejects_missing_csrf_token(self):
        self.client.post(
            "/auth/setup",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"},
        )
        self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"},
        )
        response = self.client.post("/admin/settings", data={"global_api_enabled": "0"})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            from src.db import setting
            self.assertEqual(setting("global_api_enabled"), "1")

    def test_secret_key_is_generated_and_reused(self):
        database = os.path.join(self.temp_dir.name, "auto-secret.db")
        first = create_app({"TESTING": True, "DATABASE": database, "SECRET_KEY": None})
        second = create_app({"TESTING": True, "DATABASE": database, "SECRET_KEY": None})
        self.assertTrue(first.config["SECRET_KEY"])
        self.assertEqual(first.config["SECRET_KEY"], second.config["SECRET_KEY"])

    def test_parse_without_token_is_rejected(self):
        """没有任何凭证时必须 401，而不是放行。

        2026-09-22：原为 `test_api_key_is_required`（断言 API_KEY_REQUIRED）。
        撤销密钥通道时这一条是**承重**的 —— 去掉 parse.py 里那段 `if access is None`
        守卫，/api/v1/parse 就变成完全公开的解析接口，那正是撤销密钥要堵的洞。
        错误码从 INVALID 单列为 REQUIRED：没带凭证和凭证是坏的对调用方是两件事。
        """
        response = self.client.get("/api/v1/parse?url=https://example.com")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "WX_TOKEN_REQUIRED")

    def test_garbage_token_is_rejected(self):
        """带了一枚不存在的令牌 —— 与「没带」是两个不同的错误码。"""
        response = self.client.get(
            "/api/v1/parse?url=https://example.com", headers={"X-WX-Token": "not-a-real-token"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error_code"], "WX_TOKEN_INVALID")

    def test_disabled_account_rejects_token(self):
        """令牌路径的 403 只剩「停用」一条：`active=0` → ACCOUNT_DISABLED。

        接管了原 `test_expired_account_rejects_token` 的槽位 —— 账号过期那条路
        随 `users.expires_at` 一起没了，但 access.py 的停用分支仍要在令牌路径上有人钉住
        （`test_wx_login.py` 的 `test_disabled_account_is_403_not_200` 钉的是解析路径）。
        """
        token, user_id = self.create_wx_customer(token="wx-token-disabled", username="disabled_user")
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
            db.commit()
        response = self.client.get(
            "/api/v1/parse?url=https://example.com", headers={"X-WX-Token": token}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error_code"], "ACCOUNT_DISABLED")

    def test_valid_token_can_call_get_api(self):
        """令牌合法且额度未用尽 → 200 且扣掉当日一次。

        2026-09-22：原同形用例 `test_permanent_account_accepts_key` 与
        `test_expired_account_rejects_token` 已随 `users.expires_at` 删除 ——
        令牌路径上「账号是否过期」不再是变量，拒绝只剩上面那条「停用」。
        """
        token, _ = self.create_wx_customer()
        with patch("src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/1"), patch(
            "src.api.parse.ParserFactory.create_parser", return_value=self.parser()
        ):
            response = self.client.get(
                "/api/v1/parse?url=https://example.com", headers={"X-WX-Token": token}
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["succ"])

    def test_user_qps_limit_is_enforced(self):
        """限流桶按用户算：同一用户连发两次，第二次 429。

        2026-09-22：原为 `test_user_qps_is_shared_by_all_of_the_users_keys`，
        论证的是「一个用户的多把密钥共用一个桶」。密钥没了，桶还在，
        且主体名逐字保持不变（仍是 `user:{id}`）—— 这条用例现在钉的就是那件事本身。
        """
        token, _ = self.create_wx_customer(qps=1)
        with patch("src.api.access.time.time", return_value=123456), patch(
            "src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/1"
        ), patch("src.api.parse.ParserFactory.create_parser", return_value=self.parser()):
            first = self.client.get("/api/v1/parse?url=https://example.com", headers={"X-WX-Token": token})
            second = self.client.get("/api/v1/parse?url=https://example.com", headers={"X-WX-Token": token})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.get_json()["error_code"], "RATE_LIMITED")
        self.assertIn("账号限制", second.get_json()["retdesc"])

    def test_platform_qps_is_shared_by_different_users(self):
        """平台桶是**跨用户**的：两个各自限流宽松的用户，撞同一个平台上限。"""
        first_token, _ = self.create_wx_customer(token="wx-token-a", username="customer_a", qps=10)
        second_token, _ = self.create_wx_customer(token="wx-token-b", username="customer_b", qps=10)
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO platform_settings(platform,enabled,qps_limit) VALUES('抖音',1,1)")
            db.commit()
        with patch("src.api.access.time.time", return_value=123456), patch(
            "src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/1"
        ), patch("src.api.parse.ParserFactory.create_parser", return_value=self.parser()):
            first = self.client.get("/api/v1/parse?url=https://example.com", headers={"X-WX-Token": first_token})
            second = self.client.get("/api/v1/parse?url=https://example.com", headers={"X-WX-Token": second_token})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.get_json()["error_code"], "PLATFORM_RATE_LIMITED")

    def test_rate_limit_state_is_shared_between_app_instances(self):
        limiter = SQLiteRateLimiter()
        with patch("src.api.access.time.time", return_value=123456):
            with self.app.app_context():
                self.assertTrue(limiter.consume("shared:test", 1))
            second_app = create_app({
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": self.app.config["DATABASE"],
            })
            with second_app.app_context():
                self.assertFalse(SQLiteRateLimiter().consume("shared:test", 1))

    def test_forwarded_ip_is_only_used_for_trusted_proxies(self):
        headers = {"X-Forwarded-For": "203.0.113.10, 10.0.0.1"}
        with self.app.test_request_context("/", headers=headers, environ_base={"REMOTE_ADDR": "192.0.2.10"}):
            self.app.config["TRUST_PROXY_HEADERS"] = False
            self.assertEqual(get_client_ip(), "192.0.2.10")
            self.app.config["TRUST_PROXY_HEADERS"] = True
            self.assertEqual(get_client_ip(), "203.0.113.10")

    def test_login_attempts_are_rate_limited_by_username(self):
        self.client.post(
            "/auth/setup",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"},
        )
        with patch("src.api.access.time.time", return_value=123456):
            for _ in range(10):
                response = self.client.post(
                    "/auth/login",
                    data={"csrf_token": self.csrf(), "username": "admin", "password": "wrong-password"},
                )
                self.assertEqual(response.status_code, 200)
            limited = self.client.post(
                "/auth/login",
                data={"csrf_token": self.csrf(), "username": "admin", "password": "wrong-password"},
            )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("登录尝试过于频繁", limited.get_data(as_text=True))

    def test_registration_attempts_are_rate_limited_by_ip(self):
        with patch("src.api.access.time.time", return_value=123456):
            for _ in range(5):
                response = self.client.post(
                    "/auth/register",
                    data={"csrf_token": self.csrf(), "username": "candidate", "password": "password123", "confirm_password": "different"},
                )
                self.assertEqual(response.status_code, 200)
            limited = self.client.post(
                "/auth/register",
                data={"csrf_token": self.csrf(), "username": "candidate", "password": "password123", "confirm_password": "different"},
            )
        self.assertEqual(limited.status_code, 429)
        self.assertIn("注册尝试过于频繁", limited.get_data(as_text=True))

    def test_disabled_platform_is_rejected(self):
        token, _ = self.create_wx_customer()
        with self.app.app_context():
            db = get_db()
            db.execute("INSERT INTO platform_settings(platform,enabled) VALUES('抖音',0)")
            db.commit()
        with patch("src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/1"):
            response = self.client.get(
                "/api/v1/parse?url=https://example.com", headers={"X-WX-Token": token}
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error_code"], "PLATFORM_DISABLED")


    def test_registration_assigns_default_credits(self):
        """注册只发积分。2026-09-22 起不再有「试用期」这个概念，账号建出来就是长期有效的。"""
        # Mismatched password failure test
        mismatch = self.client.post(
            "/auth/register",
            data={"csrf_token": self.csrf(), "username": "mismatch_user", "password": "password123", "confirm_password": "differentpassword"},
            follow_redirects=True,
        )
        self.assertIn("两次输入的密码不一致", mismatch.get_data(as_text=True))

        response = self.client.post(
            "/auth/register",
            data={"csrf_token": self.csrf(), "username": "new_credit_user", "password": "password123", "confirm_password": "password123"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("账号已开通并赠送 100 积分", response.get_data(as_text=True))
        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE username='new_credit_user'").fetchone()
            self.assertEqual(user["credits"], 100)
            self.assertEqual(user["active"], 1)

    def test_registration_with_unlimited_credits(self):
        from src.db import set_setting
        with self.app.app_context():
            set_setting("default_initial_credits", -1)
            get_db().commit()

        response = self.client.post(
            "/auth/register",
            data={"csrf_token": self.csrf(), "username": "perm_user", "password": "password123", "confirm_password": "password123"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("无限解析额度", response.get_data(as_text=True))
        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE username='perm_user'").fetchone()
            self.assertEqual(user["credits"], -1)

    def test_get_daily_trend(self):
        from src.db import get_daily_trend
        with self.app.app_context():
            db = get_db()
            now_iso = utcnow()
            db.execute(
                "INSERT INTO request_logs(user_id,api_key_id,platform,path,status_code,error_code,duration_ms,created_at) "
                "VALUES(1, 1, 'douyin', '/api/v1/parse', 200, NULL, 50, ?)", (now_iso,)
            )
            db.execute(
                "INSERT INTO request_logs(user_id,api_key_id,platform,path,status_code,error_code,duration_ms,created_at) "
                "VALUES(1, 1, 'douyin', '/api/v1/parse', 500, 'ERR', 100, ?)", (now_iso,)
            )
            db.commit()

            trend_all = get_daily_trend(user_id=None, days=7)
            self.assertEqual(len(trend_all["trend"]), 7)
            self.assertGreaterEqual(trend_all["max_val"], 2)

            trend_user = get_daily_trend(user_id=1, days=7)
            self.assertEqual(len(trend_user["trend"]), 7)
            self.assertGreaterEqual(trend_user["max_val"], 2)

    def test_log_url_is_sanitized(self):
        value = sanitize_log_url(
            "复制链接 https://example.com/video/1?share_id=42&token=secret&sign=abc#private"
        )
        self.assertEqual(
            value,
            "https://example.com/video/1?share_id=42&token=secret&sign=abc#private",
        )
        self.assertEqual(
            sanitize_log_url("https://user:password@example.com/private"),
            "https://user:password@example.com/private",
        )

    def test_admin_can_export_logs_as_csv(self):
        self.client.post("/auth/setup", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/login", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"})
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO request_logs(platform,path,status_code,error_code,duration_ms,input_url,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("抖音", "/api/v1/parse", 400, "MEDIA_NOT_FOUND", 123, "https://example.com/video/1", utcnow()),
            )
            db.commit()

        response = self.client.get("/admin/logs/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        self.assertIn("Content-Length", response.headers)
        body = response.get_data(as_text=True)
        self.assertIn("请求 URL", body)
        self.assertIn("https://example.com/video/1", body)
        self.assertIn("MEDIA_NOT_FOUND", body)

        # Test with error status filter and all mode
        filtered_resp = self.client.get("/admin/logs/export.csv?logs_status=error&export_type=all")
        self.assertEqual(filtered_resp.status_code, 200)
        self.assertIn("https://example.com/video/1", filtered_resp.get_data(as_text=True))

        # Test with 200 status filter (should exclude error log)
        ok_resp = self.client.get("/admin/logs/export.csv?logs_status=200&export_type=all")
        self.assertEqual(ok_resp.status_code, 200)
        self.assertNotIn("https://example.com/video/1", ok_resp.get_data(as_text=True))

    def test_user_can_export_only_own_logs_as_csv(self):
        self.client.post(
            "/auth/register",
            data={"csrf_token": self.csrf(), "username": "log_user", "password": "password123", "confirm_password": "password123"},
        )
        self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "log_user", "password": "password123"},
        )
        with self.app.app_context():
            db = get_db()
            user = db.execute("SELECT id FROM users WHERE username='log_user'").fetchone()
            db.execute(
                "INSERT INTO request_logs(user_id,platform,path,status_code,duration_ms,input_url,created_at) VALUES(?,?,?,?,?,?,?)",
                (user["id"], "抖音", "/api/v1/parse", 200, 88, "https://example.com/mine", utcnow()),
            )
            db.execute(
                "INSERT INTO request_logs(platform,path,status_code,duration_ms,input_url,created_at) VALUES(?,?,?,?,?,?)",
                ("快手", "/api/v1/parse", 200, 99, "https://example.com/other", utcnow()),
            )
            db.commit()

        response = self.client.get("/console/logs/export.csv")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("https://example.com/mine", body)
        self.assertNotIn("https://example.com/other", body)

    def test_admin_can_batch_delete_logs(self):
        self.client.post("/auth/setup", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/login", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"})
        with self.app.app_context():
            db = get_db()
            c1 = db.execute(
                "INSERT INTO request_logs(path,status_code,duration_ms,created_at) VALUES(?,?,?,?)",
                ("/api/v1/parse", 200, 1, utcnow()),
            )
            c2 = db.execute(
                "INSERT INTO request_logs(path,status_code,duration_ms,created_at) VALUES(?,?,?,?)",
                ("/api/v1/parse", 200, 1, utcnow()),
            )
            db.commit()
            id1 = c1.lastrowid
            id2 = c2.lastrowid

        response = self.client.post(
            "/admin/logs/batch",
            data={"csrf_token": self.csrf(), "ids": str(id1), "action": "delete"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin/logs"))

        # Follow redirect and verify flash message
        response = self.client.get(response.location, follow_redirects=True)
        self.assertIn("已批量删除选中的 1 条日志记录", response.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(get_db().execute("SELECT COUNT(*) count FROM request_logs").fetchone()["count"], 1)

    def test_update_api_tip_settings(self):
        # Create admin and login
        self.client.post("/auth/setup", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/login", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"})

        # Update settings via admin
        res = self.client.post(
            "/admin/settings",
            data={
                "csrf_token": self.csrf(),
                "global_api_enabled": "1",
                "demo_enabled": "1",
                "registration_enabled": "1",
                "default_user_qps": "5",
                "default_initial_credits": "-1",
                "api_tip_enabled": "1",
                "api_tip_author": "custom_author",
                "api_tip_website": "https://example.com/api",
                "api_tip_notice": "自定义接口服务文案",
            },
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)

        with self.app.app_context():
            from src.db import setting
            self.assertEqual(setting("default_initial_credits"), "-1")
            self.assertEqual(setting("api_tip_author"), "custom_author")
            self.assertEqual(setting("api_tip_website"), "https://example.com/api")
            self.assertEqual(setting("api_tip_notice"), "自定义接口服务文案")

    def test_user_change_password(self):
        # Register customer
        self.client.post(
            "/auth/register",
            data={"csrf_token": self.csrf(), "username": "user1", "password": "oldpassword123", "confirm_password": "oldpassword123"},
        )
        # Login
        self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "user1", "password": "oldpassword123"},
        )

        # Fail change with wrong old password
        res = self.client.post(
            "/auth/change-password",
            data={
                "csrf_token": self.csrf(),
                "old_password": "wrongoldpassword",
                "new_password": "newpassword123",
                "confirm_password": "newpassword123",
            },
            follow_redirects=True,
        )
        self.assertIn("原密码错误", res.get_data(as_text=True))

        # Fail change with password too short
        res = self.client.post(
            "/auth/change-password",
            data={
                "csrf_token": self.csrf(),
                "old_password": "oldpassword123",
                "new_password": "short",
                "confirm_password": "short",
            },
            follow_redirects=True,
        )
        self.assertIn("新密码至少需要 8 位", res.get_data(as_text=True))

        # Fail change with mismatched confirmation
        res = self.client.post(
            "/auth/change-password",
            data={
                "csrf_token": self.csrf(),
                "old_password": "oldpassword123",
                "new_password": "newpassword123",
                "confirm_password": "different123",
            },
            follow_redirects=True,
        )
        self.assertIn("两次输入的新密码不一致", res.get_data(as_text=True))

        # Successful password change
        res = self.client.post(
            "/auth/change-password",
            data={
                "csrf_token": self.csrf(),
                "old_password": "oldpassword123",
                "new_password": "newpassword123",
                "confirm_password": "newpassword123",
            },
            follow_redirects=True,
        )
        self.assertIn("密码修改成功", res.get_data(as_text=True))

        # Verify old password no longer works, new password works
        self.client.post("/auth/logout", data={"csrf_token": self.csrf()})
        fail_login = self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "user1", "password": "oldpassword123"},
            follow_redirects=True,
        )
        self.assertIn("用户名或密码错误", fail_login.get_data(as_text=True))

        succ_login = self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "user1", "password": "newpassword123"},
        )
        self.assertEqual(succ_login.status_code, 302)

    def test_admin_reset_customer_password(self):
        # Create admin
        self.client.post(
            "/auth/setup",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "adminpassword123", "confirm_password": "adminpassword123"},
        )
        # Register customer
        self.client.post(
            "/auth/register",
            data={"csrf_token": self.csrf(), "username": "customer1", "password": "userpassword123", "confirm_password": "userpassword123"},
        )

        with self.app.app_context():
            customer = get_db().execute("SELECT * FROM users WHERE username='customer1'").fetchone()
            customer_id = customer["id"]

        # Admin login
        self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "admin", "password": "adminpassword123"},
        )

        # Admin resets customer password with short password
        res = self.client.post(
            f"/admin/users/{customer_id}/reset-password",
            data={"csrf_token": self.csrf(), "new_password": "short"},
            follow_redirects=True,
        )
        self.assertIn("重置密码至少需要 8 位", res.get_data(as_text=True))

        # Admin resets customer password successfully
        res = self.client.post(
            f"/admin/users/{customer_id}/reset-password",
            data={"csrf_token": self.csrf(), "new_password": "resetpassword888"},
            follow_redirects=True,
        )
        self.assertIn("已成功重置客户「customer1」的密码", res.get_data(as_text=True))

        # Verify customer can login with reset password
        self.client.post("/auth/logout", data={"csrf_token": self.csrf()})
        succ_login = self.client.post(
            "/auth/login",
            data={"csrf_token": self.csrf(), "username": "customer1", "password": "resetpassword888"},
        )
        self.assertEqual(succ_login.status_code, 302)

    def test_concurrent_credit_reservations_cannot_exceed_balance(self):
        """并发预占不能把余额扣成负数 —— 这是 `reserve_user_credit` 存在的唯一理由。

        2026-09-22：本用例原挂在密钥路径上（用密钥触发扣减），密钥撤销后改由
        `create_wx_customer` 造一个用户。**它本身不依赖任何鉴权路径** ——
        直接调 `reserve_user_credit`，测的是 `BEGIN IMMEDIATE` + `WHERE credits>0`
        这对组合在真并发下守不守得住。撤销的是密钥，不是这个原子性，
        所以这条用例是**移植**不是删除。
        """
        _, user_id = self.create_wx_customer(qps=10, credits=1)

        barrier = Barrier(2)

        def reserve_once():
            with self.app.app_context():
                barrier.wait()
                return reserve_user_credit(user_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: reserve_once(), range(2)))

        self.assertEqual(sorted(results, key=lambda value: value is not True), [True, None])
        with self.app.app_context():
            credits = get_db().execute(
                "SELECT credits FROM users WHERE id=?", (user_id,)
            ).fetchone()["credits"]
        self.assertEqual(credits, 0)

    # 2026-09-22 删除的三条（撤销密钥通道的直接后果）：
    #   * `test_initial_credits_and_deduction` —— 断言 /register 送 100 积分、密钥调用扣 1 分。
    #     注册送积分那半截仍由 `test_registration_permanent_and_unlimited_credits` 覆盖；
    #     扣减那半截现在钉在 test_wx_login.py 的两层用例里（顺序 / 回退 / 402），
    #     在这个文件里再测一遍是同一件事的第三份拷贝。它唯一测不到的「密钥路径扣积分」
    #     已经不存在了。
    #   * `test_failed_parse_refunds_reserved_credit` —— 失败退款的**分层**版本
    #     （`test_failed_parse_refunds_the_balance_tier`）在 test_wx_login.py，
    #     且断言更强：除了余额回到 2，还断言每日额度那层没被动到。
    #   * `test_legacy_key_hash_compatibility` —— 与 `legacy_hash_api_key` 同名的那套
    #     旧哈希兼容逻辑随密钥通道一起撤销了。

    def test_admin_manage_user_credits(self):
        self.client.post("/auth/setup", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/login", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"})
        self.client.post("/auth/register", data={"csrf_token": self.csrf(), "username": "user_cred_test", "password": "password123", "confirm_password": "password123"})

        with self.app.app_context():
            user = get_db().execute("SELECT * FROM users WHERE username='user_cred_test'").fetchone()
            user_id = user["id"]

        # Admin updates user credits to 999
        res = self.client.post(
            f"/admin/users/{user_id}",
            data={"csrf_token": self.csrf(), "credits": "999", "active": "1", "qps_limit": "2"},
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)

    def test_portal_platforms_dashboard(self):
        # Setup admin first, then register regular user
        self.client.post("/auth/setup", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/register", data={"csrf_token": self.csrf(), "username": "portal_viewer", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/login", data={"csrf_token": self.csrf(), "username": "portal_viewer", "password": "password123"})

        with self.app.app_context():
            db = get_db()
            user = db.execute("SELECT id FROM users WHERE username='portal_viewer'").fetchone()
            db.execute(
                "INSERT INTO request_logs(user_id,platform,path,status_code,duration_ms,input_url,created_at) VALUES(?,?,?,?,?,?,?)",
                (user["id"], "抖音", "/api/v1/parse", 200, 50, "https://v.douyin.com/abc", utcnow()),
            )
            db.commit()

        # Access /console/platforms
        res = self.client.get("/console/platforms")
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("支持平台", body)
        self.assertIn("AcFun", body)
        # Verify read-only nature: no edit forms or submit buttons for platform settings
        self.assertNotIn('action="/admin/platforms', body)

        # Search filter
        res_search = self.client.get("/console/platforms?platforms_q=抖音")
        self.assertEqual(res_search.status_code, 200)
        self.assertIn("抖音", res_search.get_data(as_text=True))

    def test_portal_overview_shows_account_status_not_an_expiry(self):
        """概览页的账号状态。2026-09-22：`users.expires_at` 废弃后，原先的
        「有效期至 / 已到期」两件套换成 active 一个字段的「正常 / 已停用」。

        这条同时是模板的渲染守卫 —— 全库没有别的用例 GET 过 `/console/overview`，
        jinja 里少一个变量（比如已删掉的 `user_is_expired`）不会被任何测试发现。
        """
        self.client.post("/auth/setup", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/register", data={"csrf_token": self.csrf(), "username": "portal_user", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/login", data={"csrf_token": self.csrf(), "username": "portal_user", "password": "password123"})

        response = self.client.get("/console/overview")
        self.assertEqual(response.status_code, 200)
        flat = re.sub(r"\s+", " ", response.get_data(as_text=True))
        self.assertIn(
            '<span class="px-2.5 py-0.5 rounded-full text-xs font-semibold '
            'bg-emerald-50 text-emerald-700 border border-emerald-200"> 正常 </span>',
            flat,
        )
        for gone in ("有效期至", "已到期", "服务有效", "永久有效"):
            self.assertNotIn(gone, flat, f"「{gone}」随 expires_at 一起删掉了，不该还能看到")

        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE users SET active=0 WHERE username='portal_user'")
            db.commit()

        flat = re.sub(r"\s+", " ", self.client.get("/console/overview").get_data(as_text=True))
        self.assertIn(
            '<span class="px-2.5 py-0.5 rounded-full text-xs font-semibold '
            'bg-red-50 text-red-700 border border-red-200"> 已停用 </span>',
            flat,
        )
        self.assertIn("当前账号已被停用", flat)

    def test_console_topbar_api_status(self):
        self.client.post("/auth/setup", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/login", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"})

        # API is enabled by default
        res = self.client.get("/admin/overview")
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("status-pill online", body)
        self.assertIn("API 正常", body)

        # Disable API via settings (omit global_api_enabled to simulate unchecked checkbox)
        self.client.post(
            "/admin/settings",
            data={
                "csrf_token": self.csrf(),
            },
            follow_redirects=True,
        )

        # Verify topbar now reflects maintenance mode
        res_maint = self.client.get("/admin/overview")
        self.assertEqual(res_maint.status_code, 200)
        body_maint = res_maint.get_data(as_text=True)
        self.assertIn("status-pill offline", body_maint)
        self.assertIn("API 维护中", body_maint)

    def test_customer_login_with_admin_next_param(self):
        # Register a customer
        with self.app.app_context():
            db = get_db()
            from werkzeug.security import generate_password_hash
            db.execute(
                "INSERT INTO users(username,password_hash,role,qps_limit,created_at) VALUES(?,?,?,?,?)",
                ("customer_leo", generate_password_hash("password123"), "user", 2, utcnow()),
            )
            db.commit()

        # Login as customer with next pointing to /admin/overview
        response = self.client.post(
            "/auth/login?next=/admin/overview",
            data={"csrf_token": self.csrf(), "username": "customer_leo", "password": "password123"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        # Should redirect directly to portal dashboard (/console or /console/overview), not admin
        self.assertTrue(response.location.endswith("/console") or response.location.endswith("/console/overview"))
        self.assertNotIn("/admin", response.location)


    def test_batch_logs_select_all(self):
        self.client.post("/auth/setup", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/login", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"})

        with self.app.app_context():
            db = get_db()
            for i in range(3):
                db.execute(
                    "INSERT INTO request_logs(path,platform,status_code,duration_ms,created_at) VALUES(?,?,?,?,?)",
                    ("/api/v1/parse", "抖音", 500, 10, utcnow()),
                )
            for i in range(2):
                db.execute(
                    "INSERT INTO request_logs(path,platform,status_code,duration_ms,created_at) VALUES(?,?,?,?,?)",
                    ("/api/v1/parse", "快手", 200, 10, utcnow()),
                )
            db.commit()

        # Batch delete filtered by platform 抖音
        response = self.client.post(
            "/admin/logs/batch",
            data={
                "csrf_token": self.csrf(),
                "select_mode": "all",
                "logs_platform": "抖音",
                "action": "delete",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已批量删除符合筛选条件的全部 3 条日志记录", response.get_data(as_text=True))

        with self.app.app_context():
            count = get_db().execute("SELECT COUNT(*) count FROM request_logs").fetchone()["count"]
            self.assertEqual(count, 2)

    def test_batch_users_select_all(self):
        self.client.post("/auth/setup", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123", "confirm_password": "password123"})
        self.client.post("/auth/login", data={"csrf_token": self.csrf(), "username": "admin", "password": "password123"})

        with self.app.app_context():
            db = get_db()
            from werkzeug.security import generate_password_hash
            db.execute(
                "INSERT INTO users(username,password_hash,role,active,credits,created_at) VALUES(?,?,?,?,?,?)",
                ("u1", generate_password_hash("p"), "user", 1, 100, utcnow()),
            )
            db.execute(
                "INSERT INTO users(username,password_hash,role,active,credits,created_at) VALUES(?,?,?,?,?,?)",
                ("u2", generate_password_hash("p"), "user", 1, 200, utcnow()),
            )
            db.commit()

        # Batch adjust credits in all mode
        response = self.client.post(
            "/admin/users/batch",
            data={
                "csrf_token": self.csrf(),
                "select_mode": "all",
                "action": "adjust_credits",
                "credits_mode": "add",
                "credits_amount": "50",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("已批量更新符合筛选条件的全部 2 个客户账号积分", response.get_data(as_text=True))

        with self.app.app_context():
            u1 = get_db().execute("SELECT credits FROM users WHERE username='u1'").fetchone()
            u2 = get_db().execute("SELECT credits FROM users WHERE username='u2'").fetchone()
            self.assertEqual(u1["credits"], 150)
            self.assertEqual(u2["credits"], 250)

    # 2026-09-22 删除：`test_batch_keys_select_all` 与 `test_portal_batch_keys_select_all`。
    # 两条分别在打 `/admin/keys/batch` 与 `/console/keys/batch`，那两个路由连同
    # keys.html 模板一起撤销了（页面 100% 是密钥凭证 UI，留着只会撒谎）。
    # 它们论证的「批量选择范围 = 全部」仍由 `test_batch_logs_select_all`
    # 与 `test_batch_users_select_all` 覆盖 —— 那是同一套 batch 机制。


if __name__ == "__main__":
    unittest.main()

