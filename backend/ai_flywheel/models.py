from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuleStatus(str, Enum):
    UNVERIFIED_ACTIVE = "unverified_active"
    VERIFIED_ACTIVE = "verified_active"
    NEEDS_REVISION = "needs_revision"
    RETIRED = "retired"


class VerificationKind(str, Enum):
    REAL = "real"
    TEST = "test"
    STATIC = "static"
    UNKNOWN = "unknown"


class CheckResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class OutcomeStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class OutcomeOrigin(str, Enum):
    NATURAL = "natural"
    REPLAY = "replay"
    SYNTHETIC = "synthetic"


class NodeStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class SessionStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PROCESSED = "processed"
    ARCHIVED = "archived"


class SignalKind(str, Enum):
    RETRY_LOOP = "retry_loop"
    HIGH_RISK_TOOL = "high_risk_tool"
    MISSING_EVIDENCE = "missing_evidence"
    RULE_RECURRENCE = "rule_recurrence"
    BLOCKER = "blocker"
    CUSTOM = "custom"


class RetroStatus(str, Enum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class RegisterSessionRequest(BaseModel):
    client: str = Field(..., description="AI client identifier, e.g. codex, claude-code, claude, cursor")
    session_id: str = Field(..., description="Client-side session/thread ID")
    title: str = ""
    project: str = "generic"
    workspace: str = ""
    transcript_path: str | None = None
    summary: str | None = None
    content: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionRecord(BaseModel):
    session_key: str
    client: str
    session_id: str
    title: str = ""
    project: str = "generic"
    workspace: str = ""
    transcript_path: str | None = None
    summary: str | None = None
    content: str | None = None
    tags: list[str] = Field(default_factory=list)
    priority: str = "P1_GENERAL"
    status: SessionStatus = SessionStatus.PENDING
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class PreflightRequest(BaseModel):
    task: str = Field(..., description="Task prompt or summary")
    project: str = "generic"
    client: str = "generic"
    session_id: str = ""
    turn_id: str = ""
    mode: str | None = None  # auto | baseline | execution


class RuleRecord(BaseModel):
    rule_id: str
    title: str
    domain: str = "general"
    projects: list[str] = Field(default_factory=lambda: ["generic"])
    triggers: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    forbidden_routes: list[str] = Field(default_factory=list)
    applicable_skills: list[str] = Field(default_factory=list)
    source_pointers: list[str] = Field(default_factory=list)
    status: RuleStatus = RuleStatus.UNVERIFIED_ACTIVE
    version: int = 1
    origin_session_key: str | None = None
    hit_count: int = 0
    natural_pass_count: int = 0
    recurrence_count: int = 0
    revalidation_history: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class CreateRuleRequest(BaseModel):
    rule_id: str | None = None
    title: str
    domain: str = "general"
    projects: list[str] = Field(default_factory=lambda: ["generic"])
    triggers: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    forbidden_routes: list[str] = Field(default_factory=list)
    applicable_skills: list[str] = Field(default_factory=list)
    source_pointers: list[str] = Field(default_factory=list)
    origin_session_key: str | None = None


class PreflightResponse(BaseModel):
    receipt_id: str
    receipt_hash: str
    anti_laziness_mode: str
    policy_card: str
    user_preferences_card: str
    matched_rules: list[RuleRecord] = Field(default_factory=list)
    suggested_skills: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_hits: list[dict[str, Any]] = Field(default_factory=list)
    compact_context: str
    created_at: str = Field(default_factory=utc_now_iso)


class ReportSignalRequest(BaseModel):
    session_key: str = ""
    client: str = "generic"
    session_id: str = ""
    turn_id: str = ""
    kind: SignalKind
    severity: str = "warning"  # info | warning | high
    summary: str
    tool_name: str | None = None
    rule_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class RetroItem(BaseModel):
    retro_id: str
    session_key: str
    client: str
    kind: SignalKind
    severity: str
    summary: str
    tool_name: str | None = None
    rule_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    status: RetroStatus = RetroStatus.OPEN
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class OutcomeCheck(BaseModel):
    name: str
    result: CheckResult
    kind: VerificationKind
    required: bool = True
    evidence: list[str] = Field(default_factory=list)
    rule_id: str | None = None
    note: str = ""


class RecordOutcomeRequest(BaseModel):
    session_key: str = ""
    client: str = "generic"
    session_id: str = ""
    turn_id: str = ""
    preflight_receipt_id: str | None = None
    origin: OutcomeOrigin = OutcomeOrigin.NATURAL
    status: OutcomeStatus
    summary: str = ""
    checks: list[OutcomeCheck] = Field(default_factory=list)
    applied_rule_ids: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    # D6 (2026-11-23): Wiki knowledge cards actually re-read and helpful in this task.
    used_knowledge_paths: list[str] = Field(default_factory=list)


class OutcomeRecord(BaseModel):
    outcome_id: str
    receipt_hash: str
    session_key: str
    client: str
    preflight_receipt_id: str | None = None
    origin: OutcomeOrigin
    status: OutcomeStatus
    summary: str
    checks: list[OutcomeCheck]
    applied_rule_ids: list[str]
    artifacts: list[str]
    used_knowledge_paths: list[str] = Field(default_factory=list)
    revalidation_effects: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


class PlannedWrite(BaseModel):
    target_kind: str  # knowledge_doc | flywheel_rule | skill_improvement
    relative_path: str | None = None
    action: str  # create | update | register_rule
    title: str
    summary: str
    content_preview: str = ""
    rule_payload: dict[str, Any] | None = None
    skill_route: dict[str, Any] | None = None
    requires_approval: bool = False
    approval_reason: str = ""
    source_session_keys: list[str] = Field(default_factory=list)
    approved: bool = True


class NodeExecution(BaseModel):
    node_id: str
    name: str
    actor: str
    status: NodeStatus = NodeStatus.PENDING
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    internal_checks: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    summary: str = ""
    receipt_hash: str | None = None
    error: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunRecord(BaseModel):
    run_id: str
    batch_date: str = ""
    batch_seq: int = 1
    trigger: str = "manual"  # manual | scheduled | api | seed
    phase: str = "daily"  # daily | weekly | both
    runner_mode: str = "dry_run"
    approval_mode: str = "always"
    status: RunStatus = RunStatus.PENDING
    admitted_sessions: list[str] = Field(default_factory=list)
    nodes: list[NodeExecution] = Field(default_factory=list)
    planned_writes: list[PlannedWrite] = Field(default_factory=list)
    digest_markdown: str = ""
    error: str | None = None
    lock_released: bool = False
    version: int = 1
    started_at: str = Field(default_factory=utc_now_iso)
    finished_at: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
