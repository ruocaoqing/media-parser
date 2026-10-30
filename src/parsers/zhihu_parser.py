from src.parser_factory import register_parser
"""知乎问答、文章、视频与 Pin 解析器。"""

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from configs.general_constants import USER_AGENT_M
from configs.logging_config import get_logger
from src.parsers.base_parser import BaseParser
from src.utils.cookie_manager import get_platform_cookie


logger = get_logger(__name__)


@register_parser("知乎")
class ZhihuParser(BaseParser):
    """通过知乎公开 API 解析问答、文章、zvideo 与 Pin 内容。"""

    API_ROOT = "https://api.zhihu.com"

    def __init__(self, real_url):
        super().__init__(real_url)
        self.headers = {"User-Agent": USER_AGENT_M[0], "Referer": "https://www.zhihu.com/"}
        cookie = get_platform_cookie("zhihu")
        if cookie:
            self.headers["Cookie"] = cookie
        self.content_type, self.content_id = self._extract_content_id()
        self.data = self._fetch_data()

    def _extract_content_id(self):
        path = urlparse(self.real_url).path
        patterns = (
            ("answer", r"question/\d+/answer/(\d+)"),
            ("answer", r"/answer/(\d+)"),
            ("zvideo", r"/zvideo/(\d+)"),
            ("pin", r"/pin/(\d+)"),
            ("article", r"(?:(?:zhuanlan\.)?zhihu\.com/p/|/p/|/article/)(\d+)"),
        )
        for content_type, pattern in patterns:
            match = re.search(pattern, self.real_url if content_type == "article" else path)
            if match:
                return content_type, match.group(1)
        return "", ""

    def _fetch_data(self):
        if not self.content_id:
            logger.warning("Unable to extract Zhihu content ID: %s", self.real_url)
            return {}
        api_path = {
            "answer": "answers",
            "zvideo": "videos",
            "pin": "pins",
            "article": "articles",
        }.get(self.content_type)
        if not api_path:
            return {}
        try:
            response = self.session.get(
                f"{self.API_ROOT}/{api_path}/{self.content_id}",
                headers=self.headers,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and not data.get("error"):
                return data
            logger.warning("Zhihu API returned error: %s", data)
        except Exception as exc:
            logger.warning("Zhihu API fetch failed: %s", exc)

        return self._fetch_from_html()

    def _fetch_from_html(self):
        try:
            resp = self.session.get(self.real_url, headers=self.headers, timeout=10)
            if resp.status_code != 200 or not resp.text:
                return {}
            html = resp.text
            match = re.search(r'<script id="js-initialData"[^>]*>(.*?)</script>', html, re.DOTALL)
            if match:
                try:
                    state = json.loads(match.group(1)).get("initialState", {})
                    entities = state.get("entities", {})
                    mapping = {
                        "article": "articles",
                        "answer": "answers",
                        "zvideo": "zvideos",
                    }
                    target_key = mapping.get(self.content_type)
                    if target_key and self.content_id in entities.get(target_key, {}):
                        return entities[target_key][self.content_id]
                except Exception as e:
                    logger.debug("Failed to parse Zhihu SSR state: %s", e)

            soup = BeautifulSoup(html, "html.parser")
            data = {}
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                data["title"] = og_title["content"]
            og_image = soup.find("meta", property="og:image")
            if og_image and og_image.get("content"):
                data["title_image"] = og_image["content"]
            og_desc = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
            if og_desc and og_desc.get("content"):
                data["excerpt"] = og_desc["content"]
            content_div = soup.find("div", class_=re.compile(r"Post-RichText|RichText"))
            if content_div:
                data["content"] = str(content_div)
            return data
        except Exception as exc:
            logger.debug("Zhihu HTML fallback failed: %s", exc)
            return {}

    @staticmethod
    def _first_url(value):
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            return next((value.get(key) for key in ("url", "play_url") if value.get(key)), None)
        if isinstance(value, list):
            return next((ZhihuParser._first_url(item) for item in value if ZhihuParser._first_url(item)), None)
        return None

    @staticmethod
    def _playlist_url(playlist):
        if isinstance(playlist, dict):
            candidates = playlist.values()
        elif isinstance(playlist, list):
            candidates = playlist
        else:
            return None
        candidates = [item for item in candidates if isinstance(item, dict)]
        candidates.sort(
            key=lambda item: (int(item.get("bitrate") or 0), int(item.get("width") or 0) * int(item.get("height") or 0)),
            reverse=True,
        )
        return next((item.get("play_url") or item.get("url") for item in candidates if item.get("play_url") or item.get("url")), None)

    def _pin_video(self):
        for item in self.data.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "video":
                return item
        return {}

    def get_real_video_url(self):
        direct_url = self._playlist_url(self.data.get("playlist"))
        if direct_url:
            return direct_url
        pin_video = self._pin_video()
        direct_url = self._playlist_url(pin_video.get("playlist"))
        if direct_url:
            return direct_url
        lens_match = re.search(r'data-lens-id="(\d+)"', self.data.get("content") or "")
        if lens_match:
            try:
                response = self.session.get(
                    f"https://lens.zhihu.com/api/v4/videos/{lens_match.group(1)}",
                    headers=self.headers,
                    timeout=15,
                )
                response.raise_for_status()
                return self._playlist_url(response.json().get("playlist"))
            except Exception as exc:
                logger.warning("Zhihu Lens video fetch failed: %s", exc)
        return None

    def get_title_content(self):
        question = self.data.get("question") or {}
        return question.get("title") or self.data.get("title") or self.data.get("excerpt_title") or None

    def get_description(self):
        excerpt = self.data.get("excerpt") or ""
        if excerpt:
            return excerpt
        content_html = self.data.get("content_html") or self.data.get("content") or ""
        if isinstance(content_html, str):
            text = BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True)
            if text:
                return text
        return None

    def get_cover_photo_url(self):
        pin_video = self._pin_video()
        return (
            self._first_url(pin_video.get("thumbnail"))
            or self._first_url(pin_video.get("cover_url"))
            or self._first_url(self.data.get("title_image"))
            or self._first_url(self.data.get("title_img"))
            or self._first_url(self.data.get("titleImage"))
            or self._first_url(self.data.get("cover_url"))
            or self._first_url(self.data.get("cover"))
            or self._first_url(self.data.get("thumbnail"))
            or self._first_url(self.data.get("image_url"))
            or self._first_url(self.data.get("imageUrl"))
            or self._first_url(self.get_image_list())
        )

    def get_image_list(self):
        images = []
        cover = (
            self._first_url(self.data.get("title_image"))
            or self._first_url(self.data.get("title_img"))
            or self._first_url(self.data.get("titleImage"))
            or self._first_url(self.data.get("cover_url"))
            or self._first_url(self.data.get("cover"))
            or self._first_url(self.data.get("thumbnail"))
            or self._first_url(self.data.get("image_url"))
            or self._first_url(self.data.get("imageUrl"))
        )
        if cover:
            images.append(cover)

        for item in self.data.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "image":
                if url := self._first_url(item.get("url") or item.get("image")):
                    images.append(url)
        content_html = self.data.get("content") or self.data.get("content_html") or ""
        if isinstance(content_html, str):
            for image in BeautifulSoup(content_html, "html.parser").find_all("img"):
                if url := image.get("data-original") or image.get("data-actualsrc") or image.get("src"):
                    if url.startswith("http"):
                        images.append(url)
        return list(dict.fromkeys(images))

    def get_author_info(self):
        author = self.data.get("author") or {}
        return {
            "nickname": author.get("name") or author.get("nickname") or "",
            "author_id": str(author.get("id") or author.get("url_token") or author.get("urlToken") or ""),
            "avatar": author.get("avatar_url") or author.get("avatarUrl") or "",
        }
