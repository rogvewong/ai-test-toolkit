"""httpx-based runner for API cases.

Handles:
  * per-case request (method, headers, query, body)
  * latency measurement
  * assertion batch
  * evidence persistence (request/response snapshot)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from packages.modules.api_testing.assertions import AssertionResult, evaluate


@dataclass
class RunResult:
    case_id: str
    endpoint_id: str
    category: str
    status_code: int
    latency_ms: float
    body: Any
    headers: dict[str, str]
    assertions: list[AssertionResult]
    passed: bool
    evidence_paths: list[str] = field(default_factory=list)
    error: str | None = None


def _build_url(base: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


async def run_case(
    client: httpx.AsyncClient,
    case: dict[str, Any],
    *,
    base_url: str,
    evidence_dir: Path,
) -> RunResult:
    req = case.get("request", {})
    method = req.get("method", "GET").upper()
    url = _build_url(base_url, req.get("path", "/"))
    headers = req.get("headers") or {}
    query = req.get("query") or {}
    body = req.get("body")

    start = time.monotonic()
    error: str | None = None
    status, resp_body, resp_headers = 0, None, {}
    try:
        resp = await client.request(
            method,
            url,
            headers=headers,
            params=query,
            json=body if isinstance(body, (dict, list)) else None,
            content=body if isinstance(body, (bytes, str)) else None,
            timeout=req.get("timeout_ms", 10000) / 1000.0,
        )
        status = resp.status_code
        resp_headers = dict(resp.headers)
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                resp_body = resp.json()
            except ValueError:
                resp_body = resp.text
        else:
            resp_body = resp.text
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = (time.monotonic() - start) * 1000

    assertions_out: list[AssertionResult] = []
    for a in case.get("assertions", []):
        assertions_out.append(
            evaluate(
                a,
                status_code=status,
                headers={k.lower(): v for k, v in resp_headers.items()},
                body=resp_body,
                latency_ms=latency_ms,
            )
        )

    passed = bool(assertions_out) and all(a.passed for a in assertions_out) and error is None

    evidence_dir.mkdir(parents=True, exist_ok=True)
    ev_path = evidence_dir / f"{case.get('case_id', 'case')}.json"
    ev_path.write_text(
        json.dumps(
            {
                "request": {"method": method, "url": url, "headers": headers, "body": body},
                "response": {
                    "status_code": status,
                    "headers": resp_headers,
                    "body": resp_body,
                    "latency_ms": latency_ms,
                },
                "error": error,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    return RunResult(
        case_id=case.get("case_id", "unknown"),
        endpoint_id=case.get("endpoint_id", "unknown"),
        category=case.get("category", "functional"),
        status_code=status,
        latency_ms=latency_ms,
        body=resp_body,
        headers=resp_headers,
        assertions=assertions_out,
        passed=passed,
        evidence_paths=[str(ev_path)],
        error=error,
    )


async def run_batch(
    cases: list[dict[str, Any]],
    *,
    base_url: str,
    evidence_dir: Path,
    default_headers: dict[str, str] | None = None,
    concurrency: int = 5,
) -> list[RunResult]:
    import asyncio

    semaphore = asyncio.Semaphore(concurrency)
    results: list[RunResult] = []
    async with httpx.AsyncClient(headers=default_headers or {}) as client:

        async def bound(c: dict[str, Any]) -> RunResult:
            async with semaphore:
                return await run_case(
                    client, c, base_url=base_url, evidence_dir=evidence_dir
                )

        results = await asyncio.gather(*(bound(c) for c in cases))
    return results
