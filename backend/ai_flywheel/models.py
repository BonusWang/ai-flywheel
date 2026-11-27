from __future__ import annotations

import re
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

# A Wiki's project configuration page, and the declaration of a page that holds private
# material. A write into either is sensitive whatever the Wiki calls its directories: a
# configuration page carries instance names, scopes and sometimes credentials, and it must
# never be written silently under `smart`.
SENSITIVE_CONFIG_PAGE_TYPE = "project-config"
PRIVATE_SENSITIVITY = "private_sensitive"

# The page type of a Wiki's incident ledger, and the name fragment of the directory a ledger
# is kept under. An incident page records what went wrong and why: it is a sensitive write
# whatever the batch size, so the `smart` approval gate holds it for a human as it holds a
# configuration page.
INCIDENT_PAGE_TYPE = "incident"
INCIDENT_DIRECTORY_MARKER = "ai-incidents"
# The reason every surface states for holding an incident page: the plan when it is
# normalised, the route when the contract recognises the page, the approval gate.
INCIDENT_APPROVAL_REASON = "事故卡属于敏感写入，需人工确认后才会落盘"

# The leading frontmatter block of a draft, and the two lines in it that mark a private page.
_DRAFT_FRONTMATTER_RE = re.compile(r"\A(?:\ufeff)?---[ \t]*\r?\n(.*?)\r?\n?---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_SENSITIVITY_LINE_RE = re.compile(r"^\s*sensitivity:\s*[\"']?([A-Za-z0-9_-]+)", re.MULTILINE)
_CONTAINS_CREDENTIALS_LINE_RE = re.compile(
    r"^\s*contains_credentials:\s*[\"']?(?:true|yes|on|1)\b", re.MULTILINE | re.IGNORECASE
)


def declares_private_page(content_preview: str | None) -> bool:
    """Whether a draft's own frontmatter declares it a private page.

    Only the leading frontmatter block is read; an increment that carries no frontmatter
    says nothing.
    """
    block = _DRAFT_FRONTMATTER_RE.match(content_preview or "")
    if not block:
        return False
    sensitivity = _SENSITIVITY_LINE_RE.search(block.group(1))
    return bool(
        (sensitivity and sensitivity.group(1).lower() == PRIVATE_SENSITIVITY)
        or _CONTAINS_CREDENTIALS_LINE_RE.search(block.group(1))
    )


def _write_fields(write: "PlannedWrite | dict[str, Any]") -> dict[str, Any] | None:
    """What the sensitivity predicates read off a planned write, in one shape for both inputs.

    ``None`` for anything that is neither a :class:`PlannedWrite` nor a dict.
    ``page_type`` is the item's own top-level declaration, which only a dict can carry.
    """
    if isinstance(write, PlannedWrite):
        return {
            "target_kind": write.target_kind,
            "requires_approval": bool(write.requires_approval),
            "action": write.action,
            "wiki_route": write.wiki_route,
            "content_preview": write.content_preview,
            "relative_path": write.relative_path,
            "page_type": None,
        }
    if isinstance(write, dict):
        return {
            "target_kind": write.get("target_kind"),
            "requires_approval": bool(write.get("requires_approval")),
            "action": write.get("action"),
            "wiki_route": write.get("wiki_route"),
            "content_preview": write.get("content_preview"),
            "relative_path": write.get("relative_path"),
            "page_type": write.get("page_type"),
        }
    return None


def _is_incident(fields: dict[str, Any]) -> bool:
    route = fields["wiki_route"]
    route_type = str(route.get("page_type") or "").strip().lower() if isinstance(route, dict) else ""
    own_type = str(fields["page_type"] or "").strip().lower()
    path = str(fields["relative_path"] or "").replace("\\", "/").lower()
    return (
        route_type == INCIDENT_PAGE_TYPE
        or own_type == INCIDENT_PAGE_TYPE
        or INCIDENT_DIRECTORY_MARKER in path
    )


def is_incident_write(write: "PlannedWrite | dict[str, Any]") -> bool:
    """Whether a planned write is a page of a Wiki's incident ledger.

    Any one of three grounds is enough: the routing declaration names the incident
    page type, the item's own payload names it, or the target path lies in an incident
    directory. The last two hold even when no Wiki gate routed the item, so a page that
    reaches the ledger by another road is still recognised.
    """
    fields = _write_fields(write)
    return fields is not None and _is_incident(fields)


def is_sensitive_write(write: "PlannedWrite | dict[str, Any]") -> bool:
    """The one canonical sensitivity predicate for a planned write.

    The portal, the orchestrator's approval gate and the Feishu card all have to
    agree on which writes are sensitive: if they drift, "approve the routine items
    only" would approve a Skill rewrite the gate meant to hold back.

    Accepts both a :class:`PlannedWrite` and the plain dict the Feishu card
    renders, so both sides of the boundary share one implementation.

    A knowledge document is sensitive when it is a page of a Wiki's incident
    ledger, when it is declared as the project configuration page, or when its
    draft declares itself a private page. A write the pipeline recognised as one
    through the mounted Wiki's contract carries ``requires_approval`` and is
    covered by that flag.
    """
    fields = _write_fields(write)
    if fields is None:  # defensive: an unknown shape is never "routine"
        return True
    target_kind = fields["target_kind"]
    if (
        target_kind == "skill_improvement"
        or fields["requires_approval"]
        or fields["action"] in SENSITIVE_ACTIONS
        or _is_incident(fields)
    ):
        return True
    if target_kind != "knowledge_doc":
        return False
    wiki_route = fields["wiki_route"]
    content_preview = fields["content_preview"]
    page_type = str(wiki_route.get("page_type") or "").strip().lower() if isinstance(wiki_route, dict) else ""
    return page_type == SENSITIVE_CONFIG_PAGE_TYPE or declares_private_page(
        content_preview if isinstance(content_preview, str) else ""
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
