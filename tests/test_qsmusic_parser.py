import json
import unittest
from unittest.mock import Mock, patch

from src.parsers.qsmusic_parser import QSMusicParser


class QSMusicParserTest(unittest.TestCase):
    def test_maps_router_video_data(self):
        router_data = {"loaderData": {"ugc_video_page": {"videoOptions": {
            "videoName": "测试汽水作品",
            "artistName": "测试音乐人",
            "artistThumbAvatarArr": ["https://image.example.com/avatar.jpg"],
            "coverURL": "https://image.example.com/cover.jpg",
            "url": "https://v3-dy-o.zjcdn.com/video_mp4/work.mp4",
        }}}}
        response = Mock(url="https://music.douyin.com/qishui/share/ugc_video?ugc_video_id=123")
        response.text = f"<script>_ROUTER_DATA = {json.dumps(router_data)};</script>"

        with patch("requests.Session.get", return_value=response):
            parser = QSMusicParser("https://qishui.douyin.com/s/code/")

        self.assertEqual(parser.get_title_content(), "测试汽水作品")
        self.assertEqual(parser.get_real_video_url(), "https://v3-dy-o.zjcdn.com/video_mp4/work.mp4")
        self.assertEqual(parser.get_cover_photo_url(), "https://image.example.com/cover.jpg")
        self.assertEqual(parser.get_author_info()["nickname"], "测试音乐人")

    def test_maps_track_audio_data(self):
        router_data = {"loaderData": {"track_page": {"trackOptions": {
            "name": "测试歌曲",
            "artists": [{"user_info": {"nickname": "歌手", "id": 42}}],
            "album": {"cover_url": {"url": "https://image.example.com/album.jpg"}},
            "audio_url": "https://audio.example.com/song.mp3",
        }}}}
        response = Mock(url="https://music.douyin.com/track/123")
        response.text = f"<script>_ROUTER_DATA = {json.dumps(router_data)};</script>"

        with patch("requests.Session.get", return_value=response):
            parser = QSMusicParser("https://music.douyin.com/track/123")

        self.assertEqual(parser.get_audio_url(), "https://audio.example.com/song.mp3")
        self.assertEqual(parser.get_real_video_url(), None)
        self.assertEqual(parser.get_author_info()["author_id"], "42")

    def test_extracts_subtitles_from_lrc(self):
        router_data = {"loaderData": {"track_page": {
            "trackOptions": {"name": "歌词测试"},
            "audioWithLyricsOption": {"lrc": "[00:10.50]第一句歌词\n[00:15.00]第二句歌词"}
        }}}
        response = Mock(url="https://music.douyin.com/track/123")
        response.text = f"<script>_ROUTER_DATA = {json.dumps(router_data)};</script>"

        with patch("requests.Session.get", return_value=response):
            parser = QSMusicParser("https://music.douyin.com/track/123")

        self.assertEqual(
            parser.get_subtitles(),
            [{"start": 10.5, "text": "第一句歌词"}, {"start": 15.0, "text": "第二句歌词"}]
        )

    def test_extracts_subtitles_from_sentences(self):
        router_data = {"loaderData": {"track_page": {
            "trackOptions": {"name": "句子测试"},
            "audioWithLyricsOption": {"songMakerTeamSentences": [
                {"text": "句子一", "start_time": 1000, "end_time": 3000},
                {"text": "句子二", "start_time": 3500, "end_time": 6000}
            ]}
        }}}
        response = Mock(url="https://music.douyin.com/track/123")
        response.text = f"<script>_ROUTER_DATA = {json.dumps(router_data)};</script>"

        with patch("requests.Session.get", return_value=response):
            parser = QSMusicParser("https://music.douyin.com/track/123")

        self.assertEqual(
            parser.get_subtitles(),
            [
                {"text": "句子一", "start": 1000, "end": 3000},
                {"text": "句子二", "start": 3500, "end": 6000}
            ]
        )

    def test_flags_vip_preview(self):
        router_data = {"loaderData": {"track_page": {"audioWithLyricsOption": {
            "url": "https://audio.example.com/clip.mp4",
            "trackName": "会员歌曲",
            "artistName": "会员歌手",
            "artistIdStr": "10086",
            "coverURL": "https://image.example.com/cover.jpg",
            "duration": 264.333,
            "offsetDuration": 30.001,
        }}}}
        response = Mock(url="https://music.douyin.com/qishui/share/track?track_id=6845")
        response.text = f"<script>_ROUTER_DATA = {json.dumps(router_data)};</script>"

        with patch("requests.Session.get", return_value=response):
            parser = QSMusicParser("https://qishui.douyin.com/s/code/")

        self.assertTrue(parser.is_preview)
        self.assertEqual(parser.full_duration, 264.333)
        self.assertEqual(parser.get_title_content(), "会员歌曲")
        self.assertEqual(parser.get_author_info()["author_id"], "10086")
        self.assertEqual(parser.get_cover_photo_url(), "https://image.example.com/cover.jpg")

    def test_free_track_is_not_flagged_as_preview(self):
        router_data = {"loaderData": {"track_page": {"audioWithLyricsOption": {
            "url": "https://audio.example.com/full.mp4",
            "trackName": "免费歌曲",
            "duration": 388.728,
            "previewStart": 0,
            "previewEnd": 388.728,
        }}}}
        response = Mock(url="https://music.douyin.com/qishui/share/track?track_id=7225")
        response.text = f"<script>_ROUTER_DATA = {json.dumps(router_data)};</script>"

        with patch("requests.Session.get", return_value=response):
            parser = QSMusicParser("https://qishui.douyin.com/s/code/")

        self.assertFalse(parser.is_preview)
        self.assertEqual(parser.full_duration, 388.728)
        self.assertEqual(parser.get_audio_url(), "https://audio.example.com/full.mp4")

    def test_flags_preview_from_player_payload(self):
        page = Mock(url="https://music.douyin.com/qishui/share/track?track_id=6845376862848321538")
        page.text = "<html></html>"
        player = Mock()
        player.raise_for_status = Mock()
        player.json.return_value = {
            "track": {
                "name": "Just to Be in Love",
                "duration": 264333,
                "artists": [{"id": 6769796358737532929, "name": "Alex Rasov", "url_avatar": {"urls": ["https://img.example.com"], "uri": "avatar.jpg"}}],
                "album": {"url_cover": {"urls": ["https://img.example.com"], "uri": "cover.jpg"}},
            },
            "track_player": {"video_model": json.dumps({
                "video_duration": 30.001,
                "video_list": [{"main_url": "https://audio.example.com/clip.mp4"}],
            })},
        }

        with patch("requests.Session.get", side_effect=[page, player]):
            parser = QSMusicParser(
                "https://music.douyin.com/qishui/share/track?track_id=6845376862848321538"
            )

        self.assertTrue(parser.is_preview)
        self.assertEqual(parser.full_duration, 264.333)
        self.assertEqual(parser.get_audio_url(), "https://audio.example.com/clip.mp4")
        self.assertEqual(parser.get_author_info()["author_id"], "6769796358737532929")
        self.assertEqual(parser.get_author_info()["avatar"], "https://img.example.com/avatar.jpg")
        self.assertEqual(parser.get_cover_photo_url(), "https://img.example.com/cover.jpg")


if __name__ == "__main__":
    unittest.main()
