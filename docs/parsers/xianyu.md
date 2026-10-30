# 闲鱼 (Xianyu) 逆向解析指南

本篇详细记录阿里巴巴旗下 **闲鱼** 二手商品与分享内容的图文及视频解析方案。

---

## 1. 平台特征与支持能力

* **平台标识**：`闲鱼`
* **支持媒体类型**：原画商品图集 (JPEG/PNG) / 封面 / 宝贝完整标题与详情文案 / 标价 / 卖家昵称 / 视频直链
* **常见链接形态**：
  * 口令/短链：`https://e.tb.cn/h.87fj9SNrqHW8kfC?tk=y1E5Tb4wkGd`、`https://m.tb.cn/h.jabJ2N9?tk=R4rQTJFs4aG`
  * 闲鱼 Web/H5 链接：`https://www.goofish.com/item?itemId=834469366646`、`https://h5.m.goofish.com/item?id=...`
  * 商品长链：`https://2.taobao.com/item.htm?id=123456789`、`https://item.taobao.com/item.htm?id=...`
* **Cookie 依赖**：🟢 免配置，无需任何 Cookie 登录态。

---

## 2. 核心逆向流程

1. **短链展开与 itemId 提取**：
   * 请求淘口令或短链（`m.tb.cn` / `e.tb.cn`），从 HTML 内联跳转变量 `var url = '...'` 或查询参数中提取目标商品 `itemId` / `id` 及初始标价。
2. **双层移动端 H5 MTOP 动态令牌签名提取**：
   * **一级高可用接口（抗风控核心）**：`mtop.taobao.idle.item.info` (AppKey: `12574478`)。
     * **免风控特性**：不含个性化追踪负载，完全不受阿里霸下 (Baxia) WAF `RGV587_ERROR` 限制，无需任何登录态与 Cookie。
     * **核心数据提取**：稳定解析 `data.title`（标题）、`data.description`（详细文案）、`data.reservePrice`（价格）、`data.imageInfos`（高清图集与主图，托管于 `img.alicdn.com`，自动清洗裁剪后缀，天然支持 CORS 跨域免防盗链）。
   * **二级扩展接口（非阻塞补充）**：`mtop.taobao.idle.awesome.detail`。
     * 在非阻塞异常捕获下尝试请求，用于补充卖家昵称（`flowData.floating` / `sellerDO.nick`）及视频直链。若遭遇 WAF 拦截则自动跳过，不影响核心图文与价格交付。
   * **令牌握手与动态签名**：
     * 向网关发起首轮请求获取 `_m_h5_tk` 临时令牌。
     * 使用 `md5(token + "&" + t + "&" + appKey + "&" + data)` 计算动态 `sign` 请求数据。
   * **状态感知**：若宝贝已删除或下架（`FAIL_BIZ_ITEM_DEL_NOT_FOUND` / `FAIL_BIZ_ITEM_QUERY_FAILED`），自动设置 `ITEM_NOT_FOUND` 业务提示。

---

## 3. 测试与验证

* **单元测试**：[tests/test_xianyu_parser.py](file:///Users/leo/Projects/media-parser/tests/test_xianyu_parser.py)
* **执行命令**：`python3 -m unittest tests/test_xianyu_parser.py`
