"""Core Web Vitals 实测 — Playwright 真实浏览器渲染采 LCP/CLS/FCP/TTFB/Load + SSR 比。

对标模板 sheet 04:对每个模板挑一个代表页,用 PerformanceObserver 采真实指标。
不全量跑(几百页太慢),按模板取代表页(模板 → 一个 URL)。
"""
from __future__ import annotations

from typing import Any

_CWV_JS = """
() => new Promise((resolve) => {
  const out = {lcp:null, cls:0, fcp:null, ttfb:null, load:null, resources:0, transferKB:0};
  try {
    const nav = performance.getEntriesByType('navigation')[0];
    if (nav) { out.ttfb = Math.round(nav.responseStart); out.load = Math.round(nav.loadEventEnd || nav.duration); }
    const res = performance.getEntriesByType('resource') || [];
    out.resources = res.length;
    out.transferKB = Math.round(res.reduce((s,r)=>s+(r.transferSize||0),0)/1024);
    const fcp = performance.getEntriesByName('first-contentful-paint')[0];
    if (fcp) out.fcp = Math.round(fcp.startTime);
    let lcp = 0;
    try {
      new PerformanceObserver((l)=>{const e=l.getEntries(); const last=e[e.length-1]; if(last) lcp=last.startTime;}).observe({type:'largest-contentful-paint', buffered:true});
    } catch(_){}
    let cls = 0;
    try {
      new PerformanceObserver((l)=>{for(const e of l.getEntries()){ if(!e.hadRecentInput) cls += e.value; }}).observe({type:'layout-shift', buffered:true});
    } catch(_){}
    setTimeout(()=>{ out.lcp = Math.round(lcp); out.cls = Math.round(cls*1000)/1000; resolve(out); }, 2500);
  } catch(e){ resolve(out); }
})
"""


def _verdict(lcp: float | None, cls: float | None) -> str:
    if lcp is None:
        return "未采"
    if lcp <= 2500 and (cls is None or cls <= 0.1):
        return "通过"
    if lcp <= 4000 and (cls is None or cls <= 0.25):
        return "警告"
    return "不通过"


async def measure_cwv(
    template_urls: dict[str, str],
    *,
    timeout: float = 30.0,
    on_progress: Any = None,
) -> dict[str, Any]:
    """对 {模板名: 代表URL} 逐个实测 CWV + SSR 比。返回 {模板: 指标dict}。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "playwright 未安装"}

    results: dict[str, Any] = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for tmpl, url in template_urls.items():
            try:
                # 开 JS 渲染
                page = await browser.new_page(viewport={"width": 1366, "height": 900})
                resp = await page.goto(url, timeout=int(timeout * 1000), wait_until="load")
                await page.wait_for_timeout(2600)
                m = await page.evaluate(_CWV_JS)
                js_text_len = await page.evaluate("() => (document.body && document.body.innerText || '').length")
                await page.close()
                # 关 JS 渲染(SSR 可见文本)
                ssr_ratio = None
                try:
                    ctx = await browser.new_context(java_script_enabled=False)
                    p2 = await ctx.new_page()
                    await p2.goto(url, timeout=int(timeout * 1000), wait_until="domcontentloaded")
                    await p2.wait_for_timeout(600)
                    ssr_text_len = await p2.evaluate("() => (document.body && document.body.innerText || '').length")
                    if js_text_len:
                        ssr_ratio = round(min(1.0, ssr_text_len / max(1, js_text_len)), 2)
                    await ctx.close()
                except Exception:
                    pass
                m["ssr_ratio"] = ssr_ratio
                m["status"] = resp.status if resp else None
                m["verdict"] = _verdict(m.get("lcp"), m.get("cls"))
                results[tmpl] = m
            except Exception as exc:
                results[tmpl] = {"error": str(exc)[:150], "verdict": "未采"}
            if on_progress:
                try:
                    on_progress(tmpl, results[tmpl])
                except Exception:
                    pass
        await browser.close()
    return results
