# 央视频 (Yangshipin) 逆向解析指南

本篇详细记录中央广播电视总台 5G 新媒体旗舰平台 **央视频 (Yangshipin)** 的微视频、微短剧、精选栏目与图文新闻资讯作品解析方案。

---

## 1. 平台特征与支持能力

* **平台标识**：`央视频`
* **支持媒体类型**：原画/蓝光/超清无水印视频源 (`video_url`) / 文章内嵌多视频 (`video_list`) / 高分辨率封面 (`cover_url`) / 新闻图文高清插图列表 (`image_list`) / 自然段落正文文案 (`description`) / 语音播报音频 (`audio_url`) / 创作者与机构来源信息 / 视频 ID (`vid`) 与 文章 ID (`article_id`)
* **常见链接形态**：
  * 官方分享短链：`https://www.yspapp.cn/5Sqx`, `https://www.yspapp.cn/6j3j`, `https://www.yspapp.cn/d1o`
  * 图文资讯 / 新闻文章：`https://m.yangshipin.cn/static/article.html?articleid=e05kmjv3gty29`
  * 移动端竖屏微短剧/小视频：`https://m.yangshipin.cn/portrait_video?vid=l00005817wl`
  * 移动端常规横屏视频：`https://m.yangshipin.cn/video?type=0&vid=v000007pgfu&cid=...`
  * PC/网页端：`https://yangshipin.cn/...`
* **Cookie 依赖**：🟢 免配置，无需任何 Cookie 登录态。

---

## 2. 核心逆向流程

1. **Meta Refresh 重定向自动跟随**：
   * 央视频短链 `yspapp.cn/{code}` 采用 HTML `<meta http-equiv="refresh" content="0; URL='https://m.yangshipin.cn/...'"/>` 形式执行页面跳转。
   * 解析器在 `WebFetcher` 与 `YangshipinParser` 中双重内置了针对 `<meta http-equiv="refresh">` 的无感自动追踪，直接锁定最终目标落地页（视频页或图文资讯页）。
2. **SSR 双状态机数据提取**：
   * **横屏常规视频**：页面注入全局变量 `window.__STATE_video__`，解析 `payloads.sharevideo` 结构，获取 `title`、`cover_pic`、`cid`、`vid` 及 `om_info.title`（发布机构或频道名）。
   * **竖屏微短剧/短视频**：页面注入全局变量 `window.__STATE_portrait_video__`，解析 `payloads.videoDataList.items[0].videoData`，获取微短剧标题、`shareItem.shareImgUrl`（超清封面）、以及 `detailFollowItem.actorItem` 中的创作者昵称与头像。
3. **图文资讯与文章 OpenAPI 逆向**：
   * 文章页（`static/article.html?articleid={id}`）采用 Vue 单页渲染架构，通过请求官方开放接口 `https://comment.yangshipin.cn/web/article/article_info` 获取结构化数据。
   * 请求携带固定客户端身份校验参数（`vappid: "59306155"`, `vsecret: "b42702bf7309a179d102f3d51b1add2fda0bc7ada64cb801"`, `targetId: 1`），免登录获取完整文章元数据：
     * `head`：文章标题、发布来源机构（`source`）、发布时间与高清主封面（`coverImage`）。
     * `content.images`：提取文章包含的所有原始无损超清配图直链。
     * `content.content`：将富文本 HTML 解析清洗为自然段落纯文本（`description`）。
     * `audioCapsule`：提取文章伴随的语音播报音频（`audio_url`）。
     * `content.videos` / `<cctv_video>`：提取文章内嵌的视频流并自动触发 VOD 签名流解析。
4. **cKey 8.1 签名与 VOD 视频流提取**：
   * 央视频播放器使用 `cKey 8.1` 鉴权算法与 `playvv.yangshipin.cn/playvinfo` 接口交互。
   * 解析器通过先探测获取服务端实时时间戳 `curTime`，然后使用内置纯 Python 实现的 AES-128-CBC + PKCS7 算法生成签名 `cKey`，向 `playvinfo` 接口请求高清/蓝光（1080P/720P）视频下载地址与 `fvkey`，拼接构造得到完整可直接播放的 `.mp4` 直链。

---

## 3. 测试与验证

* **单元测试**：[tests/test_yangshipin_parser.py](file:///Users/leo/Projects/media-parser/tests/test_yangshipin_parser.py)
* **执行命令**：`python3 -m unittest tests/test_yangshipin_parser.py`
