"""从真实样本库读取显式标记的 Cookie 默认探针。"""

import json
from pathlib import Path

from configs.logging_config import get_logger

logger = get_logger(__name__)
SAMPLES_PATH = Path(__file__).resolve().parents[2] / "tests" / "live_parser_samples.json"


def default_probe_sample(platform_key):
    """未部署样本库时允许管理员通过后台配置链接。"""
    try:
        cases = json.loads(SAMPLES_PATH.read_text(encoding="utf-8"))["cases"]
        for case in cases:
            probe = case.get("cookie_probe", {})
            if probe.get("platform_key") == platform_key and case.get("url"):
                return case
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logger.warning("默认 Cookie 探针样本库不可用")
    return None
