import unittest
from unittest.mock import patch
from utils.web_fetcher import UrlParser
from src.parser_factory import ParserFactory
from src.parsers.fanqie_parser import FanqieParser
from utils.html_video_extractor import HtmlVideoExtractor


MOCK_NOVEL_HTML = """
<!doctype html>
<html>
<head>
    <meta data-react-helmet="true" name="og:description" content="测试短剧描述标题"/>
    <meta data-react-helmet="true" name="og:image" content="https://p3-novel.byteimg.com/cover.image"/>
    <meta data-react-helmet="true" name="og:url" content="https://v3-share.qznovel.com/video.mp4?mime_type=video_mp4"/>
</head>
<body>
    <script>
        window._ROUTER_DATA = {
            "loaderData": {
                "video-animation-share_page": {
                    "pageData": {
                        "series_data": {
                            "title": "护镖人之无敌镖人！",
                            "play_url": "https:\\u002F\\u002Fv3-share.qznovel.com\\u002Freal_play.mp4"
                        }
                    }
                }
            }
        };
    </script>
</body>
</html>
"""

# 现在的分享落地页只给 30 秒试看:play_url 里带 &start=0&end=30
MOCK_PREVIEW_HTML = """
<!doctype html>
<html>
<body>
    <script>
        window._ROUTER_DATA = {
            "loaderData": {
                "video-animation-share_page": {
                    "pageData": {
                        "series_data": {
                            "title": "全家把我当乡下老太，都吓傻了",
                            "play_url": "https://v3-share.qznovel.com\\u002Fpreview.mp4?a=8662&start=0&end=30"
                        },
                        "used_chapter_id": "7679061418129247257"
                    },
                    "linkParams": {
                        "schemeParams": {"vid": "7679061418129247257", "video_id": "7679018384272395289"}
                    }
                }
            }
        };
    </script>
</body>
</html>
"""

MOCK_PLAYER_HTML = """
<!doctype html>
<html>
<head>
    <script type="application/ld+json">
        {"@type": "VideoObject", "name": "全家把我当乡下老太，都吓傻了 第1集",
         "contentUrl": "https://v3-hgweb.qznovelvod.com\\u002Ffull.mp4?mime_type=video_mp4",
         "duration": "PT2M51S"}
    </script>
</head>
</html>
"""


class FanqieParserTest(unittest.TestCase):

    def test_platform_recognition(self):
        self.assertEqual(UrlParser.get_platform("https://novelquickapp.com/s/xCkhRnNOiTc/"), "红果短剧")
        self.assertEqual(UrlParser.get_platform("https://qznovel.com/video.mp4"), "红果短剧")
        self.assertEqual(UrlParser.get_platform("https://fqnovel.com/video.mp4"), "番茄小说")
        self.assertEqual(UrlParser.get_platform("https://zlink.fqnovel.com/dhVGe"), "番茄小说")
        self.assertEqual(UrlParser.get_platform("https://changdunovel.com/t/byybBzZfKbg/"), "番茄小说")
        self.assertEqual(UrlParser.get_platform("https://kylin.hainanyuyue.com/s/bR1qzzEd1A0/"), "红果漫剧")
        self.assertEqual(UrlParser.get_platform("https://hainanyuyue.com/"), "红果漫剧")

    def test_parser_factory_registration(self):
        cls_hongguo = ParserFactory.get_parser_class("红果短剧")
        cls_fanqie = ParserFactory.get_parser_class("番茄小说")
        cls_hongguo_comic = ParserFactory.get_parser_class("红果漫剧")
        self.assertEqual(cls_hongguo, FanqieParser)
        self.assertEqual(cls_fanqie, FanqieParser)
        self.assertEqual(cls_hongguo_comic, FanqieParser)

    def test_html_video_extractor(self):
        res = HtmlVideoExtractor.parse_page(MOCK_NOVEL_HTML)
        self.assertEqual(res['title'], "护镖人之无敌镖人！")
        self.assertEqual(res['video_url'], "https://v3-share.qznovel.com/real_play.mp4")
        self.assertEqual(res['cover_url'], "https://p3-novel.byteimg.com/cover.image")

    @patch.object(FanqieParser, 'fetch_html_content', return_value=MOCK_NOVEL_HTML)
    def test_fanqie_parser_execution(self, mock_fetch):
        parser = FanqieParser("https://novelquickapp.com/s/xCkhRnNOiTc/")
        self.assertEqual(parser.get_title_content(), "护镖人之无敌镖人！")
        self.assertEqual(parser.get_real_video_url(), "https://v3-share.qznovel.com/real_play.mp4")
        self.assertEqual(parser.get_cover_photo_url(), "https://p3-novel.byteimg.com/cover.image")

    @patch.object(FanqieParser, 'fetch_player_html', return_value=MOCK_PLAYER_HTML)
    @patch.object(FanqieParser, 'fetch_html_content', return_value=MOCK_PREVIEW_HTML)
    def test_preview_link_replaced_by_full_video(self, mock_fetch, mock_player):
        """分享页给试看链接时,要去官网播放器换成完整分集链接。"""
        parser = FanqieParser("https://novelquickapp.com/s/wQ-MK1vuras/")
        self.assertEqual(parser.get_real_video_url(), "https://v3-hgweb.qznovelvod.com/full.mp4?mime_type=video_mp4")
        self.assertEqual(parser.get_title_content(), "全家把我当乡下老太，都吓傻了")
        mock_player.assert_called_once_with(
            "https://hongguoduanju.com/player/7679018384272395289/7679061418129247257")

    @patch.object(FanqieParser, 'fetch_player_html', side_effect=RuntimeError("404"))
    @patch.object(FanqieParser, 'fetch_html_content', return_value=MOCK_PREVIEW_HTML)
    def test_fallback_to_share_url_when_player_fails(self, mock_fetch, mock_player):
        """官网播放器拿不到时,不能把视频解析弄失败,继续用分享页那条。"""
        parser = FanqieParser("https://novelquickapp.com/s/wQ-MK1vuras/")
        self.assertTrue(parser.get_real_video_url().endswith("start=0&end=30"))


if __name__ == '__main__':
    unittest.main()
