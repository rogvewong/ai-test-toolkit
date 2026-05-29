"""Step 4 — API Testing Orchestrator.

Runs substeps 4.1 → 4.5, optionally plugging in the api_testing module for
actual request execution before the final report synthesis.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from packages.core.confidence import ConfidenceScorer, score_schema_conformance
from packages.core.config import standards
from packages.core.models import ApiTestReport, GateAction, GateDecision
from packages.core.models.common import ExecutionState, Severity, coerce_float
from packages.core.telemetry import get_logger
from packages.workflow.base import StepOrchestrator

logger = get_logger(__name__)


class Step4Orchestrator(StepOrchestrator):
    step_dir = "step4_api"
    step_id = "step4"

    async def execute(self) -> ApiTestReport:
        self.ctx.bind_logs(self.step_id)
        inputs = self.ctx.inputs

        # ---- 4.1 Interface inventory ------------------------------------
        _, endpoints = await self.run_substep(
            "step4.1",
            {
                "需求拆解报告": inputs.get("requirement_report"),
                "测试用例集": inputs.get("test_case_report"),
                "OpenAPI或接口文档": inputs.get("api_doc"),
            },
        )

        # ---- 4.2 Functional cases ---------------------------------------
        _, functional = await self.run_substep(
            "step4.2",
            {
                "接口清单": endpoints,
                "测试用例集": inputs.get("test_case_report"),
            },
        )

        # ---- 4.3 Security cases -----------------------------------------
        _, security = await self.run_substep(
            "step4.3",
            {
                "接口清单": endpoints,
                "鉴权接口": {
                    "auth_endpoints": endpoints.get("auth_endpoints", [])
                },
            },
        )

        # ---- 4.4 Boundary cases -----------------------------------------
        _, boundary = await self.run_substep(
            "step4.4",
            {
                "接口清单": endpoints,
                "功能接口用例": functional,
            },
        )

        # ---- Optional: real execution via api_testing module ------------
        execution = inputs.get("execution_results")  # provided externally if run

        # ---- 4.5 Final report -------------------------------------------
        resp_final, final_json = await self.run_substep(
            "step4.5",
            {
                "接口清单": endpoints,
                "功能接口用例": functional,
                "安全与权限用例": security,
                "边界与异常用例": boundary,
                "执行结果": execution or {},
            },
        )

        final_json = self._normalize(
            final_json, endpoints, functional, security, boundary
        )
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
            report = ApiTestReport.model_validate(final_json)
        except ValidationError as exc:
            logger.error("step4.schema_invalid", errors=exc.errors()[:10])
            raise

        await self.ctx.memory.project.save(
            kind="report",
            key=f"step4/{self.ctx.run_id}",
            value=report.model_dump(mode="json"),
            tags=["step4", "api_test"],
        )
        return report

    # ------------------------------------------------------------------

    def _normalize(
        self,
        final_json: dict[str, Any],
        endpoints: dict[str, Any],
        functional: dict[str, Any],
        security: dict[str, Any],
        boundary: dict[str, Any],
    ) -> dict[str, Any]:
        ep_src = final_json.get("endpoints") or endpoints.get("endpoints", [])
        final_json["api_list"] = [
            {
                "id": e.get("endpoint_id") or e.get("id") or "EP-UNK-0000",
                "method": e.get("method", "GET"),
                "path": e.get("path", "/"),
                "description": e.get("purpose") or e.get("description"),
                "auth_required": bool(e.get("requires_auth", True)),
                "schema_ref": e.get("schema_ref"),
            }
            for e in ep_src
        ]

        def coerce_result(
            r: dict[str, Any], category: str, default_state: ExecutionState
        ) -> dict[str, Any]:
            state_raw = r.get("state") or (
                "passed" if r.get("passed", False) else "failed"
            )
            return {
                "case_id": r.get("case_id", "AC-UNK-0000"),
                "api_id": r.get("endpoint_id") or r.get("api_id", "EP-UNK-0000"),
                "category": category,
                "request_snapshot": r.get("request_snapshot") or r.get("request") or {},
                "response_snapshot": r.get("response_snapshot")
                or r.get("response")
                or {},
                "assertions": [
                    {
                        "kind": a.get("type", "body_json"),
                        "expression": a.get("path")
                        or a.get("expression")
                        or a.get("op", ""),
                        "expected": a.get("expected"),
                    }
                    for a in r.get("assertions", [])
                ],
                "state": state_raw
                if state_raw
                in {"queued", "running", "passed", "failed", "flaky", "skipped", "error", "aborted"}
                else default_state.value,
                "latency_ms": coerce_float(r.get("response_time_ms") or r.get("latency_ms"), 0.0),
                "evidence_paths": [r["evidence_ref"]]
                if r.get("evidence_ref")
                else r.get("evidence_paths", []),
                "defect_ids": r.get("defect_ids", []),
            }

        functional_list = final_json.get("results") or functional.get("api_cases", [])
        final_json["functional_results"] = [
            coerce_result(r, "functional", ExecutionState.PASSED)
            for r in functional_list
            if (r.get("category") or "functional") == "functional"
        ] or [coerce_result(r, "functional", ExecutionState.QUEUED) for r in functional_list]

        sec_list = final_json.get("security_results") or security.get(
            "security_cases", []
        )
        final_json["security_results"] = [
            coerce_result(r, "security", ExecutionState.QUEUED) for r in sec_list
        ]

        bnd_list = final_json.get("boundary_results") or boundary.get(
            "boundary_cases", []
        )
        final_json["boundary_results"] = [
            coerce_result(r, "boundary", ExecutionState.QUEUED) for r in bnd_list
        ]

        # Defects
        defects_src = final_json.get("defects") or []
        for s in final_json.get("security_findings") or []:
            defects_src.append(
                {
                    "id": s.get("id", f"DEF-API-{len(defects_src):04d}"),
                    "title": s.get("description", "security finding"),
                    "severity": s.get("severity", "high"),
                    "reproduction_steps": s.get("steps", []),
                    "actual_result": s.get("description", ""),
                    "expected_result": s.get("expected", "系统应拒绝请求"),
                    "related_api_ids": [s["endpoint_id"]]
                    if s.get("endpoint_id")
                    else [],
                }
            )
        final_json["defects"] = [
            {
                "id": d.get("id") or d.get("defect_id", "DEF-API-0000"),
                "title": d.get("title", ""),
                "severity": d.get("severity", "medium"),
                "state": d.get("state", "new"),
                "reproduction_steps": d.get("reproduction_steps", []),
                "actual_result": d.get("actual_result", d.get("actual", "")),
                "expected_result": d.get("expected_result", d.get("expected", "")),
                "evidence_paths": d.get("evidence_paths", d.get("evidence_refs", [])),
                "related_case_ids": d.get("related_case_ids", []),
                "related_api_ids": d.get("related_api_ids", []),
            }
            for d in defects_src
        ]

        cov = final_json.pop("coverage_summary", None) or {}
        metrics = {
            "pass_rate": coerce_float(cov.get("p0_pass_rate"), 1.0),
            "endpoint_coverage": coerce_float(cov.get("endpoint_coverage"), 1.0),
            "security_coverage": coerce_float(cov.get("security_coverage"), 0.0),
            "avg_latency": coerce_float(cov.get("avg_latency_ms"), 0.0),
            "p95_latency": coerce_float(cov.get("p95_latency_ms"), 0.0),
        }
        final_json["metrics"] = metrics
        # Drop intermediate LLM keys that aren't part of ApiTestReport schema
        for stray in (
            "endpoints",
            "results",
            "security_findings",
            "boundary_gaps",
            "sla_violations",
        ):
            final_json.pop(stray, None)
        return final_json

    # ------------------------------------------------------------------

    def _compute_gate(self, final_json: dict[str, Any]) -> GateDecision:
        defects = final_json.get("defects", [])
        critical = sum(1 for d in defects if d.get("severity") == Severity.CRITICAL.value)
        high = sum(1 for d in defects if d.get("severity") == Severity.HIGH.value)

        p0_fail = sum(
            1
            for r in final_json.get("functional_results", [])
            if r.get("state") == "failed"
        )
        pass_rate = coerce_float(final_json.get("metrics", {}).get("pass_rate"), 1.0)

        blockers: list[str] = []
        warnings: list[str] = []

        if critical > 0:
            blockers.append(f"critical 缺陷 {critical}")
        if high >= 3:
            blockers.append(f"high 缺陷 {high} ≥ 3")
        if pass_rate < 0.9:
            blockers.append(f"接口通过率 {pass_rate:.2f} < 0.9")
        if p0_fail > 0:
            blockers.append(f"P0 接口失败 {p0_fail}")

        if blockers:
            action = GateAction.REJECT_WITH_REPORT
            reasons = ["闸门 step4_to_step6 阻塞", *blockers]
            next_step = None
        else:
            action = GateAction.PASS
            reasons = ["接口测试通过闸门检查"]
            next_step = "step6"

        return GateDecision(
            action=action,
            reasons=reasons,
            metrics={
                "critical_defects": float(critical),
                "high_defects": float(high),
                "pass_rate": pass_rate,
                "p0_fail": float(p0_fail),
            },
            blockers=blockers,
            warnings=warnings,
            next_step=next_step,
        )

    def _compute_confidence(self, final_json: dict[str, Any]):
        scorer = ConfidenceScorer()
        required = {
            "api_list",
            "functional_results",
            "security_results",
            "boundary_results",
            "defects",
            "metrics",
        }
        missing = len(required - set(final_json.keys()))
        scorer.add(
            "schema_conformance",
            score_schema_conformance(missing, len(required)),
            weight=2.0,
        )
        total = len(final_json.get("functional_results", [])) + len(
            final_json.get("security_results", [])
        ) + len(final_json.get("boundary_results", []))
        scorer.add("coverage_volume", min(1.0, total / 40.0), weight=1.0)
        scorer.add(
            "llm_self_report",
            coerce_float(final_json.get("confidence", {}).get("score"), 0.7)
            if isinstance(final_json.get("confidence"), dict)
            else 0.7,
            weight=1.0,
        )
        return scorer.block(rationale=f"total_cases={total}, schema_missing={missing}")
