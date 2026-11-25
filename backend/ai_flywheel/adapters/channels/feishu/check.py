"""Feishu interactive-mode self-check (feat-feishu-approval-card-loop, design D10/D13).

The check is the gate: interactive ask cards are only sent after it passes. It
runs inside `serve`, reusing the running click listener (never a second
consumer): version + bot identity -> send a test card -> patch it (proving the
machine can update cards) -> wait for an approver's click -> final patch without
buttons. Results are persisted with a fingerprint; any fingerprint change
(lark-cli file/version, approver set, recipient, identity) invalidates them.
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai_flywheel.adapters.channels.feishu.card import callback_button, render_message_card
from ai_flywheel.adapters.lark_cli import LarkCliRunner
from ai_flywheel.config import FlywheelConfig

CHECK_SETTING_KEY = "feishu_check"
CHECK_RUN_ID = "__feishu_check__"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def render_check_card(card_id: str, *, state: str, note: str = "") -> str:
    """Card 2.0 test card, laid out like the ask cards: one block, one action while waiting."""
    common = {"title": "AI 飞轮链路自检", "subtitle": "发送 → 改卡 → 点击回调，三段链路验证"}
    if state == "waiting":
        return render_message_card(
            **common,
            headline="**请点击下方按钮**",
            lines=["收到这张卡说明消息已送达；点击后验证卡片回调。", *([note] if note else [])],
            background="blue-50",
            template="blue",
            icon="approve_colorful",
            tag=("待点击", "yellow"),
            button=callback_button("我收到了", card_id, "ack"),
        )
    if state == "received":
        return render_message_card(
            **common,
            headline="**<font color='green'>链路验证通过</font>**",
            lines=["已收到你的点击，三段链路均可用。本卡已完成，可忽略。"],
            background="green-50",
            template="green",
            icon="done_outlined",
            tag=("已通过", "green"),
        )
    if state == "timeout":
        return render_message_card(
            **common,
            headline="**<font color='grey'>自检超时，按钮已失效</font>**",
            lines=["等待时限内没有收到点击。请重新运行 flywheel feishu-check。"],
            background="grey-50",
            template="grey",
            icon="close_outlined",
            tag=("已失效", "neutral"),
        )
    return render_message_card(
        **common,
        headline="**<font color='red'>自检未完成</font>**",
        lines=[note] if note else [],
        background="red-50",
        template="red",
        icon="warning_outlined",
        tag=("未通过", "red"),
    )


def event_card_id(event: dict[str, Any]) -> str | None:
    """Extract the card id from a flat card.action.trigger event (value may be dict or JSON str)."""
    value = event.get("action_value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict) and isinstance(value.get("card"), str):
        return value["card"]
    return None


class FeishuCheckService:
    def __init__(
        self,
        cfg: FlywheelConfig,
        runner: LarkCliRunner,
        listener_state: dict[str, Any] | Callable,  # callable returning the listener state dict
        *,
        db=None,
    ) -> None:
        self.cfg = cfg
        self.runner = runner
        self._listener_state = listener_state if callable(listener_state) else (lambda: dict(listener_state))
        self.db = db
        self._lock = threading.Lock()
        self._running = False
        self._click_event = threading.Event()
        self._click: dict[str, Any] | None = None
        self._current_card: str | None = None
        # (cache key, fingerprint) of the last computation, so the 30-second status
        # tick neither hashes the whole binary nor spawns `lark-cli --version` again.
        self._fingerprint_cache: tuple[tuple[Any, ...], dict[str, Any]] | None = None

    # ---------------- persisted state ----------------

    def _read_setting(self) -> dict[str, Any]:
        if self.db is None:
            return {}
        for row in self.db.list_system_settings():
            if row.get("key") == CHECK_SETTING_KEY or row.get("setting_key") == CHECK_SETTING_KEY:
                value = row.get("value") or row.get("setting_value")
                return value if isinstance(value, dict) else {}
        return {}

    def _write_setting(self, value: dict[str, Any]) -> None:
        if self.db is None:
            return
        self.db.set_system_setting(CHECK_SETTING_KEY, "feishu", value, updated_by="feishu_check")

    def fingerprint(self) -> dict[str, Any]:
        """The identity of the current setup, cached on what can actually change it.

        The cache key is the resolved executable (path, mtime, size) plus the approver
        set, the recipient and the sending identity: hashing the whole binary and
        spawning ``lark-cli --version`` on every 30-second status tick is not free.
        """
        fcfg = self.cfg.delivery.feishu_cli
        exe = str(self.runner.target_path or "")
        try:
            stat = Path(exe).stat()
            file_sig = (exe, stat.st_mtime_ns, stat.st_size)
        except OSError:
            file_sig = (exe, 0, 0)
        key = (file_sig, tuple(sorted(fcfg.effective_approver_ids)), fcfg.user_id, fcfg.identity)
        with self._lock:
            cached = self._fingerprint_cache
        if cached is not None and cached[0] == key:
            return dict(cached[1])

        digest = ""
        try:
            digest = hashlib.sha256(Path(exe).read_bytes()).hexdigest()[:16]
        except OSError:
            pass
        try:
            version = self.runner.version()
        except Exception:
            version = "unknown"
        result = {
            "exe": exe,
            "version": version,
            "sha256": digest,
            "approvers": sorted(fcfg.effective_approver_ids),
            "user_id": fcfg.user_id,
            "identity": fcfg.identity,
        }
        with self._lock:
            self._fingerprint_cache = (key, dict(result))
        return result

    def status(self) -> dict[str, Any]:
        stored = self._read_setting()
        running = False
        with self._lock:
            running = self._running
        out = dict(stored)
        out["running"] = running
        if out.get("state") == "passed":
            out["valid"] = out.get("fingerprint") == self.fingerprint()
        else:
            out["valid"] = False
        return out

    # ---------------- click routing ----------------

    def handle_click(self, event: dict[str, Any]) -> bool:
        card_id = event_card_id(event)
        if not card_id or card_id != self._current_card:
            return False
        self._click = event
        self._click_event.set()
        return True

    # ---------------- leftover cleanup at serve startup ----------------

    def startup_cleanup(self) -> None:
        stored = self._read_setting()
        if stored.get("state") == "running" and stored.get("message_id"):
            try:
                self.runner.patch_card(
                    message_id=stored["message_id"],
                    card_json=render_check_card(stored.get("card_id") or "", state="timeout"),
                )
            except Exception:
                pass
            stored["state"] = "failed"
            stored["reason"] = "interrupted: serve restarted while the check was running"
            stored["at"] = _now_iso()
            self._write_setting(stored)

    # ---------------- the check itself ----------------

    def run(self, *, wait_seconds: float = 120.0) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return {"state": "failed", "reason": "a check is already running"}
            self._running = True
        try:
            return self._run_inner(wait_seconds=wait_seconds)
        finally:
            with self._lock:
                self._running = False

    def _fail(self, reason: str, *, card_id: str = "", message_id: str | None = None, fingerprint: dict | None = None) -> dict[str, Any]:
        if message_id:
            try:
                self.runner.patch_card(message_id=message_id, card_json=render_check_card(card_id, state="failed", note=reason[:80]))
            except Exception:
                pass
        result = {"state": "failed", "reason": reason, "at": _now_iso(), "card_id": card_id, "message_id": message_id}
        if fingerprint:
            result["fingerprint"] = fingerprint
        self._write_setting(result)
        return result

    def _run_inner(self, *, wait_seconds: float) -> dict[str, Any]:
        fcfg = self.cfg.delivery.feishu_cli
        fp = self.fingerprint()
        # A check in flight is persisted as "running", so an interrupted serve can find
        # the leftover test card and close it at startup.
        self._write_setting({"state": "running", "at": _now_iso(), "fingerprint": fp})

        if not fcfg.enabled:
            return self._fail("feishu_cli is disabled")
        if not fcfg.interactive:
            return self._fail("interactive mode is off (enable delivery.feishu_cli.interactive first)", fingerprint=fp)
        if not fcfg.effective_approver_ids:
            return self._fail("no approvers configured (approver_open_ids plus user_id)")
        if not fcfg.user_id or fcfg.identity != "bot":
            return self._fail("interactive mode needs a private-chat user_id and the bot identity")
        try:
            self.runner.check_version()
        except Exception as exc:
            return self._fail(f"lark-cli version check failed: {exc}", fingerprint=fp)
        if not self.runner.auth_bot_ready():
            return self._fail("lark-cli bot identity is not ready (run lark-cli auth login as bot)", fingerprint=fp)
        deadline_ready = threading.Event()

        import threading as _t

        def _watch() -> None:
            for _ in range(60):
                if self._listener_state().get("ready"):
                    deadline_ready.set()
                    return
                _t.Event().wait(0.5)

        watch = _t.Thread(target=_watch, daemon=True)
        watch.start()
        watch.join(31)
        if not deadline_ready.is_set():
            state = self._listener_state()
            return self._fail(f"click listener not ready (state={state.get('state')}, reason={str(state.get('reason'))[:120]})", fingerprint=fp)

        card_id = f"fcheck-{uuid.uuid4().hex[:8]}"
        idem = f"feishu-check-{card_id}"
        with self._lock:
            self._current_card = card_id
            self._click = None
            self._click_event.clear()

        if self.db is not None:
            # The self-check card lives in the decision-cards table under a fixed
            # placeholder run id; that run row keeps the card's reference valid.
            self.db.ensure_placeholder_run(CHECK_RUN_ID)
            self.db.save_decision_card(
                card_id=card_id,
                run_id=CHECK_RUN_ID,
                kind="fcheck",
                state="open",
                title="AI 飞轮链路自检",
                body_markdown="self-check",
                options=["我收到了"],
            )

        res = self.runner.send_interactive(
            user_id=fcfg.user_id,
            card_json=render_check_card(card_id, state="waiting"),
            idempotency_key=idem,
        )
        if res.status != "sent" or not res.message_id:
            reason = "send failed" if res.status == "failed" else "send result unknown (timeout); retry may deduplicate"
            return self._fail(f"{reason}: {res.error or ''}".strip(), card_id=card_id, fingerprint=fp)
        message_id = res.message_id
        # The test card is on its way: remember where it is, so a crash can close it.
        self._write_setting(
            {
                "state": "running",
                "at": _now_iso(),
                "card_id": card_id,
                "message_id": message_id,
                "fingerprint": fp,
            }
        )

        patch = self.runner.patch_card(message_id=message_id, card_json=render_check_card(card_id, state="waiting", note="（已确认可改卡）"))
        if patch.status != "sent":
            return self._fail(
                f"this machine cannot update cards via `lark-cli api PATCH` ({patch.error or patch.status})",
                card_id=card_id,
                message_id=message_id,
                fingerprint=fp,
            )

        clicked = self._click_event.wait(wait_seconds)
        if not clicked:
            try:
                self.runner.patch_card(message_id=message_id, card_json=render_check_card(card_id, state="timeout"))
            except Exception:
                pass
            return self._fail(
                "no click received in time; check the Feishu app's card-callback subscription "
                "(developer console -> events & callbacks -> card.action.trigger, then publish a version)",
                card_id=card_id,
                message_id=message_id,
                fingerprint=fp,
            )
        click = self._click or {}
        operator = str(click.get("operator_id") or "").strip()
        if operator not in fcfg.effective_approver_ids:
            try:
                self.runner.patch_card(message_id=message_id, card_json=render_check_card(card_id, state="failed", note="点击者不在审批人名单"))
            except Exception:
                pass
            return self._fail("clicker is not in the approver set", card_id=card_id, message_id=message_id, fingerprint=fp)

        try:
            self.runner.patch_card(message_id=message_id, card_json=render_check_card(card_id, state="received"))
        except Exception:
            pass
        if self.db is not None:
            # fcheck cards are closed via a direct state write (kind filter keeps ask queries away)
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE decision_cards SET state = 'closed', closed_at = ?, version = version + 1, updated_at = ? WHERE card_id = ?",
                    (_now_iso(), _now_iso(), card_id),
                )
        result = {
            "state": "passed",
            "at": _now_iso(),
            "card_id": card_id,
            "message_id": message_id,
            "fingerprint": fp,
            "operator": operator,
        }
        self._write_setting(result)
        return result
