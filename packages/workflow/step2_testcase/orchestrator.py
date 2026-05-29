"""Step 2 — Test Case Design Orchestrator.

Runs substeps 2.1 → 2.5 and returns a validated TestCaseReport.
Implements gate logic per configs/standards/process.yaml (step2_to_step4).
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from packages.core.confidence import ConfidenceScorer, score_schema_conformance
from packages.core.config import standards
from packages.core.models import GateAction, GateDecision, TestCaseReport
from packages.core.models.common import coerce_float
from packages.core.telemetry import get_logger
from packages.workflow.base import StepOrchestrator

logger = get_logger(__name__)


class Step2Orchestrator(StepOrchestrator):
    step_dir = "step2_testcase"
    step_id = "step2"

    async def execute(self) -> TestCaseReport:
        self.ctx.bind_logs(self.step_id)
        inputs = self.ctx.inputs
        requirement_report = inputs.get("requirement_report") or {}

        # ---- 2.1 Coverage map -------------------------------------------
        _, coverage_map = await self.run_substep(
            "step2.1",
            {
                "需求拆解报告": requirement_report,
            },
        )

        # ---- 2.2 P0 test cases ------------------------------------------
        _, p0_cases_raw = await self.run_substep(
            "step2.2",
            {
                "需求覆盖地图": coverage_map,
                "需求拆解报告": requirement_report,
            },
        )

        # ---- 2.3 P1/P2 test cases ---------------------------------------
        _, p1p2_raw = await self.run_substep(
            "step2.3",
            {
                "需求覆盖地图": coverage_map,
                "P0测试用例": p0_cases_raw,
                "需求拆解报告": requirement_report,
            },
        )

        # ---- 2.4 Automation & risk annotation ---------------------------
        _, automation_risk = await self.run_substep(
            "step2.4",
            {
                "P0测试用例": p0_cases_raw,
                "P1P2测试用例": p1p2_raw,
            },
        )

        # ---- 2.5 Final report -------------------------------------------
        resp_final, final_json = await self.run_substep(
            "step2.5",
            {
                "需求覆盖地图": coverage_map,
                "P0测试用例": p0_cases_raw,
                "P1P2测试用例": p1p2_raw,
                "自动化与风险分析": automation_risk,
            },
        )

        # Normalize LLM shape → schema shape
        final_json = self._normalize(final_json, coverage_map, p0_cases_raw, p1p2_raw)
        final_json["meta"] = self.build_meta(resp_final.model_id, inputs).model_dump(
            mode="json"
        )
        final_json["gate_decision"] = self._compute_gate(final_json).model_dump(
            mode="json"
        )
        final_json["confidence"] = self._compute_confidence(final_json).model_dump(
            mode="json"
        )

        try:
            report = TestCaseReport.model_validate(final_json)
        except ValidationError as exc:
            logger.error("step2.schema_invalid", errors=exc.errors()[:10])
            raise

        await self.ctx.memory.project.save(
            kind="report",
            key=f"step2/{self.ctx.run_id}",
            value=report.model_dump(mode="json"),
            tags=["step2", "test_cases"],
        )
        return report

    # ------------------------------------------------------------------
    # Normalization & derived fields
    # ------------------------------------------------------------------

    def _normalize(
        self,
        final_json: dict[str, Any],
        coverage_map: dict[str, Any],
        p0_raw: dict[str, Any],
        p1p2_raw: dict[str, Any],
    ) -> dict[str, Any]:
        """Coerce LLM outputs to TestCaseReport schema."""
        # coverage_map fallback
        if "coverage_map" not in final_json or not final_json["coverage_map"]:
            final_json["coverage_map"] = self._coerce_coverage(coverage_map)

        # cases
        final_json["p0_cases"] = [
            self._coerce_case(c, "P0")
            for c in final_json.get("p0_cases") or p0_raw.get("cases", [])
        ]
        final_json["p1_cases"] = [
            self._coerce_case(c, "P1")
            for c in final_json.get("p1_cases") or p1p2_raw.get("p1_cases", [])
        ]
        final_json["p2_cases"] = [
            self._coerce_case(c, "P2")
            for c in final_json.get("p2_cases") or p1p2_raw.get("p2_cases", [])
        ]

        # automation summary derived from cases
        summary = {"auto": 0, "semi_auto": 0, "manual": 0}
        for bucket in ("p0_cases", "p1_cases", "p2_cases"):
            for c in final_json[bucket]:
                tag = c.get("automation_tag", "manual")
                if tag in summary:
                    summary[tag] += 1
        final_json["automation_summary"] = summary
        return final_json

    @staticmethod
    def _coerce_coverage(coverage_map: dict[str, Any]) -> list[dict[str, Any]]:
        entries = []
        for item in coverage_map.get("coverage_entries", []) or coverage_map.get(
            "entries", []
        ):
            entries.append(
                {
                    "requirement_id": item.get("requirement_id")
                    or item.get("req_id", "UNKNOWN"),
                    "flow_id": item.get("flow_id"),
                    "case_ids": item.get("case_ids", []),
                    "coverage_type": item.get("coverage_type", "positive"),
                }
            )
        return entries

    @staticmethod
    def _coerce_case(raw: dict[str, Any], default_priority: str) -> dict[str, Any]:
        steps_src = raw.get("steps", [])
        steps = []
        for idx, s in enumerate(steps_src, 1):
            if isinstance(s, str):
                steps.append({"order": idx, "action": s, "data": None, "expected": ""})
            else:
                steps.append(
                    {
                        "order": s.get("order", idx),
                        "action": s.get("action", ""),
                        "data": s.get("data"),
                        "expected": s.get("expected", ""),
                    }
                )
        if not steps:
            steps = [{"order": 1, "action": raw.get("title", ""), "data": None, "expected": raw.get("expected_result", "")}]

        return {
            "id": raw.get("id") or raw.get("case_id", "TC-UNK-0000"),
            "title": raw.get("title", ""),
            "priority": raw.get("priority", default_priority),
            "module_id": raw.get("module_id", "MOD-UNK-000"),
            "flow_id": raw.get("flow_id"),
            "preconditions": raw.get("preconditions", []),
            "steps": steps,
            "expected_result": raw.get("expected_result") or (steps[-1]["expected"] if steps else ""),
            "automation_tag": raw.get("automation_tag", "manual"),
            "tools": raw.get("tools", []),
            "requirements_covered": raw.get("requirements_covered", []),
            "tags": raw.get("tags", []),
        }

    # ------------------------------------------------------------------
    # Gate & confidence
    # ------------------------------------------------------------------

    def _compute_gate(self, final_json: dict[str, Any]) -> GateDecision:
        rules = standards.process["gate_rules"].get("step2_to_step4", {}).get(
            "block_if", []
        )
        p0_cases = final_json.get("p0_cases", [])
        all_cases = p0_cases + final_json.get("p1_cases", []) + final_json.get(
            "p2_cases", []
        )

        # P0 coverage ratio: fraction of requirements with at least one P0 case
        covered_reqs: set[str] = set()
        for c in p0_cases:
            covered_reqs.update(c.get("requirements_covered", []))
        required_reqs: set[str] = set()
        for e in final_json.get("coverage_map", []):
            required_reqs.add(e["requirement_id"])
        p0_coverage = (
            len(covered_reqs) / len(required_reqs) if required_reqs else 1.0
        )

        unlabeled_auto = sum(
            1 for c in all_cases if c.get("automation_tag") not in {"auto", "semi_auto", "manual"}
        )

        blockers: list[str] = []
        warnings: list[str] = []

        if p0_coverage < 1.0:
            blockers.append(f"P0 覆盖率 {p0_coverage:.2f} < 1.0")
        if unlabeled_auto > 0:
            blockers.append(f"未标注自动化的用例 {unlabeled_auto}")

        if blockers:
            action = GateAction.REJECT_WITH_REPORT
            reasons = ["闸门 step2_to_step4 阻塞", *blockers]
            next_step = None
        else:
            action = GateAction.PASS
            reasons = ["测试用例覆盖与自动化标签均符合闸门条件"]
            next_step = "step4"

        return GateDecision(
            action=action,
            reasons=reasons,
            metrics={
                "p0_coverage": p0_coverage,
                "unlabeled_automation_cases": float(unlabeled_auto),
                "total_cases": float(len(all_cases)),
            },
            blockers=blockers,
            warnings=warnings,
            next_step=next_step,
        )

    def _compute_confidence(self, final_json: dict[str, Any]):
        scorer = ConfidenceScorer()
        required = {
            "coverage_map",
            "p0_cases",
            "p1_cases",
            "p2_cases",
            "automation_summary",
            "gate_decision",
        }
        missing = len(required - set(final_json.keys()))
        scorer.add(
            "schema_conformance",
            score_schema_conformance(missing, len(required)),
            weight=2.0,
        )
        total_cases = sum(
            len(final_json.get(b, [])) for b in ("p0_cases", "p1_cases", "p2_cases")
        )
        scorer.add(
            "case_volume",
            min(1.0, total_cases / 30.0),  # 30+ cases → max volume signal
            weight=1.0,
        )
        scorer.add(
            "llm_self_report",
            coerce_float(final_json.get("confidence", {}).get("score"), 0.7)
            if isinstance(final_json.get("confidence"), dict)
            else 0.7,
            weight=1.0,
        )
        return scorer.block(rationale=f"total_cases={total_cases}, schema_missing={missing}")
