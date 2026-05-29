"""Integration tests for the specialized testing modules.

Modules that shell out to external tools (semgrep, k6, playwright) are
exercised in their no-binary fallback path — verifying they return an empty
or stub result rather than crashing the pipeline.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from packages.modules.api_testing.assertions import evaluate
from packages.modules.api_testing.openapi_loader import load_openapi
from packages.modules.api_testing.runner import run_case
from packages.modules.security.dedup import dedup
from packages.modules.security.scanners import RawFinding
from packages.modules.server_perf.k6_runner import (
    extract_core_metrics,
    render_script,
    run_k6,
)
from packages.modules.ui_testing.diff import compute_diff


# =====================================================================
# api_testing
# =====================================================================


def test_assertion_status_code_eq() -> None:
    r = evaluate(
        {"type": "status_code", "op": "eq", "expected": 200},
        status_code=200,
        headers={},
        body=None,
        latency_ms=0,
    )
    assert r.passed


def test_assertion_json_path_eq() -> None:
    r = evaluate(
        {"type": "json_path", "path": "$.user.id", "op": "eq", "expected": 42},
        status_code=200,
        headers={},
        body={"user": {"id": 42}},
        latency_ms=0,
    )
    assert r.passed


def test_assertion_latency_lte() -> None:
    r = evaluate(
        {"type": "response_time_ms", "op": "lte", "expected": 500},
        status_code=200,
        headers={},
        body=None,
        latency_ms=120,
    )
    assert r.passed


def test_assertion_header_contains() -> None:
    r = evaluate(
        {
            "type": "header",
            "name": "content-type",
            "op": "contains",
            "expected": "json",
        },
        status_code=200,
        headers={"content-type": "application/json"},
        body=None,
        latency_ms=0,
    )
    assert r.passed


@pytest.mark.asyncio
async def test_api_runner_against_mock_transport(tmp_path: Path) -> None:
    """Use httpx.MockTransport so we don't need a real server."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"token": "abc"}},
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(handler)
    case = {
        "case_id": "AC-LOG-0001",
        "endpoint_id": "EP-LOG-0001",
        "category": "functional",
        "request": {"method": "POST", "path": "/api/login", "body": {"u": "a"}},
        "assertions": [
            {"type": "status_code", "op": "eq", "expected": 200},
            {"type": "json_path", "path": "$.data.token", "op": "eq", "expected": "abc"},
        ],
    }

    async with httpx.AsyncClient(transport=transport) as client:
        result = await run_case(
            client, case, base_url="http://local", evidence_dir=tmp_path / "api"
        )

    assert result.passed
    assert result.status_code == 200
    assert calls == ["/api/login"]
    # Evidence file persisted with request & response snapshot
    assert result.evidence_paths
    ev = Path(result.evidence_paths[0])
    assert ev.exists()
    snap = json.loads(ev.read_text("utf-8"))
    assert snap["request"]["url"].endswith("/api/login")


@pytest.mark.asyncio
async def test_api_runner_marks_failure_on_assertion_mismatch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_case(
            client,
            {
                "case_id": "AC-ERR",
                "endpoint_id": "EP-ERR",
                "category": "functional",
                "request": {"method": "GET", "path": "/broken"},
                "assertions": [{"type": "status_code", "op": "eq", "expected": 200}],
            },
            base_url="http://local",
            evidence_dir=tmp_path / "api",
        )
    assert not result.passed
    assert result.status_code == 500


def test_openapi_loader_reads_minimal_spec(tmp_path: Path) -> None:
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/users": {
                "get": {"summary": "list users"},
                "post": {"summary": "create user"},
            }
        },
    }
    p = tmp_path / "openapi.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    endpoints = load_openapi(p)
    assert {(e.method, e.path) for e in endpoints} == {
        ("GET", "/users"),
        ("POST", "/users"),
    }


def test_openapi_loader_reads_postman_collection(tmp_path: Path) -> None:
    coll = {
        "info": {"schema": "https://schema.getpostman.com/json/collection/v2.1.0"},
        "item": [
            {
                "name": "login",
                "request": {
                    "method": "POST",
                    "url": {"path": ["api", "login"]},
                    "auth": {"type": "bearer"},
                },
            }
        ],
    }
    p = tmp_path / "coll.json"
    p.write_text(json.dumps(coll), encoding="utf-8")
    endpoints = load_openapi(p)
    assert len(endpoints) == 1
    assert endpoints[0].method == "POST"
    assert endpoints[0].path == "/api/login"
    assert endpoints[0].auth_required


# =====================================================================
# ui_testing — pixel diff
# =====================================================================


def test_pixel_diff_identical_bytes_is_zero(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    # Use Pillow if present; otherwise fall back to raw-bytes equality
    try:
        from PIL import Image  # type: ignore[import-untyped]

        Image.new("RGB", (10, 10), (255, 0, 0)).save(a)
        Image.new("RGB", (10, 10), (255, 0, 0)).save(b)
    except ImportError:
        a.write_bytes(b"identical")
        b.write_bytes(b"identical")

    diff = compute_diff(a, b)
    assert diff.diff_pct == 0.0


def test_pixel_diff_different_bytes_is_positive(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    try:
        from PIL import Image  # type: ignore[import-untyped]

        Image.new("RGB", (10, 10), (255, 0, 0)).save(a)
        Image.new("RGB", (10, 10), (0, 255, 0)).save(b)
    except ImportError:
        a.write_bytes(b"one")
        b.write_bytes(b"two")

    diff = compute_diff(a, b)
    assert diff.diff_pct > 0.0


# =====================================================================
# security — dedup
# =====================================================================


def test_security_dedup_collapses_duplicate_rule_file_line() -> None:
    findings = [
        RawFinding(
            rule_id="R1",
            severity="high",
            title="sqli",
            path="app/db.py",
            line=42,
            source="semgrep",
            extra={"cwe": "CWE-89"},
        ),
        RawFinding(
            rule_id="R1",
            severity="high",
            title="sqli",
            path="app/db.py",
            line=42,
            source="bandit",
            extra={"cwe": ["CWE-89"]},
        ),
        RawFinding(
            rule_id="R2",
            severity="medium",
            title="weak hash",
            path="app/auth.py",
            line=9,
            source="semgrep",
        ),
    ]
    deduped = dedup(findings)
    assert len(deduped) == 2
    top = deduped[0]  # sorted by severity desc
    assert top.rule_id == "R1"
    assert top.occurrences == 2
    assert set(top.sources) == {"semgrep", "bandit"}
    assert "CWE-89" in top.cwe


def test_security_dedup_preserves_severity_order() -> None:
    findings = [
        RawFinding(rule_id="L", severity="low", title="", path="a.py"),
        RawFinding(rule_id="C", severity="critical", title="", path="a.py"),
        RawFinding(rule_id="M", severity="medium", title="", path="a.py"),
    ]
    deduped = dedup(findings)
    assert [d.rule_id for d in deduped] == ["C", "M", "L"]


# =====================================================================
# server_perf — k6 wrapper
# =====================================================================


def test_k6_render_script_includes_url_and_thresholds() -> None:
    script = render_script(
        url="http://api.local/ping",
        method="GET",
        stages=[{"duration": "10s", "target": 5}],
        thresholds={"http_req_duration": ["p(95)<300"]},
    )
    assert "http://api.local/ping" in script
    assert "p(95)<300" in script
    assert "10s" in script


def test_k6_extract_core_metrics_picks_known_fields() -> None:
    summary = {
        "metrics": {
            "http_reqs": {"count": 1000, "rate": 20.0},
            "http_req_duration": {
                "avg": 55.0,
                "p(95)": 200.0,
                "p(99)": 400.0,
                "max": 800.0,
            },
            "http_req_failed": {"rate": 0.001},
        }
    }
    m = extract_core_metrics(summary)
    assert m["requests_total"] == 1000
    assert m["rps"] == 20.0
    assert m["p95_ms"] == 200.0
    assert m["p99_ms"] == 400.0
    assert m["failed_rate"] == 0.001


@pytest.mark.asyncio
async def test_k6_runner_gracefully_no_ops_when_binary_missing(tmp_path: Path) -> None:
    """Without k6 installed the runner returns a shaped error without crashing."""
    import shutil

    if shutil.which("k6"):
        pytest.skip("k6 is installed — skipping missing-binary fallback test")
    result = await run_k6(render_script(url="http://localhost/"))
    assert result.exit_code == -1
    assert "error" in result.summary
