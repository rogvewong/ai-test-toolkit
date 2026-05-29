"""Scanner wrappers for semgrep / trivy / bandit.

Each wrapper returns a normalized list of findings, regardless of source.
Missing binaries produce an empty list (so the pipeline still succeeds).
"""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RawFinding:
    rule_id: str
    severity: str  # info | low | medium | high | critical
    title: str
    path: str
    line: int | None = None
    source: str = "unknown"  # semgrep | trivy | bandit | custom
    extra: dict[str, Any] = field(default_factory=dict)


async def _run(cmd: list[str], cwd: str | Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def run_semgrep(target: str | Path, *, rules: str = "auto") -> list[RawFinding]:
    if shutil.which("semgrep") is None:
        return []
    code, stdout, _ = await _run(
        ["semgrep", "--config", rules, "--json", str(target)], cwd=target
    )
    if code not in (0, 1):
        return []
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    out: list[RawFinding] = []
    for r in data.get("results", []):
        meta = r.get("extra", {}).get("metadata", {})
        sev = (
            r.get("extra", {}).get("severity", "INFO").lower()
            or meta.get("severity", "info")
        )
        out.append(
            RawFinding(
                rule_id=r.get("check_id", "unknown"),
                severity=_map_severity(sev),
                title=r.get("extra", {}).get("message", r.get("check_id", "")),
                path=r.get("path", ""),
                line=r.get("start", {}).get("line"),
                source="semgrep",
                extra={
                    "cwe": meta.get("cwe"),
                    "owasp": meta.get("owasp"),
                    "message": r.get("extra", {}).get("message"),
                },
            )
        )
    return out


async def run_trivy(target: str | Path) -> list[RawFinding]:
    if shutil.which("trivy") is None:
        return []
    code, stdout, _ = await _run(
        ["trivy", "fs", "--quiet", "--format", "json", str(target)], cwd=target
    )
    if code not in (0, 1):
        return []
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    out: list[RawFinding] = []
    for res in data.get("Results", []) or []:
        for v in res.get("Vulnerabilities", []) or []:
            out.append(
                RawFinding(
                    rule_id=v.get("VulnerabilityID", "unknown"),
                    severity=_map_severity(v.get("Severity", "UNKNOWN").lower()),
                    title=v.get("Title") or v.get("Description", ""),
                    path=res.get("Target", ""),
                    line=None,
                    source="trivy",
                    extra={
                        "pkg": v.get("PkgName"),
                        "installed": v.get("InstalledVersion"),
                        "fixed": v.get("FixedVersion"),
                        "cwe": v.get("CweIDs"),
                    },
                )
            )
    return out


async def run_bandit(target: str | Path) -> list[RawFinding]:
    if shutil.which("bandit") is None:
        return []
    code, stdout, _ = await _run(
        ["bandit", "-q", "-r", "-f", "json", str(target)], cwd=target
    )
    if code not in (0, 1):
        return []
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []
    out: list[RawFinding] = []
    for r in data.get("results", []):
        out.append(
            RawFinding(
                rule_id=r.get("test_id", "bandit"),
                severity=_map_severity(r.get("issue_severity", "LOW").lower()),
                title=r.get("issue_text", ""),
                path=r.get("filename", ""),
                line=r.get("line_number"),
                source="bandit",
                extra={"confidence": r.get("issue_confidence")},
            )
        )
    return out


def _map_severity(raw: str) -> str:
    raw = (raw or "").lower()
    return {
        "critical": "critical",
        "high": "high",
        "error": "high",
        "warning": "medium",
        "medium": "medium",
        "moderate": "medium",
        "low": "low",
        "info": "low",
        "unknown": "low",
    }.get(raw, "low")
