"""真实 SEO 审计引擎 — 确定性采集层。

crawl_and_audit() 做全站 BFS 爬取 + 逐页 SEO 信号解析 + 技术头 + sitemap/robots
+ 内链图谱,产出结构化 SeoAuditData;Core Web Vitals 由 cwv.measure_cwv() 用
Playwright 实测代表页。两者合并后:① 交给 LLM 综合分析出总览/问题清单;
② 交给 report 层渲染成 10-sheet Excel。
"""
from .audit import crawl_and_audit, SeoAuditData
from .cwv import measure_cwv

__all__ = ["crawl_and_audit", "SeoAuditData", "measure_cwv"]
