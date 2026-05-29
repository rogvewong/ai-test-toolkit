"""稳定性测试（Monkey/Fuzz/长时/OOM/泄漏）."""
from __future__ import annotations

from typing import Any

from packages.modules.base import Executor, ExecutorInput, ExecutorOutput


class StabilityExecutor(Executor):
    module_name = "stability"

    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        raise NotImplementedError

    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        raise NotImplementedError
