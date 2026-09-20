import os

# 平台 Cookie 环境变量及别名映射表
PLATFORM_COOKIE_ALIASES = {
    "xhs": ["XHS_COOKIE", "XIAOHONGSHU_COOKIE"],
    "xiaohongshu": ["XHS_COOKIE", "XIAOHONGSHU_COOKIE"],
    "pinduoduo": ["PINDUODUO_COOKIE", "PDD_COOKIE"],
    "douyin": ["DOUYIN_COOKIE", "DY_COOKIE"],
    "yuanbao": ["YUANBAO_COOKIE"],
    "wechat_channels": ["YUANBAO_COOKIE", "WECHAT_CHANNELS_COOKIE"],
    "doubao": ["DOUBAO_COOKIE"],
    "jimeng": ["JIMENG_COOKIE"],
    "weibo": ["WEIBO_COOKIE"],
    "kuaishou": ["KUAISHOU_COOKIE", "KS_COOKIE"],
}


def get_platform_cookie(platform_key: str, env_var: str | None = None) -> str:
    """获取指定平台的环境变量 Cookie 凭据。

    按优先级依次检查：
    1. 指定的环境变量 (如 env_var="XHS_COOKIE")；
    2. 标准环境变量 ({PLATFORM}_COOKIE)；
    3. 别名列表 (如 XIAOHONGSHU_COOKIE)。
    """
    key_normalized = platform_key.lower().replace("-", "_")
    if env_var is None:
        env_var = f"{key_normalized.upper()}_COOKIE"

    # 1. 优先读取指定环境变量
    val = os.getenv(env_var, "").strip()
    if val:
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1].strip()
        return val

    # 2. 检查别名列表
    for alias in PLATFORM_COOKIE_ALIASES.get(key_normalized, []):
        val = os.getenv(alias, "").strip()
        if val:
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1].strip()
            return val

    return ""
