"""接口测试执行器 — Step 4 主力。

Three-phase pattern:
  collect  → load OpenAPI / Postman + test case bundle
  process  → run cases with httpx, aggregate metrics
  analyze  → build findings per category, summarize
"""
from __future__ import annotations

from pathlib import Path
from statistics import mean
from typing import Any

from packages.core.models.common import Severity
from packages.modules.api_testing.openapi_loader import Endpoint, load_openapi
from packages.modules.api_testing.runner import RunResult, run_batch
from packages.modules.base import Evidence, Executor, ExecutorInput, ExecutorOutput, Finding


class ApiTestingExecutor(Executor):
    module_name = "api_testing"

    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        spec_path = inp.params.get("openapi_path")
        endpoints: list[Endpoint] = []
        if spec_path:
            endpoints = load_openapi(spec_path)
        cases: list[dict[str, Any]] = inp.params.get("cases", [])
        return {"endpoints": endpoints, "cases": cases}

    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        cases = raw.get("cases") or []
        if not cases:
            return {"results": [], "metrics": {}, "endpoints": raw.get("endpoints", [])}
        evidence_dir = Path(raw.get("evidence_dir", "evidence/api"))
        base_url = raw.get("base_url", "")
        results = await run_batch(
            cases,
            base_url=base_url,
            evidence_dir=evidence_dir,
            default_headers=raw.get("default_headers"),
            concurrency=raw.get("concurrency", 5),
        )
        latencies = [r.latency_ms for r in results if r.latency_ms > 0] or [0.0]
        passed = sum(1 for r in results if r.passed)
        return {
            "results": results,
            "endpoints": raw.get("endpoints", []),
            "metrics": {
                "total": len(results),
                "passed": passed,
                "failed": len(results) - passed,
                "pass_rate": passed / max(len(results), 1),
                "avg_latency": mean(latencies),
                "p95_latency": sorted(latencies)[int(len(latencies) * 0.95) - 1]
                if latencies
                else 0.0,
            },
        }

    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        for r in processed.get("results", []):  # type: RunResult
            for path in r.evidence_paths:
                evidence.append(Evidence(kind="har", path=path))
            if r.passed:
                continue
            failed_asserts = [a for a in r.assertions if not a.passed]
            msg = "; ".join(
                f"{a.assertion.get('type', '?')}/{a.assertion.get('op', '?')} actual={a.actual}"
                for a in failed_asserts
            )
            severity = (
                Severity.CRITICAL
                if r.category == "security"
                else Severity.HIGH
                if r.category == "functional"
                else Severity.MEDIUM
            )
            findings.append(
                Finding(
                    id=f"FND-API-{r.case_id}",
                    title=f"{r.endpoint_id} 断言失败",
                    severity=severity,
                    evidence=[
                        Evidence(kind="har", path=p) for p in r.evidence_paths
                    ],
                    suggested_action=msg or (r.error or "检查响应状态"),
                )
            )
        metrics = {k: float(v) for k, v in processed.get("metrics", {}).items()}
        return ExecutorOutput(
            module=self.module_name,
            metrics=metrics,
            findings=findings,
            evidence=evidence,
            ai_summary=None,
            confidence=None,
        )
