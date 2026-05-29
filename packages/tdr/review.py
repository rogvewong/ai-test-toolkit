"""TDR review logic: dimension scoring + decision rules.

Mirrors configs/standards/tdr.yaml:
  * dimensions with weights
  * ≥2 reviewer signatures required
  * blocker comments must be resolved before pass
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.core.config import standards


@dataclass
class TdrReview:
    run_id: str
    artifacts_reviewed: list[str] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    dimension_scores: dict[str, float] = field(default_factory=dict)
    signatures: list[dict[str, Any]] = field(default_factory=list)
    follow_up_items: list[str] = field(default_factory=list)

    def add_comment(
        self,
        *,
        id: str,
        dimension: str,
        severity: str,
        location: str,
        observation: str,
        suggestion: str,
        requires_response: bool = True,
    ) -> None:
        self.comments.append(
            {
                "id": id,
                "dimension": dimension,
                "severity": severity,
                "location": location,
                "observation": observation,
                "suggestion": suggestion,
                "requires_response": requires_response,
                "resolved": False,
                "resolution_note": None,
            }
        )

    def resolve_comment(self, comment_id: str, note: str) -> None:
        for c in self.comments:
            if c["id"] == comment_id:
                c["resolved"] = True
                c["resolution_note"] = note
                return
        raise KeyError(f"comment not found: {comment_id}")


def compute_dimension_scores(reviewer_scores: list[dict[str, dict[str, float]]]) -> dict[str, float]:
    """Average per-dimension scores across reviewers.

    reviewer_scores example::
        [{"alice": {"completeness": 0.9, "correctness": 0.8}},
         {"bob":   {"completeness": 1.0, "correctness": 0.7}}]
    """
    dim_ids = {d["id"] for d in standards.tdr["review_dimensions"]}
    totals: dict[str, list[float]] = {d: [] for d in dim_ids}
    for entry in reviewer_scores:
        for _, scores in entry.items():
            for d, s in scores.items():
                if d in totals:
                    totals[d].append(float(s))
    return {
        d: (sum(v) / len(v)) if v else 0.0 for d, v in totals.items()
    }


def overall_score(dimension_scores: dict[str, float]) -> float:
    weights = {d["id"]: float(d["weight"]) for d in standards.tdr["review_dimensions"]}
    total_w = sum(weights.values()) or 1.0
    return sum(dimension_scores.get(d, 0.0) * w for d, w in weights.items()) / total_w


def decide(
    *,
    dimension_scores: dict[str, float],
    comments: list[dict[str, Any]],
    signatures: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    reasons: list[str] = []

    # Rule 1: ≥2 reviewer signatures
    reviewer_sigs = [s for s in signatures if s.get("signer_role") == "reviewer"]
    if len(reviewer_sigs) < 2:
        reasons.append(f"reviewer 签名数 {len(reviewer_sigs)} < 2")

    # Rule 2: blocker + major unresolved
    blocker_unresolved = [
        c
        for c in comments
        if c.get("severity") in {"blocker", "major"} and not c.get("resolved")
    ]
    if blocker_unresolved:
        reasons.append(f"存在 {len(blocker_unresolved)} 条 blocker/major 未闭环")

    # Rule 3: overall score threshold
    overall = overall_score(dimension_scores)
    if overall < 0.7:
        reasons.append(f"综合分 {overall:.2f} < 0.7")

    if reasons:
        if overall < 0.5 or blocker_unresolved:
            return "reject", reasons
        return "conditional_pass", reasons
    return "pass", ["全部维度达标且无 blocker"]
