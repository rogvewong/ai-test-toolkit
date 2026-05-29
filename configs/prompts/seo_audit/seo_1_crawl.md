---
id: seo.1
name: 站点抓取与基础健康
version: 1.0.0
model_tier: sonnet
temperature: 0.2
max_tokens: 6000
placeholders: [站点信息, robots.txt, sitemap.xml, 抓取样本]
output_format: json
output_schema: seo_crawl
---
你是一名资深技术 SEO 专家。请审计该站点的爬虫可达性、健康状态与全局结构。

输入：
- 站点信息（域名 / 主语言 / 多区域 / 协议 / 部署平台 / CDN）：{{站点信息}}
- robots.txt：{{robots.txt}}
- sitemap.xml：{{sitemap.xml}}
- 抓取样本（多页 URL + 状态码 + 响应头 + 重定向链）：{{抓取样本}}

请输出以下维度：

1. **协议与重定向**
   - HTTP → HTTPS 永久重定向是否到位（301，单跳）
   - www / 非 www 选定 + 反向重定向
   - 末尾斜杠 (/) 一致性
   - 重定向链是否 ≤ 1 跳；是否存在循环 / 软 404

2. **robots.txt**
   - 是否存在；语法是否合法
   - 是否误屏蔽核心目录或 sitemap
   - 是否声明 Sitemap: <url>
   - 是否对 Googlebot / Bingbot / GPTBot 等差异化处理（合理性）

3. **sitemap.xml**
   - 是否存在且可达；GZIP 大小
   - URL 数 / 是否分片
   - 每条 lastmod / changefreq / priority 完整度
   - 是否包含 4xx/5xx 或非 canonical 的 URL（污染）
   - 多语言时 hreflang 是否在 sitemap 内

4. **canonical**
   - 每个抓取页面是否声明 rel="canonical" 且自指
   - 重复内容场景是否指向规范 URL

5. **hreflang（多语言）**
   - 是否使用 self + reciprocal（A↔B 互指）
   - x-default 是否声明
   - 与 canonical 是否一致

6. **状态码体检**
   - 4xx / 5xx 比例
   - 软 404（200 但页面是"未找到"内容）

7. **抓取深度**
   - 主导航能否在 ≤3 跳触达所有重要类型页
   - 是否存在 orphan page（不在任何内链中）

### 输出格式（必须是合法 JSON）
```json
{
  "protocol_and_redirects": {
    "https_enforced": true,
    "www_canonical": "non_www",
    "trailing_slash": "consistent",
    "redirect_chain_ok": true,
    "issues": []
  },
  "robots_txt": {
    "present": true,
    "valid": true,
    "blocks_critical": [],
    "sitemap_declared": true
  },
  "sitemap": {
    "reachable": true,
    "url_count": 0,
    "lastmod_coverage": 0.0,
    "contains_non_2xx": false,
    "contains_non_canonical": false,
    "hreflang_in_sitemap": false
  },
  "canonical": {
    "self_canonical_rate": 0.0,
    "issues": []
  },
  "hreflang": {
    "applicable": false,
    "self_and_reciprocal_ok": null,
    "x_default_present": null,
    "consistency_with_canonical": null
  },
  "status_health": {
    "non_2xx_rate": 0.0,
    "soft_404_examples": []
  },
  "crawl_depth": {
    "max_depth_for_key_pages": 0,
    "orphan_pages": []
  },
  "issues": [
    {"id":"SEO-CRW-0001","severity":"high","title":"sitemap 中包含 4 个 404 URL","fix":"从 build 流程过滤"}
  ],
  "confidence": {"score": 0.0, "rationale": "..."}
}
```
