#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
天枢·视频弱网采集器（宿主机执行）
=================================
容器内 Playwright Chromium 不带 H.264/AAC 专有编码，CDN 视频(几乎都是 H.264)放不出来，
故视频弱网必须走宿主机真 Google Chrome(有编码)。CDP `Network.emulateNetworkConditions`
是【浏览器上下文级】限速——宿主机本机网络完全不受影响、跑完即复位，绝不会让宿主机断网。

播放器无关：不管 HLS.js / dash.js / video.js / 自研 MSE，最终都喂给一个标准 `<video>` 元素，
本采集器只给这个 `<video>` 插标准 HTML5 监听 + 抓 CDN 分片网络，因此无需知道播放器技术栈。

逐视频页 × 各档位(online→4g→fast_3g→slow_3g→2g→offline→recover)驱动：
起播(TTFF)/缓冲卡顿(waiting)/自适应码率(分辨率变化)/seek 到未缓冲/断网断流/恢复续播/CDN 分片失败/
用户提示(loading/错误/断网文案)，产出 evidence.md 喂给 network_resilience 分析。

用法: python3 net_video_collect.py --url <视频页URL> [--pages a,b] [--play-seconds 8] [--out DIR]
依赖: websocket-client、宿主机 Google Chrome
"""
import argparse, atexit, base64, json, os, re, signal, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

try:
    import websocket  # websocket-client
except Exception:
    websocket = None

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HOST_DATA = "/Users/sunwong/Claude-test/ai_test_toolkit/data"
SEG_RE = re.compile(r'\.(m3u8|mpd|ts|m4s|mp4|cmfv|cmfa|webm|key)(\?|$)', re.I)

# 视频弱网档位(CDP Network.emulateNetworkConditions；dl/ul 字节/秒，lat 毫秒)
PROFILES = [
    ("online",  {"offline": False, "dl": -1, "ul": -1, "lat": 0}),
    ("4g",      {"offline": False, "dl": 4 * 1024 * 1024 // 8, "ul": 3 * 1024 * 1024 // 8, "lat": 20}),
    ("fast_3g", {"offline": False, "dl": 1600 * 1024 // 8, "ul": 750 * 1024 // 8, "lat": 150}),
    ("slow_3g", {"offline": False, "dl": 400 * 1024 // 8, "ul": 400 * 1024 // 8, "lat": 400}),
    ("2g",      {"offline": False, "dl": 256 * 1024 // 8, "ul": 256 * 1024 // 8, "lat": 800}),
]
ONLINE = {"offline": False, "dl": -1, "ul": -1, "lat": 0}
OFFLINE = {"offline": True, "dl": 0, "ul": 0, "lat": 0}

# <video> 通用插桩：事件监听 + 状态采集到 window.__vm（播放器无关）
INSTRUMENT_JS = r"""
(() => {
  const v = document.querySelector('video');
  if (!v) return {found:false};
  if (window.__vm && window.__vm._inst) return {found:true, already:true, src:(v.currentSrc||v.src||'').slice(0,140)};
  const vm = window.__vm = {_inst:true, t0:Date.now(), tff:null, waiting:0, stalled:0, playing:0,
                            seeking:0, errors:0, errCode:null, resChanges:[], lastW:0, events:[]};
  ['waiting','stalled','playing','seeking','seeked','error','canplay','loadedmetadata','ended'].forEach(t=>{
    v.addEventListener(t, ()=>{
      vm[t]=(vm[t]||0)+1;
      vm.events.push({t, ms:Date.now()-vm.t0, ct:+(v.currentTime||0).toFixed(2)});
      if(vm.events.length>240) vm.events.shift();
      if(t==='playing' && vm.tff===null) vm.tff = Date.now()-vm.t0;
      if(t==='error'){ vm.errors++; vm.errCode = v.error && v.error.code; }
    });
  });
  vm._poll = setInterval(()=>{
    if(v.videoWidth && v.videoWidth!==vm.lastW){
      vm.resChanges.push({ms:Date.now()-vm.t0, w:v.videoWidth, h:v.videoHeight, ct:+(v.currentTime||0).toFixed(2)});
      vm.lastW = v.videoWidth;
    }
  }, 500);
  return {found:true, src:(v.currentSrc||v.src||'').slice(0,140)};
})()
"""

READ_JS = r"""
(() => {
  const v = document.querySelector('video'); if(!v) return {found:false};
  const vm = window.__vm || {};
  const buf = v.buffered.length ? +v.buffered.end(v.buffered.length-1).toFixed(1) : 0;
  const toasts = [...document.querySelectorAll('[class*=toast i],[class*=error i],[class*=dialog i],[class*=modal i],[class*=tip i],[class*=retry i],[class*=net i],[role=alert]')]
      .map(e=>(e.innerText||'').trim()).filter(t=>t && t.length<80).slice(0,6);
  const loadingEls = document.querySelectorAll('[class*=load i],[class*=spin i],[class*=buffer i],[class*=skeleton i]').length;
  return {found:true, currentTime:+(v.currentTime||0).toFixed(2), duration:+(v.duration||0).toFixed(1),
          videoW:v.videoWidth, videoH:v.videoHeight, buffered:buf, bufferAhead:+(buf-(v.currentTime||0)).toFixed(1),
          readyState:v.readyState, networkState:v.networkState, paused:v.paused, ended:v.ended,
          errCode:(v.error&&v.error.code)||null,
          tff:vm.tff||null, waiting:vm.waiting||0, stalled:vm.stalled||0, errors:vm.errors||0,
          resChanges:vm.resChanges||[], loadingEls, toasts};
})()
"""

PLAY_JS = r"""
(async () => {
  const v = document.querySelector('video'); if(!v) return 'no-video';
  v.muted = true;
  try { await v.play(); return 'play-ok ct='+(v.currentTime||0).toFixed(2); }
  catch(e) { return 'play-err:'+e.message; }
})()
"""

# 每档位开测前重置「增量计数器」（保留 tff/t0），使 waiting/resChanges 反映本档位而非累计
RESET_JS = r"""
(() => {
  const vm = window.__vm; if(!vm) return 0;
  vm.waiting=0; vm.stalled=0; vm.errors=0; vm.resChanges=[];
  vm.lastW=(document.querySelector('video')||{}).videoWidth||0; vm.events=[];
  return 1;
})()
"""

# ----------------------------------------------------------------------------- 清理
_STARTED = {"chrome": None, "datadir": None}
_CLEANED = False
_LOG = []

def cleanup():
    global _CLEANED
    if _CLEANED: return
    _CLEANED = True
    p = _STARTED["chrome"]
    if p and p.poll() is None:
        try: p.terminate(); p.wait(timeout=6)
        except Exception:
            try: p.kill()
            except Exception: pass
    if _STARTED["datadir"]:
        subprocess.run(["pkill", "-f", _STARTED["datadir"]], capture_output=True)
    _LOG.append("已关闭探针 Chrome,限速随浏览器销毁,宿主机网络未受任何影响")

def _sig(s, f):
    cleanup(); sys.exit(130)

# ----------------------------------------------------------------------------- CDP
class CDP:
    def __init__(self, ws_url, timeout=30):
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, max_size=None, timeout=timeout)
        self._id = 0
        self.events = []
    def send(self, method, params=None, timeout=30):
        self._id += 1; mid = self._id
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m: raise RuntimeError(m["error"].get("message", str(m["error"])))
                return m.get("result", {})
            if "method" in m: self.events.append(m)
    def drain(self, seconds):
        end = time.monotonic() + seconds
        self.ws.settimeout(0.4)
        while time.monotonic() < end:
            try:
                m = json.loads(self.ws.recv())
                if "method" in m: self.events.append(m)
            except Exception:
                continue
    def ev(self, expr, awaitp=True, timeout=30):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": awaitp}, timeout)
        return r.get("result", {}).get("value")
    def set_net(self, cfg):
        self.send("Network.emulateNetworkConditions", {
            "offline": cfg["offline"], "latency": cfg["lat"],
            "downloadThroughput": cfg["dl"], "uploadThroughput": cfg["ul"]})
    def screenshot(self, path):
        try:
            r = self.send("Page.captureScreenshot", {"format": "png"}, timeout=20)
            with open(path, "wb") as f: f.write(base64.b64decode(r["data"]))
            return os.path.basename(path)
        except Exception:
            return None
    def close(self):
        try: self.ws.close()
        except Exception: pass

def _http_json(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())

def parse_segments(events):
    """从一段 CDP Network 事件里统计 CDN 视频分片请求/失败。"""
    reqs, fails, status4xx = {}, [], []
    manifests = 0
    for e in events:
        m = e.get("method"); p = e.get("params", {})
        if m == "Network.requestWillBeSent":
            u = p.get("request", {}).get("url", "")
            mm = SEG_RE.search(u)
            if mm:
                reqs[p.get("requestId")] = u
                if mm.group(1).lower() in ("m3u8", "mpd"): manifests += 1
        elif m == "Network.responseReceived":
            u = p.get("response", {}).get("url", "")
            if SEG_RE.search(u):
                st = p.get("response", {}).get("status", 0)
                if st >= 400: status4xx.append({"status": st, "url": u[:90]})
        elif m == "Network.loadingFailed":
            rid = p.get("requestId")
            if rid in reqs:
                fails.append({"error": p.get("errorText"), "url": reqs[rid][:90]})
    return {"requested": len(reqs), "manifests": manifests,
            "failed": len(fails) + len(status4xx),
            "fail_samples": (status4xx + fails)[:6]}

# ----------------------------------------------------------------------------- 单视频页弱网采集
def collect_video_page(cdp, url, outdir, page_idx, play_seconds=8, do_seek=True):
    page = {"_url": url, "_page_index": page_idx, "profiles": [], "notes": []}
    cdp.send("Page.navigate", {"url": url}, timeout=40)
    cdp.drain(1.0)  # 尽早插桩,争取在播放器自动起播前挂上监听以测准 TTFF
    # 找 + 插桩 <video>（有的站点播放器懒加载/需点击,重试几次 + 试点播放钮）
    inst = None
    for attempt in range(6):
        inst = cdp.ev(INSTRUMENT_JS, awaitp=False) or {}
        if inst.get("found"): break
        # 试点常见播放入口 / 视频中心唤起播放器
        cdp.ev("""(()=>{const sels=['.play','[class*=play i]','[aria-label*=play i]','button'];
          for(const s of sels){const el=document.querySelector(s); if(el){el.click(); break;}}
          (document.querySelector('video')||{}).click&&document.querySelector('video').click(); return 1;})()""", awaitp=False)
        cdp.drain(2.0)
    if not inst or not inst.get("found"):
        page["notes"].append("未在本页找到 <video> 元素(可能需登录/点击进入播放页,或播放器用 canvas 渲染);本页跳过视频测,以 CDN 分片网络为参照")
        page["video_found"] = False
        # 仍抓一次网络看有无分片
        cdp.drain(2.0)
        page["segments_overall"] = parse_segments(cdp.events)
        return page
    page["video_found"] = True
    page["video_src"] = inst.get("src")

    # 起播:online 档从头 play,量 TTFF
    cdp.set_net(ONLINE)
    ev0 = len(cdp.events)
    cdp.ev(PLAY_JS, timeout=20)
    cdp.drain(play_seconds)
    m = cdp.ev(READ_JS, awaitp=False) or {}
    m["_profile"] = "online"; m["_phase"] = "起播+播放"
    m["_segments"] = parse_segments(cdp.events[ev0:])
    m["_screenshot"] = cdp.screenshot(os.path.join(outdir, f"vid_p{page_idx}_online.png"))
    page["profiles"].append(m)
    sys.stderr.write(f"  [video p{page_idx}] online TTFF={m.get('tff')}ms res={m.get('videoW')}x{m.get('videoH')} ct={m.get('currentTime')} seg={m['_segments']['requested']}\n")

    # 弱网各档:切档 → seek 到未缓冲位(强制弱网取新分片)→ 播 → 量卡顿/降码率/分片失败
    for name, cfg in PROFILES[1:]:
        cdp.set_net(cfg)
        cdp.ev(RESET_JS, awaitp=False)  # 计数器归零 → waiting/resChanges 反映本档位
        ev0 = len(cdp.events)
        if do_seek:
            # seek 到“当前缓冲末尾 + 一点”,逼它在弱网下拉新分片
            cdp.ev("(()=>{const v=document.querySelector('video');if(v&&v.buffered.length){try{v.currentTime=Math.min(v.duration-1,v.buffered.end(v.buffered.length-1)+0.5);}catch(e){}}return 1;})()", awaitp=False)
        cdp.ev(PLAY_JS, timeout=20)
        cdp.drain(play_seconds)
        m = cdp.ev(READ_JS, awaitp=False) or {}
        m["_profile"] = name; m["_phase"] = "弱网播放(seek到未缓冲后)"
        m["_segments"] = parse_segments(cdp.events[ev0:])
        m["_screenshot"] = cdp.screenshot(os.path.join(outdir, f"vid_p{page_idx}_{name}.png"))
        page["profiles"].append(m)
        sys.stderr.write(f"  [video p{page_idx}] {name} waiting={m.get('waiting')} res={m.get('videoW')}x{m.get('videoH')} bufAhead={m.get('bufferAhead')} segFail={m['_segments']['failed']}\n")

    # 断网断流:先恢复 online 缓冲一点 → seek 到未缓冲 → 断网 → 看是否停/报错/提示
    cdp.set_net(ONLINE); cdp.drain(2.0)
    cdp.ev("(()=>{const v=document.querySelector('video');if(v&&v.buffered.length){try{v.currentTime=v.buffered.end(v.buffered.length-1)+0.3;}catch(e){}}return 1;})()", awaitp=False)
    cdp.set_net(OFFLINE)
    cdp.ev(RESET_JS, awaitp=False)
    ev0 = len(cdp.events)
    cdp.ev(PLAY_JS, timeout=20)
    cdp.drain(play_seconds)
    m = cdp.ev(READ_JS, awaitp=False) or {}
    m["_profile"] = "offline"; m["_phase"] = "断网中(seek到未缓冲后)"
    m["_segments"] = parse_segments(cdp.events[ev0:])
    m["_screenshot"] = cdp.screenshot(os.path.join(outdir, f"vid_p{page_idx}_offline.png"))
    page["profiles"].append(m)
    sys.stderr.write(f"  [video p{page_idx}] offline stalled={m.get('stalled')} waiting={m.get('waiting')} err={m.get('errCode')} toasts={m.get('toasts')}\n")

    # 恢复:online → 看是否自动续播、从断点续还是从头、是否重取分片
    ct_before = m.get("currentTime")
    cdp.set_net(ONLINE)
    cdp.ev(RESET_JS, awaitp=False)
    ev0 = len(cdp.events)
    cdp.ev(PLAY_JS, timeout=20)
    cdp.drain(play_seconds)
    m2 = cdp.ev(READ_JS, awaitp=False) or {}
    m2["_profile"] = "recover"; m2["_phase"] = "恢复在线"
    m2["_ct_before_recover"] = ct_before
    m2["_resumed"] = (m2.get("currentTime", 0) or 0) > (ct_before or 0) + 0.5
    m2["_segments"] = parse_segments(cdp.events[ev0:])
    m2["_screenshot"] = cdp.screenshot(os.path.join(outdir, f"vid_p{page_idx}_recover.png"))
    page["profiles"].append(m2)
    sys.stderr.write(f"  [video p{page_idx}] recover resumed={m2['_resumed']} ct={m2.get('currentTime')}(断网时{ct_before}) seg={m2['_segments']['requested']}\n")
    return page

# ----------------------------------------------------------------------------- evidence.md
def emit_evidence(report, outdir):
    out = []
    out.append(f"# 视频播放弱网实测证据  ·  run={report['run_id']}")
    out.append(f"目标视频页：{report['url']}　|　页面数：{len(report['pages'])}")
    out.append("")
    out.append("## 〇、采集口径与边界（诚实声明）")
    out.append("- **执行环境**：宿主机真 Google Chrome(有 H.264/AAC 编码,容器 Playwright Chromium 无编码故不可用)。")
    out.append("- **限速方式**：CDP `Network.emulateNetworkConditions`(浏览器上下文级)——宿主机本机网络全程不受影响、跑完即复位。")
    out.append("- **播放器无关**：只插标准 `<video>` 元素 + 抓 CDN 分片网络,适用任意 HLS/DASH/自研 MSE 播放器。")
    out.append("- **档位**：online / 4g / fast_3g / slow_3g / 2g / offline / recover；弱网档均 seek 到未缓冲位以逼真拉新分片。")
    out.append("- **未覆盖(标 needs_real_device)**：iOS 原生 HLS(AVPlayer,限速会影响整机故不自动化)、真机弱网手感/切网流量提醒/真机性能毫秒；只测免费视频(付费/DRM 不在范围)。")
    out.append("")
    for pg in report["pages"]:
        out.append(f"## 视频页 {pg['_page_index']}：{pg['_url']}")
        if not pg.get("video_found"):
            out.append(f"> {('；'.join(pg.get('notes', [])) or '未找到 <video>')}")
            seg = pg.get("segments_overall", {})
            if seg:
                out.append(f"> 期间 CDN 分片网络：请求 {seg.get('requested')}、失败 {seg.get('failed')}")
            out.append(""); continue
        out.append(f"- 视频源：`{pg.get('video_src')}`")
        out.append("")
        out.append("| 档位 | 阶段 | 起播TTFF | 卡顿waiting | 当前分辨率(ABR) | 缓冲余量s | readyState | 分片请求/失败 | 用户提示/loading |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for m in pg["profiles"]:
            seg = m.get("_segments", {})
            res = f"{m.get('videoW')}x{m.get('videoH')}"
            rc = m.get("resChanges") or []
            res_note = f"(变化{len(rc)}次)" if len(rc) > 1 else ""
            toasts = "；".join(m.get("toasts") or []) or ("loading×%d" % m.get("loadingEls", 0) if m.get("loadingEls") else "—")
            out.append(f"| {m.get('_profile')} | {m.get('_phase','')} | {m.get('tff') if m.get('_profile')=='online' else '—'} | {m.get('waiting')} | {res}{res_note} | {m.get('bufferAhead')} | {m.get('readyState')} | {seg.get('requested')}/{seg.get('failed')} | {toasts[:40]} |")
        out.append("")
        # 关键判读旗标
        flags = []
        on = next((m for m in pg["profiles"] if m["_profile"] == "online"), {})
        if on.get("tff") and on["tff"] > 3000: flags.append(f"online 起播 TTFF={on['tff']}ms 偏慢(>3s)")
        for m in pg["profiles"]:
            if m.get("waiting", 0) >= 2: flags.append(f"{m['_profile']} 档卡顿 {m['waiting']} 次(rebuffer)")
            sf = m.get("_segments", {}).get("failed", 0)
            if sf: flags.append(f"{m['_profile']} 档 CDN 分片失败 {sf} 条(看 token/防盗链/超时):{m['_segments'].get('fail_samples')}")
        off = next((m for m in pg["profiles"] if m["_profile"] == "offline"), {})
        if off:
            if not off.get("toasts") and off.get("errCode") is None and off.get("loadingEls", 0) == 0:
                flags.append("★断网中无任何用户提示(疑似静默失败:不报错、不提示断网)——需重点人工核对截图")
            if off.get("errCode"): flags.append(f"断网播放 video.error.code={off['errCode']}")
        rec = next((m for m in pg["profiles"] if m["_profile"] == "recover"), {})
        if rec:
            flags.append("恢复后" + ("自动续播 ✓(断点续)" if rec.get("_resumed") else "★未自动续播(卡死/需手动,断网时 ct=%s)" % rec.get("_ct_before_recover")))
        abr = [m for m in pg["profiles"] if len(m.get("resChanges") or []) > 1]
        if abr:
            flags.append("观察到分辨率变化(疑似 ABR 自适应):" + ", ".join(f"{m['_profile']}{m['resChanges']}" for m in abr)[:200])
        else:
            flags.append("各档位分辨率未见变化(可能未启用 ABR 自适应,或弱网未触发降档——需结合分片码率人工核)")
        out.append("**机器初筛旗标(供分析起点,非最终判定):**")
        for f in flags: out.append(f"- {f}")
        out.append("")
    out.append("> 请据此做视频弱网深度分析:起播体验、弱网卡顿与 ABR 降码率、seek 续播、断网断流的用户提示(★避免静默失败)、")
    out.append("> 恢复后断点续播、CDN 分片失败(token/防盗链/超时)、各档位劣化是否可接受。未覆盖项标 needs_real_device。")
    txt = "\n".join(out)
    with open(os.path.join(outdir, "evidence.md"), "w", encoding="utf-8") as f:
        f.write(txt)
    return txt

# ----------------------------------------------------------------------------- 主
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="视频播放页 URL")
    ap.add_argument("--pages", default="", help="逗号分隔的额外视频页 URL")
    ap.add_argument("--play-seconds", type=int, default=8)
    ap.add_argument("--no-seek", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--max-runtime", type=int, default=900)
    args = ap.parse_args()
    if websocket is None:
        print(json.dumps({"error": "websocket-client 未安装"})); return

    signal.signal(signal.SIGINT, _sig); signal.signal(signal.SIGTERM, _sig)
    atexit.register(cleanup)
    import threading
    def _wd():
        time.sleep(args.max_runtime); cleanup(); os._exit(124)
    threading.Thread(target=_wd, daemon=True).start()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    outdir = args.out or os.path.join(HOST_DATA, "net_video_evidence", run_id)
    os.makedirs(outdir, exist_ok=True)
    pages = [args.url] + [p for p in args.pages.split(",") if p.strip()]
    pages = list(dict.fromkeys(pages))

    port = 19533
    datadir = "/tmp/_netvid_" + str(os.getpid())
    _STARTED["datadir"] = datadir
    proc = subprocess.Popen(
        [CHROME, f"--remote-debugging-port={port}", f"--user-data-dir={datadir}", "--headless=new",
         "--remote-allow-origins=*", "--autoplay-policy=no-user-gesture-required",
         "--mute-audio", "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _STARTED["chrome"] = proc

    report = {"run_id": run_id, "url": args.url, "pages": [], "outdir": outdir}
    try:
        ver = None
        for _ in range(40):
            try: ver = _http_json(f"http://127.0.0.1:{port}/json/version"); break
            except Exception: time.sleep(0.5)
        if not ver:
            report["error"] = "宿主 Chrome CDP 未就绪"
        else:
            report["chrome"] = ver.get("Browser")
            tg = next((t for t in _http_json(f"http://127.0.0.1:{port}/json") if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
            cdp = CDP(tg["webSocketDebuggerUrl"])
            cdp.send("Page.enable"); cdp.send("Network.enable"); cdp.send("Runtime.enable")
            sys.stderr.write(f"== 视频弱网采集 run={run_id} url={args.url} pages={len(pages)} ==\n")
            for i, u in enumerate(pages):
                report["pages"].append(collect_video_page(cdp, u, outdir, i, play_seconds=args.play_seconds, do_seek=not args.no_seek))
            cdp.set_net(ONLINE)  # 复位
            cdp.close()
    finally:
        cleanup()

    report["cleanup_log"] = _LOG
    with open(os.path.join(outdir, "raw.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    try:
        emit_evidence(report, outdir); sys.stderr.write("== evidence.md 已生成 ==\n")
    except Exception as e:
        sys.stderr.write(f"== evidence.md 失败: {e} ==\n")
    sys.stderr.write(f"== 完成。证据目录 {outdir} ==\n")
    print(outdir)

if __name__ == "__main__":
    main()
