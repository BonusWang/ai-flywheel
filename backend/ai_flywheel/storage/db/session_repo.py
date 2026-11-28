"""Sessions, the retro queue, outcomes, preflight receipts and retry counters."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ai_flywheel.models import (
    OutcomeCheck,
    OutcomeOrigin,
    OutcomeRecord,
    OutcomeStatus,
    RetroItem,
    RetroStatus,
    SessionRecord,
    SessionStatus,
    SignalKind,
)
from ai_flywheel.storage.db.schema import _load_json_list, _load_json_map


class SessionRepoMixin:
    """Sessions, the retro queue, outcomes, preflight receipts and retry counters."""


    # ---------------- Sessions ----------------
    # A session in one of these states is owned by the flywheel: a late lifecycle hook
    # must not re-open it or wipe the triage metadata that decided its fate.
    LIFECYCLE_PROTECTED_STATUSES = frozenset(
        {
            SessionStatus.IN_PROGRESS.value,
            SessionStatus.PROCESSED.value,
            SessionStatus.ARCHIVED.value,
        }
    )

    def upsert_session(
        self, record: SessionRecord, *, preserve_existing_lifecycle: bool = False
    ) -> SessionRecord:
        """Insert or update a session.

        With ``preserve_existing_lifecycle`` the write never changes the lifecycle of a
        session the flywheel already owns (``in_progress`` / ``processed`` / ``archived``):
        the status is kept, the tags are unioned and the metadata is merged (existing keys
        win, new ones are added). It is how a late ``Stop`` / ``SessionEnd`` /
        ``SessionStart`` hook updates a session without re-opening it.
        """
        now = self._now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT created_at, status, tags_json, metadata_json,
                       opt_out, opt_out_reason, latest_content_hash,
                       latest_turn_count, last_consumed_turn, last_consumed_hash
                FROM sessions WHERE session_key = ?
                """,
                (record.session_key,),
            ).fetchone()
            created_at = existing["created_at"] if existing else record.created_at
            status = record.status.value
            tags = list(record.tags)
            metadata = dict(record.metadata)
            if (
                preserve_existing_lifecycle
                and existing
                and existing["status"] in self.LIFECYCLE_PROTECTED_STATUSES
            ):
                status = existing["status"]
                tags = list(dict.fromkeys([*_load_json_list(existing["tags_json"]), *tags]))
                merged = _load_json_map(existing["metadata_json"])
                merged.update(metadata)
                metadata = merged
            elif existing and existing["status"] == SessionStatus.PROCESSED.value and record.content:
                # Re-open if substantial new content was pushed
                status = SessionStatus.PENDING.value
            # The opt-out is a user decision: a late lifecycle hook never undoes it.
            opt_out = bool(record.opt_out)
            opt_out_reason = record.opt_out_reason
            if preserve_existing_lifecycle and existing and existing["opt_out"]:
                opt_out = True
                opt_out_reason = existing["opt_out_reason"] or opt_out_reason
            # The fingerprint / watermark columns only move forward, so an
            # update that carries no content never erases them.
            latest_content_hash = record.latest_content_hash or (
                existing["latest_content_hash"] if existing else ""
            )
            latest_turn_count = max(
                int(record.latest_turn_count),
                int(existing["latest_turn_count"]) if existing else 0,
            )
            last_consumed_turn = max(
                int(record.last_consumed_turn),
                int(existing["last_consumed_turn"]) if existing else 0,
            )
            last_consumed_hash = record.last_consumed_hash or (
                existing["last_consumed_hash"] if existing else ""
            )
            conn.execute(
                """
                INSERT INTO sessions (
                    session_key, client, session_id, title, project, workspace,
                    transcript_path, summary, content, tags_json, priority, status,
                    metadata_json, opt_out, opt_out_reason, latest_content_hash,
                    latest_turn_count, last_consumed_turn, last_consumed_hash,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    opt_out = excluded.opt_out,
                    opt_out_reason = excluded.opt_out_reason,
                    latest_content_hash = excluded.latest_content_hash,
                    latest_turn_count = excluded.latest_turn_count,
                    last_consumed_turn = excluded.last_consumed_turn,
                    last_consumed_hash = excluded.last_consumed_hash,
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
                    json.dumps(tags, ensure_ascii=False),
                    record.priority,
                    status,
                    json.dumps(metadata, ensure_ascii=False),
                    1 if opt_out else 0,
                    opt_out_reason,
                    latest_content_hash,
                    latest_turn_count,
                    last_consumed_turn,
                    last_consumed_hash,
                    created_at,
                    now,
                ),
            )
        return self.get_session(record.session_key)  # type: ignore[return-value]

    def _row_to_session(self, row: sqlite3.Row) -> SessionRecord:
        keys = set(row.keys())
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
            opt_out=bool(row["opt_out"]) if "opt_out" in keys else False,
            opt_out_reason=row["opt_out_reason"] if "opt_out_reason" in keys else "",
            latest_content_hash=(
                row["latest_content_hash"] if "latest_content_hash" in keys else ""
            ),
            latest_turn_count=(
                int(row["latest_turn_count"]) if "latest_turn_count" in keys else 0
            ),
            last_consumed_turn=(
                int(row["last_consumed_turn"]) if "last_consumed_turn" in keys else 0
            ),
            last_consumed_hash=(
                row["last_consumed_hash"] if "last_consumed_hash" in keys else ""
            ),
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

    # The triage priority order P00 admits sessions by.
    PRIORITY_ORDER_SQL = (
        "CASE priority "
        "WHEN 'P0_CORE' THEN 0 "
        "WHEN 'P1_SIGNAL' THEN 1 "
        "WHEN 'P1_GENERAL' THEN 2 "
        "WHEN 'P2_META' THEN 3 "
        "ELSE 9 END"
    )

    def list_pending_sessions_prioritized(self, limit: int = 50) -> list[SessionRecord]:
        """Pending sessions ordered by triage priority (P0_CORE → P1_SIGNAL → P1_GENERAL → P2_META).

        The ordering lives in SQL, not in Python: sorting a recency-limited page in memory
        starves the high-priority sessions that fall outside it, so a P0_CORE session
        beyond the first rows would never be admitted.
        """
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM sessions
                WHERE status = ?
                ORDER BY {self.PRIORITY_ORDER_SQL} ASC, updated_at DESC
                LIMIT ?
                """,
                (SessionStatus.PENDING.value, limit),
            ).fetchall()
            return [self._row_to_session(r) for r in rows]

    def update_session_status(self, session_key: str, status: SessionStatus) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, updated_at = ? WHERE session_key = ?",
                (status.value, self._now_iso(), session_key),
            )

    def set_session_opt_out(
        self, session_key: str, opt_out: bool, reason: str = ""
    ) -> SessionRecord | None:
        """Record (or clear) a user's opt-out on the session profile."""
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET opt_out = ?, opt_out_reason = ?, updated_at = ?
                WHERE session_key = ?
                """,
                (1 if opt_out else 0, reason if opt_out else "", self._now_iso(), session_key),
            )
        return self.get_session(session_key)

    def update_session_watermark(
        self,
        session_key: str,
        *,
        latest_content_hash: str | None = None,
        latest_turn_count: int | None = None,
        last_consumed_turn: int | None = None,
        last_consumed_hash: str | None = None,
    ) -> None:
        """Move the content fingerprint / consumption watermark of a session.

        Only the arguments that are not ``None`` are written, so a caller can
        update the consumed pointer without restating the latest fingerprint.
        """
        assignments: list[str] = []
        params: list[Any] = []
        if latest_content_hash is not None:
            assignments.append("latest_content_hash = ?")
            params.append(latest_content_hash)
        if latest_turn_count is not None:
            assignments.append("latest_turn_count = ?")
            params.append(int(latest_turn_count))
        if last_consumed_turn is not None:
            assignments.append("last_consumed_turn = ?")
            params.append(int(last_consumed_turn))
        if last_consumed_hash is not None:
            assignments.append("last_consumed_hash = ?")
            params.append(last_consumed_hash)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        params.append(self._now_iso())
        params.append(session_key)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE session_key = ?",
                params,
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
                    self._now_iso(),
                ),
            )

    def count_preflights(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM preflight_receipts").fetchone()
            return int(row["cnt"]) if row else 0

    # ---------------- Exact counts ----------------
    # The publication layer reports how many things there are. Counting a page
    # of rows caps the number at the page size, so the counts live in SQL: a
    # portal that says "20 sessions" when there are 200 is lying.

    def count_sessions(self, status: str | None = None) -> int:
        with self.connect() as conn:
            if status:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM sessions WHERE status = ?", (status,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM sessions").fetchone()
            return int(row["cnt"]) if row else 0

    def count_outcomes(self, origin: str | None = None) -> int:
        with self.connect() as conn:
            if origin:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM outcomes WHERE origin = ?", (origin,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM outcomes").fetchone()
            return int(row["cnt"]) if row else 0

    def count_real_verified_natural_outcomes(self) -> int:
        """Natural outcomes carrying at least one `real` check that passed.

        The check list lives in a JSON column, so the aggregate is computed by
        SQLite's JSON functions: reading the rows and counting them in Python
        would cap the number at the page size.
        """
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM outcomes o
                WHERE o.origin = ?
                  AND EXISTS (
                    SELECT 1 FROM json_each(o.checks_json) c
                    WHERE json_extract(c.value, '$.kind') = 'real'
                      AND json_extract(c.value, '$.result') = 'pass'
                  )
                """,
                (OutcomeOrigin.NATURAL.value,),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def save_outcome(self, record: OutcomeRecord) -> OutcomeRecord:
        now = self._now_iso()
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

    def count_retro_items(self, status: str | None = None) -> int:
        with self.connect() as conn:
            if status:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM retro_queue WHERE status = ?",
                    (status,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS cnt FROM retro_queue").fetchone()
            return int(row["cnt"]) if row else 0

    def get_retro_items_by_ids(self, retro_ids: list[str]) -> list[RetroItem]:
        ids = [rid for rid in dict.fromkeys(retro_ids) if rid]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM retro_queue WHERE retro_id IN ({placeholders}) ORDER BY created_at DESC",
                ids,
            ).fetchall()
            return [self._row_to_retro(r) for r in rows]

    def update_retro_status(self, retro_id: str, status: RetroStatus) -> RetroItem | None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE retro_queue SET status = ?, updated_at = ? WHERE retro_id = ?",
                (status.value, self._now_iso(), retro_id),
            )
            row = conn.execute("SELECT * FROM retro_queue WHERE retro_id = ?", (retro_id,)).fetchone()
            return self._row_to_retro(row) if row else None

    def bulk_update_retro_status(self, retro_ids: list[str], status: RetroStatus) -> int:
        ids = [rid for rid in dict.fromkeys(retro_ids) if rid]
        if not ids:
            return 0
        now = self._now_iso()
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE retro_queue
                SET status = ?, updated_at = ?
                WHERE retro_id IN ({placeholders})
                """,
                (status.value, now, *ids),
            )
            return int(cur.rowcount)

    def resolve_retros_for_sessions(self, session_keys: list[str], run_id: str) -> int:
        """Close the OPEN retro items of the given sessions and link them to the settling run.

        Returns how many items were closed. The retro items of sessions that were not
        settled stay open: they are still waiting for the next batch.
        """
        keys = [key for key in dict.fromkeys(session_keys) if key]
        if not keys:
            return 0
        placeholders = ",".join("?" for _ in keys)
        now = self._now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE retro_queue
                SET status = ?, linked_run_id = ?, updated_at = ?
                WHERE status = ? AND session_key IN ({placeholders})
                """,
                (RetroStatus.RESOLVED.value, run_id, now, RetroStatus.OPEN.value, *keys),
            )
            return int(cur.rowcount)

    # ---------------- Retry Counter ----------------
    def bump_retry_counter(self, counter_key: str) -> int:
        now = self._now_iso()
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
