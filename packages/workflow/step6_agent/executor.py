"""Execution environment that handles tool_use events from the Step 6 agent.

Real executors (httpx / playwright) can be injected via handlers. In dry-run
mode the environment returns deterministic stubs so unit tests pass without
any external service.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from packages.core.telemetry import get_logger
from packages.workflow.step6_agent.tool_contract import shell_is_allowed

logger = get_logger(__name__)


HttpHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
BrowserHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class CaseRecord:
    case_id: str
    state: str = "blocked"
    steps: list[dict[str, Any]] = field(default_factory=list)
    attribution_hint: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    logs_ref: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None


@dataclass
class ExecutionEnvironment:
    evidence_dir: Path
    http: HttpHandler | None = None
    browser: BrowserHandler | None = None
    shell_enabled: bool = False
    dry_run: bool = True
    records: dict[str, CaseRecord] = field(default_factory=dict)

    async def handle_tool(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        handler = {
            "http_request": self._http,
            "browser_action": self._browser,
            "shell_readonly": self._shell,
            "record_result": self._record,
        }.get(name)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            return await handler(params)
        except Exception as exc:  # noqa: BLE001 — surface as tool_result
            logger.exception("tool.error", tool=name)
            return {"error": str(exc)}

    async def _http(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.http is not None and not self.dry_run:
            return await self.http(params)
        await asyncio.sleep(0)
        return {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body": {"code": 0, "mock": True, "echo": params},
            "latency_ms": 42,
        }

    async def _browser(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.browser is not None and not self.dry_run:
            return await self.browser(params)
        await asyncio.sleep(0)
        if params.get("action") == "screenshot":
            snap = self.evidence_dir / f"snap_{int(time.time()*1000)}.png"
            snap.write_bytes(b"\x89PNG\r\n\x1a\n")
            return {"ok": True, "path": str(snap)}
        return {"ok": True, "mock": True, "action": params.get("action")}

    async def _shell(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.shell_enabled:
            return {"error": "shell disabled"}
        cmd = params.get("command", "")
        if not shell_is_allowed(cmd):
            return {"error": f"command not in safe list: {cmd!r}"}
        if self.dry_run:
            return {"stdout": "mock output", "returncode": 0}
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=params.get("timeout_s", 30)
            )
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": "timeout"}
        return {
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "returncode": proc.returncode,
        }

    async def _record(self, params: dict[str, Any]) -> dict[str, Any]:
        case_id = params["case_id"]
        rec = self.records.setdefault(case_id, CaseRecord(case_id=case_id))
        rec.state = params.get("state", rec.state)
        rec.steps = params.get("steps", rec.steps)
        rec.attribution_hint = params.get("attribution_hint", rec.attribution_hint)
        rec.evidence_refs = params.get("evidence_refs", rec.evidence_refs)
        rec.logs_ref = params.get("logs_ref", rec.logs_ref)
        rec.ended_at = time.time()
        # persist to disk
        out = self.evidence_dir / f"{case_id}.json"
        out.write_text(
            json.dumps(rec.__dict__, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return {"ok": True, "persisted": str(out)}

    def records_as_json(self) -> list[dict[str, Any]]:
        return [r.__dict__ for r in self.records.values()]
