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


def _figma_api_frames(file_key: str, token: str, prefer_node: str = "", max_frames: int = 15) -> dict:
    """在宿主机(住宅 IP,不被 Figma WAF 拦)调 Figma REST API 逐帧渲染。

    /v1/files?depth=2 枚举顶层 Frame → 选页 → /v1/images 批量渲染 → 逐帧下载 → base64。
    容器直连 api.figma.com 会被数据中心 IP 403,故由本助手代发。
    """
    import httpx  # 自带 certifi,避免 urllib 在框架 Python 上的证书校验失败

    def _get(url, params=None):
        with httpx.Client(timeout=60.0, follow_redirects=True) as c:
            r = c.get(url, headers={"X-Figma-Token": token}, params=params or {})
            return r.status_code, r.content

    try:
        st, body = _get(f"https://api.figma.com/v1/files/{file_key}", {"depth": "2"})
        if st != 200:
            return {"ok": False, "error": f"Figma files API {st}"}
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
            return {"ok": False, "error": f"Figma images API {st2}"}
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
            return {"ok": False, "error": "未渲染出任何帧"}
        return {"ok": True, "frames": out, "error": None}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


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
        elif self.path == "/frames":
            fk = data.get("file_key"); tok = data.get("token")
            if not fk or not tok:
                self._send(400, {"ok": False, "error": "missing file_key/token"}); return
            self._send(200, _figma_api_frames(fk, tok, data.get("prefer_node", ""),
                                               int(data.get("max_frames", 15))))
        else:
            self._send(404, {"ok": False, "error": "not found"})


def cmd_serve() -> None:
    print(f"[serve] Figma 读图助手(按用户)0.0.0.0:{PORT} | base={PROFILE_BASE}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    cmd_serve()
