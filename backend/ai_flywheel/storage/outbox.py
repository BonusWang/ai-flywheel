"""feat-feishu-approval-card-loop：发件箱数据层。

card_deliveries 扩展为受管投递：占位租约、退避重试、放弃与重排、
同步版本与监听可用性去重。交互行（feishu_cli）与文本行（feishu_text）
各自独立成行、各用各的幂等键。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ai_flywheel.storage.db import FlywheelDB


# 重试节奏与放弃窗口照搬参照实现（相对 first_try_at，分钟）
RESEND_AT_MINUTES = (0, 1, 3, 7, 15, 25, 35, 45, 55)
RESEND_WINDOW_MINUTES = 57
LEASE_SECONDS = 60
UNKNOWN_SEND_LIMIT = 9
INTERACTIVE_FAIL_LIMIT = 3


def next_retry_at(first_try_at: str, tries: int, now: str) -> str | None:
    """下一次重试时间；达到第 9 次或超出窗口后返回 None（放弃）。"""
    idx = min(tries, len(RESEND_AT_MINUTES) - 1)
    base = datetime.fromisoformat(first_try_at)
    candidate = base + timedelta(minutes=RESEND_AT_MINUTES[idx])
    now_dt = datetime.fromisoformat(now)
    if candidate <= now_dt:
        candidate = now_dt + timedelta(seconds=1)
    if (now_dt - base).total_seconds() > RESEND_WINDOW_MINUTES * 60 or tries >= len(RESEND_AT_MINUTES):
        return None
    return candidate.isoformat()


class OutboxDB:
    """card_deliveries 上的受管投诊访问层（组合 FlywheelDB，不改动既有方法签名）。"""

    def __init__(self, db: FlywheelDB) -> None:
        self.db = db
        # The column contract is owned by the schema migrations; running them
        # again here keeps a standalone OutboxDB safe on an older database.
        with db.write_tx() as conn:
            db._ensure_outbox_columns(conn)

    # ---------------- 登记与首发 ----------------

    def register(self, *, card_id: str, channel: str, status: str = "pending", reason: str = "") -> None:
        with self.db.write_tx() as conn:
            conn.execute(
                """
                INSERT INTO card_deliveries (delivery_id, card_id, channel, status, reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_id, channel) DO NOTHING
                """,
                (f"del-{card_id}-{channel}", card_id, channel, status, reason, self.db._now_iso(), self.db._now_iso()),
            )

    def claim_for_send(self, *, delivery_id: str, now: str | None = None) -> bool:
        """占位：同一行同一时刻只有一个发送方，租约 60 秒。"""
        ts = self.db._now_iso(now)
        lease = (datetime.fromisoformat(ts) + timedelta(seconds=LEASE_SECONDS)).isoformat()
        with self.db.write_tx() as conn:
            cur = conn.execute(
                """
                UPDATE card_deliveries SET status = 'sending', next_attempt_at = ?, deferred_at = NULL,
                    first_try_at = COALESCE(first_try_at, ?), updated_at = ?
                WHERE delivery_id = ? AND status IN ('pending', 'retrying')
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                """,
                (lease, ts, ts, delivery_id, ts),
            )
            return cur.rowcount == 1

    def reclaim_stuck_sending(self, *, now: str | None = None) -> None:
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            conn.execute(
                "UPDATE card_deliveries SET status = 'retrying' WHERE status = 'sending' AND next_attempt_at < ?",
                (ts,),
            )

    def mark_result(
        self,
        *,
        delivery_id: str,
        status: str,
        now: str | None = None,
        external_ref: str | None = None,
        error: str | None = None,
        reason: str | None = None,
        interactive_fails: int | None = None,
        unknown_sends: int | None = None,
    ) -> None:
        """结果写回：sent / retrying(安排退避) / given_up / skipped。"""
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            row = conn.execute(
                "SELECT tries, first_try_at, status, external_ref FROM card_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return
            tries = int(row["tries"] or 0) + 1
            sets = ["status = ?", "tries = ?", "updated_at = ?", "reason = COALESCE(?, reason)"]
            params: list = [status, tries, ts, reason]
            if external_ref:
                sets.append("external_ref = COALESCE(external_ref, ?)")
                params.append(external_ref)
            if error is not None:
                sets.append("last_error = ?")
                params.append(error)
            if interactive_fails is not None:
                sets.append("interactive_fails = ?")
                params.append(int(interactive_fails))
            if unknown_sends is not None:
                sets.append("unknown_sends = ?")
                params.append(int(unknown_sends))
            if status in ("sent", "skipped", "given_up"):
                sets.append("next_attempt_at = NULL")
                sets.append("deferred_at = NULL")
                if status == "given_up":
                    sets.append("given_up = 1")
            elif status == "retrying":
                first = row["first_try_at"] or ts
                nxt = next_retry_at(first, tries, ts)
                if nxt is None:
                    sets[0] = "status = ?"
                    params[0] = "given_up"
                    sets.append("next_attempt_at = NULL")
                    sets.append("given_up = 1")
                else:
                    sets.append("next_attempt_at = ?")
                    params.append(nxt)
            params.append(delivery_id)
            conn.execute(f"UPDATE card_deliveries SET {', '.join(sets)} WHERE delivery_id = ?", params)

    def defer(self, *, delivery_id: str, now: str | None = None, defer_seconds: int | None = None) -> None:
        """Deferred by a transient listener state: no try is counted, and the first such
        judgement starts the clock that bounds the deferral.

        ``defer_seconds`` also pushes the next attempt out, so the delivery is not
        picked up again before the deferral window has passed.
        """
        ts = self.db._now_iso(now)
        sets = ["deferred_at = COALESCE(deferred_at, ?)", "updated_at = ?"]
        params: list = [ts, ts]
        if defer_seconds is not None:
            sets.insert(0, "next_attempt_at = ?")
            params.insert(0, (datetime.fromisoformat(ts) + timedelta(seconds=defer_seconds)).isoformat())
        with self.db.write_tx() as conn:
            conn.execute(
                f"UPDATE card_deliveries SET {', '.join(sets)} "
                "WHERE delivery_id = ? AND status IN ('pending', 'retrying')",
                (*params, delivery_id),
            )

    def mark_degraded(self, *, card_id: str, channel: str, reason: str, now: str | None = None) -> None:
        """The interactive row fell back to text: terminal for that row."""
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            conn.execute(
                "UPDATE card_deliveries SET status = 'degraded', reason = ?, given_up = 1, updated_at = ? "
                "WHERE card_id = ? AND channel = ?",
                (reason, ts, card_id, channel),
            )

    def record_sync_failure(self, *, delivery_id: str, error: str, give_up: bool = False, now: str | None = None) -> None:
        """One failed state-sync attempt; the row is given up on after too many."""
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            conn.execute(
                "UPDATE card_deliveries SET last_error = ?, given_up = ?, updated_at = ? WHERE delivery_id = ?",
                (error, 1 if give_up else 0, ts, delivery_id),
            )

    def send_counters(self, *, card_id: str, channel: str) -> tuple[int, int]:
        """The (explicit failures, unknown outcomes) counters of one delivery row."""
        row = self.get(card_id=card_id, channel=channel)
        if not row:
            return (0, 0)
        return (int(row.get("interactive_fails") or 0), int(row.get("unknown_sends") or 0))

    def record_send_outcome(self, *, delivery_id: str, outcome: str, now: str | None = None) -> tuple[int, int]:
        """Count one send attempt by outcome kind and return the new counters.

        ``outcome`` is ``failed`` (the send was refused) or ``unknown`` (the
        process died or timed out: the message may still have gone out). The two
        are counted apart, because only unknown outcomes may be retried blindly.
        """
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            row = conn.execute(
                "SELECT interactive_fails, unknown_sends FROM card_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return (0, 0)
            fails = int(row["interactive_fails"] or 0)
            unknowns = int(row["unknown_sends"] or 0)
            if outcome == "failed":
                fails += 1
            elif outcome == "unknown":
                unknowns += 1
            conn.execute(
                "UPDATE card_deliveries SET interactive_fails = ?, unknown_sends = ?, updated_at = ? "
                "WHERE delivery_id = ?",
                (fails, unknowns, ts, delivery_id),
            )
            return (fails, unknowns)

    def clear_deferrals(self) -> None:
        """The listener is ready again: the next blip starts a fresh deferral window."""
        with self.db.write_tx() as conn:
            conn.execute(
                "UPDATE card_deliveries SET deferred_at = NULL "
                "WHERE channel = 'feishu_cli' AND deferred_at IS NOT NULL AND status IN ('pending', 'retrying')"
            )

    def mark_delivered(self, *, card_id: str, version: int, avail: str, ask_card_json: str, now: str | None = None) -> None:
        """Record that the interactive card as sent already shows this version and
        availability. Without it the next sync tick would see the row as lagging and
        overwrite a still-open card (wiping its buttons)."""
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            conn.execute(
                """
                UPDATE card_deliveries
                SET synced_version = ?, synced_avail = ?, ask_card_json = ?, updated_at = ?
                WHERE card_id = ? AND channel = 'feishu_cli'
                """,
                (version, avail, ask_card_json, ts, card_id),
            )

    # ---------------- 查询 ----------------

    def get(self, *, card_id: str, channel: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM card_deliveries WHERE card_id = ? AND channel = ?", (card_id, channel)
            ).fetchone()
            return dict(row) if row else None

    def list_due(self, *, now: str | None = None) -> list[dict]:
        ts = self.db._now_iso(now)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.* FROM card_deliveries d
                JOIN decision_cards c ON c.card_id = d.card_id
                WHERE d.status IN ('pending', 'retrying')
                  AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?)
                  AND c.state = 'open'
                ORDER BY d.created_at
                """,
                (ts,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_needing_sync(self, *, avail: str) -> list[dict]:
        """已送达的交互行，卡片版本或监听可用性落后即需同步（avail 由协调层给出）。

        A row that fell back to text is included once it carries a message reference:
        that happens when an approver answers on a card whose send was never recorded,
        and the outcome must still reach the card they clicked.
        """
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*, c.version AS card_version, c.state AS card_state
                FROM card_deliveries d
                JOIN decision_cards c ON c.card_id = d.card_id
                WHERE d.channel = 'feishu_cli' AND d.status IN ('sent', 'degraded')
                  AND d.external_ref IS NOT NULL
                  AND (d.synced_version < c.version OR d.synced_avail <> ?)
                  AND d.given_up = 0
                """,
                (avail,),
            ).fetchall()
            return [dict(r) for r in rows]

    def resume_abandoned_sync(self) -> int:
        """A new process life retries the state sync of rows an earlier one had given up on.
        Only sync resumes: these rows are never resent."""
        with self.db.write_tx() as conn:
            cur = conn.execute(
                "UPDATE card_deliveries SET given_up = 0 WHERE given_up = 1 AND status IN ('sent', 'degraded')"
            )
            return cur.rowcount

    def mark_synced(self, *, delivery_id: str, version: int, avail: str, now: str | None = None) -> None:
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            conn.execute(
                "UPDATE card_deliveries SET synced_version = ?, synced_avail = ?, updated_at = ? WHERE delivery_id = ?",
                (version, avail, ts, delivery_id),
            )

    def bind_external_ref(self, *, card_id: str, channel: str, external_ref: str, now: str | None = None) -> None:
        """投递记录无消息引用时，把首条有效回答所在消息绑定为送达消息。"""
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            conn.execute(
                """
                UPDATE card_deliveries SET external_ref = COALESCE(external_ref, ?), updated_at = ?
                WHERE card_id = ? AND channel = ? AND external_ref IS NULL
                """,
                (external_ref, ts, card_id, channel),
            )

    # ---------------- 启动重排（发送阶段放弃的行） ----------------

    def requeue_abandoned(self, *, now: str | None = None) -> int:
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            cur = conn.execute(
                """
                UPDATE card_deliveries SET status = 'pending', given_up = 0, tries = 0,
                    first_try_at = NULL, next_attempt_at = NULL, deferred_at = NULL, updated_at = ?
                WHERE given_up = 1 AND status = 'given_up'
                  AND NOT EXISTS (
                    SELECT 1 FROM card_deliveries o
                    WHERE o.card_id = card_deliveries.card_id
                      AND o.channel IN ('feishu_cli', 'feishu_text')
                      AND o.status = 'sent'
                  )
                  AND EXISTS (SELECT 1 FROM decision_cards c WHERE c.card_id = card_deliveries.card_id AND c.state = 'open')
                """,
                (ts,),
            )
            return cur.rowcount

    def skip_delivery(self, *, card_id: str, channel: str, reason: str, now: str | None = None) -> None:
        ts = self.db._now_iso(now)
        with self.db.write_tx() as conn:
            conn.execute(
                """
                UPDATE card_deliveries SET status = 'skipped', reason = ?, deferred_at = NULL, updated_at = ?
                WHERE card_id = ? AND channel = ?
                """,
                (reason, ts, card_id, channel),
            )
