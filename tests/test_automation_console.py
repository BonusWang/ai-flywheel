"""Tests for the Automation Console control plane & hot-reloadable scheduler."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig, WorkflowConfig
from ai_flywheel.core import automation as automation_mod
from ai_flywheel.core.automation import describe_cron, next_fire_time_iso, validate_cron
from ai_flywheel.core.time_utils import SHANGHAI_TZ
from ai_flywheel.models import (
    NodeExecution,
    NodeStatus,
    RunStatus,
    SessionRecord,
    SessionStatus,
    WorkflowRunRecord,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.clock import Clock, FakeClock


def _make_runtime(
    tmp_path: Path,
    *,
    process_role: str = "serve",
    schedule_enabled: bool = True,
    clock: Clock | None = None,
) -> FlywheelRuntime:
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.server.data_dir = str(tmp_path / "data")
    cfg.sessions.scan_sources = []
    cfg.workflow.schedule_enabled = schedule_enabled
    cfg.workflow.daily_cron = "0 20 * * *"
    cfg.workflow.friday_weekly_enabled = True
    cfg.runner.mode = "dry_run"
    return FlywheelRuntime(cfg, skip_fts_sync=True, process_role=process_role, clock=clock)


def test_cron_validation_and_human_descriptions():
    cleaned, _ = validate_cron("  0   20 * * *  ")
    assert cleaned == "0 20 * * *"
    assert describe_cron("0 20 * * *") == "每天 20:00（上海时间）"
    assert describe_cron("30 21 * * 1-5") == "工作日 21:30（上海时间）"
    assert describe_cron("15 8 * * 5") == "每周五 08:15（上海时间）"

    nxt = next_fire_time_iso("0 20 * * *", now=datetime(2026, 11, 25, 1, 0, tzinfo=timezone.utc))
    assert nxt is not None
    assert nxt.endswith("+08:00")
    assert "T20:00:00+08:00" in nxt

    for bad in ("", "0 20 * *", "0 20 * * * *", "99 20 * * *", "not a cron expr here"):
        with pytest.raises(ValueError):
            validate_cron(bad)


def test_serve_role_hot_reloads_schedule_without_restart(tmp_path: Path):
    rt = _make_runtime(tmp_path, process_role="serve", schedule_enabled=True)
    app = create_app(runtime=rt, process_role="serve")
    with TestClient(app) as client:
        snap = client.get("/api/site/automations").json()
        assert snap["scheduler"]["enabled"] is True
        assert snap["scheduler"]["running"] is True
        assert snap["scheduler"]["timezone"] == "Asia/Shanghai"
        assert snap["scheduler"]["cron"] == "0 20 * * *"
        assert snap["scheduler"]["next_run_at"] is not None
        assert snap["scheduler"]["next_run_at"].endswith("+08:00")
        assert "T20:00:00+08:00" in snap["scheduler"]["next_run_at"]

        # Hot-update cron to 21:30 — immediate, no restart required
        res = client.put(
            "/api/site/automations/schedule",
            json={"daily_cron": "30 21 * * *", "friday_weekly_enabled": False},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["restart_required"] is False
        assert body["automations"]["scheduler"]["cron"] == "30 21 * * *"
        assert body["automations"]["scheduler"]["friday_weekly_enabled"] is False
        assert "T21:30:00+08:00" in (body["automations"]["scheduler"]["next_run_at"] or "")

        # Pause scheduler live — job stops, manual runs still work
        res_off = client.put("/api/site/automations/schedule", json={"schedule_enabled": False})
        assert res_off.status_code == 200
        assert res_off.json()["automations"]["scheduler"]["enabled"] is False
        assert res_off.json()["automations"]["scheduler"]["running"] is False
        assert res_off.json()["automations"]["scheduler"]["next_run_at"] is None
        assert res_off.json()["automations"]["scheduler"]["preview_next_run_at"] is not None

        # Manual run still succeeds when auto-scheduler is paused
        run_resp = client.post("/api/site/runs", json={"phase": "daily"})
        assert run_resp.status_code == 200
        assert run_resp.json()["run"]["status"] == "PASS"

        # Re-enable scheduler live
        res_on = client.put("/api/site/automations/schedule", json={"schedule_enabled": True})
        assert res_on.status_code == 200
        assert res_on.json()["automations"]["scheduler"]["running"] is True
        assert res_on.json()["automations"]["scheduler"]["next_run_at"] is not None


def test_invalid_cron_rejected_without_corrupting_running_job(tmp_path: Path):
    rt = _make_runtime(tmp_path, process_role="serve", schedule_enabled=True)
    app = create_app(runtime=rt, process_role="serve")
    with TestClient(app) as client:
        bad = client.put("/api/site/automations/schedule", json={"daily_cron": "99 99 * * *"})
        assert bad.status_code == 400
        snap = client.get("/api/site/automations").json()
        assert snap["scheduler"]["cron"] == "0 20 * * *"
        assert snap["scheduler"]["running"] is True


def test_non_serve_role_never_starts_background_scheduler(tmp_path: Path):
    rt = _make_runtime(tmp_path, process_role="cli", schedule_enabled=True)
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        snap = client.get("/api/site/automations").json()
        assert snap["scheduler"]["enabled"] is True
        assert snap["scheduler"]["role_active"] is False
        assert snap["scheduler"]["running"] is False
        assert snap["scheduler"]["next_run_at"] is None
        # Updating cron still validates and saves without spawning a scheduler thread
        res = client.put("/api/site/automations/schedule", json={"daily_cron": "15 9 * * *"})
        assert res.status_code == 200
        assert res.json()["automations"]["scheduler"]["cron"] == "15 9 * * *"
        assert res.json()["automations"]["scheduler"]["running"] is False


def test_manual_recovery_endpoint_reaps_stale_running_run(tmp_path: Path):
    rt = _make_runtime(tmp_path, process_role="serve", schedule_enabled=False)
    stale_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    crashed = WorkflowRunRecord(
        run_id="run-stale-auto-1",
        batch_date="2026-11-25",
        batch_seq=1,
        trigger="scheduled",
        phase="daily",
        runner_mode="dry_run",
        approval_mode="smart",
        status=RunStatus.RUNNING,
        admitted_sessions=[],
        nodes=[
            NodeExecution(node_id="P00_PREPARE", name="准备与对账", actor="prepare-worker", status=NodeStatus.PASS),
            NodeExecution(node_id="P01_BEGIN", name="独占启动", actor="lock-worker", status=NodeStatus.PASS),
            NodeExecution(node_id="P02_REVIEW", name="全文审查", actor="review-worker", status=NodeStatus.RUNNING),
            NodeExecution(node_id="P03_ROUTE_PLAN", name="路由与写入计划", actor="merge-worker", status=NodeStatus.PENDING),
            NodeExecution(node_id="P04_WRITE_VERIFY", name="受控写入与验收", actor="writer-worker", status=NodeStatus.PENDING),
            NodeExecution(node_id="P05_SETTLE", name="台账与队列结算", actor="settlement-worker", status=NodeStatus.PENDING),
            NodeExecution(node_id="P06_REPORT", name="机器报告与摘要", actor="report-worker", status=NodeStatus.PENDING),
            NodeExecution(node_id="P07_DELIVER", name="交付与终态释锁", actor="delivery-worker", status=NodeStatus.PENDING),
        ],
        started_at=stale_ts,
        created_at=stale_ts,
        updated_at=stale_ts,
    )
    rt.db.save_workflow_run(crashed)
    rt.db.acquire_workflow_lock(crashed.run_id)
    with rt.db.connect() as conn:
        conn.execute(
            "UPDATE workflow_runs SET updated_at = ? WHERE run_id = ?",
            (stale_ts, crashed.run_id),
        )
        conn.execute(
            "UPDATE workflow_lock SET heartbeat_at = ? WHERE run_id = ?",
            (stale_ts, crashed.run_id),
        )

    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        resp = client.post("/api/site/automations/recover")
        assert resp.status_code == 200
        data = resp.json()
        assert data["reaped_count"] == 1
        assert "run-stale-auto-1" in data["reaped_run_ids"]
        assert data["automations"]["daemons"]["recovery"]["last_pass_at"] is not None
        assert data["automations"]["daemons"]["recovery"]["total_reaped_count"] == 1


# ---------------------------------------------------------------- lost fire: configuration & the job


def test_misfire_grace_and_catch_up_come_with_safe_defaults_and_a_lower_bound():
    assert FlywheelConfig().workflow.misfire_grace_seconds == 14400
    assert FlywheelConfig().workflow.catchup_missed_schedule is True
    assert WorkflowConfig(misfire_grace_seconds=60).misfire_grace_seconds == 60
    for too_small in (59, 0, -1):
        with pytest.raises(ValidationError):
            WorkflowConfig(misfire_grace_seconds=too_small)


def _daily_job(rt: FlywheelRuntime):
    """The one APScheduler job the manager owns."""
    job = rt.automation._scheduler.get_job("daily_flywheel")
    assert job is not None
    return job


def test_the_daily_job_waits_the_configured_grace(tmp_path: Path):
    rt = _make_runtime(tmp_path, process_role="serve")
    rt.automation.start(process_role="serve")
    try:
        # Not APScheduler's own one second: a host that wakes a little late keeps its batch.
        assert _daily_job(rt).misfire_grace_time == 14400
        rt.cfg.workflow.misfire_grace_seconds = 900
        rt.automation.sync_from_config()
        assert _daily_job(rt).misfire_grace_time == 900
    finally:
        rt.automation.stop()


def _wait_until(condition: Callable[[], bool], timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_a_wake_up_inside_the_grace_still_runs_the_daily_batch(tmp_path: Path):
    """The job's fire time passes while nothing is processed; then the scheduler resumes."""
    rt = _make_runtime(tmp_path, process_role="serve")
    rt.cfg.workflow.friday_weekly_enabled = False
    rt.automation.start(process_role="serve")
    scheduler = rt.automation._scheduler
    outcomes: list[int] = []
    scheduler.add_listener(
        lambda event: outcomes.append(event.code),
        EVENT_JOB_EXECUTED | EVENT_JOB_MISSED | EVENT_JOB_ERROR | EVENT_JOB_MAX_INSTANCES,
    )

    def _sleep_over_the_fire_time() -> None:
        scheduler.pause()
        scheduler.modify_job(
            "daily_flywheel",
            next_run_time=datetime.now(SHANGHAI_TZ) + timedelta(milliseconds=300),
        )
        # 1.5 s later that fire is about 1.2 s overdue.
        time.sleep(1.5)
        scheduler.resume()

    try:
        _sleep_over_the_fire_time()
        assert _wait_until(lambda: bool(outcomes)), "the scheduler never reported the job"
        # 1.2 s late is inside the four-hour grace: the batch runs.
        assert outcomes == [EVENT_JOB_EXECUTED]
        assert [r.trigger for r in rt.db.list_workflow_runs(limit=5)] == ["scheduled"]

        # Control: with a grace of one second the same lateness loses the batch.
        outcomes.clear()
        scheduler.modify_job("daily_flywheel", misfire_grace_time=1)
        _sleep_over_the_fire_time()
        assert _wait_until(lambda: bool(outcomes)), "the scheduler never reported the job"
        assert outcomes == [EVENT_JOB_MISSED]
        assert len(rt.db.list_workflow_runs(limit=5)) == 1
    finally:
        rt.automation.stop()


# ---------------------------------------------------------------- lost fire: detect, record, catch up


def _lost_fire_clock() -> FakeClock:
    """Stopped at 21:15 Shanghai time on a Wednesday (2026-11-25): a 20:00 fire never ran."""
    return FakeClock(datetime(2026, 11, 25, 13, 15, tzinfo=timezone.utc))


def _evening(hour: int, minute: int = 0) -> datetime:
    """A moment on the evening of that day, as an aware Shanghai time."""
    return datetime(2026, 11, 25, hour, minute, tzinfo=SHANGHAI_TZ)


def _backlog(rt: FlywheelRuntime, session_id: str = "late-1") -> None:
    """One conversation waiting for a batch to distill it."""
    items = rt.queue.on_explicit_enqueue(
        client="claude-code",
        session_id=session_id,
        project="demo",
        title="部署报错修复",
        content=(
            "用户：部署时报错，连接池超时，请排查。"
            "助手：先看日志，再核对连接池上限；修复后重新部署，测试验收通过。"
        ),
    )
    assert len(items) == 1, items


def _misfire_audits(rt: FlywheelRuntime) -> list[dict[str, Any]]:
    return rt.db.list_audit_events(entity_type="automation", entity_id="daily_flywheel", limit=10)


def _recording_runner(rt: FlywheelRuntime, started: list[str]) -> Callable[[str], None]:
    """A stand-in for the batch starter: it notes the id and gives the lock straight back."""

    def _start(run_id: str) -> None:
        started.append(run_id)
        rt.db.release_workflow_lock(run_id)

    return _start


def _seed_scheduled_run(rt: FlywheelRuntime, *, started_at: str, run_id: str = "run-earlier") -> None:
    """A finished scheduled batch that started at ``started_at`` (UTC, ISO 8601)."""
    rt.db.save_workflow_run(
        WorkflowRunRecord(
            run_id=run_id,
            batch_date="2026-11-25",
            batch_seq=1,
            trigger="scheduled",
            phase="daily",
            runner_mode="dry_run",
            approval_mode="auto",
            status=RunStatus.PASS,
            admitted_sessions=[],
            nodes=[],
            started_at=started_at,
            created_at=started_at,
            updated_at=started_at,
        )
    )


def _await_scheduled_run(rt: FlywheelRuntime, run_id: str | None = None, timeout: float = 60.0):
    """The scheduled batch once it has reached a terminal state."""
    live = (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL)
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        runs = [
            r
            for r in rt.db.list_workflow_runs(limit=50)
            if r.trigger == "scheduled" and (run_id is None or r.run_id == run_id)
        ]
        if runs:
            seen = [f"{r.run_id} {r.status.value}" for r in runs]
            if runs[0].status not in live:
                return runs[0]
        time.sleep(0.05)
    raise AssertionError(f"the scheduled batch never finished: {seen}")


@pytest.mark.parametrize(
    ("cron", "now", "expected"),
    [
        # Still ahead, exactly due, and given as a UTC instant (13:15 UTC is 21:15 in Shanghai).
        ("0 20 * * *", _evening(19, 59), None),
        ("0 20 * * *", _evening(20, 0), "2026-11-25T20:00:00+08:00"),
        ("0 20 * * *", datetime(2026, 11, 25, 13, 15, tzinfo=timezone.utc), "2026-11-25T20:00:00+08:00"),
        # A day that the cron does not fire on (it fires on the 26th only).
        ("0 20 26 * *", _evening(21, 15), None),
        ("0 20 25 * *", _evening(21, 15), "2026-11-25T20:00:00+08:00"),
        # Several fires a day: the latest one that has passed.
        ("0 9,20 * * *", _evening(12, 0), "2026-11-25T09:00:00+08:00"),
        ("0 9,20 * * *", _evening(21, 15), "2026-11-25T20:00:00+08:00"),
        ("*/5 * * * *", _evening(21, 17), "2026-11-25T21:15:00+08:00"),
        ("* * * * *", _evening(21, 15), "2026-11-25T21:15:00+08:00"),
        # An expression that is not a schedule.
        ("99 99 * * *", _evening(21, 15), None),
    ],
)
def test_the_fire_to_look_at_is_the_latest_one_of_today_in_shanghai_time(
    tmp_path: Path, cron: str, now: datetime, expected: str | None
):
    rt = _make_runtime(tmp_path, process_role="serve")
    rt.cfg.workflow.daily_cron = cron
    fired = rt.automation.scheduled_today(now=now)
    assert (fired.isoformat() if fired else None) == expected


def test_a_lost_fire_is_recorded_and_caught_up_once_a_day(tmp_path: Path):
    clock = _lost_fire_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    _backlog(rt)
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)

    result = rt.automation.check_missed_schedule_catchup()

    assert result["missed_today"] is True
    assert result["scheduled_today"] == "2026-11-25T20:00:00+08:00"
    assert result["catchup_window_until"] == "2026-11-26T00:00:00+08:00"
    assert result["misfire_audited"] is True
    assert result["reason"] == "catchup_started"
    assert started == [result["run_id"]]

    (audit,) = _misfire_audits(rt)
    assert audit["event_type"] == "schedule_misfire_detected"
    assert audit["actor"] == "automation"
    assert audit["reason_code"] == "SCHEDULE_MISFIRE"
    assert audit["created_at"].startswith("2026-11-25T13:15")
    assert audit["payload"] == {
        "scheduled_today": "2026-11-25T20:00:00+08:00",
        "catchup_window_until": "2026-11-26T00:00:00+08:00",
        "misfire_grace_seconds": 14400,
        "catchup_missed_schedule": True,
        "process_started_at": "2026-11-25T21:15:00+08:00",
        "process_role": "serve",
    }

    # Every later pass that day finds the miss on record and starts no second batch.
    for minutes in (1, 30, 120):
        clock.advance(minutes * 60)
        again = rt.automation.check_missed_schedule_catchup()
        assert again["reason"] == "catchup_already_started"
        assert again["misfire_audited"] is True
        assert again["catchup_started"] is False
    assert len(_misfire_audits(rt)) == 1
    assert len(started) == 1


def test_the_catch_up_batch_runs_to_its_end_as_the_scheduled_batch(tmp_path: Path):
    rt = _make_runtime(tmp_path, process_role="serve")
    rt.cfg.workflow.friday_weekly_enabled = False
    rt.cfg.workflow.approval_mode = "auto"
    rt.cfg.safety_valves.auto_approve_writes = True
    _backlog(rt)

    result = rt.automation.check_missed_schedule_catchup(now=_evening(21, 15))
    assert result["catchup_started"] is True
    run_id = result["run_id"]

    run = _await_scheduled_run(rt, run_id)
    assert run.run_id == run_id
    assert run.trigger == "scheduled"
    assert run.phase == "daily"
    assert run.status == RunStatus.PASS, run.error
    assert run.admitted_sessions == ["claude-code:late-1"]
    assert rt.db.get_workflow_lock() is None
    assert rt.db.get_session("claude-code:late-1").status is SessionStatus.PROCESSED

    # The batch is the scheduled one now: the next pass has nothing left to catch up.
    after = rt.automation.check_missed_schedule_catchup(now=_evening(21, 20))
    assert after["missed_today"] is False
    assert after["reason"] == "already_ran_today"
    assert len([r for r in rt.db.list_workflow_runs(limit=50) if r.trigger == "scheduled"]) == 1


def test_nothing_is_recorded_while_the_fire_is_still_ahead_or_a_batch_already_ran(tmp_path: Path):
    rt = _make_runtime(tmp_path, clock=_lost_fire_clock())
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    _backlog(rt)

    ahead = rt.automation.check_missed_schedule_catchup(now=_evening(19, 15))
    assert ahead["reason"] == "not_due_yet"
    assert ahead["missed_today"] is False
    assert ahead["scheduled_today"] is None

    # A batch that started after the fire time is the one the fire owed — whoever started it.
    _seed_scheduled_run(rt, started_at="2026-11-25T12:05:00+00:00")
    done = rt.automation.check_missed_schedule_catchup()
    assert done["reason"] == "already_ran_today"
    assert done["missed_today"] is False

    rt.cfg.workflow.schedule_enabled = False
    off = rt.automation.check_missed_schedule_catchup()
    assert off["reason"] == "schedule_disabled"
    assert off["missed_today"] is False

    assert started == []
    assert _misfire_audits(rt) == []


def test_an_early_batch_does_not_hide_a_later_fire_that_was_lost(tmp_path: Path):
    rt = _make_runtime(tmp_path, clock=_lost_fire_clock())
    rt.cfg.workflow.daily_cron = "0 9,20 * * *"
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    _backlog(rt)
    _seed_scheduled_run(rt, started_at="2026-11-25T01:05:00+00:00")  # the 09:00 batch

    result = rt.automation.check_missed_schedule_catchup()

    assert result["scheduled_today"] == "2026-11-25T20:00:00+08:00"
    assert result["missed_today"] is True
    assert result["reason"] == "catchup_started"
    assert len(started) == 1


def test_a_fire_outside_its_grace_window_is_reported_but_never_caught_up(tmp_path: Path):
    rt = _make_runtime(tmp_path, clock=_lost_fire_clock())
    rt.cfg.workflow.misfire_grace_seconds = 3600
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    _backlog(rt)

    late = rt.automation.check_missed_schedule_catchup()
    assert late["reason"] == "outside_grace_window"
    assert late["missed_today"] is True
    assert late["catchup_window_until"] == "2026-11-25T21:00:00+08:00"
    assert started == []
    assert _misfire_audits(rt) == []

    # The same minute one hour earlier is the last one inside the window.
    last_chance = rt.automation.check_missed_schedule_catchup(now=_evening(21, 0))
    assert last_chance["reason"] == "catchup_started"
    assert len(started) == 1


def test_a_lost_fire_with_nothing_to_do_leaves_no_trace(tmp_path: Path):
    rt = _make_runtime(tmp_path, clock=_lost_fire_clock())
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)

    idle = rt.automation.check_missed_schedule_catchup()
    assert idle["reason"] == "no_backlog"
    assert idle["missed_today"] is True
    assert idle["misfire_audited"] is False
    assert started == []
    assert _misfire_audits(rt) == []
    assert rt.db.get_workflow_lock() is None

    # A conversation that is registered but not queued yet is work too.
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:waiting-1",
            client="claude-code",
            session_id="waiting-1",
            title="部署报错修复",
            project="demo",
            content="用户：部署时报错，连接池超时，请排查。助手：已定位并修复，验收通过。",
            status=SessionStatus.PENDING,
        )
    )
    busy = rt.automation.check_missed_schedule_catchup()
    assert busy["reason"] == "catchup_started"
    assert len(started) == 1


def test_a_disabled_workflow_is_never_caught_up(tmp_path: Path):
    rt = _make_runtime(tmp_path, clock=_lost_fire_clock())
    rt.cfg.safety_valves.workflow_enabled = False
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    _backlog(rt)

    result = rt.automation.check_missed_schedule_catchup()

    assert result["reason"] == "workflow_disabled"
    assert result["missed_today"] is True
    assert result["misfire_audited"] is False
    assert started == []
    assert _misfire_audits(rt) == []


def test_a_process_that_is_not_serving_never_records_or_catches_up_a_lost_fire(tmp_path: Path):
    rt = _make_runtime(tmp_path, process_role="cli", clock=_lost_fire_clock())
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    _backlog(rt)

    result = rt.automation.check_missed_schedule_catchup()

    assert result["reason"] == "role_inactive"
    assert result["missed_today"] is True
    assert result["misfire_audited"] is False
    assert result["catchup_started"] is False
    assert started == []
    assert _misfire_audits(rt) == []
    assert rt.db.count_workflow_runs() == 0
    assert rt.db.get_workflow_lock() is None


def test_a_busy_lock_defers_the_catch_up_without_spending_its_one_attempt(tmp_path: Path):
    clock = _lost_fire_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    _backlog(rt)
    assert rt.db.acquire_workflow_lock("run-in-progress") is True

    deferred = rt.automation.check_missed_schedule_catchup()
    assert deferred["reason"] == "lock_busy"
    # The miss went on record on the first pass; the running batch was left alone.
    assert deferred["misfire_audited"] is True
    assert rt.db.get_workflow_lock()["run_id"] == "run-in-progress"
    assert started == []

    # The first pass after the lock is free starts the day's one catch-up batch.
    clock.advance(300)
    rt.db.release_workflow_lock("run-in-progress")
    taken = rt.automation.check_missed_schedule_catchup()
    assert taken["reason"] == "catchup_started"
    assert len(started) == 1
    assert len(_misfire_audits(rt)) == 1


def test_a_catch_up_that_cannot_start_gives_the_lock_back_and_is_tried_again(tmp_path: Path):
    clock = _lost_fire_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    _backlog(rt)

    def _cannot_start(run_id: str) -> None:
        raise RuntimeError("the batch could not be started")

    rt.automation._catchup_runner = _cannot_start
    with pytest.raises(RuntimeError, match="could not be started"):
        rt.automation.check_missed_schedule_catchup()
    assert rt.db.get_workflow_lock() is None

    # A failed start is not the day's attempt, and the miss is not recorded twice.
    clock.advance(60)
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    retried = rt.automation.check_missed_schedule_catchup()
    assert retried["reason"] == "catchup_started"
    assert len(started) == 1
    assert len(_misfire_audits(rt)) == 1


def test_a_restarted_process_neither_repeats_todays_audit_nor_loses_the_catch_up(tmp_path: Path):
    clock = _lost_fire_clock()
    first = _make_runtime(tmp_path, clock=clock)
    _backlog(first)
    first_started: list[str] = []
    first.automation._catchup_runner = _recording_runner(first, first_started)
    assert first.automation.check_missed_schedule_catchup()["reason"] == "catchup_started"

    # The service restarts on the same data before the batch it started is on record.
    clock.advance(300)
    second = _make_runtime(tmp_path, clock=clock)
    assert second.automation.started_at == "2026-11-25T21:20:00+08:00"
    second_started: list[str] = []
    second.automation._catchup_runner = _recording_runner(second, second_started)
    again = second.automation.check_missed_schedule_catchup()
    assert again["reason"] == "catchup_started"
    assert again["misfire_audited"] is True
    assert len(second_started) == 1
    assert len(_misfire_audits(second)) == 1

    # Once a batch after the fire time is on record, a later process leaves it at that.
    _seed_scheduled_run(second, started_at="2026-11-25T13:21:00+00:00", run_id="run-caught-up")
    clock.advance(60)
    third = _make_runtime(tmp_path, clock=clock)
    done = third.automation.check_missed_schedule_catchup()
    assert done["reason"] == "already_ran_today"
    assert done["missed_today"] is False
    assert len(_misfire_audits(third)) == 1


# ---------------------------------------------------------------- a fire from before midnight


def _after_midnight_clock() -> FakeClock:
    """Stopped at 00:30 Shanghai time on a Thursday (2026-11-26): last night's 23:00 fire never ran."""
    return FakeClock(datetime(2026, 11, 25, 16, 30, tzinfo=timezone.utc))


def _next_day(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """A moment on the day after that Wednesday, as an aware Shanghai time."""
    return datetime(2026, 11, 26, hour, minute, second, tzinfo=SHANGHAI_TZ)


LAST_NIGHTS_FIRE = "2026-11-25T23:00:00+08:00"


@pytest.mark.parametrize(
    ("cron", "grace", "now", "expected"),
    [
        # Last night's fire, half an hour into its four-hour window.
        ("0 23 * * *", 14400, _next_day(0, 30), LAST_NIGHTS_FIRE),
        # Three in the morning is its last moment; a second later it is not looked at any more.
        ("0 23 * * *", 14400, _next_day(3, 0), LAST_NIGHTS_FIRE),
        ("0 23 * * *", 14400, _next_day(3, 0, 1), None),
        # A window of one hour had closed before midnight.
        ("0 23 * * *", 3600, _next_day(0, 30), None),
        # An earlier fire of the night before, and the latest of the night's several.
        ("0 22 * * *", 14400, _next_day(1, 30), "2026-11-25T22:00:00+08:00"),
        ("0 9,23 * * *", 14400, _next_day(0, 30), LAST_NIGHTS_FIRE),
        # Once today has a fire of its own, that is the one to look at.
        ("0 0,23 * * *", 14400, _next_day(0, 30), "2026-11-26T00:00:00+08:00"),
        # A schedule that fires on other days only owes nothing.
        ("0 23 26 * *", 14400, _next_day(0, 30), None),
    ],
)
def test_a_fire_from_the_night_before_is_looked_at_while_its_window_is_open(
    tmp_path: Path, cron: str, grace: int, now: datetime, expected: str | None
):
    rt = _make_runtime(tmp_path, process_role="serve")
    rt.cfg.workflow.daily_cron = cron
    rt.cfg.workflow.misfire_grace_seconds = grace

    fired = rt.automation.scheduled_today(now=now)

    assert (fired.isoformat() if fired else None) == expected


def test_a_lost_fire_from_before_midnight_is_recorded_and_caught_up_after_it(tmp_path: Path):
    clock = _after_midnight_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    rt.cfg.workflow.daily_cron = "0 23 * * *"
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    _backlog(rt)

    # The console says it before the check does: last night's batch is outstanding.
    scheduler = rt.automation.get_snapshot()["scheduler"]
    assert scheduler["scheduled_today"] == LAST_NIGHTS_FIRE
    assert scheduler["catchup_window_until"] == "2026-11-26T03:00:00+08:00"
    assert scheduler["missed_today"] is True

    result = rt.automation.check_missed_schedule_catchup()

    assert result["missed_today"] is True
    assert result["scheduled_today"] == LAST_NIGHTS_FIRE
    assert result["catchup_window_until"] == "2026-11-26T03:00:00+08:00"
    assert result["misfire_audited"] is True
    assert result["reason"] == "catchup_started"
    assert started == [result["run_id"]]
    (audit,) = _misfire_audits(rt)
    assert audit["event_type"] == "schedule_misfire_detected"
    assert audit["payload"]["scheduled_today"] == LAST_NIGHTS_FIRE
    assert audit["payload"]["catchup_window_until"] == "2026-11-26T03:00:00+08:00"
    assert "2026-11-25 23:00" in audit["summary"]

    # Every later pass that night finds the catch-up on record and starts no second batch,
    # up to and including the last minute of the window.
    for minutes in (5, 30, 114, 1):
        clock.advance(minutes * 60)
        again = rt.automation.check_missed_schedule_catchup()
        assert again["reason"] == "catchup_already_started"
        assert again["catchup_started"] is False
    assert clock.now() == _next_day(3, 0)
    assert len(started) == 1
    assert len(_misfire_audits(rt)) == 1

    # A second past three the fire can no longer be caught up: it is not looked at, and
    # the next time the claims are read its claim is gone.
    clock.advance(1)
    closed = rt.automation.check_missed_schedule_catchup()
    assert closed["reason"] == "not_due_yet"
    assert closed["missed_today"] is False
    assert rt.automation._live_fire_claims(clock.now()) == {}
    assert _fire_claims(rt) == {}


def test_catching_up_last_nights_fire_does_not_use_up_tonights(tmp_path: Path):
    clock = _after_midnight_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    rt.cfg.workflow.daily_cron = "0 23 * * *"
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    _backlog(rt)
    assert rt.automation.check_missed_schedule_catchup()["reason"] == "catchup_started"

    # The next evening the same thing happens again: a second fire is lost.
    clock.advance(int((_next_day(23, 15) - _next_day(0, 30)).total_seconds()))
    tonight = rt.automation.check_missed_schedule_catchup()

    # It is another fire, with a miss and a batch of its own — not "already started".
    assert tonight["scheduled_today"] == "2026-11-26T23:00:00+08:00"
    assert tonight["missed_today"] is True
    assert tonight["misfire_audited"] is True
    assert tonight["reason"] == "catchup_started"
    assert len(started) == 2
    assert sorted(a["payload"]["scheduled_today"] for a in _misfire_audits(rt)) == [
        LAST_NIGHTS_FIRE,
        "2026-11-26T23:00:00+08:00",
    ]


def test_the_serve_recovery_loop_catches_up_last_nights_fire_on_its_first_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLYWHEEL_RECOVERY_INTERVAL", "0.2")
    rt = _make_runtime(tmp_path, process_role="serve")
    rt.cfg.workflow.daily_cron = "0 23 * * *"
    rt.cfg.workflow.friday_weekly_enabled = False
    rt.cfg.workflow.approval_mode = "auto"
    rt.cfg.safety_valves.auto_approve_writes = True
    _backlog(rt)
    # The service came up half an hour after midnight; last night's 23:00 batch never ran.
    monkeypatch.setattr(rt.automation, "_clock", lambda: datetime(2026, 11, 25, 16, 30, tzinfo=timezone.utc))

    app = create_app(runtime=rt, process_role="serve")
    with TestClient(app) as client:
        run = _await_scheduled_run(rt)
        assert run.status == RunStatus.PASS, run.error
        assert run.phase == "daily"
        (audit,) = _misfire_audits(rt)
        assert audit["payload"]["scheduled_today"] == LAST_NIGHTS_FIRE
        scheduler = client.get("/api/site/automations").json()["scheduler"]
        assert scheduler["catchup_run_id"] == run.run_id
        assert scheduler["missed_today"] is False
        assert len([r for r in rt.db.list_workflow_runs(limit=50) if r.trigger == "scheduled"]) == 1


# ---------------------------------------------------------------- the claim on a fire, shared by the cron job and the catch-up


def _fire_claims(rt: FlywheelRuntime) -> dict[str, Any]:
    """The claims on scheduled fires as they are stored in ``system_settings``.

    Read from the row itself, not through the manager's copy of it: this is what a
    restarted process finds. A build that keeps no claim store has no row, so nothing
    is on record.
    """
    key = getattr(automation_mod, "FIRE_CLAIMS_KEY", "")
    rows = [row for row in rt.db.list_system_settings() if row["key"] == key]
    assert len(rows) <= 1, rows
    return rows[0]["value"] if rows else {}


def _claim_of(rt: FlywheelRuntime, fire: str) -> dict[str, Any]:
    """The claim on record for ``fire``, and that it is the only one."""
    claims = _fire_claims(rt)
    assert list(claims) == [fire], claims
    return claims[fire]


@pytest.mark.parametrize(
    ("clock", "cron", "fire"),
    [
        (_lost_fire_clock, "0 20 * * *", "2026-11-25T20:00:00+08:00"),
        (_after_midnight_clock, "0 23 * * *", LAST_NIGHTS_FIRE),
    ],
)
def test_a_fire_the_cron_job_took_is_not_caught_up_again(
    tmp_path: Path, clock: FakeClock, cron: str, fire: str
):
    rt = _make_runtime(tmp_path, clock=clock())
    rt.cfg.workflow.daily_cron = cron
    rt.cfg.workflow.friday_weekly_enabled = False
    rt.cfg.workflow.approval_mode = "auto"
    rt.cfg.safety_valves.auto_approve_writes = True
    catchups: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, catchups)
    _backlog(rt)

    # The scheduler wakes for the fire — here well after it, as the recovery loop is about
    # to look for it — and the batch runs to its end under the claim.
    rt.automation._run_scheduled_daily()

    (run,) = [r for r in rt.db.list_workflow_runs(limit=5) if r.trigger == "scheduled"]
    assert run.status is RunStatus.PASS, run.error
    claim = _claim_of(rt, fire)
    assert claim["source"] == "cron"
    assert claim["run_id"] == run.run_id

    again = rt.automation.check_missed_schedule_catchup()

    assert again["reason"] == "already_ran_today"
    assert again["missed_today"] is False
    assert again["catchup_started"] is False
    assert catchups == []
    # It was never a miss: the fire got its batch.
    assert _misfire_audits(rt) == []


def test_the_catch_up_leaves_a_fire_to_the_cron_batch_starting_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The moment a claim is for: the lock is taken, the batch is not on record yet."""
    clock = _lost_fire_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    _backlog(rt)
    catchups: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, catchups)
    fire = "2026-11-25T20:00:00+08:00"
    cron_batches: list[str] = []

    def _batch_starting(**kwargs: Any) -> None:
        cron_batches.append(kwargs["run_id"])
        assert rt.db.get_workflow_lock()["run_id"] == kwargs["run_id"]

    monkeypatch.setattr(rt.orchestrator, "start_run", _batch_starting)
    rt.automation._run_scheduled_daily()
    assert len(cron_batches) == 1

    again = rt.automation.check_missed_schedule_catchup()

    # No run record says the fire is taken — only the claim the cron job made does.
    assert rt.db.count_workflow_runs() == 0
    assert again["reason"] == "already_ran_today"
    assert again["missed_today"] is False
    assert catchups == []
    assert _misfire_audits(rt) == []
    claim = _claim_of(rt, fire)
    assert claim["source"] == "cron"
    assert claim["run_id"] == cron_batches[0]
    assert rt.automation.get_snapshot()["scheduler"]["missed_today"] is False


def test_the_cron_job_leaves_a_fire_the_catch_up_already_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _lost_fire_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    _backlog(rt)
    catchups: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, catchups)
    assert rt.automation.check_missed_schedule_catchup()["reason"] == "catchup_started"
    cron_batches: list[dict[str, Any]] = []
    monkeypatch.setattr(rt.orchestrator, "start_run", lambda **kwargs: cron_batches.append(kwargs))

    # The scheduler wakes for the same fire a moment later.
    rt.automation._run_scheduled_daily()

    assert cron_batches == []
    assert catchups and len(catchups) == 1
    claim = _claim_of(rt, "2026-11-25T20:00:00+08:00")
    assert claim["source"] == "catchup"
    assert claim["run_id"] == catchups[0]
    assert len(_misfire_audits(rt)) == 1


def test_a_busy_lock_costs_the_cron_job_no_claim_and_the_catch_up_still_gets_the_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _lost_fire_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    _backlog(rt)
    cron_batches: list[dict[str, Any]] = []
    monkeypatch.setattr(rt.orchestrator, "start_run", lambda **kwargs: cron_batches.append(kwargs))
    assert rt.db.acquire_workflow_lock("run-in-progress") is True

    # Another batch holds the lock when the scheduler wakes: a warning, not a failure.
    rt.automation._run_scheduled_daily()

    assert cron_batches == []
    assert _fire_claims(rt) == {}
    assert rt.db.get_workflow_lock()["run_id"] == "run-in-progress"

    # The fire is still the catch-up's to take once the lock is free, inside the window.
    clock.advance(300)
    rt.db.release_workflow_lock("run-in-progress")
    catchups: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, catchups)
    taken = rt.automation.check_missed_schedule_catchup()

    assert taken["reason"] == "catchup_started"
    assert catchups == [taken["run_id"]]
    claim = _claim_of(rt, "2026-11-25T20:00:00+08:00")
    assert claim["source"] == "catchup"
    assert claim["run_id"] == taken["run_id"]


def test_a_cron_batch_that_cannot_start_gives_back_its_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _lost_fire_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    _backlog(rt)

    def _cannot_start(**kwargs: Any) -> None:
        raise RuntimeError("the batch could not be started")

    monkeypatch.setattr(rt.orchestrator, "start_run", _cannot_start)
    with pytest.raises(RuntimeError, match="could not be started"):
        rt.automation._start_cron_batch()
    assert rt.db.get_workflow_lock() is None
    assert _fire_claims(rt) == {}

    clock.advance(60)
    catchups: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, catchups)
    assert rt.automation.check_missed_schedule_catchup()["reason"] == "catchup_started"
    assert len(catchups) == 1


def test_a_catch_up_batch_that_never_takes_ownership_does_not_keep_its_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = _lost_fire_clock()
    rt = _make_runtime(tmp_path, clock=clock)
    _backlog(rt)

    def _cannot_start(**kwargs: Any) -> None:
        raise RuntimeError("the batch could not be started")

    monkeypatch.setattr(rt.orchestrator, "start_run", _cannot_start)
    failed = rt.automation.check_missed_schedule_catchup()
    assert failed["reason"] == "catchup_started"

    # The batch thread found it could not start, and undid what was done for it.
    assert _wait_until(lambda: rt.db.get_workflow_lock() is None), "the lock of the failed batch stayed"
    assert _fire_claims(rt) == {}
    assert rt.automation.get_snapshot()["scheduler"]["catchup_run_id"] is None

    # So the next pass starts the catch-up again, and does not record the miss twice.
    clock.advance(300)
    catchups: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, catchups)
    retried = rt.automation.check_missed_schedule_catchup()
    assert retried["reason"] == "catchup_started"
    assert catchups == [retried["run_id"]]
    assert retried["run_id"] != failed["run_id"]
    assert len(_misfire_audits(rt)) == 1
    assert _claim_of(rt, "2026-11-25T20:00:00+08:00")["run_id"] == retried["run_id"]


def test_a_claim_counts_after_a_restart_only_through_the_batch_it_started(tmp_path: Path):
    clock = _lost_fire_clock()
    fire = "2026-11-25T20:00:00+08:00"
    first = _make_runtime(tmp_path, clock=clock)
    _backlog(first)
    first_started: list[str] = []
    first.automation._catchup_runner = _recording_runner(first, first_started)
    assert first.automation.check_missed_schedule_catchup()["reason"] == "catchup_started"
    assert _claim_of(first, fire)["run_id"] == first_started[0]

    # The service restarts on the same data: the claim is still stored, but its batch
    # never got on record, so the fire is caught up again rather than written off.
    clock.advance(300)
    second = _make_runtime(tmp_path, clock=clock)
    second_started: list[str] = []
    second.automation._catchup_runner = _recording_runner(second, second_started)
    again = second.automation.check_missed_schedule_catchup()
    assert again["reason"] == "catchup_started"
    assert second_started != first_started
    assert _claim_of(second, fire)["run_id"] == second_started[0]

    # One whose batch did get on record before a restart is kept, and answered by that record.
    _seed_scheduled_run(second, started_at="2026-11-25T13:21:00+00:00", run_id=second_started[0])
    clock.advance(60)
    third = _make_runtime(tmp_path, clock=clock)
    done = third.automation.check_missed_schedule_catchup()
    assert done["reason"] == "already_ran_today"
    assert done["missed_today"] is False
    assert _claim_of(third, fire)["run_id"] == second_started[0]


def test_a_lost_fire_is_only_recorded_when_catch_up_is_off(tmp_path: Path):
    rt = _make_runtime(tmp_path, clock=_lost_fire_clock())
    rt.cfg.workflow.catchup_missed_schedule = False
    started: list[str] = []
    rt.automation._catchup_runner = _recording_runner(rt, started)
    _backlog(rt)

    result = rt.automation.check_missed_schedule_catchup()
    assert result["missed_today"] is True
    assert result["misfire_audited"] is True
    assert result["reason"] == "catchup_disabled"
    assert result["catchup_started"] is False
    assert started == []
    assert rt.db.count_workflow_runs() == 0
    assert [a["event_type"] for a in _misfire_audits(rt)] == ["schedule_misfire_detected"]

    with TestClient(create_app(runtime=rt, process_role="serve")) as client:
        scheduler = client.get("/api/site/automations").json()["scheduler"]
        assert scheduler["missed_today"] is True
        assert scheduler["catchup_missed_schedule"] is False
        assert scheduler["catchup_run_id"] is None


def test_health_and_the_automation_snapshot_say_when_the_process_started(tmp_path: Path):
    rt = _make_runtime(tmp_path, clock=_lost_fire_clock())
    with TestClient(create_app(runtime=rt, process_role="serve")) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["started_at"] == "2026-11-25T21:15:00+08:00"

        scheduler = client.get("/api/site/automations").json()["scheduler"]
        assert scheduler["started_at"] == health["started_at"]
        assert scheduler["misfire_grace_seconds"] == 14400
        assert scheduler["catchup_missed_schedule"] is True
        assert scheduler["scheduled_today"] == "2026-11-25T20:00:00+08:00"
        assert scheduler["catchup_window_until"] == "2026-11-26T00:00:00+08:00"
        # 21:15 and nothing scheduled has run: today's batch is outstanding.
        assert scheduler["missed_today"] is True
        assert scheduler["catchup_run_id"] is None

        _seed_scheduled_run(rt, started_at="2026-11-25T12:05:00+00:00")
        after = client.get("/api/site/automations").json()
        assert after["scheduler"]["missed_today"] is False
        assert after["metrics"]["today_scheduled_ran"] is True


def test_started_at_is_this_process_own_shanghai_timestamp(tmp_path: Path):
    just_before = datetime.now(timezone.utc)
    rt = _make_runtime(tmp_path, process_role="serve")
    started = datetime.fromisoformat(rt.automation.started_at)
    assert started.utcoffset() == timedelta(hours=8)
    assert just_before <= started <= datetime.now(timezone.utc)


def test_the_serve_recovery_loop_catches_up_a_lost_fire_on_its_first_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLYWHEEL_RECOVERY_INTERVAL", "0.2")
    rt = _make_runtime(tmp_path, process_role="serve")
    rt.cfg.workflow.friday_weekly_enabled = False
    rt.cfg.workflow.approval_mode = "auto"
    rt.cfg.safety_valves.auto_approve_writes = True
    _backlog(rt)
    # The service came up after the cron time of a day whose 20:00 batch never ran.
    monkeypatch.setattr(rt.automation, "_clock", lambda: _evening(21, 15))

    app = create_app(runtime=rt, process_role="serve")
    with TestClient(app) as client:
        run = _await_scheduled_run(rt)
        assert run.status == RunStatus.PASS, run.error
        assert run.phase == "daily"
        (audit,) = _misfire_audits(rt)
        assert audit["event_type"] == "schedule_misfire_detected"
        assert audit["payload"]["process_role"] == "serve"

        # More recovery passes go by: no second batch, and the miss is not recorded twice.
        time.sleep(0.7)
        assert len([r for r in rt.db.list_workflow_runs(limit=50) if r.trigger == "scheduled"]) == 1
        assert len(_misfire_audits(rt)) == 1
        scheduler = client.get("/api/site/automations").json()["scheduler"]
        assert scheduler["catchup_run_id"] == run.run_id
        assert scheduler["missed_today"] is False


def test_the_recovery_loop_looks_for_a_lost_fire_on_its_first_pass_and_then_rarely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLYWHEEL_RECOVERY_INTERVAL", "0.05")
    rt = _make_runtime(tmp_path, process_role="serve")
    checks: list[int] = []
    real_check = rt.automation.check_missed_schedule_catchup

    def _counting_check(now: datetime | None = None) -> dict[str, Any]:
        checks.append(1)
        return real_check(now)

    monkeypatch.setattr(rt.automation, "check_missed_schedule_catchup", _counting_check)
    monkeypatch.setattr("ai_flywheel.app.CATCHUP_CHECK_INTERVAL_SECONDS", 3600.0)

    with TestClient(create_app(runtime=rt, process_role="serve")):
        assert _wait_until(lambda: bool(checks)), "the recovery loop never looked for a lost fire"
        # Several more recovery passes go by before the check's own interval is over.
        time.sleep(0.4)
        assert len(checks) == 1

        monkeypatch.setattr("ai_flywheel.app.CATCHUP_CHECK_INTERVAL_SECONDS", 0.0)
        assert _wait_until(lambda: len(checks) >= 3)


def test_a_process_that_is_not_serving_runs_neither_the_recovery_loop_nor_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FLYWHEEL_RECOVERY_INTERVAL", "0.05")
    rt = _make_runtime(tmp_path, process_role="cli")
    _backlog(rt)
    checks: list[int] = []
    monkeypatch.setattr(
        rt.automation, "check_missed_schedule_catchup", lambda *args, **kwargs: checks.append(1)
    )

    with TestClient(create_app(runtime=rt, process_role="cli")) as client:
        time.sleep(0.3)
        assert checks == []
        assert client.get("/api/site/automations").json()["daemons"]["recovery"]["last_pass_at"] is None
        assert rt.db.count_workflow_runs() == 0
        assert _misfire_audits(rt) == []
