"""Schema, terminal-status contract and FTS helpers for the flywheel store.

The SQL DDL, the status vocabulary and the query-term helpers live here so the
repository modules can share one definition of "what the tables look like" and
"what a terminal run is".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


# Trigger source of the placeholder run rows that back internal decision cards
# (the interactive-mode self-check). Such runs never appear in the run lists.
INTERNAL_RUN_TRIGGER = "internal"

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    client TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT 'generic',
    workspace TEXT NOT NULL DEFAULT '',
    transcript_path TEXT,
    summary TEXT,
    content TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    priority TEXT NOT NULL DEFAULT 'P1_GENERAL',
    status TEXT NOT NULL DEFAULT 'pending',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    opt_out INTEGER NOT NULL DEFAULT 0,
    opt_out_reason TEXT NOT NULL DEFAULT '',
    latest_content_hash TEXT NOT NULL DEFAULT '',
    latest_turn_count INTEGER NOT NULL DEFAULT 0,
    last_consumed_turn INTEGER NOT NULL DEFAULT 0,
    last_consumed_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT 'general',
    projects_json TEXT NOT NULL DEFAULT '["generic"]',
    triggers_json TEXT NOT NULL DEFAULT '[]',
    checklist_json TEXT NOT NULL DEFAULT '[]',
    forbidden_routes_json TEXT NOT NULL DEFAULT '[]',
    applicable_skills_json TEXT NOT NULL DEFAULT '[]',
    source_pointers_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'unverified_active',
    version INTEGER NOT NULL DEFAULT 1,
    origin_session_key TEXT,
    hit_count INTEGER NOT NULL DEFAULT 0,
    natural_pass_count INTEGER NOT NULL DEFAULT 0,
    recurrence_count INTEGER NOT NULL DEFAULT 0,
    revalidation_history_json TEXT NOT NULL DEFAULT '[]',
    supersedes_rule_id TEXT,
    effective_from TEXT,
    effective_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preflight_receipts (
    receipt_id TEXT PRIMARY KEY,
    receipt_hash TEXT NOT NULL,
    client TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT 'generic',
    task_fingerprint TEXT NOT NULL,
    anti_laziness_mode TEXT NOT NULL,
    matched_rule_ids_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    receipt_hash TEXT NOT NULL,
    session_key TEXT NOT NULL,
    client TEXT NOT NULL,
    preflight_receipt_id TEXT,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    checks_json TEXT NOT NULL,
    applied_rule_ids_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    revalidation_effects_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retro_queue (
    retro_id TEXT PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    session_key TEXT NOT NULL,
    client TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    summary TEXT NOT NULL,
    tool_name TEXT,
    rule_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    trigger_source TEXT NOT NULL,
    phase TEXT NOT NULL,
    runner_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    admitted_sessions_json TEXT NOT NULL DEFAULT '[]',
    nodes_json TEXT NOT NULL DEFAULT '[]',
    planned_writes_json TEXT NOT NULL DEFAULT '[]',
    digest_markdown TEXT NOT NULL DEFAULT '',
    error TEXT,
    lock_released INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_lock (
    lock_id INTEGER PRIMARY KEY CHECK (lock_id = 1),
    run_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_overrides (
    policy_id TEXT PRIMARY KEY,
    content_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retry_counters (
    counter_key TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    doc_path UNINDEXED,
    title_index,
    description_index,
    body UNINDEXED,
    body_index,
    tags_index,
    project_id UNINDEXED,
    page_type UNINDEXED,
    status UNINDEXED,
    title_raw UNINDEXED,
    description_raw UNINDEXED,
    tags_raw UNINDEXED,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS knowledge_card_metrics (
    doc_path TEXT PRIMARY KEY,
    surface_count INTEGER NOT NULL DEFAULT 0,
    cited_count INTEGER NOT NULL DEFAULT 0,
    helped_count INTEGER NOT NULL DEFAULT 0,
    last_surfaced_at TEXT,
    last_helped_at TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

-- ---------------- v2 Normalized Governance, Pipeline, Decision & Asset Tables ----------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'general',
    value_json TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    summary TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS workflow_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL DEFAULT 0,
    node_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    duration_ms INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    checks_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    receipt_hash TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, node_id)
);

CREATE TABLE IF NOT EXISTS run_sessions (
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    session_key TEXT NOT NULL,
    admitted_chars INTEGER NOT NULL DEFAULT 0,
    disposition TEXT NOT NULL DEFAULT 'admitted',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, session_key)
);

CREATE TABLE IF NOT EXISTS planned_writes (
    write_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    seq_no INTEGER NOT NULL DEFAULT 0,
    target_kind TEXT NOT NULL,
    action TEXT NOT NULL,
    relative_path TEXT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    content_preview TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    skill_route_json TEXT NOT NULL DEFAULT '{}',
    requires_approval INTEGER NOT NULL DEFAULT 0,
    approval_reason TEXT NOT NULL DEFAULT '',
    decision_status TEXT NOT NULL DEFAULT 'pending',
    approved INTEGER NOT NULL DEFAULT 1,
    result_sha256 TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_cards (
    card_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'ask',
    state TEXT NOT NULL DEFAULT 'open',
    title TEXT NOT NULL,
    body_markdown TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '[]',
    selected_option TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS card_deliveries (
    delivery_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES decision_cards(card_id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    external_ref TEXT,
    tries INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(card_id, channel)
);

CREATE TABLE IF NOT EXISTS skill_assets (
    skill_name TEXT PRIMARY KEY,
    source_dir TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'manual',
    sha256 TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    last_run_id TEXT,
    backup_path TEXT,
    invoke_count INTEGER NOT NULL DEFAULT 0,
    verified_pass_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    health_state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ---------------- v3 True Flywheel: Mistakes, Dissents & Growth Ledger ----------------
CREATE TABLE IF NOT EXISTS mistake_observations (
    obs_id TEXT PRIMARY KEY,
    root_cause_key TEXT NOT NULL UNIQUE,
    severity_grade TEXT NOT NULL DEFAULT 'yellow_caught',
    title TEXT NOT NULL,
    phenomenon TEXT NOT NULL DEFAULT '',
    truth TEXT NOT NULL DEFAULT '',
    thinking_root_cause TEXT NOT NULL DEFAULT '',
    caught_by TEXT NOT NULL DEFAULT 'self',
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    session_keys_json TEXT NOT NULL DEFAULT '[]',
    promoted_rule_id TEXT,
    status TEXT NOT NULL DEFAULT 'observing',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dissent_records (
    dissent_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL DEFAULT 'rule',
    target_id TEXT NOT NULL,
    counter_claim TEXT NOT NULL,
    condition_scope TEXT NOT NULL DEFAULT '',
    evidence_pointer TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flywheel_metrics_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    period_key TEXT NOT NULL UNIQUE,
    red_count INTEGER NOT NULL DEFAULT 0,
    yellow_count INTEGER NOT NULL DEFAULT 0,
    green_count INTEGER NOT NULL DEFAULT 0,
    repeat_mistake_rate REAL NOT NULL DEFAULT 0.0,
    self_catch_rate REAL NOT NULL DEFAULT 0.0,
    active_rule_count INTEGER NOT NULL DEFAULT 0,
    dormant_rule_count INTEGER NOT NULL DEFAULT 0,
    open_dissent_count INTEGER NOT NULL DEFAULT 0,
    expired_card_count INTEGER NOT NULL DEFAULT 0,
    stale_skill_count INTEGER NOT NULL DEFAULT 0,
    sample_sufficient INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Secondary query indexes
CREATE INDEX IF NOT EXISTS idx_sessions_status_updated ON sessions(status, priority, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_client_project ON sessions(client, project);
CREATE INDEX IF NOT EXISTS idx_rules_status_hits ON rules(status, hit_count DESC);
CREATE INDEX IF NOT EXISTS idx_preflight_client_created ON preflight_receipts(client, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_outcomes_session_created ON outcomes(session_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_retro_status_session ON retro_queue(status, session_key);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_started ON workflow_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_nodes_run ON workflow_nodes(run_id, step_order);
CREATE INDEX IF NOT EXISTS idx_planned_writes_run ON planned_writes(run_id, seq_no);
CREATE INDEX IF NOT EXISTS idx_planned_writes_status ON planned_writes(decision_status, target_kind);
CREATE INDEX IF NOT EXISTS idx_decision_cards_state ON decision_cards(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_card_deliveries_status ON card_deliveries(status, channel);
CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON audit_events(entity_type, entity_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_system_settings_cat ON system_settings(category);
CREATE INDEX IF NOT EXISTS idx_mistake_obs_status_count ON mistake_observations(status, occurrence_count DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dissent_target_status ON dissent_records(target_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_snapshots_period ON flywheel_metrics_snapshots(period_key DESC);

-- ---------------- v4 Data Foundation: Unified Code Dictionary & Distill Queue ----------------
CREATE TABLE IF NOT EXISTS sys_dict_items (
    dict_type TEXT NOT NULL,
    code TEXT NOT NULL,
    label_zh TEXT NOT NULL,
    label_en TEXT NOT NULL DEFAULT '',
    group_key TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 100,
    is_builtin INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dict_type, code)
);

CREATE TABLE IF NOT EXISTS distill_queue (
    queue_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
    parent_session_key TEXT NOT NULL DEFAULT '',
    is_subagent INTEGER NOT NULL DEFAULT 0,
    client TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT 'generic',
    workspace TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    chunk_index INTEGER NOT NULL DEFAULT 1,
    chunk_total INTEGER NOT NULL DEFAULT 1,
    ingress_channel TEXT NOT NULL DEFAULT 'codex_archive',
    status TEXT NOT NULL DEFAULT 'queued',
    priority TEXT NOT NULL DEFAULT 'P1_GENERAL',
    exclude_reason_code TEXT NOT NULL DEFAULT '',
    exclude_reason_detail TEXT NOT NULL DEFAULT '',
    focus_note TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    total_chars INTEGER NOT NULL DEFAULT 0,
    compacted_chars INTEGER NOT NULL DEFAULT 0,
    is_oversized INTEGER NOT NULL DEFAULT 0,
    consumed_from_turn INTEGER NOT NULL DEFAULT 0,
    consumed_to_turn INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    last_error TEXT NOT NULL DEFAULT '',
    run_id TEXT,
    source_path TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    enqueued_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_key, revision, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_sys_dict_type_enabled ON sys_dict_items(dict_type, enabled, sort_order ASC);
CREATE INDEX IF NOT EXISTS idx_distill_queue_status_priority ON distill_queue(status, priority, enqueued_at ASC);
CREATE INDEX IF NOT EXISTS idx_distill_queue_client_status ON distill_queue(client, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_distill_queue_session_rev ON distill_queue(session_key, revision DESC, chunk_index ASC);
CREATE INDEX IF NOT EXISTS idx_distill_queue_run ON distill_queue(run_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_event_type ON audit_events(event_type, seq DESC);
"""

KNOWLEDGE_FTS_COLUMNS = (
    "doc_path",
    "title_index",
    "description_index",
    "body",
    "body_index",
    "tags_index",
    "project_id",
    "page_type",
    "status",
    "title_raw",
    "description_raw",
    "tags_raw",
)

_KNOWLEDGE_FTS_SQL = """
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    doc_path UNINDEXED,
    title_index,
    description_index,
    body UNINDEXED,
    body_index,
    tags_index,
    project_id UNINDEXED,
    page_type UNINDEXED,
    status UNINDEXED,
    title_raw UNINDEXED,
    description_raw UNINDEXED,
    tags_raw UNINDEXED,
    tokenize='unicode61'
)
"""

_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_ASCII_TERM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-/]{1,}")

# The severity grades, from the mildest to the most dangerous.
SEVERITY_RANK = {"green_minor": 0, "yellow_caught": 1, "red_silent": 2}


def normalize_root_cause_key(root_cause_key: str) -> str:
    """The canonical key of a root cause: kebab-case, lower case, no leading or trailing dash.

    Both the observation pool and the promotion rules key on it, so a key written by a
    model and a key written by a rule must normalize to the same string.
    """
    return re.sub(r"[^a-z0-9\-_]+", "-", (root_cause_key or "").strip().lower()).strip("-")


@dataclass(frozen=True)
class KnowledgeQueryTerms:
    match_terms: list[str]
    overlap_terms: list[str]
    like_terms: list[str]


def cjk_bigrams(text: str) -> list[str]:
    """Overlapping CJK bigrams (single-character runs kept as-is)."""
    out: list[str] = []
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[index : index + 2] for index in range(len(run) - 1))
    return out


def build_knowledge_search_text(*parts: str) -> str:
    """Expand CJK runs into space-separated bigrams while keeping ASCII tokens intact."""
    segments: list[str] = []
    for text in parts:
        if not text:
            continue
        cursor = 0
        pieces: list[str] = []
        for match in _CJK_RUN.finditer(text):
            pieces.append(text[cursor : match.start()])
            pieces.append(" " + " ".join(cjk_bigrams(match.group(0))) + " ")
            cursor = match.end()
        pieces.append(text[cursor:])
        segments.append("".join(pieces))
    return "\n".join(segments)


def _knowledge_query_terms(query: str) -> KnowledgeQueryTerms:
    seen: list[str] = []
    like_terms: list[str] = []
    for run in _CJK_RUN.findall(query):
        terms = cjk_bigrams(run)
        for term in terms:
            if len(term) < 2:
                if term not in like_terms:
                    like_terms.append(term)
                continue
            if term not in seen:
                seen.append(term)
    for match in _ASCII_TERM.finditer(query):
        term = match.group(0).casefold()
        if term not in seen:
            seen.append(term)
    return KnowledgeQueryTerms(
        match_terms=seen,
        overlap_terms=list(dict.fromkeys(seen + like_terms)),
        like_terms=like_terms,
    )


def _knowledge_snippet(body: str, terms: list[str], width: int = 60) -> str:
    if not body:
        return ""
    folded = body.casefold()
    best = -1
    best_term = ""
    for term in terms:
        if not term:
            continue
        position = folded.find(term.casefold())
        if position >= 0 and (best < 0 or position < best):
            best = position
            best_term = term
    if best < 0:
        return body[: width * 2].replace("\n", " ")
    start = max(0, best - width)
    end = min(len(body), best + len(best_term) + width)
    snippet = body[start:end].replace("\n", " ")
    relative = best - start
    return (
        snippet[:relative]
        + "["
        + snippet[relative : relative + len(best_term)]
        + "]"
        + snippet[relative + len(best_term) :]
    )


TERMINAL_RUN_STATUSES = {"PASS", "PARTIAL", "CANCELLED", "FAILED"}


def to_shanghai_date(iso_str: str | None) -> str:
    """The Shanghai calendar date of an ISO timestamp (today when there is none)."""
    if not iso_str:
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    except Exception:
        return iso_str[:10]


def _load_json_list(raw: Any) -> list[Any]:
    """A JSON list column, or an empty list when the column is missing or malformed."""
    try:
        loaded = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return list(loaded) if isinstance(loaded, list) else []


def _load_json_map(raw: Any) -> dict[str, Any]:
    """A JSON object column, or an empty dict when the column is missing or malformed."""
    try:
        loaded = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}

class RunAlreadyFinalizedError(RuntimeError):
    """Raised when a writer tries to overwrite a terminal run status with a different status.

    Guards against zombie executors (e.g. a run reaped as interrupted whose original
    executor later wakes up) resurrecting a run via the whole-row upsert.
    """

    def __init__(self, run_id: str, stored_status: str) -> None:
        super().__init__(f"Run {run_id} is already finalized as {stored_status}; refusing to overwrite with a different status.")
        self.run_id = run_id
        self.stored_status = stored_status
