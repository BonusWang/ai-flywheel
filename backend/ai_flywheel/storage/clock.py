"""Injectable clock for lock/heartbeat/claim/recovery time (design D6 of
fix-approval-claim-and-recovery).

Production uses SystemClock; tests inject FakeClock and advance it explicitly.
db functions accept an explicit ``now`` parameter which takes precedence over
the injected clock, so tests can pass frozen timestamps directly.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


class Clock:
    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError

    def now_iso(self) -> str:
        return self.now().isoformat()

    def sleep(self, seconds: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock(Clock):
    """Manual clock for tests: construct at a start time, then ``advance``."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)
