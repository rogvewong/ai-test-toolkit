"""网络性能测试执行器（弱网/丢包/DNS/CDN/TLS/协议）."""
from __future__ import annotations

from typing import Any

from packages.modules.base import Executor, ExecutorInput, ExecutorOutput


class NetworkPerfExecutor(Executor):
    module_name = "network_perf"

    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        # TODO: tc/netem shaping, curl TTFB, dnsperf probes
        raise NotImplementedError

    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        raise NotImplementedError
