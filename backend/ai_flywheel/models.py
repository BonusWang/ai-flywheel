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
    DORMANT = "dormant"
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
    # A run the maintainer cancelled at the approval gate: nothing was written.
    # Distinct from PARTIAL, which means "partly done" — the two must not be
    # interchangeable, or a cancellation reads like a half-finished batch.
    CANCELLED = "CANCELLED"
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


class QueueStatus(str, Enum):
    """The lifecycle of one distill-queue inventory item.

    Distinct from :class:`SessionStatus`: a session is an entity, a queue item
    is one unit of work over it (one revision, one chunk).
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    PROCESSED = "processed"
    EXCLUDED = "excluded"
    WITHDRAWN = "withdrawn"
    DEAD_LETTER = "dead_letter"


class IngressChannel(str, Enum):
    """How a distill-queue item entered the queue."""

    MCP_EXPLICIT = "mcp_explicit"
    MANUAL_PORTAL = "manual_portal"
    CODEX_ARCHIVE = "codex_archive"
    CLAUDE_EXIT = "claude_exit"
    LEGACY_SCAN = "legacy_scan"


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
    # The profile extensions. They carry safe defaults so every pre-v4 caller
    # keeps constructing a valid record without naming them.
    opt_out: bool = False
    opt_out_reason: str = ""
    latest_content_hash: str = ""
    latest_turn_count: int = 0
    last_consumed_turn: int = 0
    last_consumed_hash: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class DictItem(BaseModel):
    """One row of the unified ``sys_dict_items`` code dictionary."""

    dict_type: str
    code: str
    label_zh: str
    label_en: str = ""
    group_key: str = ""
    description: str = ""
    sort_order: int = 100
    is_builtin: bool = True
    enabled: bool = True
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class DistillQueueItem(BaseModel):
    """One unit of distill work: a session revision, optionally one chunk of it."""

    queue_id: str
    session_key: str
    parent_session_key: str = ""
    is_subagent: bool = False
    client: str
    project: str = "generic"
    workspace: str = ""
    title: str = ""
    revision: int = 1
    chunk_index: int = 1
    chunk_total: int = 1
    ingress_channel: IngressChannel = IngressChannel.CODEX_ARCHIVE
    status: QueueStatus = QueueStatus.QUEUED
    priority: str = "P1_GENERAL"
    exclude_reason_code: str = ""
    exclude_reason_detail: str = ""
    focus_note: str = ""
    content_hash: str = ""
    total_chars: int = 0
    compacted_chars: int = 0
    is_oversized: bool = False
    consumed_from_turn: int = 0
    consumed_to_turn: int = 0
    retry_count: int = 0
    max_retries: int = 3
    last_error: str = ""
    run_id: str | None = None
    source_path: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    enqueued_at: str = Field(default_factory=utc_now_iso)
    claimed_at: str | None = None
    completed_at: str | None = None
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
    last_hit_at: str | None = None
    dormant_at: str | None = None
    root_cause_key: str | None = None
    # Lightweight bi-temporal lineage. `supersedes_rule_id` names the rule this
    # one replaces; `effective_from` / `effective_to` bracket the window in which
    # it is in force (`effective_to` NULL means it still is).
    supersedes_rule_id: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
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
    # When set, registering this rule also retires the rule it replaces.
    supersedes_rule_id: str | None = None


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
    has_queued_session: bool | None = None
    settlement_mode: str | None = None
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
    used_skills: list[str] = Field(default_factory=list)


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
    used_skills: list[str] = Field(default_factory=list)
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
    # The planner's routing declaration for a knowledge_doc (page_type, project, reason), carried
    # untouched for the Wiki gate to validate. The dry-run planner has no model and never sets it.
    wiki_route: dict[str, Any] | None = None
    # Non-primary paths this approved write will also change, such as the
    # project practice index. P03 previews them before approval and P04 records
    # their actual receipt in the same write receipt.
    side_effects: list[dict[str, Any]] = Field(default_factory=list)
    requires_approval: bool = False
    approval_reason: str = ""
    source_session_keys: list[str] = Field(default_factory=list)
    approved: bool = True


# The write actions that always need a human: they destroy or replace core content.
SENSITIVE_ACTIONS = frozenset({"delete", "overwrite_core"})


def is_sensitive_write(write: "PlannedWrite | dict[str, Any]") -> bool:
    """The one canonical sensitivity predicate for a planned write.

    The portal, the orchestrator's approval gate and the Feishu card all have to
    agree on which writes are sensitive: if they drift, "approve the routine items
    only" would approve a Skill rewrite the gate meant to hold back.

    Accepts both a :class:`PlannedWrite` and the plain dict the Feishu card
    renders, so both sides of the boundary share one implementation.
    """
    if isinstance(write, PlannedWrite):
        target_kind = write.target_kind
        requires_approval = bool(write.requires_approval)
        action = write.action
    elif isinstance(write, dict):
        target_kind = write.get("target_kind")
        requires_approval = bool(write.get("requires_approval"))
        action = write.get("action")
    else:  # pragma: no cover - defensive: an unknown shape is never "routine"
        return True
    return (
        target_kind == "skill_improvement"
        or requires_approval
        or action in SENSITIVE_ACTIONS
    )


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
