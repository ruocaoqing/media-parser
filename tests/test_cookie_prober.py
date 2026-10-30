"""Cookie 双跑对照探针测试：全部 mock 解析链路，不出网。"""

import unittest
from unittest.mock import patch

from src.utils import cookie_registry
from src.utils.cookie_prober import _canary_url, _judge, _run_parse, probe_platform
from src.utils.cookie_samples import default_probe_sample


def _ok_result(live_count=0, image_count=3):
    return {
        "ok": True,
        "reason": "OK",
        "detail": {"has_video": True, "image_count": image_count, "live_count": live_count},
        "live_count": live_count,
    }


def _fail_result(reason="MEDIA_NOT_FOUND"):
    return {"ok": False, "reason": reason, "detail": {}, "live_count": 0}


class JudgeMatrixTest(unittest.TestCase):
    def test_both_ok_no_gain(self):
        status, detail = _judge("douyin", _ok_result(0), _ok_result(0))
        self.assertEqual(status, "unverified")
        self.assertEqual(detail["signal"], "BOTH_OK")

    def test_cookie_gain_via_video_or_audio(self):
        for key in ("has_video", "has_audio", "image_count"):
            with self.subTest(key=key):
                with_result, without_result = _ok_result(), _ok_result()
                with_result["detail"][key] = 1
                without_result["detail"][key] = 0
                self.assertEqual(_judge("doubao", with_result, without_result)[0], "healthy")

    def test_cookie_gain_via_live_count(self):
        status, detail = _judge("douyin", _ok_result(live_count=2), _ok_result(0))
        self.assertEqual(status, "healthy")
        self.assertEqual(detail["signal"], "COOKIE_GAIN")

    def test_with_ok_without_fail(self):
        status, detail = _judge("douyin", _ok_result(), _fail_result())
        self.assertEqual(status, "healthy")
        self.assertEqual(detail["signal"], "COOKIE_GAIN")

    def test_with_fail_without_ok_is_stale(self):
        status, detail = _judge("douyin", _fail_result(), _ok_result())
        self.assertEqual(status, "stale")
        self.assertEqual(detail["signal"], "COOKIE_BROKEN")

    def test_both_failed_is_probe_failed(self):
        status, detail = _judge("douyin", _fail_result(), _fail_result("REDIRECT_FAILED"))
        self.assertEqual(status, "probe_failed")
        self.assertEqual(detail["signal"], "BOTH_FAILED")


class ProbePlatformTest(unittest.TestCase):
    def setUp(self):
        # 每个用例重置防抖时间戳，避免用例间串扰。
        import src.utils.cookie_prober as prober
        prober._last_probe_at.clear()

    def test_unregistered_platform(self):
        result = probe_platform("nosuch")
        self.assertEqual(result["status"], "probe_failed")

    def test_not_configured_short_circuits(self):
        with patch("src.utils.cookie_registry.get_cookie_source", return_value="none"):
            result = probe_platform("douyin")
        self.assertEqual(result["status"], "not_configured")

    def test_debounce_blocks_second_call(self):
        def fake_parse(url, platforms):
            return _ok_result()

        with patch("src.utils.cookie_registry.get_cookie_source", return_value="database"), \
             patch("src.utils.cookie_prober._canary_url", return_value="https://v.douyin.com/x/"), \
             patch.object(cookie_registry, "record_probe"), \
             patch("src.utils.cookie_prober._run_parse", side_effect=fake_parse):
            self.assertEqual(probe_platform("douyin")["status"], "unverified")
            # 同平台 5 分钟内第二次探针被防抖拦截，不执行解析。
            second = probe_platform("douyin")
        self.assertEqual(second["status"], "debounced")

    def test_full_probe_records_and_judges(self):
        calls = []

        def fake_parse(url, platforms):
            calls.append(url)
            # 第一路（带 Cookie）失败、第二路（无 Cookie 对照）成功 → stale 实锤。
            return _fail_result() if len(calls) == 1 else _ok_result()

        with patch("src.utils.cookie_registry.get_cookie_source", return_value="database"), \
             patch("src.utils.cookie_prober._canary_url", return_value="https://v.douyin.com/x/"), \
             patch("src.utils.cookie_prober._run_parse", side_effect=fake_parse), \
             patch.object(cookie_registry, "record_probe") as record:
            result = probe_platform("douyin")
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["detail"]["signal"], "COOKIE_BROKEN")
        self.assertEqual(len(calls), 2)
        record.assert_called_once()
        self.assertEqual(record.call_args[0][1], "stale")

    def test_no_cookie_shadowing_takes_effect(self):
        """_no_cookie 上下文内 cookie_manager 读取被遮蔽。"""
        import src.utils.cookie_manager as cm
        from src.utils.cookie_prober import _no_cookie

        from concurrent.futures import ThreadPoolExecutor
        from src.utils.cookie_manager import get_platform_cookie as imported_reader

        with patch("src.db.setting", return_value="sessionid=real;"):
            with _no_cookie():
                self.assertEqual(imported_reader("douyin"), "")
                # 并发解析仍能读取自身凭据。
                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertEqual(pool.submit(imported_reader, "douyin").result(), "sessionid=real;")
            self.assertEqual(cm.get_platform_cookie("douyin"), "sessionid=real;")

    def test_no_cookie_restores_context_on_exception(self):
        from src.utils.cookie_manager import cookies_suppressed
        from src.utils.cookie_prober import _no_cookie
        with self.assertRaises(RuntimeError):
            with _no_cookie():
                raise RuntimeError("test")
        self.assertFalse(cookies_suppressed())

    def test_no_cookie_disables_independent_douyin_uifid(self):
        import os
        from src.parsers.douyin_parser import DouyinParser
        from src.utils.cookie_prober import _no_cookie
        parser = object.__new__(DouyinParser)
        with patch.dict(os.environ, {"DOUYIN_UIFID": "configured-uifid"}):
            self.assertEqual(parser._get_uifid(), "configured-uifid")
            with _no_cookie():
                self.assertEqual(parser._get_uifid(), "")

    def test_parse_exception_is_a_single_route_failure(self):
        with patch("src.utils.cookie_prober.WebFetcher.fetch_redirect_url", side_effect=RuntimeError("test")):
            result = _run_parse("https://v.douyin.com/x/", {"抖音"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "PARSE_EXCEPTION")

    def test_cover_only_is_not_success_but_video_list_is(self):
        from unittest.mock import Mock
        parser = Mock()
        parser.get_real_video_url.return_value = None
        parser.get_video_list.return_value = []
        parser.get_cover_photo_url.return_value = "https://example.com/cover.jpg"
        parser.get_image_list.return_value = []
        parser.get_audio_url.return_value = None
        with patch("src.utils.cookie_prober.WebFetcher.fetch_redirect_url", return_value="url"), \
             patch("src.utils.cookie_prober.UrlParser.get_platform", return_value="抖音"), \
             patch("src.utils.cookie_prober.UrlParser.extract_video_address", return_value="url"), \
             patch("src.utils.cookie_prober.ParserFactory.create_parser", return_value=parser):
            self.assertFalse(_run_parse("url", {"抖音"})["ok"])
            parser.get_video_list.return_value = [{"url": "https://example.com/video.mp4"}]
            self.assertTrue(_run_parse("url", {"抖音"})["ok"])


class DefaultSampleTest(unittest.TestCase):
    def test_explicit_defaults_cover_registered_credentials_once(self):
        import json
        from src.utils.cookie_samples import SAMPLES_PATH
        cases = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))["cases"]
        marked = [case for case in cases if "cookie_probe" in case]
        keys = [case["cookie_probe"]["platform_key"] for case in marked]
        self.assertEqual(set(keys), set(cookie_registry.COOKIE_REGISTRY))
        self.assertEqual(len(keys), len(set(keys)))
        for case in marked:
            entry = cookie_registry.get_entry(case["cookie_probe"]["platform_key"])
            self.assertIn(case["platform"], entry["log_names"])
            self.assertTrue(case["cookie_probe"]["capability"])
        self.assertEqual(default_probe_sample("yuanbao")["platform"], "视频号")
        self.assertIn("feed_id=", default_probe_sample("pinduoduo")["url"])
        self.assertIn("live_media", default_probe_sample("douyin")["media_types"])

    def test_previous_success_without_gain_is_unverified(self):
        from src.utils.cookie_registry import _derive_status
        with patch.object(cookie_registry, "get_cookie_source", return_value="database"), \
             patch.object(cookie_registry, "check_cookie_fields", return_value={"status": "ok"}), \
             patch.object(cookie_registry, "passive_signal", return_value={"total": 0}), \
             patch.object(cookie_registry, "latest_probe") as latest:
            for trigger, signal in (("field_check", None), ("probe", "BOTH_OK"), ("probe", "COOKIE_GAIN")):
                latest.return_value = {"status": "healthy", "trigger_type": trigger, "detail": {"signal": signal}}
                self.assertEqual(_derive_status("douyin"), "healthy" if signal == "COOKIE_GAIN" else "unverified")

    def test_custom_link_precedes_sample(self):
        with patch("src.utils.cookie_prober.setting", return_value=" https://custom.example/test "):
            self.assertEqual(_canary_url("douyin"), "https://custom.example/test")
        with patch("src.utils.cookie_prober.setting", return_value=""):
            self.assertEqual(_canary_url("douyin"), default_probe_sample("douyin")["url"])
        self.assertIsNone(_canary_url("unknown"))

    def test_missing_or_invalid_sample_file_allows_custom_configuration(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        with TemporaryDirectory() as directory:
            path = Path(directory) / "samples.json"
            with patch("src.utils.cookie_samples.SAMPLES_PATH", path):
                self.assertIsNone(default_probe_sample("douyin"))
                path.write_text("invalid json")
                self.assertIsNone(default_probe_sample("douyin"))

    def test_run_parse_platform_mismatch(self):
        from utils.web_fetcher import UrlParser

        with patch("src.utils.cookie_prober.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/1"), \
             patch.object(UrlParser, "get_platform", return_value="快手"):
            result = _run_parse("https://v.douyin.com/x/", {"抖音"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "PLATFORM_MISMATCH")


if __name__ == "__main__":
    unittest.main()
