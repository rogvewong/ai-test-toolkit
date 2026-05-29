"""Common enums and base types shared across all 6 report models.

Aligned with configs/standards/data.yaml.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ConfidenceGrade(str, Enum):
    AUTO_PASS = "auto_pass"
    SUGGEST_REVIEW = "suggest_review"
    MANDATORY_REVIEW = "mandatory_review"


class GateAction(str, Enum):
    PASS = "pass"
    WARN_AND_CONTINUE = "warn_and_continue"
    REJECT_WITH_REPORT = "reject_with_report"
    CONDITIONAL_PASS = "conditional_pass"


class ExecutionState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    FLAKY = "flaky"
    SKIPPED = "skipped"
    ERROR = "error"
    ABORTED = "aborted"


class DefectState(str, Enum):
    NEW = "new"
    TRIAGING = "triaging"
    CONFIRMED = "confirmed"
    FIXING = "fixing"
    FIXED = "fixed"
    VERIFIED = "verified"
    CLOSED = "closed"
    WONT_FIX = "wont_fix"
    REOPENED = "reopened"


def coerce_float(v: Any, default: float = 0.0) -> float:
    """Best-effort float coercion.

    LLM outputs frequently contain `null` for optional numeric fields. The
    usual ``dict.get(key, default)`` pattern does NOT protect against that —
    ``.get`` returns ``None`` when the key exists with value ``None``, and
    ``float(None)`` raises ``TypeError``. This helper handles None, invalid
    strings, and any other non-coercible value by returning ``default``.
    """
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def grade_confidence(score: float) -> ConfidenceGrade:
    if score >= 0.90:
        return ConfidenceGrade.AUTO_PASS
    if score >= 0.70:
        return ConfidenceGrade.SUGGEST_REVIEW
    return ConfidenceGrade.MANDATORY_REVIEW


def fingerprint(payload: Any) -> str:
    """Deterministic sha256 of arbitrary payload for idempotency/cache keys."""
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str, module: str, serial: int) -> str:
    """Generate ID in the form {PREFIX}-{MODULE}-{SERIAL:04d}. Module must be 3 chars."""
    if len(module) != 3:
        raise ValueError(f"module must be 3 chars, got {module!r}")
    return f"{prefix}-{module.upper()}-{serial:04d}"


class ReportMeta(BaseModel):
    """Metadata block required on every report (data.yaml: version_traceability)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=lambda: str(uuid4()))
    toolkit_version: str = "0.1.0"
    sop_version: str = "1.0.0"
    prompt_version: str = "1.0.0"
    model_id: str
    model_version: str | None = None
    input_fingerprint: str
    step_id: str
    produced_at_utc: datetime = Field(default_factory=utc_now)
    actor: str = "ai_agent"
    tenant_id: str | None = None
    project_id: str | None = None
    # 用户填写的项目编号 + 名称 — 报告 hero 区显示,/reports 列表也会读
    project_code: str | None = None
    project_name: str | None = None


class GateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: GateAction
    reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    next_step: str | None = None


class ConfidenceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0.0, le=1.0)
    grade: ConfidenceGrade
    signals: dict[str, float] = Field(default_factory=dict)
    rationale: str | None = None
