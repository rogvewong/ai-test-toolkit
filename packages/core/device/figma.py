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
