"""The ask-card ledger, its delivery rows and the audit trail."""
from __future__ import annotations

import json
from typing import Any


class AuditRepoMixin:
    """The ask-card ledger, its delivery rows and the audit trail."""


    # ---------------- v2 Audit Events, Decision Cards, Deliveries, Skill Assets, System Settings ----------------
    def _insert_audit_event(
        self,
        conn: Any,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor: str = "system",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        from_status: str = "",
        to_status: str = "",
        reason_code: str = "",
    ) -> int:
        """Write one audit row on an existing connection.

        Repositories that already hold a write transaction (the queue status
        transition) call this so the state update and its trail commit or roll
        back together, instead of opening a second connection.
        """
        now = self._now_iso()
        cur = conn.execute(
            """
            INSERT INTO audit_events (
                entity_type, entity_id, event_type, actor, summary, payload_json,
                from_status, to_status, reason_code, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entity_type,
                entity_id,
                event_type,
                actor,
                summary,
                json.dumps(payload or {}, ensure_ascii=False),
                from_status,
                to_status,
                reason_code,
                now,
            ),
        )
        return int(cur.lastrowid or 0)

    def append_audit_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor: str = "system",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        from_status: str = "",
        to_status: str = "",
        reason_code: str = "",
    ) -> int:
        """Append one audit event; the status-transition arguments are optional.

        Existing callers keep the old keyword set: an omitted transition lands
        as the empty string, so no caller has to change to stay compatible.
        """
        with self.connect() as conn:
            return self._insert_audit_event(
                conn,
                entity_type=entity_type,
                entity_id=entity_id,
                event_type=event_type,
                actor=actor,
                summary=summary,
                payload=payload,
                from_status=from_status,
                to_status=to_status,
                reason_code=reason_code,
            )

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
        now = self._now_iso()
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

    def get_decision_card(self, card_id: str) -> dict[str, Any] | None:
        """One ask card by its id, as a plain dict."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM decision_cards WHERE card_id = ?", (card_id,)).fetchone()
            return dict(row) if row else None

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
        now = self._now_iso()
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
