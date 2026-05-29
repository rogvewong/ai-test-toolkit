"""客户端性能（FPS/启动/内存/CPU/电量/包体）— PerfDog-like."""
from __future__ import annotations

from typing import Any

from packages.modules.base import Executor, ExecutorInput, ExecutorOutput


class ClientPerfExecutor(Executor):
    module_name = "client_perf"

    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        # TODO: adb/perfetto/iOS Instruments invocation
        raise NotImplementedError

    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        raise NotImplementedError
