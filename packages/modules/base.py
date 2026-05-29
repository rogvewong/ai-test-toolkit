"""Unified Executor abstraction for the 10 specialized test modules.

All modules follow the three-layer pattern:
  collection → processing → ai_analysis
and emit evidence + metrics + AI-generated summary in a uniform envelope.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from packages.core.models.common import ConfidenceBlock, Severity


@dataclass
class ExecutorInput:
    run_id: str
    project_id: str
    tenant_id: str
    targets: list[str]  # URLs / test IDs / device IDs depending on module
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Evidence:
    kind: str  # log | screenshot | har | trace | dump | metric
    path: str
    description: str | None = None


@dataclass
class Finding:
    id: str
    title: str
    severity: Severity
    evidence: list[Evidence] = field(default_factory=list)
    suggested_action: str | None = None


@dataclass
class ExecutorOutput:
    module: str
    metrics: dict[str, float] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    ai_summary: str | None = None
    confidence: ConfidenceBlock | None = None


class Executor(ABC):
    """Each specialized module implements one Executor."""

    module_name: str

    @abstractmethod
    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        """Phase 1 — raw data acquisition (hit APIs, run k6, capture frames)."""

    @abstractmethod
    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Phase 2 — deterministic metrics + structured events extraction."""

    @abstractmethod
    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        """Phase 3 — LLM-assisted analysis + findings."""

    async def run(self, inp: ExecutorInput) -> ExecutorOutput:
        raw = await self.collect(inp)
        processed = await self.process(raw)
        return await self.analyze(processed, inp)
