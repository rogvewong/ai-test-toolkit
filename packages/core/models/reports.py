"""Pydantic models for the 6 unified reports (data.yaml)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from packages.core.models.common import (
    ConfidenceBlock,
    DefectState,
    ExecutionState,
    GateDecision,
    Priority,
    ReportMeta,
    Severity,
)


# =====================================================================
# Step 1 — Requirement Breakdown Report (需求拆解报告)
# =====================================================================

class Material(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: str  # prd | flow_chart | design_doc | api_doc
    source_path: str | None = None
    completeness_score: float = Field(ge=0, le=1)
    missing_items: list[str] = Field(default_factory=list)


class Module(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    scope: str
    entry_points: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    risk_level: Severity = Severity.MEDIUM


class Flow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    module_id: str
    steps: list[str]
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    interaction_points: list[str] = Field(default_factory=list)


class Ambiguity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    location: str
    description: str
    impact: Severity
    suggested_question: str
    blocker: bool = False


class RequirementBreakdownReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: ReportMeta
    materials: list[Material]
    modules: list[Module]
    flows: list[Flow]
    ambiguities: list[Ambiguity]
    gate_decision: GateDecision
    confidence: ConfidenceBlock


# =====================================================================
# Step 2 — Test Case Report (测试用例报告)
# =====================================================================

class CoverageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: str
    flow_id: str | None = None
    case_ids: list[str]
    coverage_type: str  # positive | negative | boundary | exception | security


class TestStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: int
    action: str
    data: dict[str, object] | None = None
    expected: str


class TestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    priority: Priority
    module_id: str
    flow_id: str | None = None
    preconditions: list[str] = Field(default_factory=list)
    steps: list[TestStep]
    expected_result: str
    automation_tag: str  # auto | semi_auto | manual
    tools: list[str] = Field(default_factory=list)
    requirements_covered: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TestCaseReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: ReportMeta
    coverage_map: list[CoverageEntry]
    p0_cases: list[TestCase]
    p1_cases: list[TestCase]
    p2_cases: list[TestCase]
    automation_summary: dict[str, int]  # {"auto": N, "semi_auto": N, "manual": N}
    gate_decision: GateDecision
    confidence: ConfidenceBlock


# =====================================================================
# Step 4 — API Test Report (接口测试报告)
# =====================================================================

class ApiEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    method: str
    path: str
    description: str | None = None
    auth_required: bool = True
    schema_ref: str | None = None


class Assertion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str  # status | body_json | header | latency | schema
    expression: str
    expected: object


class ApiTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    api_id: str
    category: str  # functional | security | permission | boundary
    request_snapshot: dict[str, object]
    response_snapshot: dict[str, object]
    assertions: list[Assertion]
    state: ExecutionState
    latency_ms: float
    evidence_paths: list[str] = Field(default_factory=list)
    defect_ids: list[str] = Field(default_factory=list)


class Defect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    severity: Severity
    state: DefectState = DefectState.NEW
    reproduction_steps: list[str]
    actual_result: str
    expected_result: str
    evidence_paths: list[str] = Field(default_factory=list)
    related_case_ids: list[str] = Field(default_factory=list)
    related_api_ids: list[str] = Field(default_factory=list)


class ApiTestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: ReportMeta
    api_list: list[ApiEndpoint]
    functional_results: list[ApiTestResult]
    security_results: list[ApiTestResult]
    boundary_results: list[ApiTestResult]
    defects: list[Defect]
    metrics: dict[str, float]  # pass_rate, avg_latency, p95_latency, etc.
    gate_decision: GateDecision
    confidence: ConfidenceBlock


# =====================================================================
# Step 5 — UI Consistency Report (UI一致性报告)
# =====================================================================

class UiReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: str  # figma | zeplin | design_doc
    screen_name: str
    version: str
    url_or_path: str


class UiDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    reference_id: str
    capture_path: str
    kind: str  # layout | color | typography | copy | icon | interaction
    delta_pct: float = Field(ge=0, le=1)
    severity: Severity
    evidence_paths: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None


class UiConsistencyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: ReportMeta
    references: list[UiReference]
    diffs: list[UiDiff]
    overall_deviation_ratio: float = Field(ge=0, le=1)
    severity_map: dict[str, int]  # {"critical": N, "high": N, ...}
    gate_decision: GateDecision
    confidence: ConfidenceBlock


# =====================================================================
# Step 6 — Execution Report (Agent执行报告)
# =====================================================================

class PreCheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    passed: bool
    details: str | None = None


class ExecutionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    case_id: str
    action: str
    state: ExecutionState
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: float | None = None
    evidence_paths: list[str] = Field(default_factory=list)
    error: str | None = None


class RootCauseAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failure_id: str
    hypothesis: str
    supporting_signals: list[str]
    confidence: float = Field(ge=0, le=1)
    suggested_action: str


class ExecutionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: ReportMeta
    pre_check: list[PreCheckItem]
    plan_digest: str
    steps: list[ExecutionStep]
    failures: list[ExecutionStep]
    rca: list[RootCauseAnalysis]
    metrics: dict[str, float]  # success_rate, avg_case_seconds, etc.
    gate_decision: GateDecision
    confidence: ConfidenceBlock


# =====================================================================
# TDR Review Report (技术评审报告)
# =====================================================================

class ReviewComment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    dimension: str  # completeness | correctness | feasibility | risk_awareness | traceability
    severity: str  # blocker | major | minor | suggestion
    location: str
    observation: str
    suggestion: str
    requires_response: bool
    resolved: bool = False
    resolution_note: str | None = None


class Signature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signer_role: str  # author | reviewer | moderator
    signer_id: str
    signed_at: datetime
    artifact_hash: str
    signature: str  # ed25519


class TdrReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: ReportMeta
    artifacts_reviewed: list[str]
    dimension_scores: dict[str, float]  # per-dimension 0..1
    overall_score: float = Field(ge=0, le=1)
    comments: list[ReviewComment]
    decision: str  # pass | conditional_pass | reject
    follow_up_items: list[str] = Field(default_factory=list)
    signatures: list[Signature]
