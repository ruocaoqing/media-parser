from src.parser_factory import register_parser

import random
from urllib.parse import parse_qs, urlparse

from src.parsers.base_parser import BaseParser
from configs.general_constants import USER_AGENT_PC

# 最右实况图（动态照片）的动态片段实测 1-3 秒，帖子主视频实测 15 秒以上，用时长区分两者
LIVE_PHOTO_MAX_DURATION = 5

@register_parser("最右")
class ZuiyouParser(BaseParser):
    def __init__(self, real_url):
        super().__init__(real_url)
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": random.choice(USER_AGENT_PC),
            "Referer": "https://share.xiaochuankeji.cn/",
        }
        self.data = self.fetch_html_data()

    def fetch_html_data(self):
        video_id = parse_qs(urlparse(self.real_url).query).get("pid", [None])[0]
        if not video_id:
            return {}
        try:
            int_video_id = int(video_id)
        except (TypeError, ValueError):
            return {}
        req_url = "https://share.xiaochuankeji.cn/planck/share/post/detail_h5"
        post_data = {"h_av": "5.2.13.011", "pid": int_video_id}
        try:
            resp = self.session.post(req_url, headers=self.headers, json=post_data, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    def _extract_image_url(self, img_node):
        if not isinstance(img_node, dict):
            return None
        urls_dict = img_node.get("urls")
        if isinstance(urls_dict, dict):
            for key in ("origin", "origin_webp", "540", "540_webp", "360", "360_webp"):
                cand = urls_dict.get(key, {}).get("urls", [])
                if cand and isinstance(cand, list):
                    return cand[0]
        return img_node.get("url")

    def _motion_media(self):
        """帖子中带动态片段的媒体条目，返回 {图片 id: 视频信息}。"""
        try:
            data = self.data.get("data", {}).get("post", {})
            return {str(key): value for key, value in (data.get("videos") or {}).items()}
        except (AttributeError, TypeError):
            return {}

    @staticmethod
    def _is_live_photo(video_info):
        """时长在实况上限内的是实况图的动态片段，不是帖子主视频。"""
        if not isinstance(video_info, dict):
            return False
        duration = video_info.get("dur")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            return False
        return duration <= LIVE_PHOTO_MAX_DURATION

    def get_real_video_url(self):
        try:
            videos = self._motion_media()
            if not videos:
                return None
            data = self.data.get("data", {}).get("post", {})
            imgs = data.get("imgs") or []
            # 优先取 imgs[0] 对应的视频，再按接口返回顺序兜底
            keys = ([str(imgs[0].get("id"))] if imgs else []) + list(videos)
            for key in keys:
                video_info = videos.get(key)
                if not isinstance(video_info, dict) or not video_info.get("url"):
                    continue
                if self._is_live_photo(video_info):
                    continue
                return video_info["url"]
            return None
        except Exception:
            return None

    def get_image_list(self):
        try:
            videos = self._motion_media()
            imgs = self.data.get("data", {}).get("post", {}).get("imgs") or []
            image_list = []
            for img in imgs:
                video_info = videos.get(str(img.get("id")))
                if video_info is None:
                    url = self._extract_image_url(img)
                    if url:
                        image_list.append(url)
                    continue
                # id 命中 videos 的条目是动态媒体：实况图按「封面 + 动态片段」返回，主视频的封面帧跳过
                if self._is_live_photo(video_info) and video_info.get("url"):
                    cover = self._extract_image_url(img)
                    if cover:
                        image_list.append({
                            "url": cover,
                            "live_photo_url": video_info["url"],
                        })
            return image_list
        except Exception:
            return []

    def get_cover_photo_url(self):
        try:
            data = self.data.get("data", {}).get("post", {})
            if data.get("cover"):
                return data["cover"]
            imgs = data.get("imgs") or []
            if imgs:
                return self._extract_image_url(imgs[0]) or ""
            return ""
        except Exception:
            return ""

    def get_title_content(self):
        return None

    def get_description(self):
        try:
            return self.data.get("data", {}).get("post", {}).get("content") or None
        except (KeyError, TypeError):
            return None

    def get_author_info(self):
        try:
            member = self.data.get("data", {}).get("post", {}).get("member", {})
            avatar_urls = member.get("avatar_urls", {}).get("origin", {}).get("urls", [])
            return {
                "nickname": member.get("name", ""),
                "author_id": str(member.get("id", "")),
                "avatar": avatar_urls[0] if avatar_urls else "",
            }
        except (KeyError, TypeError):
            return {}
