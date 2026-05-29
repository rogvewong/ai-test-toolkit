"""Step 5 — UI Consistency Orchestrator."""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from packages.core.confidence import ConfidenceScorer, score_schema_conformance
from packages.core.models import GateAction, GateDecision, UiConsistencyReport
from packages.core.models.common import coerce_float
from packages.core.telemetry import get_logger
from packages.workflow.base import StepOrchestrator

logger = get_logger(__name__)


class Step5Orchestrator(StepOrchestrator):
    step_dir = "step5_ui"
    step_id = "step5"

    async def execute(self) -> UiConsistencyReport:
        self.ctx.bind_logs(self.step_id)
        inputs = self.ctx.inputs

        _, baseline = await self.run_substep(
            "step5.1",
            {
                "需求拆解报告": inputs.get("requirement_report"),
                "测试用例集": inputs.get("test_case_report"),
                "设计稿或截图清单": inputs.get("design_assets") or [],
            },
        )

        _, structure = await self.run_substep(
            "step5.2",
            {
                "比对基准": baseline,
                "实际截图或DOM快照": inputs.get("actual_snapshots") or [],
            },
        )

        _, interaction = await self.run_substep(
            "step5.3",
            {
                "比对基准": baseline,
                "状态录屏或快照": inputs.get("state_captures") or [],
                "结构差异": structure,
            },
        )

        _, dual_end = await self.run_substep(
            "step5.4",
            {
                "比对基准": baseline,
                "结构差异": structure,
                "交互差异": interaction,
            },
        )

        resp_final, final_json = await self.run_substep(
            "step5.5",
            {
                "比对基准": baseline,
                "结构差异": structure,
                "交互差异": interaction,
                "双端差异": dual_end,
            },
        )

        final_json = self._normalize(final_json, baseline, structure, interaction, dual_end)
        final_json["meta"] = self.build_meta(resp_final.model_id, inputs).model_dump(mode="json")
        final_json["gate_decision"] = self._compute_gate(final_json).model_dump(mode="json")
        final_json["confidence"] = self._compute_confidence(final_json).model_dump(mode="json")

        try:
            report = UiConsistencyReport.model_validate(final_json)
        except ValidationError as exc:
            logger.error("step5.schema_invalid", errors=exc.errors()[:10])
            raise

        await self.ctx.memory.project.save(
            kind="report",
            key=f"step5/{self.ctx.run_id}",
            value=report.model_dump(mode="json"),
            tags=["step5", "ui_consistency"],
        )
        return report

    # ------------------------------------------------------------------

    def _normalize(
        self,
        final_json: dict[str, Any],
        baseline: dict[str, Any],
        structure: dict[str, Any],
        interaction: dict[str, Any],
        dual_end: dict[str, Any],
    ) -> dict[str, Any]:
        refs_src = final_json.get("baselines") or baseline.get("baselines", [])
        final_json["references"] = [
            {
                "id": b.get("page_id") or "PAGE-UNK-0000",
                "source": b.get("baseline_source") or "design_doc",
                "screen_name": b.get("page_name") or b.get("page_id") or "",
                "version": b.get("baseline_ref") or "unknown",
                "url_or_path": b.get("baseline_ref") or "",
            }
            for b in refs_src
        ]

        diffs: list[dict[str, Any]] = []

        # from structure
        for page in structure.get("page_diffs", []):
            for d in page.get("diffs", []):
                diffs.append(
                    {
                        "id": d.get("diff_id", f"UID-UNK-{len(diffs):04d}"),
                        "reference_id": page.get("page_id", "PAGE-UNK-0000"),
                        "capture_path": d.get("actual_ref", ""),
                        "kind": d.get("category", "layout"),
                        "delta_pct": coerce_float(page.get("total_diff_pct"), 0.0),
                        "severity": self._severity(d.get("severity", "minor")),
                        "evidence_paths": [d.get("actual_ref")]
                        if d.get("actual_ref")
                        else [],
                        "suggested_fix": d.get("note"),
                    }
                )

        # from interaction
        for d in interaction.get("interaction_diffs", []):
            diffs.append(
                {
                    "id": d.get("diff_id", f"UII-UNK-{len(diffs):04d}"),
                    "reference_id": d.get("page_id", "PAGE-UNK-0000"),
                    "capture_path": d.get("evidence_ref", ""),
                    "kind": "interaction",
                    "delta_pct": 0.0,
                    "severity": self._severity(d.get("severity", "minor")),
                    "evidence_paths": [d.get("evidence_ref")]
                    if d.get("evidence_ref")
                    else [],
                    "suggested_fix": d.get("suggested_fix"),
                }
            )

        # from dual_end
        for d in dual_end.get("cross_platform_diffs", []):
            diffs.append(
                {
                    "id": d.get("diff_id", f"UCD-UNK-{len(diffs):04d}"),
                    "reference_id": d.get("page_id", "PAGE-UNK-0000"),
                    "capture_path": (d.get("evidence_refs") or [""])[0],
                    "kind": "layout",
                    "delta_pct": 0.0,
                    "severity": self._severity(d.get("severity", "minor")),
                    "evidence_paths": d.get("evidence_refs", []),
                    "suggested_fix": d.get("suggested_fix"),
                }
            )

        # if final_json provides its own diffs, prefer them
        final_json["diffs"] = final_json.get("diffs") or diffs

        severity_map: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for d in final_json["diffs"]:
            sev = d.get("severity", "low")
            if sev in severity_map:
                severity_map[sev] += 1
        final_json["severity_map"] = severity_map

        per_page = final_json.pop("per_page_stats", None) or []
        max_page_diff = max(
            (coerce_float(p.get("total_diff_pct"), 0.0) for p in per_page), default=0.0
        )
        final_json["overall_deviation_ratio"] = coerce_float(
            final_json.get("overall_deviation_ratio"), max_page_diff
        )
        # Drop intermediate LLM keys not in UiConsistencyReport
        for stray in (
            "baselines",
            "page_diffs",
            "interaction_diffs",
            "cross_platform_diffs",
            "per_platform_stats",
            "regression_risk",
            "fix_priority",
            "severity_summary",
        ):
            final_json.pop(stray, None)
        return final_json

    @staticmethod
    def _severity(raw: str) -> str:
        mapping = {"cosmetic": "low", "minor": "low", "major": "high", "critical": "critical"}
        return mapping.get(raw, raw if raw in {"critical", "high", "medium", "low"} else "low")

    # ------------------------------------------------------------------

    def _compute_gate(self, final_json: dict[str, Any]) -> GateDecision:
        deviation = float(final_json.get("overall_deviation_ratio", 0.0))
        sev = final_json.get("severity_map", {})
        critical = int(sev.get("critical", 0))

        blockers: list[str] = []
        warnings: list[str] = []

        if deviation > 0.2:
            blockers.append(f"UI 偏差率 {deviation:.2f} > 0.2")
        if critical > 0:
            blockers.append(f"critical UI 差异 {critical}")

        if blockers:
            action = GateAction.REJECT_WITH_REPORT
            reasons = ["闸门 step5_to_step6 阻塞", *blockers]
            next_step = None
        elif deviation > 0.1:
            action = GateAction.WARN_AND_CONTINUE
            reasons = [f"UI 偏差率 {deviation:.2f} 进入 warn 阈值"]
            next_step = "step6"
            warnings = reasons
        else:
            action = GateAction.PASS
            reasons = ["UI 偏差率在安全阈值内"]
            next_step = "step6"

        return GateDecision(
            action=action,
            reasons=reasons,
            metrics={
                "overall_deviation_ratio": deviation,
                "critical_diffs": float(critical),
            },
            blockers=blockers,
            warnings=warnings,
            next_step=next_step,
        )

    def _compute_confidence(self, final_json: dict[str, Any]):
        scorer = ConfidenceScorer()
        required = {
            "references",
            "diffs",
            "overall_deviation_ratio",
            "severity_map",
            "gate_decision",
        }
        missing = len(required - set(final_json.keys()))
        scorer.add(
            "schema_conformance",
            score_schema_conformance(missing, len(required)),
            weight=2.0,
        )
        scorer.add(
            "baseline_ready",
            1.0 if final_json.get("references") else 0.3,
            weight=1.0,
        )
        scorer.add(
            "llm_self_report",
            coerce_float(final_json.get("confidence", {}).get("score"), 0.7)
            if isinstance(final_json.get("confidence"), dict)
            else 0.7,
            weight=1.0,
        )
        return scorer.block(
            rationale=f"diffs={len(final_json.get('diffs', []))}, schema_missing={missing}"
        )
