"""The distill-queue inventory: one row per revision / chunk of a session.

``sessions`` is the entity profile; ``distill_queue`` is the work inventory.
The unique ``(session_key, revision, chunk_index)`` triple lets the same
session be re-enqueued as a new revision or split into oversized chunks, and
each item carries its own retry counter, run attribution and exclusion reason.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from ai_flywheel.models import DistillQueueItem, IngressChannel, QueueStatus

#: Statuses after which a queue item never runs again.
TERMINAL_QUEUE_STATUSES = frozenset(
    {
        QueueStatus.PROCESSED.value,
        QueueStatus.EXCLUDED.value,
        QueueStatus.WITHDRAWN.value,
        QueueStatus.DEAD_LETTER.value,
    }
)


def _as_value(value: QueueStatus | IngressChannel | str) -> str:
    return value.value if isinstance(value, (QueueStatus, IngressChannel)) else str(value)


class QueueRepoMixin:
    """Queue item CRUD, prioritized claim queries, inventory summaries and trail writes."""

    # The triage priority order a queue claim uses, mirroring the session order.
    QUEUE_PRIORITY_ORDER_SQL = (
        "CASE priority "
        "WHEN 'P0_CORE' THEN 0 "
        "WHEN 'P1_SIGNAL' THEN 1 "
        "WHEN 'P1_GENERAL' THEN 2 "
        "WHEN 'P2_META' THEN 3 "
        "ELSE 9 END"
    )

    @staticmethod
    def _row_to_queue_item(row: sqlite3.Row) -> DistillQueueItem:
        keys = row.keys() if hasattr(row, "keys") else ()
        return DistillQueueItem(
            queue_id=row["queue_id"],
            session_key=row["session_key"],
            parent_session_key=str(row["parent_session_key"] or "") if "parent_session_key" in keys else "",
            is_subagent=bool(row["is_subagent"]) if "is_subagent" in keys else False,
            client=row["client"],
            project=row["project"],
            workspace=row["workspace"],
            title=row["title"],
            revision=int(row["revision"]),
            chunk_index=int(row["chunk_index"]),
            chunk_total=int(row["chunk_total"]),
            ingress_channel=IngressChannel(row["ingress_channel"]),
            status=QueueStatus(row["status"]),
            priority=row["priority"],
            exclude_reason_code=row["exclude_reason_code"],
            exclude_reason_detail=row["exclude_reason_detail"],
            focus_note=row["focus_note"],
            content_hash=row["content_hash"],
            total_chars=int(row["total_chars"]),
            compacted_chars=int(row["compacted_chars"]),
            is_oversized=bool(row["is_oversized"]),
            consumed_from_turn=int(row["consumed_from_turn"]),
            consumed_to_turn=int(row["consumed_to_turn"]),
            retry_count=int(row["retry_count"]),
            max_retries=int(row["max_retries"]),
            last_error=row["last_error"],
            run_id=row["run_id"],
            source_path=row["source_path"],
            payload=json.loads(row["payload_json"] or "{}"),
            enqueued_at=row["enqueued_at"],
            claimed_at=row["claimed_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def upsert_queue_item(self, item: DistillQueueItem) -> DistillQueueItem:
        """Insert or update a queue item by ``queue_id``.

        The ``(session_key, revision, chunk_index)`` uniqueness is left to the
        table: a second queue id for the same unit of work raises
        ``sqlite3.IntegrityError`` instead of silently duplicating it.
        """
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO distill_queue (
                    queue_id, session_key, parent_session_key, is_subagent,
                    client, project, workspace, title,
                    revision, chunk_index, chunk_total, ingress_channel, status,
                    priority, exclude_reason_code, exclude_reason_detail, focus_note,
                    content_hash, total_chars, compacted_chars, is_oversized,
                    consumed_from_turn, consumed_to_turn, retry_count, max_retries,
                    last_error, run_id, source_path, payload_json, enqueued_at,
                    claimed_at, completed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(queue_id) DO UPDATE SET
                    session_key = excluded.session_key,
                    parent_session_key = excluded.parent_session_key,
                    is_subagent = excluded.is_subagent,
                    client = excluded.client,
                    project = excluded.project,
                    workspace = excluded.workspace,
                    title = excluded.title,
                    revision = excluded.revision,
                    chunk_index = excluded.chunk_index,
                    chunk_total = excluded.chunk_total,
                    ingress_channel = excluded.ingress_channel,
                    status = excluded.status,
                    priority = excluded.priority,
                    exclude_reason_code = excluded.exclude_reason_code,
                    exclude_reason_detail = excluded.exclude_reason_detail,
                    focus_note = excluded.focus_note,
                    content_hash = excluded.content_hash,
                    total_chars = excluded.total_chars,
                    compacted_chars = excluded.compacted_chars,
                    is_oversized = excluded.is_oversized,
                    consumed_from_turn = excluded.consumed_from_turn,
                    consumed_to_turn = excluded.consumed_to_turn,
                    retry_count = excluded.retry_count,
                    max_retries = excluded.max_retries,
                    last_error = excluded.last_error,
                    run_id = excluded.run_id,
                    source_path = excluded.source_path,
                    payload_json = excluded.payload_json,
                    enqueued_at = excluded.enqueued_at,
                    claimed_at = excluded.claimed_at,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    item.queue_id,
                    item.session_key,
                    item.parent_session_key or "",
                    1 if item.is_subagent else 0,
                    item.client,
                    item.project,
                    item.workspace,
                    item.title,
                    item.revision,
                    item.chunk_index,
                    item.chunk_total,
                    item.ingress_channel.value,
                    item.status.value,
                    item.priority,
                    item.exclude_reason_code,
                    item.exclude_reason_detail,
                    item.focus_note,
                    item.content_hash,
                    item.total_chars,
                    item.compacted_chars,
                    1 if item.is_oversized else 0,
                    item.consumed_from_turn,
                    item.consumed_to_turn,
                    item.retry_count,
                    item.max_retries,
                    item.last_error,
                    item.run_id,
                    item.source_path,
                    json.dumps(item.payload or {}, ensure_ascii=False),
                    item.enqueued_at or now,
                    item.claimed_at,
                    item.completed_at,
                    item.created_at or now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM distill_queue WHERE queue_id = ?", (item.queue_id,)
            ).fetchone()
            return self._row_to_queue_item(row)

    def get_queue_item(self, queue_id: str) -> DistillQueueItem | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM distill_queue WHERE queue_id = ?", (queue_id,)
            ).fetchone()
            return self._row_to_queue_item(row) if row else None

    @staticmethod
    def _queue_filters(
        *,
        status: QueueStatus | str | None,
        client: str | None,
        ingress_channel: IngressChannel | str | None,
        project: str | None,
        session_key: str | None,
    ) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(_as_value(status))
        if client:
            where.append("client = ?")
            params.append(client)
        if ingress_channel:
            where.append("ingress_channel = ?")
            params.append(_as_value(ingress_channel))
        if project:
            where.append("project = ?")
            params.append(project)
        if session_key:
            where.append("session_key = ?")
            params.append(session_key)
        return where, params

    def list_queue_items(
        self,
        status: QueueStatus | str | None = None,
        client: str | None = None,
        ingress_channel: IngressChannel | str | None = None,
        project: str | None = None,
        session_key: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[DistillQueueItem]:
        where, params = self._queue_filters(
            status=status,
            client=client,
            ingress_channel=ingress_channel,
            project=project,
            session_key=session_key,
        )
        sql = "SELECT * FROM distill_queue"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY enqueued_at DESC, queue_id ASC LIMIT ? OFFSET ?"
        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        with self.connect() as conn:
            rows = conn.execute(sql, [*params, safe_limit, safe_offset]).fetchall()
            return [self._row_to_queue_item(row) for row in rows]

    def count_queue_items(
        self,
        status: QueueStatus | str | None = None,
        client: str | None = None,
        ingress_channel: IngressChannel | str | None = None,
        project: str | None = None,
        session_key: str | None = None,
    ) -> int:
        where, params = self._queue_filters(
            status=status,
            client=client,
            ingress_channel=ingress_channel,
            project=project,
            session_key=session_key,
        )
        sql = "SELECT COUNT(*) AS cnt FROM distill_queue"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row["cnt"]) if row else 0

    def list_active_queue_session_keys(self) -> set[str]:
        """Return the deduplicated set of session_keys currently in active queue states (queued/processing)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT session_key FROM distill_queue WHERE status IN (?, ?)",
                (QueueStatus.QUEUED.value, QueueStatus.PROCESSING.value),
            ).fetchall()
            return {str(row["session_key"]) for row in rows if row["session_key"]}

    def list_queued_items_prioritized(
        self, limit: int = 50, *, include_subagents: bool = False
    ) -> list[DistillQueueItem]:
        """Queued items ordered by triage priority, then oldest first.

        The ordering lives in SQL, not in Python: sorting a recency-limited page
        in memory would starve the high-priority items outside it.
        By default, subagents are excluded from automatic batch claims.
        """
        subagent_filter = "" if include_subagents else " AND is_subagent = 0"
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM distill_queue
                WHERE status = ?{subagent_filter}
                ORDER BY {self.QUEUE_PRIORITY_ORDER_SQL} ASC, enqueued_at ASC
                LIMIT ?
                """,
                (QueueStatus.QUEUED.value, max(1, int(limit))),
            ).fetchall()
            return [self._row_to_queue_item(row) for row in rows]

    def get_latest_queue_revision(self, session_key: str) -> int:
        """The highest revision ever enqueued for a session (0 when none)."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(revision) AS max_rev FROM distill_queue WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            return int(row["max_rev"]) if row and row["max_rev"] is not None else 0

    def list_session_queue_items(
        self, session_key: str, statuses: list[QueueStatus | str] | None = None
    ) -> list[DistillQueueItem]:
        """Every queue item of one session, newest revision first, chunks in order."""
        sql = "SELECT * FROM distill_queue WHERE session_key = ?"
        params: list[Any] = [session_key]
        if statuses:
            values = [_as_value(status) for status in statuses]
            placeholders = ",".join("?" for _ in values)
            sql += f" AND status IN ({placeholders})"
            params.extend(values)
        sql += " ORDER BY revision DESC, chunk_index ASC"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_queue_item(row) for row in rows]

    def update_queue_item_status(
        self,
        queue_id: str,
        status: QueueStatus,
        *,
        run_id: str | None = None,
        exclude_reason_code: str | None = None,
        exclude_reason_detail: str | None = None,
        last_error: str | None = None,
        increment_retry: bool = False,
        reset_retry: bool = False,
        clear_run_id: bool = False,
        actor: str = "system",
        reason_code: str = "",
        summary: str = "",
        record_audit: bool = True,
    ) -> DistillQueueItem | None:
        """Transition one queue item and write the structured trail in the same transaction.

        The audit row records ``from_status`` / ``to_status`` / ``reason_code``
        on ``audit_events``, so every entity's state history can be queried the
        same way without a per-domain history table.

        ``run_id`` binds the item to the claiming run; ``clear_run_id`` releases
        that binding when a batch returns the item to the queue.
        """
        now = self._now_iso()
        new_status = status.value
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM distill_queue WHERE queue_id = ?", (queue_id,)
            ).fetchone()
            if row is None:
                return None
            old_status = str(row["status"])
            retry_count = int(row["retry_count"])
            if reset_retry:
                retry_count = 0
            elif increment_retry:
                retry_count += 1
            claimed_at = row["claimed_at"]
            if new_status == QueueStatus.PROCESSING.value and not claimed_at:
                claimed_at = now
            completed_at = row["completed_at"]
            if new_status in TERMINAL_QUEUE_STATUSES and not completed_at:
                completed_at = now
            conn.execute(
                """
                UPDATE distill_queue SET
                    status = ?,
                    run_id = CASE WHEN ? THEN NULL ELSE COALESCE(?, run_id) END,
                    exclude_reason_code = COALESCE(?, exclude_reason_code),
                    exclude_reason_detail = COALESCE(?, exclude_reason_detail),
                    last_error = COALESCE(?, last_error),
                    retry_count = ?,
                    claimed_at = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE queue_id = ?
                """,
                (
                    new_status,
                    1 if clear_run_id else 0,
                    run_id,
                    exclude_reason_code,
                    exclude_reason_detail,
                    last_error,
                    retry_count,
                    claimed_at,
                    completed_at,
                    now,
                    queue_id,
                ),
            )
            if record_audit:
                self._insert_audit_event(
                    conn,
                    entity_type="queue_item",
                    entity_id=queue_id,
                    event_type="status_changed",
                    actor=actor,
                    summary=summary or f"{old_status} -> {new_status}",
                    payload={
                        "run_id": None if clear_run_id else (run_id or row["run_id"]),
                        "retry_count": retry_count,
                    },
                    from_status=old_status,
                    to_status=new_status,
                    reason_code=reason_code or (exclude_reason_code or ""),
                )
            updated = conn.execute(
                "SELECT * FROM distill_queue WHERE queue_id = ?", (queue_id,)
            ).fetchone()
            return self._row_to_queue_item(updated)

    def update_queue_item_metadata(
        self,
        queue_id: str,
        *,
        title: str | None = None,
        workspace: str | None = None,
        parent_session_key: str | None = None,
        is_subagent: bool | None = None,
        payload: dict[str, Any] | None = None,
    ) -> DistillQueueItem | None:
        """Update mutable display/hierarchy metadata on a queue item in place."""
        now = self._now_iso()
        fields: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]
        if title is not None:
            fields.append("title = ?")
            params.append(title)
        if workspace is not None:
            fields.append("workspace = ?")
            params.append(workspace)
        if parent_session_key is not None:
            fields.append("parent_session_key = ?")
            params.append(parent_session_key)
        if is_subagent is not None:
            fields.append("is_subagent = ?")
            params.append(1 if is_subagent else 0)
        if payload is not None:
            fields.append("payload_json = ?")
            params.append(json.dumps(payload, ensure_ascii=False))
        params.append(queue_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE distill_queue SET {', '.join(fields)} WHERE queue_id = ?",
                params,
            )
            row = conn.execute(
                "SELECT * FROM distill_queue WHERE queue_id = ?", (queue_id,)
            ).fetchone()
            return self._row_to_queue_item(row) if row else None

    def summarize_queue(self, max_sessions_per_run: int = 10) -> dict[str, Any]:
        """Inventory totals: per status, per client (by status) and per channel.

        ``estimated_batches`` is how many batches of ``max_sessions_per_run``
        the queued backlog would need: the number the overview page shows so a
        backlog of 200 does not read like "about one run".
        """
        by_status: dict[str, int] = {status.value: 0 for status in QueueStatus}
        by_channel: dict[str, int] = {channel.value: 0 for channel in IngressChannel}
        by_client: dict[str, dict[str, int]] = {}
        total = 0
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, client, ingress_channel, COUNT(*) AS cnt
                FROM distill_queue
                GROUP BY status, client, ingress_channel
                """
            ).fetchall()
        for row in rows:
            count = int(row["cnt"])
            status = str(row["status"])
            client = str(row["client"])
            channel = str(row["ingress_channel"])
            total += count
            if status in by_status:
                by_status[status] += count
            if channel in by_channel:
                by_channel[channel] += count
            bucket = by_client.setdefault(
                client, {item: 0 for item in by_status} | {"total": 0}
            )
            if status in bucket:
                bucket[status] += count
            bucket["total"] += count
        queued = by_status[QueueStatus.QUEUED.value]
        per_run = max(1, int(max_sessions_per_run))
        return {
            "total": total,
            "by_status": by_status,
            "by_client": by_client,
            "by_channel": by_channel,
            "estimated_batches": math.ceil(queued / per_run) if queued > 0 else 0,
        }
