import json
import os
import unittest
from unittest.mock import patch

from src.utils import cookie_registry
from src.utils.cookie_registry import (
    COOKIE_REGISTRY,
    build_finding,
    build_overview,
    check_cookie_fields,
    get_cookie_source,
    get_cookie_value,
    passive_signal,
    run_auto_check,
    _parse_cookie_pairs,
)


class CookieRegistryTest(unittest.TestCase):
    def test_registry_covers_every_settings_cookie_key(self):
        """设置页写入的每个 cookie_* 键都必须被注册表覆盖。"""
        settings_keys = {
            "cookie_doubao", "cookie_douyin", "cookie_jimeng", "cookie_kuaishou",
            "cookie_pinduoduo", "cookie_wechat_channels", "cookie_weibo",
            "cookie_xiaohongshu", "cookie_yuanbao", "cookie_zhihu",
        }
        covered = {key for entry in COOKIE_REGISTRY.values() for key in entry["db_keys"]}
        self.assertEqual(settings_keys, covered)

    def test_parse_cookie_pairs_tolerates_messy_input(self):
        pairs = _parse_cookie_pairs('sessionid="abc"; broken; odin_tt=xyz; UIFID=deadbeef')
        self.assertEqual(pairs.get("sessionid"), "abc")
        self.assertEqual(pairs.get("odin_tt"), "xyz")
        self.assertEqual(pairs.get("UIFID"), "deadbeef")

    def test_check_fields_reports_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            result = check_cookie_fields("douyin", "sessionid=abc; odin_tt=xyz;")
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("sessionid_ss", result["missing"])
        self.assertIn("passport_csrf_token", result["missing"])
        self.assertIn("UIFID", result["missing"])

    def test_check_fields_ok_with_uifid_in_cookie(self):
        cookie = "sessionid=a; sessionid_ss=b; passport_csrf_token=c; odin_tt=d; UIFID=e"
        with patch.dict(os.environ, {}, clear=True):
            result = check_cookie_fields("douyin", cookie)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["missing"], [])

    def test_check_fields_ok_with_uifid_env(self):
        cookie = "sessionid=a; sessionid_ss=b; passport_csrf_token=c; odin_tt=d"
        with patch.dict(os.environ, {"DOUYIN_UIFID": "env-uifid-value"}, clear=True):
            result = check_cookie_fields("douyin", cookie)
        self.assertEqual(result["status"], "ok")

    def test_check_fields_empty(self):
        result = check_cookie_fields("douyin", "")
        self.assertEqual(result["status"], "empty")

    def test_weibo_accepts_sub_field(self):
        result = check_cookie_fields("weibo", "SUB=token123;")
        self.assertEqual(result["status"], "ok")

    def test_xiaohongshu_has_no_required_fields(self):
        result = check_cookie_fields("xiaohongshu", "a1=anything")
        self.assertEqual(result["status"], "ok")

    def test_cookie_value_prefers_database_over_env(self):
        def fake_setting(name, default=None):
            return "sessionid=db;" if name == "cookie_douyin" else default

        with patch("src.utils.cookie_registry.setting", side_effect=fake_setting):
            with patch.dict(os.environ, {"DOUYIN_COOKIE": "sessionid=env;"}, clear=True):
                self.assertEqual(get_cookie_value("douyin"), "sessionid=db;")
                self.assertEqual(get_cookie_source("douyin"), "database")

    def test_cookie_source_env_and_none(self):
        with patch("src.utils.cookie_registry.setting", return_value=None):
            with patch.dict(os.environ, {"DOUBAO_COOKIE": "sessionid_ss=env;"}, clear=True):
                self.assertEqual(get_cookie_source("doubao"), "env")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(get_cookie_source("doubao"), "none")

    def test_passive_signal_counts_cookie_errors(self):
        class Row(dict):
            def __getitem__(self, key):
                return dict.__getitem__(self, key)

        class FakeDB:
            def __init__(self):
                self.params = None
                self.sql = None

            def execute(self, sql, params=None):
                self.sql, self.params = sql, params
                return self

            def fetchone(self):
                return Row(total=10, cookie_required=7)

        fake = FakeDB()
        with patch("src.utils.cookie_registry.get_db", return_value=fake):
            signal = passive_signal("douyin")
        self.assertEqual(signal, {"total": 10, "cookie_required": 7})
        self.assertIn("抖音", fake.params)
        self.assertIn("COOKIE_REQUIRED", fake.sql)

    def test_overview_serializes_and_sorts(self):
        class FakeDB:
            def __init__(self):
                self.sql = ""

            def execute(self, sql, params=None):
                self.sql = sql
                return self

            def fetchone(self):
                if "cookie_probe_records" in self.sql:
                    return None  # 无历史探针记录
                return {"total": 0, "cookie_required": 0}

            def fetchall(self):
                return []

        with patch("src.utils.cookie_registry.get_db", return_value=FakeDB()), \
             patch("src.utils.cookie_registry.setting", return_value=None), \
             patch.dict(os.environ, {}, clear=True):
            overview = build_overview()
        types = [item["platform_key"] for item in overview["entries"]]
        self.assertEqual(len(types), len(COOKIE_REGISTRY))
        # 强制型在前
        severities = [item["severity"] for item in overview["entries"]]
        self.assertEqual(severities[0], "critical")
        self.assertEqual(severities[-1], "fallback")
        self.assertEqual(overview["summary"]["not_configured"], len(COOKIE_REGISTRY))
        finding = build_finding(overview)
        self.assertIsNotNone(finding)
        # 必须可 JSON 序列化（模板与 API 共用）
        json.dumps(overview, ensure_ascii=False)


    def test_run_auto_check_records_only_anomalies(self):
        calls = []

        def fake_derive(key):
            calls.append(key)
            return {"douyin": "stale", "weibo": "incomplete"}.get(key, "healthy")

        def fake_fields(key):
            return {"douyin": {"status": "ok", "missing": []},
                    "weibo": {"status": "incomplete", "missing": ["SUB"]},
                    "doubao": {"status": "ok", "missing": []}}[key]

        with patch.dict(os.environ, {"DOUYIN_COOKIE": "a=1", "WEIBO_COOKIE": "a=1", "DOUBAO_COOKIE": "a=1"}, clear=True), \
             patch("src.utils.cookie_registry.get_db", return_value=_SilentDB()), \
             patch("src.utils.cookie_registry.setting", return_value=None), \
             patch("src.utils.cookie_registry._derive_status", side_effect=fake_derive), \
             patch("src.utils.cookie_registry.check_cookie_fields", side_effect=fake_fields):
            findings = run_auto_check()
        # 仅异常平台写记录，healthy 不刷屏。
        self.assertEqual(len(findings), 2)
        self.assertTrue(any("抖音" in f for f in findings))
        self.assertTrue(any("SUB" in f for f in findings))


class _SilentDB:
    """吞掉 record_probe 写入的假 DB。"""

    def execute(self, sql, params=None):
        return self

    def commit(self):
        pass


if __name__ == "__main__":
    unittest.main()
