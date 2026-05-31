#!/usr/bin/env python3
"""宿主 Figma 读图助手 — 跑在用户 Mac 上,用真实 Chrome + 按用户隔离的持久 profile。

为什么需要它:
  容器里的浏览器是全新 profile + 数据中心 IP,无法继承用户的 Google SSO 登录。
  本助手跑在宿主,用真实 Chrome(channel=chrome)+ 每个用户独立的持久 profile。
  用户在 step5 点「登录」→ 弹真实 Chrome → Google 一键登录 → 会话持久保存(按用户),
  以后该用户免登录;不同用户各自登录;可「退出登录」清除。

按用户隔离:
  profile 目录 = <base>/u<user_id>;每个用户独立 marker。所有接口带 user 参数。

接口:
  POST /login   {user}      → 后台弹真实 Chrome 让该用户登录;成功写 marker
  GET  /status?user=        → {logged_in, login_running}
  POST /shot    {url, user} → 该用户登录态下打开 Figma 链接截图(base64)
  POST /logout  {user}      → 清除该用户登录(删 profile)
  GET  /health              → {ok}
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROFILE_BASE = os.environ.get("AITK_FIGMA_PROFILE_BASE") or str(Path.home() / ".aitk-figma-profiles")
PORT = int(os.environ.get("AITK_FIGMA_RUNNER_PORT", "8077"))
CHROME_CHANNEL = "chrome"
_REAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
_IGNORE_ARGS = ["--enable-automation"]

_login_lock = threading.Lock()
_login_running: dict[str, bool] = {}   # user → 是否有登录窗口在跑


def _uid(user) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]", "", str(user or "default"))
    return s or "default"


def _profile_dir(user) -> str:
    return str(Path(PROFILE_BASE) / f"u{_uid(user)}")


def _marker(user) -> Path:
    return Path(_profile_dir(user)) / ".figma_logged_in"


def _new_context(pw, headless: bool, user):
    d = _profile_dir(user)
    Path(d).mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        d, channel=CHROME_CHANNEL, headless=headless,
        user_agent=_REAL_UA, viewport={"width": 1600, "height": 1000},
        locale="zh-CN", args=_LAUNCH_ARGS, ignore_default_args=_IGNORE_ARGS,
    )


def _login_worker(user):
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            ctx = _new_context(pw, headless=False, user=user)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto("https://www.figma.com/login", wait_until="domcontentloaded")
            except Exception:
                pass
            deadline = time.time() + 300
            ok = False
            clear_streak = 0
            while time.time() < deadline:
                time.sleep(5)
                try:
                    url_now = page.url.lower()
                    cur = (page.inner_text("body") or "")[:200].lower()
                except Exception:
                    url_now, cur = "", ""
                on_login = ("/login" in url_now or "accounts.google" in url_now
                            or any(k in cur for k in ("sign up", "continue with email")))
                if not on_login:
                    clear_streak += 1
                    if clear_streak >= 2:
                        ok = True
                        break
                else:
                    clear_streak = 0
            if ok:
                m = _marker(user); m.parent.mkdir(parents=True, exist_ok=True)
                m.write_text(str(int(time.time())))
            ctx.close()
    except Exception:
        pass
    finally:
        _login_running[_uid(user)] = False


def _node_from_url(u: str) -> str | None:
    m = re.search(r"node-id=([0-9A-Za-z%:\-]+)", u or "")
    return m.group(1) if m else None


def _capture_frames(page, max_frames: int = 12) -> list[dict]:
    """逐帧抽取:用 Tab 在「顶层 Frame」间循环,每帧 Shift+2 缩放充满画布后截图。

    为什么这样:Figma 里无选择时按 Tab 选中第一个顶层对象,再按 Tab 依次选下一个
    同级对象(不进子层);Shift+2 缩放到选中对象。于是可逐个顶层 Frame 截成单独的图,
    而不是把整张画布(缩略总览)截成一张读不清的图。
    结束条件:截图内容重复(循环回到已截过的帧)或 node-id 重复或到上限。
    """
    frames: list[dict] = []
    # 聚焦画布 + 清空选择,回到顶层
    try:
        page.mouse.click(760, 460)  # 点画布空白处聚焦
        time.sleep(0.4)
    except Exception:
        pass
    for _ in range(3):
        try:
            page.keyboard.press("Escape")
            time.sleep(0.25)
        except Exception:
            pass
    seen_nodes: set[str] = set()
    seen_imgs: set[str] = set()
    for i in range(max_frames):
        try:
            page.keyboard.press("Tab")       # 选中下一个顶层对象
            time.sleep(0.9)
            node = _node_from_url(page.url)
            if node and node in seen_nodes:
                break                         # 循环回到已截过的帧 → 结束
            page.keyboard.press("Shift+2")    # 缩放到选中帧充满画布
            time.sleep(1.1)
            png = page.screenshot(full_page=False)
        except Exception:
            break
        h = hashlib.md5(png).hexdigest()
        if h in seen_imgs:
            break                             # 截图重复 → 已遍历完
        seen_imgs.add(h)
        if node:
            seen_nodes.add(node)
        frames.append({"name": node or f"frame{i + 1}",
                       "png_b64": base64.b64encode(png).decode("ascii")})
    return frames


def shot(url: str, user) -> dict:
    from playwright.sync_api import sync_playwright
    if _login_running.get(_uid(user)):
        return {"ok": False, "error": "登录窗口未关闭,请先完成登录"}
    with sync_playwright() as pw:
        ctx = _new_context(pw, headless=True, user=user)
        try:
            _apply_session(ctx, user)   # 乙案:注入操作机导出的登录会话
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            time.sleep(6)
            txt = (page.inner_text("body") or "")[:300].lower()
            if any(k in txt for k in ("log in", "sign up", "continue with")) and not page.query_selector("canvas"):
                return {"ok": False, "error": "未登录 — 请先在 step5 点「登录 Figma」"}
            try:
                page.wait_for_selector("canvas", timeout=15000)
            except Exception:
                pass
            time.sleep(4)
            # 隐藏左右面板/工具栏(Cmd+\),让每帧截图只剩画布,干净。
            try:
                page.mouse.move(760, 460)
                page.keyboard.press("Meta+Backslash")
                time.sleep(1.2)
            except Exception:
                pass
            # 逐个顶层 Frame 抽成单独的图(B:不再截整张画布缩略图)。
            frames = _capture_frames(page, max_frames=12)
            if not frames:
                # 兜底:老逻辑 — node-id 预选的单帧 / 整张
                try:
                    page.keyboard.press("Shift+2")
                    time.sleep(1.2)
                except Exception:
                    pass
                png = page.screenshot(full_page=False)
                frames = [{"name": _node_from_url(url) or "frame1",
                           "png_b64": base64.b64encode(png).decode("ascii")}]
            try:
                m = _marker(user); m.parent.mkdir(parents=True, exist_ok=True)
                m.write_text(str(int(time.time())))
            except Exception:
                pass
            return {"ok": True, "frames": frames,
                    "png_b64": frames[0]["png_b64"],  # 向后兼容(单帧字段)
                    "error": None}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        finally:
            ctx.close()


_FIGMA_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".aitk-figma-cache")


def _figma_cache_path(file_key: str, prefer_node: str) -> str:
    raw = f"{file_key}__{prefer_node or 'auto'}"
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in raw)
    return os.path.join(_FIGMA_CACHE_DIR, safe + ".json")


def _figma_read_cache(cpath: str):
    try:
        if os.path.exists(cpath):
            with open(cpath, "r", encoding="utf-8") as fh:
                c = json.loads(fh.read())
            if c.get("ok") and c.get("frames"):
                return c
    except Exception:
        pass
    return None


def _figma_api_frames(file_key: str, token: str, prefer_node: str = "",
                      max_frames: int = 15, force: bool = False) -> dict:
    """在宿主机(住宅 IP,不被 Figma WAF 拦)调 Figma REST API 逐帧渲染。

    /v1/files?depth=2 枚举顶层 Frame → 选页 → /v1/images 批量渲染 → 逐帧下载 → base64。
    容器直连 api.figma.com 会被数据中心 IP 403,故由本助手代发。

    缓存:每个文件(file_key+prefer_node)只渲染一次,落盘复用。Figma /v1/images 限流极严
    (low 档,耗光要 ~4.5 天),所以预览/多次跑同一设计稿都走缓存,零 API 调用;force=True 强制重读。
    API 失败时(如 429 限流)若有旧缓存,回退用缓存,保证已读过的文件始终可用。
    """
    import httpx  # 自带 certifi,避免 urllib 在框架 Python 上的证书校验失败

    cpath = _figma_cache_path(file_key, prefer_node)
    if not force:
        cached = _figma_read_cache(cpath)
        if cached:
            cached = dict(cached); cached["_cached"] = True
            return cached

    def _get(url, params=None):
        with httpx.Client(timeout=60.0, follow_redirects=True) as c:
            r = c.get(url, headers={"X-Figma-Token": token}, params=params or {})
            return r.status_code, r.content

    def _fail(msg):
        # API 失败 → 有旧缓存就回退用缓存(限流期间已读过的文件照样能用)
        fb = _figma_read_cache(cpath)
        if fb:
            fb = dict(fb); fb["_cached"] = True; fb["_stale_note"] = msg
            return fb
        return {"ok": False, "error": msg}

    try:
        st, body = _get(f"https://api.figma.com/v1/files/{file_key}", {"depth": "2"})
        if st != 200:
            return _fail(f"Figma files API {st}")
        doc = (json.loads(body) or {}).get("document", {})
        pages = [p for p in doc.get("children", []) if p.get("type") == "CANVAS"]
        if not pages:
            return {"ok": False, "error": "文件无可读页面"}

        def _screen_like(node):
            # 只要"界面"形态的帧:跳过极端长条(banner/分割条)和极小组件/图标。
            bb = node.get("absoluteBoundingBox") or {}
            w, h = bb.get("width") or 0, bb.get("height") or 0
            if not w or not h:
                return True  # 无尺寸信息就不过滤
            ar = w / h
            return 0.18 <= ar <= 3.2 and (w * h) >= 80000

        def _top(page):
            fr = []
            for ch in page.get("children", []):
                t = ch.get("type")
                if t in ("FRAME", "COMPONENT", "COMPONENT_SET", "INSTANCE") and ch.get("visible", True) is not False:
                    if _screen_like(ch):
                        fr.append({"id": ch["id"], "name": ch.get("name", "")})
                elif t == "SECTION":
                    for sub in ch.get("children", []):
                        if (sub.get("type") in ("FRAME", "COMPONENT") and sub.get("visible", True) is not False
                                and _screen_like(sub)):
                            fr.append({"id": sub["id"], "name": sub.get("name", "")})
            return fr

        chosen = None
        if prefer_node:
            for p in pages:
                if prefer_node in {c.get("id") for c in p.get("children", [])}:
                    chosen = p
                    break
        if chosen is None:
            chosen = max(pages, key=lambda p: len(_top(p)))
        frames = _top(chosen)[:max_frames]
        if not frames:
            return {"ok": False, "error": "该页没有顶层 Frame"}

        ids = ",".join(f["id"] for f in frames)
        st2, body2 = _get(f"https://api.figma.com/v1/images/{file_key}",
                          {"ids": ids, "format": "png", "scale": "2"})
        if st2 != 200:
            return _fail(f"Figma images API {st2}")
        images = (json.loads(body2) or {}).get("images", {}) or {}
        out = []
        for f in frames:
            iu = images.get(f["id"])
            if not iu:
                continue
            try:
                with httpx.Client(timeout=60.0, follow_redirects=True) as c:
                    content = c.get(iu).content
            except Exception:
                continue
            if content:
                out.append({"name": f.get("name") or f["id"],
                            "png_b64": base64.b64encode(content).decode("ascii")})
        if not out:
            return _fail("未渲染出任何帧")
        result = {"ok": True, "frames": out, "error": None}
        try:
            os.makedirs(_FIGMA_CACHE_DIR, exist_ok=True)
            with open(cpath, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(result, ensure_ascii=False))
        except Exception:
            pass
        return result
    except Exception as exc:
        return _fail(f"{type(exc).__name__}: {str(exc)[:160]}")


def _session_file(user) -> Path:
    return Path(_profile_dir(user)) / "figma_session.json"


def _map_cookies(raw: list) -> list[dict]:
    """把 Cookie-Editor / Chrome DevTools 导出的 cookie JSON 映射成 Playwright add_cookies 格式。"""
    ss_map = {"no_restriction": "None", "none": "None", "lax": "Lax",
              "strict": "Strict", "unspecified": "Lax", "": "Lax"}
    out: list[dict] = []
    for c in raw or []:
        if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
            continue
        ss = ss_map.get(str(c.get("sameSite") or "").lower().replace("-", "_"), "Lax")
        ck = {
            "name": str(c["name"]),
            "value": str(c.get("value", "")),
            "domain": str(c["domain"]),
            "path": c.get("path") or "/",
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": ss,
        }
        if ss == "None":
            ck["secure"] = True
        exp = c.get("expirationDate") or c.get("expires")
        if exp and not c.get("session"):
            try:
                ck["expires"] = int(float(exp))
            except Exception:
                pass
        out.append(ck)
    return out


def _apply_session(ctx, user) -> bool:
    """抓图前把保存的会话 cookie 注入当前 context(不依赖 Chrome profile 的 cookie 持久化)。"""
    f = _session_file(user)
    if not f.exists():
        return False
    try:
        cks = json.loads(f.read_text())
        if cks:
            ctx.add_cookies(cks)
            return True
    except Exception:
        pass
    return False


def inject_figma_session(raw_cookies: list, user="default") -> dict:
    """乙案:把操作机导出的 Figma 会话 cookie 存盘 + 注入校验,使宿主浏览器「以用户身份登录」。"""
    from playwright.sync_api import sync_playwright
    figma = [c for c in _map_cookies(raw_cookies) if "figma" in c.get("domain", "").lower()]
    if not figma:
        return {"ok": False, "error": "未发现 figma.com 的 Cookie(请在已登录的 figma.com 页面导出)"}
    d = Path(_profile_dir(user)); d.mkdir(parents=True, exist_ok=True)
    _session_file(user).write_text(json.dumps(figma))
    try:
        with sync_playwright() as pw:
            ctx = _new_context(pw, headless=True, user=user)
            try:
                ctx.add_cookies(figma)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("https://www.figma.com/files", timeout=45000, wait_until="domcontentloaded")
                time.sleep(4)
                if "/login" in page.url.lower():
                    try:
                        _session_file(user).unlink()
                    except Exception:
                        pass
                    return {"ok": False, "logged_in": False,
                            "error": "Cookie 注入后仍跳登录页 — 会话可能已过期或缺关键 Cookie,请在 figma.com 重新登录后重新导出"}
                m = _marker(user); m.parent.mkdir(parents=True, exist_ok=True)
                m.write_text(str(int(time.time())))
                return {"ok": True, "logged_in": True, "cookie_count": len(figma)}
            finally:
                ctx.close()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def _figma_export_frames(url: str, user="default", max_frames: int = 50, force: bool = False) -> dict:
    """乙案核心:登录态浏览器触发 Figma『Export frames to PDF』→ 拆页 → 屏幕帧 base64。

    完全走浏览器(像人一样导出),不碰 REST API,零额度;view-only(只读)权限也能导出。
    结果按 file_key 落盘缓存,预览/多次跑复用。
    """
    import re as _re, base64 as _b64, tempfile, os as _os
    m = _re.search(r"/(?:design|file|proto)/([A-Za-z0-9]+)", url or "")
    file_key = m.group(1) if m else "unknown"
    cpath = _figma_cache_path(file_key, "export")
    if not force:
        c = _figma_read_cache(cpath)
        if c:
            c = dict(c); c["_cached"] = True
            return c
    from playwright.sync_api import sync_playwright
    pdf_path = None
    try:
        with sync_playwright() as pw:
            ctx = _new_context(pw, headless=True, user=user)
            try:
                _apply_session(ctx, user)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                try:
                    page.wait_for_selector("canvas", timeout=30000)
                except Exception:
                    pass
                time.sleep(9)
                body = (page.inner_text("body") or "")[:400].lower()
                if ("log in" in body or "sign up" in body) and not page.query_selector("canvas"):
                    return {"ok": False, "error": "宿主浏览器未登录 Figma — 请重新导入会话"}
                page.mouse.click(800, 500); time.sleep(0.4)
                page.keyboard.press("Escape"); time.sleep(0.4)
                page.keyboard.press("Meta+Slash"); time.sleep(1.3)      # 快捷操作面板
                page.keyboard.type("Export frames to PDF", delay=45); time.sleep(1.5)
                page.keyboard.press("Enter"); time.sleep(2.4)           # 打开导出设置框
                with page.expect_download(timeout=300000) as dl:
                    clicked = False
                    try:
                        page.get_by_role("button", name="Export").last.click(timeout=3500); clicked = True
                    except Exception:
                        pass
                    if not clicked:
                        page.keyboard.press("Enter")                    # 设置框默认按钮=Export
                    page.wait_for_timeout(4000)
                d = dl.value
                fd, pdf_path = tempfile.mkstemp(suffix=".pdf"); _os.close(fd)
                d.save_as(pdf_path)
            finally:
                ctx.close()
        import fitz
        doc = fitz.open(pdf_path)
        frames = []
        for pg in doc:
            r = pg.rect; w, h = r.width, r.height
            ar = (w / h) if h else 0
            if 0.4 <= ar <= 0.62 and h >= 600:        # 竖屏手机帧
                pix = pg.get_pixmap(matrix=fitz.Matrix(2, 2))
                frames.append({"name": f"screen{len(frames) + 1}",
                               "png_b64": _b64.b64encode(pix.tobytes("png")).decode("ascii")})
                if len(frames) >= max_frames:
                    break
        doc.close()
        if not frames:
            return {"ok": False, "error": "导出的 PDF 未拆出屏幕帧(竖屏比例)"}
        result = {"ok": True, "frames": frames, "error": None}
        try:
            _os.makedirs(_FIGMA_CACHE_DIR, exist_ok=True)
            with open(cpath, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(result, ensure_ascii=False))
        except Exception:
            pass
        return result
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        if pdf_path:
            try:
                _os.remove(pdf_path)
            except Exception:
                pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("content-length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True}); return
        if self.path.startswith("/status"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            user = (q.get("user") or ["default"])[0]
            self._send(200, {"ok": True, "logged_in": _marker(user).exists(),
                             "login_running": bool(_login_running.get(_uid(user)))})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        data = self._body()
        user = data.get("user", "default")
        if self.path == "/login":
            with _login_lock:
                if not _login_running.get(_uid(user)):
                    _login_running[_uid(user)] = True
                    threading.Thread(target=_login_worker, args=(user,), daemon=True).start()
            self._send(200, {"ok": True, "started": True, "login_running": True})
        elif self.path == "/logout":
            try:
                shutil.rmtree(_profile_dir(user), ignore_errors=True)
            except Exception:
                pass
            self._send(200, {"ok": True, "logged_in": False})
        elif self.path == "/shot":
            if not data.get("url"):
                self._send(400, {"ok": False, "error": "missing url"}); return
            self._send(200, shot(data["url"], user))
        elif self.path == "/figma-session":
            cks = data.get("cookies")
            if not isinstance(cks, list) or not cks:
                self._send(400, {"ok": False, "error": "missing cookies (应为 cookie JSON 数组)"}); return
            self._send(200, inject_figma_session(cks, user))
        elif self.path == "/export-frames":
            if not data.get("url"):
                self._send(400, {"ok": False, "error": "missing url"}); return
            self._send(200, _figma_export_frames(data["url"], user,
                                                  int(data.get("max_frames", 50)),
                                                  bool(data.get("force", False))))
        elif self.path == "/frames":
            fk = data.get("file_key"); tok = data.get("token")
            if not fk or not tok:
                self._send(400, {"ok": False, "error": "missing file_key/token"}); return
            self._send(200, _figma_api_frames(fk, tok, data.get("prefer_node", ""),
                                               int(data.get("max_frames", 15)),
                                               bool(data.get("force", False))))
        else:
            self._send(404, {"ok": False, "error": "not found"})


def cmd_serve() -> None:
    print(f"[serve] Figma 读图助手(按用户)0.0.0.0:{PORT} | base={PROFILE_BASE}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    cmd_serve()
