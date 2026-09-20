# 皮皮搞笑 (Pipigaoxiao) 逆向解析指南

本篇详细记录 **皮皮搞笑** 帖子短视频与搞笑内容的逆向提取方案。

---

## 1. 平台特征与支持能力

* **平台标识**：`皮皮搞笑`
* **支持媒体类型**：无水印视频 (MP4) / 图集 / 帖子文案 / 封面 / 作者
* **常见链接形态**：
  * 移动分享页：`https://h5.pipigx.com/pp/post/815491325984?pid=815491325984&type=post`
  * 空间/新版分享页：`https://h5.ippzone.com/spacey/post/897800342766?pid=897800342766&type=post`
* **Cookie 依赖**：无需 Cookie。

---

## 2. 核心逆向流程

1. **ID 提取**：从 Query 参数或 Path 中提取 `pid` / `post_id`。
2. **H5 分享接口**：
   * 接口：`POST https://h5.pipigx.com/ppapi/share/fetch_content`
   * 请求体：`{"mid": null, "pid": <pid>, "type": "post"}`（JSON，表单方式会返回"请求参数错误"）
   * 响应：`data.post`，其中 `content` 为文案、`imgs` 为图片/视频素材、`videos` 为视频直链映射。
3. **视频直链提取**：`post.videos[str(imgs[0].id)].url`。
4. **图集提取**：`post.imgs` 中 `video` 不为真的条目即配图，直链取
   `imgs[i].urls.origin.urls[0]`（原图），缺失时依次回落到 `540` / `360` 变体，
   最后回落 `https://file.ippzone.com/img/view/id/{imgs[i].id}`。
   该 URL 目录下没有文件名，是按图片 ID 动态返回的。

> 视频帖的 `imgs[0]` 是视频本身（`video: 1`，且没有 `urls`），不要当成配图返回。

---

## 3. 测试与验证

* **单元测试**：`python -m unittest tests/test_pipigaoxiao_parser.py`
* **样本验证**：`python tests/manual_verify_parsers.py --platform 皮皮搞笑`
