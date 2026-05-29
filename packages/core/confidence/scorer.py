"""Confidence scoring.

Fuses multiple signals into a single 0..1 score and maps it to a grade per
configs/standards/quality.yaml confidence_grading.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from packages.core.models.common import ConfidenceBlock, grade_confidence


@dataclass
class Signal:
    name: str
    value: float  # 0..1
    weight: float = 1.0
    note: str | None = None


@dataclass
class ConfidenceScorer:
    """Weighted average of signals, clipped to [0,1]."""

    signals: list[Signal] = field(default_factory=list)

    def add(
        self, name: str, value: float, weight: float = 1.0, note: str | None = None
    ) -> None:
        self.signals.append(
            Signal(name=name, value=max(0.0, min(1.0, value)), weight=weight, note=note)
        )

    def score(self) -> float:
        if not self.signals:
            return 0.0
        total_w = sum(s.weight for s in self.signals)
        if total_w == 0:
            return 0.0
        raw = sum(s.value * s.weight for s in self.signals) / total_w
        return max(0.0, min(1.0, raw))

    def block(self, rationale: str | None = None) -> ConfidenceBlock:
        s = self.score()
        return ConfidenceBlock(
            score=s,
            grade=grade_confidence(s),
            signals={sig.name: sig.value for sig in self.signals},
            rationale=rationale,
        )


def score_schema_conformance(errors: int, checks: int) -> float:
    """1.0 when 0 errors across N checks; drops linearly with error ratio."""
    if checks <= 0:
        return 0.0
    return max(0.0, 1.0 - errors / checks)


def score_input_quality(completeness: float, ambiguities_blocker: int) -> float:
    """Combine input completeness with blocker ambiguity count."""
    penalty = min(1.0, ambiguities_blocker * 0.15)
    return max(0.0, min(1.0, completeness - penalty))
