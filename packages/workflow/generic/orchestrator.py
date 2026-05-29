"""Direct-prompt chain orchestrator.

每个工具下的 5 个 substep 都是「独立可用的子测试」：
  - 拿同一份 user 输入（业务材料）作为 placeholder
  - 不依赖前序 substep 的输出
  - 各自产生一份 JSON 报告（含本子测试的发现 / gate）

最终报告 = 所有 substep 的输出聚合 + 取最后一个 substep 的 gate_decision 作为整体闸门。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from packages.core.models.common import GateAction, GateDecision, ReportMeta
from packages.core.telemetry import get_logger
from packages.workflow.base import StepOrchestrator

logger = get_logger(__name__)


class GenericReport(BaseModel):
    """Light report: meta + 各 substep 输出 + 取整体 gate。

    统一报告契约字段(从 finalize substep 提升到顶层,供 UI 直接读取)：
    verdict / verdict_summary / risks / blockers / issues / cases
    """

    meta: ReportMeta
    step_id: str
    name: str
    substeps: dict[str, Any] = Field(default_factory=dict)
    final: dict[str, Any] = Field(default_factory=dict)
    gate_decision: GateDecision | None = None
    confidence: dict[str, Any] = Field(default_factory=dict)
    # 统一报告契约
    verdict: str | None = None
    verdict_summary: str | None = None
    risks: list[Any] = Field(default_factory=list)
    blockers: list[Any] = Field(default_factory=list)
    issues: list[Any] = Field(default_factory=list)
    cases: list[Any] = Field(default_factory=list)


class DirectChainOrchestrator(StepOrchestrator):
    """所有工具的统一编排器。"""

    display_name: str = "Tool"

    def _user_blob(self) -> str:
        i = self.ctx.inputs or {}
        blob = (
            i.get("documents")
            or i.get("raw_documents")
            or i.get("prd")
            or i.get("requirement_report")
            or i.get("test_case_report")
            or ""
        )
        if isinstance(blob, dict):
            import json as _json
            blob = _json.dumps(blob, ensure_ascii=False, indent=2)
        return blob or ""

    def placeholders_for(self, tpl: Any) -> dict[str, Any]:
        """把用户的单一输入塞给模板里**所有声明**的 placeholder。

        这样 prompt 里写 `{{业务材料}}` / `{{页面盘点}}` / `{{站点信息}}` 都能拿到同一份 user blob。
        模板可继续声明自己的 placeholder 名字，用户只需粘贴一份。
        """
        blob = self._user_blob()
        return {name: blob for name in tpl.placeholders}

    def _coerce_gate(self, raw: Any) -> GateDecision | None:
        if not isinstance(raw, dict):
            return None
        action = raw.get("action")
        if not action:
            return None
        s = str(action).lower().strip()
        if "reject" in s:
            ga = GateAction.REJECT_WITH_REPORT
        elif "warn" in s or "conditional" in s:
            ga = GateAction.WARN_AND_CONTINUE
        elif "proceed" in s or s == "pass":
            ga = GateAction.PASS
        else:
            try:
                ga = GateAction(s)
            except ValueError:
                ga = GateAction.WARN_AND_CONTINUE
        reasons = raw.get("reasons") or []
        if not isinstance(reasons, list):
            reasons = [str(reasons)]
        return GateDecision(action=ga, reasons=[str(r) for r in reasons])

    async def execute(self) -> GenericReport:
        self.ctx.bind_logs(self.step_id)

        templates_by_file = {tpl.path.name: tpl for tpl in self.prompts.templates.values()}
        substep_outputs: dict[str, Any] = {}
        last_model: str | None = None

        for fname in self.prompts.order:
            tpl = templates_by_file.get(fname)
            if tpl is None:
                raise RuntimeError(f"prompt file {fname} declared in meta.order but not loaded")
            sub_id = tpl.id
            # 同一份 user blob → 模板声明的所有 placeholder
            placeholders = self.placeholders_for(tpl)

            resp, parsed = await self.run_substep(sub_id, placeholders)
            if resp is None:
                substep_outputs[sub_id] = None
                continue
            last_model = resp.model_id
            substep_outputs[sub_id] = parsed

        # 找最后一个非 None 输出当 final（按 order 反向）
        final: Any = {}
        for sid in reversed(list(substep_outputs)):
            if substep_outputs[sid] is not None:
                final = substep_outputs[sid]
                break

        gate = self._coerce_gate(final.get("gate_decision") if isinstance(final, dict) else None)
        if gate is None:
            # 退而求其次：扫所有 substep 找最严重的 gate
            for sid in substep_outputs:
                v = substep_outputs[sid]
                if isinstance(v, dict) and v.get("gate_decision"):
                    g = self._coerce_gate(v["gate_decision"])
                    if g and (gate is None or g.action == GateAction.REJECT_WITH_REPORT):
                        gate = g

        confidence = final.get("confidence", {}) if isinstance(final, dict) else {}

        # 把 finalize substep 输出的统一契约字段提升到顶层 — 供 UI 直接读
        # 优先从 final 拿；某些工具可能在中间步就给齐了，作为兜底也扫所有 substep
        def _pull(field: str, accumulate_list: bool = False) -> Any:
            # 列表字段(cases/issues/risks/blockers)→ 永远累加所有 substep(含 finalize),
            # 去重靠 ID。之前的 bug:finalize 有该字段时直接只返回 finalize 的列表,
            # 导致前 4 个子步骤的用例 / 问题全丢。finalize 只需贡献自己新增的那部分,
            # 系统负责把 5 个子步骤的产出合并齐。
            if accumulate_list:
                seen_ids: set[str] = set()
                merged: list[Any] = []
                for out in substep_outputs.values():
                    if not isinstance(out, dict):
                        continue
                    arr = out.get(field)
                    if not isinstance(arr, list):
                        continue
                    for item in arr:
                        key = ""
                        if isinstance(item, dict):
                            key = str(item.get("id") or item.get("issue_id") or item.get("case_id") or "")
                        if key and key in seen_ids:
                            continue
                        if key:
                            seen_ids.add(key)
                        merged.append(item)
                return merged
            # 标量字段(verdict / verdict_summary)→ 优先取 finalize 的
            if isinstance(final, dict) and final.get(field):
                return final[field]
            return None

        verdict_val = _pull("verdict")
        verdict_summary_val = _pull("verdict_summary")
        risks_val = _pull("risks", accumulate_list=True) or []
        blockers_val = _pull("blockers", accumulate_list=True) or []
        issues_val = _pull("issues", accumulate_list=True) or []
        cases_val = _pull("cases", accumulate_list=True) or []

        return GenericReport(
            meta=self.build_meta(last_model or "?", self.ctx.inputs),
            step_id=self.step_id,
            name=self.display_name,
            substeps=substep_outputs,
            final=final if isinstance(final, dict) else {"value": final},
            gate_decision=gate,
            confidence=confidence,
            verdict=verdict_val if isinstance(verdict_val, str) else None,
            verdict_summary=verdict_summary_val if isinstance(verdict_summary_val, str) else None,
            risks=risks_val,
            blockers=blockers_val,
            issues=issues_val,
            cases=cases_val,
        )


# ---------------------------------------------------------------------
# 8 个工具：只声明 step_dir / step_id / display_name
# 所有都共享同一个 execute() — 5 个 substep 各自独立测试
# ---------------------------------------------------------------------


class RequirementReviewOrchestrator(DirectChainOrchestrator):
    step_dir = "step1_requirement"
    step_id = "step1"
    display_name = "需求评审"


class TestCaseDesignOrchestrator(DirectChainOrchestrator):
    step_dir = "step2_testcase"
    step_id = "step2"
    display_name = "测试用例设计"


class ApiTestOrchestrator(DirectChainOrchestrator):
    step_dir = "step4_api"
    step_id = "step4"
    display_name = "接口测试"


class UiConsistencyOrchestrator(DirectChainOrchestrator):
    step_dir = "step5_ui"
    step_id = "step5"
    display_name = "UI 一致性比对"


class AgentExecutionOrchestrator(DirectChainOrchestrator):
    step_dir = "step6_agent"
    step_id = "step6"
    display_name = "Agent 自动化执行"


class NetworkResilienceOrchestrator(DirectChainOrchestrator):
    step_dir = "network_resilience"
    step_id = "network_resilience"
    display_name = "弱网/断网测试"


class SeoAuditOrchestrator(DirectChainOrchestrator):
    step_dir = "seo_audit"
    step_id = "seo_audit"
    display_name = "SEO 深度审计"


class H5AdaptOrchestrator(DirectChainOrchestrator):
    step_dir = "h5_adapt"
    step_id = "h5_adapt"
    display_name = "H5 适配初审"
