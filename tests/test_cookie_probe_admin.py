"""探针配置与状态的管理端集成测试，不请求外部平台。"""

import os
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from src.utils.cookie_registry import _derive_status, latest_probe
from src.db import setting


class CookieProbeAdminTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret",
                               "DATABASE": os.path.join(self.directory.name, "test.db")})
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        self.client.post("/auth/setup", data={"csrf_token": "test-csrf", "username": "admin",
                                             "password": "password123", "confirm_password": "password123"})
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        self.client.post("/auth/login", data={"csrf_token": "test-csrf", "username": "admin",
                                             "password": "password123"})

    def tearDown(self):
        self.directory.cleanup()

    def post(self, endpoint, **data):
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf"
        return self.client.post("/admin/api/cookies/" + endpoint,
                                data={"csrf_token": "test-csrf", **data})

    def test_save_credential_and_restore_sample_require_verification(self):
        result = self.post("credential", platform="weibo", value="SUB=test;")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["data"]["status"], "unverified")
        result = self.post("canary", platform="weibo", url="")
        self.assertIn("样本库", result.json["message"])
        with self.app.app_context():
            self.assertEqual(_derive_status("weibo"), "unverified")
            self.assertEqual(latest_probe("weibo")["status"], "unverified")
        page = self.client.get("/admin/cookies")
        self.assertEqual(page.status_code, 200)
        self.assertIn("待验证", page.get_data(as_text=True))
        self.assertIn("default_canary_url", page.get_data(as_text=True))

    def test_link_must_match_credential_platform(self):
        result = self.post("canary", platform="douyin", url="https://weixin.qq.com/sph/AzGrUgqzFv")
        self.assertEqual(result.status_code, 400)
        self.assertIn("不匹配", result.json["message"])

    def test_save_custom_link_and_missing_defaults(self):
        result = self.post("canary", platform="douyin", url="https://v.douyin.com/custom/")
        self.assertTrue(result.json["succ"])
        with self.app.app_context():
            self.assertEqual(latest_probe("douyin")["status"], "unverified")
        with patch("src.utils.cookie_samples.default_probe_sample", return_value=None):
            result = self.post("canary", platform="douyin", url="")
        self.assertIn("暂无默认样例", result.json["message"])

    def test_modal_saves_cookie_and_link_together(self):
        url = "https://weibo.com/123456/AbCdEf"
        result = self.post("credential", platform="weibo", value="SUB=new;", canary_url=url)
        self.assertTrue(result.json["succ"])
        with self.app.app_context():
            self.assertEqual(setting("cookie_weibo"), "SUB=new;")
            self.assertEqual(setting("canary_weibo"), url)
            self.assertEqual(_derive_status("weibo"), "unverified")

    def test_invalid_link_does_not_partially_save_cookie(self):
        self.post("credential", platform="weibo", value="SUB=original;")
        for url in ("not-a-link", "https://v.douyin.com/custom/"):
            with self.subTest(url=url):
                result = self.post("credential", platform="weibo", value="SUB=changed;", canary_url=url)
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.json["field"], "canary_url")
                with self.app.app_context():
                    self.assertEqual(setting("cookie_weibo"), "SUB=original;")
                    self.assertFalse(setting("canary_weibo"))

    def test_clear_cookie_uses_actual_environment_and_restores_default_link(self):
        for env_cookie, expected_source, expected_status in (
            ("SUB=environment;", "env", "unverified"), ("", "none", "not_configured"),
        ):
            with self.subTest(source=expected_source), patch.dict(os.environ, {"WEIBO_COOKIE": env_cookie}):
                self.post("credential", platform="weibo", value="SUB=database;",
                          canary_url="https://weibo.com/123456/AbCdEf")
                result = self.post("credential", platform="weibo", value="", canary_url="")
                self.assertEqual(result.json["data"]["status"], expected_status)
                overview = self.client.get("/admin/api/cookies/overview").json["data"]
                item = next(item for item in overview["entries"] if item["platform_key"] == "weibo")
                self.assertEqual(item["source"], expected_source)
                self.assertEqual(item["status"], expected_status)
                self.assertFalse(item["canary_url"])
                self.assertTrue(item["default_canary_url"])

    def test_probe_result_time_overview_and_rate_limit(self):
        from src.utils.cookie_prober import _last_probe_at
        _last_probe_at.clear()
        self.addCleanup(_last_probe_at.clear)
        self.post("credential", platform="weibo", value="SUB=test;", canary_url="")
        results = [
            {"ok": True, "reason": "OK", "detail": {"has_video": True}, "live_count": 0},
            {"ok": False, "reason": "MEDIA_NOT_FOUND", "detail": {}, "live_count": 0},
        ]
        with patch("src.utils.cookie_prober._run_parse", side_effect=results) as parse:
            first = self.post("probe", platform="weibo")
            second = self.post("probe", platform="weibo")
        self.assertEqual(first.json["data"]["status"], "healthy")
        self.assertEqual(second.status_code, 429)
        self.assertEqual(parse.call_count, 2)
        overview = self.client.get("/admin/api/cookies/overview").json["data"]
        item = next(item for item in overview["entries"] if item["platform_key"] == "weibo")
        self.assertEqual(item["status"], "healthy")
        self.assertTrue(item["last_probe"]["probed_at"])
        self.assertGreaterEqual(overview["summary"]["healthy"], 1)
