"""认证模式配置 — 让用户在 OAuth 订阅 / API Key / 其他 之间二选一。

存储位置：~/Library/Application Support/AITestToolkit/configs/auth.json
结构：
    {
      "mode": "unset" | "oauth" | "api_key",  # 必须用户手动选；unset 跑工具会被拦
      "api_key": "sk-ant-..."                  # 仅 mode=api_key 时使用，存明文（本地）
    }

设计原则：**默认 unset**，用户必须到设置页主动点一下「OAuth」或「API Key」
才算生效。即使本机 ~/.claude/account.json 已存在（其他工具留下的），也不会
被自动复用 — 这是「全部人都需要人工手动操作」原则的实现。

使用：
    from packages.core.auth_config import get_auth_mode, get_api_key
    mode = get_auth_mode()  # 缺省 "unset"，用户没选就报错
    if mode == "unset":
        raise RuntimeError("请先到设置 → 认证模式 选择连接方式")
    if mode == "api_key":
        key = get_api_key()  # 注入子进程 env

LLM 客户端在 packages/core/llm/client.py 里读这两个值决定要不要把
ANTHROPIC_API_KEY 注进 CLI 子进程。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

AuthMode = Literal["unset", "oauth", "api_key"]
DEFAULT_MODE: AuthMode = "unset"


def _config_path() -> Path:
    """统一的配置文件路径。

    优先级:
    1. $AITK_DATA_DIR/configs/auth.json   — Docker / 强制覆盖
    2. macOS: ~/Library/Application Support/AITestToolkit/configs/
    3. Linux: $XDG_DATA_HOME/AITestToolkit/configs/ 或 ~/.local/share/AITestToolkit/configs/
    4. Windows: %APPDATA%/AITestToolkit/configs/
    """
    import sys as _sys
    explicit = os.environ.get("AITK_DATA_DIR")
    if explicit:
        base = Path(explicit).expanduser() / "configs"
    elif _sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "AITestToolkit" / "configs"
    elif _sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = (Path(appdata) / "AITestToolkit" if appdata else Path.home() / "AITestToolkit") / "configs"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        if xdg:
            base = Path(xdg) / "AITestToolkit" / "configs"
        else:
            base = Path.home() / ".local" / "share" / "AITestToolkit" / "configs"
    base.mkdir(parents=True, exist_ok=True)
    return base / "auth.json"


def _read() -> dict[str, Any]:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        # 文件损坏（手动编辑出错等）时退回默认 — 不抛异常打断用户
        return {}


def _write(d: dict[str, Any]) -> None:
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # chmod 0o600：用户独占可读可写（含 API key 时尽量缩小暴露面）
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def get_auth_mode() -> AuthMode:
    """返回当前激活的认证模式。

    用户没在设置页选过 → 返回 'unset'，跑工具时会被 client.py 拦下报清晰错误。
    选过 oauth / api_key → 返回对应值。
    """
    d = _read()
    m = d.get("mode")
    if m in ("oauth", "api_key", "unset"):
        return m  # type: ignore[return-value]
    # 兼容老版本：local 模式视作 oauth（行为最接近）
    if m == "local":
        return "oauth"
    return DEFAULT_MODE


def get_api_key() -> str | None:
    """返回已保存的 API key（明文）。

    仅当 mode == 'api_key' 时被 LLM client 读取注入子进程。
    OAuth 模式下即使有 key 也不使用（client.py 里清空 env 中的两个相关变量）。
    """
    d = _read()
    k = d.get("api_key")
    if isinstance(k, str) and k.strip():
        return k.strip()
    return None


def get_figma_token() -> str | None:
    """返回已保存的 Figma 个人访问令牌(PAT)。用于 UI 比对时拉取设计图。"""
    d = _read()
    t = d.get("figma_token")
    if isinstance(t, str) and t.strip():
        return t.strip()
    # 兜底:环境变量
    env = os.environ.get("FIGMA_TOKEN") or os.environ.get("AITK_FIGMA_TOKEN")
    return env.strip() if env and env.strip() else None


def set_figma_token(token: str | None) -> None:
    """写入/清空 Figma PAT。"""
    d = _read()
    if token and token.strip():
        d["figma_token"] = token.strip()
    else:
        d.pop("figma_token", None)
    _write(d)


def get_figma_login() -> dict[str, str]:
    """返回 Figma 账号密码(浏览器登录态读图用)。{email, password} 或空。"""
    d = _read()
    fl = d.get("figma_login") or {}
    return {"email": fl.get("email", ""), "password": fl.get("password", "")}


def set_figma_login(email: str | None, password: str | None) -> None:
    """写入/清空 Figma 账号密码(本机明文,供持久化浏览器自动登录)。"""
    d = _read()
    if email and email.strip():
        d["figma_login"] = {"email": email.strip(), "password": (password or "").strip()}
    else:
        d.pop("figma_login", None)
    _write(d)


def set_auth_mode(mode: AuthMode, api_key: str | None = None) -> None:
    """更新认证模式 + 可选写入 API key。

    - mode='oauth'    : OAuth 订阅 — 工具跑 `claude login` 写入 ~/.claude/，复用订阅
    - mode='api_key'  : API Key — 用户粘贴 sk-ant-... 跳过登录
    - mode='unset'    : 未配置 → 跑工具会被拦
    """
    if mode not in ("oauth", "api_key", "unset"):
        raise ValueError(f"unsupported auth mode: {mode}")
    d = _read()
    d["mode"] = mode
    if api_key is not None:
        # 显式传空串 = 清空；非空 = 覆盖
        if api_key.strip():
            d["api_key"] = api_key.strip()
        else:
            d.pop("api_key", None)
    _write(d)


def clear_api_key() -> None:
    d = _read()
    d.pop("api_key", None)
    _write(d)


def mark_oauth_logged_in() -> None:
    """记录"用户在本工具内主动完成过 OAuth 登录"。

    设计意图：OAuth 模式不应仅凭 ~/.claude/account.json 存在就显示已登录 —
    那个文件可能是用户在终端 / 其他工具登录的残留。要求用户在本工具内
    主动走过浏览器 OAuth 流程才算"已登录"。
    """
    import time
    d = _read()
    d["oauth_logged_in_at"] = int(time.time())
    _write(d)


def get_oauth_logged_in_at() -> int | None:
    """返回 OAuth 登录的时间戳；未登录返回 None。"""
    d = _read()
    v = d.get("oauth_logged_in_at")
    return int(v) if isinstance(v, (int, float)) else None


def clear_oauth_logged_in() -> None:
    """清除 OAuth 登录标记 — disconnect / mode 切换时调用。"""
    d = _read()
    d.pop("oauth_logged_in_at", None)
    _write(d)


# ── OAuth token 存储（web OAuth flow 拿回的 access_token）───────────────────
# 设计：toolkit 在 web 端自己跑 OAuth 流程，把 access_token / refresh_token
# 存到 auth.json，不再依赖 ~/.claude/account.json。LLM client 在 mode=oauth
# 时把 access_token 作为 ANTHROPIC_AUTH_TOKEN 注给 Claude CLI 子进程。

def set_oauth_tokens(
    access_token: str,
    refresh_token: str | None = None,
    expires_at: int | None = None,
    account: dict[str, Any] | None = None,
) -> None:
    """保存 OAuth 流程换到的 token 三元组 + 账号资料。"""
    import time
    d = _read()
    d["oauth_access_token"] = access_token
    if refresh_token:
        d["oauth_refresh_token"] = refresh_token
    if expires_at:
        d["oauth_expires_at"] = int(expires_at)
    if account:
        d["oauth_account"] = account
    d["oauth_logged_in_at"] = int(time.time())
    _write(d)


def get_oauth_access_token() -> str | None:
    d = _read()
    v = d.get("oauth_access_token")
    return v.strip() if isinstance(v, str) and v.strip() else None


def get_oauth_refresh_token() -> str | None:
    d = _read()
    v = d.get("oauth_refresh_token")
    return v.strip() if isinstance(v, str) and v.strip() else None


def get_oauth_expires_at() -> int | None:
    d = _read()
    v = d.get("oauth_expires_at")
    return int(v) if isinstance(v, (int, float)) else None


def get_oauth_account() -> dict[str, Any]:
    d = _read()
    v = d.get("oauth_account")
    return v if isinstance(v, dict) else {}


def clear_oauth_tokens() -> None:
    """彻底清掉 OAuth — disconnect oauth purge=True 走这里。"""
    d = _read()
    for k in (
        "oauth_access_token", "oauth_refresh_token",
        "oauth_expires_at", "oauth_account", "oauth_logged_in_at",
    ):
        d.pop(k, None)
    _write(d)


def mask_api_key(key: str | None) -> str:
    """用于 UI 回显：只露前 7 后 4，中间打码。

    sk-ant-api03-XXXXXXXXXX...XXXX → sk-ant-…XXXX
    """
    if not key:
        return ""
    s = key.strip()
    if len(s) <= 12:
        return "•" * len(s)
    return f"{s[:7]}…{s[-4:]}"


def is_api_mode_ready() -> bool:
    """API 模式下当前配置是否能跑（mode 选了 api_key 且 key 非空）。"""
    return get_auth_mode() == "api_key" and bool(get_api_key())
