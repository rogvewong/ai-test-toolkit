#!/usr/bin/env python3
"""宿主 Figma 读图助手 — 跑在用户 Mac 上,用真实 Chrome + 持久 profile。

为什么需要它:
  容器里的浏览器是全新 profile + 数据中心 IP,无法继承用户的 Google SSO 登录。
  本助手跑在宿主,用真实 Chrome(channel=chrome)+ 持久 profile。
  用户在 step5 点「登录」→ 弹出真实 Chrome → 用 Google 一键登录(秒通)→ 会话
  持久保存,以后直接读图。容器通过 HTTP 调它。

交互模型(避免 profile 锁冲突,用 marker 文件记录登录态):
  POST /login   → 后台弹出真实 Chrome 让用户登录;登录成功写 marker 并关闭
  GET  /status  → {logged_in, login_running}(读 marker,不抢 profile)
  POST /shot {url} → 持久登录态下打开 Figma 链接截图(登录窗口须先关闭)
  GET  /health  → {ok}
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROFILE_DIR = os.environ.get("AITK_FIGMA_PROFILE") or str(Path.home() / ".aitk-figma-profile")
PORT = int(os.environ.get("AITK_FIGMA_RUNNER_PORT", "8077"))
CHROME_CHANNEL = "chrome"
_REAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
_IGNORE_ARGS = ["--enable-automation"]
_MARKER = Path(PROFILE_DIR) / ".figma_logged_in"

_login_lock = threading.Lock()
_login_running = False


def _new_context(pw, headless: bool):
    return pw.chromium.launch_persistent_context(
        PROFILE_DIR, channel=CHROME_CHANNEL, headless=headless,
        user_agent=_REAL_UA, viewport={"width": 1600, "height": 1000},
        locale="zh-CN", args=_LAUNCH_ARGS, ignore_default_args=_IGNORE_ARGS,
    )


def _page_logged_in(page) -> bool:
    try:
        page.goto("https://www.figma.com/files/recent", timeout=40000, wait_until="domcontentloaded")
        time.sleep(4)
        txt = (page.inner_text("body") or "")[:300].lower()
        return not any(k in txt for k in ("log in", "sign up", "continue with"))
    except Exception:
        return False


def _login_worker():
    """后台:弹出真实 Chrome 让用户登录,成功后写 marker 并关闭。"""
    global _login_running
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            ctx = _new_context(pw, headless=False)  # 头显,用户能操作
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto("https://www.figma.com/login", wait_until="domcontentloaded")
            except Exception:
                pass
            deadline = time.time() + 300
            ok = False
            while time.time() < deadline:
                time.sleep(5)
                try:
                    cur = (page.inner_text("body") or "")[:200].lower()
                except Exception:
                    cur = ""
                if not any(k in cur for k in ("log in", "sign up", "continue with")):
                    if _page_logged_in(page):
                        ok = True
                        break
            if ok:
                _MARKER.parent.mkdir(parents=True, exist_ok=True)
                _MARKER.write_text(str(int(time.time())))
            ctx.close()
    except Exception:
        pass
    finally:
        _login_running = False


def shot(url: str) -> dict:
    from playwright.sync_api import sync_playwright
    if _login_running:
        return {"ok": False, "error": "登录窗口未关闭,请先完成登录"}
    with sync_playwright() as pw:
        ctx = _new_context(pw, headless=True)
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
            png = page.screenshot(full_page=False)
            return {"ok": True, "png_b64": base64.b64encode(png).decode("ascii"), "error": None}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        finally:
            ctx.close()


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

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
        elif self.path == "/status":
            self._send(200, {"ok": True, "logged_in": _MARKER.exists(),
                             "login_running": _login_running})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path == "/login":
            global _login_running
            with _login_lock:
                if not _login_running:
                    _login_running = True
                    threading.Thread(target=_login_worker, daemon=True).start()
            self._send(200, {"ok": True, "started": True, "login_running": _login_running})
        elif self.path == "/logout":
            try:
                _MARKER.unlink()
            except Exception:
                pass
            self._send(200, {"ok": True})
        elif self.path == "/shot":
            try:
                n = int(self.headers.get("content-length") or 0)
                data = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._send(400, {"ok": False, "error": "bad json"}); return
            if not data.get("url"):
                self._send(400, {"ok": False, "error": "missing url"}); return
            self._send(200, shot(data["url"]))
        else:
            self._send(404, {"ok": False, "error": "not found"})


def cmd_serve() -> None:
    print(f"[serve] Figma 读图助手 0.0.0.0:{PORT} | profile={PROFILE_DIR} | logged_in={_MARKER.exists()}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    cmd_serve()
