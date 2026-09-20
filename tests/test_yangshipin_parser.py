import unittest
from unittest.mock import MagicMock, patch

from src.parsers.yangshipin_parser import YangshipinParser


class YangshipinParserTest(unittest.TestCase):
    def test_parses_portrait_video_successfully(self):
        fake_html = """
        <html>
        <head><title>央视频</title></head>
        <body>
            <script>
            window.__STATE_portrait_video__ = {
                "payloads": {
                    "videoDataList": {
                        "items": [
                            {
                                "videoData": {
                                    "vid": "l00005817wl",
                                    "title": "5次助攻对4次助攻！巅峰对决",
                                    "shareItem": {
                                        "shareImgUrl": "https://jietufengmian.yangshipin.cn/cover1.jpg"
                                    },
                                    "detailFollowItem": {
                                        "actorItem": {
                                            "nickName": {"text": "奥运来了"},
                                            "headUrl": "https://mpuser.ysp.cctv.cn/avatar1.jpeg"
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            };
            </script>
        </body>
        </html>
        """

        def mock_fetch(self, vid):
            self.video_url = "https://mp4playcloud-cdn.ysp.cctv.cn/l00005817wl.mp4?vkey=mock"

        with patch.object(YangshipinParser, "fetch_html_content", return_value=fake_html), \
             patch.object(YangshipinParser, "_fetch_video_url_by_vid", side_effect=mock_fetch, autospec=True):
            parser = YangshipinParser("https://m.yangshipin.cn/portrait_video?vid=l00005817wl")
            self.assertEqual(parser.get_title_content(), "5次助攻对4次助攻！巅峰对决")
            self.assertEqual(parser.get_cover_photo_url(), "https://jietufengmian.yangshipin.cn/cover1.jpg")
            self.assertEqual(
                parser.get_author_info(),
                {"name": "奥运来了", "avatar": "https://mpuser.ysp.cctv.cn/avatar1.jpeg"},
            )
            self.assertEqual(parser.get_real_video_url(), "https://mp4playcloud-cdn.ysp.cctv.cn/l00005817wl.mp4?vkey=mock")
            self.assertEqual(parser.get_image_list(), [])

    def test_parses_landscape_video_successfully(self):
        fake_html = """
        <html>
        <head><title>央视频</title></head>
        <body>
            <script>
            window.__STATE_video__ = {
                "payloads": {
                    "sharevideo": {
                        "vid": "v000007pgfu",
                        "title": "《普法栏目剧》远山的守望",
                        "cover_pic": "https://jietufengmian.yangshipin.cn/cover2.jpg",
                        "om_info": {
                            "title": "社会与法频道"
                        }
                    }
                }
            };
            </script>
        </body>
        </html>
        """

        def mock_fetch(self, vid):
            self.video_url = "https://mp4playcloud-cdn.ysp.cctv.cn/v000007pgfu.mp4?vkey=mock"

        with patch.object(YangshipinParser, "fetch_html_content", return_value=fake_html), \
             patch.object(YangshipinParser, "_fetch_video_url_by_vid", side_effect=mock_fetch, autospec=True):
            parser = YangshipinParser("https://m.yangshipin.cn/video?type=0&vid=v000007pgfu")
            self.assertEqual(parser.get_title_content(), "《普法栏目剧》远山的守望")
            self.assertEqual(parser.get_cover_photo_url(), "https://jietufengmian.yangshipin.cn/cover2.jpg")
            self.assertEqual(parser.get_author_info(), {"name": "社会与法频道", "avatar": None})
            self.assertEqual(parser.get_real_video_url(), "https://mp4playcloud-cdn.ysp.cctv.cn/v000007pgfu.mp4?vkey=mock")
            self.assertEqual(parser.get_image_list(), [])

    def test_ckey_generation_and_playvinfo_flow(self):
        probe_resp = MagicMock()
        probe_resp.text = '({"em":85,"exem":-3,"curTime":1789545734})'

        vinfo_resp = MagicMock()
        vinfo_resp.text = '({"s":"o","vl":{"cnt":1,"vi":[{"fn":"test.mp4","fvkey":"ABC123KEY","ul":{"ui":[{"url":"https://mp4playcloud-cdn.ysp.cctv.cn/"}]}}]}})'

        fake_html = """
        <html><body>
        <script>
        window.__STATE_portrait_video__ = {
            "payloads": {"videoDataList": {"items": [{"videoData": {"vid": "test_vid", "title": "测试视频"}}]}}
        };
        </script>
        </body></html>
        """
        with patch.object(YangshipinParser, "fetch_html_content", return_value=fake_html), \
             patch("requests.Session.get", side_effect=[probe_resp, vinfo_resp]):
            parser = YangshipinParser("https://m.yangshipin.cn/portrait_video?vid=test_vid")
            self.assertEqual(
                parser.get_real_video_url(),
                "https://mp4playcloud-cdn.ysp.cctv.cn/test.mp4?vkey=ABC123KEY&platform=2",
            )
            self.assertEqual(parser.get_image_list(), [])

    def test_follows_meta_refresh_redirect(self):
        meta_html = """
        <!DOCTYPE html>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="0; URL='https://m.yangshipin.cn/portrait_video?vid=5Sqx'"/>
        <title>央视频</title>
        """
        detail_html = """
        <html><body>
            <script>
            window.__STATE_portrait_video__ = {
                "payloads": {
                    "videoDataList": {
                        "items": [
                            {
                                "videoData": {
                                    "title": "跳转后的视频标题",
                                    "shareItem": {"shareImgUrl": "https://jietufengmian.yangshipin.cn/cover3.jpg"}
                                }
                            }
                        ]
                    }
                }
            };
            </script>
        </body></html>
        """
        with patch.object(YangshipinParser, "fetch_html_content", side_effect=[meta_html, detail_html]), \
             patch.object(YangshipinParser, "_fetch_video_url_by_vid", return_value=None):
            parser = YangshipinParser("https://www.yspapp.cn/5Sqx")
            self.assertEqual(parser.get_title_content(), "跳转后的视频标题")
            self.assertEqual(parser.get_cover_photo_url(), "https://jietufengmian.yangshipin.cn/cover3.jpg")
            self.assertEqual(parser.get_image_list(), ["https://jietufengmian.yangshipin.cn/cover3.jpg"])

    def test_parses_article_successfully(self):
        meta_html = """
        <!DOCTYPE html>
        <meta charset="utf-8">
        <meta http-equiv="refresh" content="0; URL='https://m.yangshipin.cn/static/article.html?articleid=e05kmjv3gty29'"/>
        <title>央视频</title>
        """
        article_page_html = "<html><head><title>文章</title></head><body></body></html>"
        mock_article_resp = MagicMock()
        mock_article_resp.status_code = 200
        mock_article_resp.json.return_value = {
            "data": {
                "errCode": 0,
                "head": {
                    "title": "亚运乒乓球签表：上届冠亚军孙颖莎早田希娜同半区",
                    "source": "体坛网",
                    "publishTime": "2026-09-18 21:04",
                    "coverImage": "https://jietufengmian.yangshipin.cn/cover_article.jpg"
                },
                "content": {
                    "content": "<p>体坛周报全媒体原创</p><p>北京时间9月18日晚上，2026爱知-名古屋亚运会乒乓球项目各项签表正式出炉。</p>",
                    "images": [
                        "https://jietufengmian.yangshipin.cn/img1.jpg",
                        "https://jietufengmian.yangshipin.cn/img2.jpg"
                    ]
                }
            },
            "ret": 0
        }

        with patch.object(YangshipinParser, "fetch_html_content", side_effect=[meta_html, article_page_html]), \
             patch("requests.Session.get", return_value=mock_article_resp):
            parser = YangshipinParser("https://www.yspapp.cn/6j3j")
            self.assertEqual(parser.get_title_content(), "亚运乒乓球签表：上届冠亚军孙颖莎早田希娜同半区")
            self.assertEqual(parser.get_cover_photo_url(), "https://jietufengmian.yangshipin.cn/cover_article.jpg")
            self.assertEqual(parser.get_author_info(), {"name": "体坛网", "avatar": None})
            self.assertEqual(
                parser.get_description(),
                "体坛周报全媒体原创\n\n北京时间9月18日晚上，2026爱知-名古屋亚运会乒乓球项目各项签表正式出炉。"
            )
            self.assertEqual(
                parser.get_image_list(),
                [
                    "https://jietufengmian.yangshipin.cn/img1.jpg",
                    "https://jietufengmian.yangshipin.cn/img2.jpg"
                ]
            )
            self.assertIsNone(parser.get_real_video_url())
            self.assertIsNone(parser.get_audio_url())

    def test_parses_article_with_embedded_video_and_audio(self):
        mock_article_resp = MagicMock()
        mock_article_resp.status_code = 200
        mock_article_resp.json.return_value = {
            "data": {
                "errCode": 0,
                "head": {
                    "title": "新闻联播重点快讯",
                    "source": "央视新闻",
                    "coverImage": "https://jietufengmian.yangshipin.cn/cover_news.jpg"
                },
                "content": {
                    "content": "<p>今日新闻要点如下：</p><cctv_video id=\"v00000embedded\"></cctv_video>",
                    "images": []
                },
                "audioCapsule": {
                    "audioUrl": "https://audio.ysp.cctv.cn/news.mp3"
                }
            },
            "ret": 0
        }

        def mock_fetch_video(self, vid):
            self.video_url = f"https://mp4playcloud-cdn.ysp.cctv.cn/{vid}.mp4"

        with patch.object(YangshipinParser, "fetch_html_content", return_value="<html></html>"), \
             patch("requests.Session.get", return_value=mock_article_resp), \
             patch.object(YangshipinParser, "_fetch_video_url_by_vid", side_effect=mock_fetch_video, autospec=True):
            parser = YangshipinParser("https://m.yangshipin.cn/static/article.html?articleid=art123")
            self.assertEqual(parser.get_title_content(), "新闻联播重点快讯")
            self.assertEqual(parser.get_author_info(), {"name": "央视新闻", "avatar": None})
            self.assertEqual(parser.get_audio_url(), "https://audio.ysp.cctv.cn/news.mp3")
            self.assertEqual(parser.get_real_video_url(), "https://mp4playcloud-cdn.ysp.cctv.cn/v00000embedded.mp4")
            self.assertEqual(parser.get_video_list(), ["https://mp4playcloud-cdn.ysp.cctv.cn/v00000embedded.mp4"])


if __name__ == "__main__":
    unittest.main()
