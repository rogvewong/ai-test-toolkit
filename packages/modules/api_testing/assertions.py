"""Assertion evaluator for API test cases.

Supported types:
  - status_code / status (eq, in, ne, lte, gte)
  - json_path (exists, eq, in, contains, regex)
  - header (eq, contains)
  - schema (jsonschema.validate)
  - response_time_ms (lte, gte)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

try:
    import jsonschema
except ImportError:  # jsonschema optional
    jsonschema = None  # type: ignore[assignment]


@dataclass
class AssertionResult:
    assertion: dict[str, Any]
    passed: bool
    actual: Any
    message: str | None = None


def _json_path(obj: Any, path: str) -> Any:
    """Minimal JSONPath ($ . notation with indexing) — not full JSONPath."""
    cur = obj
    if path.startswith("$"):
        path = path[1:]
    for part in [p for p in re.split(r"\.", path) if p]:
        m = re.match(r"([^\[\]]+)(?:\[(\d+)\])?$", part)
        if not m:
            return None
        key, idx = m.group(1), m.group(2)
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
        if idx is not None:
            if isinstance(cur, list) and int(idx) < len(cur):
                cur = cur[int(idx)]
            else:
                return None
    return cur


def _cmp(op: str, actual: Any, expected: Any) -> tuple[bool, str | None]:
    try:
        if op == "eq":
            return actual == expected, None
        if op == "ne":
            return actual != expected, None
        if op == "lte":
            return float(actual) <= float(expected), None
        if op == "gte":
            return float(actual) >= float(expected), None
        if op == "in":
            return actual in expected, None
        if op == "exists":
            return actual is not None, None
        if op == "contains":
            return str(expected) in str(actual), None
        if op == "regex":
            return bool(re.search(str(expected), str(actual))), None
    except (TypeError, ValueError) as exc:
        return False, str(exc)
    return False, f"unsupported op {op}"


def evaluate(
    assertion: dict[str, Any],
    *,
    status_code: int,
    headers: dict[str, str],
    body: Any,
    latency_ms: float,
) -> AssertionResult:
    kind = assertion.get("type") or assertion.get("kind", "status_code")
    op = assertion.get("op", "eq")
    expected = assertion.get("expected")

    if kind in {"status_code", "status"}:
        actual = status_code
    elif kind in {"json_path", "body_json"}:
        path = assertion.get("path") or assertion.get("expression", "$")
        actual = _json_path(body, path)
    elif kind == "header":
        name = assertion.get("name") or assertion.get("expression", "")
        actual = headers.get(name.lower()) or headers.get(name)
    elif kind == "response_time_ms" or kind == "latency":
        actual = latency_ms
    elif kind == "schema":
        if jsonschema is None:
            return AssertionResult(
                assertion=assertion,
                passed=False,
                actual=None,
                message="jsonschema not installed",
            )
        try:
            jsonschema.validate(instance=body, schema=expected)
            return AssertionResult(assertion=assertion, passed=True, actual=body)
        except jsonschema.ValidationError as exc:
            return AssertionResult(
                assertion=assertion, passed=False, actual=body, message=str(exc)
            )
    else:
        return AssertionResult(
            assertion=assertion,
            passed=False,
            actual=None,
            message=f"unknown assertion kind {kind}",
        )

    ok, msg = _cmp(op, actual, expected)
    return AssertionResult(assertion=assertion, passed=ok, actual=actual, message=msg)
