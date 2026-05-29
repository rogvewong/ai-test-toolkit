"""adb 封装 — 容器内 adb client 驱动宿主机 Android 模拟器。

架构(已实测):
  容器后端 ──adb connect host.docker.internal:<port>──→ 宿主 MuMu / Android Studio AVD

- MuMu adb 默认绑 0.0.0.0:16384,容器可直连。
- Android Studio AVD adb 绑 127.0.0.1:5555,需宿主 adb server 用 -a 暴露后再连(P2 处理)。

环境变量:
  AITK_ADB_TARGETS  逗号分隔的 adb 连接目标,默认 "host.docker.internal:16384"
                    例: "host.docker.internal:16384,host.docker.internal:5555"
"""
from __future__ import annotations

import asyncio
import os
import shutil

DEFAULT_ADB_TARGETS = "host.docker.internal:16384"
_ADB_TIMEOUT = 30.0


class AdbError(RuntimeError):
    """adb 调用失败。"""


def adb_targets() -> list[str]:
    raw = os.environ.get("AITK_ADB_TARGETS") or DEFAULT_ADB_TARGETS
    return [t.strip() for t in raw.split(",") if t.strip()]


def adb_available() -> bool:
    return shutil.which("adb") is not None


async def _run_adb(*args: str, timeout: float = _ADB_TIMEOUT) -> tuple[int, str, str]:
    """跑一条 adb 命令,返回 (returncode, stdout, stderr)。"""
    if not adb_available():
        raise AdbError("adb 未安装(容器镜像需包含 android platform-tools / adb)")
    proc = await asyncio.create_subprocess_exec(
        "adb", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise AdbError(f"adb {' '.join(args)} 超时({timeout}s)")
    return proc.returncode or 0, out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip()


async def adb_connect(target: str) -> dict[str, str]:
    """adb connect <target>。返回 {target, ok, message}。"""
    rc, out, err = await _run_adb("connect", target, timeout=15.0)
    text = out or err
    # adb connect 成功输出含 "connected to" / "already connected"
    ok = "connected to" in text.lower() or "already connected" in text.lower()
    return {"target": target, "ok": ok, "message": text}


async def list_devices() -> list[dict[str, str]]:
    """adb devices -l → 解析出在线设备列表。"""
    rc, out, err = await _run_adb("devices", "-l")
    if rc != 0:
        raise AdbError(f"adb devices 失败: {err or out}")
    devices: list[dict[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info: dict[str, str] = {"serial": serial, "state": state}
        for p in parts[2:]:
            if ":" in p:
                k, v = p.split(":", 1)
                info[k] = v
        devices.append(info)
    return devices


async def adb_status() -> dict[str, object]:
    """连接所有配置目标 + 列设备 — 用于前端"设备自检"。"""
    if not adb_available():
        return {"adb_available": False, "targets": adb_targets(), "connects": [], "devices": [],
                "error": "adb 未安装"}
    connects = []
    for t in adb_targets():
        try:
            connects.append(await adb_connect(t))
        except AdbError as exc:
            connects.append({"target": t, "ok": False, "message": str(exc)})
    try:
        devices = await list_devices()
    except AdbError as exc:
        return {"adb_available": True, "targets": adb_targets(), "connects": connects,
                "devices": [], "error": str(exc)}
    online = [d for d in devices if d.get("state") == "device"]
    return {
        "adb_available": True,
        "targets": adb_targets(),
        "connects": connects,
        "devices": devices,
        "online_count": len(online),
        "error": None,
    }
