"""安全测试执行器（SAST/DAST/SCA/Secrets/OWASP）.

Pipeline:
  collect  → fan out to semgrep / trivy / bandit
  process  → dedup + LLM-assisted false-positive triage
  analyze  → convert to Findings with severity mapping
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from packages.core.models.common import Severity
from packages.modules.base import Evidence, Executor, ExecutorInput, ExecutorOutput, Finding
from packages.modules.security.dedup import DedupedFinding, dedup, triage_false_positives
from packages.modules.security.scanners import run_bandit, run_semgrep, run_trivy


class SecurityExecutor(Executor):
    module_name = "security"

    async def collect(self, inp: ExecutorInput) -> dict[str, Any]:
        target = Path(inp.params.get("target_path", "."))
        tasks = []
        if inp.params.get("enable_semgrep", True):
            tasks.append(run_semgrep(target, rules=inp.params.get("semgrep_rules", "auto")))
        if inp.params.get("enable_trivy", True):
            tasks.append(run_trivy(target))
        if inp.params.get("enable_bandit", True):
            tasks.append(run_bandit(target))
        findings = []
        for batch in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(batch, Exception):
                continue
            findings.extend(batch)
        return {"findings": findings, "target": str(target)}

    async def process(self, raw: dict[str, Any]) -> dict[str, Any]:
        deduped = dedup(raw.get("findings") or [])
        triaged = await triage_false_positives(deduped, llm=raw.get("llm"))
        return {"findings": triaged, "target": raw.get("target")}

    async def analyze(
        self, processed: dict[str, Any], inp: ExecutorInput
    ) -> ExecutorOutput:
        findings_out: list[Finding] = []
        evidence_out: list[Evidence] = []
        metrics: dict[str, float] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        sev_map = {
            "critical": Severity.CRITICAL,
            "high": Severity.HIGH,
            "medium": Severity.MEDIUM,
            "low": Severity.LOW,
        }
        for d in processed.get("findings", []):  # type: DedupedFinding
            metrics[d.severity] = metrics.get(d.severity, 0) + 1
            findings_out.append(
                Finding(
                    id=f"SEC-{d.rule_id}",
                    title=d.title or d.rule_id,
                    severity=sev_map.get(d.severity, Severity.LOW),
                    evidence=[Evidence(kind="log", path=d.path, description=str(d.line))],
                    suggested_action=", ".join(d.cwe) if d.cwe else None,
                )
            )
        return ExecutorOutput(
            module=self.module_name,
            metrics={k: float(v) for k, v in metrics.items()},
            findings=findings_out,
            evidence=evidence_out,
        )
