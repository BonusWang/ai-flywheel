"""fix-approval-claim-and-recovery：编排器与接口层的回归测试。

覆盖 tasks 1.1/1.2/1.3/1.5 的流程面：批准与取消走认领原语、锁忙 409、
心跳随挂起停止、执行期间失去锁按 fail-run 收尾、无执行者运行的收尾、
审计的来源渠道与连续持有标记。
"""
from __future__ import annotations

import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_flywheel.app import create_app
from ai_flywheel.config import load_config
from ai_flywheel.core.orchestrator import ApprovalLockBusy, LockLostError, WorkflowLockError
from ai_flywheel.models import RegisterSessionRequest, RunStatus, SessionStatus
from ai_flywheel.runtime import FlywheelRuntime
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def rt(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = load_config(base_dir=tmp_path)
    return FlywheelRuntime(cfg)


def _seed_session(rt: FlywheelRuntime, key: str = "claude:flow-01") -> None:
    rt.register_session(
        RegisterSessionRequest(
            client=key.split(":")[0],
            session_id=key.split(":", 1)[1],
            title="修复构建缓存失效导致部署报错",
            project="demo",
            summary="定位缓存报错并总结排查步骤",
            content="User: 构建报错修复。Assistant: 清理构建缓存并验证通过。",
        )
    )


def _suspend(rt: FlywheelRuntime, key: str = "claude:flow-01"):
    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.planned_writes
    return run


# ---------------- 批准与取消（tasks 1.1） ----------------

def test_approve_flow_via_claim_primitive(rt: FlywheelRuntime):
    _seed_session(rt)
    run = _suspend(rt)
    done = rt.orchestrator.approve_and_resume(run.run_id)
    assert done.status == RunStatus.PASS
    assert rt.db.get_workflow_lock() is None
    assert rt.db.get_session("claude:flow-01").status == SessionStatus.PROCESSED
    events = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id)
    approved = [e for e in events if e["event_type"] == "approved"]
    assert len(approved) == 1
    assert approved[0]["payload"]["source"] == "portal"
    # 挂起期间自持未过期锁 → 认领为连续持有
    assert approved[0]["payload"]["reacquired"] is True
    with rt.db.connect() as conn:
        row = conn.execute("SELECT state FROM decision_cards WHERE run_id = ?", (run.run_id,)).fetchone()
    assert row["state"] == "done"


def test_concurrent_approve_only_one_wins(rt: FlywheelRuntime):
    _seed_session(rt)
    run = _suspend(rt)
    barrier = threading.Barrier(3, timeout=30)
    results: list = []
    errors: list = []

    def _approve():
        barrier.wait()
        try:
            results.append(rt.orchestrator.approve_and_resume(run.run_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_approve)
    t2 = threading.Thread(target=_approve)
    t1.start(); t2.start()
    barrier.wait()
    t1.join(timeout=120); t2.join(timeout=120)
    # 只有一方拿到 PASS，另一方被拒（ValueError：不再等待审批）
    assert len([r for r in results if r.status == RunStatus.PASS]) == 1
    assert len(errors) == 1
    events = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id)
    assert len([e for e in events if e["event_type"] == "approved"]) == 1
    assert rt.db.get_workflow_lock() is None


def test_approve_lock_busy_rejected_and_retryable(rt: FlywheelRuntime):
    _seed_session(rt)
    run = _suspend(rt)
    # 把锁腾出来再让给别的运行，模拟挂起期间锁被接管
    assert rt.db.release_workflow_lock(run.run_id) is True
    assert rt.db.acquire_workflow_lock("run-other") is True
    with pytest.raises(ApprovalLockBusy) as exc_info:
        rt.orchestrator.approve_and_resume(run.run_id)
    assert exc_info.value.lock_run_id == "run-other"
    assert exc_info.value.lock_expires_at
    # 运行与卡片保持原状，可重试
    assert rt.db.get_workflow_run(run.run_id).status == RunStatus.AWAITING_APPROVAL
    with rt.db.connect() as conn:
        row = conn.execute("SELECT state FROM decision_cards WHERE run_id = ?", (run.run_id,)).fetchone()
    assert row["state"] == "open"
    assert rt.db.release_workflow_lock("run-other") is True
    done = rt.orchestrator.approve_and_resume(run.run_id)
    assert done.status == RunStatus.PASS
    events = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id)
    assert [e for e in events if e["event_type"] == "approved"][0]["payload"]["reacquired"] is False


def test_reject_flow_and_other_lock_untouched(rt: FlywheelRuntime):
    _seed_session(rt)
    run = _suspend(rt)
    assert rt.db.release_workflow_lock(run.run_id) is True
    assert rt.db.acquire_workflow_lock("run-other") is True
    rejected = rt.orchestrator.reject_run(run.run_id, reason="测试取消", source="feishu")
    assert rejected.status == RunStatus.CANCELLED
    assert rejected.lock_released is True
    assert rt.db.get_session("claude:flow-01").status == SessionStatus.PENDING
    # 别人的锁保持不动
    assert rt.db.get_workflow_lock()["run_id"] == "run-other"
    events = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id)
    assert [e for e in events if e["event_type"] == "rejected"][0]["payload"]["source"] == "feishu"
    with rt.db.connect() as conn:
        row = conn.execute("SELECT state FROM decision_cards WHERE run_id = ?", (run.run_id,)).fetchone()
    assert row["state"] == "closed"
    rt.db.release_workflow_lock("run-other")


def test_approve_and_reject_race_only_one_wins(rt: FlywheelRuntime):
    _seed_session(rt)
    run = _suspend(rt)
    barrier = threading.Barrier(3, timeout=30)
    outcomes: dict[str, RunStatus | None] = {"approve": None, "reject": None}

    def _approve():
        barrier.wait()
        try:
            outcomes["approve"] = rt.orchestrator.approve_and_resume(run.run_id).status
        except Exception:
            outcomes["approve"] = None

    def _reject():
        barrier.wait()
        try:
            outcomes["reject"] = rt.orchestrator.reject_run(run.run_id, reason="race").status
        except Exception:
            outcomes["reject"] = None

    t1 = threading.Thread(target=_approve)
    t2 = threading.Thread(target=_reject)
    t1.start(); t2.start()
    barrier.wait()
    t1.join(timeout=120); t2.join(timeout=120)
    won = [v for v in outcomes.values() if v is not None]
    assert len(won) == 1  # 恰有一方生效
    events = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id)
    kinds = {e["event_type"] for e in events}
    assert not ({"approved", "rejected"} <= kinds)
    assert rt.db.get_workflow_lock() is None


# ---------------- 心跳与检查点（tasks 1.2） ----------------

def test_heartbeat_stops_on_suspend_and_completion(rt: FlywheelRuntime):
    _seed_session(rt)
    run = _suspend(rt)
    assert rt.orchestrator.heartbeat._thread is None  # 挂起返回前已停止
    done = rt.orchestrator.approve_and_resume(run.run_id)
    assert done.status == RunStatus.PASS
    assert rt.orchestrator.heartbeat._thread is None
    assert rt.db.get_workflow_lock() is None


def test_lock_lost_during_execution_fails_run(rt: FlywheelRuntime, monkeypatch):
    _seed_session(rt)
    run = _suspend(rt)
    # 让检查点判定锁已丢失（心跳线程持有原始绑定，不受影响）
    monkeypatch.setattr(rt.db, "renew_workflow_lock", lambda *a, **k: False)
    failed = rt.orchestrator.approve_and_resume(run.run_id)
    assert failed.status == RunStatus.FAILED
    assert "失去独占锁" in (failed.error or "")
    events = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id)
    assert [e for e in events if e["event_type"] == "run_failed"]
    with rt.db.connect() as conn:
        row = conn.execute("SELECT state FROM decision_cards WHERE run_id = ?", (run.run_id,)).fetchone()
    assert row["state"] == "failed"


def test_start_run_lock_busy_still_raises_and_creates_nothing(rt: FlywheelRuntime):
    _seed_session(rt)
    run = _suspend(rt)  # 持锁挂起
    before_ids = {r.run_id for r in rt.db.list_workflow_runs(limit=200)}
    with pytest.raises(WorkflowLockError):
        rt.orchestrator.start_run(trigger="test", phase="daily")
    after_ids = {r.run_id for r in rt.db.list_workflow_runs(limit=200)}
    assert after_ids == before_ids
    rt.orchestrator.approve_and_resume(run.run_id)


# ---------------- 收尾没有执行者的运行（tasks 1.3） ----------------

def _make_stale_running(rt: FlywheelRuntime, *, heartbeat_minutes_ago: int, key: str = "claude:stale-01"):
    rt.register_session(
        RegisterSessionRequest(
            client="claude",
            session_id=key.split(":", 1)[1],
            title="会被中断的运行",
            summary="x",
            content="y",
        )
    )
    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    rt.db.claim_approval(run.run_id, approved_mask=[True] * len(run.planned_writes), selected_option="同意并写入", source="portal")
    now = datetime.now(timezone.utc)
    stale_at = (now - timedelta(minutes=heartbeat_minutes_ago)).isoformat()
    rt.db.renew_workflow_lock(run.run_id, now=stale_at)
    return run


def test_reaper_collects_interrupted_run(rt: FlywheelRuntime):
    run = _make_stale_running(rt, heartbeat_minutes_ago=10)
    recovered = rt.orchestrator.recover_interrupted_runs()
    assert run.run_id in recovered
    stored = rt.db.get_workflow_run(run.run_id)
    assert stored.status == RunStatus.FAILED
    statuses = {n.node_id: n.status.value for n in stored.nodes}
    assert statuses["P04_WRITE_VERIFY"] == "FAILED"
    assert statuses["P05_SETTLE"] == "SKIPPED"
    assert rt.db.get_session("claude:stale-01").status == SessionStatus.PENDING
    with rt.db.connect() as conn:
        row = conn.execute("SELECT state, result_summary FROM decision_cards WHERE run_id = ?", (run.run_id,)).fetchone()
    assert row["state"] == "failed" and "中断" in row["result_summary"]
    assert rt.db.get_workflow_lock() is None
    # 幂等：再收尾一次是空操作
    assert rt.orchestrator.recover_interrupted_runs() == []
    events = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id)
    assert len([e for e in events if e["event_type"] == "run_failed"]) == 1


def test_reaper_leaves_executing_runs_alone(rt: FlywheelRuntime):
    _seed_session(rt, "claude:fresh-01")
    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    rt.db.claim_approval(run.run_id, approved_mask=[True] * len(run.planned_writes), selected_option="同意并写入", source="portal")
    rt.db.renew_workflow_lock(run.run_id)  # 新鲜心跳
    assert rt.orchestrator.recover_interrupted_runs() == []
    assert rt.db.get_workflow_run(run.run_id).status == RunStatus.RUNNING


def test_zombie_executor_cannot_resurrect_reaped_run(rt: FlywheelRuntime):
    from ai_flywheel.storage.db import RunAlreadyFinalizedError

    run = _make_stale_running(rt, heartbeat_minutes_ago=10)
    rt.orchestrator.recover_interrupted_runs()
    zombie = rt.db.get_workflow_run(run.run_id)  # 僵尸执行者读到 FAILED 前的旧副本路径
    zombie.status = RunStatus.RUNNING
    with pytest.raises(RunAlreadyFinalizedError):
        rt.db.save_workflow_run(zombie)
    assert rt.db.get_workflow_run(run.run_id).status == RunStatus.FAILED


# ---------------- REST 契约（tasks 1.5） ----------------

def test_rest_approve_returns_409_with_lock_expiry(rt: FlywheelRuntime):
    _seed_session(rt)
    run = _suspend(rt)
    rt.db.release_workflow_lock(run.run_id)
    rt.db.acquire_workflow_lock("run-other")
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        resp = client.post(f"/api/site/runs/{run.run_id}/approve", json={"approved_indices": None})
        assert resp.status_code == 409
        assert "预计" in resp.json()["detail"]
        # 取消不受锁影响
        rej = client.post(f"/api/site/runs/{run.run_id}/reject", json={"reason": "rest"})
        assert rej.status_code == 200
        assert rej.json()["run"]["status"] == "CANCELLED"
    rt.db.release_workflow_lock("run-other")


def test_heartbeat_survives_renewal_exception():
    import time as _time

    from ai_flywheel.core.heartbeat import HeartbeatManager

    calls = {"n": 0}

    def flaky(run_id: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return True

    hm = HeartbeatManager(flaky, interval_seconds=0.05)
    hm.start("run-x")
    _time.sleep(0.4)
    alive = hm._thread is not None and hm._thread.is_alive()
    hm.stop()
    assert alive
    assert calls["n"] >= 2


def test_serve_recovery_loop_reaps_on_first_pass(rt: FlywheelRuntime, monkeypatch):
    """process_role=serve 的 lifespan 必须真的启动恢复线程（曾因缺 import 静默死亡）。"""
    import time as _time

    monkeypatch.setenv("FLYWHEEL_RECOVERY_INTERVAL", "1")
    monkeypatch.setenv("FLYWHEEL_RECOVERY_STALE_SECONDS", "2")
    run = _make_stale_running(rt, heartbeat_minutes_ago=10, key="claude:loop-01")
    app = create_app(runtime=rt, process_role="serve")
    with TestClient(app) as client:
        deadline = _time.time() + 15
        reaped = False
        while _time.time() < deadline:
            body = client.get(f"/api/site/runs/{run.run_id}").json()
            if body["run"]["status"] == "FAILED":
                reaped = True
                break
            _time.sleep(0.5)
        assert reaped
        assert rt.db.get_workflow_lock() is None
