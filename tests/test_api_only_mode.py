import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import create_app
from src.db import get_db


class ApiOnlyModeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_api_only.db")
        self.app = create_app({
            "TESTING": True,
            "API_ONLY": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE": self.db_path,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _create_mock_parser():
        parser = Mock()
        parser.get_title_content.return_value = "测试标题"
        parser.get_description.return_value = "测试描述"
        parser.get_real_video_url.return_value = "https://example.com/video.mp4"
        parser.get_video_list.return_value = []
        parser.get_cover_photo_url.return_value = "https://example.com/cover.jpg"
        parser.get_author_info.return_value = {"name": "创作者"}
        parser.get_image_list.return_value = []
        parser.get_audio_url.return_value = None
        parser.get_subtitles.return_value = None
        return parser

    def test_web_routes_disabled(self):
        """测试 Web UI / Admin / Auth 路由在 API_ONLY 模式下返回 404"""
        res_index = self.client.get("/")
        self.assertEqual(res_index.status_code, 404)

        res_admin = self.client.get("/admin")
        self.assertEqual(res_admin.status_code, 404)

        res_portal = self.client.get("/portal")
        self.assertEqual(res_portal.status_code, 404)

        res_login = self.client.get("/auth/login")
        self.assertEqual(res_login.status_code, 404)

    def test_creation_config_is_available_in_api_only_mode(self):
        response = self.client.get("/api/v1/wx/creation-config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"], {"local_images_enabled": False})

    def test_health_check_works(self):
        """测试健康检查接口正常返回 200"""
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    @patch("src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/7341234567890123456")
    @patch("src.api.parse.ParserFactory.create_parser")
    def test_api_v1_parse_get_without_api_key(self, mock_factory, mock_fetch):
        """测试 GET /api/v1/parse 免 API Key 直接调用"""
        mock_factory.return_value = self._create_mock_parser()

        res = self.client.get("/api/v1/parse?url=https://v.douyin.com/test1234/")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["succ"])
        self.assertEqual(data["data"]["title"], "测试标题")
        self.assertEqual(data["data"]["video_url"], "https://example.com/video.mp4")

    @patch("src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/7341234567890123456")
    @patch("src.api.parse.ParserFactory.create_parser")
    def test_api_v1_parse_post_json_without_api_key(self, mock_factory, mock_fetch):
        """测试 POST /api/v1/parse 免 API Key 并支持 JSON payload 直接调用"""
        mock_factory.return_value = self._create_mock_parser()

        res = self.client.post("/api/v1/parse", json={"url": "https://v.douyin.com/test1234/"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["succ"])
        self.assertEqual(data["data"]["title"], "测试标题")

    @patch("src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/7341234567890123456")
    @patch("src.api.parse.ParserFactory.create_parser")
    def test_api_parse_post_without_demo_limits(self, mock_factory, mock_fetch):
        """测试 POST /api/parse 在 API_ONLY 模式下免 demo 限流与校验"""
        mock_factory.return_value = self._create_mock_parser()

        self.app.testing = False
        for i in range(10):
            res = self.client.post("/api/parse", json={"text": f"https://v.douyin.com/test{i}/"})
            self.assertEqual(res.status_code, 200, f"第 {i+1} 次请求失败")
            self.assertTrue(res.get_json()["succ"])

    @patch("src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/7341234567890123456")
    @patch("src.api.parse.ParserFactory.create_parser")
    def test_api_parse_get_support(self, mock_factory, mock_fetch):
        """测试 GET /api/parse 同样支持直接传 query 参数解析"""
        mock_factory.return_value = self._create_mock_parser()

        res = self.client.get("/api/parse?url=https://v.douyin.com/test1234/")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["succ"])
        self.assertEqual(data["data"]["title"], "测试标题")

    @patch("src.api.parse.WebFetcher.fetch_redirect_url", return_value="https://www.douyin.com/video/7341234567890123456")
    @patch("src.api.parse.ParserFactory.create_parser")
    def test_no_request_logs_in_api_only(self, mock_factory, mock_fetch):
        """测试 API_ONLY 模式下不向 request_logs 写入日志"""
        mock_factory.return_value = self._create_mock_parser()

        self.client.get("/api/v1/parse?url=https://v.douyin.com/test1234/")
        self.client.post("/api/parse", json={"text": "https://v.douyin.com/test1234/"})

        with self.app.app_context():
            db = get_db()
            count = db.execute("SELECT COUNT(*) as cnt FROM request_logs").fetchone()["cnt"]
            self.assertEqual(count, 0)
