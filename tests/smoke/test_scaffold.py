"""Smoke tests — no network, no LLM calls. Verifies scaffold integrity.

Run: pytest tests/smoke -v
"""
from __future__ import annotations

import json

import pytest


def test_imports_core() -> None:
    from packages.core import config, llm, memory, prompts, telemetry, confidence  # noqa: F401
    from packages.core.models import (
        RequirementBreakdownReport,
        TestCaseReport,
        ApiTestReport,
        UiConsistencyReport,
        ExecutionReport,
        TdrReviewReport,
    )
    # cheap sanity: all 6 reports are pydantic models
    for cls in (
        RequirementBreakdownReport,
        TestCaseReport,
        ApiTestReport,
        UiConsistencyReport,
        ExecutionReport,
        TdrReviewReport,
    ):
        assert hasattr(cls, "model_validate")


def test_imports_modules_all_ten() -> None:
    from packages.modules.api_testing import ApiTestingExecutor
    from packages.modules.server_perf import ServerPerfExecutor
    from packages.modules.network_perf import NetworkPerfExecutor
    from packages.modules.client_perf import ClientPerfExecutor
    from packages.modules.security import SecurityExecutor
    from packages.modules.stability import StabilityExecutor
    from packages.modules.compatibility import CompatibilityExecutor
    from packages.modules.data_consistency import DataConsistencyExecutor
    from packages.modules.ui_testing import UiTestingExecutor
    from packages.modules.crash_analysis import CrashAnalysisExecutor

    executors = [
        ApiTestingExecutor,
        ServerPerfExecutor,
        NetworkPerfExecutor,
        ClientPerfExecutor,
        SecurityExecutor,
        StabilityExecutor,
        CompatibilityExecutor,
        DataConsistencyExecutor,
        UiTestingExecutor,
        CrashAnalysisExecutor,
    ]
    assert len(executors) == 10
    for e in executors:
        assert hasattr(e, "module_name")


def test_standards_yaml_loads() -> None:
    from packages.core.config import standards

    for name in ("process", "quality", "data", "tdr"):
        doc = getattr(standards, name)
        assert isinstance(doc, dict)
        assert doc.get("version"), f"{name}.yaml missing version"


def test_prompts_step1_loads_all_five() -> None:
    from packages.core.prompts import load_step

    step = load_step("step1_requirement")
    assert step.step_id == "step1"
    for sub_id in ("step1.1", "step1.2", "step1.3", "step1.4", "step1.5"):
        tpl = step.get(sub_id)
        assert tpl.id == sub_id
        assert tpl.body, f"{sub_id} empty body"
        assert tpl.output_format == "json"


def test_prompt_render_replaces_chinese_placeholders() -> None:
    from packages.core.prompts import load_step

    tpl = load_step("step1_requirement").get("step1.1")
    rendered = tpl.render({"PRD": "登录模块PRD", "原型图": "登录原型"})
    assert "登录模块PRD" in rendered
    assert "登录原型" in rendered
    # Missing placeholders are marked
    assert "(未提供)" in rendered
    assert "未提供" in rendered


def test_confidence_grades() -> None:
    from packages.core.models.common import ConfidenceGrade, grade_confidence

    assert grade_confidence(0.95) is ConfidenceGrade.AUTO_PASS
    assert grade_confidence(0.80) is ConfidenceGrade.SUGGEST_REVIEW
    assert grade_confidence(0.50) is ConfidenceGrade.MANDATORY_REVIEW


def test_fingerprint_is_deterministic() -> None:
    from packages.core.models.common import fingerprint

    a = fingerprint({"b": 2, "a": 1})
    b = fingerprint({"a": 1, "b": 2})
    assert a == b
    assert len(a) == 64


def test_new_id_format() -> None:
    from packages.core.models.common import new_id

    assert new_id("TC", "log", 7) == "TC-LOG-0007"
    with pytest.raises(ValueError):
        new_id("TC", "xx", 1)  # module must be 3 chars


@pytest.mark.asyncio
async def test_sqlite_memory_roundtrip(tmp_path) -> None:
    from packages.core.memory import LayeredMemory, SqliteMemoryStore

    store = SqliteMemoryStore(str(tmp_path / "mem.db"))
    mem = LayeredMemory(store=store, run_id="r1", project_id="p1", tenant_id="t1")
    await mem.session.save(kind="turn", key="k1", value={"foo": "bar"})
    rec = await mem.session.get("k1")
    assert rec is not None
    assert rec.value == {"foo": "bar"}
    hits = await mem.session.search(query="bar")
    assert len(hits) == 1


def test_gate_pass_when_no_blockers() -> None:
    """Gate decision on a clean input should yield PASS."""
    from packages.core.models.common import GateAction
    from packages.workflow.step1_requirement.orchestrator import Step1Orchestrator

    # Build a minimal orchestrator just to call the pure gate function.
    # We bypass __init__ since we don't need ctx for _compute_gate.
    orch = Step1Orchestrator.__new__(Step1Orchestrator)
    decision = orch._compute_gate(
        inventory={
            "materials": [{"type": "prd", "provided": True}],
            "overall_completeness_score": 0.95,
        },
        gaps={"blocker_count": 0, "ambiguities": []},
    )
    assert decision.action is GateAction.PASS
    assert decision.next_step == "step2"


def test_gate_rejects_when_prd_missing() -> None:
    from packages.core.models.common import GateAction
    from packages.workflow.step1_requirement.orchestrator import Step1Orchestrator

    orch = Step1Orchestrator.__new__(Step1Orchestrator)
    decision = orch._compute_gate(
        inventory={"materials": [], "overall_completeness_score": 0.4},
        gaps={"blocker_count": 5},
    )
    assert decision.action is GateAction.REJECT_WITH_REPORT
    assert any("PRD" in b for b in decision.blockers)


def test_capabilities_yaml_present_for_all_modules() -> None:
    import yaml
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "packages" / "modules"
    expected = {
        "api_testing",
        "server_perf",
        "network_perf",
        "client_perf",
        "security",
        "stability",
        "compatibility",
        "data_consistency",
        "ui_testing",
        "crash_analysis",
    }
    for mod in expected:
        path = root / mod / "capabilities.yaml"
        assert path.exists(), f"{mod}/capabilities.yaml missing"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc["module"] == mod
        assert "priority_matrix" in doc


def test_report_model_example_roundtrips_json() -> None:
    """Construct a minimal RequirementBreakdownReport and ensure round-trip."""
    from packages.core.models import (
        ConfidenceBlock,
        ConfidenceGrade,
        GateAction,
        GateDecision,
        ReportMeta,
        RequirementBreakdownReport,
    )

    report = RequirementBreakdownReport(
        meta=ReportMeta(
            model_id="claude-sonnet-4-6",
            input_fingerprint="f" * 64,
            step_id="step1",
        ),
        materials=[],
        modules=[],
        flows=[],
        ambiguities=[],
        gate_decision=GateDecision(action=GateAction.PASS, next_step="step2"),
        confidence=ConfidenceBlock(score=0.9, grade=ConfidenceGrade.AUTO_PASS),
    )
    dumped = report.model_dump(mode="json")
    re_parsed = RequirementBreakdownReport.model_validate(
        json.loads(json.dumps(dumped, default=str))
    )
    assert re_parsed.gate_decision.action is GateAction.PASS
