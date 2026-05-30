"""SEO 采集层 — 全站 BFS 爬取 + 逐页解析 + 技术 SEO + sitemap + 内链图谱。

设计目标:对标用户给的 10-sheet SEO 报告模板的「执行方法」(sheet 10):
  - 全站抓取 + 内链图谱(BFS,深度 + 入链 + 死链)
  - 静态 SEO 30+ 项(title/meta/H 标签/img/canonical/og/twitter/hreflang/JSON-LD)
  - 结构化数据校验(按 schema.org 统计 @type、识别 @graph 嵌套)
  - 内链锚文本质量(识别通用词/空锚文本)
  - 技术 SEO(HTTP 版本/TLS/压缩/HSTS/CSP/nosniff/cache)
  - sitemap 验证(下载分片解析 URL + lastmod + 与爬取集对齐)
  - EN 本地化(EN 路径页 title/h1/desc 中文字符计数)

纯确定性、可复现;不调用 LLM。结果是事实层,分析层(总览/问题清单)再交给 LLM。
"""
from __future__ import annotations

import asyncio
import re
import ssl
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse, urldefrag

import httpx
from lxml import html as lxml_html

# ── 通用锚文本(内链质量:这些算"低质量/空锚") ──
_GENERIC_ANCHORS = {
    "", "更多", "点击", "点击这里", "查看更多", "立即观看", "立即查看", "阅读全文",
    "详情", "查看", "了解更多", "more", "click here", "read more", "here", "link",
    "查看详情", "更多内容", "下一页", "上一页",
}

# ── 严重度/状态枚举 ──
PASS, WARN, FAIL = "通过", "警告", "不通过"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TianshuSEOBot/1.0; +https://qatools.icu)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class PageAudit:
    url: str
    status: int = 0
    depth: int = 0
    inlinks: int = 0
    template: str = "其他页"
    is_en: bool = False
    # SEO 信号
    title: str = ""
    title_len: int = 0
    desc: str = ""
    desc_len: int = 0
    meta_keywords_count: int = 0
    h1_count: int = 0
    h1_text: str = ""
    h1_has_i18n_placeholder: bool = False
    heading_skip: bool = False          # 标题层级跳级(H1→H3 缺 H2 等)
    img_total: int = 0
    img_with_alt: int = 0
    img_with_dim: int = 0               # 含 width/height
    alt_pct: int = 0
    canonical: str = ""
    canonical_issue: str = ""           # 自指 / 跨频道 / 跨语言 / 缺
    has_hreflang: bool = False
    hreflang_has_xdefault: bool = False
    has_og: bool = False
    has_twitter: bool = False
    jsonld_count: int = 0
    jsonld_types: list[str] = field(default_factory=list)
    title_cn_chars: int = 0             # EN 本地化:EN 页 title 里中文字符数
    # 逐项判定
    pwf: dict[str, str] = field(default_factory=dict)  # 检查项 -> PASS/WARN/FAIL

    @property
    def p_count(self) -> int:
        return sum(1 for v in self.pwf.values() if v == PASS)

    @property
    def w_count(self) -> int:
        return sum(1 for v in self.pwf.values() if v == WARN)

    @property
    def f_count(self) -> int:
        return sum(1 for v in self.pwf.values() if v == FAIL)


@dataclass
class SeoAuditData:
    base_url: str = ""
    host: str = ""
    crawled_at: str = ""                # 由调用方注入(脚本里无 Date.now)
    pages: list[PageAudit] = field(default_factory=list)
    dead_links: list[dict[str, Any]] = field(default_factory=list)
    orphan_pages: list[str] = field(default_factory=list)
    max_depth: int = 0
    depth_dist: dict[int, int] = field(default_factory=dict)
    internal_links_total: int = 0
    generic_anchor_count: int = 0
    tech: dict[str, Any] = field(default_factory=dict)        # 技术 SEO 头
    sitemap: dict[str, Any] = field(default_factory=dict)     # sitemap 解析
    jsonld_dist: dict[str, int] = field(default_factory=dict) # 全站 @type 分布
    cwv: dict[str, Any] = field(default_factory=dict)         # Core Web Vitals(后注入)
    templates: dict[str, dict[str, Any]] = field(default_factory=dict)  # 各模板聚合
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON 可序列化(含 PageAudit 的 P/W/F 计数),供持久化 + Excel 渲染。"""
        from dataclasses import asdict
        pages = []
        for p in self.pages:
            d = asdict(p)
            d["p_count"], d["w_count"], d["f_count"] = p.p_count, p.w_count, p.f_count
            pages.append(d)
        return {
            "base_url": self.base_url, "host": self.host, "crawled_at": self.crawled_at,
            "pages": pages, "dead_links": self.dead_links, "orphan_pages": self.orphan_pages,
            "max_depth": self.max_depth, "depth_dist": self.depth_dist,
            "internal_links_total": self.internal_links_total,
            "generic_anchor_count": self.generic_anchor_count,
            "tech": self.tech, "sitemap": self.sitemap, "jsonld_dist": self.jsonld_dist,
            "cwv": self.cwv, "templates": self.templates, "notes": self.notes,
            "summary": self.summary_stats(),
        }

    def summary_stats(self) -> dict[str, Any]:
        ok = [p for p in self.pages if p.status == 200]
        return {
            "pages_crawled": len(self.pages),
            "pages_ok": len(ok),
            "dead_links": len(self.dead_links),
            "orphan_pages": len(self.orphan_pages),
            "max_depth": self.max_depth,
            "internal_links": self.internal_links_total,
            "generic_anchor_pct": round(self.generic_anchor_count / max(1, self.internal_links_total) * 100, 2),
            "title_dup": _dup_count([p.title for p in ok if p.title]),
            "desc_short": sum(1 for p in ok if 0 < p.desc_len < 50),
            "desc_missing": sum(1 for p in ok if p.desc_len == 0),
            "alt_below80": sum(1 for p in ok if p.img_total > 0 and p.alt_pct < 80),
            "en_title_cn": sum(1 for p in ok if p.is_en and p.title_cn_chars > 0),
            "h1_i18n_leak": sum(1 for p in ok if p.h1_has_i18n_placeholder),
            "jsonld_missing": sum(1 for p in ok if p.jsonld_count == 0),
        }


def _dup_count(items: list[str]) -> int:
    seen: dict[str, int] = {}
    for it in items:
        seen[it] = seen.get(it, 0) + 1
    return sum(c for c in seen.values() if c > 1)


_CN_RE = re.compile(r"[一-鿿]")
_I18N_PLACEHOLDER_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9]*\.[a-zA-Z][a-zA-Z0-9.]*\b")  # 形如 channelLabel.av


def _looks_i18n_placeholder(text: str) -> bool:
    """H1 文本疑似未渲染的 i18n key(如 channelLabel.av / common.title)。"""
    t = (text or "").strip()
    if not t or _CN_RE.search(t):
        return False
    # 纯英文且形如 a.b.c 的 key、或含明显占位符大括号
    if "{{" in t or "}}" in t:
        return True
    m = _I18N_PLACEHOLDER_RE.fullmatch(t)
    return bool(m)


def _classify_template(path: str, audit: "PageAudit") -> str:
    """通用模板分类(基于路径形态 + 信号),站点无关。"""
    segs = [s for s in path.split("/") if s]
    # 去掉语言前缀 zh/en
    if segs and segs[0] in ("zh", "en", "zh-cn", "en-us"):
        segs = segs[1:]
    n = len(segs)
    if n == 0:
        return "首页"
    last = segs[-1].lower()
    if any(k in path.lower() for k in ("/rank", "/top", "/ranking", "/hot")):
        return "排行榜"
    if any(k in path.lower() for k in ("/tag", "/tags", "/label")):
        return "标签聚合页"
    if any(k in path.lower() for k in ("/chapter", "/read", "/episode")):
        return "章节阅读页"
    if any(k in path.lower() for k in ("/play", "/watch", "/video")):
        return "播放页"
    if any(k in path.lower() for k in ("/section", "/topic", "/special", "/category")):
        return "专题分类页"
    if n == 1:
        return "频道页"
    # 含长 id 段视为详情页
    if any(re.fullmatch(r"[0-9a-f]{8,}", s) or s.isdigit() for s in segs):
        return "详情页"
    return "其他页" if n >= 3 else "频道页"


def audit_page(url: str, status: int, html_text: str, base_host: str) -> PageAudit:
    """解析单页 HTML,抽取全部 SEO 信号并逐项打分。"""
    pa = PageAudit(url=url, status=status)
    parsed = urlparse(url)
    pa.is_en = "/en" in parsed.path.lower() or parsed.path.lower().startswith("/en")
    pa.template = _classify_template(parsed.path, pa)
    if status != 200 or not html_text:
        return pa
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return pa

    def _txt(el) -> str:
        return (el.text_content() or "").strip() if el is not None else ""

    # title
    t = doc.find(".//title")
    pa.title = _txt(t)
    pa.title_len = len(pa.title)
    # meta description / keywords
    for m in doc.xpath("//meta"):
        name = (m.get("name") or m.get("property") or "").lower()
        content = m.get("content") or ""
        if name == "description":
            pa.desc = content.strip()
            pa.desc_len = len(pa.desc)
        elif name == "keywords":
            pa.meta_keywords_count = len([k for k in re.split(r"[,，]", content) if k.strip()])
        elif name.startswith("og:"):
            pa.has_og = True
        elif name.startswith("twitter:"):
            pa.has_twitter = True
    # headings
    levels = []
    for lvl in range(1, 7):
        els = doc.xpath(f"//h{lvl}")
        if lvl == 1:
            pa.h1_count = len(els)
            if els:
                pa.h1_text = _txt(els[0])
                pa.h1_has_i18n_placeholder = any(_looks_i18n_placeholder(_txt(e)) for e in els)
        for e in els:
            levels.append(lvl)
    # 层级跳级:出现 hN 时前面没有 h(N-1)
    seen_levels: set[int] = set()
    pa.heading_skip = False
    for el in doc.xpath("//h1|//h2|//h3|//h4|//h5|//h6"):
        lvl = int(el.tag[1])
        if lvl > 1 and (lvl - 1) not in seen_levels:
            pa.heading_skip = True
        seen_levels.add(lvl)
    # images
    imgs = doc.xpath("//img")
    pa.img_total = len(imgs)
    pa.img_with_alt = sum(1 for im in imgs if (im.get("alt") or "").strip())
    pa.img_with_dim = sum(1 for im in imgs if im.get("width") and im.get("height"))
    pa.alt_pct = round(pa.img_with_alt / pa.img_total * 100) if pa.img_total else 100
    # canonical
    can = doc.xpath("//link[@rel='canonical']/@href")
    if can:
        pa.canonical = can[0]
        cu = urlparse(urljoin(url, pa.canonical))
        if cu.geturl().rstrip("/") == url.rstrip("/"):
            pa.canonical_issue = "自指"
        elif cu.netloc and cu.netloc != base_host:
            pa.canonical_issue = "跨站"
        else:
            # 跨语言?跨频道?
            su = urlparse(url)
            slang = "en" if "/en" in su.path else "zh"
            clang = "en" if "/en" in cu.path else "zh"
            if slang != clang:
                pa.canonical_issue = "跨语言"
            elif su.path.split("/")[:3] != cu.path.split("/")[:3]:
                pa.canonical_issue = "跨频道"
            else:
                pa.canonical_issue = "自指"
    else:
        pa.canonical_issue = "缺"
    # hreflang
    hreflangs = doc.xpath("//link[@rel='alternate']/@hreflang")
    pa.has_hreflang = bool(hreflangs)
    pa.hreflang_has_xdefault = "x-default" in [h.lower() for h in hreflangs]
    # JSON-LD
    types: list[str] = []
    for s in doc.xpath("//script[@type='application/ld+json']"):
        pa.jsonld_count += 1
        types.extend(_extract_jsonld_types(s.text_content() or ""))
    pa.jsonld_types = types
    # EN 本地化:EN 页 title 中文字符
    if pa.is_en:
        pa.title_cn_chars = len(_CN_RE.findall(pa.title))

    _score_page(pa)
    return pa


def _extract_jsonld_types(text: str) -> list[str]:
    import json as _json
    out: list[str] = []
    try:
        data = _json.loads(text)
    except Exception:
        # 宽松:正则抓 @type
        return re.findall(r'"@type"\s*:\s*"([^"]+)"', text)

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            t = o.get("@type")
            if isinstance(t, str):
                out.append(t)
            elif isinstance(t, list):
                out.extend(x for x in t if isinstance(x, str))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return out


def _score_page(pa: PageAudit) -> None:
    """按通过标准(模板 sheet 10)逐项 PASS/WARN/FAIL。"""
    s = pa.pwf
    # Title
    if pa.title_len == 0:
        s["Title"] = FAIL
    elif 10 <= pa.title_len <= 60:
        s["Title"] = PASS
    else:
        s["Title"] = WARN
    # Description
    if pa.desc_len == 0:
        s["Description"] = FAIL
    elif 50 <= pa.desc_len <= 160:
        s["Description"] = PASS
    else:
        s["Description"] = WARN
    # H1
    if pa.h1_count == 0 or pa.h1_has_i18n_placeholder or pa.h1_count > 1:
        s["H1"] = FAIL
    else:
        s["H1"] = PASS
    # 标题层级
    s["标题层级"] = WARN if pa.heading_skip else PASS
    # 图片 Alt
    if pa.img_total == 0:
        s["图片Alt"] = PASS
    elif pa.alt_pct >= 80:
        s["图片Alt"] = PASS
    elif pa.alt_pct >= 50:
        s["图片Alt"] = WARN
    else:
        s["图片Alt"] = FAIL
    # canonical
    if pa.canonical_issue in ("缺", "跨语言", "跨站"):
        s["canonical"] = FAIL
    elif pa.canonical_issue in ("跨频道",):
        s["canonical"] = WARN
    else:
        s["canonical"] = PASS
    # JSON-LD(详情页缺才算 FAIL)
    if pa.jsonld_count > 0:
        s["JSON-LD"] = PASS
    else:
        s["JSON-LD"] = FAIL if pa.template == "详情页" else WARN
    # hreflang
    s["hreflang"] = PASS if pa.hreflang_has_xdefault else (WARN if pa.has_hreflang else FAIL)
    # EN 本地化
    if pa.is_en:
        s["EN本地化"] = FAIL if pa.title_cn_chars > 0 else PASS


async def crawl_and_audit(
    entry_url: str,
    *,
    max_pages: int = 300,
    max_depth: int = 5,
    concurrency: int = 8,
    timeout: float = 20.0,
    on_progress: Any = None,
) -> SeoAuditData:
    """BFS 爬取并逐页审计。同源限制;记录深度/入链/死链。"""
    entry_url = entry_url.strip()
    if not entry_url.startswith("http"):
        entry_url = "https://" + entry_url
    base = urlparse(entry_url)
    host = base.netloc
    data = SeoAuditData(base_url=entry_url, host=host)

    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(entry_url, 0)]
    seen.add(_norm(entry_url))
    inlink_counter: dict[str, int] = {}
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        headers=_HEADERS, timeout=timeout, follow_redirects=True,
        verify=False, limits=httpx.Limits(max_connections=concurrency + 4),
    ) as client:
        while queue and len(data.pages) < max_pages:
            batch = queue[: concurrency * 2]
            queue = queue[concurrency * 2:]

            async def fetch_one(u: str, depth: int):
                async with sem:
                    try:
                        r = await client.get(u)
                        ctype = r.headers.get("content-type", "")
                        text = r.text if "html" in ctype else ""
                        return u, depth, r.status_code, text, r
                    except Exception as exc:
                        return u, depth, 0, "", exc

            results = await asyncio.gather(*[fetch_one(u, d) for u, d in batch])
            for u, depth, status, text, r in results:
                if len(data.pages) >= max_pages:
                    break
                if status == 0 or status >= 400:
                    data.dead_links.append({"url": u, "status": status, "depth": depth})
                    if status == 0:
                        continue
                pa = audit_page(u, status, text, host)
                pa.depth = depth
                data.pages.append(pa)
                data.depth_dist[depth] = data.depth_dist.get(depth, 0) + 1
                data.max_depth = max(data.max_depth, depth)
                if on_progress:
                    try:
                        on_progress(len(data.pages), max_pages, u)
                    except Exception:
                        pass
                # 抽链入队
                if status == 200 and text and depth < max_depth:
                    for link, anchor in _extract_links(text, u):
                        if urlparse(link).netloc != host:
                            continue
                        data.internal_links_total += 1
                        if (anchor or "").strip().lower() in _GENERIC_ANCHORS:
                            data.generic_anchor_count += 1
                        nl = _norm(link)
                        inlink_counter[nl] = inlink_counter.get(nl, 0) + 1
                        if nl not in seen and len(seen) < max_pages * 3:
                            seen.add(nl)
                            queue.append((link, depth + 1))

    # 回填入链数
    for pa in data.pages:
        pa.inlinks = inlink_counter.get(_norm(pa.url), 0)
    data.orphan_pages = [pa.url for pa in data.pages if pa.depth > 0 and pa.inlinks == 0]
    # 全站 JSON-LD 分布
    dist: dict[str, int] = {}
    for pa in data.pages:
        for t in pa.jsonld_types:
            dist[t] = dist.get(t, 0) + 1
    data.jsonld_dist = dict(sorted(dist.items(), key=lambda kv: -kv[1]))
    # 各模板聚合
    data.templates = _aggregate_templates(data)
    # 技术 SEO + sitemap(并行)
    try:
        data.tech = await _tech_seo(entry_url)
    except Exception as exc:
        data.tech = {"error": str(exc)[:200]}
    try:
        data.sitemap = await _fetch_sitemaps(entry_url, {_norm(p.url) for p in data.pages})
    except Exception as exc:
        data.sitemap = {"error": str(exc)[:200]}
    return data


def _norm(u: str) -> str:
    u, _ = urldefrag(u)
    return u.rstrip("/")


def _extract_links(html_text: str, base_url: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        doc = lxml_html.fromstring(html_text)
    except Exception:
        return out
    for a in doc.xpath("//a[@href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absu = urljoin(base_url, href)
        if not absu.startswith("http"):
            continue
        anchor = (a.text_content() or "").strip()
        out.append((absu, anchor))
    return out


def _aggregate_templates(data: SeoAuditData) -> dict[str, dict[str, Any]]:
    agg: dict[str, dict[str, Any]] = {}
    for pa in data.pages:
        if pa.status != 200:
            continue
        t = agg.setdefault(pa.template, {"pages": 0, "pass": 0, "warn": 0, "fail": 0, "issues": {}})
        t["pages"] += 1
        t["pass"] += pa.p_count
        t["warn"] += pa.w_count
        t["fail"] += pa.f_count
        for k, v in pa.pwf.items():
            if v in (WARN, FAIL):
                t["issues"][k] = t["issues"].get(k, 0) + 1
    return agg


async def _tech_seo(url: str) -> dict[str, Any]:
    """技术 SEO 头:HTTP 版本/TLS/压缩/安全头/缓存。"""
    res: dict[str, Any] = {}
    async with httpx.AsyncClient(headers=_HEADERS, timeout=15.0, follow_redirects=True, verify=False) as c:
        r = await c.get(url)
        h = {k.lower(): v for k, v in r.headers.items()}
        res["http_version"] = r.http_version
        res["status"] = r.status_code
        res["https"] = url.startswith("https")
        res["alt_svc"] = h.get("alt-svc", "")
        res["content_encoding"] = h.get("content-encoding", "")
        res["hsts"] = h.get("strict-transport-security", "")
        res["csp"] = h.get("content-security-policy", "")
        res["x_content_type_options"] = h.get("x-content-type-options", "")
        res["x_frame_options"] = h.get("x-frame-options", "")
        res["cache_control"] = h.get("cache-control", "")
        res["server"] = h.get("server", "")
    return res


async def _fetch_sitemaps(base_url: str, crawled: set[str]) -> dict[str, Any]:
    """robots.txt + sitemap 解析:URL 总量、lastmod 覆盖、与爬取集对齐。"""
    p = urlparse(base_url)
    root = f"{p.scheme}://{p.netloc}"
    out: dict[str, Any] = {"robots": {}, "sitemaps": [], "url_count": 0, "with_lastmod": 0,
                           "shards": [], "protocol_in_robots": ""}
    async with httpx.AsyncClient(headers=_HEADERS, timeout=15.0, follow_redirects=True, verify=False) as c:
        # robots.txt
        sm_urls: list[str] = []
        try:
            rb = await c.get(root + "/robots.txt")
            out["robots"] = {"status": rb.status_code, "len": len(rb.text)}
            if rb.status_code == 200:
                for line in rb.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        sm = line.split(":", 1)[1].strip()
                        sm_urls.append(sm)
                        if sm.startswith("http://"):
                            out["protocol_in_robots"] = "http"
                        elif sm.startswith("https://"):
                            out["protocol_in_robots"] = out["protocol_in_robots"] or "https"
                out["robots"]["has_ai_blocker"] = "GPTBot" in rb.text or "CCBot" in rb.text
                out["robots"]["user_agent_groups"] = rb.text.lower().count("user-agent:")
        except Exception:
            pass
        if not sm_urls:
            sm_urls = [root + "/sitemap.xml"]
        # 解析 sitemap(支持 sitemap-index → 分片;限制分片数防爆)
        all_urls: list[str] = []
        with_lastmod = 0
        shards: list[dict[str, Any]] = []
        to_parse = list(sm_urls)
        parsed_count = 0
        index_status = None
        while to_parse and parsed_count < 8:
            sm = to_parse.pop(0)
            parsed_count += 1
            try:
                rs = await c.get(sm)
                if index_status is None:
                    index_status = rs.status_code
                if rs.status_code != 200:
                    shards.append({"url": sm, "status": rs.status_code, "urls": 0})
                    continue
                body = rs.text
                # sitemap-index?
                child = re.findall(r"<sitemap>.*?<loc>(.*?)</loc>", body, re.S)
                if child:
                    for ch in child[:6]:
                        to_parse.append(ch.strip())
                    shards.append({"url": sm, "status": 200, "type": "index", "children": len(child)})
                    continue
                locs = re.findall(r"<url>.*?<loc>(.*?)</loc>(.*?)</url>", body, re.S)
                shard_lastmod = 0
                for loc, rest in locs:
                    all_urls.append(loc.strip())
                    if "<lastmod>" in rest:
                        with_lastmod += 1
                        shard_lastmod += 1
                shards.append({"url": sm, "status": 200, "type": "urlset",
                               "urls": len(locs), "with_lastmod": shard_lastmod})
            except Exception as exc:
                shards.append({"url": sm, "error": str(exc)[:80]})
        out["index_status"] = index_status
        out["sitemaps"] = sm_urls
        out["shards"] = shards
        out["url_count"] = len(all_urls)
        out["with_lastmod"] = with_lastmod
        # 与爬取集对齐:被爬到的页里有多少在 sitemap
        sm_set = {_norm(u) for u in all_urls}
        if crawled:
            covered = sum(1 for u in crawled if u in sm_set)
            out["crawl_coverage_pct"] = round(covered / len(crawled) * 100, 1)
    return out
