"""数据一致性测试（读写/主从/缓存/事务/幂等/对账）."""
from __future__ import annotations

from typing import Any

from packages.modules.base import Executor, ExecutorInput, ExecutorOutput


class DataConsistencyExecutor(Executor):
    module_name = "data_consistency"

    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        raise NotImplementedError

    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        raise NotImplementedError
