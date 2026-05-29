"""兼容性测试（去多机型，保留 OS / WebView / 字体 / 深色 / i18n / 厂商通道）."""
from __future__ import annotations

from typing import Any

from packages.modules.base import Executor, ExecutorInput, ExecutorOutput


class CompatibilityExecutor(Executor):
    module_name = "compatibility"

    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        raise NotImplementedError

    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        raise NotImplementedError
