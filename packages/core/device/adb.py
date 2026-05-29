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
import re
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


# ── UI 自动化:跳过弹窗 + 翻页(uiautomator + input tap)──
_BOUNDS_RE = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
# 可点掉/可前进的按钮文案(年龄确认、协议、保存提示、引导页等);
# 不含 取消/退出/删除/卸载/拒绝/不同意 等危险词。
_DISMISS_KW = [
    "跳过", "同意并继续", "我已年满", "已满18", "已满 18", "年满18", "我已阅读并同意",
    "同意", "我知道了", "知道了", "稍后", "下次再说", "暂不", "继续", "下一步",
    "立即体验", "开始体验", "开始使用", "进入应用", "进入", "确定", "好的", "允许", "我已18",
    "skip", "agree", "continue", "next", "accept", "got it", "enter", "confirm", "allow", "i agree", "ok",
]


async def tap(serial: str, x: int, y: int) -> None:
    await _run_adb("-s", serial, "shell", "input", "tap", str(int(x)), str(int(y)), timeout=15.0)


async def ui_dump(serial: str) -> str:
    """dump 当前界面的 UI 层级 XML(uiautomator)。"""
    try:
        await adb_shell(serial, "uiautomator", "dump", "/sdcard/aitk_ui.xml", timeout=20.0)
        return await adb_shell(serial, "cat", "/sdcard/aitk_ui.xml", timeout=15.0)
    except AdbError:
        return ""


def _parse_clickables(xml: str) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for m in re.finditer(r"<node\b[^>]*>", xml or ""):
        tag = m.group(0)
        if 'clickable="true"' not in tag:
            continue
        tm = re.search(r'text="([^"]*)"', tag)
        dm = re.search(r'content-desc="([^"]*)"', tag)
        text = (tm.group(1) if tm else "") or (dm.group(1) if dm else "")
        bm = _BOUNDS_RE.search(tag)
        if not bm:
            continue
        x1, y1, x2, y2 = map(int, bm.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        out.append({"text": text, "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2, "y2": y2})
    return out


def _is_dismiss(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t or len(t) > 16:
        return False
    return any(k.lower() in t for k in _DISMISS_KW)


async def dismiss_popups(serial: str, max_rounds: int = 6) -> list[str]:
    """循环找"跳过/同意/确认/我知道了/年满18"等按钮点掉,直到没有弹窗按钮。"""
    dismissed: list[str] = []
    for _ in range(max_rounds):
        xml = await ui_dump(serial)
        if not xml:
            break
        cands = [c for c in _parse_clickables(xml) if _is_dismiss(str(c["text"]))]
        if not cands:
            break
        c = cands[0]
        await tap(serial, int(c["cx"]), int(c["cy"]))
        dismissed.append(str(c["text"]))
        await asyncio.sleep(2.0)
    return dismissed


async def _screen_size(serial: str) -> tuple[int, int]:
    try:
        out = await adb_shell(serial, "wm", "size")
        m = re.search(r"(\d+)x(\d+)", out)
        if m:
            return int(m.group(1)), int(m.group(2))
    except AdbError:
        pass
    return 1080, 1920


async def run_app_and_capture(
    apk_path: str,
    out_dir: str,
    name_prefix: str,
    package: str | None = None,
    component: str | None = None,
    screens: int = 1,
    settle_seconds: float = 6.0,
    explore: bool = True,
    max_pages: int = 3,
) -> dict[str, object]:
    """完整闭环:连模拟器 → 装 APK → 启动 → 跳过弹窗 → 截首页 → 翻内部页逐页截 → 卸载。

    返回 {ok, serial, package, screenshots:[{filename,...}], steps:[...], error}。
    screenshots 形状与 _capture_screenshots_for_tool 一致,可直接喂 ctx.screenshots。
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
    _idx = [0]

    async def _cap(label: str) -> None:
        _idx[0] += 1
        fname = f"{name_prefix}_app_{_idx[0]}.png"
        await screencap(serial, str(_P(out_dir) / fname))
        shots.append({"url": f"app://{pkg}", "viewport": label,
                      "width": "", "height": "", "filename": fname})
        steps.append(f"截图: {fname}({label})")

    try:
        await launch_app(serial, pkg, comp)
        steps.append("已启动 APP")
        _P(out_dir).mkdir(parents=True, exist_ok=True)
        await asyncio.sleep(settle_seconds)
        # 跳过启动弹窗(年龄确认 / 协议 / 保存二维码提示 / 引导页等)
        dm = await dismiss_popups(serial)
        if dm:
            steps.append("跳过弹窗: " + ", ".join(dm[:8]))
        await asyncio.sleep(1.5)
        await _cap("首页")
        if explore:
            # 翻内部页:点底部导航栏(屏幕底部 ~18% 内的可点元素)
            W, H = await _screen_size(serial)
            xml = await ui_dump(serial)
            navs = [c for c in _parse_clickables(xml) if int(c["cy"]) > H * 0.82]
            seen: set[int] = set()
            uniq: list[dict[str, object]] = []
            for c in navs:
                bucket = int(c["cx"]) // max(1, W // 8)  # 按横向位置去重
                if bucket in seen:
                    continue
                seen.add(bucket)
                uniq.append(c)
            steps.append(f"底部导航候选: {len(uniq)} 个")
            for c in uniq[:max_pages]:
                try:
                    await tap(serial, int(c["cx"]), int(c["cy"]))
                    await asyncio.sleep(2.0)
                    await dismiss_popups(serial, 2)
                    label = (str(c["text"]).strip() or "内部页")[:8]
                    await _cap(f"页面·{label}")
                except Exception:
                    continue
    finally:
        try:
            await uninstall(serial, pkg)
            steps.append("已卸载 APP")
        except AdbError:
            steps.append("卸载失败(忽略)")
    return {"ok": True, "serial": serial, "package": pkg, "screenshots": shots, "steps": steps, "error": None}
