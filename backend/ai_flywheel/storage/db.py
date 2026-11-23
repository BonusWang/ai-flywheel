from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
    title,
    body,
    tags
);
"""


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
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)

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
                    recurrence_count, revalidation_history_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    rule.created_at,
                    now,
                ),
            )
        return self.get_rule(rule.rule_id)  # type: ignore[return-value]

    def _row_to_rule(self, row: sqlite3.Row) -> RuleRecord:
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
        with self.connect() as conn:
            conn.execute(
                "UPDATE rules SET hit_count = hit_count + 1, updated_at = ? WHERE rule_id = ?",
                (utc_now_iso(), rule_id),
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
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO outcomes (
                    outcome_id, receipt_hash, session_key, client, preflight_receipt_id,
                    origin, status, summary, checks_json, applied_rule_ids_json,
                    artifacts_json, revalidation_effects_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    record.created_at,
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
                INSERT INTO retry_counters (counter_key, count, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(counter_key) DO UPDATE SET count = excluded.count, updated_at = excluded.updated_at
                """,
                (counter_key, new_count, now),
            )
            return new_count

    # ---------------- Workflow Runs & Lock ----------------
    def acquire_workflow_lock(self, run_id: str) -> bool:
        with self.connect() as conn:
            existing = conn.execute("SELECT run_id FROM workflow_lock WHERE lock_id = 1").fetchone()
            if existing:
                return False
            conn.execute(
                "INSERT INTO workflow_lock (lock_id, run_id, acquired_at) VALUES (1, ?, ?)",
                (run_id, utc_now_iso()),
            )
            return True

    def get_workflow_lock(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT run_id, acquired_at FROM workflow_lock WHERE lock_id = 1").fetchone()
            return dict(row) if row else None

    def release_workflow_lock(self, run_id: str | None = None) -> bool:
        with self.connect() as conn:
            if run_id:
                cur = conn.execute("DELETE FROM workflow_lock WHERE lock_id = 1 AND run_id = ?", (run_id,))
            else:
                cur = conn.execute("DELETE FROM workflow_lock WHERE lock_id = 1")
            return cur.rowcount > 0

    def save_workflow_run(self, run: WorkflowRunRecord) -> WorkflowRunRecord:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    run_id, trigger_source, phase, runner_mode, status,
                    admitted_sessions_json, nodes_json, planned_writes_json,
                    digest_markdown, error, lock_released, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status,
                    admitted_sessions_json = excluded.admitted_sessions_json,
                    nodes_json = excluded.nodes_json,
                    planned_writes_json = excluded.planned_writes_json,
                    digest_markdown = excluded.digest_markdown,
                    error = excluded.error,
                    lock_released = excluded.lock_released,
                    finished_at = excluded.finished_at
                """,
                (
                    run.run_id,
                    run.trigger,
                    run.phase,
                    run.runner_mode,
                    run.status.value,
                    json.dumps(run.admitted_sessions, ensure_ascii=False),
                    json.dumps([n.model_dump(mode="json") for n in run.nodes], ensure_ascii=False),
                    json.dumps([w.model_dump(mode="json") for w in run.planned_writes], ensure_ascii=False),
                    run.digest_markdown,
                    run.error,
                    1 if run.lock_released else 0,
                    run.started_at,
                    run.finished_at,
                ),
            )
        return run

    def _row_to_run(self, row: sqlite3.Row) -> WorkflowRunRecord:
        return WorkflowRunRecord(
            run_id=row["run_id"],
            trigger=row["trigger_source"],
            phase=row["phase"],
            runner_mode=row["runner_mode"],
            status=RunStatus(row["status"]),
            admitted_sessions=json.loads(row["admitted_sessions_json"]),
            nodes=[NodeExecution.model_validate(n) for n in json.loads(row["nodes_json"])],
            planned_writes=[PlannedWrite.model_validate(w) for w in json.loads(row["planned_writes_json"])],
            digest_markdown=row["digest_markdown"],
            error=row["error"],
            lock_released=bool(row["lock_released"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
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
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO policy_overrides (policy_id, content_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    content_json = excluded.content_json,
                    updated_at = excluded.updated_at
                """,
                (policy_id, json.dumps(content, ensure_ascii=False), utc_now_iso()),
            )

    def delete_policy_override(self, policy_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM policy_overrides WHERE policy_id = ?", (policy_id,))

    # ---------------- Knowledge FTS5 ----------------
    def rebuild_knowledge_fts(self, docs: list[dict[str, str]]) -> int:
        with self.connect() as conn:
            conn.execute("DELETE FROM knowledge_fts")
            conn.executemany(
                "INSERT INTO knowledge_fts (doc_path, title, body, tags) VALUES (?, ?, ?, ?)",
                [(d["doc_path"], d["title"], d["body"], d.get("tags", "")) for d in docs],
            )
        return len(docs)

    def search_knowledge_fts(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        tokens = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split() if len(t) >= 2]
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in tokens[:8])
        with self.connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT doc_path, title, snippet(knowledge_fts, 2, '[', ']', '...', 24) AS snippet
                    FROM knowledge_fts
                    WHERE knowledge_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return []
