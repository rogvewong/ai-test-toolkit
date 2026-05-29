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


async def _first_online_serial() -> str | None:
    """连接所有目标后,返回第一个在线设备的 serial。"""
    for t in adb_targets():
        try:
            await adb_connect(t)
        except AdbError:
            pass
    for d in await list_devices():
        if d.get("state") == "device":
            return d["serial"]
    return None


async def adb_shell(serial: str, *args: str, timeout: float = _ADB_TIMEOUT) -> str:
    rc, out, err = await _run_adb("-s", serial, "shell", *args, timeout=timeout)
    if rc != 0:
        raise AdbError(f"adb shell {' '.join(args)} 失败: {err or out}")
    return out


async def _third_party_packages(serial: str) -> set[str]:
    out = await adb_shell(serial, "pm", "list", "packages", "-3")
    return {ln.split(":", 1)[1].strip() for ln in out.splitlines() if ln.startswith("package:")}


async def install_apk(serial: str, apk_path: str) -> set[str]:
    """装 APK(-r 覆盖 -g 自动授权),返回本次新增的第三方包名集合。"""
    before = await _third_party_packages(serial)
    rc, out, err = await _run_adb("-s", serial, "install", "-r", "-g", apk_path, timeout=180.0)
    text = out or err
    if rc != 0 or "Success" not in text:
        raise AdbError(f"adb install 失败: {text}")
    after = await _third_party_packages(serial)
    return after - before


async def resolve_launch_component(serial: str, package: str) -> str | None:
    """拿 package 的可启动 Activity 组件名(pkg/.Activity)。"""
    try:
        out = await adb_shell(serial, "cmd", "package", "resolve-activity", "--brief", package)
        for ln in out.splitlines():
            ln = ln.strip()
            if "/" in ln and ln.startswith(package):
                return ln
    except AdbError:
        pass
    return None


async def launch_app(serial: str, package: str, component: str | None = None) -> None:
    """启动 APP — 优先用 am start 指定组件,否则 monkey LAUNCHER 兜底。"""
    if component:
        rc, out, err = await _run_adb("-s", serial, "shell", "am", "start", "-n", component, timeout=30.0)
        if rc == 0 and "Error" not in (out + err):
            return
    # 兜底:monkey 触发 LAUNCHER intent
    await _run_adb("-s", serial, "shell", "monkey", "-p", package,
                   "-c", "android.intent.category.LAUNCHER", "1", timeout=30.0)


async def screencap(serial: str, out_path: str) -> None:
    """截当前屏 → PNG 落盘(exec-out 直出二进制,避免 shell 转义损坏)。"""
    rc, raw, err = await _run_adb_bytes("-s", serial, "exec-out", "screencap", "-p", timeout=30.0)
    if rc != 0 or not raw:
        raise AdbError(f"screencap 失败: {err}")
    from pathlib import Path as _P
    _P(out_path).write_bytes(raw)


async def uninstall(serial: str, package: str) -> None:
    await _run_adb("-s", serial, "uninstall", package, timeout=60.0)


async def _run_adb_bytes(*args: str, timeout: float = _ADB_TIMEOUT) -> tuple[int, bytes, str]:
    """跑 adb,stdout 当二进制返回(给 screencap 用)。"""
    if not adb_available():
        raise AdbError("adb 未安装")
    proc = await asyncio.create_subprocess_exec(
        "adb", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise AdbError(f"adb {' '.join(args)} 超时")
    return proc.returncode or 0, out, err.decode("utf-8", "replace").strip()


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


async def run_app_and_capture(
    apk_path: str,
    out_dir: str,
    name_prefix: str,
    package: str | None = None,
    component: str | None = None,
    screens: int = 1,
    settle_seconds: float = 6.0,
) -> dict[str, object]:
    """完整闭环:连模拟器 → 装 APK → 启动 → 截图(若干屏)→ 卸载。

    返回 {ok, serial, package, screenshots:[{filename,...}], steps:[...], error}。
    screenshots 的形状与 _capture_screenshots_for_tool 一致,可直接喂给 ctx.screenshots。
    """
    from pathlib import Path as _P
    steps: list[str] = []
    serial = await _first_online_serial()
    if not serial:
        return {"ok": False, "error": "无在线模拟器设备(请确认宿主模拟器已启动)", "steps": steps}
    steps.append(f"设备: {serial}")
    new_pkgs: set[str] = set()
    try:
        new_pkgs = await install_apk(serial, apk_path)
        steps.append(f"安装成功,新增包: {sorted(new_pkgs) or '(未检出,可能已装过)'}")
    except AdbError as exc:
        return {"ok": False, "serial": serial, "error": f"安装失败: {exc}", "steps": steps}

    pkg = package or (sorted(new_pkgs)[0] if new_pkgs else None)
    if not pkg:
        return {"ok": False, "serial": serial, "error": "无法确定包名(装包未检出新包且未提供 package)", "steps": steps}

    comp = component or await resolve_launch_component(serial, pkg)
    steps.append(f"包名: {pkg} | 启动组件: {comp or '(用 monkey 兜底)'}")
    shots: list[dict[str, object]] = []
    try:
        await launch_app(serial, pkg, comp)
        steps.append("已启动 APP")
        _P(out_dir).mkdir(parents=True, exist_ok=True)
        for i in range(max(1, screens)):
            await asyncio.sleep(settle_seconds if i == 0 else 2.0)
            fname = f"{name_prefix}_app_{i+1}.png"
            await screencap(serial, str(_P(out_dir) / fname))
            shots.append({"url": f"app://{pkg}", "viewport": f"屏{i+1}",
                          "width": "", "height": "", "filename": fname})
            steps.append(f"截图: {fname}")
    finally:
        # 跑完强制卸载,避免模拟器堆积
        try:
            await uninstall(serial, pkg)
            steps.append("已卸载 APP")
        except AdbError:
            steps.append("卸载失败(忽略)")
    return {"ok": True, "serial": serial, "package": pkg, "screenshots": shots, "steps": steps, "error": None}
