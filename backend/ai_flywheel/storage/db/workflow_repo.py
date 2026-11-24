"""Workflow runs, the exclusive lock, and the atomic approval claim primitives."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from ai_flywheel.models import (
    NodeExecution,
    PlannedWrite,
    RunStatus,
    WorkflowRunRecord,
)
from ai_flywheel.storage.db.schema import (
    INTERNAL_RUN_TRIGGER,
    RunAlreadyFinalizedError,
    TERMINAL_RUN_STATUSES,
    to_shanghai_date,
)


class WorkflowRepoMixin:
    """Workflow runs, the exclusive lock and the approval claim primitives."""


    def acquire_workflow_lock(self, run_id: str, ttl_seconds: int = 3600, *, now: str | None = None) -> bool:
        """Acquire or refresh the single workflow lock via one conditional upsert.

        Succeeds when the lock is free, expired, has no expiry, or already belongs to
        ``run_id`` (refreshing expiry and heartbeat). Single-statement atomic; rowcount
        decides the outcome. First DML on the connection opens the transaction.
        """
        ts = self._now_iso(now)
        expires = (datetime.fromisoformat(ts) + timedelta(seconds=ttl_seconds)).isoformat()
        with self.write_tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO workflow_lock (lock_id, run_id, acquired_at, expires_at, heartbeat_at, created_at, updated_at)
                VALUES (1, :run_id, :now, :expires, :now, :now, :now)
                ON CONFLICT(lock_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at,
                    heartbeat_at = excluded.heartbeat_at,
                    created_at = workflow_lock.created_at,
                    updated_at = excluded.updated_at
                WHERE workflow_lock.run_id = excluded.run_id
                   OR workflow_lock.expires_at IS NULL
                   OR workflow_lock.expires_at <= :now
                """,
                {"run_id": run_id, "now": ts, "expires": expires},
            )
            return cur.rowcount == 1

    def renew_workflow_lock(self, run_id: str, *, now: str | None = None) -> bool:
        """Refresh expiry + heartbeat for a lock still owned by ``run_id``."""
        ts = self._now_iso(now)
        expires = (datetime.fromisoformat(ts) + timedelta(seconds=self._lock_ttl_seconds)).isoformat()
        with self.write_tx() as conn:
            cur = conn.execute(
                """
                UPDATE workflow_lock
                SET expires_at = ?, heartbeat_at = ?, updated_at = ?
                WHERE lock_id = 1 AND run_id = ?
                """,
                (expires, ts, ts, run_id),
            )
            return cur.rowcount == 1

    def get_workflow_lock(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT run_id, acquired_at, expires_at, heartbeat_at FROM workflow_lock WHERE lock_id = 1"
            ).fetchone()
            return dict(row) if row else None

    def release_workflow_lock(self, run_id: str | None = None) -> bool:
        with self.write_tx() as conn:
            if run_id:
                cur = conn.execute("DELETE FROM workflow_lock WHERE lock_id = 1 AND run_id = ?", (run_id,))
            else:
                cur = conn.execute("DELETE FROM workflow_lock WHERE lock_id = 1")
            return cur.rowcount > 0

    def claim_approval(
        self,
        run_id: str,
        *,
        approved_mask: list[bool],
        selected_option: str,
        source: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Atomically claim an awaiting-approval run for approval.

        One write transaction: conditional lock upsert (fresh heartbeat) -> conditional
        run transition AWAITING_APPROVAL -> RUNNING with approved flags persisted ->
        card decided (+option, version bump) -> audit event with source and whether the
        lock was continuously held. Any failed gate rolls the whole transaction back.
        """
        ts = self._now_iso(now)
        with self.write_tx() as conn:
            prior = conn.execute(
                "SELECT run_id, expires_at FROM workflow_lock WHERE lock_id = 1"
            ).fetchone()
            acquired = conn.execute(
                """
                INSERT INTO workflow_lock (lock_id, run_id, acquired_at, expires_at, heartbeat_at, created_at, updated_at)
                VALUES (1, :run_id, :now, :expires, :now, :now, :now)
                ON CONFLICT(lock_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at,
                    heartbeat_at = excluded.heartbeat_at,
                    created_at = workflow_lock.created_at,
                    updated_at = excluded.updated_at
                WHERE workflow_lock.run_id = excluded.run_id
                   OR workflow_lock.expires_at IS NULL
                   OR workflow_lock.expires_at <= :now
                """,
                {"run_id": run_id, "now": ts, "expires": (datetime.fromisoformat(ts) + timedelta(seconds=self._lock_ttl_seconds)).isoformat()},
            )
            if acquired.rowcount != 1:
                conn.execute("ROLLBACK")
                return {
                    "claimed": False,
                    "reason": "lock_busy",
                    "lock_run_id": dict(prior)["run_id"] if prior else None,
                    "lock_expires_at": dict(prior)["expires_at"] if prior else None,
                }

            row = conn.execute(
                "SELECT planned_writes_json FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError(f"Workflow run not found: {run_id}")
            writes = json.loads(row["planned_writes_json"] or "[]")
            if len(approved_mask) != len(writes):
                conn.execute("ROLLBACK")
                raise ValueError(
                    f"approved_mask length {len(approved_mask)} != planned writes {len(writes)} for {run_id}"
                )
            for i, w in enumerate(writes):
                w["approved"] = bool(approved_mask[i])
            claimed = conn.execute(
                """
                UPDATE workflow_runs
                SET status = 'RUNNING', planned_writes_json = ?, updated_at = ?
                WHERE run_id = ? AND status = 'AWAITING_APPROVAL'
                """,
                (json.dumps(writes, ensure_ascii=False), ts, run_id),
            )
            if claimed.rowcount != 1:
                conn.execute("ROLLBACK")
                return {"claimed": False, "reason": "not_awaiting"}

            lock_continuously_held = bool(
                prior is not None
                and prior["run_id"] == run_id
                and prior["expires_at"] is not None
                and prior["expires_at"] > ts
            )
            conn.execute(
                """
                UPDATE decision_cards
                SET state = 'decided', selected_option = ?, decided_at = COALESCE(decided_at, ?),
                    version = version + 1, updated_at = ?
                WHERE run_id = ? AND kind = 'ask'
                """,
                (selected_option, ts, ts, run_id),
            )
            conn.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, actor, summary, payload_json, created_at)
                VALUES ('workflow_run', ?, 'approved', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source,
                    f"人工批准放行（来源={source}）",
                    json.dumps(
                        {
                            "source": source,
                            # The truthful name: the lock was held by this very run and
                            # had not expired, so the claim continued it instead of
                            # taking it over from somebody else.
                            "lock_continuously_held": lock_continuously_held,
                            # The historical key, kept for backward compatibility: it
                            # carries the same value as lock_continuously_held.
                            "reacquired": lock_continuously_held,
                            "selected_option": selected_option,
                        },
                        ensure_ascii=False,
                    ),
                    ts,
                ),
            )
            return {
                "claimed": True,
                # True when this run held the lock continuously up to this claim.
                "lock_continuously_held": lock_continuously_held,
                # Legacy alias of lock_continuously_held; the old name reads as
                # "the lock had to be re-acquired", which is the opposite.
                "reacquired": lock_continuously_held,
                "expires_at": (datetime.fromisoformat(ts) + timedelta(seconds=self._lock_ttl_seconds)).isoformat(),
            }

    def claim_rejection(
        self,
        run_id: str,
        *,
        reason: str,
        source: str,
        nodes_json: str,
        digest_markdown: str,
        now: str | None = None,
    ) -> bool:
        """Atomically cancel an awaiting-approval run: CANCELLED + sessions back to pending
        + P04-P07 node snapshot + own lock row removed + card closed + audit, one transaction.
        Does not require the lock and never touches another run's lock row."""
        ts = self._now_iso(now)
        with self.write_tx() as conn:
            cur = conn.execute(
                """
                UPDATE workflow_runs
                SET status = 'CANCELLED', error = ?, finished_at = ?, lock_released = 1,
                    nodes_json = ?, digest_markdown = ?, updated_at = ?
                WHERE run_id = ? AND status = 'AWAITING_APPROVAL'
                """,
                (reason, ts, nodes_json, digest_markdown, ts, run_id),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                """
                UPDATE sessions SET status = 'pending', updated_at = ?
                WHERE session_key IN (SELECT session_key FROM run_sessions WHERE run_id = ?)
                """,
                (ts, run_id),
            )
            conn.execute("DELETE FROM workflow_lock WHERE lock_id = 1 AND run_id = ?", (run_id,))
            conn.execute(
                """
                UPDATE decision_cards
                SET state = 'closed', selected_option = '取消本次写入',
                    decided_at = COALESCE(decided_at, ?), closed_at = ?, version = version + 1, updated_at = ?
                WHERE run_id = ? AND kind = 'ask'
                """,
                (ts, ts, ts, run_id),
            )
            conn.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, actor, summary, payload_json, created_at)
                VALUES ('workflow_run', ?, 'rejected', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source,
                    f"取消本次写入并释放锁（来源={source}）：{reason}",
                    json.dumps({"source": source, "reason": reason}, ensure_ascii=False),
                    ts,
                ),
            )
            return True

    def finish_run_terminal(
        self,
        run_id: str,
        *,
        status: str,
        card_state: str,
        error: str | None = None,
        result_summary: str | None = None,
        failed_node: str | None = None,
        selected_option: str | None = None,
        source: str = "system",
        audit_event: tuple[str, str, dict[str, Any]] | None = None,
        now: str | None = None,
    ) -> bool:
        """Single conditional entry into a terminal state: status first, then card
        (state/result/failed node, version bump), optional audit event, and removal of
        the run's own lock row — all in one transaction. Idempotent: applying to an
        already-final run is a no-op returning False."""
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"Invalid terminal status: {status}")
        ts = self._now_iso(now)
        with self.write_tx() as conn:
            cur = conn.execute(
                """
                UPDATE workflow_runs
                SET status = ?, error = COALESCE(?, error), finished_at = ?, lock_released = 1, updated_at = ?
                WHERE run_id = ? AND status IN ('RUNNING', 'AWAITING_APPROVAL')
                """,
                (status, error, ts, ts, run_id),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                return False
            conn.execute("DELETE FROM workflow_lock WHERE lock_id = 1 AND run_id = ?", (run_id,))
            conn.execute(
                """
                UPDATE decision_cards
                SET state = ?,
                    selected_option = COALESCE(?, selected_option),
                    result_summary = COALESCE(?, result_summary),
                    failed_node = COALESCE(?, failed_node),
                    decided_at = COALESCE(decided_at, ?),
                    closed_at = CASE WHEN ? IN ('done', 'closed', 'failed') THEN ? ELSE closed_at END,
                    version = version + 1, updated_at = ?
                WHERE run_id = ? AND kind = 'ask'
                """,
                (card_state, selected_option, result_summary, failed_node, ts, card_state, ts, ts, run_id),
            )
            if audit_event is not None:
                event_type, summary, payload = audit_event
                conn.execute(
                    """
                    INSERT INTO audit_events (entity_type, entity_id, event_type, actor, summary, payload_json, created_at)
                    VALUES ('workflow_run', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        event_type,
                        source,
                        summary,
                        json.dumps(payload or {}, ensure_ascii=False),
                        ts,
                    ),
                )
            return True

    def reconcile_decision_cards(self, *, now: str | None = None) -> list[str]:
        """Reconcile ask-card state against run status (one guarded statement per rule,
        checking run status via EXISTS inside the UPDATE to avoid read skew). Returns
        the run_ids whose cards were corrected."""
        ts = self._now_iso(now)
        changed: list[str] = []
        rules: list[tuple[str, str]] = [
            ("AWAITING_APPROVAL", "open"),
            ("PASS", "done"),
            ("PARTIAL", "closed"),
            ("CANCELLED", "closed"),
            ("FAILED", "failed"),
        ]
        with self.write_tx() as conn:
            for run_status, card_state in rules:
                rows = conn.execute(
                    """
                    SELECT d.run_id FROM decision_cards d
                    JOIN workflow_runs r ON r.run_id = d.run_id
                    WHERE d.kind = 'ask' AND d.state <> ?
                      AND r.status = ?
                    """,
                    (card_state, run_status),
                ).fetchall()
                for r in rows:
                    rid: str = r["run_id"]
                    if card_state == "open":
                        cur = conn.execute(
                            """
                            UPDATE decision_cards SET state = 'open', decided_at = NULL, selected_option = NULL,
                                version = version + 1, updated_at = ?
                            WHERE run_id = ? AND kind = 'ask' AND state <> 'open'
                              AND EXISTS (SELECT 1 FROM workflow_runs WHERE run_id = ? AND status = 'AWAITING_APPROVAL')
                            """,
                            (ts, rid, rid),
                        )
                    else:
                        cur = conn.execute(
                            """
                            UPDATE decision_cards SET state = ?, version = version + 1, updated_at = ?
                            WHERE run_id = ? AND kind = 'ask' AND state <> ?
                              AND EXISTS (SELECT 1 FROM workflow_runs WHERE run_id = ? AND status = ?)
                            """,
                            (card_state, ts, rid, card_state, rid, run_status),
                        )
                    if cur.rowcount == 1:
                        changed.append(rid)
                        conn.execute(
                            """
                            INSERT INTO audit_events (entity_type, entity_id, event_type, actor, summary, payload_json, created_at)
                            VALUES ('decision_card', ?, 'card_state_corrected', 'reconciler', ?, ?, ?)
                            """,
                            (
                                f"card-{rid}",
                                f"提问卡状态与运行终态不符，已校正为 {card_state}",
                                json.dumps({"run_id": rid, "run_status": run_status, "card_state": card_state}, ensure_ascii=False),
                                ts,
                            ),
                        )
        return changed

    def list_stale_running_runs(self, *, now: str | None = None, stale_after_seconds: int = 300) -> list[dict[str, Any]]:
        """Runs in RUNNING whose executor is gone: no own lock row, or a NULL/stale
        heartbeat. Full scan by status (not bounded by recency)."""
        ts = self._now_iso(now)
        cutoff = (datetime.fromisoformat(ts) - timedelta(seconds=stale_after_seconds)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.run_id, r.started_at, r.admitted_sessions_json, r.nodes_json,
                       l.run_id AS lock_run_id, l.heartbeat_at
                FROM workflow_runs r
                LEFT JOIN workflow_lock l ON l.lock_id = 1 AND l.run_id = r.run_id
                WHERE r.status = 'RUNNING'
                  AND (l.lock_id IS NULL OR l.heartbeat_at IS NULL OR l.heartbeat_at < ?)
                """,
                (cutoff,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                d = dict(row)
                d["admitted_sessions"] = json.loads(d.pop("admitted_sessions_json", "[]") or "[]")
                d["nodes"] = json.loads(d.pop("nodes_json", "[]") or "[]")
                out.append(d)
            return out

    def save_workflow_run(self, run: WorkflowRunRecord) -> WorkflowRunRecord:
        now = self._now_iso()
        bdate = run.batch_date or to_shanghai_date(run.started_at)
        with self.write_tx() as conn:
            existing = conn.execute(
                "SELECT batch_seq, version, created_at, status FROM workflow_runs WHERE run_id = ?",
                (run.run_id,),
            ).fetchone()
            if existing:
                stored_status = existing["status"]
                if stored_status in TERMINAL_RUN_STATUSES and run.status.value != stored_status:
                    raise RunAlreadyFinalizedError(run.run_id, stored_status)
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

            # The admitted sessions are mirrored into `run_sessions`: the
            # cancellation claim reads them back to put the sessions on the queue
            # again. The node and write snapshots are NOT mirrored into
            # `workflow_nodes` / `planned_writes`: nothing reads them, and a
            # second copy of the same run drifts away from the truth.
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
        return run

    def _row_to_run(self, row: sqlite3.Row) -> WorkflowRunRecord:
        keys = set(row.keys())
        return WorkflowRunRecord(
            run_id=row["run_id"],
            batch_date=row["batch_date"] if "batch_date" in keys and row["batch_date"] else to_shanghai_date(row["started_at"]),
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
                "SELECT * FROM workflow_runs WHERE trigger_source <> ? ORDER BY started_at DESC LIMIT ?",
                (INTERNAL_RUN_TRIGGER, limit),
            ).fetchall()
            return [self._row_to_run(r) for r in rows]

    def count_workflow_runs(self) -> int:
        """How many runs the portal shows — every one of them, not one page.

        The internal trigger (the interactive self-check placeholder) is left
        out, exactly as `list_workflow_runs` leaves it out.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM workflow_runs WHERE trigger_source <> ?",
                (INTERNAL_RUN_TRIGGER,),
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def ensure_placeholder_run(self, run_id: str, *, phase: str = "check") -> None:
        """Create the workflow_runs row a decision card's foreign key points at.

        The interactive-mode self-check is kept in the decision-cards table under a fixed
        placeholder run id; this row makes that reference valid and is kept out of the
        run lists (its trigger source marks it as internal).
        """
        now = self._now_iso()
        with self.write_tx() as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (
                    run_id, trigger_source, phase, runner_mode, status, started_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'none', 'PASS', ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (run_id, INTERNAL_RUN_TRIGGER, phase, now, now, now),
            )
