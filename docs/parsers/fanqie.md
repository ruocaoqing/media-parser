# 番茄小说 / 红果短剧 / 红果漫剧逆向解析指南

本篇详细记录 **番茄小说**、**红果短剧**与**红果漫剧**推广落地页的逆向提取方案及 Referer 防盗链注意事项。

---

## 1. 平台特征与支持能力

* **支持平台标识**：`番茄小说` / `红果短剧` / `红果漫剧`
* **支持媒体类型**：高清视频 (MP4) / 剧集封面 / 剧集标题
* **常见链接形态**：
  * 红果短剧：`https://novelquickapp.com/s/xCkhRnNOiTc/`
  * 番茄小说：`https://changdunovel.com/t/byybBzZfKbg/`
  * 红果漫剧：`https://kylin.hainanyuyue.com/s/bR1qzzEd1A0/` / `https://hainanyuyue.com/`
* **Cookie 依赖**：无需 Cookie

---

## 2. 核心逆向流程

1. **重定向追踪**：短链 302 重定向至字节跳动 `video-animation-share` 推广落地页 (`/ug/pages/video-animation-share?...`)。
2. **提取 HTML 内嵌 JSON 结构**：
   * 页面脚本中包含 `window._ROUTER_DATA` 结构化对象；
   * 从 `loaderData['video-animation-share_page']['pageData']['series_data']` 提取 `title` 与 `play_url`；
   * 备选兜底：解析 HTML `<meta property="og:url">` 与 `<meta property="og:image">`。

---

## 3. ⚠️ 重要发现：CDN Referer 防盗链机制

字节跳动短剧 CDN 域名（`qznovel.com` / `fqnovel.com` / `qznovelvod.com`）开启了严格的 **Referer 防盗链校验**。

### 表现与排查结论
* **带第三方 Referer 访问**（如从第三方网页直接点击链接）：CDN 返回 **HTTP 403 Forbidden**，导致浏览器/播放器显示“无法播放”。
* **无 Referer 访问 (No Referer)**：CDN 返回 **HTTP 200 OK**，视频可 100% 正常播放与下载。

### 客户端/前端接入注意事项
前端在渲染或提供视频播放/下载链接时，需确保剥离 Referer Header：
```html
<!-- 全局 Head 禁用 Referer 发送 -->
<meta name="referrer" content="no-referrer">

<!-- 或在 video 标签显式声明 referrerpolicy -->
<video src="parsed_video_url" referrerpolicy="no-referrer" controls></video>
```

---

## 4. 推广落地页只给 30 秒试看处理机制

### 现象与原理
* 推广落地页中 `series_data.play_url` 携带 `&start=0&end=30`，且 CDN 上的实体切片本身仅 30 秒（约 1.85 MB）。
* 鉴权签名覆盖整个 Query 参数，修改 `end`、删除参数或追加 `&full=1` 均会导致 CDN 返回 **HTTP 403**。

### 完整分集置换方案
当检测到 `play_url` 包含试看标识（`[?&]end=\d+`）时，通过以下逻辑获取完整分集：
1. 从 `window._ROUTER_DATA` 提取：
   * **剧集 ID**：`linkParams.schemeParams.video_id`
   * **分集 ID**：`pageData.used_chapter_id`
2. 请求官网网页播放器：`https://hongguoduanju.com/player/<剧集 ID>/<分集 ID>`。
3. 从页面 `<script type="application/ld+json">` 中提取 `VideoObject.contentUrl` 作为完整无截断视频链接。

### 容错与降级策略
* 若官网播放器请求失败（404、超时）或未找到 `contentUrl`，自动降级沿用分享页原有链接，确保接口整体可用性。
* 严禁回退到 `/player/<剧集 ID>`（默认返回第 1 集），避免造成分集错乱。
