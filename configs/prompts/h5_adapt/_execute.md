# 【已弃用 · v4.0.0 起不再使用】

本文件是 h5_adapt v3.x 时代「容器内桌面 Chromium 模拟视口、Claude 驱动浏览器」执行阶段的协议。

自 **v4.0.0** 起，H5 适配走查改为「**分析宿主机三端真机/模拟器证据**」模型：
- 真机/模拟器证据由宿主机采集器 `scripts/h5_device_collect.py` 在宿主机实采
  （iOS Xcode 模拟器真 WebKit、Android Studio AVD 真 Chrome、本机 Chrome 多视口），
  逐页 × 横竖屏取真 DOM + 真截图，汇成 `evidence.md` 作为 documents 传入；
- 容器内不再驱动任何浏览器（已从 `main.py` 的 `_browser_cfg` / `_TOOL_VIEWPORTS` 移除 h5_adapt）；
- h5_1~h5_5 子步只读分析 `evidence.md`。

故本文件**不再被加载、不再生效**，仅保留作历史说明。新执行模型见 `meta.yaml` 的 `common_system_suffix`
与各子步；采集口径见 `scripts/h5_device_collect.py` 顶部说明。
