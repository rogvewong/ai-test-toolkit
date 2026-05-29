"""Integration tests for the Step 6 ExecutionEnvironment tool dispatcher,
plus the cross-step reporting aggregator + markdown/html renderers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from packages.reporting import aggregate, render_html, render_markdown
from packages.workflow.step6_agent.executor import ExecutionEnvironment
from packages.workflow.step6_agent.tool_contract import shell_is_allowed


# =====================================================================
# ExecutionEnvironment
# =====================================================================


@pytest.mark.asyncio
async def test_execenv_http_dry_run_returns_stub(tmp_path: Path) -> None:
    env = ExecutionEnvironment(evidence_dir=tmp_path / "ev", dry_run=True)
    result = await env.handle_tool(
        "http_request", {"method": "GET", "url": "http://x.local/p"}
    )
    assert result["status_code"] == 200
    assert result["body"]["mock"] is True


@pytest.mark.asyncio
async def test_execenv_browser_screenshot_writes_png(tmp_path: Path) -> None:
    env = ExecutionEnvironment(evidence_dir=tmp_path / "ev", dry_run=True)
    result = await env.handle_tool("browser_action", {"action": "screenshot"})
    assert result["ok"] is True
    snap = Path(result["path"])
    assert snap.exists()
    # PNG magic number
    assert snap.read_bytes().startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_execenv_shell_disabled_by_default(tmp_path: Path) -> None:
    env = ExecutionEnvironment(evidence_dir=tmp_path / "ev", dry_run=True)
    result = await env.handle_tool(
        "shell_readonly", {"command": "adb logcat -d"}
    )
    assert result["error"] == "shell disabled"


@pytest.mark.asyncio
async def test_execenv_shell_rejects_non_whitelisted(tmp_path: Path) -> None:
    env = ExecutionEnvironment(
        evidence_dir=tmp_path / "ev", dry_run=True, shell_enabled=True
    )
    result = await env.handle_tool("shell_readonly", {"command": "rm -rf /"})
    assert "error" in result
    assert "safe list" in result["error"]


@pytest.mark.asyncio
async def test_execenv_shell_accepts_whitelisted(tmp_path: Path) -> None:
    env = ExecutionEnvironment(
        evidence_dir=tmp_path / "ev", dry_run=True, shell_enabled=True
    )
    result = await env.handle_tool(
        "shell_readonly", {"command": "git status --short"}
    )
    assert result.get("stdout") == "mock output"
    assert result["returncode"] == 0


@pytest.mark.asyncio
async def test_execenv_record_result_persists_and_collects(tmp_path: Path) -> None:
    env = ExecutionEnvironment(evidence_dir=tmp_path / "ev", dry_run=True)
    await env.handle_tool(
        "record_result",
        {
            "case_id": "TC-ABC-0001",
            "state": "passed",
            "evidence_refs": ["x.png"],
            "steps": [{"order": 1, "action": "click"}],
        },
    )
    records = env.records_as_json()
    assert len(records) == 1
    assert records[0]["case_id"] == "TC-ABC-0001"
    assert records[0]["state"] == "passed"
    # On-disk artifact
    persisted = tmp_path / "ev" / "TC-ABC-0001.json"
    assert persisted.exists()
    on_disk = json.loads(persisted.read_text(encoding="utf-8"))
    assert on_disk["case_id"] == "TC-ABC-0001"


@pytest.mark.asyncio
async def test_execenv_unknown_tool_returns_error(tmp_path: Path) -> None:
    env = ExecutionEnvironment(evidence_dir=tmp_path / "ev", dry_run=True)
    result = await env.handle_tool("sorcery", {})
    assert "unknown tool" in result["error"]


def test_shell_whitelist_accepts_expected_prefixes() -> None:
    assert shell_is_allowed("git status")
    assert shell_is_allowed("kubectl get pods -n default")
    assert not shell_is_allowed("sudo rm -rf /")
    assert not shell_is_allowed("bash -c whoami")


# =====================================================================
# reporting aggregator + renderers
# =====================================================================


def _fake_step_reports() -> dict[str, dict[str, object]]:
    return {
        "step1": {
            "gate_decision": {
                "action": "pass",
                "reasons": ["all good"],
                "next_step": "step2",
            },
            "confidence": {"score": 0.92, "grade": "auto_pass"},
        },
        "step4": {
            "gate_decision": {
                "action": "warn_and_continue",
                "reasons": ["high latency"],
                "next_step": "step6",
            },
            "confidence": {"score": 0.81, "grade": "suggest_review"},
            "defects": [
                {"severity": "high", "title": "X"},
                {"severity": "medium", "title": "Y"},
            ],
        },
        "step6": {
            "gate_decision": {"action": "pass", "reasons": [], "next_step": "step7"},
            "confidence": {"score": 0.95, "grade": "auto_pass"},
            "metrics": {"success_rate": 0.95, "total": 40, "failed": 2},
            "defects": [{"severity": "low", "title": "Z"}],
        },
    }


def test_aggregator_gates_and_defects() -> None:
    bundle = aggregate(
        run_id="r1",
        project_id="p",
        tenant_id="t",
        step_reports=_fake_step_reports(),
    )
    gates = bundle.gates()
    assert [g["step"] for g in gates] == ["step1", "step4", "step6"]

    totals = bundle.defect_totals()
    # step4: 1 high, 1 medium; step6: 1 low
    assert totals == {"critical": 0, "high": 1, "medium": 1, "low": 1}


def test_markdown_renderer_includes_gates_and_confidence() -> None:
    bundle = aggregate(
        run_id="r1",
        project_id="p",
        tenant_id="t",
        step_reports=_fake_step_reports(),
    )
    md = render_markdown(bundle)
    assert "AI Test Toolkit — Run `r1`" in md
    assert "Gate Decisions" in md
    assert "step1" in md and "step4" in md and "step6" in md
    assert "warn_and_continue" in md
    assert "auto_pass" in md and "suggest_review" in md
    # Defect totals table
    assert "| 0 | 1 | 1 | 1 |" in md


def test_html_renderer_produces_html() -> None:
    bundle = aggregate(
        run_id="r1",
        project_id="p",
        tenant_id="t",
        step_reports=_fake_step_reports(),
    )
    html = render_html(bundle)
    assert "<html" in html.lower() or "<!doctype" in html.lower()
    assert "r1" in html


def test_aggregator_handles_missing_steps_gracefully() -> None:
    bundle = aggregate(
        run_id="r1",
        project_id="p",
        tenant_id="t",
        step_reports={},
    )
    assert bundle.gates() == []
    assert bundle.confidence() == []
    md = render_markdown(bundle)
    assert "r1" in md
