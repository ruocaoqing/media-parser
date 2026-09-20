import json
import re
import time
import uuid
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from configs.general_constants import USER_AGENT_M
from configs.logging_config import get_logger
from src.parser_factory import register_parser
from src.parsers.base_parser import BaseParser

logger = get_logger(__name__)


@register_parser("央视频")
class YangshipinParser(BaseParser):
    """央视频 (Yangshipin) 客户端及 H5 平台解析器，支持横竖屏微短剧/精选视频元数据、原画封面、作者信息与无水印视频播放流提取。"""

    def __init__(self, real_url):
        super().__init__(real_url)
        self.headers = {
            "User-Agent": USER_AGENT_M[0],
            "Referer": "https://m.yangshipin.cn/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        self.vid = None
        self.article_id = None
        self.title = ""
        self.cover_url = None
        self.video_url = None
        self.description = None
        self.audio_url = None
        self.image_list = []
        self.video_list = []
        self.author = None
        self._parse()

    def _generate_ckey(self, vid: str, rnd: str, guid: str, platform: str = "4330701", app_ver: str = "1.3.5") -> str:
        """生成央视频 playvinfo 接口所需的 cKey 加密签名 (AES-128-CBC + PKCS7)"""
        part1 = f"|{vid}|{rnd}|mg3c3b04ba|{app_ver}|{guid}|{platform}|"
        part2 = "https://m.yangshipin.cn/|mozilla/5.0 (iphone; cpu||Mozilla|Netscape|iPhone|"
        tr = part1 + part2

        # 模拟前端 32 位有符号整数哈希
        pr = 0
        for ch in tr:
            pr = ((pr << 5) - pr + ord(ch)) & 0xFFFFFFFF
        if pr >= 0x80000000:
            pr -= 0x100000000

        ur = f"|{pr}{tr}"
        key = bytes.fromhex("4e2918885fd98109869d14e0231a0bf4")
        iv = bytes.fromhex("16b17e519ddd0ce5b79d7a63a4dd801c")

        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded = pad(ur.encode("utf-8"), AES.block_size, style="pkcs7")
        encrypted = cipher.encrypt(padded)
        return f"--01{encrypted.hex().upper()}"

    def _fetch_video_url_by_vid(self, vid: str) -> None:
        """通过 playvv.yangshipin.cn/playvinfo 请求提取高清/蓝光视频下载及播放链接"""
        if not vid:
            return

        api_url = "https://playvv.yangshipin.cn/playvinfo"
        guid = uuid.uuid4().hex
        headers = {
            "User-Agent": USER_AGENT_M[0],
            "Referer": "https://m.yangshipin.cn/",
            "Accept": "*/*",
        }

        try:
            # 1. 探测获取服务端同步时间戳 curTime
            probe_params = {
                "vid": vid,
                "platform": "4330701",
                "otype": "json",
            }
            probe_res = self.session.get(api_url, params=probe_params, headers=headers, timeout=5)
            probe_text = probe_res.text.strip().strip("()")
            probe_data = json.loads(probe_text)
            cur_time = str(probe_data.get("curTime") or int(time.time()))

            # 2. 生成 cKey 签名并请求视频流信息
            ckey = self._generate_ckey(vid, cur_time, guid)
            req_params = {
                "charge": "0",
                "defaultfmt": "auto",
                "otype": "json",
                "guid": guid,
                "flowid": f"{guid}_4330701",
                "platform": "4330701",
                "sdtfrom": "v7007",
                "defnpayver": "1",
                "appVer": "1.3.5",
                "host": "m.yangshipin.cn",
                "ehost": f"https://m.yangshipin.cn/portrait_video?vid={vid}",
                "refer": "m.yangshipin.cn",
                "sphttps": "1",
                "sphls": "1",
                "_rnd": cur_time,
                "spwm": "4",
                "vid": vid,
                "defn": "fhd",
                "encryptVer": "8.1",
                "cKey": ckey,
            }

            res = self.session.get(api_url, params=req_params, headers=headers, timeout=5)
            resp_text = res.text.strip().strip("()")
            data = json.loads(resp_text)

            # 3. 解析视频文件地址与 vkey
            vi_list = data.get("vl", {}).get("vi", [])
            if vi_list:
                vi = vi_list[0]
                ui_list = vi.get("ul", {}).get("ui", [])
                base_url = ui_list[0].get("url") if ui_list else None
                fn = vi.get("fn")
                fvkey = vi.get("fvkey")
                if base_url and fn and fvkey:
                    self.video_url = f"{base_url}{fn}?vkey={fvkey}&platform=2"

                if not self.title and vi.get("ti"):
                    self.title = vi.get("ti")

        except Exception as e:
            logger.warning("Failed to fetch video stream from Yangshipin playvinfo for vid %s: %s", vid, e)

    @staticmethod
    def _clean_article_html(html_content: str) -> str | None:
        """将文章富文本 HTML 解析转换为保留自然段落的纯文本。"""
        if not html_content:
            return None
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup.find_all(["script", "style"]):
            tag.decompose()
        for tag in soup.find_all("br"):
            tag.replace_with("\n")
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "section", "article"]):
            tag.append("\n")
        lines = []
        for line in soup.get_text().replace("\xa0", " ").splitlines():
            clean_line = re.sub(r"[\t\f\v ]+", " ", line).strip()
            if clean_line:
                lines.append(clean_line)
        return "\n\n".join(lines) if lines else None

    def _fetch_article_info(self, article_id: str) -> None:
        """通过央视频开放接口获取图文文章/资讯数据及内嵌多媒体。"""
        if not article_id:
            return

        api_urls = [
            "https://comment.yangshipin.cn/web/article/article_info",
            "https://h5access.yangshipin.cn/web/article/article_info",
        ]
        params = {
            "vappid": "59306155",
            "vsecret": "b42702bf7309a179d102f3d51b1add2fda0bc7ada64cb801",
            "raw": "1",
            "targetId": "1",
            "id": article_id,
        }
        headers = {
            "User-Agent": USER_AGENT_M[0],
            "Referer": "https://m.yangshipin.cn/",
            "Accept": "application/json, text/plain, */*",
        }

        for api_url in api_urls:
            try:
                resp = self.session.get(api_url, params=params, headers=headers, timeout=5)
                if resp.status_code != 200:
                    continue
                res_json = resp.json()
                data = res_json.get("data", {})
                if not data or data.get("errCode") != 0:
                    continue

                head = data.get("head") or {}
                if not self.title:
                    self.title = (head.get("title") or "").strip()
                if not self.cover_url:
                    self.cover_url = head.get("coverImage")
                source = head.get("source")
                if source and not self.author:
                    self.author = {
                        "name": source,
                        "avatar": None,
                    }

                content_obj = data.get("content") or {}
                raw_images = content_obj.get("images") or []
                for img in raw_images:
                    if img and img not in self.image_list:
                        self.image_list.append(img)

                raw_html = content_obj.get("content") or ""
                if raw_html:
                    self.description = self._clean_article_html(raw_html)

                audio_capsule = data.get("audioCapsule")
                if isinstance(audio_capsule, dict) and audio_capsule.get("audioUrl"):
                    self.audio_url = audio_capsule.get("audioUrl")
                elif isinstance(audio_capsule, str) and audio_capsule.startswith("http"):
                    self.audio_url = audio_capsule

                # 内嵌视频提取
                videos = content_obj.get("videos") or []
                embedded_vids = []
                for v in videos:
                    if isinstance(v, dict) and v.get("vid"):
                        embedded_vids.append(v.get("vid"))
                    elif isinstance(v, str):
                        embedded_vids.append(v)

                if not embedded_vids and raw_html:
                    embedded_vids = re.findall(r'<cctv_video\s+[^>]*id=[\'"]([^\'"]+)[\'"]', raw_html, re.I)

                if embedded_vids:
                    if not self.vid:
                        self.vid = embedded_vids[0]
                    for vid in embedded_vids:
                        self._fetch_video_url_by_vid(vid)
                        if self.video_url and self.video_url not in self.video_list:
                            self.video_list.append(self.video_url)

                break
            except Exception as e:
                logger.warning("Failed to fetch article info for Yangshipin article %s from %s: %s", article_id, api_url, e)

    def _parse(self):
        if not self.real_url:
            return

        # 尝试先从 URL 中提取 vid 与 article_id
        parsed_url = urlparse(self.real_url)
        qs = parse_qs(parsed_url.query)
        self.vid = qs.get("vid", [None])[0]
        self.article_id = qs.get("articleid", [None])[0] or qs.get("articleId", [None])[0] or qs.get("id", [None])[0]

        html = self.fetch_html_content()
        if not html:
            logger.warning("Failed to fetch HTML content for Yangshipin URL: %s", self.real_url)
            if self.article_id:
                self._fetch_article_info(self.article_id)
            elif self.vid:
                self._fetch_video_url_by_vid(self.vid)
            return

        # 兼容短链未重定向时页面内的 meta refresh 跳转
        meta_refresh = re.search(r'''<meta\s+http-equiv=["']refresh["']\s+content=["'][^;]+;\s*URL=['"]([^'"]+)['"]''', html, re.I)
        if meta_refresh:
            redirect_url = meta_refresh.group(1).strip()
            if redirect_url.startswith("/"):
                redirect_url = f"https://m.yangshipin.cn{redirect_url}"
            self.real_url = redirect_url
            parsed_url = urlparse(self.real_url)
            qs = parse_qs(parsed_url.query)
            if not self.vid or self.vid == "5Sqx":
                self.vid = qs.get("vid", [None])[0]
            if not self.article_id:
                self.article_id = qs.get("articleid", [None])[0] or qs.get("articleId", [None])[0] or qs.get("id", [None])[0]
            html = self.fetch_html_content()
            if not html:
                if self.article_id:
                    self._fetch_article_info(self.article_id)
                elif self.vid:
                    self._fetch_video_url_by_vid(self.vid)
                return

        # 若目标为文章页，直接调用文章接口解析
        if not self.article_id and "article" in parsed_url.path:
            article_match = re.search(r'article(?:id)?=([0-9a-zA-Z_]+)', self.real_url)
            if article_match:
                self.article_id = article_match.group(1)

        if self.article_id:
            self._fetch_article_info(self.article_id)

        try:
            # 1. 尝试从横屏视频 STATE 提取 (__STATE_video__)
            data_vid = self._extract_state_json(html, "window.__STATE_video__")
            if data_vid:
                sv = data_vid.get("payloads", {}).get("sharevideo", {})
                if not self.vid:
                    self.vid = sv.get("vid")
                if not self.title:
                    self.title = (sv.get("title") or "").strip()
                if not self.cover_url:
                    self.cover_url = sv.get("cover_pic")
                om_info = sv.get("om_info", {})
                author_name = om_info.get("title")
                if author_name and not self.author:
                    self.author = {
                        "name": author_name,
                        "avatar": None,
                    }

            # 2. 尝试从竖屏视频 STATE 提取 (__STATE_portrait_video__)
            data_port = self._extract_state_json(html, "window.__STATE_portrait_video__")
            if data_port:
                items = data_port.get("payloads", {}).get("videoDataList", {}).get("items", [])
                if items:
                    vd = items[0].get("videoData", {})
                    if not self.vid:
                        self.vid = vd.get("vid")
                    if not self.title:
                        self.title = (vd.get("title") or "").strip()
                    if not self.cover_url:
                        self.cover_url = (
                            vd.get("shareItem", {}).get("shareImgUrl")
                            or vd.get("poster", {}).get("poster", {}).get("imageUrl")
                        )
                    actor = vd.get("detailFollowItem", {}).get("actorItem", {})
                    if actor and not self.author:
                        nick_name = actor.get("nickName", {}).get("text")
                        head_url = actor.get("headUrl")
                        if nick_name or head_url:
                            self.author = {
                                "name": nick_name,
                                "avatar": head_url,
                            }

            # 3. 兜底：从 OpenGraph / HTML 标签提取
            if not self.title or not self.cover_url:
                soup = BeautifulSoup(html, "lxml")
                if not self.title:
                    og_title = soup.find("meta", property="og:title")
                    if og_title and og_title.get("content"):
                        self.title = og_title["content"].strip()
                    elif soup.title and soup.title.string:
                        self.title = soup.title.string.strip()

                if not self.cover_url:
                    og_img = soup.find("meta", property="og:image")
                    if og_img and og_img.get("content"):
                        self.cover_url = og_img["content"].strip()

            if not self.vid and not self.article_id:
                vid_match = re.search(r'[?&]vid=([0-9a-zA-Z_]+)', self.real_url) or re.search(r'["\']vid["\']\s*:\s*["\']([0-9a-zA-Z_]+)["\']', html)
                if vid_match:
                    self.vid = vid_match.group(1)

        except Exception as exc:
            logger.exception("Error parsing Yangshipin page: %s", exc)

        # 4. 根据提取到的 vid 请求视频播放地址
        if self.vid and not self.video_url:
            self._fetch_video_url_by_vid(self.vid)

        if self.title:
            self.title = re.sub(r'<[^>]+>', '', self.title).strip()

        if not self.video_url and not self.image_list and self.cover_url:
            self.image_list.append(self.cover_url)

    def _extract_state_json(self, html, state_key):
        """从页面提取指定 state_key 的 JSON 数据对象"""
        idx = html.find(state_key)
        if idx == -1:
            return None
        start = html.find("{", idx)
        if start == -1:
            return None
        try:
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(html[start:])
            return data
        except Exception as e:
            logger.warning("Failed to decode %s JSON: %s", state_key, e)
            return None

    def get_real_video_url(self):
        return self.video_url

    def get_title_content(self):
        return self.title or ""

    def get_description(self):
        return self.description

    def get_cover_photo_url(self):
        return self.cover_url

    def get_author_info(self):
        return self.author

    def get_audio_url(self):
        return self.audio_url

    def get_image_list(self):
        return self.image_list

    def get_video_list(self):
        return self.video_list
