"""Execution heartbeat for the workflow lock (design D3 of fix-approval-claim-and-recovery).

A daemon thread renews the lock (expiry + heartbeat_at) every ``interval`` seconds
while a run executes. Renewal errors are logged and retried on the next tick —
the thread never exits on its own. The executor's synchronous checkpoints
(P04 per write / P05 per session / P07 pre-deliver) are the authority on lock
loss; this thread only keeps the lock alive across long nodes.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger("ai_flywheel.heartbeat")


class HeartbeatManager:
    def __init__(
        self,
        renew: Callable[[str], bool],
        *,
        interval_seconds: float = 15.0,
    ) -> None:
        self._renew = renew
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_id: str | None = None
        self._last_success: str | None = None
        # start() and stop() race when a run is approved while another one is
        # being reaped: without the lock they could leave two threads running
        # against the same run id.
        self._lock = threading.Lock()

    @property
    def last_success(self) -> str | None:
        return self._last_success

    def start(self, run_id: str) -> None:
        with self._lock:
            self._stop_locked()
            self._run_id = run_id
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._loop, args=(run_id,), name=f"flywheel-heartbeat-{run_id}", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._stop_locked(timeout=timeout)

    def _stop_locked(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        self._run_id = None
        if thread is not None:
            thread.join(timeout=timeout)

    def _loop(self, run_id: str) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                if self._renew(run_id):
                    from ai_flywheel.models import utc_now_iso

                    self._last_success = utc_now_iso()
                else:
                    logger.warning("heartbeat: lock renewal returned 0 rows for %s (stolen or gone); executor checkpoints will handle", run_id)
            except Exception:  # never let the thread die on transient db errors
                logger.exception("heartbeat: renewal error for %s; will retry next tick", run_id)
