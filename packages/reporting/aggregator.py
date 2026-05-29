"""Cross-step report aggregation.

Combines per-step reports (requirement_breakdown / test_case /
api_test / ui_consistency / execution / tdr) into a single bundle
suitable for a rollup dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReportBundle:
    run_id: str
    project_id: str
    tenant_id: str
    step1: dict[str, Any] | None = None
    step2: dict[str, Any] | None = None
    step4: dict[str, Any] | None = None
    step5: dict[str, Any] | None = None
    step6: dict[str, Any] | None = None
    tdr: dict[str, Any] | None = None
    module_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def gates(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for step_id in ("step1", "step2", "step4", "step5", "step6"):
            rpt = getattr(self, step_id)
            if not rpt:
                continue
            gate = rpt.get("gate_decision")
            if gate:
                out.append({"step": step_id, **gate})
        return out

    def confidence(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for step_id in ("step1", "step2", "step4", "step5", "step6"):
            rpt = getattr(self, step_id)
            if not rpt:
                continue
            conf = rpt.get("confidence")
            if conf:
                out.append({"step": step_id, **conf})
        return out

    def defect_totals(self) -> dict[str, int]:
        totals = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for src in (self.step4, self.step6):
            if not src:
                continue
            for d in src.get("defects", []) or []:
                sev = d.get("severity", "low")
                totals[sev] = totals.get(sev, 0) + 1
        return totals


def aggregate(
    *,
    run_id: str,
    project_id: str,
    tenant_id: str,
    step_reports: dict[str, dict[str, Any]] | None = None,
    module_outputs: dict[str, dict[str, Any]] | None = None,
    tdr: dict[str, Any] | None = None,
) -> ReportBundle:
    step_reports = step_reports or {}
    return ReportBundle(
        run_id=run_id,
        project_id=project_id,
        tenant_id=tenant_id,
        step1=step_reports.get("step1"),
        step2=step_reports.get("step2"),
        step4=step_reports.get("step4"),
        step5=step_reports.get("step5"),
        step6=step_reports.get("step6"),
        tdr=tdr,
        module_outputs=module_outputs or {},
    )
