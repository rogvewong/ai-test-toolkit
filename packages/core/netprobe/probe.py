"""弱网/断网探测 — Playwright + CDP Network.emulateNetworkConditions 真实限速/断网。

对目标 URL 在每个网络档位下真实加载,采集:
  - 是否成功到达(reached)、HTTP 状态、加载时长、FCP、可见文本量、资源数
  - 控制台错误、是否出现「加载中/错误提示/降级内容」(弱网&断网体验关键)
  - 断网态:是否给出友好错误页(而非白屏/原始报错)
并做韧性测试:在线加载 → 断网重载 → 恢复在线重载,看是否自动恢复。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# 网络档位(下行/上行 bytes/s,延迟 ms;-1 表示不限速)
NET_PROFILES: dict[str, dict[str, Any]] = {
    "WiFi/基线":   {"offline": False, "dl": -1, "ul": -1, "lat": 0},
    "4G":          {"offline": False, "dl": 4 * 1024 * 1024 // 8, "ul": 3 * 1024 * 1024 // 8, "lat": 20},
    "3G(快)":     {"offline": False, "dl": 1600 * 1024 // 8, "ul": 750 * 1024 // 8, "lat": 150},
    "弱网(慢3G)": {"offline": False, "dl": 400 * 1024 // 8, "ul": 400 * 1024 // 8, "lat": 400},
    "2G/极弱":     {"offline": False, "dl": 256 * 1024 // 8, "ul": 256 * 1024 // 8, "lat": 800},
    "断网/离线":   {"offline": True, "dl": 0, "ul": 0, "lat": 0},
}


@dataclass
class ProfileResult:
    profile: str
    reached: bool = False
    status: int | None = None
    load_ms: int | None = None
    fcp_ms: int | None = None
    text_len: int = 0
    resources: int = 0
    console_errors: int = 0
    timed_out: bool = False
    has_error_ui: bool = False     # 是否出现"网络错误/重试"等提示文案
    has_spinner: bool = False      # 是否出现加载态
    verdict: str = "未测"
    note: str = ""
    screenshot: str = ""           # 文件名


@dataclass
class NetProbeData:
    url: str = ""
    tested_at: str = ""
    profiles: list[ProfileResult] = field(default_factory=list)
    recovery: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict
        return {
            "url": self.url, "tested_at": self.tested_at,
            "profiles": [asdict(p) for p in self.profiles],
            "recovery": self.recovery, "notes": self.notes,
        }


_FCP_JS = "() => { const e=performance.getEntriesByName('first-contentful-paint')[0]; return e?Math.round(e.startTime):null; }"
_ERR_KEYWORDS = ["网络", "重试", "加载失败", "无法连接", "断网", "offline", "network error",
                 "retry", "连接超时", "请检查网络", "稍后再试"]
_SPIN_HINT = ["loading", "加载中", "spinner", "正在加载"]


async def _probe_one(browser, url: str, profile: str, cfg: dict[str, Any],
                     shots_dir, name_prefix: str, idx: int, timeout_ms: int) -> ProfileResult:
    import time as _t
    res = ProfileResult(profile=profile)
    ctx = await browser.new_context(viewport={"width": 390, "height": 844})  # 移动端视口更贴弱网场景
    page = await ctx.new_page()
    errs = []
    page.on("console", lambda m: errs.append(m.type) if m.type == "error" else None)
    try:
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Network.enable")
        await cdp.send("Network.emulateNetworkConditions", {
            "offline": cfg["offline"],
            "downloadThroughput": cfg["dl"],
            "uploadThroughput": cfg["ul"],
            "latency": cfg["lat"],
        })
    except Exception as exc:
        res.note = f"CDP 限速失败: {str(exc)[:80]}"
    t0 = _t.time()
    try:
        resp = await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        res.reached = True
        res.status = resp.status if resp else None
        res.load_ms = int((_t.time() - t0) * 1000)
        await page.wait_for_timeout(1500)
        try:
            res.fcp_ms = await page.evaluate(_FCP_JS)
            res.text_len = await page.evaluate("() => (document.body&&document.body.innerText||'').length")
            res.resources = await page.evaluate("() => performance.getEntriesByType('resource').length")
            body_txt = (await page.evaluate("() => (document.body&&document.body.innerText||'').toLowerCase()")) or ""
            res.has_error_ui = any(k.lower() in body_txt for k in _ERR_KEYWORDS)
            html = (await page.content()).lower()
            res.has_spinner = any(k in html for k in _SPIN_HINT)
        except Exception:
            pass
    except Exception as exc:
        res.timed_out = "timeout" in str(exc).lower()
        res.reached = False
        res.load_ms = int((_t.time() - t0) * 1000)
        res.note = (res.note + " | " if res.note else "") + f"加载失败: {type(exc).__name__}"
    res.console_errors = len(errs)
    # 截图
    try:
        fn = f"{name_prefix}_{idx}_{profile.replace('/', '_')}.png"
        await page.screenshot(path=str(shots_dir / fn))
        res.screenshot = fn
    except Exception:
        pass
    # 判定
    res.verdict = _verdict_profile(res, cfg)
    await ctx.close()
    return res


def _verdict_profile(r: ProfileResult, cfg: dict[str, Any]) -> str:
    if cfg["offline"]:
        # 断网:理想是给出友好错误提示页;白屏/纯报错算不通过
        if r.has_error_ui:
            return "通过"
        if r.text_len > 40:
            return "警告"   # 有内容但没明确错误提示(可能是缓存或未处理)
        return "不通过"     # 白屏
    # 在线档位:看是否成功加载 + 速度
    if not r.reached:
        return "不通过"
    if r.timed_out:
        return "不通过"
    if r.load_ms is None:
        return "警告"
    # 弱网档位放宽阈值
    if cfg["dl"] == -1 or cfg["dl"] >= 1600 * 1024 // 8:  # WiFi/4G/快3G
        return "通过" if r.load_ms <= 5000 else "警告"
    # 慢档位
    if r.load_ms <= 12000:
        return "通过"
    return "警告" if r.load_ms <= 25000 else "不通过"


async def probe_network(
    url: str,
    shots_dir: Any,
    *,
    name_prefix: str = "net",
    profiles: dict[str, dict[str, Any]] | None = None,
    timeout_ms: int = 30000,
    on_progress: Any = None,
) -> NetProbeData:
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    profiles = profiles or NET_PROFILES
    data = NetProbeData(url=url)
    from pathlib import Path as _P
    shots_dir = _P(shots_dir)
    shots_dir.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        data.notes.append("playwright 未安装")
        return data
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        idx = 0
        for pname, cfg in profiles.items():
            idx += 1
            if on_progress:
                try:
                    on_progress(pname, idx, len(profiles))
                except Exception:
                    pass
            try:
                r = await _probe_one(browser, url, pname, cfg, shots_dir, name_prefix, idx, timeout_ms)
            except Exception as exc:
                r = ProfileResult(profile=pname, note=f"探测异常: {str(exc)[:80]}", verdict="未测")
            data.profiles.append(r)
        # 韧性测试:断网→恢复
        try:
            data.recovery = await _recovery_test(browser, url, timeout_ms)
        except Exception as exc:
            data.recovery = {"error": str(exc)[:120]}
        await browser.close()
    return data


async def _recovery_test(browser, url: str, timeout_ms: int) -> dict[str, Any]:
    """在线加载 → 断网重载 → 恢复在线重载,观察是否自动恢复。"""
    out: dict[str, Any] = {}
    ctx = await browser.new_context(viewport={"width": 390, "height": 844})
    page = await ctx.new_page()
    cdp = await ctx.new_cdp_session(page)
    await cdp.send("Network.enable")
    try:
        await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        out["online_ok"] = True
    except Exception:
        out["online_ok"] = False
    # 断网重载
    await cdp.send("Network.emulateNetworkConditions", {"offline": True, "dl": 0, "ul": 0, "latency": 0}
                   if False else {"offline": True, "downloadThroughput": 0, "uploadThroughput": 0, "latency": 0})
    try:
        await page.reload(timeout=8000, wait_until="domcontentloaded")
        txt = (await page.evaluate("() => (document.body&&document.body.innerText||'').toLowerCase()")) or ""
        out["offline_has_error_ui"] = any(k.lower() in txt for k in _ERR_KEYWORDS)
        out["offline_text_len"] = len(txt)
    except Exception:
        out["offline_reload_failed"] = True
    # 恢复在线
    await cdp.send("Network.emulateNetworkConditions", {"offline": False, "downloadThroughput": -1, "uploadThroughput": -1, "latency": 0})
    try:
        await page.reload(timeout=timeout_ms, wait_until="domcontentloaded")
        tlen = await page.evaluate("() => (document.body&&document.body.innerText||'').length")
        out["recovered"] = tlen > 40
        out["recovered_text_len"] = tlen
    except Exception:
        out["recovered"] = False
    await ctx.close()
    return out
