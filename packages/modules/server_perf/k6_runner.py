"""k6 subprocess wrapper + JSON summary parser."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

K6_SCRIPT_TEMPLATE = """
import http from 'k6/http';
import {{ check, sleep }} from 'k6';
export const options = {{
  stages: {stages_json},
  thresholds: {thresholds_json},
}};
const URL = {url_json};
const METHOD = {method_json};
const HEADERS = {headers_json};
const BODY = {body_json};
export default function () {{
  const params = {{ headers: HEADERS }};
  let resp;
  if (METHOD === 'GET') {{
    resp = http.get(URL, params);
  }} else {{
    resp = http.request(METHOD, URL, BODY ? JSON.stringify(BODY) : null, params);
  }}
  check(resp, {{ 'status is 2xx': (r) => r.status >= 200 && r.status < 300 }});
  sleep(1);
}}
"""


@dataclass
class K6Result:
    exit_code: int
    summary: dict[str, Any]
    raw_stdout: str
    raw_stderr: str
    summary_path: Path | None


def render_script(
    *,
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any = None,
    stages: list[dict[str, Any]] | None = None,
    thresholds: dict[str, list[str]] | None = None,
) -> str:
    return K6_SCRIPT_TEMPLATE.format(
        url_json=json.dumps(url),
        method_json=json.dumps(method.upper()),
        headers_json=json.dumps(headers or {}),
        body_json=json.dumps(body),
        stages_json=json.dumps(
            stages
            or [
                {"duration": "30s", "target": 10},
                {"duration": "1m", "target": 50},
                {"duration": "30s", "target": 0},
            ]
        ),
        thresholds_json=json.dumps(
            thresholds
            or {
                "http_req_duration": ["p(95)<500"],
                "http_req_failed": ["rate<0.01"],
            }
        ),
    )


async def run_k6(script: str, *, summary_out: Path | None = None) -> K6Result:
    if shutil.which("k6") is None:
        return K6Result(
            exit_code=-1,
            summary={"error": "k6 not installed"},
            raw_stdout="",
            raw_stderr="",
            summary_path=None,
        )
    tmp = Path(tempfile.mkdtemp(prefix="k6_"))
    script_path = tmp / "script.js"
    script_path.write_text(script, encoding="utf-8")
    summary_path = summary_out or (tmp / "summary.json")
    cmd = ["k6", "run", "--summary-export", str(summary_path), str(script_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    summary: dict[str, Any] = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = {"error": "malformed summary"}
    return K6Result(
        exit_code=proc.returncode or 0,
        summary=summary,
        raw_stdout=stdout_b.decode(errors="replace"),
        raw_stderr=stderr_b.decode(errors="replace"),
        summary_path=summary_path if summary_path.exists() else None,
    )


def extract_core_metrics(summary: dict[str, Any]) -> dict[str, float]:
    """Pick the metrics the orchestrator cares about."""
    metrics = summary.get("metrics", {})

    def m(name: str, key: str) -> float:
        return float((metrics.get(name) or {}).get(key, 0.0))

    return {
        "requests_total": m("http_reqs", "count"),
        "rps": m("http_reqs", "rate"),
        "failed_rate": m("http_req_failed", "rate"),
        "avg_ms": m("http_req_duration", "avg"),
        "p95_ms": m("http_req_duration", "p(95)") or m("http_req_duration", "p95"),
        "p99_ms": m("http_req_duration", "p(99)") or m("http_req_duration", "p99"),
        "max_ms": m("http_req_duration", "max"),
    }
