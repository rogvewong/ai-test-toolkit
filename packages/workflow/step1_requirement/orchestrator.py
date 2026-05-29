"""Step 1 — Requirement Breakdown Orchestrator.

Runs substeps 1.1 → 1.5 and returns a validated RequirementBreakdownReport.
Implements gate logic per configs/standards/process.yaml (step1_to_step2).
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from packages.core.confidence import (
    ConfidenceScorer,
    score_input_quality,
    score_schema_conformance,
)
from packages.core.config import standards
from packages.core.models import (
    GateAction,
    GateDecision,
    RequirementBreakdownReport,
)
from packages.core.models.common import coerce_float
from packages.core.telemetry import get_logger
from packages.workflow.base import StepOrchestrator

logger = get_logger(__name__)


class Step1Orchestrator(StepOrchestrator):
    step_dir = "step1_requirement"
    step_id = "step1"

    async def execute(self) -> RequirementBreakdownReport:
        self.ctx.bind_logs(self.step_id)
        inputs = self.ctx.inputs

        # ---- 1.1 Material inventory -------------------------------------
        _, inv = await self.run_substep(
            "step1.1",
            {
                "PRD": inputs.get("prd"),
                "原型图": inputs.get("prototype"),
                "UI设计稿": inputs.get("ui_design"),
                "流程图": inputs.get("flow_chart"),
                "接口文档": inputs.get("api_doc"),
                "业务规则": inputs.get("business_rules"),
            },
        )

        # ---- 1.2 Module breakdown ---------------------------------------
        _, modules = await self.run_substep(
            "step1.2",
            {
                "PRD": inputs.get("prd"),
                "原型图": inputs.get("prototype"),
                "UI设计稿": inputs.get("ui_design"),
                "业务规则": inputs.get("business_rules"),
            },
        )

        # ---- 1.3 Flow & interaction -------------------------------------
        _, flows = await self.run_substep(
            "step1.3",
            {
                "功能模块拆解结果": modules,
                "PRD": inputs.get("prd"),
                "接口文档": inputs.get("api_doc"),
                "业务规则": inputs.get("business_rules"),
            },
        )

        # ---- 1.4 Gap detection ------------------------------------------
        _, gaps = await self.run_substep(
            "step1.4",
            {
                "功能模块拆解": modules,
                "流程分析": flows,
                "交互分析": flows,  # 1.3 already fuses interaction
                "PRD": inputs.get("prd"),
                "UI设计稿": inputs.get("ui_design"),
            },
        )

        # ---- 1.5 Final report -------------------------------------------
        resp_final, final_json = await self.run_substep(
            "step1.5",
            {
                "材料盘点结果": inv,
                "功能模块拆解": modules,
                "流程分析": flows,
                "交互分析": flows,
                "遗漏排查结果": gaps,
            },
        )

        # Inject meta (the LLM does not know run_id, fingerprint, etc.)
        final_json["meta"] = self.build_meta(
            resp_final.model_id, inputs
        ).model_dump(mode="json")

        # Re-compute gate_decision locally using structured signals
        final_json["gate_decision"] = self._compute_gate(inv, gaps).model_dump(mode="json")

        # Compute confidence
        final_json["confidence"] = self._compute_confidence(inv, gaps, final_json).model_dump(mode="json")

        # Validate against schema
        try:
            report = RequirementBreakdownReport.model_validate(final_json)
        except ValidationError as exc:
            logger.error("step1.schema_invalid", errors=exc.errors())
            raise

        # Persist final report to project memory
        await self.ctx.memory.project.save(
            kind="report",
            key=f"step1/{self.ctx.run_id}",
            value=report.model_dump(mode="json"),
            tags=["step1", "requirement_breakdown"],
        )
        return report

    # ------------------------------------------------------------------
    # Gate & confidence logic (local, deterministic — not LLM-decided)
    # ------------------------------------------------------------------

    def _compute_gate(
        self, inventory: dict[str, Any], gaps: dict[str, Any]
    ) -> GateDecision:
        gate_rules = standards.process["gate_rules"]["step1_to_step2"]["block_if"]
        completeness = coerce_float(inventory.get("overall_completeness_score"), 0.0)
        prd_present = any(
            m.get("type") == "prd" and m.get("provided", False)
            for m in inventory.get("materials", [])
        )
        blocker_count = int(gaps.get("blocker_count", 0)) or len(
            [a for a in gaps.get("ambiguities", []) if a.get("blocker")]
        )

        reasons: list[str] = []
        blockers: list[str] = []

        if not prd_present:
            blockers.append("PRD 未提供")
        if completeness < 0.8:
            blockers.append(f"材料完整度 {completeness:.2f} < 0.8")
        if blocker_count > 3:
            blockers.append(f"blocker 级遗漏点 {blocker_count} > 3")

        if blockers:
            action = GateAction.REJECT_WITH_REPORT
            reasons = ["闸门 step1_to_step2 阻塞", *blockers]
            next_step = None
        elif blocker_count > 0:
            action = GateAction.WARN_AND_CONTINUE
            reasons = [f"存在 {blocker_count} 条 blocker 需 follow-up"]
            next_step = "step2"
        else:
            action = GateAction.PASS
            reasons = ["所有闸门条件通过"]
            next_step = "step2"

        return GateDecision(
            action=action,
            reasons=reasons,
            metrics={
                "completeness_score": completeness,
                "blocker_count": float(blocker_count),
            },
            blockers=blockers,
            warnings=[],
            next_step=next_step,
        )

    def _compute_confidence(
        self,
        inventory: dict[str, Any],
        gaps: dict[str, Any],
        final_json: dict[str, Any],
    ):
        scorer = ConfidenceScorer()
        completeness = coerce_float(inventory.get("overall_completeness_score"), 0.0)
        blocker_count = int(gaps.get("blocker_count", 0))
        required_top_keys = {"materials", "modules", "flows", "ambiguities", "gate_decision"}
        missing = len(required_top_keys - set(final_json.keys()))
        scorer.add(
            "schema_conformance",
            score_schema_conformance(missing, len(required_top_keys)),
            weight=2.0,
        )
        scorer.add(
            "input_quality",
            score_input_quality(completeness, blocker_count),
            weight=1.5,
        )
        scorer.add(
            "llm_self_report",
            coerce_float(final_json.get("confidence", {}).get("score") if isinstance(final_json.get("confidence"), dict) else None, 0.7),
            weight=1.0,
        )
        return scorer.block(
            rationale=f"completeness={completeness:.2f}, blockers={blocker_count}, schema_missing={missing}"
        )
