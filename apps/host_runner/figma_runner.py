#!/usr/bin/env python3
"""宿主 Figma 读图助手 — 跑在用户 Mac 上,用真实 Chrome + 持久 profile。

为什么需要它:
  容器里的浏览器是全新 profile + 数据中心 IP,无法继承用户的 Google SSO 登录。
  本助手跑在宿主,用真实 Chrome + 持久 profile(用户一次性 Google 登录),
  之后像用户平时的浏览器一样自动登录,容器通过 HTTP 调它拿 Figma 设计图。

模式:
  python figma_runner.py login   # 一次性:弹出 Chrome 让用户登录 Figma(Google),登录后自动退出
  python figma_runner.py serve    # 常驻:HTTP 服务(默认 0.0.0.0:8077),容器调 /shot 拿设计图

HTTP 接口:
  GET  /health           → {"ok": true, "logged_in": bool}
  POST /shot  {"url": …}  → {"ok": bool, "png_b64": …}  (Figma 节点截图)
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROFILE_DIR = os.environ.get("AITK_FIGMA_PROFILE") or str(Path.home() / ".aitk-figma-profile")
PORT = int(os.environ.get("AITK_FIGMA_RUNNER_PORT", "8077"))
CHROME_CHANNEL = "chrome"  # 用真实 Chrome,降低 Google "浏览器不安全" 拦截
_REAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
_IGNORE_ARGS = ["--enable-automation"]


def _new_context(pw, headless: bool):
    return pw.chromium.launch_persistent_context(
        PROFILE_DIR, channel=CHROME_CHANNEL, headless=headless,
        user_agent=_REAL_UA, viewport={"width": 1600, "height": 1000},
        locale="zh-CN", args=_LAUNCH_ARGS, ignore_default_args=_IGNORE_ARGS,
    )


def _is_logged_in(page) -> bool:
    try:
        page.goto("https://www.figma.com/files/recent", timeout=40000, wait_until="domcontentloaded")
        time.sleep(4)
        txt = (page.inner_text("body") or "")[:300].lower()
        return not any(k in txt for k in ("log in", "sign up", "continue with"))
    except Exception:
        return False


def cmd_login() -> None:
    """弹出真实 Chrome 让用户登录 Figma(Google SSO),登录成功后自动退出。"""
    from playwright.sync_api import sync_playwright
    print(f"[login] profile = {PROFILE_DIR}")
    print("[login] 即将弹出 Chrome 窗口 — 请在窗口里用 Google 登录 Figma。登录成功后本程序自动退出。")
    with sync_playwright() as pw:
        ctx = _new_context(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.figma.com/login", wait_until="domcontentloaded")
        deadline = time.time() + 300  # 5 分钟给用户登录
        ok = False
        while time.time() < deadline:
            time.sleep(5)
            try:
                cur = (page.inner_text("body") or "")[:200].lower()
            except Exception:
                cur = ""
            # 登录后通常跳到 files/home,登录墙文案消失
            if "log in" not in cur and "sign up" not in cur and "continue with" not in cur:
                # 再确认一次
                if _is_logged_in(page):
                    ok = True
                    break
        ctx.close()
    print("[login] 登录成功 ✅,会话已保存到 profile。" if ok else "[login] 超时未检测到登录(可重试)。")
    sys.exit(0 if ok else 2)


def shot(url: str) -> dict:
    """在持久登录态下打开 Figma 链接并截图。返回 {ok, png_b64, error}。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = _new_context(pw, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            time.sleep(6)
            txt = (page.inner_text("body") or "")[:300].lower()
            if any(k in txt for k in ("log in", "sign up", "continue with")) and not page.query_selector("canvas"):
                return {"ok": False, "error": "未登录(请先运行 login 模式登录一次)"}
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


def logged_in_quick() -> bool:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = _new_context(pw, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return _is_logged_in(page)
        finally:
            ctx.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
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
            self._send(200, {"ok": True, "logged_in": logged_in_quick()})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/shot":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("content-length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"})
            return
        url = data.get("url")
        if not url:
            self._send(400, {"ok": False, "error": "missing url"})
            return
        self._send(200, shot(url))


def cmd_serve() -> None:
    print(f"[serve] Figma 读图助手监听 0.0.0.0:{PORT} | profile={PROFILE_DIR}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if mode == "login":
        cmd_login()
    elif mode == "status":
        print("logged_in:", logged_in_quick())
    else:
        cmd_serve()
