"""飞书审批通道协调器（feat-feishu-approval-card-loop）。

一个对象串起：提问卡的登记与首发（任意进程）、serve 内每 30 秒的发件箱线程
（补投、重试、状态回写、可用性收口）、点击的校验与受理（经变更 A 的认领原语
在唯一执行线程里续接）、审计白名单。

这是 ``core.approval_channel.ApprovalChannel`` 的飞书实现：编排器与运行时只
通过契约调用，不依赖本模块的类型。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ai_flywheel.adapters.channels.feishu.card import (
    APPROVAL_MODE_LABEL,
    KIND_LABEL,
    is_sensitive,
    render_ask_card,
    render_state_card,
    sanitize,
    with_notice,
)
from ai_flywheel.adapters.lark_cli import LarkCliRunner
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.approval_channel import ApprovalBusyError
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.outbox import INTERACTIVE_FAIL_LIMIT, UNKNOWN_SEND_LIMIT, OutboxDB

logger = logging.getLogger("ai_flywheel.feishu_channel")

DEFERRAL_SECONDS = 120
OPTION_ACTIONS = {"approve_all": ("同意并写入", None), "approve_routine": ("仅同意常规项", "routine"), "reject": ("取消本次写入", "reject")}


class FeishuChannel:
    """飞书审批通道：结构性满足 ``ApprovalChannel`` 契约。"""

    def __init__(
        self,
        cfg: FlywheelConfig,
        db: FlywheelDB,
        outbox: OutboxDB,
        runner: LarkCliRunner,
        *,
        listener=None,
        check=None,
        approve_fn: Callable[..., Any] | None = None,
        reject_fn: Callable[..., Any] | None = None,
        process_role: str = "cli",
        interval: float = 30.0,
        on_after_interactive_send: Callable[[str, str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.outbox = outbox
        self.runner = runner
        self.listener = listener
        self.check = check
        self.approve_fn = approve_fn
        self.reject_fn = reject_fn
        self.process_role = process_role
        self.interval = interval
        # Optional hook called after an interactive card was sent: (card_id, message_id).
        # Verification harnesses use it to park the send before the message id is
        # recorded; production never sets it.
        self.on_after_interactive_send = on_after_interactive_send
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None if process_role != "serve" else ThreadPoolExecutor(max_workers=1, thread_name_prefix="flywheel-feishu-exec")
        self._sync_failures: dict[tuple[str, int | None], int] = {}
        self._sync_lock = threading.Lock()

    # ---------------- 可用性与闸门 ----------------

    def availability(self) -> str:
        fcfg = self.cfg.delivery.feishu_cli
        if self.listener is None:
            return "down:no_listener"
        st = self.listener.state
        if not fcfg.interaction_ready:
            return "down:config"
        if st.get("state") == "ready":
            if self.check is not None and not self.check.status().get("valid"):
                return "down:check_invalid"
            return "ok"
        if st.get("state") in {"backoff", "starting"}:
            return f"transient:{st.get('state')}"
        return f"down:{st.get('state')}"

    def interactive_allowed(self) -> bool:
        return self.process_role == "serve" and self.availability() == "ok"

    # ---------------- 提问卡首发（任意进程，编排器挂起分支调用） ----------------

    def first_send(self, *, card_id: str, run, reasons: list[str], portal_url: str) -> None:
        fcfg = self.cfg.delivery.feishu_cli
        if not fcfg.enabled:
            return
        self.outbox.register(card_id=card_id, channel="feishu_cli")
        avail = self.availability() if self.process_role == "serve" else "down:not_serve"
        if avail == "ok":
            self._try_send_interactive(card_id=card_id, run=run, reasons=reasons, portal_url=portal_url)
            return
        if avail.startswith("transient:"):
            # 暂缓：不计尝试，120 秒后由发件箱线程重试；起点持久化在 next_attempt_at
            row = self.outbox.get(card_id=card_id, channel="feishu_cli")
            if row and row["status"] in ("pending", "retrying") and not row["next_attempt_at"]:
                self.outbox.defer(
                    delivery_id=row["delivery_id"], defer_seconds=DEFERRAL_SECONDS
                )
            return
        self._degrade_to_text(card_id=card_id, reason=f"interactive unavailable ({avail})", run=run, reasons=reasons, portal_url=portal_url)

    def _card_writes(self, run) -> list[dict[str, Any]]:
        return [
            {
                "target_kind": w.target_kind,
                "action": w.action,
                "relative_path": w.relative_path,
                "title": w.title,
                "summary": w.summary,
                "approval_reason": w.approval_reason,
                "content_preview": w.content_preview,
                # The sensitivity predicate reads this flag: without it a
                # knowledge_doc / flywheel_rule that needs approval would look routine
                # and "仅同意常规项" would approve it.
                "requires_approval": bool(w.requires_approval),
            }
            for w in run.planned_writes
        ]

    def _try_send_interactive(self, *, card_id: str, run, reasons: list[str], portal_url: str) -> None:
        delivery_id = f"del-{card_id}-feishu_cli"
        if not self.outbox.claim_for_send(delivery_id=delivery_id):
            return
        fcfg = self.cfg.delivery.feishu_cli
        card_json = self._render_ask(card_id, run, reasons)
        res = self.runner.send_interactive(user_id=fcfg.user_id, card_json=card_json, idempotency_key=f"flywheel-ask-{run.run_id}")
        if res.status == "sent":
            if self.on_after_interactive_send is not None:
                self.on_after_interactive_send(card_id, res.message_id or "")
            self.outbox.mark_result(delivery_id=delivery_id, status="sent", external_ref=res.message_id)
            # The card as sent already shows this version and availability; without
            # this the next sync tick would treat it as lagging and overwrite it.
            card_row = self._card_row(card_id) or {}
            self.outbox.mark_delivered(
                card_id=card_id,
                version=int(card_row.get("version") or 1),
                avail=self._avail_key(),
                ask_card_json=card_json,
            )
            return
        # The two outcomes are counted apart: an explicit failure may not be
        # retried blindly, an unknown one may be (the message may have gone out).
        fails, unknowns = self.outbox.record_send_outcome(delivery_id=delivery_id, outcome=res.status)
        if unknowns >= UNKNOWN_SEND_LIMIT or fails >= INTERACTIVE_FAIL_LIMIT:
            self._degrade_to_text(card_id=card_id, reason=f"degraded after fails:{fails} unknown:{unknowns}", run=run, reasons=reasons, portal_url=portal_url)
            return
        self.outbox.mark_result(
            delivery_id=delivery_id,
            status="retrying",
            error=res.error,
            reason=f"fails:{fails} unknown:{unknowns}",
            interactive_fails=fails,
            unknown_sends=unknowns,
        )

    def _degrade_to_text(self, *, card_id: str, reason: str, run, reasons: list[str], portal_url: str) -> None:
        self.outbox.mark_degraded(card_id=card_id, channel="feishu_cli", reason=reason)
        self.outbox.register(card_id=card_id, channel="feishu_text")
        self._send_text_row(
            card_id=card_id,
            run=run,
            reasons=reasons,
            portal_url=portal_url,
            degrade_reason=reason,
        )

    @staticmethod
    def _format_text_reasons(reasons: list[str]) -> str:
        cleaned: list[str] = []
        for raw in reasons[:2]:
            text = sanitize(raw, limit=180)
            text = re.sub(r"[。；;]+\s*[；;]+", "；", text).strip("。；; ")
            if text and text not in cleaned:
                cleaned.append(text)
        return "；".join(cleaned) if cleaned else "等待人工确认"

    def _fallback_footer(self, *, degrade_reason: str, portal_url: str) -> str:
        fcfg = self.cfg.delivery.feishu_cli
        if not fcfg.interactive:
            return f"当前未开启飞书交互卡片审批，请到电脑前处理（可在控制台「接入与设置」开启交互审批并完成自检）：{portal_url}"
        if "down:check_invalid" in degrade_reason or (
            self.process_role == "serve" and self.availability() == "down:check_invalid"
        ):
            return f"飞书交互卡片尚未通过链路自检，请到电脑前处理（或在控制台运行链路自检）：{portal_url}"
        return f"飞书按钮暂不可用，请到电脑前处理：{portal_url}"

    def _send_text_row(
        self,
        *,
        card_id: str,
        run,
        reasons: list[str],
        portal_url: str,
        degrade_reason: str = "",
    ) -> None:
        delivery_id = f"del-{card_id}-feishu_text"
        if not self.outbox.claim_for_send(delivery_id=delivery_id):
            return
        fcfg = self.cfg.delivery.feishu_cli
        if not fcfg.user_id and not fcfg.chat_id:
            self.outbox.mark_result(delivery_id=delivery_id, status="skipped", reason="no user_id or chat_id configured")
            return
        # Text fallbacks carry the same untrusted titles / summaries / reasons as cards: sanitize them too.
        items = "\n".join(
            f"- [{KIND_LABEL.get(w.target_kind, w.target_kind)}] {sanitize(w.title, limit=80)}：{sanitize(w.summary, limit=120)}"
            for w in run.planned_writes[:6]
        )
        reason_line = self._format_text_reasons(reasons)
        footer = self._fallback_footer(degrade_reason=degrade_reason, portal_url=portal_url)
        text = (
            f"【AI 飞轮待审批（{run.batch_date} #{run.batch_seq:02d}）】\n\n"
            f"批次触发重要变更确认：{reason_line}\n\n{items}\n\n"
            f"{footer}"
        )[:3500]
        res = self.runner.send_text(user_id=fcfg.user_id, chat_id=fcfg.chat_id if not fcfg.user_id else "", text=text, idempotency_key=f"flywheel-ask-{run.run_id}-text")
        # A text fallback that did not go out is retried, not stranded: the outbox turns
        # "retrying" into "given_up" once the give-up window is over.
        status = res.status if res.status == "sent" else "retrying"
        self.outbox.mark_result(delivery_id=delivery_id, status=status, external_ref=res.message_id, error=res.error)

    # ---------------- 发件箱线程（serve） ----------------

    def start(self) -> None:
        if self._thread is not None or self.process_role != "serve":
            return
        self._stop.clear()
        # A delivery row abandoned by an earlier process is requeued once, here: doing
        # it on every tick would restart the give-up window every 30 seconds instead of
        # letting the 57-minute window elapse.
        self.outbox.requeue_abandoned()
        self._thread = threading.Thread(target=self._loop, name="flywheel-feishu-outbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._executor is not None:
            self._executor.shutdown(wait=False)

    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("outbox tick failed")
            if self._stop.wait(self.interval):
                return

    def tick(self) -> None:
        self.outbox.reclaim_stuck_sending()
        avail = self.availability()
        if self.cfg.delivery.feishu_cli.enabled:
            for row in self.outbox.list_due():
                card = self._card_row(row["card_id"])
                run = self.db.get_workflow_run(card["run_id"]) if card else None
                if card is None or run is None or card.get("state") != "open":
                    self.outbox.skip_delivery(card_id=row["card_id"], channel=row["channel"], reason="card left open state before delivery")
                    continue
                reasons = self._pause_reasons(card["run_id"], card.get("title") or "等待审批")
                url = self._portal_url()
                if row["channel"] == "feishu_cli":
                    if avail == "ok":
                        self._try_send_interactive(card_id=row["card_id"], run=run, reasons=reasons, portal_url=url)
                    elif avail.startswith("transient:"):
                        pass  # 继续暂缓
                    else:
                        self._degrade_to_text(card_id=row["card_id"], reason=f"interactive unavailable ({avail})", run=run, reasons=reasons, portal_url=url)
                else:
                    cli_row = self.outbox.get(card_id=row["card_id"], channel="feishu_cli")
                    self._send_text_row(
                        card_id=row["card_id"],
                        run=run,
                        reasons=reasons,
                        portal_url=url,
                        degrade_reason=str((cli_row or {}).get("reason") or ""),
                    )
        self._sync_cards()

    def _card_row(self, card_id: str) -> dict | None:
        return self.db.get_decision_card(card_id)

    def _avail_key(self) -> str:
        """Availability as card sync sees it: transient listener states keep the buttons."""
        avail = self.availability()
        if avail == "ok" or avail.startswith("transient:"):
            return "ok"
        return avail

    def _portal_url(self) -> str:
        return f"http://{self.cfg.server.host}:{self.cfg.server.port}/runs"

    def _pause_reasons(self, run_id: str, fallback: str = "等待审批") -> list[str]:
        """Why the run paused, from its own audit trail (recorded before any card was sent)."""
        for ev in self.db.list_audit_events(entity_type="workflow_run", entity_id=run_id, limit=50):
            if ev.get("event_type") == "awaiting_approval":
                reasons = [str(r) for r in (ev.get("payload") or {}).get("reasons", [])]
                return reasons or [fallback]
        return [fallback]

    def _render_ask(self, card_id: str, run, reasons: list[str]) -> str:
        mode = APPROVAL_MODE_LABEL.get(getattr(run, "approval_mode", ""), "人工审批")
        label = f"{run.batch_date} #{run.batch_seq:02d}"
        return render_ask_card(
            card_id=card_id,
            batch_label=label,
            subtitle=f"批次 {label} · {mode}",
            reasons=reasons,
            writes=self._card_writes(run),
            portal_url=self._portal_url(),
        )

    def _ask_card_json(self, row: dict, card: dict, run) -> str:
        """The ask card as delivered (saved at send time), or a faithful re-render for rows without a copy."""
        saved = row.get("ask_card_json")
        if saved:
            return saved
        return self._render_ask(
            card["card_id"], run, self._pause_reasons(card["run_id"], card.get("title") or "等待审批")
        )

    def _sync_cards(self) -> None:
        with self._sync_lock:
            return self._sync_cards_unlocked()

    def _sync_soon(self) -> None:
        try:
            self._sync_cards()
        except Exception:
            logger.exception("card sync failed")

    def _sync_cards_unlocked(self) -> None:
        """Bring delivered interactive cards to their current state.

        A card is patched only when it changed since the last sync (version lag) or
        the listener availability changed. An open card keeps its buttons: it is
        re-rendered only to restore them after an outage, or to say they are unavailable.
        """
        key = self._avail_key()
        for row in self.outbox.list_needing_sync(avail=key):
            card = self._card_row(row["card_id"])
            run = self.db.get_workflow_run(card["run_id"]) if card else None
            if card is None or run is None:
                continue
            version = int(card.get("version") or 0)
            lagging = int(row.get("synced_version") or 0) < version
            state = card.get("state")
            label = f"{run.batch_date} #{run.batch_seq:02d}"
            if state != "open":
                if not lagging:
                    # A settled card has no buttons: availability is irrelevant, only record it.
                    self.outbox.mark_synced(delivery_id=row["delivery_id"], version=version, avail=key)
                    continue
                card_json = render_state_card(
                    card_id=row["card_id"],
                    state=state,
                    batch_label=label,
                    selected=card.get("selected_option") or "",
                    result_summary=card.get("result_summary") or "",
                )
            elif key == "ok":
                if not lagging and (row.get("synced_avail") or "") == "ok":
                    continue
                card_json = self._ask_card_json(row, card, run)  # restore the ask card with its buttons
            else:
                if not lagging and (row.get("synced_avail") or "") == key:
                    continue
                card_json = render_state_card(
                    card_id=row["card_id"], state="closed", unavailable=True, batch_label=label
                )
            if self._patch(row, card_json, version=version):
                self.outbox.mark_synced(delivery_id=row["delivery_id"], version=version, avail=key)

    def _patch(self, row: dict, card_json: str, *, version: int | None, count_failure: bool = True) -> bool:
        ref = row.get("external_ref")
        if not ref:
            return False
        res = self.runner.patch_card(message_id=ref, card_json=card_json)
        if res.status == "sent":
            self._sync_failures.pop((row["delivery_id"], version), None)
            return True
        error = (res.error or f"patch {res.status}")[:300]
        if not count_failure:
            return False  # one-off hint: never counts towards giving up on state sync
        times = self._sync_failures.get((row["delivery_id"], version), 0) + 1
        self._sync_failures[(row["delivery_id"], version)] = times
        self.outbox.record_sync_failure(
            delivery_id=row["delivery_id"],
            error=error,
            # Give up on syncing this card version; the message stays as last rendered.
            give_up=times >= UNKNOWN_SEND_LIMIT,
        )
        return False

    # ---------------- 点击受理 ----------------

    def handle_click(self, event: dict[str, Any]) -> None:
        try:
            self._handle_click(event)
        except Exception:
            logger.exception("click handling failed")

    def _handle_click(self, event: dict[str, Any]) -> None:
        from ai_flywheel.adapters.channels.feishu.check import event_card_id

        card_id = event_card_id(event)
        if not card_id:
            return
        if card_id.startswith("fcheck-"):
            if self.check is not None:
                self.check.handle_click(event)
            return
        value = event.get("action_value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = {}
        action = str((value or {}).get("action") or "")
        if action not in OPTION_ACTIONS:
            return
        operator = str(event.get("operator_id") or "").strip()
        message_id = str(event.get("message_id") or "")
        fcfg = self.cfg.delivery.feishu_cli
        card = self._card_row(card_id)
        if card is None:
            return  # 未知卡片：只进限速日志，不进审计
        run = self.db.get_workflow_run(card["run_id"])
        if run is None or card.get("state") != "open" or run.status.value != "AWAITING_APPROVAL":
            self._audit(card_id, "answer_late", {"card_id": card_id, "reason_code": "not_open"})
            return
        if operator not in fcfg.effective_approver_ids:
            self._audit(card_id, "answer_denied", {"card_id": card_id, "reason_code": "not_approver", "operator_id": operator[:64]})
            return
        row = self.outbox.get(card_id=card_id, channel="feishu_cli")
        ref = (row or {}).get("external_ref")
        if not ref:
            self.outbox.bind_external_ref(card_id=card_id, channel="feishu_cli", external_ref=message_id)
        elif ref != message_id:
            self._audit(card_id, "answer_denied", {"card_id": card_id, "reason_code": "ref_mismatch"})
            return
        label, mode = OPTION_ACTIONS[action]
        if mode == "reject":
            self._execute(card_id, lambda: self.reject_fn(card["run_id"], reason="飞书卡片：取消本次写入", source="feishu"))
            return
        indices = None
        if mode == "routine":
            indices = [i for i, w in enumerate(run.planned_writes) if not is_sensitive(self._card_writes(run)[i])]
        # The chosen option is recorded on the card, so the outcome says which of the
        # three buttons was pressed.
        self._execute(
            card_id,
            lambda: self.approve_fn(
                card["run_id"], approved_indices=indices, source="feishu", selected_option=label
            ),
        )

    def _execute(self, card_id: str, fn) -> None:
        def _run() -> None:
            # Feedback first: the claim commits at the start of fn, so a moment later
            # the card can show "已受理" without waiting for the 30-second outbox tick.
            timer = threading.Timer(1.5, self._sync_soon)
            timer.daemon = True
            timer.start()
            try:
                fn()
            except ApprovalBusyError as exc:
                timer.cancel()
                # The card stays open and clickable: a one-off notice goes right above its
                # actions. The other batch keeps renewing its lock, so no "retry at" time.
                self._audit(
                    card_id,
                    "answer_busy",
                    {"card_id": card_id, "reason_code": "lock_busy", "lock_expires_at": exc.lock_expires_at},
                )
                row = self.outbox.get(card_id=card_id, channel="feishu_cli")
                card = self._card_row(card_id)
                run = self.db.get_workflow_run(card["run_id"]) if card else None
                if row and card and run and card.get("state") == "open":
                    base = row.get("ask_card_json") or self._render_ask(card_id, run, self._pause_reasons(run.run_id))
                    self._patch(row, with_notice(base, "另一批次正在执行，请等它结束后再点。"), version=None, count_failure=False)
            except ValueError as exc:
                timer.cancel()
                self._audit(card_id, "answer_late", {"card_id": card_id, "reason_code": "not_awaiting", "detail": str(exc)[:120]})
            else:
                self._sync_soon()  # settled: write the outcome back right away

        if self._executor is not None:
            self._executor.submit(_run)
        else:
            _run()

    def _audit(self, card_id: str, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self.db.append_audit_event(
                entity_type="decision_card",
                entity_id=card_id,
                event_type=event_type,
                actor="feishu_channel",
                summary=event_type,
                payload=payload,
            )
        except Exception:
            pass
