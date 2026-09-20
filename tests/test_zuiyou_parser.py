import unittest
from unittest.mock import Mock, patch

from src.parsers.zuiyou_parser import ZuiyouParser


class ZuiyouParserTest(unittest.TestCase):
    @patch("src.parsers.zuiyou_parser.random.choice", return_value="test-agent")
    def test_extracts_title_and_author_from_post_detail(self, _user_agent):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "post": {
                    "content": "测试最右作品",
                    "member": {
                        "id": 42,
                        "name": "测试作者",
                        "avatar_urls": {"origin": {"urls": ["https://image.example.com/avatar.jpg"]}},
                    },
                }
            }
        }

        with patch("requests.Session.post", return_value=response) as post:
            parser = ZuiyouParser("https://share.xiaochuankeji.cn/hybrid/share/post?pid=123")

        post.assert_called_once_with(
            "https://share.xiaochuankeji.cn/planck/share/post/detail_h5",
            headers=parser.headers,
            json={"h_av": "5.2.13.011", "pid": 123},
            timeout=10,
        )
        self.assertIsNone(parser.get_title_content())
        self.assertEqual(parser.get_description(), "测试最右作品")
        self.assertEqual(
            parser.get_author_info(),
            {"nickname": "测试作者", "author_id": "42", "avatar": "https://image.example.com/avatar.jpg"},
        )

    def test_extracts_images_and_cover_for_photo_post(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "post": {
                    "content": "测试图文帖子",
                    "imgs": [
                        {
                            "id": 1001,
                            "urls": {
                                "origin": {"urls": ["https://image.example.com/origin1.jpg"]},
                                "360": {"urls": ["https://image.example.com/thumb1.jpg"]},
                            },
                        },
                        {
                            "id": 1002,
                            "urls": {
                                "540": {"urls": ["https://image.example.com/540_2.jpg"]},
                            },
                        },
                    ],
                    "member": {"id": 1, "name": "作者"},
                }
            }
        }

        with patch("requests.Session.post", return_value=response):
            parser = ZuiyouParser("https://share.xiaochuankeji.cn/hybrid/share/post?pid=123")

        self.assertIsNone(parser.get_real_video_url())
        self.assertEqual(
            parser.get_image_list(),
            ["https://image.example.com/origin1.jpg", "https://image.example.com/540_2.jpg"],
        )
        self.assertEqual(parser.get_cover_photo_url(), "https://image.example.com/origin1.jpg")

    def test_extracts_video_and_images_for_mixed_post(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "post": {
                    "content": "视频图集混合帖",
                    "videos": {
                        "2001": {"dur": 84, "url": "https://video.example.com/main.mp4"},
                    },
                    "imgs": [
                        {
                            "id": 2001,
                            "video": 1,
                            "urls": {"360": {"urls": ["https://image.example.com/frame_2001.jpg"]}},
                        },
                        {
                            "id": 2002,
                            "urls": {"origin": {"urls": ["https://image.example.com/img_2002.jpg"]}},
                        },
                        {
                            "id": 2003,
                            "urls": {"origin": {"urls": ["https://image.example.com/img_2003.jpg"]}},
                        },
                    ],
                    "member": {"id": 1, "name": "作者"},
                }
            }
        }

        with patch("requests.Session.post", return_value=response):
            parser = ZuiyouParser("https://share.xiaochuankeji.cn/hybrid/share/post?pid=425713568")

        self.assertEqual(parser.get_real_video_url(), "https://video.example.com/main.mp4")
        self.assertEqual(
            parser.get_image_list(),
            ["https://image.example.com/img_2002.jpg", "https://image.example.com/img_2003.jpg"],
        )
        self.assertEqual(parser.get_cover_photo_url(), "https://image.example.com/frame_2001.jpg")

    def test_extracts_live_photos_and_static_images(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "post": {
                    "content": "图片+实况混合帖",
                    "videos": {
                        "3002": {"dur": 2, "url": "https://video.example.com/live_3002.mp4"},
                        "3003": {"dur": 3, "url": "https://video.example.com/live_3003.mp4"},
                    },
                    "imgs": [
                        {
                            "id": 3001,
                            "urls": {"origin": {"urls": ["https://image.example.com/static_3001.jpg"]}},
                        },
                        {
                            "id": 3002,
                            "video": 1,
                            "urls": {"origin": {"urls": ["https://image.example.com/live_cover_3002.jpg"]}},
                        },
                        {
                            "id": 3003,
                            "video": 1,
                            "urls": {"origin": {"urls": ["https://image.example.com/live_cover_3003.jpg"]}},
                        },
                    ],
                    "member": {"id": 1, "name": "作者"},
                }
            }
        }

        with patch("requests.Session.post", return_value=response):
            parser = ZuiyouParser("https://share.xiaochuankeji.cn/hybrid/share/post?pid=425444752")

        self.assertIsNone(parser.get_real_video_url())
        self.assertEqual(
            parser.get_image_list(),
            [
                "https://image.example.com/static_3001.jpg",
                {
                    "url": "https://image.example.com/live_cover_3002.jpg",
                    "live_photo_url": "https://video.example.com/live_3002.mp4",
                },
                {
                    "url": "https://image.example.com/live_cover_3003.jpg",
                    "live_photo_url": "https://video.example.com/live_3003.mp4",
                },
            ],
        )

    def test_pure_video_post_extracts_video_and_empty_image_list(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "data": {
                "post": {
                    "content": "纯视频帖",
                    "videos": {
                        "4001": {"dur": 15, "url": "https://video.example.com/video_4001.mp4"},
                    },
                    "imgs": [
                        {
                            "id": 4001,
                            "video": 1,
                            "urls": {"origin": {"urls": ["https://image.example.com/cover_4001.jpg"]}},
                        },
                    ],
                    "member": {"id": 1, "name": "作者"},
                }
            }
        }

        with patch("requests.Session.post", return_value=response):
            parser = ZuiyouParser("https://share.xiaochuankeji.cn/hybrid/share/post?pid=423835942")

        self.assertEqual(parser.get_real_video_url(), "https://video.example.com/video_4001.mp4")
        self.assertEqual(parser.get_image_list(), [])
        self.assertEqual(parser.get_cover_photo_url(), "https://image.example.com/cover_4001.jpg")

    def test_skips_request_without_post_id(self):
        with patch("requests.Session.post") as post:
            parser = ZuiyouParser("https://share.xiaochuankeji.cn/hybrid/share/post")

        post.assert_not_called()
        self.assertEqual(parser.data, {})


if __name__ == "__main__":
    unittest.main()
