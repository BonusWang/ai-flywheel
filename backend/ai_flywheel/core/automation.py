"""Automation scheduler & background daemon control plane.

Owns the APScheduler ``daily_flywheel`` job (always evaluated in Asia/Shanghai time),
hot-reloads schedule changes without restarting ``flywheel serve``, tracks recovery
watchdog passes, and builds the status snapshot served by ``GET /api/site/automations``.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ai_flywheel.adapters.runners.clients import resolve_runner_mode
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.orchestrator import EventBroker, WorkflowLockError, WorkflowOrchestrator
from ai_flywheel.core.time_utils import SHANGHAI_TZ, shanghai_today_ymd
from ai_flywheel.models import RunStatus, SessionStatus
from ai_flywheel.storage.db import FlywheelDB

logger = logging.getLogger("ai_flywheel.automation")

DAILY_JOB_ID = "daily_flywheel"
SHANGHAI_TZ_NAME = "Asia/Shanghai"

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


def _shanghai_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone(SHANGHAI_TZ).isoformat()


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

        # Recovery watchdog telemetry (updated by the serve recovery thread & manual trigger)
        self._recovery_interval = float(os.environ.get("FLYWHEEL_RECOVERY_INTERVAL", "30"))
        self._recovery_stale_after = int(os.environ.get("FLYWHEEL_RECOVERY_STALE_SECONDS", "300"))
        self._recovery_last_pass_at: str | None = None
        self._recovery_last_reaped_count = 0
        self._recovery_total_reaped_count = 0
        self._recovery_last_error: str | None = None

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
            misfire_grace_time=300,
        )

    def _run_scheduled_daily(self) -> None:
        if not self.cfg.safety_valves.workflow_enabled:
            logger.info("scheduled daily run skipped: safety_valves.workflow_enabled is false")
            return
        try:
            self.orchestrator.start_run(trigger="scheduled", phase="auto")
        except WorkflowLockError as exc:
            logger.warning("scheduled daily run skipped (workflow lock busy): %s", exc)
        except Exception:
            logger.exception("scheduled daily run failed")
        finally:
            self.broker.publish("automation_updated", {"reason": "scheduled_run_finished"})

    # ---------------- recovery watchdog ----------------

    def record_recovery_pass(self, reaped_ids: list[str], error: str | None = None) -> None:
        with self._lock:
            self._recovery_last_pass_at = _shanghai_now_iso()
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

    def get_snapshot(self) -> dict[str, Any]:
        today = shanghai_today_ymd()
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
            preview_next = next_fire_time_iso(cron_expr)
        except ValueError as exc:
            cron_error = cron_error or str(exc)

        with self._lock:
            live_next: str | None = None
            job_active = False
            if self._started and self._scheduler is not None and self._scheduler.running:
                job = self._scheduler.get_job(DAILY_JOB_ID)
                if job is not None:
                    job_active = True
                    if job.next_run_time is not None:
                        live_next = job.next_run_time.astimezone(SHANGHAI_TZ).isoformat()
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
