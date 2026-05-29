"""服务端性能测试执行器 — k6-driven。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from packages.core.models.common import Severity
from packages.modules.base import Evidence, Executor, ExecutorInput, ExecutorOutput, Finding
from packages.modules.server_perf.k6_runner import extract_core_metrics, render_script, run_k6


class ServerPerfExecutor(Executor):
    module_name = "server_perf"

    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        url = inp.params["url"]
        script = inp.params.get("script") or render_script(
            url=url,
            method=inp.params.get("method", "GET"),
            headers=inp.params.get("headers"),
            body=inp.params.get("body"),
            stages=inp.params.get("stages"),
            thresholds=inp.params.get("thresholds"),
        )
        evidence_dir = Path(inp.params.get("evidence_dir", "evidence/perf"))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        summary_out = evidence_dir / f"{inp.run_id}_summary.json"
        result = await run_k6(script, summary_out=summary_out)
        return {"result": result, "evidence_dir": evidence_dir}

    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        result = raw["result"]
        metrics = extract_core_metrics(result.summary)
        return {"metrics": metrics, "result": result}

    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        metrics = processed["metrics"]
        result = processed["result"]
        findings: list[Finding] = []
        target_p95 = float(inp.params.get("target_p95_ms", 500))
        target_fail_rate = float(inp.params.get("target_fail_rate", 0.01))

        if metrics.get("p95_ms", 0) > target_p95:
            findings.append(
                Finding(
                    id="PERF-P95",
                    title=f"P95 延迟 {metrics['p95_ms']:.0f}ms 超过目标 {target_p95:.0f}ms",
                    severity=Severity.HIGH,
                    evidence=[Evidence(kind="metric", path=str(result.summary_path))]
                    if result.summary_path
                    else [],
                    suggested_action="定位慢接口 / 数据库慢查询 / 下游依赖",
                )
            )
        if metrics.get("failed_rate", 0) > target_fail_rate:
            findings.append(
                Finding(
                    id="PERF-FAILRATE",
                    title=f"失败率 {metrics['failed_rate']:.2%} 超过 {target_fail_rate:.2%}",
                    severity=Severity.CRITICAL,
                    evidence=[Evidence(kind="metric", path=str(result.summary_path))]
                    if result.summary_path
                    else [],
                    suggested_action="检查错误日志与下游服务状态",
                )
            )
        evidence: list[Evidence] = []
        if result.summary_path:
            evidence.append(Evidence(kind="metric", path=str(result.summary_path)))
        return ExecutorOutput(
            module=self.module_name,
            metrics=metrics,
            findings=findings,
            evidence=evidence,
        )
