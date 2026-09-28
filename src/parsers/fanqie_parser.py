import json
import random
import re
from src.parsers.base_parser import BaseParser
from src.parser_factory import register_parser
from utils.html_video_extractor import HtmlVideoExtractor
from configs.general_constants import USER_AGENT_M
from configs.logging_config import get_logger

logger = get_logger(__name__)

# 官网网页播放器。推广落地页只给 30 秒试看(play_url 里带 &start=0&end=30),
# 完整分集要打这个站的 /player/<剧集 id>/<分集 id>,页面 ld+json 里的
# contentUrl 就是完整视频。红果短剧、红果漫剧、番茄小说的剧都挂在这一个站上。
PLAYER_HOST = "https://hongguoduanju.com"
# 试看链接的特征:查询串里有 end=<秒数>
PREVIEW_MARK = re.compile(r'[?&]end=\d+')
CONTENT_URL_PATTERN = re.compile(r'"contentUrl"\s*:\s*"(https[^"]+)"')
# 分享页 _ROUTER_DATA 里的 linkParams.schemeParams.video_id 是剧集 id,
# pageData.used_chapter_id 是当前分集 id。
SCHEME_PARAMS_PATTERN = re.compile(r'"schemeParams"\s*:\s*(\{.*?\})', re.DOTALL)
CHAPTER_ID_PATTERN = re.compile(r'"used_chapter_id"\s*:\s*"?(\d+)"?')


@register_parser("番茄小说", "红果短剧", "红果漫剧")
class FanqieParser(BaseParser):
    """番茄小说 / 红果短剧 / 红果漫剧推广页解析器"""

    def __init__(self, real_url):
        super().__init__(real_url)
        self.headers = {
            "content-type": "application/json; charset=UTF-8",
            "User-Agent": random.choice(USER_AGENT_M)
        }
        self.parsed_data = self._parse_html()

    def _parse_html(self):
        try:
            html = self.fetch_html_content()
            parsed = HtmlVideoExtractor.parse_page(html)
            full_url = self._fetch_full_video_url(html, parsed.get('video_url'))
            if full_url:
                parsed['video_url'] = full_url
            return parsed
        except Exception as e:
            logger.error(f"Failed to fetch or parse HTML for {self.real_url}: {e}")
            return {'title': None, 'video_url': None, 'cover_url': None, 'author': None}

    def _fetch_full_video_url(self, html, share_video_url):
        """把试看链接换成完整分集链接;拿不到就返回 None,继续用分享页那条。"""
        if not html or not share_video_url or not PREVIEW_MARK.search(share_video_url):
            return None

        series_id, chapter_id = self._extract_ids(html)
        if not (series_id and chapter_id):
            logger.warning(f"试看链接但没解析出剧集/分集 id: {self.real_url}")
            return None

        player_url = f"{PLAYER_HOST}/player/{series_id}/{chapter_id}"
        try:
            page = self.fetch_player_html(player_url)
        except Exception as e:
            logger.warning(f"官网播放器页取不到({player_url}): {e}")
            return None

        match = CONTENT_URL_PATTERN.search(page or '')
        if not match:
            logger.warning(f"官网播放器页没有 contentUrl({player_url})")
            return None
        return HtmlVideoExtractor._clean_url(match.group(1)) or match.group(1).replace(r'\u002F', '/')

    @staticmethod
    def _extract_ids(html):
        """从分享页 _ROUTER_DATA 抽 (剧集 id, 分集 id)。"""
        series_id = None
        scheme_match = SCHEME_PARAMS_PATTERN.search(html)
        if scheme_match:
            try:
                series_id = json.loads(scheme_match.group(1)).get('video_id')
            except ValueError:
                logger.warning(f"schemeParams 不是合法 JSON: {scheme_match.group(1)[:80]}")
        if not series_id:
            vid_match = re.search(r'"video_id"\s*:\s*"?(\d+)"?', html)
            if vid_match:
                series_id = vid_match.group(1)
        chapter_match = CHAPTER_ID_PATTERN.search(html)
        return series_id, (chapter_match.group(1) if chapter_match else None)

    def fetch_player_html(self, url):
        resp = self.session.get(url, headers=self.headers, timeout=5)
        resp.raise_for_status()
        return resp.text

    def get_real_video_url(self):
        return self.parsed_data.get('video_url')

    def get_title_content(self):
        return self.parsed_data.get('title') or ""

    def get_cover_photo_url(self):
        return self.parsed_data.get('cover_url') or ""

    def get_author_info(self):
        author_name = self.parsed_data.get('author')
        if author_name:
            return {'nickname': author_name}
        return {}
