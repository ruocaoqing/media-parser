"""Cookie 真实探针：金丝雀链接双跑对照判定。

同一条金丝雀链接分别带当前 Cookie 与强制无 Cookie 各解析一次：

- 两路都成功且未观察到增益 → unverified（不能证明凭据有效）
- 带 Cookie 成功、无 Cookie 失败/降级 → healthy（Cookie 增益明确）
- 带 Cookie 失败、无 Cookie 成功 → stale（凭据已失效的实锤信号）
- 两路都失败 → probe_failed（金丝雀链接或网络问题，不怪 Cookie）

探针直接复用解析链路（ParserFactory 建实例），不走 HTTP 自调用，
不占用 worker 与限流额度；"强制无 Cookie"通过 ContextVar 隔离当前解析的凭据读取，不改动其他请求。
"""

import threading
import time

from configs.logging_config import get_logger
from src.db import setting
from src.parser_factory import ParserFactory
from src.utils import cookie_registry
from src.utils.cookie_manager import without_platform_cookies as _no_cookie
from src.utils.cookie_samples import default_probe_sample
from utils.web_fetcher import UrlParser, WebFetcher

logger = get_logger(__name__)

# 同平台探针最小间隔（秒），防止管理员连点触发目标平台风控。
PROBE_DEBOUNCE_SECONDS = 5 * 60


def _canary_url(platform_key):
    entry = cookie_registry.get_entry(platform_key)
    if not entry:
        return None
    url = (setting(entry["canary_setting"]) or "").strip()
    if url:
        return url
    sample = default_probe_sample(platform_key)
    return sample["url"] if sample else None


def _run_parse(share_url, expected_platforms):
    """复用解析链路跑一次完整解析，返回统一的媒体结果摘要。"""
    try:
        return _parse_media(share_url, expected_platforms)
    except Exception as exc:
        logger.warning("Cookie 探针单路解析失败: %s", type(exc).__name__)
        return {"ok": False, "reason": "PARSE_EXCEPTION", "detail": {}}


def _parse_media(share_url, expected_platforms):
    redirect_url = WebFetcher.fetch_redirect_url(share_url)
    if not redirect_url:
        return {"ok": False, "reason": "REDIRECT_FAILED", "detail": "短链重定向失败"}
    platform = UrlParser.get_platform(redirect_url)
    if platform not in expected_platforms:
        return {"ok": False, "reason": "PLATFORM_MISMATCH",
                "detail": f"期望 {'/'.join(expected_platforms)}，实际 {platform}"}
    real_url = UrlParser.extract_video_address(redirect_url)
    parser = ParserFactory.create_parser(platform, real_url)

    result = {
        "title": parser.get_title_content(),
        "video_url": parser.get_real_video_url(),
        "video_list": _safe(parser.get_video_list) or [],
        "cover_url": parser.get_cover_photo_url(),
        "image_list": _safe(parser.get_image_list) or [],
        "audio_url": _safe(parser.get_audio_url),
    }
    live_urls = [
        img.get("live_photo_url") for img in result["image_list"]
        if isinstance(img, dict) and img.get("live_photo_url")
    ]
    has_media = bool(
        result["video_url"] or result["video_list"] or result["image_list"] or result["audio_url"]
    )
    return {
        "ok": has_media,
        "reason": "OK" if has_media else "MEDIA_NOT_FOUND",
        "detail": {
            "has_video": bool(result["video_url"] or result["video_list"]),
            "has_audio": bool(result["audio_url"]),
            "image_count": len(result["image_list"]),
            "live_count": len(live_urls),
        },
        "live_count": len(live_urls),
    }


def _safe(func, default=None):
    if not callable(func):
        return default
    try:
        return func()
    except Exception:
        return default


def _judge(platform_key, with_result, without_result):
    """按双跑对照矩阵判定。"""
    if with_result["ok"] and without_result["ok"]:
        gain = (
            with_result.get("live_count", 0) > without_result.get("live_count", 0)
        )
        gain = gain or any(
            with_result["detail"].get(key, 0) > without_result["detail"].get(key, 0)
            for key in ("has_video", "has_audio", "image_count")
        )
        if gain:
            return "healthy", {"signal": "COOKIE_GAIN", "with": with_result["detail"], "without": without_result["detail"]}
        return "unverified", {"signal": "BOTH_OK", "with": with_result["detail"], "without": without_result["detail"],
                           "note": "两路均可解析，未检测到 Cookie 增益，凭据有效性待验证；可更换依赖凭据的样例"}
    if with_result["ok"] and not without_result["ok"]:
        return "healthy", {"signal": "COOKIE_GAIN", "with": with_result["detail"],
                           "without": {"reason": without_result["reason"]}}
    if not with_result["ok"] and without_result["ok"]:
        return "stale", {"signal": "COOKIE_BROKEN", "with": {"reason": with_result["reason"]},
                         "without": without_result["detail"]}
    return "probe_failed", {"signal": "BOTH_FAILED", "with": {"reason": with_result["reason"]},
                            "without": {"reason": without_result["reason"]}}


_probe_lock = threading.Lock()
_last_probe_at = {}


def probe_platform(platform_key, force=False):
    """对单平台执行双跑对照探针。返回 {status, detail, duration_ms}。"""
    entry = cookie_registry.get_entry(platform_key)
    if not entry:
        return {"status": "probe_failed", "detail": {"error": "未注册的平台"}, "duration_ms": 0}
    if cookie_registry.get_cookie_source(platform_key) == "none":
        return {"status": "not_configured", "detail": {"error": "未配置凭据"}, "duration_ms": 0}

    canary = _canary_url(platform_key)
    if not canary:
        return {"status": "probe_failed",
                "detail": {"error": "暂无默认探针样例，请在巡检页填写测试样例链接"},
                "duration_ms": 0}

    with _probe_lock:
        last = _last_probe_at.get(platform_key, 0)
        elapsed = time.monotonic() - last
        if not force and elapsed < PROBE_DEBOUNCE_SECONDS:
            return {"status": "debounced",
                    "detail": {"error": f"距上次探针不足 {PROBE_DEBOUNCE_SECONDS // 60} 分钟，已跳过（防风控）"},
                    "duration_ms": 0}
        _last_probe_at[platform_key] = time.monotonic()

    started = time.monotonic()
    expected_platforms = set(entry["log_names"])
    try:
        with_result = _run_parse(canary, expected_platforms)
        with _no_cookie():
            without_result = _run_parse(canary, expected_platforms)
        status, detail = _judge(platform_key, with_result, without_result)
    except Exception as exc:  # 探针失败不抛出，统一结构化返回
        logger.warning(f"Cookie 探针异常 {platform_key}: {exc}")
        status, detail = "probe_failed", {"signal": "EXCEPTION", "error": str(exc)[:200]}

    duration_ms = int((time.monotonic() - started) * 1000)
    cookie_registry.record_probe(platform_key, status, "probe", detail, duration_ms)
    return {"status": status, "detail": detail, "duration_ms": duration_ms}
