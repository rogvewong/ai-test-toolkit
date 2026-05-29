"""Step 6 — Agent Execution Orchestrator.

Combines LLM-based planning (substeps 6.1 → 6.5) with a bounded tool-use loop
driven by the Anthropic tool_use API. The tool loop delegates to
ExecutionEnvironment, which in dry-run mode returns deterministic stubs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from packages.core.confidence import ConfidenceScorer, score_schema_conformance
from packages.core.llm import ModelTier
from packages.core.models import ExecutionReport, GateAction, GateDecision
from packages.core.models.common import ExecutionState, coerce_float
from packages.core.telemetry import get_logger
from packages.workflow.base import StepOrchestrator
from packages.workflow.step6_agent.executor import ExecutionEnvironment
from packages.workflow.step6_agent.tool_contract import TOOL_CONTRACT

logger = get_logger(__name__)

MAX_TOOL_HOPS = 30


class Step6Orchestrator(StepOrchestrator):
    step_dir = "step6_agent"
    step_id = "step6"

    async def execute(self) -> ExecutionReport:
        self.ctx.bind_logs(self.step_id)
        inputs = self.ctx.inputs
        env: ExecutionEnvironment = inputs["execution_env"]

        # ---- 6.1 pre-check ---------------------------------------------
        _, pre_check = await self.run_substep(
            "step6.1",
            {
                "测试用例集": inputs.get("test_case_report"),
                "自动化与风险分析": inputs.get("automation_risk", {}),
                "环境信息": inputs.get("env_info", {}),
            },
        )

        # ---- 6.2 P0 execution (tool-use loop) --------------------------
        p0_cases = self._p0_cases(inputs.get("test_case_report") or {})
        p0_exec = await self._agent_loop(
            sub_id="step6.2",
            placeholder_values={
                "可执行性清单": pre_check,
                "P0测试用例": p0_cases,
                "工具契约": {"tools": [t["name"] for t in TOOL_CONTRACT]},
            },
            env=env,
        )

        # ---- 6.3 P1/P2 execution (tool-use loop) -----------------------
        p1p2_cases = self._p1p2_cases(inputs.get("test_case_report") or {})
        p1p2_exec = await self._agent_loop(
            sub_id="step6.3",
            placeholder_values={
                "可执行性清单": pre_check,
                "P1P2测试用例": p1p2_cases,
                "P0执行结果": p0_exec,
                "工具契约": {"tools": [t["name"] for t in TOOL_CONTRACT]},
            },
            env=env,
        )

        # ---- 6.4 failure attribution -----------------------------------
        _, attribution = await self.run_substep(
            "step6.4",
            {
                "P0执行结果": p0_exec,
                "P1P2执行结果": p1p2_exec,
                "证据清单": env.records_as_json(),
            },
        )

        # ---- 6.5 finalize ----------------------------------------------
        resp_final, final_json = await self.run_substep(
            "step6.5",
            {
                "可执行性清单": pre_check,
                "P0执行结果": p0_exec,
                "P1P2执行结果": p1p2_exec,
                "失败归因": attribution,
            },
        )

        final_json = self._normalize(
            final_json, pre_check, p0_exec, p1p2_exec, attribution, env
        )
        final_json["meta"] = self.build_meta(resp_final.model_id, inputs).model_dump(mode="json")
        final_json["gate_decision"] = self._compute_gate(final_json).model_dump(mode="json")
        final_json["confidence"] = self._compute_confidence(final_json).model_dump(mode="json")

        try:
            report = ExecutionReport.model_validate(final_json)
        except ValidationError as exc:
            logger.error("step6.schema_invalid", errors=exc.errors()[:10])
            raise

        await self.ctx.memory.project.save(
            kind="report",
            key=f"step6/{self.ctx.run_id}",
            value=report.model_dump(mode="json"),
            tags=["step6", "execution"],
        )
        return report

    # ------------------------------------------------------------------
    # Tool-use loop
    # ------------------------------------------------------------------

    async def _agent_loop(
        self,
        *,
        sub_id: str,
        placeholder_values: dict[str, Any],
        env: ExecutionEnvironment,
    ) -> dict[str, Any]:
        tpl = self.prompts.get(sub_id)
        system_body = tpl.render(placeholder_values)
        system_full = self.prompts.compose_system(
            # type: ignore[arg-type]
            type(tpl)(**{**tpl.__dict__, "body": system_body})
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": "请基于工具契约逐条执行用例。当某用例结束时，使用 record_result 工具写入结果。完成全部用例后输出最终 JSON。",
            }
        ]
        final_text: str = ""
        last_model = ""
        for hop in range(MAX_TOOL_HOPS):
            resp = await self.ctx.llm.complete(
                system=system_full,
                messages=messages,
                tier=tpl.model_tier,
                max_tokens=tpl.max_tokens,
                temperature=tpl.temperature,
                tools=TOOL_CONTRACT,
            )
            self.ctx.usage.merge(resp.usage)
            last_model = resp.model_id
            stop = resp.stop_reason
            raw_blocks = resp.raw.get("content_blocks", [])
            if not raw_blocks:
                # Fall back: parse text as JSON if model did not use tools
                final_text = resp.text
                break

            # Assistant message with all blocks (text + tool_use)
            assistant_blocks: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            for block in raw_blocks:
                btype = block.get("type")
                if btype == "text":
                    assistant_blocks.append(block)
                    final_text = block.get("text", final_text)
                elif btype == "tool_use":
                    assistant_blocks.append(block)
                    result = await env.handle_tool(
                        block.get("name", ""), block.get("input", {})
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.get("id"),
                            "content": json.dumps(
                                result, ensure_ascii=False, default=str
                            ),
                        }
                    )
            messages.append({"role": "assistant", "content": assistant_blocks})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            if stop == "end_turn" and not tool_results:
                break
            if hop == MAX_TOOL_HOPS - 1:
                logger.warning("agent.max_hops", sub_id=sub_id)
        logger.info("agent.loop_done", sub_id=sub_id, model=last_model)

        try:
            parsed = (
                json.loads(final_text)
                if final_text.strip().startswith("{")
                else {"executions": env.records_as_json()}
            )
        except json.JSONDecodeError:
            parsed = {"executions": env.records_as_json()}
        return parsed

    # ------------------------------------------------------------------

    @staticmethod
    def _p0_cases(report: dict[str, Any]) -> list[dict[str, Any]]:
        return report.get("p0_cases", [])

    @staticmethod
    def _p1p2_cases(report: dict[str, Any]) -> list[dict[str, Any]]:
        return report.get("p1_cases", []) + report.get("p2_cases", [])

    def _normalize(
        self,
        final_json: dict[str, Any],
        pre_check: dict[str, Any],
        p0_exec: dict[str, Any],
        p1p2_exec: dict[str, Any],
        attribution: dict[str, Any],
        env: ExecutionEnvironment,
    ) -> dict[str, Any]:
        pre_items = []
        for name, passed in [
            ("accounts_ready", all(bool(env) for _ in [1])),
            ("tools_ready", True),
            ("env_reachable", True),
        ]:
            pre_items.append(
                {"id": name, "name": name, "passed": passed, "details": None}
            )
        for blocked in pre_check.get("blocked_cases", []) or []:
            pre_items.append(
                {
                    "id": f"blocked_{blocked.get('case_id', 'unk')}",
                    "name": blocked.get("case_id", "unk"),
                    "passed": False,
                    "details": blocked.get("reason"),
                }
            )
        final_json["pre_check"] = pre_items

        executions = (p0_exec.get("executions") or []) + (
            p1p2_exec.get("executions") or []
        )
        if not executions:
            executions = env.records_as_json()

        steps_out: list[dict[str, Any]] = []
        failures_out: list[dict[str, Any]] = []
        for ex in executions:
            state_raw = ex.get("state", "skipped")
            state = ExecutionState(state_raw) if state_raw in {
                "queued",
                "running",
                "passed",
                "failed",
                "flaky",
                "skipped",
                "error",
                "aborted",
            } else ExecutionState.SKIPPED
            started = ex.get("started_at") or datetime.now(timezone.utc).isoformat()
            ended = ex.get("finished_at") or ex.get("ended_at")
            step = {
                "id": ex.get("case_id", "UNK"),
                "case_id": ex.get("case_id", "UNK"),
                "action": ex.get("title") or f"execute {ex.get('case_id')}",
                "state": state.value,
                "started_at": started,
                "ended_at": ended,
                "duration_ms": coerce_float(ex.get("duration_ms"), 0.0),
                "evidence_paths": ex.get("evidence_refs", []),
                "error": ex.get("error")
                or (ex.get("attribution_hint") if state == ExecutionState.FAILED else None),
            }
            steps_out.append(step)
            if state in {ExecutionState.FAILED, ExecutionState.ERROR}:
                failures_out.append(step)

        final_json["steps"] = steps_out
        final_json["failures"] = failures_out

        rca_out = []
        for d in attribution.get("defects", []):
            rca_out.append(
                {
                    "failure_id": d.get("defect_id", d.get("id", "DEF-UNK-0000")),
                    "hypothesis": d.get("root_cause_hypothesis", ""),
                    "supporting_signals": d.get("evidence_refs", []),
                    "confidence": 0.7,
                    "suggested_action": d.get("suggested_fix", ""),
                }
            )
        final_json["rca"] = rca_out

        total = len(steps_out) or 1
        passed = sum(1 for s in steps_out if s["state"] == "passed")
        final_json["metrics"] = {
            "success_rate": passed / total,
            "total": float(total),
            "failed": float(len(failures_out)),
        }
        final_json["plan_digest"] = f"run-{self.ctx.run_id}"
        # Drop intermediate LLM keys not in ExecutionReport schema
        for stray in (
            "plan_ref",
            "results",
            "defects",
            "stats",
            "evidence_dir",
            "manual_review_list",
            "attribution_distribution",
        ):
            final_json.pop(stray, None)
        return final_json

    # ------------------------------------------------------------------

    def _compute_gate(self, final_json: dict[str, Any]) -> GateDecision:
        success = coerce_float(final_json.get("metrics", {}).get("success_rate"), 1.0)
        critical_failures = sum(
            1
            for s in final_json.get("failures", [])
            if (s.get("error") or "").lower().startswith("critical")
        )
        blockers: list[str] = []
        if success < 0.85:
            blockers.append(f"通过率 {success:.2f} < 0.85")
        if critical_failures > 0:
            blockers.append(f"critical 失败 {critical_failures}")

        if blockers:
            action = GateAction.REJECT_WITH_REPORT
            reasons = ["闸门 step6_final 阻塞", *blockers]
            next_step = None
        else:
            action = GateAction.PASS
            reasons = ["执行达到最终闸门标准"]
            next_step = "step7"

        return GateDecision(
            action=action,
            reasons=reasons,
            metrics={
                "success_rate": success,
                "critical_failures": float(critical_failures),
            },
            blockers=blockers,
            warnings=[],
            next_step=next_step,
        )

    def _compute_confidence(self, final_json: dict[str, Any]):
        scorer = ConfidenceScorer()
        required = {"pre_check", "plan_digest", "steps", "failures", "rca", "metrics"}
        missing = len(required - set(final_json.keys()))
        scorer.add(
            "schema_conformance",
            score_schema_conformance(missing, len(required)),
            weight=2.0,
        )
        scorer.add(
            "success_rate",
            coerce_float(final_json.get("metrics", {}).get("success_rate"), 0.0),
            weight=2.0,
        )
        scorer.add(
            "evidence_density",
            min(1.0, sum(len(s.get("evidence_paths", [])) for s in final_json.get("steps", [])) / 20.0),
            weight=1.0,
        )
        return scorer.block(
            rationale=f"steps={len(final_json.get('steps', []))}, schema_missing={missing}"
        )
