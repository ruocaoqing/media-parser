# -*- coding: utf-8 -*-
"""字节系 VOD 播放地址「摇签」解析器。

抖音视频 URL 有两类形态：
1. play 网关地址（api-play-hl.amemv.com / aweme.snssdk.com 的 /aweme/v1/play/...）：
   客户端请求时会 302 派发到随机 CDN 节点（实测同一地址 50 次派发出 29 个域名，
   含 bdcgslb.com / jspcdn.cn 等无法进入 downloadFile 白名单的第三方 PCDN）；
2. 直连 CDN 地址（*.douyinvod.com / *.zjcdn.com / *.365yg.com 的 /tos/ 链接）。

实测字节签名按节点绑定（对最终地址换主机名必 403），因此只能「重摇」不能「改写」：
服务端并发多次跟随 302，优先返回命中白名单主机的最终地址；
全部未命中时退回最后一次结果（fail-open，与未开启本功能时的行为一致）。

第二道关（2026-10-03 加）：字节 CDN 的 Referer 白名单按节点配置，同一签名地址
「无 Referer 206 / 小程序 Referer 403」的节点真实存在（当晚实测 15 个在播节点中
5 个如此，含 v5-gz2*、v13-cold、v95-*-mc-cold 族；放行的含 v26*、v9*、v27-cold）。
只查主机白名单会把这类节点当命中交出去 —— 小程序端就是「预览能放、保存 403」。
因此命中白名单后，再以小程序真实身份（servicewechat.com Referer，微信强制携带、
客户端改不掉）向该节点取 1 字节复核；拒绝的节点视同未摇中，继续摇。

复核结论按主机名做进程级缓存（节点策略实测粘性强：v5-gz2-a 隔十天两次复测都拦），
每个新节点只探一次、之后查表；默认 24 小时保质期（DOUYIN_VOD_MINIPROGRAM_TTL 秒），
到期作废、下次摇到再探 —— 派发池出新名字或节点改配置都不需要人工维护。
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from configs.logging_config import get_logger

logger = get_logger(__name__)

# play 网关主机：这些主机上的 /aweme/v1/play/ 地址会 302 派发到随机节点
PLAY_GATEWAY_HOSTS = {'api-play-hl.amemv.com', 'aweme.snssdk.com', 'www.douyin.com', 'www.iesdouyin.com'}

# 摇签探测头：Range 只取 1 字节，跟完 302 立即断开
_PROBE_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36'),
    'Range': 'bytes=0-0',
}

# 小程序身份复核用的 Referer 默认值：wx.downloadFile 对外一律带
# https://servicewechat.com/{appid}/{version}/page-frame.html，appid 对应
# DeMark 小程序（wx90f4713862652c67，公开信息，与上面白名单同属这套小程序的配置）。
DEFAULT_MINIPROGRAM_REFERER = 'https://servicewechat.com/wx90f4713862652c67/1.0.0/page-frame.html'


def get_probe_referer():
    """小程序身份复核的 Referer；环境变量 DOUYIN_VOD_PROBE_REFERER 优先。"""
    return os.getenv('DOUYIN_VOD_PROBE_REFERER', '').strip() or DEFAULT_MINIPROGRAM_REFERER


def _miniprogram_can_download(url, timeout):
    """以小程序身份（servicewechat Referer）向最终节点取 1 字节。

    206/200 = 该节点真允许小程序下载；403 = 节点 Referer 白名单不放行，交出去
    必然「预览能放、保存 403」。探测请求自身异常（超时/网络抖动）时按放行处理：
    没有证据说明节点拒载，别让复核链路的故障拖垮整个摇签（fail-open，同本模块哲学）。
    """
    headers = dict(_PROBE_HEADERS)
    headers['Referer'] = get_probe_referer()
    try:
        with requests.get(url, headers=headers, stream=True, timeout=timeout) as resp:
            return resp.status_code in (200, 206)
    except Exception:
        return True


# 复核结论的进程级缓存：主机名 -> (可下载?, 记录时间戳)。
# 存「坏」过时的代价只是多摇几次（无害）；存「好」过时会把 403 节点交出去 ——
# 所以带保质期，到期作废重探，最坏污染窗口 = 一个 TTL。
_MINIPROGRAM_VERDICTS = {}
_MINIPROGRAM_VERDICTS_LOCK = threading.Lock()


def _cached_miniprogram_verdict(host, url, timeout):
    """查表为主：有新鲜结论直接用；没见过（或已过期）才真探一次并记表。

    并发解析撞上同一个新主机时可能各探一次（竞态）——重复探一次是无害的幂等操作，
    不值得为去重把探测串行化。服务器重启表自然清空，重新遇一个探一个。
    """
    ttl = _env_int('DOUYIN_VOD_MINIPROGRAM_TTL', 86400)
    now = time.time()
    with _MINIPROGRAM_VERDICTS_LOCK:
        entry = _MINIPROGRAM_VERDICTS.get(host)
    if entry is not None and now - entry[1] <= ttl:
        return entry[0]
    verdict = _miniprogram_can_download(url, timeout)
    with _MINIPROGRAM_VERDICTS_LOCK:
        _MINIPROGRAM_VERDICTS[host] = (verdict, time.time())
    return verdict

# 默认白名单 = 小程序 downloadFile 合法域名清单中的字节系 VOD 节点（60 个）。
# 小程序白名单调整后需同步更新此处，或改用环境变量 DOUYIN_VOD_ALLOWED_HOSTS。
DEFAULT_ALLOWED_HOSTS = frozenset((
    'n98-v-ncdnon.douyinvod.com',
    'v11-default.365yg.com',
    'v11-hcd.douyinvod.com',
    'v11-o.douyinvod.com',
    'v11.douyinvod.com',
    'v13-default.365yg.com',
    'v26-default.365yg.com',
    'v26-hcd.douyinvod.com',
    'v26-kefu.douyinvod.com',
    'v26-web.douyinvod.com',
    'v26.douyinvod.com',
    'v27-daily-a.douyinvod.com',
    'v3-c.douyinvod.com',
    'v3-dy-o.zjcdn.com',
    'v3-kefu.douyinvod.com',
    'v3-web.douyinvod.com',
    'v5-default.365yg.com',
    'v5-dy-o-abtest.zjcdn.com',
    'v5-dy-ov-experiment.zjcdn.com',
    'v5-ex-y.douyinvod.com',
    'v5-gz-a.douyinvod.com',
    'v5-gz2-a.douyinvod.com',
    'v5-hl-mly-ov.zjcdn.com',
    'v5-hl-zenl-ov.zjcdn.com',
    'v5-se-ex-mc-default.365yg.com',
    'v5-se-ex-mc-e.douyinvod.com',
    'v5-se-gddgtc-default.365yg.com',
    'v5-se-gddgtc.douyinvod.com',
    'v5-se-jltc-default.365yg.com',
    'v5-se-qn-daily-cm.douyinvod.com',
    'v5-se-sjy-daily.douyinvod.com',
    'v5-se2-gddgtc-e.douyinvod.com',
    'v5-se2-jltc-e.douyinvod.com',
    'v5-se2-zjnbtc-e.douyinvod.com',
    'v6-cold1.douyinvod.com',
    'v6-daily-a.douyinvod.com',
    'v6-daily-colda.douyinvod.com',
    'v6-default.365yg.com',
    'v6-kefu.douyinvod.com',
    'v81.douyinvod.com',
    'v9-default.365yg.com',
    'v9-hcd.douyinvod.com',
    'v9-kefu.douyinvod.com',
    'v9-v2-mps-cdn.douyinvod.com',
    'v9-y.douyinvod.com',
    'v9.douyinvod.com',
    'v95-aw-default.365yg.com',
    'v95-bj-cold.douyinvod.com',
    'v95-hzyy-thr-daily-a.douyinvod.com',
    'v95-se-zjwztc-default.365yg.com',
    'v95-sz-cold.douyinvod.com',
    'v95-ynkmtc-default.365yg.com',
    'v95-zj-b.douyinvod.com',
    'v95-zj-colda.douyinvod.com',
    'v95-zj-coldb.douyinvod.com',
    'v95-zjb-b.douyinvod.com',
    'v95-zjjx2tc-default.365yg.com',
    'v95-zjjx2tc.douyinvod.com',
    'v96-sz-daily-a.douyinvod.com',
    'v96-y.douyinvod.com',
))

_BYTE_VOD_SUFFIXES = ('douyinvod.com', 'zjcdn.com', '365yg.com')


def _env_bool(key, default):
    val = os.getenv(key, '').strip().lower()
    if not val:
        return default
    return val not in ('0', 'false', 'no', 'off')


def _env_int(key, default):
    val = os.getenv(key, '').strip()
    return int(val) if val.isdigit() and int(val) > 0 else default


def get_allowed_hosts():
    """摇签白名单主机集合；环境变量 DOUYIN_VOD_ALLOWED_HOSTS 优先（逗号分隔完整主机名）。"""
    raw = os.getenv('DOUYIN_VOD_ALLOWED_HOSTS', '').strip()
    if raw:
        hosts = {h.strip() for h in raw.split(',') if h.strip()}
        if hosts:
            return hosts
    return DEFAULT_ALLOWED_HOSTS


def is_play_url(url):
    """是否字节 play 网关地址（会 302 派发到随机 CDN 节点）。"""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ('http', 'https') or not p.netloc:
        return False
    return p.netloc in PLAY_GATEWAY_HOSTS and p.path.startswith('/aweme/v1/play')


def _follow_redirects(play_url, timeout):
    """跟随 302 取最终地址；Range 探测只取 1 字节，异常返回 None。"""
    try:
        with requests.get(play_url, headers=_PROBE_HEADERS, stream=True,
                          allow_redirects=True, timeout=timeout) as resp:
            if resp.status_code in (200, 206):
                return resp.url
    except Exception:
        pass
    return None


def resolve_play_url(play_url, allowed_hosts=None, batch_size=None,
                     max_attempts=None, timeout=3):
    """并发摇签：返回 (best_url, hit)。

    每批 batch_size 个并发请求，as_completed 流式检查：任一结果命中白名单
    且通过小程序身份复核（结论按主机缓存跨解析复用，
    DOUYIN_VOD_MINIPROGRAM_CHECK=0 可关）立即返回，
    不等批内慢请求；整批未命中再摇下一批，累计最多 max_attempts 次。
    全部未命中返回 (最后一次结果, False)；环境变量关闭本功能时返回 (None, False)。
    """
    if not _env_bool('DOUYIN_VOD_RESOLVE_ENABLED', True):
        return None, False
    allowed = allowed_hosts if allowed_hosts is not None else get_allowed_hosts()
    if not allowed:
        return None, False
    batch = batch_size or _env_int('DOUYIN_VOD_RESOLVE_BATCH', 4)
    limit = max_attempts or _env_int('DOUYIN_VOD_RESOLVE_MAX_ATTEMPTS', 12)
    verify_miniprogram = _env_bool('DOUYIN_VOD_MINIPROGRAM_CHECK', True)

    done, last_final = 0, None
    while done < limit:
        n = min(batch, limit - done)
        done += n
        with ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(_follow_redirects, play_url, timeout) for _ in range(n)]
            for fut in as_completed(futures):
                final = fut.result()
                if not final:
                    continue
                last_final = final
                host = urlparse(final).netloc
                if host not in allowed:
                    continue
                if verify_miniprogram and not _cached_miniprogram_verdict(host, final, timeout):
                    logger.info('VOD 摇签命中白名单但节点拒绝小程序下载(403)，重摇: %s', host)
                    continue
                logger.info('VOD 摇签命中白名单且小程序可下载: %s (第 %d 次尝试)',
                            host, done)
                # 命中即返回，剩余 future 随 with 块收尾取消
                for f in futures:
                    f.cancel()
                return final, True
    if last_final:
        logger.info('VOD 摇签 %d 次未命中白名单，退回最后一次: %s',
                    limit, urlparse(last_final).netloc)
    return last_final, False


def resolve_media_url(url):
    """统一入口：play 地址摇签收口；其他地址原样返回（白名单外的字节 CDN 仅记日志）。"""
    if not url or not isinstance(url, str):
        return url
    if is_play_url(url):
        best, _hit = resolve_play_url(url)
        if best:
            return best
        logger.warning('VOD 摇签全部失败，退回 play 原始地址')
        return url
    try:
        host = urlparse(url).netloc
    except Exception:
        return url
    if host and not any(host == s or host.endswith('.' + s) for s in _BYTE_VOD_SUFFIXES):
        return url  # 非字节 VOD 家族，不归本模块管
    if host not in get_allowed_hosts():
        logger.info('直连字节 CDN 地址不在摇签白名单，原样返回: %s', host)
    return url
