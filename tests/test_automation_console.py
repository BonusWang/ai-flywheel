"""Tests for the Automation Console control plane & hot-reloadable scheduler."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.automation import describe_cron, next_fire_time_iso, validate_cron
from ai_flywheel.models import NodeExecution, NodeStatus, RunStatus, WorkflowRunRecord
from ai_flywheel.runtime import FlywheelRuntime


def _make_runtime(tmp_path: Path, *, process_role: str = "serve", schedule_enabled: bool = True) -> FlywheelRuntime:
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.server.data_dir = str(tmp_path / "data")
    cfg.sessions.scan_sources = []
    cfg.workflow.schedule_enabled = schedule_enabled
    cfg.workflow.daily_cron = "0 20 * * *"
    cfg.workflow.friday_weekly_enabled = True
    cfg.runner.mode = "dry_run"
    return FlywheelRuntime(cfg, skip_fts_sync=True, process_role=process_role)


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
