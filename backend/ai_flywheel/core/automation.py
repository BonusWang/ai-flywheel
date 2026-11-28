"""Automation scheduler & background daemon control plane.

Owns the APScheduler ``daily_flywheel`` job (always evaluated in Asia/Shanghai time),
hot-reloads schedule changes without restarting ``flywheel serve``, tracks recovery
watchdog passes, finds the scheduled fire the service was not awake for and catches
it up, and builds the status snapshot served by ``GET /api/site/automations``.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ai_flywheel.adapters.runners.clients import resolve_runner_mode
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.orchestrator import EventBroker, WorkflowLockError, WorkflowOrchestrator
from ai_flywheel.core.time_utils import SHANGHAI_TZ, shanghai_now_iso, shanghai_today_ymd
from ai_flywheel.models import QueueStatus, RunStatus, SessionStatus, WorkflowRunRecord
from ai_flywheel.storage.db import FlywheelDB

logger = logging.getLogger("ai_flywheel.automation")

DAILY_JOB_ID = "daily_flywheel"
SHANGHAI_TZ_NAME = "Asia/Shanghai"

#: Audit trail of a scheduled fire that passed without a batch (at most one per fire).
MISFIRE_AUDIT_EVENT = "schedule_misfire_detected"
#: Where that audit event lives: one entity, the daily job itself.
MISFIRE_AUDIT_ENTITY = ("automation", DAILY_JOB_ID)
#: How many of those events are read to tell whether one fire is already on record.
#: Each check records at most the newest fire, so the one asked about is near the top.
MISFIRE_AUDIT_LOOKBACK = 20
#: Where a scheduled fire is claimed. The cron job and the catch-up both write and read
#: it, so a fire is started at most once however the two of them race: one row of
#: ``system_settings`` whose value maps each claimed fire — ISO 8601 in Shanghai time —
#: to the batch that took it.
FIRE_CLAIMS_KEY = f"{DAILY_JOB_ID}:fire_claims"
FIRE_CLAIMS_CATEGORY = "automation"
FIRE_CLAIMS_ACTOR = "automation"
#: How rarely the recovery loop looks for a missed fire. The window it may catch a
#: fire up in is measured in hours, so a few minutes change nothing.
CATCHUP_CHECK_INTERVAL_SECONDS = 300.0

_DOW_LABELS: dict[str, str] = {
    "0": "周日",
    "7": "周日",
    "sun": "周日",
    "1": "周一",
    "mon": "周一",
    "2": "周二",
    "tue": "周二",
    "3": "周三",
    "wed": "周三",
    "4": "周四",
    "thu": "周四",
    "5": "周五",
    "fri": "周五",
    "6": "周六",
    "sat": "周六",
}


def normalize_cron(expr: str) -> str:
    """Collapse surrounding/internal whitespace and verify 5-field crontab shape."""
    cleaned = " ".join((expr or "").strip().split())
    parts = cleaned.split(" ") if cleaned else []
    if len(parts) != 5:
        raise ValueError(
            f"Cron 表达式必须为 5 段（分 时 日 月 周），当前为 {len(parts)} 段：{expr!r}"
        )
    return cleaned


def validate_cron(expr: str) -> tuple[str, CronTrigger]:
    """Validate a 5-field crontab expression in Asia/Shanghai time."""
    cleaned = normalize_cron(expr)
    try:
        trigger = CronTrigger.from_crontab(cleaned, timezone=SHANGHAI_TZ)
    except Exception as exc:
        raise ValueError(f"非法的 Cron 表达式 {cleaned!r}：{exc}") from exc
    return cleaned, trigger


def describe_cron(expr: str) -> str:
    """Render common 5-field cron expressions as readable Chinese text."""
    try:
        cleaned = normalize_cron(expr)
    except ValueError:
        return f"无效表达式：{expr}"
    minute, hour, dom, month, dow = cleaned.split(" ")
    if re.fullmatch(r"\d{1,2}", minute) and re.fullmatch(r"\d{1,2}", hour):
        hhmm = f"{int(hour):02d}:{int(minute):02d}"
        if dom == "*" and month == "*":
            if dow == "*":
                return f"每天 {hhmm}（上海时间）"
            dow_low = dow.lower()
            if dow_low in {"1-5", "mon-fri"}:
                return f"工作日 {hhmm}（上海时间）"
            if dow_low in {"0,6", "6,0", "sat,sun", "sun,sat"}:
                return f"周末 {hhmm}（上海时间）"
            if dow_low in _DOW_LABELS:
                return f"每{_DOW_LABELS[dow_low]} {hhmm}（上海时间）"
    if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
        step = minute[2:]
        if step.isdigit():
            return f"每 {int(step)} 分钟"
    if re.fullmatch(r"\d{1,2}", minute) and hour.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        step = hour[2:]
        if step.isdigit():
            return f"每 {int(step)} 小时第 {int(minute):02d} 分（上海时间）"
    return f"Cron: {cleaned}（上海时间）"


def next_fire_time_iso(expr: str, *, now: datetime | None = None) -> str | None:
    """Compute the next Asia/Shanghai trigger timestamp for a 5-field cron expression."""
    _, trigger = validate_cron(expr)
    base = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI_TZ)
    nxt = trigger.get_next_fire_time(None, base)
    if nxt is None:
        return None
    return nxt.astimezone(SHANGHAI_TZ).isoformat()


def recovery_watchdog_settings() -> tuple[float, int]:
    """Single source of truth for the recovery watchdog interval & stale threshold."""
    interval = float(os.environ.get("FLYWHEEL_RECOVERY_INTERVAL", "30"))
    stale_after = int(os.environ.get("FLYWHEEL_RECOVERY_STALE_SECONDS", "300"))
    return interval, stale_after


def _as_shanghai(value: datetime | None) -> datetime:
    """An aware Shanghai time; a naive value is read as a Shanghai time."""
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=SHANGHAI_TZ)
    return current.astimezone(SHANGHAI_TZ)


def _parse_utc(value: str | None) -> datetime | None:
    """Read a stored ISO 8601 timestamp (UTC); one that cannot be read is ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _fire_key(fired_at: datetime) -> str:
    """The one text a fire is known by: ISO 8601 in Shanghai time, the same in the claim
    store and in the audit trail for the cron job and for the catch-up."""
    return fired_at.astimezone(SHANGHAI_TZ).isoformat()


def _latest_fire_within(trigger: CronTrigger, earliest: datetime, latest: datetime) -> datetime | None:
    """The last time in ``[earliest, latest]`` at which ``trigger`` fires."""
    if earliest > latest:
        return None
    fire = trigger.get_next_fire_time(None, earliest)
    found: datetime | None = None
    # A cron expression fires at most once a minute, so the span bounds the loop.
    for _ in range(int((latest - earliest).total_seconds() // 60) + 2):
        if fire is None or fire > latest:
            break
        found = fire
        fire = trigger.get_next_fire_time(fire, fire)
    return found


def _latest_fire_by(
    trigger: CronTrigger, current: datetime, *, window_opens: datetime | None = None
) -> datetime | None:
    """The latest time at or before ``current`` at which ``trigger`` should have fired.

    Today's (Shanghai). A day that has no fire of its own yet looks back to the latest
    fire of the day before, but only from ``window_opens`` on — the moment the grace
    window reaches back to. A 23:00 fire is as lost at 00:30 the next day as it was at
    23:05 while its window is open; one whose window closed before midnight is not
    looked back at. Without ``window_opens`` it is today's fires alone.
    """
    start_of_day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    latest = _latest_fire_within(trigger, start_of_day, current)
    if latest is not None or window_opens is None or window_opens >= start_of_day:
        return latest
    return _latest_fire_within(trigger, window_opens, start_of_day - timedelta(microseconds=1))


class AutomationManager:
    """Lifecycle-managed scheduler & background daemon telemetry for AI Flywheel."""

    def __init__(
        self,
        cfg: FlywheelConfig,
        db: FlywheelDB,
        orchestrator: WorkflowOrchestrator,
        broker: EventBroker,
        *,
        process_role: str = "cli",
        feishu_provider: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
        catchup_runner: Callable[[str], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.orchestrator = orchestrator
        self.broker = broker
        self.process_role = process_role
        self._feishu_provider = feishu_provider
        self._lock = threading.RLock()
        self._scheduler: BackgroundScheduler | None = None
        self._started = False
        self._cron_error: str | None = None
        # An injectable clock — the store's — so "it is 21:15" can be stated in a
        # test and the audit trail it reads agrees.
        self._clock = clock
        # When this process came up. /health and the automation snapshot report it, so
        # "was the service running at the scheduled time?" can be answered from outside.
        self.started_at: str = self._now().isoformat()
        # One misfire audit and one catch-up batch per scheduled fire (per process). A
        # busy lock does not count as that attempt.
        self._misfire_audited_fire = ""
        self._catchup_run_id: str | None = None
        # What starts the catch-up batch for a run id (replaced in tests).
        self._catchup_runner = catchup_runner or self._run_catchup_batch
        # The claims on scheduled fires. ``None`` until the row in ``system_settings`` is
        # first read; from then on this process's own view of it, kept current by its
        # writes. A claim carries the id of the process that made it, so a restarted
        # process can tell its own from one it inherited.
        self._instance = uuid.uuid4().hex
        self._fire_claims: dict[str, dict[str, Any]] | None = None

        # Recovery watchdog telemetry (updated by the serve recovery thread & manual trigger)
        self._recovery_interval, self._recovery_stale_after = recovery_watchdog_settings()
        self._recovery_last_pass_at: str | None = None
        self._recovery_last_reaped_count = 0
        self._recovery_total_reaped_count = 0
        self._recovery_last_error: str | None = None

    def _now(self, now: datetime | None = None) -> datetime:
        """The current time as an aware Shanghai time: the one given, the injected clock, or now."""
        if now is not None:
            return _as_shanghai(now)
        return _as_shanghai(self._clock() if self._clock is not None else None)

    # ---------------- scheduler lifecycle ----------------

    @property
    def role_active(self) -> bool:
        return self.process_role == "serve"

    @property
    def scheduler_running(self) -> bool:
        with self._lock:
            return bool(
                self._started
                and self._scheduler is not None
                and self._scheduler.running
                and self._scheduler.get_job(DAILY_JOB_ID) is not None
            )

    def start(self, *, process_role: str | None = None) -> None:
        """Start the background scheduler when running in the ``serve`` role."""
        with self._lock:
            if process_role is not None:
                self.process_role = process_role
            if not self.role_active:
                return
            self._started = True
            self._sync_locked()

    def stop(self) -> None:
        """Shut down APScheduler cleanly on service exit."""
        with self._lock:
            self._started = False
            sched = self._scheduler
            self._scheduler = None
            if sched is not None and sched.running:
                try:
                    sched.shutdown(wait=False)
                except Exception:
                    logger.warning("scheduler shutdown failed", exc_info=True)

    def sync_from_config(self) -> None:
        """Hot-reload the live scheduler job from ``self.cfg.workflow``."""
        with self._lock:
            self._sync_locked()

    def _sync_locked(self) -> None:
        self._cron_error = None
        if not (self._started and self.role_active):
            return

        if not self.cfg.workflow.schedule_enabled:
            if self._scheduler is not None and self._scheduler.running:
                try:
                    if self._scheduler.get_job(DAILY_JOB_ID) is not None:
                        self._scheduler.remove_job(DAILY_JOB_ID)
                except Exception:
                    logger.warning("failed removing daily_flywheel job", exc_info=True)
            return

        try:
            _, trigger = validate_cron(self.cfg.workflow.daily_cron)
        except ValueError as exc:
            self._cron_error = str(exc)
            logger.error("cannot schedule daily_flywheel: %s", exc)
            return

        if self._scheduler is None or not self._scheduler.running:
            self._scheduler = BackgroundScheduler(timezone=SHANGHAI_TZ)
            self._scheduler.start()

        self._scheduler.add_job(
            self._run_scheduled_daily,
            trigger=trigger,
            id=DAILY_JOB_ID,
            name="每日自动沉淀批（P00-P07）",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            # How late the daily batch may still run after its fire time. A host that
            # slept over the cron time, or a service that was too busy to fire, resumes
            # the batch when it wakes instead of losing it.
            misfire_grace_time=int(self.cfg.workflow.misfire_grace_seconds),
        )

    def _run_scheduled_daily(self) -> None:
        if not self.cfg.safety_valves.workflow_enabled:
            logger.info("scheduled daily run skipped: safety_valves.workflow_enabled is false")
            return
        try:
            self._start_cron_batch()
        except WorkflowLockError as exc:
            logger.warning("scheduled daily run skipped (workflow lock busy): %s", exc)
        except Exception:
            logger.exception("scheduled daily run failed")
        finally:
            self.broker.publish("automation_updated", {"reason": "scheduled_run_finished"})

    def _start_cron_batch(self) -> str | None:
        """Start the batch of the fire the scheduler woke the daily job for (its run id).

        The fire is named the way the catch-up names it and claimed in the same store,
        so whichever of the two reaches a fire first, the other leaves it: the recovery
        loop may have caught a fire up moments before the scheduler thread woke for it.
        A fire that cannot be named — the cron expression is invalid by now — is run
        without a claim rather than not run. Nothing is started for a fire already taken,
        and a busy lock raises ``WorkflowLockError`` with no claim made.
        """
        current = self._now()
        fired_at: datetime | None = None
        try:
            _, trigger = validate_cron(self.cfg.workflow.daily_cron)
        except ValueError as exc:
            logger.warning("cannot name the fire of the daily job, running it unclaimed: %s", exc)
        else:
            fired_at = self._fire_to_look_at(trigger, current)
        if fired_at is not None and self._fire_answered(fired_at, now=current):
            logger.info("scheduled daily run skipped: the fire at %s is already taken", _fire_key(fired_at))
            return None
        run_id = self._claim_and_start(
            fired_at,
            source="cron",
            now=current,
            start=lambda run_id: self.orchestrator.start_run(
                trigger="scheduled", phase="auto", run_id=run_id
            ),
        )
        if run_id is None and fired_at is not None:
            logger.info("scheduled daily run skipped: the fire at %s is already taken", _fire_key(fired_at))
        return run_id

    # ---------------- missed fire: detect, record, catch up ----------------

    def _fire_to_look_at(self, trigger: CronTrigger, current: datetime) -> datetime | None:
        """The scheduled fire a batch may still owe at ``current``: today's latest one, or
        — until today has one of its own — the latest one of the day before, for as long
        as the grace window reaches back to it."""
        window = timedelta(seconds=int(self.cfg.workflow.misfire_grace_seconds))
        return _latest_fire_by(trigger, current, window_opens=current - window)

    def scheduled_today(self, now: datetime | None = None) -> datetime | None:
        """The latest scheduled fire (Shanghai time) that a batch may still owe.

        Today's latest one; before today has one, the latest one of the day before while
        its grace window is open — a 23:00 batch is as lost at 00:30 the next day as it
        was at 23:05. ``None`` when there is no such fire or the cron expression is invalid.
        """
        try:
            _, trigger = validate_cron(self.cfg.workflow.daily_cron)
        except ValueError:
            return None
        return self._fire_to_look_at(trigger, self._now(now))

    def _scheduled_run_since(self, fired_at: datetime) -> bool:
        """Whether a scheduled batch has started at or after ``fired_at`` (a catch-up counts).

        A day-level "did anything scheduled run today" is not enough: a cron may fire
        several times a day, and an early batch must not hide a later fire that was lost.
        """
        for run in self.db.list_workflow_runs(limit=200):
            if run.trigger != "scheduled":
                continue
            started = _parse_utc(run.started_at or run.created_at)
            if started is not None and started >= fired_at:
                return True
        return False

    def _has_backlog(self) -> bool:
        """Whether there is anything for a batch to do: queued items or sessions awaiting review."""
        return (
            self.db.count_queue_items(status=QueueStatus.QUEUED.value) > 0
            or self.db.count_sessions(status=SessionStatus.PENDING.value) > 0
        )

    def _misfire_audit_on_record(self, fire: str) -> bool:
        """Whether the lost fire ``fire`` (its key) is already in the audit trail."""
        entity_type, entity_id = MISFIRE_AUDIT_ENTITY
        for row in self.db.list_audit_events(
            entity_type=entity_type, entity_id=entity_id, limit=MISFIRE_AUDIT_LOOKBACK
        ):
            payload = row.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            if row.get("event_type") == MISFIRE_AUDIT_EVENT and payload.get("scheduled_today") == fire:
                return True
        return False

    # ---------------- the claim on a scheduled fire ----------------

    def _read_fire_claims(self) -> dict[str, dict[str, Any]]:
        """The claim row as it is stored; what cannot be read or understood is no claim."""
        try:
            rows = self.db.list_system_settings()
        except Exception:
            logger.warning("could not read the claims on scheduled fires", exc_info=True)
            return {}
        for row in rows:
            if row.get("key") != FIRE_CLAIMS_KEY:
                continue
            value = row.get("value")
            if not isinstance(value, dict):
                return {}
            return {
                str(fire): claim
                for fire, claim in value.items()
                if isinstance(claim, dict) and claim.get("run_id")
            }
        return {}

    def _write_fire_claims(self, claims: dict[str, dict[str, Any]]) -> None:
        try:
            self.db.set_system_setting(
                FIRE_CLAIMS_KEY, FIRE_CLAIMS_CATEGORY, claims, updated_by=FIRE_CLAIMS_ACTOR
            )
        except Exception:
            # This process still knows its own claims. What is lost is the record of them
            # for a later one, and a later one follows a claim only through the run
            # record of its batch anyway.
            logger.warning("could not store the claims on scheduled fires", exc_info=True)

    def _live_fire_claims(self, now: datetime) -> dict[str, dict[str, Any]]:
        """The claims that still count, by fire; the stored row is trimmed to match.

        Two things end a claim. The grace window of its fire closes: nothing catches that
        fire up any more. Or it turns out never to have come to a batch: a claim left by
        an earlier process counts only through the run record of the batch it started.
        A batch that is not on record after a restart never really started — its fire is
        to be caught up again, not written off.
        """
        window = timedelta(seconds=int(self.cfg.workflow.misfire_grace_seconds))
        with self._lock:
            if self._fire_claims is None:
                self._fire_claims = self._read_fire_claims()
            live: dict[str, dict[str, Any]] = {}
            for fire, claim in self._fire_claims.items():
                fired_at = _parse_utc(fire)
                if fired_at is None or fired_at + window < now:
                    continue
                inherited = claim.get("process") != self._instance
                if inherited and self.db.get_workflow_run(str(claim.get("run_id"))) is None:
                    continue
                live[fire] = claim
            if live != self._fire_claims:
                self._fire_claims = live
                self._write_fire_claims(live)
            return live

    def _fire_answered(self, fired_at: datetime, *, now: datetime) -> bool:
        """Whether a batch was started for ``fired_at``: one is on record, or the fire is claimed."""
        if self._scheduled_run_since(fired_at):
            return True
        return _fire_key(fired_at) in self._live_fire_claims(now)

    def _record_fire_claim(
        self, fired_at: datetime, run_id: str, *, source: str, now: datetime
    ) -> None:
        """Write down that the batch ``run_id`` took the fire at ``fired_at``.

        The caller holds the workflow lock, which makes the claim exclusive: no other
        start can be between its lock and its claim at the same time.
        """
        with self._lock:
            claims = dict(self._live_fire_claims(now))
            claims[_fire_key(fired_at)] = {
                "run_id": run_id,
                "source": source,
                "claimed_at": now.isoformat(),
                "process_started_at": self.started_at,
                "process": self._instance,
            }
            self._fire_claims = claims
            self._write_fire_claims(claims)

    def _withdraw_fire_claim(self, run_id: str) -> None:
        """Take back the claim a start that failed left: its fire is not answered."""
        with self._lock:
            if self._fire_claims is None:
                return
            claims = {
                fire: claim
                for fire, claim in self._fire_claims.items()
                if claim.get("run_id") != run_id
            }
            if claims == self._fire_claims:
                return
            self._fire_claims = claims
            self._write_fire_claims(claims)

    def _claim_and_start(
        self,
        fired_at: datetime | None,
        *,
        source: str,
        now: datetime,
        start: Callable[[str], Any],
    ) -> str | None:
        """Take the lock, re-check & claim ``fired_at``, and start its batch; the batch's run id.

        The order is the point. The lock comes first, in the calling thread: when it is
        busy, ``WorkflowLockError`` is raised and nothing has been claimed, so the fire
        is left for a later pass. Once the lock is held, ``_fire_answered`` is checked
        again under the lock to close any TOCTOU window between cron and catch-up; if the
        fire was already taken, the lock is released and ``None`` is returned. Otherwise
        the claim goes on record and the batch is started under the lock. A start that
        fails gives both back, the claim and the lock: that attempt is not the fire's.
        """
        run_id = self.orchestrator.new_run_id()
        if not self.db.acquire_workflow_lock(run_id):
            raise WorkflowLockError(f"the workflow lock is held: {self.db.get_workflow_lock()}")
        if fired_at is not None:
            if self._fire_answered(fired_at, now=now):
                self.db.release_workflow_lock(run_id)
                return None
            self._record_fire_claim(fired_at, run_id, source=source, now=now)
        try:
            start(run_id)
        except Exception:
            self._withdraw_fire_claim(run_id)
            self.db.release_workflow_lock(run_id)
            raise
        return run_id

    def check_missed_schedule_catchup(self, now: datetime | None = None) -> dict[str, Any]:
        """Find the scheduled fire the service was not awake for, record it, catch it up.

        APScheduler runs only a fire it is awake for, and remembers one only for as long
        as this process lives. The job's misfire grace covers a host that slept over the
        cron time; it cannot cover a service that was started, or had its schedule
        re-enabled, *after* the cron time — that batch was lost without a trace. This
        check gets it back. The ``serve`` recovery loop calls it now and then; it is safe
        to call as often as that loop runs.

        A miss is confirmed when the latest fire has passed — today's, or yesterday's
        while its grace window is still open — no batch has been started for it, the
        window is still open and there is work queued. It is then written to the audit
        trail once per fire and — when ``catchup_missed_schedule`` is on — one catch-up
        batch is started for it, also once. What makes it once is the claim the cron job
        shares (``_claim_and_start``). A busy lock is not that one attempt: the batch is
        left for a later pass. A process that is not in the ``serve`` role never starts
        a batch.
        """
        current = self._now(now)
        grace = int(self.cfg.workflow.misfire_grace_seconds)
        window = timedelta(seconds=grace)
        result: dict[str, Any] = {
            "checked_at": current.isoformat(),
            "scheduled_today": None,
            "catchup_window_until": None,
            "missed_today": False,
            "misfire_audited": False,
            "catchup_started": False,
            "run_id": None,
            "reason": "",
        }
        if not self.cfg.workflow.schedule_enabled:
            result["reason"] = "schedule_disabled"
            return result
        try:
            _, trigger = validate_cron(self.cfg.workflow.daily_cron)
        except ValueError as exc:
            result["reason"] = f"invalid_cron: {exc}"
            return result
        fired_at = self._fire_to_look_at(trigger, current)
        if fired_at is None:
            result["reason"] = "not_due_yet"
            return result
        fire = _fire_key(fired_at)
        result["scheduled_today"] = fire
        result["catchup_window_until"] = (fired_at + window).isoformat()
        if self._scheduled_run_since(fired_at):
            result["reason"] = "already_ran_today"
            return result
        claim = self._live_fire_claims(current).get(fire)
        if claim is not None and claim.get("source") != "catchup":
            # The cron job of this process took the fire; its batch is not on record yet.
            result["reason"] = "already_ran_today"
            return result
        result["missed_today"] = True
        if not self.role_active:
            result["reason"] = "role_inactive"
            return result
        if not self.cfg.safety_valves.workflow_enabled:
            result["reason"] = "workflow_disabled"
            return result
        if current > fired_at + window:
            result["reason"] = "outside_grace_window"
            return result
        if not self._has_backlog():
            result["reason"] = "no_backlog"
            return result

        # Confirmed: a fire was lost and there is work it should have done. It is recorded
        # once per fire — in this process, and (after a restart) by the audit trail itself.
        with self._lock:
            audited_before = self._misfire_audited_fire == fire
        if not audited_before and self._misfire_audit_on_record(fire):
            audited_before = True
        if audited_before:
            result["misfire_audited"] = True
        else:
            self.db.append_audit_event(
                entity_type=MISFIRE_AUDIT_ENTITY[0],
                entity_id=MISFIRE_AUDIT_ENTITY[1],
                event_type=MISFIRE_AUDIT_EVENT,
                actor="automation",
                summary=(
                    f"{fired_at.strftime('%Y-%m-%d %H:%M')} 的定时沉淀未运行：应触发时间已过、"
                    f"此后没有计划批次，而待处理工作存在；宽限窗口至 "
                    f"{(fired_at + window).strftime('%Y-%m-%d %H:%M')}"
                ),
                reason_code="SCHEDULE_MISFIRE",
                payload={
                    "scheduled_today": fire,
                    "catchup_window_until": result["catchup_window_until"],
                    "misfire_grace_seconds": grace,
                    "catchup_missed_schedule": bool(self.cfg.workflow.catchup_missed_schedule),
                    "process_started_at": self.started_at,
                    "process_role": self.process_role,
                },
            )
            with self._lock:
                self._misfire_audited_fire = fire
            result["misfire_audited"] = True
        if not self.cfg.workflow.catchup_missed_schedule:
            result["reason"] = "catchup_disabled"
            return result
        if claim is not None:
            # This process already started its catch-up for this fire.
            result["reason"] = "catchup_already_started"
            return result

        try:
            run_id = self._claim_and_start(
                fired_at, source="catchup", now=current, start=self._catchup_runner
            )
        except WorkflowLockError:
            # A busy lock is a clean "not now", not a failure nobody sees.
            logger.warning(
                "scheduled catch-up for %s skipped: the workflow lock is busy; "
                "it is retried while the grace window is open",
                fire,
            )
            result["reason"] = "lock_busy"
            return result
        if run_id is None:
            latest_claim = self._live_fire_claims(current).get(fire)
            if latest_claim is not None and latest_claim.get("source") == "catchup":
                result["reason"] = "catchup_already_started"
            else:
                result["missed_today"] = False
                result["reason"] = "already_ran_today"
            return result
        with self._lock:
            self._catchup_run_id = run_id
        logger.info("scheduled catch-up run %s started for the missed fire at %s", run_id, fire)
        result["catchup_started"] = True
        result["run_id"] = run_id
        result["reason"] = "catchup_started"
        return result

    def _run_catchup_batch(self, run_id: str) -> None:
        """Start the catch-up batch on its own worker thread (the lock is already held).

        The batch never runs on the recovery thread: a batch can take minutes, and
        recovery has to keep passing while it does. This is the same handshake the
        portal's background trigger uses — the lock is taken by the caller, the
        pipeline runs elsewhere.
        """

        def _runner() -> None:
            try:
                self.orchestrator.start_run(trigger="scheduled", phase="auto", run_id=run_id)
            except Exception:
                logger.exception("scheduled catch-up run %s failed", run_id)
                # The batch never took ownership: the lock this process took must not
                # stay, and the fire it was claimed for is not answered.
                self._withdraw_fire_claim(run_id)
                self.db.release_workflow_lock(run_id)
                with self._lock:
                    if self._catchup_run_id == run_id:
                        self._catchup_run_id = None
            finally:
                self.broker.publish("automation_updated", {"reason": "scheduled_run_finished"})

        threading.Thread(target=_runner, name=f"flywheel-catchup-{run_id}", daemon=True).start()

    # ---------------- recovery watchdog ----------------

    def record_recovery_pass(self, reaped_ids: list[str], error: str | None = None) -> None:
        with self._lock:
            self._recovery_last_pass_at = shanghai_now_iso()
            self._recovery_last_reaped_count = len(reaped_ids)
            self._recovery_total_reaped_count += len(reaped_ids)
            self._recovery_last_error = error

    def run_recovery_now(self, stale_after_seconds: int | None = None) -> dict[str, Any]:
        stale = self._recovery_stale_after if stale_after_seconds is None else int(stale_after_seconds)
        try:
            reaped = self.orchestrator.recover_interrupted_runs(stale_after_seconds=stale)
        except Exception as exc:
            self.record_recovery_pass([], error=str(exc))
            raise
        reaped_ids = list(reaped)
        self.record_recovery_pass(reaped_ids, error=None)
        self.broker.publish(
            "automation_updated",
            {"reason": "manual_recovery", "reaped_count": len(reaped_ids)},
        )
        return {
            "reaped_count": len(reaped_ids),
            "reaped_run_ids": reaped_ids,
            "checked_at": self._recovery_last_pass_at,
        }

    # ---------------- read snapshot ----------------

    def get_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        current = self._now(now)
        today = shanghai_today_ymd(current)
        runs = self.db.list_workflow_runs(limit=200)
        scheduled_runs = [r for r in runs if r.trigger == "scheduled"]
        today_runs = [r for r in runs if r.batch_date == today]
        today_scheduled = [r for r in scheduled_runs if r.batch_date == today]
        latest_run = runs[0] if runs else None
        latest_scheduled = scheduled_runs[0] if scheduled_runs else None

        cron_expr = (self.cfg.workflow.daily_cron or "0 20 * * *").strip()
        cron_error = self._cron_error
        preview_next: str | None = None
        try:
            preview_next = next_fire_time_iso(cron_expr, now=current)
        except ValueError as exc:
            cron_error = cron_error or str(exc)

        # The fire to look at: whether it has passed, whether it was lost, and until when
        # the recovery loop may still catch it up.
        grace = int(self.cfg.workflow.misfire_grace_seconds)
        scheduled_today: datetime | None = None
        catchup_window_until: datetime | None = None
        missed_today = False
        if self.cfg.workflow.schedule_enabled:
            try:
                _, trigger = validate_cron(self.cfg.workflow.daily_cron)
            except ValueError:
                trigger = None
            if trigger is not None:
                scheduled_today = self._fire_to_look_at(trigger, current)
                if scheduled_today is not None:
                    catchup_window_until = scheduled_today + timedelta(seconds=grace)
                    missed_today = not self._fire_answered(scheduled_today, now=current)

        with self._lock:
            live_next: str | None = None
            job_active = False
            if self._started and self._scheduler is not None and self._scheduler.running:
                job = self._scheduler.get_job(DAILY_JOB_ID)
                if job is not None:
                    job_active = True
                    if job.next_run_time is not None:
                        live_next = job.next_run_time.astimezone(SHANGHAI_TZ).isoformat()
            catchup_run_id = self._catchup_run_id
            recovery_info = {
                "role_active": self.role_active,
                "interval_seconds": self._recovery_interval,
                "stale_after_seconds": self._recovery_stale_after,
                "last_pass_at": self._recovery_last_pass_at,
                "last_reaped_count": self._recovery_last_reaped_count,
                "total_reaped_count": self._recovery_total_reaped_count,
                "last_error": self._recovery_last_error,
            }

        pending_sessions = self.db.count_sessions(status=SessionStatus.PENDING.value)
        awaiting_runs = sum(1 for r in runs if r.status == RunStatus.AWAITING_APPROVAL)
        feishu_info = (
            self._feishu_provider()
            if self._feishu_provider is not None
            else {
                "enabled": bool(self.cfg.delivery.feishu_cli.enabled),
                "interactive": bool(self.cfg.delivery.feishu_cli.interactive),
                "availability": "disabled",
                "listener_state": "stopped",
                "check_valid": False,
            }
        )

        recent_items = []
        for r in runs[:100]:
            duration_ms = sum(int(n.duration_ms or 0) for n in r.nodes)
            recent_items.append(
                {
                    "run_id": r.run_id,
                    "batch_date": r.batch_date,
                    "batch_seq": r.batch_seq,
                    "trigger": r.trigger,
                    "phase": r.phase,
                    "status": r.status.value,
                    "runner_mode": r.runner_mode,
                    "approval_mode": r.approval_mode,
                    "admitted_count": len(r.admitted_sessions),
                    "planned_writes_count": len(r.planned_writes),
                    "duration_ms": duration_ms,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "created_at": r.created_at,
                    "error": r.error,
                }
            )

        return {
            "scheduler": {
                "enabled": bool(self.cfg.workflow.schedule_enabled),
                "running": job_active,
                "role_active": self.role_active,
                "timezone": SHANGHAI_TZ_NAME,
                "cron": cron_expr,
                "human_schedule": describe_cron(cron_expr),
                "friday_weekly_enabled": bool(self.cfg.workflow.friday_weekly_enabled),
                "next_run_at": live_next if job_active else None,
                "preview_next_run_at": preview_next,
                "last_scheduled_run_at": (
                    (latest_scheduled.started_at or latest_scheduled.created_at)
                    if latest_scheduled is not None
                    else None
                ),
                "last_run_at": (
                    (latest_run.started_at or latest_run.created_at) if latest_run is not None else None
                ),
                "last_run_id": latest_run.run_id if latest_run is not None else None,
                "last_run_status": latest_run.status.value if latest_run is not None else None,
                "last_run_trigger": latest_run.trigger if latest_run is not None else None,
                "cron_error": cron_error,
                # When this process came up, and how a scheduled fire it was not awake
                # for is handled: how late a batch may still run, whether the recovery
                # loop catches a lost one up, and whether the latest one — today's, or
                # yesterday's inside its grace window — is still outstanding.
                "started_at": self.started_at,
                "misfire_grace_seconds": grace,
                "catchup_missed_schedule": bool(self.cfg.workflow.catchup_missed_schedule),
                "missed_today": missed_today,
                "scheduled_today": scheduled_today.isoformat() if scheduled_today else None,
                "catchup_window_until": catchup_window_until.isoformat() if catchup_window_until else None,
                "catchup_run_id": catchup_run_id,
            },
            "metrics": {
                "today_date": today,
                "today_runs_count": len(today_runs),
                "today_passed_count": sum(1 for r in today_runs if r.status == RunStatus.PASS),
                "today_awaiting_count": sum(1 for r in today_runs if r.status == RunStatus.AWAITING_APPROVAL),
                "today_failed_count": sum(1 for r in today_runs if r.status == RunStatus.FAILED),
                "today_scheduled_ran": len(today_scheduled) > 0,
                "total_scheduled_runs": len(scheduled_runs),
                "total_scheduled_passed": sum(1 for r in scheduled_runs if r.status == RunStatus.PASS),
                "total_runs": self.db.count_workflow_runs(),
                "pending_sessions": pending_sessions,
                "awaiting_approval_runs": awaiting_runs,
            },
            "policy": {
                "workflow_enabled": bool(self.cfg.safety_valves.workflow_enabled),
                "approval_mode": self.cfg.workflow.approval_mode,
                "effective_approval_mode": self.cfg.effective_approval_mode,
                "runner_mode": resolve_runner_mode(self.cfg),
                "max_sessions_per_run": self.cfg.workflow.max_sessions_per_run,
                "max_writes_per_run": self.cfg.workflow.max_writes_per_run,
            },
            "daemons": {
                "recovery": recovery_info,
                "feishu": feishu_info,
            },
            "lock": self.db.get_workflow_lock(),
            "recent_runs": recent_items,
        }
