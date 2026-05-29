"""Shared fixtures for integration tests.

Integration tests exercise orchestrators end-to-end with a *stubbed* LLM so
that we get deterministic outputs without hitting the Anthropic API.
Network-dependent modules (httpx, semgrep, k6, playwright) are either stubbed
or skipped when their external dependencies are missing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import pytest

from packages.core.llm import LlmResponse, ModelTier, Usage
from packages.core.memory import LayeredMemory, SqliteMemoryStore
from packages.workflow.base import StepContext


@dataclass
class StubLlmClient:
    """Drop-in replacement for LlmClient.

    Call-based routing: the test provides either
      1. a list of responses (consumed in order), or
      2. a callable receiving (call_index, system, messages, tools) returning
         the response shape.

    A response can be:
      * a dict → serialised to JSON text with one text block
      * a tuple (text, content_blocks) → sent verbatim (useful for tool_use)
    """

    responses: list[Any] | Callable[..., Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self,
        *,
        system: Any,
        messages: list[dict[str, Any]],
        tier: ModelTier = ModelTier.SONNET,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        cacheable_system: bool = True,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        allow_degrade: bool = True,
    ) -> LlmResponse:
        idx = len(self.calls)
        self.calls.append(
            {
                "index": idx,
                "system": system if isinstance(system, str) else json.dumps(system),
                "messages": messages,
                "tools": tools,
                "tier": tier.value,
            }
        )
        if callable(self.responses):
            payload = self.responses(idx, system, messages, tools)
        else:
            if idx >= len(self.responses):
                raise AssertionError(
                    f"stub LLM exhausted after {len(self.responses)} responses "
                    f"(call #{idx})"
                )
            payload = self.responses[idx]

        if isinstance(payload, tuple):
            text, blocks = payload
        else:
            text = json.dumps(payload, ensure_ascii=False)
            blocks = [{"type": "text", "text": text}]

        return LlmResponse(
            text=text,
            model_id="stub-model",
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=10),
            raw={"usage": {}, "stop_reason": "end_turn", "content_blocks": blocks},
        )


@pytest.fixture
def tmp_memory(tmp_path: Path) -> LayeredMemory:
    store = SqliteMemoryStore(str(tmp_path / "mem.db"))
    return LayeredMemory(
        store=store,
        run_id=str(uuid4()),
        project_id="proj-int",
        tenant_id="tenant-int",
    )


@pytest.fixture
def make_ctx(tmp_path: Path, tmp_memory: LayeredMemory):
    """Build a StepContext backed by a stub LLM."""

    def _factory(
        responses: Any,
        *,
        inputs: dict[str, Any] | None = None,
    ) -> tuple[StepContext, StubLlmClient]:
        llm = StubLlmClient(responses=responses)
        ctx = StepContext(
            run_id=tmp_memory.run_id,
            project_id=tmp_memory.project_id,
            tenant_id=tmp_memory.tenant_id,
            inputs=inputs or {},
            memory=tmp_memory,
            llm=llm,  # type: ignore[arg-type]
            evidence_dir=tmp_path / "evidence",
        )
        return ctx, llm

    return _factory


@pytest.fixture
def passing_requirement_report() -> dict[str, Any]:
    """A minimal but complete Step 1 requirement report shape."""
    return {
        "modules": [
            {
                "id": "MOD-LOG-001",
                "name": "登录",
                "scope": "用户身份鉴权",
                "entry_points": ["/login"],
                "dependencies": [],
                "risk_level": "high",
            }
        ],
        "flows": [
            {
                "id": "FLOW-LOG-001",
                "name": "登录主流程",
                "module_id": "MOD-LOG-001",
                "steps": ["输入账号", "输入密码", "提交"],
            }
        ],
        "requirements": [
            {"id": "REQ-LOG-001", "text": "用户可使用手机号登录"},
            {"id": "REQ-LOG-002", "text": "用户可使用密码登录"},
        ],
    }
