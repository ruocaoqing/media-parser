from src.parser_factory import register_parser
"""汽水音乐分享解析器。"""

import json
import re
from urllib.parse import parse_qs, unquote, urlparse

from configs.logging_config import get_logger
from src.parsers.base_parser import BaseParser


logger = get_logger(__name__)


@register_parser("汽水音乐")
class QSMusicParser(BaseParser):
    """解析汽水音乐歌曲与 UGC 视频分享页。"""

    MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
    )

    def __init__(self, real_url):
        super().__init__(real_url)
        self.headers = {"User-Agent": self.MOBILE_UA}
        self.title = ""
        self.cover_url = None
        self.video_url = None
        self.audio_url = None
        self.author = {"nickname": "", "author_id": "", "avatar": ""}
        self.subtitles = None
        self.is_preview = False
        self.full_duration = None
        self.track_id = self._extract_track_id(real_url)
        self._parse_page()

    def _parse_page(self):
        try:
            response = self.session.get(
                self.real_url, headers=self.headers, allow_redirects=True, timeout=15
            )
            self.html_content = response.text
            self.track_id = self.track_id or self._extract_track_id(response.url)
            self._parse_router_data(response.text)
        except Exception as exc:
            logger.warning("Failed to fetch QSMusic HTML page: %s", exc)

        if not self.title or not (self.video_url or self.audio_url):
            self._parse_track_payload()

    def _parse_router_data(self, html):
        match = re.search(r"_ROUTER_DATA\s*=\s*(\{.*?\});", html or "", re.DOTALL)
        if not match:
            return
        try:
            loader_data = (json.loads(match.group(1)).get("loaderData") or {})
            for page_data in loader_data.values():
                if not isinstance(page_data, dict):
                    continue
                options = page_data.get("videoOptions") or {}
                if options:
                    self.title = self.title or options.get("videoName") or options.get("title") or ""
                    self.author["nickname"] = self.author["nickname"] or options.get("artistName") or ""
                    avatars = options.get("artistThumbAvatarArr") or []
                    if avatars and not self.author["avatar"]:
                        self.author["avatar"] = self._first_url(avatars)
                    self.cover_url = self.cover_url or options.get("coverURL") or options.get("firstFrameURL")
                    stream_url = options.get("url")
                    if stream_url:
                        if "video_mp4" in stream_url or "douyinvod.com" in stream_url:
                            self.video_url = self.video_url or stream_url
                        else:
                            self.audio_url = self.audio_url or stream_url
                    self.subtitles = self.subtitles or self._extract_subtitles_from_dict(options)

                lyrics_opt = page_data.get("audioWithLyricsOption") or {}
                if isinstance(lyrics_opt, dict) and lyrics_opt.get("url"):
                    self.audio_url = self.audio_url or lyrics_opt["url"]
                    self.title = self.title or lyrics_opt.get("trackName") or ""
                    self.author["nickname"] = self.author["nickname"] or lyrics_opt.get("artistName") or ""
                    self.author["author_id"] = self.author["author_id"] or str(lyrics_opt.get("artistIdStr") or "")
                    self.cover_url = self.cover_url or lyrics_opt.get("coverURL")
                    self._apply_duration(
                        lyrics_opt.get("duration"),
                        lyrics_opt.get("offsetDuration") or lyrics_opt.get("duration"),
                    )

                track = page_data.get("trackOptions") or page_data.get("track") or page_data.get("seo_track") or {}
                track = track.get("track") if isinstance(track.get("track"), dict) else track
                if not isinstance(track, dict) or not track:
                    continue
                self.title = self.title or track.get("name") or track.get("title") or ""
                artist = track.get("artist") or ((track.get("artists") or [{}])[0] or {}).get("user_info") or (track.get("artists") or [{}])[0] or {}
                if isinstance(artist, dict):
                    self.author["nickname"] = self.author["nickname"] or artist.get("nickname") or artist.get("name") or ""
                    self.author["author_id"] = self.author["author_id"] or str(artist.get("id") or artist.get("user_id") or "")
                    self.author["avatar"] = self.author["avatar"] or self._image_url(artist.get("avatar_url") or artist.get("url_avatar")) or ""
                album = track.get("album") or {}
                self.cover_url = self.cover_url or self._image_url(album.get("cover_url") or album.get("url_cover")) or self._image_url(track.get("cover_url"))
                self.audio_url = self.audio_url or track.get("audio_url") or track.get("play_url") or track.get("main_url")

                self.subtitles = self.subtitles or self._extract_subtitles_from_dict(lyrics_opt or track)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Error parsing QSMusic router data: %s", exc)

    def _parse_track_payload(self):
        """走 Web 播放接口兜底。"""
        if not self.track_id:
            return
        headers = {
            "User-Agent": self.MOBILE_UA,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://music.douyin.com/qishui/share/track?track_id={self.track_id}",
        }

        payload = None
        for path in ("/luna/h5/track_v2", "/luna/h5/seo_track"):
            try:
                response = self.session.get(
                    "https://beta-luna.douyin.com" + path,
                    params={"track_id": self.track_id, "device_platform": "web"},
                    headers=headers,
                    timeout=15,
                )
                response.raise_for_status()
                candidate = response.json()
            except Exception as exc:
                logger.warning("Failed to fetch QSMusic track payload (%s): %s", path, exc)
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if not payload:
            return

        track = payload.get("track") or ((payload.get("seo_track") or {}).get("track") or {})
        if not isinstance(track, dict):
            track = {}
        self.title = self.title or track.get("name") or ""
        artist = ((track.get("artists") or [{}])[0] or {}).get("user_info") or (track.get("artists") or [{}])[0] or {}
        if isinstance(artist, dict):
            self.author["nickname"] = self.author["nickname"] or artist.get("nickname") or artist.get("name") or ""
            self.author["author_id"] = self.author["author_id"] or str(artist.get("id") or artist.get("user_id") or "")
            self.author["avatar"] = self.author["avatar"] or self._image_url(artist.get("medium_avatar_url") or artist.get("url_avatar")) or ""
        album = track.get("album") or {}
        self.cover_url = self.cover_url or self._image_url(album.get("cover_url") or album.get("url_cover"))

        player = payload.get("track_player") or {}
        model = self._load_video_model(player.get("video_model"))
        if model:
            video = (model.get("video_list") or [{}])[0] or {}
            self.audio_url = self.audio_url or video.get("main_url") or video.get("backup_url")
            self._apply_duration((track.get("duration") or 0) / 1000, model.get("video_duration"))

    @staticmethod
    def _load_video_model(raw):
        if not raw:
            return None
        try:
            model = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return None
        return model if isinstance(model, dict) else None

    def _apply_duration(self, full_seconds, clip_seconds):
        """比对曲目声明时长与实际下发流时长，识别汽水 VIP 试听片段。"""
        if not isinstance(full_seconds, (int, float)) or full_seconds <= 0:
            return
        self.full_duration = round(float(full_seconds), 3)
        if not isinstance(clip_seconds, (int, float)) or clip_seconds <= 0:
            return
        self.is_preview = clip_seconds < full_seconds - 1

    @staticmethod
    def _extract_track_id(url):
        decoded = unquote(url or "")
        parsed = urlparse(decoded)
        params = parse_qs(parsed.query)
        for key in ("track_id", "ugc_video_id"):
            if value := (params.get(key) or [None])[0]:
                return value
        for prefix in ("/track/", "/video/"):
            if prefix in parsed.path:
                if value := "".join(char for char in parsed.path.split(prefix, 1)[1] if char.isdigit()):
                    return value
        return None

    @staticmethod
    def _first_url(value):
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            return next((item for item in value if isinstance(item, str) and item), None)
        if isinstance(value, dict):
            return next((value.get(key) for key in ("url", "origin_url", "large_url") if value.get(key)), None)
        return None

    @classmethod
    def _image_url(cls, value):
        """兼容汽水图片载荷：直链、URL 列表，以及 urls[0] + uri 的模板形式。"""
        if isinstance(value, dict) and value.get("urls"):
            base = cls._first_url(value.get("urls"))
            uri = value.get("uri")
            if base and uri:
                return base.rstrip("/") + "/" + str(uri).lstrip("/")
            return base
        return cls._first_url(value)

    def _extract_subtitles_from_dict(self, data):
        """从字典数据中提炼字幕/歌词列表。"""
        if not isinstance(data, dict):
            return None

        sentences = (
            data.get("songMakerTeamSentences")
            or data.get("sentences")
            or data.get("lyrics")
            or data.get("subtitles")
        )
        if isinstance(sentences, list) and sentences:
            items = []
            for line in sentences:
                if isinstance(line, str) and line.strip():
                    items.append({"text": line.strip()})
                elif isinstance(line, dict):
                    text = (
                        line.get("text")
                        or line.get("content")
                        or line.get("sentence")
                        or line.get("lyric")
                    )
                    if text:
                        item = {"text": str(text).strip()}
                        if "start_time" in line or "startTime" in line:
                            item["start"] = line.get("start_time") or line.get("startTime")
                        if "end_time" in line or "endTime" in line:
                            item["end"] = line.get("end_time") or line.get("endTime")
                        items.append(item)
            if items:
                return items

        lrc_content = (
            data.get("lrc")
            or data.get("lyric")
            or data.get("lyrics_text")
            or data.get("lyric_string")
        )
        if isinstance(lrc_content, str) and lrc_content.strip():
            return self._parse_lrc_text(lrc_content)

        return None

    @staticmethod
    def _parse_lrc_text(lrc_text):
        lines = lrc_text.splitlines()
        subtitles = []
        pattern = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\](.*)")
        for line in lines:
            line = line.strip()
            if not line:
                continue
            match = pattern.match(line)
            if match:
                minutes, seconds, text = match.groups()
                text = text.strip()
                if text:
                    total_seconds = int(minutes) * 60 + float(seconds)
                    subtitles.append({"start": round(total_seconds, 3), "text": text})
            elif not line.startswith("["):
                subtitles.append({"text": line})
        return subtitles if subtitles else None

    def get_real_video_url(self):
        return self.video_url

    def get_audio_url(self):
        return self.audio_url

    def get_title_content(self):
        return self.title

    def get_cover_photo_url(self):
        return self.cover_url

    def get_author_info(self):
        return self.author

    def get_subtitles(self):
        return self.subtitles

    def get_video_list(self):
        return [self.video_url] if self.video_url else []
