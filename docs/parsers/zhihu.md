# 知乎 (Zhihu) 逆向解析指南

本篇详细记录知乎问答（Answer）、专栏文章（Article）、视频（Zvideo）与想法（Pin / Video Pin）的多形态解析方案。

---

## 1. 平台特征与支持能力

* **平台标识**：`知乎`
* **支持媒体类型**：
  * 专栏文章（顶部题图、正文高清插图图集、文章摘要与正文）
  * 知乎视频 (MP4) 与 Video Pin
  * 想法（Pin 图文及内嵌视频）
  * 问答回答正文与作者信息
* **常见链接形态**：
  * 专栏文章：`https://zhuanlan.zhihu.com/p/262266851` 或 `https://www.zhihu.com/p/262266851`
  * 想法：`https://www.zhihu.com/pin/2066168388699807826`
  * 视频：`https://www.zhihu.com/zvideo/12345678`
  * 问答：`https://www.zhihu.com/question/123/answer/456`
* **Cookie 依赖**：**可选（常规免配）**。公开内容默认无需 Cookie 即可解析；当服务部署在机房云服务器 IP 触发知乎风控（HTTP 403 / 错误码 40362）或解析登录可见内容时，可在后台「系统设置」或环境变量中配置 `ZHIHU_COOKIE`。

---

## 2. 核心逆向流程

### 2.1 多形态路由分发 (`_extract_content_id`)
通过正则表达式自动识别链接类型与内容 ID：
* `article` ➔ `api.zhihu.com/articles/{id}` 或网页端专栏文章
* `answer` ➔ `api.zhihu.com/answers/{id}`
* `zvideo` ➔ `api.zhihu.com/videos/{id}`
* `pin` ➔ `api.zhihu.com/pins/{id}`

### 2.2 媒体提取规则
* **封面图提取 (`get_cover_photo_url`)**：
  * 专栏文章优先提取顶部题图（`title_image`、`titleImage`、`title_img`）；
  * 视频 / 想法优先提取 `thumbnail` 或 `cover_url`；
  * 若无专有封面，则自动回退提取正文图集的第一张图片。
* **图集提取 (`get_image_list`)**：
  * 专栏文章自动将题图置于图集首位；
  * 从正文 HTML 中通过 BeautifulSoup 解析 `<img>` 标签的 `data-original`、`data-actualsrc` 或 `src` 原图，去重后拼装返回。
* **视频提取 (`get_real_video_url`)**：
  * 从 `playlist` 播放列表按码率和分辨率降序排序，选取最高清晰度（HD/SD）MP4 直链；
  * 支持解析正文中内嵌的 Lens 视频组件（`lens.zhihu.com/api/v4/videos/{lens_id}`）。

### 2.3 容灾与降级机制 (`_fetch_from_html`)
当知乎移动端 API 遭遇 403 风控或网络异常时，解析器自动回退至网页端抓取：
1. 优先解析网页中的 `<script id="js-initialData">`，从 Next.js SSR `initialState` 中提取结构化文章、问答或视频实体；
2. 兜底解析页面 `<meta property="og:image">`、`<meta property="og:title">` 及富文本正文容器。

---

## 3. 测试与验证

* **单元测试**：[tests/test_zhihu_parser.py](file:///Users/leo/Projects/media-parser/tests/test_zhihu_parser.py)
* **执行命令**：`python3 -m unittest tests/test_zhihu_parser.py`
