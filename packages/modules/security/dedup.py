"""Finding deduplication + LLM-assisted false-positive triage.

Two layers:
  1. Deterministic: same rule_id + path + line → one finding.
  2. Heuristic: semgrep rule family + trivy CVE overlap collapse via CWE.
LLM-based FP triage is optional and only invoked when an `LlmClient` is
supplied.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from packages.modules.security.scanners import RawFinding


@dataclass
class DedupedFinding:
    key: str
    rule_id: str
    severity: str
    title: str
    path: str
    line: int | None
    occurrences: int
    sources: list[str]
    cwe: list[str]
    extra: dict[str, Any]


def _fingerprint(rf: RawFinding) -> str:
    line_part = str(rf.line) if rf.line is not None else ""
    return f"{rf.rule_id}|{rf.path}|{line_part}"


def _merge_cwe(a: Any, b: Any) -> list[str]:
    out: list[str] = []
    for v in (a, b):
        if isinstance(v, list):
            out.extend(str(x) for x in v)
        elif isinstance(v, str) and v:
            out.append(v)
    return sorted(set(out))


def dedup(findings: list[RawFinding]) -> list[DedupedFinding]:
    buckets: dict[str, list[RawFinding]] = defaultdict(list)
    for f in findings:
        buckets[_fingerprint(f)].append(f)

    result: list[DedupedFinding] = []
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    for key, items in buckets.items():
        items.sort(key=lambda x: severity_rank.get(x.severity, 0), reverse=True)
        head = items[0]
        cwe: list[str] = []
        for it in items:
            cwe = _merge_cwe(cwe, it.extra.get("cwe"))
        result.append(
            DedupedFinding(
                key=key,
                rule_id=head.rule_id,
                severity=head.severity,
                title=head.title,
                path=head.path,
                line=head.line,
                occurrences=len(items),
                sources=sorted({it.source for it in items}),
                cwe=cwe,
                extra=dict(head.extra),
            )
        )
    result.sort(key=lambda d: severity_rank.get(d.severity, 0), reverse=True)
    return result


async def triage_false_positives(
    findings: list[DedupedFinding], *, llm: Any = None, max_items: int = 25
) -> list[DedupedFinding]:
    """Optional LLM pass. If llm is None, returns findings unchanged."""
    if llm is None or not findings:
        return findings
    head = findings[:max_items]
    prompt = (
        "以下是安全扫描结果。请对每条判断是否为误报（true_positive / false_positive / needs_context），"
        "并给出简短理由。输出 JSON：[{key, verdict, reason}]。"
    )
    import json as _json

    payload = [
        {
            "key": f.key,
            "rule_id": f.rule_id,
            "severity": f.severity,
            "title": f.title,
            "path": f.path,
            "line": f.line,
            "cwe": f.cwe,
        }
        for f in head
    ]
    messages = [{"role": "user", "content": prompt + "\n\n" + _json.dumps(payload, ensure_ascii=False)}]
    try:
        resp = await llm.complete(
            system="你是安全扫描结果审阅专家。严格输出 JSON，数组长度与输入一致。",
            messages=messages,
            max_tokens=4000,
            temperature=0.1,
        )
        verdicts = resp.json()
    except Exception:  # noqa: BLE001
        return findings

    verdict_by_key = {v["key"]: v for v in verdicts if isinstance(v, dict) and "key" in v}
    out: list[DedupedFinding] = []
    for f in findings:
        v = verdict_by_key.get(f.key)
        if v and v.get("verdict") == "false_positive":
            continue
        if v:
            f.extra["triage"] = v
        out.append(f)
    return out
