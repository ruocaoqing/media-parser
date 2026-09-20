# 汽水音乐 (Qishui Music) 逆向解析指南

本篇详细记录字节跳动旗下 **汽水音乐** UGC 视频与歌曲音频的提取方案。

---

## 1. 平台特征与支持能力

* **平台标识**：`汽水音乐`
* **支持媒体类型**：高清 UGC 视频 (MP4) / 歌曲音频 (audio_mp4) / 专辑封面 / 歌曲名称与作者 / 歌词
* **常见链接形态**：
  * 分享短链：`https://qishui.douyin.com/s/iX21ep91/`
  * 落地页：`https://music.douyin.com/qishui/share/track?track_id=xxx`
* **Cookie 依赖**：无需 Cookie。

---

## 2. 核心逆向流程

1. **短链跳转**：跟随 302 跳到 `music.douyin.com/qishui/share/track?track_id=...`，优先取 `track_id` 查询参数，其次兼容 `/track/<id>`、`/video/<id>` 路径。
2. **SSR 页面提取**：解析 `_ROUTER_DATA` 里 `loaderData.track_page.audioWithLyricsOption`，取出 `url`（音频直链）、`trackName`、`artistName`、`artistIdStr`、`coverURL`、`duration`、`offsetDuration`。
3. **播放接口兜底**：页面拿不到有效音频时，依次请求 `https://beta-luna.douyin.com/luna/h5/track_v2`、`.../luna/h5/seo_track`，从 `track_player.video_model.video_list[0]` 取 `main_url`。
4. **UGC 视频分支**：`videoOptions` 页面（`/share/ugc_video`）走原逻辑，取视频直链。

---

## 3. VIP 曲目试听片段识别

汽水对**会员曲目**（`label_info.only_vip_playable = true`）向未登录请求下发 30~60 秒试听片段：

* 声明时长：`audioWithLyricsOption.duration` 或 `track.duration`；
* 实际流时长：`audioWithLyricsOption.offsetDuration` 或 `video_model.video_duration`；
* 当流时长显著小于完整时长时，解析器在响应中明确标记 `is_preview: true` 并返回 `full_duration`（秒），防止下游误认为完整音频。

---

## 4. 测试与验证

* **单元测试**：[tests/test_qsmusic_parser.py](file:///Users/leo/Projects/media-parser/tests/test_qsmusic_parser.py)
* **执行命令**：`pytest tests/test_qsmusic_parser.py`
