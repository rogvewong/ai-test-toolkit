"""End-to-end orchestrator tests with a stubbed LLM.

We don't exercise the LLM at all — the `StubLlmClient` fixture returns a
scripted sequence of responses for each substep. The goal is to verify that
each orchestrator:
  * wires its substeps in the right order
  * normalises LLM output to the strict report schema
  * produces a deterministic gate decision
  * persists the final report to project memory
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from packages.core.models import (
    ApiTestReport,
    ExecutionReport,
    UiConsistencyReport,
)
from packages.core.models import TestCaseReport as _TestCaseReport  # avoid pytest collection warning
from packages.core.models.common import GateAction
from packages.workflow.step2_testcase import Step2Orchestrator
from packages.workflow.step4_api import Step4Orchestrator
from packages.workflow.step5_ui import Step5Orchestrator
from packages.workflow.step6_agent import Step6Orchestrator
from packages.workflow.step6_agent.executor import ExecutionEnvironment


# =====================================================================
# Step 2 — Test Case Design
# =====================================================================


STEP2_RESPONSES: list[dict[str, Any]] = [
    # 2.1 coverage map
    {
        "coverage_entries": [
            {
                "requirement_id": "REQ-LOG-001",
                "flow_id": "FLOW-LOG-001",
                "case_ids": ["TC-LOG-0001"],
                "coverage_type": "positive",
            },
            {
                "requirement_id": "REQ-LOG-002",
                "flow_id": "FLOW-LOG-001",
                "case_ids": ["TC-LOG-0002"],
                "coverage_type": "negative",
            },
        ]
    },
    # 2.2 P0 cases
    {
        "cases": [
            {
                "id": "TC-LOG-0001",
                "title": "手机号登录正常流程",
                "priority": "P0",
                "module_id": "MOD-LOG-001",
                "flow_id": "FLOW-LOG-001",
                "preconditions": ["账号已激活"],
                "steps": [
                    {"order": 1, "action": "输入手机号", "expected": "UI 接收"},
                    {"order": 2, "action": "输入密码", "expected": "UI 接收"},
                    {"order": 3, "action": "点击登录", "expected": "登录成功"},
                ],
                "expected_result": "跳转首页",
                "automation_tag": "auto",
                "requirements_covered": ["REQ-LOG-001"],
            },
            {
                "id": "TC-LOG-0002",
                "title": "密码错误",
                "priority": "P0",
                "module_id": "MOD-LOG-001",
                "steps": [{"order": 1, "action": "输入错误密码", "expected": "报错"}],
                "expected_result": "提示密码错误",
                "automation_tag": "auto",
                "requirements_covered": ["REQ-LOG-002"],
            },
        ]
    },
    # 2.3 P1/P2 cases
    {
        "p1_cases": [
            {
                "id": "TC-LOG-0003",
                "title": "验证码登录",
                "priority": "P1",
                "module_id": "MOD-LOG-001",
                "steps": ["输入验证码"],
                "expected_result": "登录成功",
                "automation_tag": "semi_auto",
                "requirements_covered": ["REQ-LOG-001"],
            }
        ],
        "p2_cases": [
            {
                "id": "TC-LOG-0004",
                "title": "记住我",
                "priority": "P2",
                "module_id": "MOD-LOG-001",
                "steps": ["勾选记住我"],
                "expected_result": "下次自动登录",
                "automation_tag": "manual",
                "requirements_covered": [],
            }
        ],
    },
    # 2.4 automation & risk
    {
        "automation_summary": {"auto": 2, "semi_auto": 1, "manual": 1},
        "risks": [],
    },
    # 2.5 finalize — intentionally empty; orchestrator fills from prior substeps
    {},
]


@pytest.mark.asyncio
async def test_step2_orchestrator_happy_path(
    make_ctx, passing_requirement_report
) -> None:
    ctx, stub = make_ctx(
        STEP2_RESPONSES,
        inputs={"requirement_report": passing_requirement_report},
    )
    report = await Step2Orchestrator(ctx).execute()

    assert isinstance(report, _TestCaseReport)
    assert len(stub.calls) == 5
    # coverage_map was derived from 2.1's LLM payload
    assert {e.requirement_id for e in report.coverage_map} == {
        "REQ-LOG-001",
        "REQ-LOG-002",
    }
    # Case buckets normalised
    assert [c.id for c in report.p0_cases] == ["TC-LOG-0001", "TC-LOG-0002"]
    assert [c.id for c in report.p1_cases] == ["TC-LOG-0003"]
    assert [c.id for c in report.p2_cases] == ["TC-LOG-0004"]
    # Automation summary derived deterministically
    assert report.automation_summary == {"auto": 2, "semi_auto": 1, "manual": 1}
    # Gate: all P0 reqs covered, all cases labelled → PASS
    assert report.gate_decision.action is GateAction.PASS
    assert report.gate_decision.next_step == "step2" or report.gate_decision.next_step == "step4"

    # Report persisted to project memory
    rec = await ctx.memory.project.get(f"step2/{ctx.run_id}")
    assert rec is not None


@pytest.mark.asyncio
async def test_step2_gate_rejects_on_missing_p0_coverage(
    make_ctx, passing_requirement_report
) -> None:
    # Drop REQ-LOG-002 from P0 (keep it only as P1) → p0_coverage < 1.0
    responses: list[dict[str, Any]] = [dict(r) for r in STEP2_RESPONSES]
    responses[1] = {
        "cases": [
            {
                "id": "TC-LOG-0001",
                "title": "手机号登录",
                "priority": "P0",
                "module_id": "MOD-LOG-001",
                "steps": [{"order": 1, "action": "登录", "expected": "成功"}],
                "expected_result": "跳转首页",
                "automation_tag": "auto",
                "requirements_covered": ["REQ-LOG-001"],
            }
        ]
    }
    ctx, _ = make_ctx(
        responses, inputs={"requirement_report": passing_requirement_report}
    )
    report = await Step2Orchestrator(ctx).execute()
    assert report.gate_decision.action is GateAction.REJECT_WITH_REPORT
    assert any("P0 覆盖率" in b for b in report.gate_decision.blockers)


# =====================================================================
# Step 4 — API Testing
# =====================================================================


STEP4_RESPONSES: list[dict[str, Any]] = [
    # 4.1 interface inventory
    {
        "endpoints": [
            {
                "endpoint_id": "EP-LOG-0001",
                "method": "POST",
                "path": "/api/login",
                "purpose": "用户登录",
                "requires_auth": False,
            }
        ],
        "auth_endpoints": ["EP-LOG-0001"],
    },
    # 4.2 functional
    {
        "api_cases": [
            {
                "case_id": "AC-LOG-0001",
                "endpoint_id": "EP-LOG-0001",
                "category": "functional",
                "request_snapshot": {"phone": "13800001111"},
                "response_snapshot": {"code": 0},
                "assertions": [{"type": "status_code", "op": "eq", "expected": 200}],
                "state": "passed",
                "response_time_ms": 120,
            }
        ]
    },
    # 4.3 security
    {
        "security_cases": [
            {
                "case_id": "SEC-LOG-0001",
                "endpoint_id": "EP-LOG-0001",
                "request_snapshot": {"phone": "' or 1=1"},
                "response_snapshot": {"code": 400},
                "assertions": [{"type": "status_code", "op": "eq", "expected": 400}],
                "state": "passed",
            }
        ]
    },
    # 4.4 boundary
    {
        "boundary_cases": [
            {
                "case_id": "BND-LOG-0001",
                "endpoint_id": "EP-LOG-0001",
                "request_snapshot": {"phone": ""},
                "response_snapshot": {"code": 400},
                "assertions": [],
                "state": "passed",
            }
        ]
    },
    # 4.5 finalize
    {
        "defects": [],
        "coverage_summary": {
            "p0_pass_rate": 1.0,
            "endpoint_coverage": 1.0,
            "security_coverage": 1.0,
            "avg_latency_ms": 120,
            "p95_latency_ms": 150,
        },
    },
]


@pytest.mark.asyncio
async def test_step4_orchestrator_happy_path(make_ctx, passing_requirement_report) -> None:
    ctx, stub = make_ctx(
        STEP4_RESPONSES,
        inputs={
            "requirement_report": passing_requirement_report,
            "test_case_report": {"p0_cases": []},
            "api_doc": "paths: {'/api/login': {}}",
        },
    )
    report = await Step4Orchestrator(ctx).execute()

    assert isinstance(report, ApiTestReport)
    assert len(stub.calls) == 5
    assert [e.id for e in report.api_list] == ["EP-LOG-0001"]
    assert len(report.functional_results) == 1
    assert len(report.security_results) == 1
    assert len(report.boundary_results) == 1
    assert report.metrics["pass_rate"] == 1.0
    assert report.gate_decision.action is GateAction.PASS
    assert report.gate_decision.next_step == "step6"


@pytest.mark.asyncio
async def test_step4_gate_rejects_on_critical_defect(make_ctx, passing_requirement_report) -> None:
    resps: list[dict[str, Any]] = [dict(r) for r in STEP4_RESPONSES]
    resps[4] = {
        **resps[4],
        "defects": [
            {
                "id": "DEF-API-0001",
                "title": "SQL 注入",
                "severity": "critical",
                "reproduction_steps": ["发送 ' or 1=1"],
                "actual_result": "返回 200",
                "expected_result": "应该返回 400",
            }
        ],
    }
    ctx, _ = make_ctx(
        resps,
        inputs={
            "requirement_report": passing_requirement_report,
            "test_case_report": {"p0_cases": []},
            "api_doc": None,
        },
    )
    report = await Step4Orchestrator(ctx).execute()
    assert report.gate_decision.action is GateAction.REJECT_WITH_REPORT
    assert report.gate_decision.metrics["critical_defects"] == 1.0


# =====================================================================
# Step 5 — UI Consistency
# =====================================================================


STEP5_RESPONSES: list[dict[str, Any]] = [
    # 5.1 baseline
    {
        "baselines": [
            {
                "page_id": "PAGE-LOG-0001",
                "page_name": "登录页",
                "baseline_source": "design_doc",
                "baseline_ref": "figma://login/v1",
            }
        ]
    },
    # 5.2 structure diff
    {
        "page_diffs": [
            {
                "page_id": "PAGE-LOG-0001",
                "total_diff_pct": 0.05,
                "diffs": [
                    {
                        "diff_id": "UID-LOG-0001",
                        "category": "color",
                        "severity": "minor",
                        "actual_ref": "evidence/login.png",
                        "note": "按钮颜色偏差 3%",
                    }
                ],
            }
        ]
    },
    # 5.3 interaction diff
    {"interaction_diffs": []},
    # 5.4 dual end
    {"cross_platform_diffs": []},
    # 5.5 finalize
    {
        "per_page_stats": [{"page_id": "PAGE-LOG-0001", "total_diff_pct": 0.05}],
        "overall_deviation_ratio": 0.05,
    },
]


@pytest.mark.asyncio
async def test_step5_orchestrator_happy_path(make_ctx) -> None:
    ctx, stub = make_ctx(
        STEP5_RESPONSES,
        inputs={
            "requirement_report": {},
            "test_case_report": {},
            "design_assets": [{"url": "figma://login/v1"}],
            "actual_snapshots": [],
            "state_captures": [],
        },
    )
    report = await Step5Orchestrator(ctx).execute()

    assert isinstance(report, UiConsistencyReport)
    assert len(stub.calls) == 5
    assert len(report.references) == 1
    assert report.overall_deviation_ratio == pytest.approx(0.05)
    # Minor → low severity under the orchestrator's mapping
    assert report.severity_map["low"] == 1
    assert report.gate_decision.action is GateAction.PASS


@pytest.mark.asyncio
async def test_step5_gate_warns_when_deviation_above_10_pct(make_ctx) -> None:
    resps = [dict(r) for r in STEP5_RESPONSES]
    resps[4] = {
        "per_page_stats": [{"page_id": "PAGE-LOG-0001", "total_diff_pct": 0.15}],
        "overall_deviation_ratio": 0.15,
    }
    ctx, _ = make_ctx(
        resps,
        inputs={
            "requirement_report": {},
            "test_case_report": {},
            "design_assets": [],
            "actual_snapshots": [],
            "state_captures": [],
        },
    )
    report = await Step5Orchestrator(ctx).execute()
    assert report.gate_decision.action is GateAction.WARN_AND_CONTINUE


@pytest.mark.asyncio
async def test_step5_gate_rejects_when_deviation_above_20_pct(make_ctx) -> None:
    resps = [dict(r) for r in STEP5_RESPONSES]
    resps[4] = {
        "per_page_stats": [{"page_id": "PAGE-LOG-0001", "total_diff_pct": 0.35}],
        "overall_deviation_ratio": 0.35,
    }
    ctx, _ = make_ctx(
        resps,
        inputs={
            "requirement_report": {},
            "test_case_report": {},
            "design_assets": [],
            "actual_snapshots": [],
            "state_captures": [],
        },
    )
    report = await Step5Orchestrator(ctx).execute()
    assert report.gate_decision.action is GateAction.REJECT_WITH_REPORT


# =====================================================================
# Step 6 — Agent Execution
# =====================================================================


@pytest.mark.asyncio
async def test_step6_orchestrator_dry_run(make_ctx, tmp_path: Path) -> None:
    """Tool-use loops are short-circuited by returning text-only blocks so
    the loop exits on the first hop without dispatching tools."""
    responses: list[Any] = [
        # 6.1 pre-check
        {"blocked_cases": []},
        # 6.2 P0 execution — plain text block, no tool_use → loop exits
        (
            '{"executions": [{"case_id": "TC-LOG-0001", "state": "passed", '
            '"duration_ms": 50, "evidence_refs": ["ev1.log"]}]}',
            [{"type": "text", "text": "{\"executions\": [{\"case_id\": \"TC-LOG-0001\", \"state\": \"passed\", \"duration_ms\": 50, \"evidence_refs\": [\"ev1.log\"]}]}"}],
        ),
        # 6.3 P1/P2 execution
        (
            '{"executions": []}',
            [{"type": "text", "text": "{\"executions\": []}"}],
        ),
        # 6.4 attribution
        {"defects": []},
        # 6.5 finalize
        {},
    ]
    env = ExecutionEnvironment(evidence_dir=tmp_path / "step6_env", dry_run=True)
    ctx, stub = make_ctx(
        responses,
        inputs={
            "test_case_report": {
                "p0_cases": [{"id": "TC-LOG-0001"}],
                "p1_cases": [],
                "p2_cases": [],
            },
            "automation_risk": {},
            "env_info": {},
            "execution_env": env,
        },
    )
    report = await Step6Orchestrator(ctx).execute()

    assert isinstance(report, ExecutionReport)
    # 5 substep calls, all single-hop because no tool_use emitted
    assert len(stub.calls) == 5
    assert len(report.steps) == 1
    assert report.steps[0].state.value == "passed"
    assert report.metrics["success_rate"] == 1.0
    assert report.gate_decision.action is GateAction.PASS
    assert report.gate_decision.next_step == "step7"


@pytest.mark.asyncio
async def test_step6_gate_rejects_on_low_success_rate(make_ctx, tmp_path: Path) -> None:
    # 3 cases, only 1 passes → 0.33 success < 0.85 → reject
    executions = {
        "executions": [
            {"case_id": "TC-A", "state": "passed", "duration_ms": 10, "evidence_refs": []},
            {"case_id": "TC-B", "state": "failed", "duration_ms": 10, "evidence_refs": []},
            {"case_id": "TC-C", "state": "failed", "duration_ms": 10, "evidence_refs": []},
        ]
    }
    import json as _json

    exec_text = _json.dumps(executions)
    responses: list[Any] = [
        {"blocked_cases": []},
        (exec_text, [{"type": "text", "text": exec_text}]),
        ('{"executions": []}', [{"type": "text", "text": '{"executions": []}'}]),
        {"defects": []},
        {},
    ]
    env = ExecutionEnvironment(evidence_dir=tmp_path / "step6_env", dry_run=True)
    ctx, _ = make_ctx(
        responses,
        inputs={
            "test_case_report": {
                "p0_cases": [{"id": "TC-A"}, {"id": "TC-B"}, {"id": "TC-C"}],
                "p1_cases": [],
                "p2_cases": [],
            },
            "automation_risk": {},
            "env_info": {},
            "execution_env": env,
        },
    )
    report = await Step6Orchestrator(ctx).execute()
    assert report.gate_decision.action is GateAction.REJECT_WITH_REPORT
    assert report.metrics["success_rate"] < 0.85
