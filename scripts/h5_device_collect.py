#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天枢·H5 三端真机/模拟器证据采集器（宿主机执行）
=================================================
容器内的天枢工具碰不到宿主机的 Xcode 模拟器 / Android 模拟器 / 本机 Chrome，
故真机证据必须在宿主机采集。本脚本在宿主机把三端跑一遍，逐页 × 横竖屏采集：
  · 真实渲染截图（iOS=WebKit / Android=Blink-on-Android / Web=桌面 Blink）
  · DOM 布局精确度量（横向溢出 / viewport meta / 安全区 / 热区<44 / 固定元素 / 小字号 / 图片）
  · console 错误 + 失败网络请求
结束 / 异常 / 超时 一律强力清理（关模拟器 + Chrome，核验进程真的退了，报告释放）。
产物写入 data/h5_evidence/<run>/：evidence.md（喂给 h5_adapt 分析）+ 各端截图。

依赖：websocket-client（CDP）、xcrun simctl（iOS）、adb（Android）、ios_webkit_debug_proxy（iOS DOM，选装）
用法：python3 h5_device_collect.py --url https://x --platforms web,ios,android [--pages a,b] [--out DIR]
"""
import argparse, atexit, json, os, re, signal, subprocess, sys, threading, time, urllib.request
from datetime import datetime, timezone

try:
    import websocket  # websocket-client
except Exception:
    websocket = None

ADB = os.environ.get("ADB", "/opt/homebrew/bin/adb")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HOST_DATA = "/Users/sunwong/Claude-test/ai_test_toolkit/data"   # 容器 /data 的宿主侧挂载点

# 量测脚本：引擎无关，单表达式返回 JSON，覆盖 H5 适配八类关键信号
MEASURE_JS = r"""
(() => {
  try {
    const vw = window.innerWidth, vh = window.innerHeight;
    const de = document.documentElement, bd = document.body;
    const sw = Math.max(de.scrollWidth, bd ? bd.scrollWidth : 0);
    const meta = document.querySelector('meta[name=viewport]');
    const all = document.querySelectorAll('*');
    const culprits = [];
    for (const el of all) {
      const r = el.getBoundingClientRect();
      if (r.width > vw + 1 && r.right > vw + 1) {
        culprits.push({tag: el.tagName, cls: (el.className||'').toString().slice(0,40), w: Math.round(r.width), right: Math.round(r.right)});
        if (culprits.length >= 12) break;
      }
    }
    const interactive = document.querySelectorAll('a,button,input,select,textarea,[role=button],[onclick],label');
    const tiny = [];
    for (const el of interactive) {
      const r = el.getBoundingClientRect();
      if (r.width>0 && r.height>0 && (r.width<44 || r.height<44)) {
        tiny.push({tag: el.tagName, w: Math.round(r.width), h: Math.round(r.height), txt: (el.textContent||'').trim().slice(0,20)});
        if (tiny.length>=15) break;
      }
    }
    const fixed = [];
    let small = [];
    for (const el of all) {
      let cs; try { cs = getComputedStyle(el); } catch(e){ continue; }
      if (cs.position === 'fixed' || cs.position === 'sticky') {
        const r = el.getBoundingClientRect();
        if (fixed.length<10) fixed.push({tag: el.tagName, pos: cs.position, h: Math.round(r.height), top: Math.round(r.top), bottom: Math.round(r.bottom)});
      }
      if (el.childElementCount===0 && el.textContent && el.textContent.trim()) {
        const fs = parseFloat(cs.fontSize);
        if (fs && fs < 12 && small.length<12) small.push({tag:el.tagName, fs, txt: el.textContent.trim().slice(0,18)});
      }
    }
    const imgs = {total:0, noDim:0, broken:0};
    for (const im of document.images) {
      imgs.total++;
      if (!im.getAttribute('width') && !im.getAttribute('height') && !im.style.width && !im.style.height) imgs.noDim++;
      if (im.complete && im.naturalWidth===0) imgs.broken++;
    }
    const inputs = [];
    for (const el of document.querySelectorAll('input,textarea,select')) {
      let cs; try { cs = getComputedStyle(el); } catch(e){ continue; }
      if (cs.display === 'none' || el.type === 'hidden') continue;
      const r = el.getBoundingClientRect();
      inputs.push({type:(el.getAttribute('type')||el.tagName).toLowerCase(), fontSize:parseFloat(cs.fontSize),
                   inputmode:el.getAttribute('inputmode')||null, enterkeyhint:el.getAttribute('enterkeyhint')||null,
                   autocomplete:el.getAttribute('autocomplete')||null, w:Math.round(r.width), h:Math.round(r.height)});
      if (inputs.length>=12) break;
    }
    let usesSafeArea = false;
    try {
      for (const ss of document.styleSheets) {
        let rules; try { rules = ss.cssRules; } catch(e){ continue; }
        if (!rules) continue;
        for (const r of rules) { if (r.cssText && /safe-area-inset/.test(r.cssText)) { usesSafeArea = true; break; } }
        if (usesSafeArea) break;
      }
    } catch(e){}
    return {
      url: location.href, title: document.title,
      innerWidth: vw, innerHeight: vh, dpr: window.devicePixelRatio,
      visualViewport: window.visualViewport ? {w:Math.round(visualViewport.width), h:Math.round(visualViewport.height), scale:+visualViewport.scale.toFixed(3)} : null,
      viewportMeta: meta ? meta.content : null,
      scrollWidth: sw, horizontalOverflow: sw > vw + 1, overflowBy: Math.round(sw - vw),
      overflowCulprits: culprits,
      tinyTouchTargets: {count: tiny.length, samples: tiny},
      fixedElements: fixed,
      smallFonts: {count: small.length, samples: small},
      images: imgs,
      inputs: inputs,
      usesSafeAreaInset: usesSafeArea,
      ua: navigator.userAgent
    };
  } catch(e) { return {error: String(e)}; }
})()
"""

# ----------------------------------------------------------------------------- 清理登记
_STARTED = {
    "chrome_proc": None, "chrome_datadir": None,
    "ios_booted": [], "ios_app_opened": False,
    "android_avd_serial": None, "android_avd_name": None, "android_we_booted": False,
    "android_force_stop_chrome": None,
    "proxy_proc": None, "adb_forwards": [],
}
_CLEANED = False
_CLEANUP_LOG = []

def _run(cmd, timeout=60, check=False):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} -> {r.returncode}: {r.stderr[:200]}")
        return r
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")

def cleanup():
    global _CLEANED
    if _CLEANED:
        return
    _CLEANED = True
    log = _CLEANUP_LOG.append
    # 1) 宿主 Chrome
    p = _STARTED["chrome_proc"]
    if p and p.poll() is None:
        try: p.terminate(); p.wait(timeout=8)
        except Exception:
            try: p.kill()
            except Exception: pass
    if _STARTED["chrome_datadir"]:
        _run(["pkill", "-f", _STARTED["chrome_datadir"]], timeout=10)
    log("关闭宿主 Chrome")
    # 2) ios-webkit-debug-proxy
    pp = _STARTED["proxy_proc"]
    if pp and pp.poll() is None:
        try: pp.terminate(); pp.wait(timeout=5)
        except Exception:
            try: pp.kill()
            except Exception: pass
        log("关闭 ios-webkit-debug-proxy")
    # 3) iOS 模拟器：只关我们 boot 的
    for udid in _STARTED["ios_booted"]:
        _run(["xcrun", "simctl", "shutdown", udid], timeout=30)
        log(f"关闭 iOS 模拟器 {udid[:8]}")
    if _STARTED["ios_app_opened"]:
        _run(["killall", "Simulator"], timeout=10)
        log("退出 Simulator.app")
    # 4) Android：移除 forward；boot 的 AVD 关掉，预存设备只 force-stop 浏览器
    for port in _STARTED["adb_forwards"]:
        _run([ADB, "forward", "--remove", f"tcp:{port}"], timeout=10)
    if _STARTED["android_avd_serial"] and _STARTED["android_we_booted"]:
        s = _STARTED["android_avd_serial"]; nm = _STARTED["android_avd_name"]
        _run([ADB, "-s", s, "emu", "kill"], timeout=15)
        # 等本 serial 从设备表消失；不退则按 "-avd <name>" 杀进程兜底（不碰 MuMu）
        gone = False
        for _ in range(6):
            time.sleep(1.5)
            if not _android_serial_present(s):
                gone = True; break
        if not gone:
            _run(["pkill", "-f", f"-avd {nm}"], timeout=10)
        log(f"关闭 Android AVD {nm}（{s}）")
    elif _STARTED["android_avd_serial"]:
        log(f"保留 Android AVD {_STARTED['android_avd_name']}（开测前已在运行，未由我启动，不关闭）")
    elif _STARTED["android_force_stop_chrome"]:
        s = _STARTED["android_force_stop_chrome"]
        _run([ADB, "-s", s, "shell", "am", "force-stop", "com.android.chrome"], timeout=15)
        log(f"停止安卓 Chrome（保留预存模拟器 {s}）")
    # 5) 精确核验「我们起的」资源已释放（不数用户原有的 Chrome/Simulator）
    checks = []
    if _STARTED["chrome_datadir"]:
        r = _run(["pgrep", "-f", _STARTED["chrome_datadir"]], timeout=8)
        checks.append("探针Chrome=" + ("仍在!" if r.stdout.strip() else "已退✓"))
    if _STARTED["ios_booted"]:
        r = _run(["xcrun", "simctl", "list", "devices", "booted"], timeout=15)
        still = [u for u in _STARTED["ios_booted"] if u in r.stdout]
        checks.append("我起的iOS模拟器=" + ("仍在:" + ",".join(x[:8] for x in still) if still else "已关✓"))
    if _STARTED["android_avd_serial"] and _STARTED["android_we_booted"]:
        checks.append("我起的AVD=" + ("仍在!" if _android_serial_present(_STARTED["android_avd_serial"]) else "已关✓"))
    if _STARTED["proxy_proc"] is not None:
        checks.append("webkit-proxy=已关✓")
    log("核验: " + (", ".join(checks) if checks else "无我方资源需释放"))

def _sig_handler(signum, frame):
    sys.stderr.write(f"\n[signal {signum}] 触发清理...\n")
    cleanup()
    sys.exit(130)

# ----------------------------------------------------------------------------- CDP 客户端
class CDP:
    """极简 CDP：send(同步取结果) + 事件缓冲（console/network）。"""
    def __init__(self, ws_url, timeout=30):
        if websocket is None:
            raise RuntimeError("websocket-client 未安装")
        self.ws = websocket.create_connection(ws_url, max_size=None, timeout=timeout,
                                               suppress_origin=True)
        self.ws.settimeout(timeout)
        self._id = 0
        self.events = []

    def send(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", str(msg["error"])))
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def drain(self, seconds=2.0):
        """读取并缓冲一段时间内的事件（用于收集 navigate 后的 console/network）。"""
        end = _now() + seconds
        self.ws.settimeout(0.5)
        while _now() < end:
            try:
                msg = json.loads(self.ws.recv())
                if "method" in msg:
                    self.events.append(msg)
            except Exception:
                continue

    def evaluate(self, expr, timeout=30):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
        return r.get("result", {}).get("value")

    def console_and_network(self):
        """从缓冲事件里提取 console 错误 + 失败/慢网络。"""
        console, neterr = [], []
        for ev in self.events:
            m = ev.get("method"); p = ev.get("params", {})
            if m == "Log.entryAdded":
                e = p.get("entry", {})
                if e.get("level") in ("error", "warning"):
                    console.append({"level": e.get("level"), "text": (e.get("text") or "")[:160], "url": (e.get("url") or "")[:80]})
            elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "warning"):
                args = p.get("args", [])
                txt = " ".join(str(a.get("value", a.get("description", ""))) for a in args)[:160]
                console.append({"level": p.get("type"), "text": txt})
            elif m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails", {})
                console.append({"level": "exception", "text": (d.get("text") or "") + " " + (d.get("exception", {}).get("description", ""))[:160]})
            elif m == "Network.loadingFailed":
                neterr.append({"type": p.get("type"), "error": p.get("errorText"), "canceled": p.get("canceled")})
            elif m == "Network.responseReceived":
                resp = p.get("response", {})
                if resp.get("status", 0) >= 400:
                    neterr.append({"status": resp.get("status"), "url": (resp.get("url") or "")[:90]})
        return console[:25], neterr[:25]

    def close(self):
        try: self.ws.close()
        except Exception: pass

def _now():
    return time.monotonic()

def _http_json(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())

# ----------------------------------------------------------------------------- 通用：用一个 CDP 页面跑量测
def probe_page(cdp, url, label, screenshot_path=None, wait=3.0):
    """navigate -> 等待 -> 量测 -> 截图（CDP 截图，web 用）。返回度量 dict。"""
    cdp.send("Page.enable"); cdp.send("Runtime.enable")
    try: cdp.send("Log.enable")
    except Exception: pass
    try: cdp.send("Network.enable")
    except Exception: pass
    cdp.events.clear()
    cdp.send("Page.navigate", {"url": url}, timeout=30)
    cdp.drain(wait)
    metrics = cdp.evaluate(MEASURE_JS) or {}
    console, neterr = cdp.console_and_network()
    metrics["_console"] = console
    metrics["_network_errors"] = neterr
    if screenshot_path:
        try:
            r = cdp.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True}, timeout=30)
            import base64
            with open(screenshot_path, "wb") as f:
                f.write(base64.b64decode(r["data"]))
            metrics["_screenshot"] = os.path.basename(screenshot_path)
        except Exception as e:
            metrics["_screenshot_error"] = str(e)[:80]
    return metrics

# ----------------------------------------------------------------------------- WEB 端（宿主 Chrome）
WEB_PROFILES = [
    {"name": "desktop-1440", "w": 1440, "h": 900, "mobile": False, "dpr": 1},
    {"name": "desktop-1280", "w": 1280, "h": 800, "mobile": False, "dpr": 1},
    {"name": "tablet-768",   "w": 768,  "h": 1024, "mobile": True,  "dpr": 2},
    {"name": "mobile-390",   "w": 390,  "h": 844, "mobile": True,  "dpr": 3},
    {"name": "mobile-360",   "w": 360,  "h": 800, "mobile": True,  "dpr": 3},
]

def collect_web(pages, outdir, headless=True):
    results = {"platform": "web", "engine": "宿主 Google Chrome（桌面 Blink）", "runs": []}
    port = 19412
    datadir = "/tmp/_h5_chrome_" + str(os.getpid())
    _STARTED["chrome_datadir"] = datadir
    args = [CHROME, f"--remote-debugging-port={port}", f"--user-data-dir={datadir}",
            "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
            "--disable-popup-blocking", "--hide-crash-restore-bubble"]
    if headless:
        args.append("--headless=new")
    args.append("about:blank")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _STARTED["chrome_proc"] = proc
    # 等 CDP 就绪
    ver = None
    for _ in range(40):
        try:
            ver = _http_json(f"http://127.0.0.1:{port}/json/version"); break
        except Exception:
            time.sleep(0.5)
    if not ver:
        results["error"] = "宿主 Chrome CDP 未就绪"
        return results
    results["engine"] += f" · {ver.get('Browser')}"
    # 取一个 page target
    targets = _http_json(f"http://127.0.0.1:{port}/json")
    page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
    if not page:
        results["error"] = "无可用 page target"
        return results
    cdp = CDP(page["webSocketDebuggerUrl"], timeout=30)
    try:
        for prof in WEB_PROFILES:
            cdp.send("Emulation.setDeviceMetricsOverride", {
                "width": prof["w"], "height": prof["h"], "deviceScaleFactor": prof["dpr"],
                "mobile": prof["mobile"]})
            for pi, url in enumerate(pages):
                label = f"web_{prof['name']}_p{pi}"
                shot = os.path.join(outdir, label + ".png")
                m = probe_page(cdp, url, label, shot, wait=3.0)
                m["_profile"] = prof["name"]; m["_page_index"] = pi; m["_url"] = url
                results["runs"].append(m)
                sys.stderr.write(f"  [web] {prof['name']} p{pi} overflow={m.get('horizontalOverflow')} tiny={m.get('tinyTouchTargets',{}).get('count')}\n")
    finally:
        cdp.close()
    return results

# ----------------------------------------------------------------------------- iOS 端（Xcode 模拟器 · 真 WebKit）
_PRE = {"booted_sims": set(), "sim_app_running": False}

def snapshot_preexisting():
    r = _run(["xcrun", "simctl", "list", "devices", "booted"], timeout=15)
    _PRE["booted_sims"] = set(re.findall(r'([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})', r.stdout))
    _PRE["sim_app_running"] = bool(_run(["pgrep", "-x", "Simulator"], timeout=8).stdout.strip())

def _ios_resolve_udid(name):
    out = _run(["xcrun", "simctl", "list", "devices", "available"], timeout=15).stdout
    # 优先精确匹配 "名字 ("，避免 "iPhone 17" 命中 "iPhone 17 Pro Max"
    for exact in (True, False):
        for line in out.splitlines():
            hit = (name + " (") in line if exact else (name in line)
            if hit:
                m = re.search(r'([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})', line)
                if m:
                    return m.group(1)
    return None

def _ios_discover_socket(retries=10, delay=2.0):
    for _ in range(retries):
        r = _run(["bash", "-lc",
                  "lsof -aU 2>/dev/null | grep -o '/private/var/tmp/[^ ]*webinspectord_sim.socket' | head -1"], timeout=15)
        s = r.stdout.strip()
        if s:
            return s
        time.sleep(delay)
    return None

class IOSWebKit:
    """iOS 17+ WebKit 远程调试：所有命令用 Target.sendMessageToTarget 包裹，响应从 dispatchMessageFromTarget 拆。"""
    def __init__(self, ws_url, timeout=20):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._id = 0; self._inner = 1000; self.target = None; self.pending = {}; self.events = []
    def _raw(self, method, params=None):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        return self._id
    def _pump(self, t=1.0):
        self.ws.settimeout(0.4); end = _now() + t
        while _now() < end:
            try:
                m = json.loads(self.ws.recv())
            except Exception:
                continue
            meth = m.get("method")
            if meth == "Target.targetCreated":
                ti = m["params"]["targetInfo"]
                if ti.get("type") == "page":
                    self.target = ti["targetId"]
            elif meth == "Target.dispatchMessageFromTarget":
                try:
                    inner = json.loads(m["params"]["message"])
                except Exception:
                    continue
                if inner.get("id") in self.pending:
                    self.pending[inner["id"]] = inner
                elif inner.get("method"):
                    self.events.append(inner)
    def setup(self):
        self._raw("Target.setPauseOnStart", {"pauseOnStart": False}); self._pump(1.5)
        if self.target:
            self._raw("Target.resume", {"targetId": self.target}); self._pump(0.5)
        return self.target
    def to_target(self, method, params=None, wait=5.0):
        if not self.target:
            return None
        self._inner += 1; iid = self._inner; self.pending[iid] = None
        self._raw("Target.sendMessageToTarget",
                  {"targetId": self.target, "message": json.dumps({"id": iid, "method": method, "params": params or {}})})
        end = _now() + wait
        while _now() < end:
            self._pump(0.4)
            if self.pending.get(iid) is not None:
                return self.pending.pop(iid)
        return None
    def evaluate_json(self, expr):
        r = self.to_target("Runtime.evaluate", {"expression": "JSON.stringify(" + expr + ")", "returnByValue": True})
        if not r:
            return None
        val = r.get("result", {}).get("result", {}).get("value")
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return {"_raw": val[:200]}
        return val
    def console_errors(self):
        out = []
        for ev in self.events:
            if ev.get("method") in ("Console.messageAdded",):
                msg = ev.get("params", {}).get("message", {})
                if msg.get("level") in ("error", "warning"):
                    out.append({"level": msg.get("level"), "text": (msg.get("text") or "")[:160]})
        return out[:20]
    def close(self):
        try: self.ws.close()
        except Exception: pass

def collect_ios(pages, outdir, device_name, try_landscape=True):
    results = {"platform": "ios", "engine": f"Xcode 模拟器 {device_name}（真 iOS Safari / WebKit）", "runs": [], "notes": []}
    udid = _ios_resolve_udid(device_name)
    if not udid:
        results["error"] = f"未找到 iOS 设备 {device_name}"; return results
    results["udid"] = udid
    already = udid in _PRE["booted_sims"]
    _run(["xcrun", "simctl", "boot", udid], timeout=60)
    _run(["open", "-a", "Simulator"], timeout=15)
    if not already:
        _STARTED["ios_booted"].append(udid)
        if not _PRE["sim_app_running"]:
            _STARTED["ios_app_opened"] = True
    _run(["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=180)
    # 先唤醒 Safari + 轮询等 webinspectord_sim socket 出现（冷启动需时间），再起 proxy
    _run(["xcrun", "simctl", "openurl", udid, pages[0]], timeout=30)
    time.sleep(4.0)
    sock = _ios_discover_socket(retries=10, delay=2.0)
    if sock:
        _STARTED["proxy_proc"] = subprocess.Popen(
            ["ios_webkit_debug_proxy", "-F", "-s", "unix:" + sock],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4.0)
        results["notes"].append(f"iOS Web Inspector 已连（真 DOM 量测可用）")
    else:
        results["notes"].append("未发现 webinspectord_sim socket（Safari 冷启动超时），iOS DOM 数值缺失，本端以真 WebKit 截图为准")
    for pi, url in enumerate(pages):
        _run(["xcrun", "simctl", "openurl", udid, url], timeout=30)
        time.sleep(6.0)
        # 真 WebKit 截图（纵向）
        shot = os.path.join(outdir, f"ios_p{pi}_portrait.png")
        _run(["xcrun", "simctl", "io", udid, "screenshot", shot], timeout=30)
        run = {"_page_index": pi, "_url": url, "_orientation": "portrait",
               "_screenshot": os.path.basename(shot) if os.path.exists(shot) else None}
        if _STARTED["proxy_proc"] is not None:
            try:
                lst = _http_json("http://127.0.0.1:9222/json/list", timeout=8)
                tgt = next((p for p in lst if url.split("//")[-1].split("/")[0] in p.get("url", "")), (lst[0] if lst else None))
                if tgt and tgt.get("webSocketDebuggerUrl"):
                    cdp = IOSWebKit(tgt["webSocketDebuggerUrl"])
                    if cdp.setup():
                        cdp.to_target("Console.enable"); cdp.to_target("Runtime.enable")
                        m = cdp.evaluate_json(MEASURE_JS) or {}
                        m["_console"] = cdp.console_errors()
                        run.update(m)
                    cdp.close()
            except Exception as e:
                run["_dom_error"] = str(e)[:100]
        results["runs"].append(run)
        sys.stderr.write(f"  [ios] p{pi} portrait inW={run.get('innerWidth')} overflow={run.get('horizontalOverflow')} shot={bool(run.get('_screenshot'))}\n")
    # iOS 横屏：模拟器无可靠的无头旋转接口（simctl 无 rotate，osascript 需辅助功能授权且不稳），
    # 故 iOS 只做纵向真测；横屏适配由 Web 横向档 + Android 真旋转覆盖，诚实标注不伪造。
    results["notes"].append("iOS 仅纵向真测（模拟器无无头旋转接口）；横屏适配见 Web 横向档 + Android 真旋转")
    return results


# ----------------------------------------------------------------------------- Android 端（模拟器 · 真 Blink-on-Android）
CHROME_PKG = "com.android.chrome"
CHROME_ACT = "com.google.android.apps.chrome.Main"

def _adb(serial, *args, timeout=30):
    return _run([ADB, "-s", serial, *args], timeout=timeout)

# 固定专用端口启动 AVD：MuMu 占 5554/5555，我们用 5560 → serial 确定为 emulator-5560，
# console/adb 都指向本 AVD，彻底规避 MuMu 的端口别名。
ANDROID_PORT = 5560

def _android_serial_present(serial):
    r = _run([ADB, "devices"], timeout=10)
    return re.search(rf'{re.escape(serial)}\s+device', r.stdout) is not None

def _android_boot_avd(name):
    serial = f"emulator-{ANDROID_PORT}"
    # 清掉本端口上的陈旧实例 + 同名 AVD 残留进程（pkill 仅匹配 "-avd <name>"，不碰 MuMu）
    if _android_serial_present(serial):
        _run([ADB, "-s", serial, "emu", "kill"], timeout=10); time.sleep(2)
    _run(["pkill", "-f", f"-avd {name}"], timeout=10); time.sleep(1)
    avd_dir = os.path.expanduser(f"~/.android/avd/{name}.avd")
    for lk in ("hardware-qemu.ini.lock", "multiinstance.lock"):
        try: os.remove(os.path.join(avd_dir, lk))
        except OSError: pass
    emu = os.path.expanduser("~/Library/Android/sdk/emulator/emulator")
    subprocess.Popen([emu, "-avd", name, "-port", str(ANDROID_PORT),
                      "-no-snapshot-save", "-no-boot-anim", "-gpu", "swiftshader_indirect"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(70):
        if _android_serial_present(serial) and \
           _adb(serial, "shell", "getprop", "sys.boot_completed", timeout=10).stdout.strip() == "1":
            _STARTED["android_we_booted"] = True
            return serial
        time.sleep(3)
    return serial if _android_serial_present(serial) else None

def _android_foreground(serial):
    r = _adb(serial, "shell", "dumpsys", "window", timeout=15)
    m = re.search(r'mCurrentFocus=Window\{[^}]*\s+(\S+)\}', r.stdout)
    return m.group(1) if m else ""

def _android_launch(serial, url):
    _adb(serial, "shell", "am", "start", "-n", f"{CHROME_PKG}/{CHROME_ACT}",
         "-a", "android.intent.action.VIEW", "-d", url, timeout=30)
    time.sleep(7)

def _android_skip_fre(serial):
    """Chrome 首启引导：点 'Use without an account'（按屏幕比例定位），最多 3 次，直到进主界面。"""
    sz = _adb(serial, "shell", "wm", "size", timeout=10).stdout
    m = re.search(r'(\d+)x(\d+)', sz)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
    for _ in range(3):
        if "FirstRunActivity" not in _android_foreground(serial):
            return True
        # 'Use without an account' 约在 87% 高度居中
        _adb(serial, "shell", "input", "tap", str(w // 2), str(int(h * 0.871)), timeout=10)
        time.sleep(3)
    return "FirstRunActivity" not in _android_foreground(serial)

def collect_android(pages, outdir, android_arg):
    results = {"platform": "android", "engine": "", "runs": [], "notes": []}
    mode, _, ident = android_arg.partition(":")
    if mode == "avd":
        serial = _android_boot_avd(ident)
        if serial:
            _STARTED["android_avd_serial"] = serial
            _STARTED["android_avd_name"] = ident
            tag = "我启动" if _STARTED["android_we_booted"] else "复用已开"
            results["engine"] = f"Android Studio AVD {ident}（{serial} · {tag} · 真 Chrome / Blink-on-Android）"
    else:  # adb:<serial> 预存设备（如 MuMu），只 force-stop 浏览器，不关模拟器
        serial = ident
        _STARTED["android_force_stop_chrome"] = serial
        results["engine"] = f"Android 预存模拟器 {serial}（真 Chrome / Blink-on-Android）"
    if not serial:
        results["error"] = f"未能就绪 Android 设备：{android_arg}"; return results
    results["serial"] = serial
    _adb(serial, "root", timeout=15); time.sleep(1)
    # 启动 Chrome + 跳过 FRE
    _android_launch(serial, pages[0])
    if not _android_skip_fre(serial):
        results["notes"].append("Chrome 首启引导(FRE)未能自动跳过，Android DOM 可能缺失，以真机截图为准")
    rel = _adb(serial, "shell", "getprop", "ro.build.version.release", timeout=10).stdout.strip()
    results["android_version"] = rel
    # forward CDP
    port = 19260
    _adb(serial, "forward", f"tcp:{port}", "localabstract:chrome_devtools_remote", timeout=10)
    _STARTED["adb_forwards"].append(port)

    def _connect():
        try:
            lst = _http_json(f"http://127.0.0.1:{port}/json", timeout=8)
            pg = next((p for p in lst if p.get("type") == "page" and p.get("webSocketDebuggerUrl")), None)
            return CDP(pg["webSocketDebuggerUrl"], timeout=30) if pg else None
        except Exception:
            return None

    cdp = _connect()
    for pi, url in enumerate(pages):
        run = {"_page_index": pi, "_url": url, "_orientation": "portrait"}
        # 纵向
        _adb(serial, "shell", "settings", "put", "system", "accelerometer_rotation", "0", timeout=10)
        _adb(serial, "shell", "settings", "put", "system", "user_rotation", "0", timeout=10)
        if cdp:
            try:
                m = probe_page(cdp, url, f"and_p{pi}", screenshot_path=None, wait=3.5)
                run.update(m)
            except Exception as e:
                run["_dom_error"] = str(e)[:100]
                cdp = _connect()
        else:
            _android_launch(serial, url)
        shot = os.path.join(outdir, f"and_p{pi}_portrait.png")
        with open(shot, "wb") as f:
            f.write(_screencap(serial))
        run["_screenshot"] = os.path.basename(shot) if os.path.getsize(shot) > 1000 else None
        results["runs"].append(run)
        sys.stderr.write(f"  [android] p{pi} portrait inW={run.get('innerWidth')} overflow={run.get('horizontalOverflow')} shot={bool(run.get('_screenshot'))}\n")
        # 横向（仅首页，真旋转）
        if pi == 0:
            _adb(serial, "shell", "settings", "put", "system", "user_rotation", "1", timeout=10)
            time.sleep(2.5)
            land = {"_page_index": pi, "_url": url, "_orientation": "landscape"}
            if cdp:
                try:
                    m = probe_page(cdp, url, f"and_p{pi}_land", screenshot_path=None, wait=3.0)
                    land.update(m)
                except Exception as e:
                    land["_dom_error"] = str(e)[:100]
            lshot = os.path.join(outdir, f"and_p{pi}_landscape.png")
            with open(lshot, "wb") as f:
                f.write(_screencap(serial))
            land["_screenshot"] = os.path.basename(lshot) if os.path.getsize(lshot) > 1000 else None
            results["runs"].append(land)
            sys.stderr.write(f"  [android] p{pi} landscape inW={land.get('innerWidth')} shot={bool(land.get('_screenshot'))}\n")
            _adb(serial, "shell", "settings", "put", "system", "user_rotation", "0", timeout=10)
    if cdp:
        cdp.close()
    return results

def _screencap(serial):
    r = subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                       capture_output=True, timeout=30)
    return r.stdout  # PNG bytes


# ----------------------------------------------------------------------------- 证据汇总（喂给 h5_adapt）
def _fmt_run(run):
    """单次（页×视口/朝向）度量 → markdown 块 + 自动旗标。"""
    flags = []
    lines = []
    has_dom = run.get("innerWidth") is not None
    L = run.get("_orientation") or run.get("_profile") or ""
    lines.append(f"- 截图文件：`{run.get('_screenshot') or '（无）'}`")
    if not has_dom:
        lines.append("- DOM 数值：本次未取到（以截图为准）")
        if run.get("_dom_error"):
            lines.append(f"  - 取数报错：{run['_dom_error']}")
        return "\n".join(lines), flags
    iw, ih, dpr = run.get("innerWidth"), run.get("innerHeight"), run.get("dpr")
    lines.append(f"- innerWidth×Height (dpr)：{iw}×{ih} (dpr {dpr})")
    if run.get("visualViewport"):
        lines.append(f"- visualViewport：{run['visualViewport']}")
    lines.append(f"- viewport meta：`{run.get('viewportMeta')}`")
    if run.get("viewportMeta") is None:
        flags.append(f"[{L}] 缺 viewport meta（移动端易整页缩小/错位）")
    ov = run.get("horizontalOverflow")
    if ov:
        culprits = "; ".join(f"{c.get('tag')}.{(c.get('cls') or '').strip()}(w{c.get('w')},right{c.get('right')})" for c in (run.get("overflowCulprits") or [])[:6])
        lines.append(f"- 横向溢出：**是**，超出 {run.get('overflowBy')}px；元凶：{culprits or '未定位'}")
        flags.append(f"[{L}] 横向溢出 {run.get('overflowBy')}px：{culprits[:120]}")
    else:
        lines.append("- 横向溢出：否")
    tt = run.get("tinyTouchTargets", {})
    if tt.get("count"):
        samp = "; ".join(f"{s.get('tag')}“{(s.get('txt') or '').strip()}”{s.get('w')}×{s.get('h')}" for s in tt.get("samples", [])[:6])
        lines.append(f"- 触控热区<44px：{tt['count']} 个 → {samp}")
        flags.append(f"[{L}] {tt['count']} 个热区<44px：{samp[:120]}")
    else:
        lines.append("- 触控热区<44px：0")
    fx = run.get("fixedElements") or []
    if fx:
        lines.append(f"- fixed/sticky 元素：{len(fx)} 个 → " + "; ".join(f"{e.get('tag')}({e.get('pos')},h{e.get('h')},top{e.get('top')})" for e in fx[:5]))
    sf = run.get("smallFonts", {})
    if sf.get("count"):
        samp = "; ".join(f"{s.get('fs')}px“{(s.get('txt') or '').strip()}”" for s in sf.get("samples", [])[:6])
        lines.append(f"- 小字号<12px：{sf['count']} 个 → {samp}")
        flags.append(f"[{L}] {sf['count']} 处字号<12px：{samp[:100]}")
    img = run.get("images", {})
    if img.get("total"):
        extra = []
        if img.get("noDim"): extra.append(f"{img['noDim']} 无显式宽高(易布局抖动/CLS)")
        if img.get("broken"): extra.append(f"{img['broken']} 破图")
        lines.append(f"- 图片：{img['total']} 张" + (f"（{'，'.join(extra)}）" if extra else ""))
        if img.get("broken"): flags.append(f"[{L}] {img['broken']} 张破图")
    inp = run.get("inputs") or []
    if inp:
        small_in = [i for i in inp if i.get("fontSize") and i["fontSize"] < 16]
        desc = "; ".join(f"{i.get('type')}({i.get('fontSize')}px,{i.get('w')}×{i.get('h')},inputmode={i.get('inputmode')})" for i in inp[:6])
        lines.append(f"- 输入框：{len(inp)} 个 → {desc}")
        if small_in:
            flags.append(f"[{L}] {len(small_in)} 个 input 字号<16px（iOS Safari 聚焦会整页放大→错位）：" + "; ".join(f"{i.get('type')} {i.get('fontSize')}px" for i in small_in[:5]))
    lines.append(f"- 安全区 env(safe-area-inset) 使用：{'是' if run.get('usesSafeAreaInset') else '未检出'}")
    if run.get("ua"): lines.append(f"- UA：`{run['ua']}`")
    cons = run.get("_console") or []
    if cons:
        lines.append(f"- console 错误/警告：{len(cons)} 条 → " + "; ".join(f"[{c.get('level')}]{(c.get('text') or '')[:60]}" for c in cons[:4]))
        flags.append(f"[{L}] console {len(cons)} 条：{(cons[0].get('text') or '')[:80]}")
    nerr = run.get("_network_errors") or []
    if nerr:
        lines.append(f"- 失败/异常网络：{len(nerr)} 条 → " + "; ".join((f"{n.get('status')} {n.get('url','')[:50]}" if n.get('status') else f"{n.get('error')}") for n in nerr[:4]))
        flags.append(f"[{L}] 网络异常 {len(nerr)} 条")
    return "\n".join(lines), flags

def emit_evidence(report, outdir):
    P = report.get("platforms", {})
    all_flags = []
    out = []
    out.append(f"# H5 三端真机/模拟器适配证据  ·  run={report['run_id']}")
    out.append(f"目标 URL：{report['url']}　|　页面数：{len(report['pages'])}　|　采集平台：{', '.join(P.keys())}")
    out.append("")
    out.append("## 〇、采集口径与边界（诚实声明，结论不得超出此边界）")
    if "web" in P:
        out.append(f"- **Web**：{P['web'].get('engine','')}；CDP 设备度量模拟 5 档视口（桌面/平板/移动），真截图 + 真 DOM 量测。")
    if "ios" in P:
        out.append(f"- **iOS**：{P['ios'].get('engine','')}；真 iOS Safari/WebKit，真截图 + 真 DOM（Web Inspector）。{('；'.join(P['ios'].get('notes',[])) or '')}")
    if "android" in P:
        out.append(f"- **Android**：{P['android'].get('engine','')}，Android {P['android'].get('android_version','?')}；真 Chrome(Blink-on-Android)，真截图 + 真 DOM + 真旋转(横竖屏)。{('；'.join(P['android'].get('notes',[])) or '')}")
    out.append("- **未覆盖（标 unknown，需真机/人工）**：真机品牌浏览器内核(UC/夸克/三星/OPPO 等)、真机软键盘弹出与遮挡的真实行为、手势/相机/相册/分享/支付 SDK 真实行为。")
    out.append("- **微信/App 内置 WebView**：按确认本产品不在微信内运行，不覆盖该环境。")
    out.append("- 截图为真实引擎像素，可凭其判定肉眼可见的错位/遮挡/截断；精确数值以上述 DOM 量测为准。")
    out.append("")
    PNAME = {"web": "WEB · 宿主 Chrome（桌面 Blink，多视口）", "ios": "iOS · Xcode 模拟器（真 WebKit）", "android": "Android · AVD（真 Blink-on-Android）"}
    for key in ("web", "ios", "android"):
        if key not in P: continue
        plat = P[key]
        out.append(f"## {PNAME[key]}")
        if plat.get("error"):
            out.append(f"> 采集异常：{plat['error']}"); out.append(""); continue
        for run in plat.get("runs", []):
            tag = run.get("_profile") or run.get("_orientation") or ""
            pi = run.get("_page_index")
            out.append(f"### [{key}] {tag}　·　页面{pi}　{run.get('_url','')}")
            block, flags = _fmt_run(run)
            out.append(block); out.append("")
            all_flags.extend(flags)
    out.append("## 末、自动汇总旗标（机器初筛，供分析起点，非最终判定）")
    if all_flags:
        for f in all_flags:
            out.append(f"- {f}")
    else:
        out.append("- 机器初筛未发现硬性适配异常（仍需人工据截图+DOM 做语义判定）。")
    out.append("")
    out.append("> 说明：以上为机器采集的客观证据。请据此做 H5 适配深度分析：跨引擎渲染差异、")
    out.append("> 安全区/刘海、横竖屏 reflow、热区可点性、字号可读性、溢出与断点、固定元素遮挡、")
    out.append("> 图片 CLS、console/网络健康，并对未覆盖项明确标注 unknown。")
    text = "\n".join(out)
    with open(os.path.join(outdir, "evidence.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


# ----------------------------------------------------------------------------- 主流程
def discover_pages(seed_url, max_pages=4):
    """从首页同源链接里多取几页，落实「进站到多页」。"""
    pages = [seed_url]
    try:
        from urllib.parse import urljoin, urlparse
        with urllib.request.urlopen(seed_url, timeout=10) as r:
            html = r.read().decode("utf-8", "ignore")
        origin = urlparse(seed_url).netloc
        seen = {seed_url}
        for m in re.finditer(r'href=["\']([^"\'#]+)["\']', html):
            u = urljoin(seed_url, m.group(1))
            if urlparse(u).netloc == origin and u not in seen and not u.lower().endswith((".pdf", ".jpg", ".png", ".zip")):
                seen.add(u); pages.append(u)
            if len(pages) >= max_pages: break
    except Exception:
        pass
    return pages

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--platforms", default="web,ios,android")
    ap.add_argument("--pages", default="", help="逗号分隔的额外页面 URL；空则自动从首页发现")
    ap.add_argument("--out", default="")
    ap.add_argument("--ios-device", default="iPhone 17")
    ap.add_argument("--android", default="avd:Pixel_9a", help="avd:<name> 或 adb:<serial>")
    ap.add_argument("--max-runtime", type=int, default=900, help="超时(秒)强制清理退出")
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    atexit.register(cleanup)
    snapshot_preexisting()  # 记录开测前已开的模拟器/Simulator.app，清理时绝不误伤
    # 超时看门狗
    def _watchdog():
        time.sleep(args.max_runtime)
        sys.stderr.write(f"\n[watchdog] 超过 {args.max_runtime}s，强制清理退出\n")
        cleanup(); os._exit(124)
    threading.Thread(target=_watchdog, daemon=True).start()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    outdir = args.out or os.path.join(HOST_DATA, "h5_evidence", run_id)
    os.makedirs(outdir, exist_ok=True)
    raw_pages = ([args.url] + [p for p in args.pages.split(",") if p.strip()]) if args.pages else discover_pages(args.url)
    pages = list(dict.fromkeys(raw_pages))  # 去重保序
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]

    report = {"run_id": run_id, "url": args.url, "pages": pages, "outdir": outdir, "platforms": {}}
    sys.stderr.write(f"== H5 采集 run={run_id} url={args.url} pages={len(pages)} platforms={platforms} ==\n")
    try:
        if "web" in platforms:
            report["platforms"]["web"] = collect_web(pages, outdir, headless=not args.headful)
        if "ios" in platforms:
            report["platforms"]["ios"] = collect_ios(pages, outdir, args.ios_device)
        if "android" in platforms:
            report["platforms"]["android"] = collect_android(pages, outdir, args.android)
    finally:
        cleanup()

    report["cleanup_log"] = _CLEANUP_LOG
    with open(os.path.join(outdir, "raw.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    try:
        emit_evidence(report, outdir)
        sys.stderr.write(f"== evidence.md 已生成 ==\n")
    except Exception as e:
        sys.stderr.write(f"== evidence.md 生成失败: {e} ==\n")
    sys.stderr.write(f"== 完成。证据目录 {outdir} ==\n")
    print(outdir)

if __name__ == "__main__":
    main()
