import unittest
from unittest.mock import Mock, patch

from src.parsers.zhihu_parser import ZhihuParser


class ZhihuParserTest(unittest.TestCase):
    def test_maps_video_pin_playlist(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "excerpt_title": "测试知乎视频",
            "content_html": "<p>视频正文</p>",
            "author": {"name": "测试作者", "id": "author-id", "avatar_url": "https://image.example.com/avatar.jpg"},
            "content": [{
                "type": "video",
                "thumbnail": "https://image.example.com/cover.jpg",
                "playlist": [
                    {"quality": "sd", "bitrate": 100, "url": "https://video.example.com/sd.mp4"},
                    {"quality": "hd", "bitrate": 200, "url": "https://video.example.com/hd.mp4"},
                ],
            }],
        }
        with patch("requests.Session.get", return_value=response) as get:
            parser = ZhihuParser("https://www.zhihu.com/pin/2066168388699807826")

        self.assertEqual(parser.get_real_video_url(), "https://video.example.com/hd.mp4")
        self.assertEqual(parser.get_cover_photo_url(), "https://image.example.com/cover.jpg")
        self.assertEqual(parser.get_title_content(), "测试知乎视频")
        self.assertEqual(parser.get_author_info()["nickname"], "测试作者")
        self.assertEqual(get.call_args.args[0], "https://api.zhihu.com/pins/2066168388699807826")

    def test_article_title_image_cover_and_image_list(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "title": "知乎专栏测试文章",
            "title_image": "https://picx.zhimg.com/v2-cover123_b.jpg",
            "image_url": "",
            "content": "<p>正文内容</p><img src=\"https://picx.zhimg.com/v2-body456.jpg\">",
            "author": {"name": "测试作者", "id": "123456", "avatar_url": "https://picx.zhimg.com/avatar.jpg"},
        }
        with patch("requests.Session.get", return_value=response) as get:
            parser = ZhihuParser("https://zhuanlan.zhihu.com/p/262266851")

        self.assertEqual(parser.get_cover_photo_url(), "https://picx.zhimg.com/v2-cover123_b.jpg")
        self.assertEqual(parser.get_image_list(), [
            "https://picx.zhimg.com/v2-cover123_b.jpg",
            "https://picx.zhimg.com/v2-body456.jpg",
        ])
        self.assertEqual(parser.get_title_content(), "知乎专栏测试文章")
        self.assertEqual(parser.get_author_info()["nickname"], "测试作者")
        self.assertEqual(get.call_args.args[0], "https://api.zhihu.com/articles/262266851")

    def test_article_without_body_images_preserves_cover_in_image_list(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "title": "纯文本带题图专栏文章",
            "title_image": "https://picx.zhimg.com/v2-cover999.jpg",
            "content": "<p>只有文字，没有正文插图</p>",
            "author": {"name": "测试作者", "id": "123456"},
        }
        with patch("requests.Session.get", return_value=response):
            parser = ZhihuParser("https://www.zhihu.com/p/262266851")

        self.assertEqual(parser.get_cover_photo_url(), "https://picx.zhimg.com/v2-cover999.jpg")
        self.assertEqual(parser.get_image_list(), ["https://picx.zhimg.com/v2-cover999.jpg"])

    def test_html_ssr_fallback_extracts_article_cover(self):
        html = """
        <!DOCTYPE html><html><head>
        <meta property="og:title" content="SSR标题" />
        <meta property="og:image" content="https://picx.zhimg.com/v2-ssr-cover.jpg" />
        <meta property="og:description" content="SSR摘要" />
        </head><body>
        <script id="js-initialData" type="text/json">
        {"initialState": {"entities": {"articles": {"262266851": {
            "title": "SSR测试标题",
            "titleImage": "https://picx.zhimg.com/v2-ssr-cover.jpg",
            "content": "<p>SSR正文</p>",
            "author": {"name": "SSR作者", "urlToken": "ssr-user", "avatarUrl": "https://picx.zhimg.com/ssr-avatar.jpg"}
        }}}}}
        </script>
        </body></html>
        """
        api_resp = Mock()
        api_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        html_resp = Mock()
        html_resp.status_code = 200
        html_resp.text = html

        with patch("requests.Session.get", side_effect=[api_resp, html_resp]):
            parser = ZhihuParser("https://zhuanlan.zhihu.com/p/262266851")

        self.assertEqual(parser.get_title_content(), "SSR测试标题")
        self.assertEqual(parser.get_cover_photo_url(), "https://picx.zhimg.com/v2-ssr-cover.jpg")
        self.assertEqual(parser.get_image_list(), ["https://picx.zhimg.com/v2-ssr-cover.jpg"])
        self.assertEqual(parser.get_author_info()["nickname"], "SSR作者")
