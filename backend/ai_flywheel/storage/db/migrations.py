"""Schema creation and forward migrations.

Every migration is idempotent and runs on every start, so a database written by
an older build converges to the current column contract.
"""
from __future__ import annotations

import json
import shutil
import sqlite3

from ai_flywheel.storage.db.schema import (
    KNOWLEDGE_FTS_COLUMNS,
    SCHEMA_SQL,
    _KNOWLEDGE_FTS_SQL,
    to_shanghai_date,
)


class MigrationsMixin:
    """Schema creation and forward migrations, applied once per database.

    Every step is idempotent: the migrations run on every start, so a database
    that was written by an older build converges to the current contract.
    """

    def _init_schema(self) -> None:
        preexisting = self.db_path.exists() and self.db_path.stat().st_size > 0
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.executescript(SCHEMA_SQL)
            self._migrate_knowledge_fts(conn)
            self._migrate_to_v2(conn, preexisting=preexisting)
            self._migrate_to_v3(conn, preexisting=preexisting)
            self._migrate_approval_recovery(conn)
            # Bi-temporal rule lineage: additive columns only, so a database
            # written before lineage existed keeps every row it had.
            self._migrate_rule_lineage(conn)
            # The delivery ledger gets its managed-delivery columns here, not in
            # the Feishu layer: the schema must be the same whether or not the
            # Feishu channel is enabled at startup.
            self._ensure_outbox_columns(conn)
            # v4: the unified dictionary, the distill queue and the structured
            # state-transition columns of the one generic trail table.
            self._migrate_to_v4(conn, preexisting=preexisting)
            # The builtin dictionary is planted on every start, not only when the
            # migration first runs: a new builtin item reaches an existing database.
            self._seed_builtin_dicts(conn)

    @staticmethod
    def _to_shanghai_date(iso_str: str | None) -> str:
        return to_shanghai_date(iso_str)

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

        now = self._now_iso()
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

        now = self._now_iso()
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

    def _migrate_to_v4(self, conn: sqlite3.Connection, *, preexisting: bool) -> None:
        """The data foundation: dictionary table, distill queue and trail columns.

        Idempotent by construction: the tables and indexes come from
        ``SCHEMA_SQL`` with ``IF NOT EXISTS``, and every added column goes
        through ``_ensure_column``. A pre-existing (v3) database is first copied
        to ``<db>.bak-v3``; existing rows are never rewritten.
        """
        applied = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 4"
        ).fetchone()
        if applied:
            return

        # Auto-backup the existing v3 database before the v4 upgrade.
        if preexisting and self.db_path.exists():
            backup = self.db_path.with_name(f"{self.db_path.name}.bak-v3")
            if not backup.exists():
                try:
                    shutil.copy2(self.db_path, backup)
                except OSError:
                    pass

        now = self._now_iso()
        # The session profile: opt-out plus the content fingerprint / consumption watermark.
        self._ensure_column(conn, "sessions", "opt_out", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "sessions", "opt_out_reason", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "sessions", "latest_content_hash", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "sessions", "latest_turn_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "sessions", "last_consumed_turn", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "sessions", "last_consumed_hash", "TEXT NOT NULL DEFAULT ''")
        # The one generic trail table gains structured state transitions.
        self._ensure_column(conn, "audit_events", "from_status", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "audit_events", "to_status", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "audit_events", "reason_code", "TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_events_event_type ON audit_events(event_type, seq DESC)"
        )
        # The v4 tables and their indexes, re-asserted for a database that was
        # created by an older build but somehow lacks them.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sys_dict_type_enabled ON sys_dict_items(dict_type, enabled, sort_order ASC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_distill_queue_status_priority ON distill_queue(status, priority, enqueued_at ASC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_distill_queue_client_status ON distill_queue(client, status, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_distill_queue_session_rev ON distill_queue(session_key, revision DESC, chunk_index ASC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_distill_queue_run ON distill_queue(run_id)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, name, applied_at) VALUES (4, 'v4_data_foundation_dicts_and_queue', ?)",
            (now,),
        )

    def _migrate_knowledge_fts(self, conn: sqlite3.Connection) -> None:
        """Rebuild the FTS table when its column contract changed (pre-2026-11-23 layouts)."""
        columns = tuple(row["name"] for row in conn.execute("PRAGMA table_info(knowledge_fts)").fetchall())
        if columns != KNOWLEDGE_FTS_COLUMNS:
            conn.execute("DROP TABLE IF EXISTS knowledge_fts")
            conn.execute(_KNOWLEDGE_FTS_SQL)

    def _migrate_approval_recovery(self, conn: sqlite3.Connection) -> None:
        """Columns and cleanup for fix-approval-claim-and-recovery (idempotent)."""
        self._ensure_column(conn, "workflow_lock", "expires_at", "TEXT")
        self._ensure_column(conn, "workflow_lock", "heartbeat_at", "TEXT")
        self._ensure_column(conn, "decision_cards", "result_summary", "TEXT")
        self._ensure_column(conn, "decision_cards", "failed_node", "TEXT")
        # A lock row without expiry can never be taken over nor expired; treat as debris.
        conn.execute("DELETE FROM workflow_lock WHERE expires_at IS NULL")

    def _migrate_rule_lineage(self, conn: sqlite3.Connection) -> None:
        """The bi-temporal rule lineage columns (idempotent, additive).

        ``supersedes_rule_id`` records which rule a new one replaces;
        ``effective_from`` / ``effective_to`` bracket the window in which the
        rule is in force. Existing rows keep every value they had; their lineage
        stays NULL until a lifecycle transition fills it in.
        """
        self._ensure_column(conn, "rules", "supersedes_rule_id", "TEXT")
        self._ensure_column(conn, "rules", "effective_from", "TEXT")
        self._ensure_column(conn, "rules", "effective_to", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rules_supersedes ON rules(supersedes_rule_id)"
        )

    def _ensure_outbox_columns(self, conn: sqlite3.Connection) -> None:
        """The managed-delivery columns of ``card_deliveries`` (idempotent).

        Lives here, not in the Feishu layer: the delivery ledger's shape must not
        depend on whether the Feishu channel happens to be enabled at startup.
        """
        self._ensure_column(conn, "card_deliveries", "synced_version", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "card_deliveries", "synced_avail", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "card_deliveries", "first_try_at", "TEXT")
        self._ensure_column(conn, "card_deliveries", "next_attempt_at", "TEXT")
        # 1 = abandoned: by the send retries (status 'given_up') or by the state sync.
        # Falling back to text is recorded by status 'degraded' alone, so that a later
        # answer on an already-delivered card can still have its outcome written back.
        self._ensure_column(conn, "card_deliveries", "given_up", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "card_deliveries", "reason", "TEXT NOT NULL DEFAULT ''")
        # When the current deferral by a transient listener state began (ISO), NULL when not deferred.
        self._ensure_column(conn, "card_deliveries", "deferred_at", "TEXT")
        # The ask card exactly as delivered; used to restore its buttons after an outage.
        self._ensure_column(conn, "card_deliveries", "ask_card_json", "TEXT")
        # The send counters, one per outcome kind: explicit failures and unknown
        # outcomes are counted separately, not parsed back out of the free-text
        # reason column.
        self._ensure_column(conn, "card_deliveries", "interactive_fails", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "card_deliveries", "unknown_sends", "INTEGER NOT NULL DEFAULT 0")
        # The legacy rows are recognised by their shape, not by a one-time flag:
        # a row the managed path never claimed (no first_try_at) and that already
        # carries a send result was written by the old one-shot path, which only
        # ever sent plain text. Rows the managed path owns always carry a claim.
        conn.execute(
            """
            UPDATE card_deliveries SET channel = 'feishu_text', synced_version = 1
            WHERE channel = 'feishu_cli'
              AND first_try_at IS NULL
              AND synced_version = 0
              AND status IN ('failed', 'sent')
              AND NOT EXISTS (
                  SELECT 1 FROM card_deliveries t
                  WHERE t.card_id = card_deliveries.card_id AND t.channel = 'feishu_text'
              )
            """
        )
        # A legacy 'failed' row: resend as text when the ask card still awaits a decision,
        # otherwise there is nothing left to notify about.
        conn.execute(
            """
            UPDATE card_deliveries SET
                status = CASE WHEN EXISTS (
                              SELECT 1 FROM decision_cards c
                              WHERE c.card_id = card_deliveries.card_id AND c.state = 'open'
                          ) THEN 'pending' ELSE 'skipped' END,
                reason = 'legacy failed delivery'
            WHERE channel = 'feishu_text' AND status = 'failed' AND first_try_at IS NULL
            """
        )
