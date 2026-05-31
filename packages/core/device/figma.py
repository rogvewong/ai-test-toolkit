"""Figma 设计图抓取 — 通过 Figma REST API 把指定节点导出为 PNG,作为 UI 比对的设计基线。

为什么用 API 而非浏览器截图:
  - 容器无头浏览器访问 figma.com 被 CloudFront 403(反爬);
  - 私有文件需账号权限 → 用用户的 Figma PAT(设置页填),API 直出干净节点 PNG。

链接格式:
  https://www.figma.com/design/<fileKey>/<name>?node-id=<a>-<b>
  → file_key=<fileKey>, node_id="<a>:<b>"(API 用冒号)
"""
from __future__ import annotations

import re

FIGMA_LINK_RE = re.compile(
    r"https?://(?:www\.)?figma\.com/(?:file|design|proto)/([A-Za-z0-9]+)[^\s\]）)]*",
    re.IGNORECASE,
)
_NODE_RE = re.compile(r"node-id=([0-9]+)[-:]([0-9]+)", re.IGNORECASE)


def parse_figma_links(text: str) -> list[dict[str, str]]:
    """从一段文本里解析出所有 Figma 链接 → [{file_key, node_id, url}]。"""
    out: list[dict[str, str]] = []
    if not isinstance(text, str):
        return out
    seen: set[tuple[str, str]] = set()
    for m in FIGMA_LINK_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?)]）」")
        file_key = m.group(1)
        nm = _NODE_RE.search(url)
        node_id = f"{nm.group(1)}:{nm.group(2)}" if nm else ""
        key = (file_key, node_id)
        if key in seen:
            continue
        seen.add(key)
        out.append({"file_key": file_key, "node_id": node_id, "url": url})
    return out


async def fetch_figma_image(
    file_key: str,
    node_id: str,
    token: str,
    out_path: str,
    scale: float = 2.0,
) -> dict[str, object]:
    """调 Figma API 把节点导出为 PNG 并落盘。

    返回 {ok, path, error}。
    流程:GET /v1/images/:key?ids=:node&format=png&scale=N → 拿到 CDN 图 URL → 下载。
    若 node_id 为空,则导出整个文件的画布(取第一个 page 的第一个 frame 较复杂,
    这里要求带 node-id;无 node 时返回错误提示)。
    """
    import httpx
    if not token:
        return {"ok": False, "error": "未配置 Figma token(设置页填 Figma PAT)"}
    if not node_id:
        return {"ok": False, "error": "链接缺少 node-id,无法定位要比对的设计帧"}
    headers = {"X-Figma-Token": token}
    api = f"https://api.figma.com/v1/images/{file_key}"
    params = {"ids": node_id, "format": "png", "scale": str(scale)}
    try:
        async with httpx.AsyncClient(timeout=40.0) as cli:
            r = await cli.get(api, headers=headers, params=params)
            if r.status_code == 403:
                return {"ok": False, "error": "Figma 403:token 无权访问该文件(需该文件的所属/共享账号 token)"}
            if r.status_code != 200:
                return {"ok": False, "error": f"Figma API {r.status_code}: {r.text[:160]}"}
            data = r.json()
            if data.get("err"):
                return {"ok": False, "error": f"Figma: {data['err']}"}
            img_url = (data.get("images") or {}).get(node_id)
            if not img_url:
                return {"ok": False, "error": f"Figma 未返回节点 {node_id} 的图(节点不存在?)"}
            # 下载 CDN 图
            img = await cli.get(img_url)
            if img.status_code != 200 or not img.content:
                return {"ok": False, "error": f"下载设计图失败 HTTP {img.status_code}"}
            from pathlib import Path as _P
            _P(out_path).write_bytes(img.content)
            return {"ok": True, "path": out_path, "error": None,
                    "size": len(img.content)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


_REAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

import os as _os
HOST_RUNNER_URL = _os.environ.get("AITK_FIGMA_RUNNER_URL", "http://host.docker.internal:8077")


async def fetch_figma_via_host_runner(url: str, out_path: str, user: object = "default") -> dict[str, object]:
    """走宿主 Figma 读图助手(真实 Chrome + 按用户持久登录)拿设计图。

    宿主助手见 apps/host_runner/figma_runner.py。容器调它 → 它在宿主用该用户已登录的
    Chrome 打开 Figma 链接截图 → 返回 PNG。返回 {ok, path, error}。
    """
    import base64
    import httpx
    try:
        async with httpx.AsyncClient(timeout=180.0) as cli:
            r = await cli.post(f"{HOST_RUNNER_URL}/shot", json={"url": url, "user": str(user)})
        if r.status_code != 200:
            return {"ok": False, "error": f"宿主助手 HTTP {r.status_code}"}
        data = r.json()
        if not data.get("ok"):
            return {"ok": False, "error": f"宿主助手: {data.get('error')}"}
        from pathlib import Path as _P
        op = _P(out_path)
        # 逐帧存盘(派生文件名);兼容旧版只返回 png_b64 的情况。
        frame_list = data.get("frames")
        if not frame_list and data.get("png_b64"):
            frame_list = [{"name": "frame1", "png_b64": data["png_b64"]}]
        saved: list[dict[str, object]] = []
        for j, fr in enumerate(frame_list or []):
            b64 = fr.get("png_b64")
            if not b64:
                continue
            png = base64.b64decode(b64)
            p = op if j == 0 else op.with_name(f"{op.stem}_f{j + 1}{op.suffix}")
            p.write_bytes(png)
            saved.append({"path": str(p), "name": fr.get("name") or f"frame{j + 1}", "size": len(png)})
        if not saved:
            return {"ok": False, "error": "宿主助手未返回任何帧"}
        return {"ok": True, "path": saved[0]["path"], "frames": saved, "error": None}
    except Exception as exc:
        return {"ok": False, "error": f"连不上宿主助手({HOST_RUNNER_URL}): {type(exc).__name__}"}


async def host_runner_call(method: str, path: str, user: object = "default", **kw) -> dict[str, object]:
    """调宿主助手的 status/login/logout(带 user)。返回 JSON 或 {runner_up:False}。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as cli:
            if method == "GET":
                r = await cli.get(f"{HOST_RUNNER_URL}{path}", params={"user": str(user)})
            else:
                r = await cli.post(f"{HOST_RUNNER_URL}{path}", json={"user": str(user)})
        return {"runner_up": True, **r.json()}
    except Exception:
        return {"runner_up": False, "logged_in": False, "login_running": False}


async def fetch_figma_via_browser(
    url: str,
    out_path: str,
    email: str = "",
    password: str = "",
    profile_dir: str = "/data/figma_profile",
    timeout_ms: int = 45000,
) -> dict[str, object]:
    """走前端(浏览器)阅读 Figma:持久化登录态 → 打开设计链接 → 截图。

    - 持久化 profile_dir(不用无痕)→ 登录一次后长期保留。
    - 私有文件首次会撞登录墙 → 用 email/password 自动登录。
    - 渲染后整页截图作设计基线(画布里就是设计帧)。
    返回 {ok, path, error, logged_in}。
    """
    from pathlib import Path as _P
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        return {"ok": False, "error": "playwright 未安装"}
    _P(profile_dir).mkdir(parents=True, exist_ok=True)

    async def _is_login_wall(pg) -> bool:
        try:
            txt = (await pg.inner_text("body"))[:400].lower()
        except Exception:
            return False
        return any(k in txt for k in ("log in", "sign up", "continue with email", "登录")) \
            and await pg.query_selector("canvas") is None

    try:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                profile_dir, headless=True, user_agent=_REAL_UA,
                viewport={"width": 1600, "height": 1000}, locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await pg.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                import asyncio as _aio
                await _aio.sleep(5)

                logged_in = True
                if await _is_login_wall(pg):
                    logged_in = False
                    if not email:
                        return {"ok": False, "error": "Figma 需要登录但未配置账号(设置页填 Figma 账号密码)",
                                "logged_in": False}
                    # 自动登录
                    await pg.goto("https://www.figma.com/login", timeout=timeout_ms, wait_until="domcontentloaded")
                    await _aio.sleep(2)
                    try:
                        # 有些版本先要点"Continue with email"
                        btn = await pg.query_selector("text=Continue with email")
                        if btn:
                            await btn.click(); await _aio.sleep(1)
                        await pg.fill("input[name=email], input[type=email]", email, timeout=8000)
                        await pg.fill("input[name=password], input[type=password]", password, timeout=8000)
                        # 提交
                        sub = await pg.query_selector("button[type=submit]") or await pg.query_selector("text=Log in")
                        if sub:
                            await sub.click()
                        else:
                            await pg.keyboard.press("Enter")
                        await _aio.sleep(6)
                    except Exception as exc:
                        return {"ok": False, "error": f"自动登录失败(可能 SSO/二次验证): {str(exc)[:120]}",
                                "logged_in": False}
                    # 登录后重开设计页
                    await pg.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    await _aio.sleep(6)
                    if await _is_login_wall(pg):
                        return {"ok": False, "error": "登录后仍是登录墙(账号密码错 / 需邮箱验证码 / SSO)",
                                "logged_in": False}
                    logged_in = True

                # 等画布渲染
                try:
                    await pg.wait_for_selector("canvas", timeout=15000)
                except Exception:
                    pass
                await _aio.sleep(4)
                await pg.screenshot(path=out_path, full_page=False)
                size = _P(out_path).stat().st_size if _P(out_path).exists() else 0
                return {"ok": size > 0, "path": out_path, "size": size,
                        "logged_in": logged_in, "error": None if size > 0 else "截图为空"}
            finally:
                await ctx.close()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
