from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ai_flywheel.core.privacy import compute_valid_until, shanghai_today_ymd
from ai_flywheel.models import (
    NodeExecution,
    OutcomeCheck,
    OutcomeOrigin,
    OutcomeRecord,
    OutcomeStatus,
    PlannedWrite,
    RetroItem,
    RetroStatus,
    RuleRecord,
    RuleStatus,
    RunStatus,
    SessionRecord,
    SessionStatus,
    SignalKind,
    WorkflowRunRecord,
    utc_now_iso,
)


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
    updated_at TEXT NOT NULL
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
    created_at TEXT NOT NULL
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


class FlywheelDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        preexisting = self.db_path.exists() and self.db_path.stat().st_size > 0
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            self._migrate_knowledge_fts(conn)
            self._migrate_to_v2(conn, preexisting=preexisting)
            self._migrate_to_v3(conn, preexisting=preexisting)

    @staticmethod
    def _to_shanghai_date(iso_str: str | None) -> str:
        if not iso_str:
            return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        except Exception:
            return iso_str[:10]

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _ensure_column(self, conn: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
        if col not in self._table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    def _migrate_to_v2(self, conn: sqlite3.Connection, *, preexisting: bool) -> None:
        applied = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 2"
        ).fetchone()
        if applied:
            return

        # Auto-backup existing v1 database before running schema upgrades
        if preexisting and self.db_path.exists():
            backup = self.db_path.with_name(f"{self.db_path.name}.bak-v1")
            if not backup.exists():
                try:
                    shutil.copy2(self.db_path, backup)
                except OSError:
                    pass

        now = utc_now_iso()
        # Universal created_at & updated_at audit columns + batch/optimistic-lock columns
        self._ensure_column(conn, "workflow_runs", "batch_date", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "workflow_runs", "batch_seq", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(conn, "workflow_runs", "approval_mode", "TEXT NOT NULL DEFAULT 'always'")
        self._ensure_column(conn, "workflow_runs", "version", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column(conn, "workflow_runs", "created_at", f"TEXT NOT NULL DEFAULT '{now}'")
        self._ensure_column(conn, "workflow_runs", "updated_at", f"TEXT NOT NULL DEFAULT '{now}'")
        self._ensure_column(conn, "policy_overrides", "created_at", f"TEXT NOT NULL DEFAULT '{now}'")
        self._ensure_column(conn, "workflow_lock", "expires_at", "TEXT")
        self._ensure_column(conn, "workflow_lock", "created_at", f"TEXT NOT NULL DEFAULT '{now}'")
        self._ensure_column(conn, "workflow_lock", "updated_at", f"TEXT NOT NULL DEFAULT '{now}'")
        self._ensure_column(conn, "retry_counters", "created_at", f"TEXT NOT NULL DEFAULT '{now}'")
        self._ensure_column(conn, "preflight_receipts", "updated_at", f"TEXT NOT NULL DEFAULT '{now}'")
        self._ensure_column(conn, "outcomes", "updated_at", f"TEXT NOT NULL DEFAULT '{now}'")
        self._ensure_column(conn, "retro_queue", "linked_run_id", "TEXT")
        self._ensure_column(conn, "knowledge_card_metrics", "created_at", f"TEXT NOT NULL DEFAULT '{now}'")
        self._ensure_column(conn, "knowledge_card_metrics", "updated_at", f"TEXT NOT NULL DEFAULT '{now}'")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_runs_batch ON workflow_runs(batch_date DESC, batch_seq DESC)"
        )

        # Backfill existing workflow_runs rows into normalized child tables
        rows = conn.execute("SELECT * FROM workflow_runs ORDER BY started_at ASC").fetchall()
        date_counts: dict[str, int] = {}
        for row in rows:
            run_id = row["run_id"]
            bdate = row["batch_date"] or self._to_shanghai_date(row["started_at"])
            seq = date_counts.get(bdate, 0) + 1
            date_counts[bdate] = seq
            created = row["started_at"] or now
            updated = row["finished_at"] or row["started_at"] or now
            conn.execute(
                """
                UPDATE workflow_runs
                SET batch_date = ?, batch_seq = ?, created_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (bdate, seq, created, updated, run_id),
            )
            # Backfill child tables if empty for this run
            existing_nodes = conn.execute(
                "SELECT COUNT(1) AS c FROM workflow_nodes WHERE run_id = ?", (run_id,)
            ).fetchone()["c"]
            if existing_nodes == 0:
                try:
                    nodes_data = json.loads(row["nodes_json"] or "[]")
                    for idx, n in enumerate(nodes_data):
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO workflow_nodes (
                                run_id, step_order, node_id, name, actor, status, duration_ms,
                                summary, checks_json, artifacts_json, payload_json, receipt_hash,
                                error, started_at, finished_at, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id,
                                idx,
                                n.get("node_id", f"P0{idx}"),
                                n.get("name", ""),
                                n.get("actor", ""),
                                n.get("status", "PENDING"),
                                n.get("duration_ms"),
                                n.get("summary", ""),
                                json.dumps(n.get("internal_checks", []), ensure_ascii=False),
                                json.dumps(n.get("artifacts", []), ensure_ascii=False),
                                json.dumps(n.get("payload", {}), ensure_ascii=False),
                                n.get("receipt_hash"),
                                n.get("error"),
                                n.get("started_at"),
                                n.get("finished_at"),
                                created,
                                updated,
                            ),
                        )
                except Exception:
                    pass
                try:
                    for sk in json.loads(row["admitted_sessions_json"] or "[]"):
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO run_sessions (
                                run_id, session_key, admitted_chars, disposition, created_at, updated_at
                            ) VALUES (?, ?, 0, 'admitted', ?, ?)
                            """,
                            (run_id, str(sk), created, updated),
                        )
                except Exception:
                    pass
                try:
                    for w_idx, w in enumerate(json.loads(row["planned_writes_json"] or "[]")):
                        wid = f"{run_id}:w{w_idx}"
                        d_status = (
                            "written"
                            if row["status"] == "PASS" and w.get("approved", True)
                            else "rejected"
                            if row["status"] == "PARTIAL"
                            else "pending"
                        )
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO planned_writes (
                                write_id, run_id, seq_no, target_kind, action, relative_path,
                                title, summary, content_preview, payload_json, skill_route_json,
                                requires_approval, approval_reason, decision_status, approved,
                                version, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                wid,
                                run_id,
                                w_idx,
                                w.get("target_kind", "knowledge_doc"),
                                w.get("action", "create"),
                                w.get("relative_path"),
                                w.get("title", ""),
                                w.get("summary", ""),
                                w.get("content_preview", ""),
                                json.dumps(w.get("rule_payload") or {}, ensure_ascii=False),
                                json.dumps(w.get("skill_route") or {}, ensure_ascii=False),
                                1 if w.get("requires_approval") else 0,
                                w.get("approval_reason", ""),
                                d_status,
                                1 if w.get("approved", True) else 0,
                                created,
                                updated,
                            ),
                        )
                except Exception:
                    pass

        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) VALUES (2, 'v2_normalized_audit_and_decisions', ?)",
            (now,),
        )

    def _migrate_to_v3(self, conn: sqlite3.Connection, *, preexisting: bool) -> None:
        applied = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 3"
        ).fetchone()
        if applied:
            return

        # Auto-backup existing v2 database before running v3 schema upgrades
        if preexisting and self.db_path.exists():
            backup = self.db_path.with_name(f"{self.db_path.name}.bak-v2")
            if not backup.exists():
                try:
                    shutil.copy2(self.db_path, backup)
                except OSError:
                    pass

        now = utc_now_iso()
        # Rules lifecycle & dormancy metadata
        self._ensure_column(conn, "rules", "last_hit_at", "TEXT")
        self._ensure_column(conn, "rules", "dormant_at", "TEXT")
        self._ensure_column(conn, "rules", "root_cause_key", "TEXT")
        # Skill usage & health tracking (anti-Skill-theater)
        self._ensure_column(conn, "skill_assets", "invoke_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "skill_assets", "verified_pass_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "skill_assets", "fail_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "skill_assets", "last_used_at", "TEXT")
        self._ensure_column(conn, "skill_assets", "health_state", "TEXT NOT NULL DEFAULT 'active'")
        # Knowledge card freshness governance
        self._ensure_column(conn, "knowledge_card_metrics", "freshness_tier", "TEXT NOT NULL DEFAULT 'evolving'")
        self._ensure_column(conn, "knowledge_card_metrics", "last_verified_at", "TEXT")
        self._ensure_column(conn, "knowledge_card_metrics", "valid_until", "TEXT")
        self._ensure_column(conn, "knowledge_card_metrics", "freshness_state", "TEXT NOT NULL DEFAULT 'fresh'")
        # Outcome skill attribution
        self._ensure_column(conn, "outcomes", "used_skills_json", "TEXT NOT NULL DEFAULT '[]'")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rules_root_cause ON rules(root_cause_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_freshness ON knowledge_card_metrics(freshness_state, valid_until)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) VALUES (3, 'v3_true_flywheel_evolution', ?)",
            (now,),
        )

    def _migrate_knowledge_fts(self, conn: sqlite3.Connection) -> None:
        """Rebuild the FTS table when its column contract changed (pre-2026-11-23 layouts)."""
        columns = tuple(row["name"] for row in conn.execute("PRAGMA table_info(knowledge_fts)").fetchall())
        if columns != KNOWLEDGE_FTS_COLUMNS:
            conn.execute("DROP TABLE IF EXISTS knowledge_fts")
            conn.execute(_KNOWLEDGE_FTS_SQL)

    # ---------------- Sessions ----------------
    def upsert_session(self, record: SessionRecord) -> SessionRecord:
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT created_at, status FROM sessions WHERE session_key = ?",
                (record.session_key,),
            ).fetchone()
            created_at = existing["created_at"] if existing else record.created_at
            status = record.status.value
            if existing and existing["status"] == SessionStatus.PROCESSED.value and record.content:
                # Re-open if substantial new content was pushed
                status = SessionStatus.PENDING.value
            conn.execute(
                """
                INSERT INTO sessions (
                    session_key, client, session_id, title, project, workspace,
                    transcript_path, summary, content, tags_json, priority, status,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE sessions.title END,
                    project = CASE WHEN excluded.project != 'generic' THEN excluded.project ELSE sessions.project END,
                    workspace = CASE WHEN excluded.workspace != '' THEN excluded.workspace ELSE sessions.workspace END,
                    transcript_path = COALESCE(excluded.transcript_path, sessions.transcript_path),
                    summary = COALESCE(excluded.summary, sessions.summary),
                    content = COALESCE(excluded.content, sessions.content),
                    tags_json = excluded.tags_json,
                    priority = excluded.priority,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    record.session_key,
                    record.client,
                    record.session_id,
                    record.title,
                    record.project,
                    record.workspace,
                    record.transcript_path,
                    record.summary,
                    record.content,
                    json.dumps(record.tags, ensure_ascii=False),
                    record.priority,
                    status,
                    json.dumps(record.metadata, ensure_ascii=False),
                    created_at,
                    now,
                ),
            )
        return self.get_session(record.session_key)  # type: ignore[return-value]

    def _row_to_session(self, row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            session_key=row["session_key"],
            client=row["client"],
            session_id=row["session_id"],
            title=row["title"],
            project=row["project"],
            workspace=row["workspace"],
            transcript_path=row["transcript_path"],
            summary=row["summary"],
            content=row["content"],
            tags=json.loads(row["tags_json"]),
            priority=row["priority"],
            status=SessionStatus(row["status"]),
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_session(self, session_key: str) -> SessionRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_key = ?", (session_key,)).fetchone()
            return self._row_to_session(row) if row else None

    def list_sessions(self, status: str | None = None, limit: int = 100) -> list[SessionRecord]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_session(r) for r in rows]

    def update_session_status(self, session_key: str, status: SessionStatus) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_key = ?",
                (status.value, utc_now_iso(), session_key),
            )

    # ---------------- Rules ----------------
    def upsert_rule(self, rule: RuleRecord) -> RuleRecord:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO rules (
                    rule_id, title, domain, projects_json, triggers_json, checklist_json,
                    forbidden_routes_json, applicable_skills_json, source_pointers_json,
                    status, version, origin_session_key, hit_count, natural_pass_count,
                    recurrence_count, revalidation_history_json, last_hit_at, dormant_at,
                    root_cause_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    title = excluded.title,
                    domain = excluded.domain,
                    projects_json = excluded.projects_json,
                    triggers_json = excluded.triggers_json,
                    checklist_json = excluded.checklist_json,
                    forbidden_routes_json = excluded.forbidden_routes_json,
                    applicable_skills_json = excluded.applicable_skills_json,
                    source_pointers_json = excluded.source_pointers_json,
                    status = excluded.status,
                    version = excluded.version,
                    hit_count = excluded.hit_count,
                    natural_pass_count = excluded.natural_pass_count,
                    recurrence_count = excluded.recurrence_count,
                    revalidation_history_json = excluded.revalidation_history_json,
                    last_hit_at = COALESCE(excluded.last_hit_at, rules.last_hit_at),
                    dormant_at = excluded.dormant_at,
                    root_cause_key = COALESCE(excluded.root_cause_key, rules.root_cause_key),
                    updated_at = excluded.updated_at
                """,
                (
                    rule.rule_id,
                    rule.title,
                    rule.domain,
                    json.dumps(rule.projects, ensure_ascii=False),
                    json.dumps(rule.triggers, ensure_ascii=False),
                    json.dumps(rule.checklist, ensure_ascii=False),
                    json.dumps(rule.forbidden_routes, ensure_ascii=False),
                    json.dumps(rule.applicable_skills, ensure_ascii=False),
                    json.dumps(rule.source_pointers, ensure_ascii=False),
                    rule.status.value,
                    rule.version,
                    rule.origin_session_key,
                    rule.hit_count,
                    rule.natural_pass_count,
                    rule.recurrence_count,
                    json.dumps(rule.revalidation_history, ensure_ascii=False),
                    rule.last_hit_at,
                    rule.dormant_at,
                    rule.root_cause_key,
                    rule.created_at,
                    now,
                ),
            )
        return self.get_rule(rule.rule_id)  # type: ignore[return-value]

    def _row_to_rule(self, row: sqlite3.Row) -> RuleRecord:
        keys = set(row.keys())
        return RuleRecord(
            rule_id=row["rule_id"],
            title=row["title"],
            domain=row["domain"],
            projects=json.loads(row["projects_json"]),
            triggers=json.loads(row["triggers_json"]),
            checklist=json.loads(row["checklist_json"]),
            forbidden_routes=json.loads(row["forbidden_routes_json"]),
            applicable_skills=json.loads(row["applicable_skills_json"]),
            source_pointers=json.loads(row["source_pointers_json"]),
            status=RuleStatus(row["status"]),
            version=row["version"],
            origin_session_key=row["origin_session_key"],
            hit_count=row["hit_count"],
            natural_pass_count=row["natural_pass_count"],
            recurrence_count=row["recurrence_count"],
            revalidation_history=json.loads(row["revalidation_history_json"]),
            last_hit_at=row["last_hit_at"] if "last_hit_at" in keys else None,
            dormant_at=row["dormant_at"] if "dormant_at" in keys else None,
            root_cause_key=row["root_cause_key"] if "root_cause_key" in keys else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_rule(self, rule_id: str) -> RuleRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
            return self._row_to_rule(row) if row else None

    def list_rules(self, status: str | None = None) -> list[RuleRecord]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM rules WHERE status = ? ORDER BY updated_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM rules ORDER BY updated_at DESC").fetchall()
            return [self._row_to_rule(r) for r in rows]

    def increment_rule_hit(self, rule_id: str) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE rules SET hit_count = hit_count + 1, last_hit_at = ?, updated_at = ? WHERE rule_id = ?",
                (now, now, rule_id),
            )

    # ---------------- Preflight & Outcomes ----------------
    def save_preflight_receipt(
        self,
        receipt_id: str,
        receipt_hash: str,
        client: str,
        session_id: str,
        turn_id: str,
        project: str,
        task_fingerprint: str,
        anti_laziness_mode: str,
        matched_rule_ids: list[str],
        payload: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO preflight_receipts (
                    receipt_id, receipt_hash, client, session_id, turn_id, project,
                    task_fingerprint, anti_laziness_mode, matched_rule_ids_json, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    receipt_hash,
                    client,
                    session_id,
                    turn_id,
                    project,
                    task_fingerprint,
                    anti_laziness_mode,
                    json.dumps(matched_rule_ids, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    utc_now_iso(),
                ),
            )

    def count_preflights(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM preflight_receipts").fetchone()
            return int(row["cnt"]) if row else 0

    def save_outcome(self, record: OutcomeRecord) -> OutcomeRecord:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO outcomes (
                    outcome_id, receipt_hash, session_key, client, preflight_receipt_id,
                    origin, status, summary, checks_json, applied_rule_ids_json,
                    artifacts_json, revalidation_effects_json, used_skills_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.outcome_id,
                    record.receipt_hash,
                    record.session_key,
                    record.client,
                    record.preflight_receipt_id,
                    record.origin.value,
                    record.status.value,
                    record.summary,
                    json.dumps([c.model_dump(mode="json") for c in record.checks], ensure_ascii=False),
                    json.dumps(record.applied_rule_ids, ensure_ascii=False),
                    json.dumps(record.artifacts, ensure_ascii=False),
                    json.dumps(record.revalidation_effects, ensure_ascii=False),
                    json.dumps(record.used_skills, ensure_ascii=False),
                    record.created_at,
                    now,
                ),
            )
        return record

    def list_outcomes(self, limit: int = 100) -> list[OutcomeRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM outcomes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out: list[OutcomeRecord] = []
            for r in rows:
                keys = set(r.keys())
                used_skills = json.loads(r["used_skills_json"]) if "used_skills_json" in keys and r["used_skills_json"] else []
                out.append(
                    OutcomeRecord(
                        outcome_id=r["outcome_id"],
                        receipt_hash=r["receipt_hash"],
                        session_key=r["session_key"],
                        client=r["client"],
                        preflight_receipt_id=r["preflight_receipt_id"],
                        origin=OutcomeOrigin(r["origin"]),
                        status=OutcomeStatus(r["status"]),
                        summary=r["summary"],
                        checks=[OutcomeCheck.model_validate(c) for c in json.loads(r["checks_json"])],
                        applied_rule_ids=json.loads(r["applied_rule_ids_json"]),
                        used_skills=used_skills,
                        artifacts=json.loads(r["artifacts_json"]),
                        revalidation_effects=json.loads(r["revalidation_effects_json"]),
                        created_at=r["created_at"],
                    )
                )
            return out

    # ---------------- Retro Queue ----------------
    def enqueue_retro(self, item: RetroItem, dedupe_key: str | None = None) -> tuple[RetroItem, bool]:
        with self.connect() as conn:
            if dedupe_key:
                existing = conn.execute(
                    "SELECT * FROM retro_queue WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
                if existing:
                    return self._row_to_retro(existing), False
            conn.execute(
                """
                INSERT INTO retro_queue (
                    retro_id, dedupe_key, session_key, client, kind, severity, summary,
                    tool_name, rule_id, detail_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.retro_id,
                    dedupe_key,
                    item.session_key,
                    item.client,
                    item.kind.value,
                    item.severity,
                    item.summary,
                    item.tool_name,
                    item.rule_id,
                    json.dumps(item.detail, ensure_ascii=False),
                    item.status.value,
                    item.created_at,
                    item.updated_at,
                ),
            )
            return item, True

    def _row_to_retro(self, row: sqlite3.Row) -> RetroItem:
        return RetroItem(
            retro_id=row["retro_id"],
            session_key=row["session_key"],
            client=row["client"],
            kind=SignalKind(row["kind"]),
            severity=row["severity"],
            summary=row["summary"],
            tool_name=row["tool_name"],
            rule_id=row["rule_id"],
            detail=json.loads(row["detail_json"]),
            status=RetroStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_retro_items(self, status: str | None = None, limit: int = 100) -> list[RetroItem]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM retro_queue WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM retro_queue ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_retro(r) for r in rows]

    def update_retro_status(self, retro_id: str, status: RetroStatus) -> RetroItem | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE retro_queue SET status = ?, updated_at = ? WHERE retro_id = ?",
                (status.value, utc_now_iso(), retro_id),
            )
            row = conn.execute("SELECT * FROM retro_queue WHERE retro_id = ?", (retro_id,)).fetchone()
            return self._row_to_retro(row) if row else None

    # ---------------- Retry Counter ----------------
    def bump_retry_counter(self, counter_key: str) -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT count FROM retry_counters WHERE counter_key = ?", (counter_key,)
            ).fetchone()
            new_count = (int(row["count"]) + 1) if row else 1
            conn.execute(
                """
                INSERT INTO retry_counters (counter_key, count, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(counter_key) DO UPDATE SET count = excluded.count, updated_at = excluded.updated_at
                """,
                (counter_key, new_count, now, now),
            )
            return new_count

    # ---------------- Workflow Runs & Lock ----------------
    def acquire_workflow_lock(self, run_id: str, ttl_seconds: int = 3600) -> bool:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires = (now_dt + timedelta(seconds=ttl_seconds)).isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT run_id, expires_at FROM workflow_lock WHERE lock_id = 1"
            ).fetchone()
            if existing:
                if existing["expires_at"] and existing["expires_at"] < now:
                    conn.execute("DELETE FROM workflow_lock WHERE lock_id = 1")
                else:
                    return False
            conn.execute(
                """
                INSERT INTO workflow_lock (lock_id, run_id, acquired_at, expires_at, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (run_id, now, expires, now, now),
            )
            return True

    def get_workflow_lock(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT run_id, acquired_at, expires_at FROM workflow_lock WHERE lock_id = 1"
            ).fetchone()
            return dict(row) if row else None

    def release_workflow_lock(self, run_id: str | None = None) -> bool:
        with self.connect() as conn:
            if run_id:
                cur = conn.execute("DELETE FROM workflow_lock WHERE lock_id = 1 AND run_id = ?", (run_id,))
            else:
                cur = conn.execute("DELETE FROM workflow_lock WHERE lock_id = 1")
            return cur.rowcount > 0

    def save_workflow_run(self, run: WorkflowRunRecord) -> WorkflowRunRecord:
        now = utc_now_iso()
        bdate = run.batch_date or self._to_shanghai_date(run.started_at)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT batch_seq, version, created_at FROM workflow_runs WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
            if existing:
                bseq = int(existing["batch_seq"] or 1)
                ver = int(existing["version"] or 1) + 1
                created_at = existing["created_at"] or run.started_at or now
            else:
                max_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(batch_seq), 0) AS m FROM workflow_runs WHERE batch_date = ?",
                    (bdate,),
                ).fetchone()
                bseq = int(max_seq_row["m"]) + 1
                ver = 1
                created_at = run.created_at or run.started_at or now
            run.batch_date = bdate
            run.batch_seq = bseq
            run.version = ver
            run.created_at = created_at
            run.updated_at = now

            conn.execute(
                """
                INSERT INTO workflow_runs (
                    run_id, batch_date, batch_seq, trigger_source, phase, runner_mode,
                    approval_mode, status, admitted_sessions_json, nodes_json, planned_writes_json,
                    digest_markdown, error, lock_released, version, started_at, finished_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    batch_date = excluded.batch_date,
                    approval_mode = excluded.approval_mode,
                    status = excluded.status,
                    admitted_sessions_json = excluded.admitted_sessions_json,
                    nodes_json = excluded.nodes_json,
                    planned_writes_json = excluded.planned_writes_json,
                    digest_markdown = excluded.digest_markdown,
                    error = excluded.error,
                    lock_released = excluded.lock_released,
                    version = excluded.version,
                    finished_at = excluded.finished_at,
                    updated_at = excluded.updated_at
                """,
                (
                    run.run_id,
                    bdate,
                    bseq,
                    run.trigger,
                    run.phase,
                    run.runner_mode,
                    run.approval_mode,
                    run.status.value,
                    json.dumps(run.admitted_sessions, ensure_ascii=False),
                    json.dumps([n.model_dump(mode="json") for n in run.nodes], ensure_ascii=False),
                    json.dumps([w.model_dump(mode="json") for w in run.planned_writes], ensure_ascii=False),
                    run.digest_markdown,
                    run.error,
                    1 if run.lock_released else 0,
                    ver,
                    run.started_at,
                    run.finished_at,
                    created_at,
                    now,
                ),
            )

            # Sync normalized child tables atomically inside the same transaction
            for idx, node in enumerate(run.nodes):
                conn.execute(
                    """
                    INSERT INTO workflow_nodes (
                        run_id, step_order, node_id, name, actor, status, duration_ms,
                        summary, checks_json, artifacts_json, payload_json, receipt_hash,
                        error, started_at, finished_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, node_id) DO UPDATE SET
                        status = excluded.status,
                        duration_ms = excluded.duration_ms,
                        summary = excluded.summary,
                        checks_json = excluded.checks_json,
                        artifacts_json = excluded.artifacts_json,
                        payload_json = excluded.payload_json,
                        receipt_hash = excluded.receipt_hash,
                        error = excluded.error,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        run.run_id,
                        idx,
                        node.node_id,
                        node.name,
                        node.actor,
                        node.status.value,
                        node.duration_ms,
                        node.summary,
                        json.dumps(node.internal_checks, ensure_ascii=False),
                        json.dumps(node.artifacts, ensure_ascii=False),
                        json.dumps(node.payload, ensure_ascii=False),
                        node.receipt_hash,
                        node.error,
                        node.started_at,
                        node.finished_at,
                        created_at,
                        now,
                    ),
                )

            for sk in run.admitted_sessions:
                conn.execute(
                    """
                    INSERT INTO run_sessions (
                        run_id, session_key, admitted_chars, disposition, created_at, updated_at
                    ) VALUES (?, ?, 0, 'admitted', ?, ?)
                    ON CONFLICT(run_id, session_key) DO UPDATE SET updated_at = excluded.updated_at
                    """,
                    (run.run_id, sk, created_at, now),
                )

            for w_idx, w in enumerate(run.planned_writes):
                wid = f"{run.run_id}:w{w_idx}"
                if run.status == RunStatus.PASS:
                    d_status = "written" if w.approved else "rejected"
                elif run.status == RunStatus.PARTIAL:
                    d_status = "rejected"
                elif run.status == RunStatus.FAILED:
                    d_status = "failed"
                elif run.status == RunStatus.AWAITING_APPROVAL:
                    d_status = "pending"
                else:
                    d_status = "auto_approved" if not w.requires_approval else "pending"
                conn.execute(
                    """
                    INSERT INTO planned_writes (
                        write_id, run_id, seq_no, target_kind, action, relative_path,
                        title, summary, content_preview, payload_json, skill_route_json,
                        requires_approval, approval_reason, decision_status, approved,
                        version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(write_id) DO UPDATE SET
                        decision_status = excluded.decision_status,
                        approved = excluded.approved,
                        version = planned_writes.version + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        wid,
                        run.run_id,
                        w_idx,
                        w.target_kind,
                        w.action,
                        w.relative_path,
                        w.title,
                        w.summary,
                        w.content_preview,
                        json.dumps(w.rule_payload or {}, ensure_ascii=False),
                        json.dumps(w.skill_route or {}, ensure_ascii=False),
                        1 if w.requires_approval else 0,
                        w.approval_reason,
                        d_status,
                        1 if w.approved else 0,
                        created_at,
                        now,
                    ),
                )
        return run

    def _row_to_run(self, row: sqlite3.Row) -> WorkflowRunRecord:
        keys = set(row.keys())
        return WorkflowRunRecord(
            run_id=row["run_id"],
            batch_date=row["batch_date"] if "batch_date" in keys and row["batch_date"] else self._to_shanghai_date(row["started_at"]),
            batch_seq=int(row["batch_seq"]) if "batch_seq" in keys and row["batch_seq"] else 1,
            trigger=row["trigger_source"],
            phase=row["phase"],
            runner_mode=row["runner_mode"],
            approval_mode=row["approval_mode"] if "approval_mode" in keys and row["approval_mode"] else "always",
            status=RunStatus(row["status"]),
            admitted_sessions=json.loads(row["admitted_sessions_json"]),
            nodes=[NodeExecution.model_validate(n) for n in json.loads(row["nodes_json"])],
            planned_writes=[PlannedWrite.model_validate(w) for w in json.loads(row["planned_writes_json"])],
            digest_markdown=row["digest_markdown"],
            error=row["error"],
            lock_released=bool(row["lock_released"]),
            version=int(row["version"]) if "version" in keys and row["version"] else 1,
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            created_at=row["created_at"] if "created_at" in keys and row["created_at"] else row["started_at"],
            updated_at=row["updated_at"] if "updated_at" in keys and row["updated_at"] else (row["finished_at"] or row["started_at"]),
        )

    def get_workflow_run(self, run_id: str) -> WorkflowRunRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._row_to_run(row) if row else None

    def list_workflow_runs(self, limit: int = 30) -> list[WorkflowRunRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_run(r) for r in rows]

    # ---------------- Policy Overrides ----------------
    def get_policy_overrides(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT policy_id, content_json FROM policy_overrides").fetchall()
            return {r["policy_id"]: json.loads(r["content_json"]) for r in rows}

    def save_policy_override(self, policy_id: str, content: dict[str, Any]) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO policy_overrides (policy_id, content_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    content_json = excluded.content_json,
                    updated_at = excluded.updated_at
                """,
                (policy_id, json.dumps(content, ensure_ascii=False), now, now),
            )

    # ---------------- v2 Audit Events, Decision Cards, Deliveries, Skill Assets, System Settings ----------------
    def append_audit_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor: str = "system",
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, actor, summary, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_type,
                    entity_id,
                    event_type,
                    actor,
                    summary,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_audit_events(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if entity_type and entity_id:
                rows = conn.execute(
                    "SELECT * FROM audit_events WHERE entity_type = ? AND entity_id = ? ORDER BY seq DESC LIMIT ?",
                    (entity_type, entity_id, limit),
                ).fetchall()
            elif entity_type:
                rows = conn.execute(
                    "SELECT * FROM audit_events WHERE entity_type = ? ORDER BY seq DESC LIMIT ?",
                    (entity_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}")
                out.append(d)
            return out

    def save_decision_card(
        self,
        *,
        card_id: str,
        run_id: str,
        kind: str,
        state: str,
        title: str,
        body_markdown: str,
        options: list[str] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        closed_at = now if state == "closed" else None
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO decision_cards (
                    card_id, run_id, kind, state, title, body_markdown, options_json,
                    version, created_at, updated_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(card_id) DO UPDATE SET
                    state = excluded.state,
                    title = excluded.title,
                    body_markdown = excluded.body_markdown,
                    options_json = excluded.options_json,
                    version = decision_cards.version + 1,
                    updated_at = excluded.updated_at,
                    closed_at = COALESCE(excluded.closed_at, decision_cards.closed_at)
                """,
                (
                    card_id,
                    run_id,
                    kind,
                    state,
                    title,
                    body_markdown,
                    json.dumps(options or [], ensure_ascii=False),
                    now,
                    now,
                    closed_at,
                ),
            )
            row = conn.execute("SELECT * FROM decision_cards WHERE card_id = ?", (card_id,)).fetchone()
            return dict(row) if row else {}

    def update_decision_card_state(
        self,
        run_id: str,
        *,
        state: str,
        selected_option: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE decision_cards
                SET state = ?,
                    selected_option = COALESCE(?, selected_option),
                    decided_at = CASE WHEN ? IN ('decided', 'done', 'closed') AND decided_at IS NULL THEN ? ELSE decided_at END,
                    closed_at = CASE WHEN ? IN ('done', 'closed') THEN ? ELSE closed_at END,
                    version = version + 1,
                    updated_at = ?
                WHERE run_id = ? AND kind = 'ask'
                """,
                (state, selected_option, state, now, state, now, now, run_id),
            )

    def record_card_delivery(
        self,
        *,
        delivery_id: str,
        card_id: str,
        channel: str,
        status: str,
        external_ref: str | None = None,
        last_error: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO card_deliveries (
                    delivery_id, card_id, channel, status, external_ref, tries, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(card_id, channel) DO UPDATE SET
                    status = excluded.status,
                    external_ref = COALESCE(excluded.external_ref, card_deliveries.external_ref),
                    tries = card_deliveries.tries + 1,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (delivery_id, card_id, channel, status, external_ref, last_error, now, now),
            )

    def upsert_skill_asset(
        self,
        *,
        skill_name: str,
        source_dir: str,
        description: str,
        origin: str = "manual",
        sha256: str = "",
        last_run_id: str | None = None,
        backup_path: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_assets (
                    skill_name, source_dir, description, origin, sha256, version,
                    last_run_id, backup_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(skill_name) DO UPDATE SET
                    source_dir = excluded.source_dir,
                    description = excluded.description,
                    origin = CASE WHEN excluded.origin != 'manual' THEN excluded.origin ELSE skill_assets.origin END,
                    sha256 = CASE WHEN excluded.sha256 != '' THEN excluded.sha256 ELSE skill_assets.sha256 END,
                    version = CASE WHEN excluded.sha256 != '' AND excluded.sha256 != skill_assets.sha256 THEN skill_assets.version + 1 ELSE skill_assets.version END,
                    last_run_id = COALESCE(excluded.last_run_id, skill_assets.last_run_id),
                    backup_path = COALESCE(excluded.backup_path, skill_assets.backup_path),
                    updated_at = excluded.updated_at
                """,
                (skill_name, source_dir, description, origin, sha256, last_run_id, backup_path, now, now),
            )

    def list_skill_assets(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM skill_assets ORDER BY updated_at DESC, skill_name ASC").fetchall()
            return [dict(r) for r in rows]

    def set_system_setting(
        self,
        key: str,
        category: str,
        value: Any,
        *,
        updated_by: str = "user",
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO system_settings (key, category, value_json, updated_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    category = excluded.category,
                    value_json = excluded.value_json,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (key, category, json.dumps(value, ensure_ascii=False), updated_by, now, now),
            )

    def list_system_settings(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM system_settings ORDER BY category, key").fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["value"] = json.loads(d.pop("value_json", "null"))
                out.append(d)
            return out

    def delete_policy_override(self, policy_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM policy_overrides WHERE policy_id = ?", (policy_id,))

    # ---------------- Knowledge FTS5 ----------------
    def rebuild_knowledge_fts(self, docs: list[dict[str, str]]) -> int:
        with self.connect() as conn:
            conn.execute("DELETE FROM knowledge_fts")
            conn.executemany(
                """
                INSERT INTO knowledge_fts (
                    doc_path, title_index, description_index, body, body_index,
                    tags_index, project_id, page_type, status,
                    title_raw, description_raw, tags_raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        d["doc_path"],
                        d.get("title_index", d.get("title", "")),
                        d.get("description_index", d.get("description", "")),
                        d.get("body", ""),
                        d.get("body_index", d.get("search_text", "")),
                        d.get("tags_index", d.get("tags", "")),
                        d.get("project_id", ""),
                        d.get("page_type", ""),
                        d.get("status", ""),
                        d.get("title_raw", d.get("title", "")),
                        d.get("description_raw", d.get("description", "")),
                        d.get("tags_raw", d.get("tags", "")),
                    )
                    for d in docs
                ],
            )
        return len(docs)

    def search_knowledge_fts(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Chinese-capable search over formal Wiki cards.

        FTS5's unicode61 tokenizer treats a whole CJK run as one token, so the
        title/description/tags/body index columns store space-separated CJK
        bigrams while the ``*_raw`` columns keep the original text for display.
        Metadata carries a higher BM25 weight than body text.
        """
        term_plan = _knowledge_query_terms(query)
        if not term_plan.match_terms and not term_plan.like_terms:
            return []

        match_terms = term_plan.match_terms[:16]
        fts_query = " OR ".join('"' + term.replace('"', "") + '"' for term in match_terms)
        rows: list[sqlite3.Row] = []
        with self.connect() as conn:
            if match_terms:
                try:
                    rows = conn.execute(
                        """
                        SELECT doc_path, title_raw AS title, description_raw AS description,
                               body, tags_raw AS tags, project_id, page_type, status,
                               bm25(knowledge_fts,
                                    0.0, 8.0, 5.0, 0.0, 1.0, 4.0,
                                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0) AS score
                        FROM knowledge_fts
                        WHERE knowledge_fts MATCH ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (fts_query, max(limit * 6, 24)),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if len(rows) < limit and term_plan.like_terms:
                rows = list(rows) + self._search_knowledge_like(conn, term_plan.like_terms, limit * 6)

        required_overlap = 2 if len(term_plan.overlap_terms) >= 4 else 1
        scored: list[tuple[int, float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            haystack = "\n".join(
                str(item.get(key) or "") for key in ("title", "description", "body")
            ).casefold()
            overlap = sum(1 for term in term_plan.overlap_terms if term in haystack)
            if overlap < required_overlap:
                continue
            scored.append((overlap, float(item.pop("score", 0.0) or 0.0), item))
        scored.sort(key=lambda entry: (entry[0], -entry[1]), reverse=True)

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, __, item in scored:
            path = str(item.get("doc_path", ""))
            if not path or path in seen:
                continue
            seen.add(path)
            body = str(item.pop("body", "") or "")
            item["snippet"] = _knowledge_snippet(body, term_plan.overlap_terms)
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def _search_knowledge_like(
        self, conn: sqlite3.Connection, terms: list[str], limit: int
    ) -> list[sqlite3.Row]:
        clauses = []
        params: list[Any] = []
        for term in terms[:4]:
            clauses.append("(title_raw LIKE ? OR description_raw LIKE ? OR body LIKE ?)")
            pattern = f"%{term}%"
            params.extend([pattern, pattern, pattern])
        if not clauses:
            return []
        try:
            return conn.execute(
                f"""
                SELECT doc_path, title_raw AS title, description_raw AS description,
                       body, tags_raw AS tags, project_id, page_type, status, 0.0 AS score
                FROM knowledge_fts
                WHERE {' OR '.join(clauses)}
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    # ---------------- Knowledge card metrics (D6 + v3 freshness) ----------------
    def record_knowledge_surfaces(self, doc_paths: list[str], at: str) -> None:
        paths = [p for p in dict.fromkeys(doc_paths) if p]
        if not paths:
            return
        with self.connect() as conn:
            for path in paths:
                conn.execute(
                    """
                    INSERT INTO knowledge_card_metrics (doc_path, surface_count, last_surfaced_at, created_at, updated_at)
                    VALUES (?, 1, ?, ?, ?)
                    ON CONFLICT(doc_path) DO UPDATE SET
                        surface_count = knowledge_card_metrics.surface_count + 1,
                        last_surfaced_at = excluded.last_surfaced_at,
                        updated_at = excluded.updated_at
                    """,
                    (path, at, at, at),
                )

    def record_knowledge_usage(
        self, doc_paths: list[str], at: str, *, cited_paths: list[str] | None = None
    ) -> None:
        paths = [p for p in dict.fromkeys(doc_paths) if p]
        if not paths:
            return
        cited = {p for p in (cited_paths or []) if p}
        with self.connect() as conn:
            for path in paths:
                conn.execute(
                    """
                    INSERT INTO knowledge_card_metrics (doc_path, helped_count, cited_count, last_helped_at, created_at, updated_at)
                    VALUES (?, 1, ?, ?, ?, ?)
                    ON CONFLICT(doc_path) DO UPDATE SET
                        helped_count = knowledge_card_metrics.helped_count + 1,
                        cited_count = knowledge_card_metrics.cited_count + excluded.cited_count,
                        last_helped_at = excluded.last_helped_at,
                        updated_at = excluded.updated_at
                    """,
                    (path, 1 if path in cited else 0, at, at, at),
                )

    def upsert_knowledge_freshness(
        self,
        doc_path: str,
        *,
        freshness_tier: str = "evolving",
        last_verified_at: str | None = None,
        valid_until: str | None = None,
        freshness_state: str = "fresh",
    ) -> None:
        if not doc_path:
            return
        now = utc_now_iso()
        tier = freshness_tier if freshness_tier in {"volatile", "evolving", "evergreen"} else "evolving"
        verified = last_verified_at or shanghai_today_ymd()
        until = valid_until or compute_valid_until(tier, verified)
        state = freshness_state if freshness_state in {"fresh", "needs_recheck", "superseded", "archived"} else "fresh"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_card_metrics (
                    doc_path, freshness_tier, last_verified_at, valid_until, freshness_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_path) DO UPDATE SET
                    freshness_tier = excluded.freshness_tier,
                    last_verified_at = COALESCE(excluded.last_verified_at, knowledge_card_metrics.last_verified_at),
                    valid_until = COALESCE(excluded.valid_until, knowledge_card_metrics.valid_until),
                    freshness_state = excluded.freshness_state,
                    updated_at = excluded.updated_at
                """,
                (doc_path, tier, verified, until, state, now, now),
            )

    def refresh_knowledge_freshness(self, doc_paths: list[str], verified_date_ymd: str) -> list[str]:
        """Auto-renew last_verified_at & valid_until when real verification passes."""
        paths = [p for p in dict.fromkeys(doc_paths) if p]
        if not paths:
            return []
        now = utc_now_iso()
        refreshed: list[str] = []
        with self.connect() as conn:
            for path in paths:
                row = conn.execute(
                    "SELECT freshness_tier FROM knowledge_card_metrics WHERE doc_path = ?",
                    (path,),
                ).fetchone()
                tier = (row["freshness_tier"] if row and row["freshness_tier"] else "evolving")
                new_until = compute_valid_until(tier, verified_date_ymd)
                conn.execute(
                    """
                    INSERT INTO knowledge_card_metrics (
                        doc_path, freshness_tier, last_verified_at, valid_until, freshness_state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'fresh', ?, ?)
                    ON CONFLICT(doc_path) DO UPDATE SET
                        last_verified_at = excluded.last_verified_at,
                        valid_until = excluded.valid_until,
                        freshness_state = 'fresh',
                        updated_at = excluded.updated_at
                    """,
                    (path, tier, verified_date_ymd, new_until, now, now),
                )
                refreshed.append(path)
        return refreshed

    def mark_expired_knowledge_cards(self, today_ymd: str) -> list[dict[str, Any]]:
        """Mark fresh cards past valid_until as needs_recheck and return all cards currently needing recheck."""
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE knowledge_card_metrics
                SET freshness_state = 'needs_recheck', updated_at = ?
                WHERE valid_until IS NOT NULL AND valid_until != ''
                  AND valid_until < ?
                  AND freshness_state = 'fresh'
                """,
                (now, today_ymd),
            )
            rows = conn.execute(
                """
                SELECT doc_path, freshness_tier, last_verified_at, valid_until, freshness_state,
                       surface_count, helped_count
                FROM knowledge_card_metrics
                WHERE freshness_state = 'needs_recheck'
                   OR (valid_until IS NOT NULL AND valid_until != '' AND valid_until < ? AND freshness_state != 'archived')
                ORDER BY valid_until ASC
                """,
                (today_ymd,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_knowledge_freshness_map(self, doc_paths: list[str] | None = None) -> dict[str, dict[str, Any]]:
        today_ymd = shanghai_today_ymd()
        with self.connect() as conn:
            if doc_paths:
                placeholders = ",".join("?" for _ in doc_paths)
                rows = conn.execute(
                    f"""
                    SELECT doc_path, freshness_tier, last_verified_at, valid_until, freshness_state,
                           surface_count, helped_count
                    FROM knowledge_card_metrics
                    WHERE doc_path IN ({placeholders})
                    """,
                    tuple(doc_paths),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT doc_path, freshness_tier, last_verified_at, valid_until, freshness_state,
                           surface_count, helped_count
                    FROM knowledge_card_metrics
                    """
                ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            item = dict(r)
            valid_until = item.get("valid_until") or ""
            is_expired = bool(
                item.get("freshness_state") == "needs_recheck"
                or (valid_until and valid_until < today_ymd and item.get("freshness_state") not in {"archived", "superseded"})
            )
            item["is_expired"] = is_expired
            out[item["doc_path"]] = item
        return out

    def get_preflight_knowledge_pointers(self, receipt_id: str) -> list[str]:
        if not receipt_id:
            return []
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM preflight_receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        if not row:
            return []
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return []
        pointers = payload.get("knowledge_pointers") or []
        return [str(p) for p in pointers]

    def get_knowledge_card_metrics(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT doc_path, surface_count, cited_count, helped_count,
                       last_surfaced_at, last_helped_at,
                       freshness_tier, last_verified_at, valid_until, freshness_state
                FROM knowledge_card_metrics
                ORDER BY helped_count DESC, surface_count DESC, doc_path
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------------- v3 Mistake Observation Pool ----------------
    def upsert_mistake_observation(
        self,
        *,
        root_cause_key: str,
        title: str,
        severity_grade: str = "yellow_caught",
        phenomenon: str = "",
        truth: str = "",
        thinking_root_cause: str = "",
        caught_by: str = "self",
        session_key: str = "",
    ) -> tuple[dict[str, Any], bool]:
        key = re.sub(r"[^a-z0-9\-_]+", "-", root_cause_key.strip().lower()).strip("-") or f"obs-{uuid.uuid4().hex[:8]}"
        grade = severity_grade if severity_grade in {"red_silent", "yellow_caught", "green_minor"} else "yellow_caught"
        catcher = caught_by if caught_by in {"user", "tool", "self"} else "self"
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM mistake_observations WHERE root_cause_key = ?",
                (key,),
            ).fetchone()
            if existing:
                sessions = json.loads(existing["session_keys_json"] or "[]")
                new_count = int(existing["occurrence_count"])
                if session_key and session_key not in sessions:
                    sessions.append(session_key)
                    new_count += 1
                elif not session_key:
                    new_count += 1
                # Upgrade severity if a red_silent occurrence arrives
                merged_grade = existing["severity_grade"]
                rank = {"green_minor": 0, "yellow_caught": 1, "red_silent": 2}
                if rank.get(grade, 1) > rank.get(merged_grade, 1):
                    merged_grade = grade
                conn.execute(
                    """
                    UPDATE mistake_observations
                    SET severity_grade = ?,
                        title = CASE WHEN ? != '' THEN ? ELSE title END,
                        phenomenon = CASE WHEN ? != '' THEN ? ELSE phenomenon END,
                        truth = CASE WHEN ? != '' THEN ? ELSE truth END,
                        thinking_root_cause = CASE WHEN ? != '' THEN ? ELSE thinking_root_cause END,
                        caught_by = ?,
                        occurrence_count = ?,
                        session_keys_json = ?,
                        version = version + 1,
                        updated_at = ?
                    WHERE obs_id = ?
                    """,
                    (
                        merged_grade,
                        title.strip(),
                        title.strip(),
                        phenomenon.strip(),
                        phenomenon.strip(),
                        truth.strip(),
                        truth.strip(),
                        thinking_root_cause.strip(),
                        thinking_root_cause.strip(),
                        catcher,
                        new_count,
                        json.dumps(sessions, ensure_ascii=False),
                        now,
                        existing["obs_id"],
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM mistake_observations WHERE obs_id = ?",
                    (existing["obs_id"],),
                ).fetchone()
                return self._row_to_observation(row), False

            obs_id = f"obs-{uuid.uuid4().hex[:10]}"
            sessions = [session_key] if session_key else []
            conn.execute(
                """
                INSERT INTO mistake_observations (
                    obs_id, root_cause_key, severity_grade, title, phenomenon, truth,
                    thinking_root_cause, caught_by, occurrence_count, session_keys_json,
                    status, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'observing', 1, ?, ?)
                """,
                (
                    obs_id,
                    key,
                    grade,
                    title.strip() or key,
                    phenomenon.strip(),
                    truth.strip(),
                    thinking_root_cause.strip(),
                    catcher,
                    json.dumps(sessions, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM mistake_observations WHERE obs_id = ?",
                (obs_id,),
            ).fetchone()
            return self._row_to_observation(row), True

    @staticmethod
    def _row_to_observation(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["session_keys"] = json.loads(d.pop("session_keys_json", "[]") or "[]")
        return d

    def get_mistake_observation(self, obs_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM mistake_observations WHERE obs_id = ?", (obs_id,)).fetchone()
            return self._row_to_observation(row) if row else None

    def list_mistake_observations(
        self,
        *,
        status: str | None = None,
        severity_grade: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity_grade:
            clauses.append("severity_grade = ?")
            params.append(severity_grade)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM mistake_observations
                {where}
                ORDER BY
                    CASE severity_grade WHEN 'red_silent' THEN 0 WHEN 'yellow_caught' THEN 1 ELSE 2 END ASC,
                    occurrence_count DESC,
                    updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [self._row_to_observation(r) for r in rows]

    def update_mistake_observation_status(
        self,
        obs_id: str,
        status: str,
        *,
        promoted_rule_id: str | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE mistake_observations
                SET status = ?,
                    promoted_rule_id = COALESCE(?, promoted_rule_id),
                    version = version + 1,
                    updated_at = ?
                WHERE obs_id = ?
                """,
                (status, promoted_rule_id, now, obs_id),
            )
            row = conn.execute("SELECT * FROM mistake_observations WHERE obs_id = ?", (obs_id,)).fetchone()
            return self._row_to_observation(row) if row else None

    # ---------------- v3 Dissent & Counter-Evidence Records ----------------
    def add_dissent_record(
        self,
        *,
        target_type: str = "rule",
        target_id: str,
        counter_claim: str,
        condition_scope: str = "",
        evidence_pointer: str = "",
        session_key: str = "",
    ) -> dict[str, Any]:
        ttype = target_type if target_type in {"rule", "knowledge_doc", "skill"} else "rule"
        claim = counter_claim.strip()
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM dissent_records
                WHERE target_type = ? AND target_id = ? AND counter_claim = ? AND status = 'open'
                """,
                (ttype, target_id, claim),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE dissent_records
                    SET condition_scope = CASE WHEN ? != '' THEN ? ELSE condition_scope END,
                        evidence_pointer = CASE WHEN ? != '' THEN ? ELSE evidence_pointer END,
                        updated_at = ?
                    WHERE dissent_id = ?
                    """,
                    (
                        condition_scope.strip(),
                        condition_scope.strip(),
                        evidence_pointer.strip(),
                        evidence_pointer.strip(),
                        now,
                        existing["dissent_id"],
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM dissent_records WHERE dissent_id = ?",
                    (existing["dissent_id"],),
                ).fetchone()
                return dict(row)

            dissent_id = f"dis-{uuid.uuid4().hex[:10]}"
            conn.execute(
                """
                INSERT INTO dissent_records (
                    dissent_id, target_type, target_id, counter_claim, condition_scope,
                    evidence_pointer, session_key, status, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)
                """,
                (
                    dissent_id,
                    ttype,
                    target_id,
                    claim,
                    condition_scope.strip(),
                    evidence_pointer.strip(),
                    session_key.strip(),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM dissent_records WHERE dissent_id = ?", (dissent_id,)).fetchone()
            return dict(row)

    def list_dissent_records(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if target_id:
            clauses.append("target_id = ?")
            params.append(target_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM dissent_records {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_open_dissents_by_targets(self, target_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        ids = [t for t in dict.fromkeys(target_ids) if t]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM dissent_records
                WHERE status = 'open' AND target_id IN ({placeholders})
                ORDER BY created_at DESC
                """,
                tuple(ids),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            d = dict(r)
            grouped.setdefault(d["target_id"], []).append(d)
        return grouped

    def update_dissent_status(self, dissent_id: str, status: str) -> dict[str, Any] | None:
        if status not in {"open", "merged", "dismissed"}:
            return None
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE dissent_records
                SET status = ?, version = version + 1, updated_at = ?
                WHERE dissent_id = ?
                """,
                (status, now, dissent_id),
            )
            row = conn.execute("SELECT * FROM dissent_records WHERE dissent_id = ?", (dissent_id,)).fetchone()
            return dict(row) if row else None

    # ---------------- v3 Skill Usage & Health ----------------
    def record_skill_usage(self, skill_names: list[str], *, passed: bool, at: str | None = None) -> None:
        names = [s.strip() for s in dict.fromkeys(skill_names) if s and s.strip()]
        if not names:
            return
        now = at or utc_now_iso()
        with self.connect() as conn:
            for name in names:
                existing = conn.execute(
                    "SELECT skill_name, fail_count FROM skill_assets WHERE skill_name = ?",
                    (name,),
                ).fetchone()
                if existing:
                    new_fails = int(existing["fail_count"] or 0) + (0 if passed else 1)
                    health = "failing" if new_fails >= 3 else "active"
                    conn.execute(
                        """
                        UPDATE skill_assets
                        SET invoke_count = invoke_count + 1,
                            verified_pass_count = verified_pass_count + ?,
                            fail_count = ?,
                            last_used_at = ?,
                            health_state = ?,
                            updated_at = ?
                        WHERE skill_name = ?
                        """,
                        (1 if passed else 0, new_fails, now, health, now, name),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO skill_assets (
                            skill_name, source_dir, description, origin, sha256, version,
                            invoke_count, verified_pass_count, fail_count, last_used_at,
                            health_state, created_at, updated_at
                        ) VALUES (?, '', '', 'observed', '', 1, 1, ?, ?, ?, 'active', ?, ?)
                        """,
                        (name, 1 if passed else 0, 0 if passed else 1, now, now, now),
                    )

    # ---------------- v3 Rule Auto-Dormancy & Growth Metrics ----------------
    def mark_dormant_stale_rules(self, *, days: int = 14) -> list[dict[str, Any]]:
        """Auto-demote unverified_active rules older than `days` with hit_count == 0 to dormant."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        now = utc_now_iso()
        demoted: list[dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT rule_id, title, created_at FROM rules
                WHERE status = ? AND hit_count = 0 AND created_at < ?
                """,
                (RuleStatus.UNVERIFIED_ACTIVE.value, cutoff),
            ).fetchall()
            for r in rows:
                conn.execute(
                    """
                    UPDATE rules
                    SET status = ?, dormant_at = ?, updated_at = ?
                    WHERE rule_id = ?
                    """,
                    (RuleStatus.DORMANT.value, now, now, r["rule_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO audit_events (entity_type, entity_id, event_type, actor, summary, payload_json, created_at)
                    VALUES ('rule', ?, 'auto_dormant', 'dream', ?, ?, ?)
                    """,
                    (
                        r["rule_id"],
                        f"试运行规则超过 {days} 天零命中，自动转入休眠：{r['title']}",
                        json.dumps({"days_threshold": days, "created_at": r["created_at"]}, ensure_ascii=False),
                        now,
                    ),
                )
                demoted.append({"rule_id": r["rule_id"], "title": r["title"], "created_at": r["created_at"]})
        return demoted

    def save_metrics_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        period_key = str(snapshot["period_key"])
        snap_id = snapshot.get("snapshot_id") or f"snap-{period_key}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO flywheel_metrics_snapshots (
                    snapshot_id, period_key, red_count, yellow_count, green_count,
                    repeat_mistake_rate, self_catch_rate, active_rule_count, dormant_rule_count,
                    open_dissent_count, expired_card_count, stale_skill_count, sample_sufficient,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_key) DO UPDATE SET
                    red_count = excluded.red_count,
                    yellow_count = excluded.yellow_count,
                    green_count = excluded.green_count,
                    repeat_mistake_rate = excluded.repeat_mistake_rate,
                    self_catch_rate = excluded.self_catch_rate,
                    active_rule_count = excluded.active_rule_count,
                    dormant_rule_count = excluded.dormant_rule_count,
                    open_dissent_count = excluded.open_dissent_count,
                    expired_card_count = excluded.expired_card_count,
                    stale_skill_count = excluded.stale_skill_count,
                    sample_sufficient = excluded.sample_sufficient,
                    updated_at = excluded.updated_at
                """,
                (
                    snap_id,
                    period_key,
                    int(snapshot.get("red_count", 0)),
                    int(snapshot.get("yellow_count", 0)),
                    int(snapshot.get("green_count", 0)),
                    float(snapshot.get("repeat_mistake_rate", 0.0)),
                    float(snapshot.get("self_catch_rate", 0.0)),
                    int(snapshot.get("active_rule_count", 0)),
                    int(snapshot.get("dormant_rule_count", 0)),
                    int(snapshot.get("open_dissent_count", 0)),
                    int(snapshot.get("expired_card_count", 0)),
                    int(snapshot.get("stale_skill_count", 0)),
                    1 if snapshot.get("sample_sufficient") else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM flywheel_metrics_snapshots WHERE period_key = ?",
                (period_key,),
            ).fetchone()
            return dict(row) if row else {}

    def get_latest_metrics_snapshot(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM flywheel_metrics_snapshots ORDER BY period_key DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def list_metrics_snapshots(self, limit: int = 12) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM flywheel_metrics_snapshots ORDER BY period_key DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def compute_live_growth_metrics(self, today_ymd: str | None = None) -> dict[str, Any]:
        today = today_ymd or shanghai_today_ymd()
        stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
        with self.connect() as conn:
            obs_rows = conn.execute(
                "SELECT severity_grade, occurrence_count, caught_by, status FROM mistake_observations WHERE status != 'archived'"
            ).fetchall()
            red_count = sum(1 for r in obs_rows if r["severity_grade"] == "red_silent")
            yellow_count = sum(1 for r in obs_rows if r["severity_grade"] == "yellow_caught")
            green_count = sum(1 for r in obs_rows if r["severity_grade"] == "green_minor")
            total_obs = len(obs_rows)
            repeat_obs = sum(1 for r in obs_rows if int(r["occurrence_count"] or 1) >= 2)
            self_caught = sum(1 for r in obs_rows if r["caught_by"] in {"self", "tool"})

            active_rule_count = int(
                conn.execute(
                    "SELECT COUNT(1) AS c FROM rules WHERE status IN ('verified_active', 'unverified_active')"
                ).fetchone()["c"]
            )
            dormant_rule_count = int(
                conn.execute(
                    "SELECT COUNT(1) AS c FROM rules WHERE status = 'dormant'"
                ).fetchone()["c"]
            )
            open_dissent_count = int(
                conn.execute(
                    "SELECT COUNT(1) AS c FROM dissent_records WHERE status = 'open'"
                ).fetchone()["c"]
            )
            expired_card_count = int(
                conn.execute(
                    """
                    SELECT COUNT(1) AS c FROM knowledge_card_metrics
                    WHERE freshness_state = 'needs_recheck'
                       OR (valid_until IS NOT NULL AND valid_until != '' AND valid_until < ? AND freshness_state != 'archived')
                    """,
                    (today,),
                ).fetchone()["c"]
            )
            stale_skill_count = int(
                conn.execute(
                    """
                    SELECT COUNT(1) AS c FROM skill_assets
                    WHERE (origin = 'flywheel' AND invoke_count = 0 AND created_at < ?)
                       OR fail_count >= 3
                    """,
                    (stale_cutoff,),
                ).fetchone()["c"]
            )
            observing_pool_count = sum(1 for r in obs_rows if r["status"] == "observing")

        return {
            "red_count": red_count,
            "yellow_count": yellow_count,
            "green_count": green_count,
            "total_mistakes": total_obs,
            "observing_pool_count": observing_pool_count,
            "repeat_mistake_rate": round(repeat_obs / total_obs, 4) if total_obs > 0 else 0.0,
            "self_catch_rate": round(self_caught / total_obs, 4) if total_obs > 0 else 0.0,
            "active_rule_count": active_rule_count,
            "dormant_rule_count": dormant_rule_count,
            "open_dissent_count": open_dissent_count,
            "expired_card_count": expired_card_count,
            "stale_skill_count": stale_skill_count,
            "sample_sufficient": total_obs >= 5,
        }
