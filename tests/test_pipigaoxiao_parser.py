import unittest
from unittest.mock import patch

from src.parsers.pipigaoxiao_parser import PipigaoxiaoParser


def _image_node(img_id, video=0, live=0, origin=True):
    node = {
        "id": img_id,
        "w": 1080,
        "h": 1441,
        "video": video,
        "live": live,
        "urls": {},
    }
    if origin:
        node["urls"] = {
            "origin": {"urls": [f"http://bd-file.ippzone.com/img/view/id/{img_id}/sz/src",
                                f"http://file.ippzone.com/img/view/id/{img_id}/sz/src"]},
            "540": {"urls": [f"http://file.ippzone.com/img/view/id/{img_id}/sz/540"]},
        }
    return node


class PipigaoxiaoParserTest(unittest.TestCase):
    def _parser_with(self, post):
        with patch.object(PipigaoxiaoParser, "fetch_html_data", return_value={"data": {"post": post}}):
            return PipigaoxiaoParser("https://h5.ippzone.com/spacey/post/897800342766")

    def test_image_post_returns_all_origin_images(self):
        parser = self._parser_with({
            "content": "",
            "videos": None,
            "imgs": [_image_node(2538988134), _image_node(2538988128), _image_node(2538988129)],
        })

        self.assertEqual(parser.get_image_list(), [
            "http://bd-file.ippzone.com/img/view/id/2538988134/sz/src",
            "http://bd-file.ippzone.com/img/view/id/2538988128/sz/src",
            "http://bd-file.ippzone.com/img/view/id/2538988129/sz/src",
        ])
        self.assertIsNone(parser.get_real_video_url())

    def test_video_post_does_not_report_images(self):
        parser = self._parser_with({
            "content": "350。。。小钱钱。。晚上继续喝",
            "videos": {"2405682288": {"url": "http://video01.szsttkj.com/a/b/c"}},
            "imgs": [_image_node(2405682288, video=1, origin=False)],
        })

        self.assertEqual(parser.get_image_list(), [])
        self.assertEqual(parser.get_real_video_url(), "http://video01.szsttkj.com/a/b/c")

    def test_falls_back_to_id_url_when_variants_missing(self):
        parser = self._parser_with({"imgs": [_image_node(2538988134, origin=False)]})

        self.assertEqual(parser.get_image_list(),
                         ["https://file.ippzone.com/img/view/id/2538988134"])

    def test_empty_data_is_safe(self):
        parser = self._parser_with({})
        self.assertEqual(parser.get_image_list(), [])


if __name__ == "__main__":
    unittest.main()
