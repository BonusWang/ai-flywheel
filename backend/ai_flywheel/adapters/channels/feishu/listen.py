"""Feishu click listener thread (feat-feishu-approval-card-loop, design D2).

One daemon thread owns the single `event consume card.action.trigger` subprocess
(the EventKey is single-consumer). stdout lines are decoded as UTF-8 with
replacement and handed to a router; stderr supplies the ready marker and failure
text. Exit classification: permanent errors (auth / another consumer) back off
60 s and stop after 5 consecutive occurrences; transient errors back off 10 s
and cap at 5 min. Audit hooks fire only on state transitions.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from typing import Callable

from ai_flywheel.adapters.lark_cli import LarkCliRunner

logger = logging.getLogger("ai_flywheel.feishu_listen")

READY_MARKER = "[event] ready"


def classify_failure(stderr_text: str, code: int) -> str:
    """permanent | occupied | transient — permanent stops after N tries."""
    low = (stderr_text or "").lower()
    if any(m in low for m in ("another consumer", "already running", "already holds")):
        return "occupied"
    if any(m in low for m in ("auth failure", "authentication", "unauthorized")) or code == 3:
        return "permanent"
    return "transient"


class FeishuClickListener:
    def __init__(
        self,
        runner: LarkCliRunner,
        *,
        on_click: Callable[[dict], None],
        on_state: Callable[[dict], None] | None = None,
        transient_backoff: float = 10.0,
        permanent_backoff: float = 60.0,
        max_permanent_failures: int = 5,
        ready_timeout: float = 30.0,
        audit: Callable[[str, str], None] | None = None,
        should_run: Callable[[], bool] | None = None,
    ) -> None:
        self._runner = runner
        # Read from the live config on every check: the consumer only runs while
        # interactive mode is on (the EventKey is single-consumer for the whole app).
        self._should_run = should_run or (lambda: True)
        self.config_poll_seconds = 5.0
        self._on_click = on_click
        self._on_state = on_state or (lambda s: None)
        self._audit = audit or (lambda kind, detail: None)
        self._transient_backoff = transient_backoff
        self._permanent_backoff = permanent_backoff
        self._max_permanent = max_permanent_failures
        self._ready_timeout = ready_timeout
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc = None
        self._proc_lock = threading.Lock()
        self._state = "stopped"
        self._state_reason = "not started"
        self._consecutive = 0
        self._seen_event_ids: set[str] = set()

    # ---------------- state ----------------

    @property
    def state(self) -> dict:
        return {"state": self._state, "reason": self._state_reason, "ready": self._state == "ready"}

    def _set_state(self, state: str, reason: str) -> None:
        prev = self._state
        self._state = state
        self._state_reason = reason
        self._on_state(self.state)
        if prev != state:
            kind = "listener_state"
            self._audit(kind, f"{prev} -> {state}: {reason}")

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._wake.clear()
        self._consecutive = 0
        self._thread = threading.Thread(target=self._loop, name="flywheel-feishu-listener", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Wake an idle or re-enable wait loop immediately after a config change."""
        self._wake.set()

    def stop(self, timeout: float = 12.0) -> None:
        self._stop.set()
        self._wake.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            # Graceful: close stdin; hard-kill the tree after a grace period.
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=timeout)
            except Exception:
                self._kill_tree(proc)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._set_state("stopped", "service shutdown")

    @staticmethod
    def _graceful_stop(proc) -> None:
        """Close stdin (the documented graceful exit), hard-kill the tree after a grace period."""
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            FeishuClickListener._kill_tree(proc)

    @staticmethod
    def _kill_tree(proc) -> None:
        try:
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------------- main loop ----------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._should_run():
                self._set_state("stopped", "interactive mode off")
                self._wake.wait(5.0)
                self._wake.clear()
                continue
            self._run_once()

    def _run_once(self) -> None:
        try:
            proc = self._runner.popen(self._runner.consume_args())
        except Exception as exc:
            self._set_state("backoff", f"cannot start consumer: {exc}")
            self._wait_or_stop(self._transient_backoff)
            return
        with self._proc_lock:
            self._proc = proc

        ready = threading.Event()
        stderr_text: list[str] = []

        def _drain_stderr() -> None:
            try:
                assert proc.stderr is not None
                for raw in iter(proc.stderr.readline, b""):
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    stderr_text.append(line)
                    if READY_MARKER in line:
                        ready.set()
            except Exception:
                pass

        def _drain_stdout() -> None:
            try:
                assert proc.stdout is not None
                for raw in iter(proc.stdout.readline, b""):
                    if self._stop.is_set():
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    self._handle_line(line)
            except Exception:
                pass

        t_err = threading.Thread(target=_drain_stderr, daemon=True)
        t_out = threading.Thread(target=_drain_stdout, daemon=True)
        t_err.start()
        t_out.start()

        self._set_state("starting", "consumer launched")

        ready.wait(self._ready_timeout)
        if ready.is_set() and not self._stop.is_set():
            self._consecutive = 0
            self._set_state("ready", "consumer ready")

        while True:
            try:
                proc.wait(timeout=self.config_poll_seconds)
                break
            except subprocess.TimeoutExpired:
                if self._stop.is_set() or not self._should_run():
                    self._graceful_stop(proc)
                    break
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)
        with self._proc_lock:
            self._proc = None

        if self._stop.is_set():
            return
        if not self._should_run():
            self._set_state("stopped", "interactive mode off")
            return

        stderr_joined = "\n".join(stderr_text[-20:])
        kind = classify_failure(stderr_joined, proc.returncode)
        if kind in ("permanent", "occupied"):
            self._consecutive += 1
            if kind == "permanent" and self._consecutive >= self._max_permanent:
                self._set_state("stopped", f"permanent failure x{self._consecutive}: {stderr_joined[-160:]}")
                # The permanent-failure limit is reached: wait instead of respawning the
                # consumer in a tight loop, which would burn the API without a pause.
                self._wait_for_reenable()
                return
            backoff = self._permanent_backoff
        else:
            self._consecutive = 0
            backoff = self._transient_backoff
        self._set_state("backoff", f"{kind} failure (exit {proc.returncode}): {stderr_joined[-160:]}")
        self._wait_or_stop(backoff)

    def _wait_for_reenable(self) -> bool:
        """Wait out a permanent failure; the listener returns when interactive mode is
        switched off and on again, and the permanent-failure counter restarts then.

        Returns False when the service is shutting down.
        """
        seen_off = False
        while not self._stop.is_set():
            if self._should_run():
                if seen_off:
                    self._consecutive = 0
                    return True
            else:
                if not seen_off:
                    seen_off = True
                    self._set_state("stopped", "interactive mode off")
            self._wake.wait(0.5)
            self._wake.clear()
        return False

    def _wait_or_stop(self, seconds: float) -> None:
        self._stop.wait(seconds)

    # ---------------- event routing ----------------

    def _handle_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            logger.debug("ignoring non-JSON consume line: %s", line[:120])
            return
        if not isinstance(event, dict):
            return
        event_id = str(event.get("event_id") or "")
        if event_id:
            if event_id in self._seen_event_ids:
                return  # duplicate delivery of the same click
            self._seen_event_ids.add(event_id)
        try:
            self._on_click(event)
        except Exception:
            logger.exception("click routing failed")
