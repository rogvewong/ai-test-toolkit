"""Tool contract exposed to the Step 6 Agent.

Each entry is an Anthropic tool-use schema. Implementations live in the
ExecutionEnvironment below, which adapts to real httpx / playwright / adb
handlers supplied at runtime.
"""
from __future__ import annotations

from typing import Any

TOOL_CONTRACT: list[dict[str, Any]] = [
    {
        "name": "http_request",
        "description": "发送一次 HTTP 请求，用于接口型用例执行。严禁对生产环境执行破坏性方法。",
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "url": {"type": "string"},
                "headers": {"type": "object"},
                "query": {"type": "object"},
                "body": {},
                "timeout_ms": {"type": "integer", "default": 10000},
            },
            "required": ["method", "url"],
        },
    },
    {
        "name": "browser_action",
        "description": "通过 Playwright 操作浏览器。动作包括 goto / click / fill / wait_for / screenshot。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["goto", "click", "fill", "wait_for", "screenshot", "assert_text"],
                },
                "selector": {"type": "string"},
                "value": {"type": "string"},
                "url": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 5000},
            },
            "required": ["action"],
        },
    },
    {
        "name": "shell_readonly",
        "description": "只读 shell 指令（如 adb logcat / kubectl describe）。只允许白名单命令。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_s": {"type": "integer", "default": 30},
            },
            "required": ["command"],
        },
    },
    {
        "name": "record_result",
        "description": "把单条用例的执行结果（state / evidence / 归因提示）写入运行簿。",
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["passed", "failed", "blocked", "skipped"],
                },
                "steps": {"type": "array"},
                "attribution_hint": {"type": ["string", "null"]},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "logs_ref": {"type": ["string", "null"]},
            },
            "required": ["case_id", "state"],
        },
    },
]


SAFE_SHELL_PREFIXES = (
    "adb logcat",
    "adb shell dumpsys",
    "kubectl describe",
    "kubectl get",
    "git log",
    "git status",
    "curl -I",
)


def shell_is_allowed(cmd: str) -> bool:
    cmd = cmd.strip()
    return any(cmd.startswith(p) for p in SAFE_SHELL_PREFIXES)
