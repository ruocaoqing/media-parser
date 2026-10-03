# -*- coding: utf-8 -*-
"""VOD 摇签的「小程序身份复核」回归。

背景（2026-10-03 实测）：字节 CDN 的 Referer 白名单按节点配置，同一签名地址
存在「无 Referer 206 / 小程序 Referer 403」的节点。摇签此前只查主机白名单，
会把这类节点当命中交出去，小程序端表现为「预览能放、保存 403」。
这些用例锁定：命中白名单后必须再以小程序 Referer 复核，拒绝的节点视同未摇中。
复核结论按主机名做进程级缓存（新节点才探一次、默认 24h 保质期），用例须各自清表。
"""

import os
import threading
import time
import unittest
from unittest.mock import patch

import requests

from src.utils import vod_dispatch
from src.utils.vod_dispatch import (
    _miniprogram_can_download,
    get_probe_referer,
    resolve_play_url,
)

GZ2 = 'https://v5-gz2-a.douyinvod.com/sig/video/tos/cn/a/?a=1128'   # 白名单内、拒小程序
V26 = 'https://v26.douyinvod.com/sig/video/tos/cn/b/?a=1128'        # 白名单内、放行
STRANGER = 'https://v3-c.douyinvod.com/sig/video/tos/cn/c/?a=1128'  # 白名单外
PLAY = 'https://api-play-hl.amemv.com/aweme/v1/play/?video_id=x'


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScriptedRedirects:
    """按脚本顺序返回最终地址的 _follow_redirects 替身（线程安全）。"""

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.lock = threading.Lock()

    def __call__(self, play_url, timeout):
        with self.lock:
            if not self.sequence:
                return None
            return self.sequence.pop(0)


def hostile_or_friendly(url, timeout):
    """拒小程序的节点 → False，其余 → True。"""
    return GZ2 not in url


class MiniprogramRefererProbeTest(unittest.TestCase):
    def test_sends_miniprogram_referer_and_range(self):
        seen = {}

        def fake_get(url, **kwargs):
            seen.update(kwargs.get('headers') or {})
            return FakeResponse(206)

        with patch.object(vod_dispatch.requests, 'get', side_effect=fake_get):
            self.assertTrue(_miniprogram_can_download(V26, 3))
        self.assertEqual(seen.get('Referer'), get_probe_referer())
        self.assertEqual(seen.get('Range'), 'bytes=0-0')

    def test_status_matrix(self):
        # 206/200 = 小程序真能下载；403 = 节点 Referer 白名单不放行
        for status, expected in ((206, True), (200, True), (403, False), (404, False)):
            with patch.object(vod_dispatch.requests, 'get', return_value=FakeResponse(status)):
                self.assertEqual(_miniprogram_can_download(V26, 3), expected, msg=str(status))

    def test_probe_network_error_fail_open(self):
        # 复核请求自身异常 ≠ 节点拒载：放行，别让复核链路故障拖垮摇签
        with patch.object(vod_dispatch.requests, 'get', side_effect=requests.RequestException('boom')):
            self.assertTrue(_miniprogram_can_download(V26, 3))

    def test_referer_env_override(self):
        with patch.dict(os.environ, {'DOUYIN_VOD_PROBE_REFERER': 'https://example.com/r'}):
            self.assertEqual(get_probe_referer(), 'https://example.com/r')
        self.assertEqual(get_probe_referer(), vod_dispatch.DEFAULT_MINIPROGRAM_REFERER)


class ResolvePlayUrlMiniprogramCheckTest(unittest.TestCase):
    ALLOWED = {'v26.douyinvod.com', 'v5-gz2-a.douyinvod.com'}

    def setUp(self):
        # 复核结论是进程级缓存，用例间必须清表，否则读到上一条的旧结论
        vod_dispatch._MINIPROGRAM_VERDICTS.clear()
        self.addCleanup(vod_dispatch._MINIPROGRAM_VERDICTS.clear)

    def resolve(self, sequence, **kwargs):
        redirects = ScriptedRedirects(sequence)
        with patch.object(vod_dispatch, '_follow_redirects', redirects), \
                patch.object(vod_dispatch, '_miniprogram_can_download', hostile_or_friendly):
            return resolve_play_url('https://api-play-hl.amemv.com/aweme/v1/play/?video_id=x',
                                    allowed_hosts=self.ALLOWED, **kwargs)

    def test_whitelisted_but_403_node_is_rerolled(self):
        # 第一批全落在「白名单内但拒小程序」的节点：必须重摇到放行节点，而不是交出去
        url, hit = self.resolve([GZ2, GZ2, GZ2, V26], batch_size=2, max_attempts=4)
        self.assertTrue(hit)
        self.assertEqual(url, V26)

    def test_host_outside_whitelist_never_wins(self):
        url, hit = self.resolve([STRANGER, STRANGER, STRANGER, STRANGER], batch_size=2, max_attempts=4)
        self.assertFalse(hit)
        self.assertEqual(url, STRANGER)  # fail-open：退回最后一次

    def test_all_whitelisted_hosts_hostile_fails_open(self):
        url, hit = self.resolve([GZ2, GZ2, GZ2, GZ2], batch_size=2, max_attempts=4)
        self.assertFalse(hit)
        self.assertEqual(url, GZ2)

    def test_check_can_be_disabled_by_env(self):
        # DOUYIN_VOD_MINIPROGRAM_CHECK=0 退回旧行为：只查主机白名单
        redirects = ScriptedRedirects([GZ2])
        with patch.dict(os.environ, {'DOUYIN_VOD_MINIPROGRAM_CHECK': '0'}), \
                patch.object(vod_dispatch, '_follow_redirects', redirects), \
                patch.object(vod_dispatch, '_miniprogram_can_download',
                             lambda url, timeout: (_ for _ in ()).throw(AssertionError('不应再复核'))):
            url, hit = resolve_play_url('https://api-play-hl.amemv.com/aweme/v1/play/?video_id=x',
                                        allowed_hosts=self.ALLOWED, batch_size=1, max_attempts=2)
        self.assertTrue(hit)
        self.assertEqual(url, GZ2)

    def test_same_host_probed_once_per_resolve(self):
        calls = []

        def counting_probe(url, timeout):
            calls.append(url)
            return GZ2 not in url

        redirects = ScriptedRedirects([GZ2, GZ2, GZ2, V26])
        with patch.object(vod_dispatch, '_follow_redirects', redirects), \
                patch.object(vod_dispatch, '_miniprogram_can_download', counting_probe):
            url, hit = resolve_play_url('https://api-play-hl.amemv.com/aweme/v1/play/?video_id=x',
                                        allowed_hosts=self.ALLOWED, batch_size=2, max_attempts=4)
        self.assertTrue(hit)
        self.assertEqual(calls.count(GZ2), 1)  # 同一主机只复核一次，结果走缓存


class MiniprogramVerdictCacheTest(unittest.TestCase):
    """复核结论跨解析复用：查表为主，新主机（或过期结论）才真探一次。"""

    ALLOWED = {'v26.douyinvod.com', 'v5-gz2-a.douyinvod.com'}

    def setUp(self):
        vod_dispatch._MINIPROGRAM_VERDICTS.clear()
        self.addCleanup(vod_dispatch._MINIPROGRAM_VERDICTS.clear)

    def test_verdict_reused_across_resolves(self):
        # 第一次解析探过坏节点并记表；第二次解析再摇到它：直接查表，不再发探测请求
        probes = []

        def counting_probe(url, timeout):
            probes.append(url)
            return GZ2 not in url

        for _ in range(2):
            redirects = ScriptedRedirects([GZ2, V26])
            with patch.object(vod_dispatch, '_follow_redirects', redirects), \
                    patch.object(vod_dispatch, '_miniprogram_can_download', counting_probe):
                url, hit = resolve_play_url(PLAY, allowed_hosts=self.ALLOWED,
                                            batch_size=2, max_attempts=4)
            self.assertTrue(hit)
            self.assertEqual(url, V26)
        self.assertEqual(probes.count(GZ2), 1)

    def test_fresh_verdict_skips_probe_entirely(self):
        # 表里已有新鲜的好结论：整轮摇签零探测请求
        vod_dispatch._MINIPROGRAM_VERDICTS['v26.douyinvod.com'] = (True, time.time())
        redirects = ScriptedRedirects([V26])
        with patch.object(vod_dispatch, '_follow_redirects', redirects), \
                patch.object(vod_dispatch, '_miniprogram_can_download',
                             lambda url, timeout: (_ for _ in ()).throw(AssertionError('不应再探测'))):
            url, hit = resolve_play_url(PLAY, allowed_hosts=self.ALLOWED,
                                        batch_size=1, max_attempts=2)
        self.assertTrue(hit)
        self.assertEqual(url, V26)

    def test_expired_verdict_is_reprobed(self):
        # 过保质期的结论作废，重新探一次并覆盖时间戳
        vod_dispatch._MINIPROGRAM_VERDICTS['v26.douyinvod.com'] = (True, time.time() - 10)
        probes = []

        def counting_probe(url, timeout):
            probes.append(url)
            return True

        redirects = ScriptedRedirects([V26])
        with patch.dict(os.environ, {'DOUYIN_VOD_MINIPROGRAM_TTL': '1'}), \
                patch.object(vod_dispatch, '_follow_redirects', redirects), \
                patch.object(vod_dispatch, '_miniprogram_can_download', counting_probe):
            url, hit = resolve_play_url(PLAY, allowed_hosts=self.ALLOWED,
                                        batch_size=1, max_attempts=2)
        self.assertTrue(hit)
        self.assertEqual(probes, [V26])


if __name__ == '__main__':
    unittest.main()
