"""弱网/断网真实探测层 — Playwright CDP 多网络档位实测。

probe_network() 对目标 URL 在 WiFi/4G/3G/弱网/2G/断网 各档位下真实加载,
采集加载时长/FCP/是否成功/可见内容量/控制台错误,并做「断网→恢复」韧性测试。
确定性事实层;分析(问题清单/结论)交给 LLM。
"""
from .probe import probe_network, NetProbeData, NET_PROFILES

__all__ = ["probe_network", "NetProbeData", "NET_PROFILES"]
