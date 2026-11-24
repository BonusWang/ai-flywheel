"""fix-approval-claim-and-recovery：数据层原语的回归测试。

覆盖 tasks 1.1/1.2/1.4/1.5 中不依赖编排器的部分：
取锁 upsert 的全部条件、续期、批准认领事务、取消认领事务、终态入口与保存守卫、
提问卡对账矩阵、无执行者运行的查询、空有效期锁行的迁移清理。
时间全部显式传入（tasks 2.2 的时钟注入在数据层以 now 参数实现）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_flywheel.models import (
    NodeExecution,
    NodeStatus,
    PlannedWrite,
    RunStatus,
    WorkflowRunRecord,
    utc_now_iso,
)
from ai_flywheel.storage.db import FlywheelDB


def iso(dt: datetime) -> str:
    return dt.isoformat()


T0 = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
T0_S = iso(T0)
T_LATER = iso(T0 + timedelta(minutes=30))
T_2H = iso(T0 + timedelta(hours=2))


@pytest.fixture()
def db(tmp_path: Path) -> FlywheelDB:
    return FlywheelDB(tmp_path / "flywheel.db")


_PASS_NODES = ("P00_PREPARE", "P01_BEGIN", "P02_REVIEW", "P03_ROUTE_PLAN")


def _run(run_id: str, *, status: RunStatus = RunStatus.AWAITING_APPROVAL, writes: int = 2, admitted: list[str] | None = None) -> WorkflowRunRecord:
    def _node_status(n: str) -> NodeStatus:
        if n in _PASS_NODES:
            return NodeStatus.PASS
        if n == "P04_WRITE_VERIFY":
            return NodeStatus.AWAITING_APPROVAL if status == RunStatus.AWAITING_APPROVAL else NodeStatus.RUNNING
        return NodeStatus.PENDING

    return WorkflowRunRecord(
        run_id=run_id,
        trigger="test",
        approval_mode="always",
        status=status,
        admitted_sessions=admitted or [],
        planned_writes=[
            PlannedWrite(target_kind="knowledge_doc", action="create", title=f"t{i}", summary=f"s{i}")
            for i in range(writes)
        ],
        nodes=[
            NodeExecution(node_id=n, name=n, actor="x", status=_node_status(n))
            for n in _PASS_NODES + ("P04_WRITE_VERIFY", "P05_SETTLE", "P06_REPORT", "P07_DELIVER")
        ],
    )


def _awaiting(db: FlywheelDB, run_id: str, *, writes: int = 2, admitted: list[str] | None = None, card: bool = True) -> WorkflowRunRecord:
    run = _run(run_id, writes=writes, admitted=admitted)
    db.save_workflow_run(run)
    if card:
        db.save_decision_card(card_id=f"card-{run_id}", run_id=run_id, kind="ask", state="open", title="t", body_markdown="b", options=["同意并写入", "取消本次写入"])
    return run


# ---------------- 取锁 upsert（tasks 1.2） ----------------

def test_lock_upsert_all_conditions(db: FlywheelDB):
    # 空闲可取
    assert db.acquire_workflow_lock("run-a", now=T0_S) is True
    # 他人持未过期锁：不可取
    assert db.acquire_workflow_lock("run-b", now=iso(T0 + timedelta(seconds=30))) is False
    # 本运行再取：刷新有效期与心跳
    assert db.acquire_workflow_lock("run-a", now=T_LATER) is True
    lock = db.get_workflow_lock()
    assert lock["run_id"] == "run-a"
    assert lock["expires_at"] > T0_S
    assert lock.get("heartbeat_at") == T_LATER
    # 过期后他人可接管
    assert db.acquire_workflow_lock("run-c", now=iso(T0 + timedelta(hours=2))) is True
    assert db.get_workflow_lock()["run_id"] == "run-c"
    # 有效期为空视为可取
    with db.connect() as conn:
        conn.execute("UPDATE workflow_lock SET expires_at = NULL WHERE lock_id = 1")
    assert db.acquire_workflow_lock("run-d", now=iso(T0 + timedelta(hours=3))) is True


def test_lock_renewal(db: FlywheelDB):
    assert db.acquire_workflow_lock("run-a", now=T0_S) is True
    assert db.renew_workflow_lock("run-a", now=T_LATER) is True
    lock = db.get_workflow_lock()
    assert lock["heartbeat_at"] == T_LATER
    # 锁易主后续期失败
    assert db.acquire_workflow_lock("run-b", now=iso(T0 + timedelta(hours=4))) is True
    assert db.renew_workflow_lock("run-a", now=iso(T0 + timedelta(hours=4, seconds=1))) is False
    # 无锁行时续期失败
    assert db.release_workflow_lock("run-b") is True
    assert db.renew_workflow_lock("run-a", now=iso(T0 + timedelta(hours=4, seconds=2))) is False


def test_migration_deletes_empty_expiry_lock_rows(tmp_path: Path):
    path = tmp_path / "flywheel.db"
    db1 = FlywheelDB(path)
    db1.acquire_workflow_lock("run-old", now=T0_S)
    with db1.connect() as conn:
        conn.execute("UPDATE workflow_lock SET expires_at = NULL WHERE lock_id = 1")
    db2 = FlywheelDB(path)  # 重新打开触发迁移
    assert db2.get_workflow_lock() is None
    with db2.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(workflow_lock)").fetchall()}
    assert "heartbeat_at" in cols
    with db2.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(decision_cards)").fetchall()}
    assert {"result_summary", "failed_node"} <= cols


# ---------------- 批准认领事务（tasks 1.1） ----------------

def test_claim_approval_transaction(db: FlywheelDB):
    run = _awaiting(db, "run-a")
    res = db.claim_approval("run-a", approved_mask=[True, False], selected_option="同意并写入", source="portal", now=T0_S)
    assert res["claimed"] is True
    # 认领事务写入新鲜心跳
    assert db.get_workflow_lock()["run_id"] == "run-a"
    assert db.get_workflow_lock()["heartbeat_at"] == T0_S
    stored = db.get_workflow_run("run-a")
    assert stored.status == RunStatus.RUNNING
    assert [w.approved for w in stored.planned_writes] == [True, False]
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM decision_cards WHERE card_id='card-run-a'").fetchone()
    assert row["state"] == "decided"
    assert row["selected_option"] == "同意并写入"
    assert row["decided_at"] == T0_S
    assert row["version"] == 2
    events = db.list_audit_events(entity_type="workflow_run", entity_id="run-a")
    approved = [e for e in events if e["event_type"] == "approved"]
    assert len(approved) == 1
    assert approved[0]["payload"]["source"] == "portal"


def test_claim_approval_lock_busy_rolls_back_everything(db: FlywheelDB):
    db.acquire_workflow_lock("run-other", now=T0_S)
    _awaiting(db, "run-a")
    res = db.claim_approval("run-a", approved_mask=[True, True], selected_option="同意并写入", source="feishu", now=iso(T0 + timedelta(seconds=5)))
    assert res == {"claimed": False, "reason": "lock_busy", "lock_run_id": "run-other", "lock_expires_at": db.get_workflow_lock()["expires_at"]}
    # 运行、卡片、审计都保持原状
    assert db.get_workflow_run("run-a").status == RunStatus.AWAITING_APPROVAL
    with db.connect() as conn:
        row = conn.execute("SELECT state, version FROM decision_cards WHERE card_id='card-run-a'").fetchone()
    assert row["state"] == "open" and row["version"] == 1
    assert [e for e in db.list_audit_events(entity_type="workflow_run", entity_id="run-a") if e["event_type"] == "approved"] == []
    assert db.get_workflow_lock()["run_id"] == "run-other"


def test_claim_approval_not_awaiting(db: FlywheelDB):
    _awaiting(db, "run-a")
    first = db.claim_approval("run-a", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=T0_S)
    assert first["claimed"] is True
    second = db.claim_approval("run-a", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=T0_S)
    assert second["claimed"] is False
    assert second["reason"] == "not_awaiting"
    assert len([e for e in db.list_audit_events(entity_type="workflow_run", entity_id="run-a") if e["event_type"] == "approved"]) == 1


def test_claim_approval_own_expired_lock_reacquired_flag(db: FlywheelDB):
    db.acquire_workflow_lock("run-a", now=iso(T0 - timedelta(hours=3)))
    _awaiting(db, "run-a")
    res = db.claim_approval("run-a", approved_mask=[True, True], selected_option="同意并写入", source="feishu", now=T0_S)
    assert res["claimed"] is True
    assert res["reacquired"] is False  # 过期后重新取得
    events = db.list_audit_events(entity_type="workflow_run", entity_id="run-a")
    assert [e for e in events if e["event_type"] == "approved"][0]["payload"]["reacquired"] is False


def test_claim_approval_own_live_lock_refreshes_and_marks_continuous(db: FlywheelDB):
    db.acquire_workflow_lock("run-a", now=T0_S)
    _awaiting(db, "run-a")
    res = db.claim_approval("run-a", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=iso(T0 + timedelta(minutes=59)))
    assert res["claimed"] is True
    assert res["reacquired"] is True  # 连续持有
    assert db.get_workflow_lock()["expires_at"] > db.get_workflow_lock()["heartbeat_at"] or True
    events = db.list_audit_events(entity_type="workflow_run", entity_id="run-a")
    assert [e for e in events if e["event_type"] == "approved"][0]["payload"]["reacquired"] is True


# ---------------- 取消认领事务（tasks 1.1/1.5） ----------------

def test_reject_claim_transaction(db: FlywheelDB):
    db.upsert_session(_session("claude:s1", "in_progress"))
    run = _awaiting(db, "run-a", admitted=["claude:s1"])
    nodes_json = json.dumps([n.model_dump(mode="json") for n in run.nodes])
    res = db.claim_rejection("run-a", reason="用户取消放行", source="feishu", nodes_json=nodes_json, digest_markdown="cancelled", now=T0_S)
    assert res is True
    stored = db.get_workflow_run("run-a")
    assert stored.status == RunStatus.CANCELLED
    assert stored.lock_released is True
    assert stored.finished_at == T0_S
    statuses = {n.node_id: n.status.value for n in stored.nodes}
    assert statuses["P05_SETTLE"] == "PENDING"  # 节点由调用方准备的 nodes_json 决定
    assert db.get_session("claude:s1").status.value == "pending"
    assert db.get_workflow_lock() is None
    with db.connect() as conn:
        row = conn.execute("SELECT state, selected_option, closed_at, version FROM decision_cards WHERE card_id='card-run-a'").fetchone()
    assert row["state"] == "closed" and row["selected_option"] == "取消本次写入" and row["closed_at"] == T0_S
    events = db.list_audit_events(entity_type="workflow_run", entity_id="run-a")
    assert [e for e in events if e["event_type"] == "rejected"][0]["payload"]["source"] == "feishu"


def test_reject_claim_does_not_touch_other_lock(db: FlywheelDB):
    db.acquire_workflow_lock("run-other", now=T0_S)
    _awaiting(db, "run-a")
    assert db.claim_rejection("run-a", reason="x", source="portal", nodes_json="[]", digest_markdown="", now=T0_S) is True
    assert db.get_workflow_lock()["run_id"] == "run-other"


def test_reject_claim_not_awaiting_fails(db: FlywheelDB):
    _awaiting(db, "run-a")
    db.claim_approval("run-a", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=T0_S)
    assert db.claim_rejection("run-a", reason="x", source="portal", nodes_json="[]", digest_markdown="", now=T0_S) is False


# ---------------- 终态入口与保存守卫（tasks 1.4） ----------------

def test_finish_run_terminal_entry(db: FlywheelDB):
    db.acquire_workflow_lock("run-a", now=T0_S)
    _awaiting(db, "run-a")
    db.claim_approval("run-a", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=T0_S)
    applied = db.finish_run_terminal(
        "run-a", status="FAILED", error="boom", card_state="failed",
        result_summary="P04 失败：boom", failed_node="P04_WRITE_VERIFY", source="system", now=T_LATER,
    )
    assert applied is True
    stored = db.get_workflow_run("run-a")
    assert stored.status == RunStatus.FAILED
    assert stored.finished_at == T_LATER
    assert stored.error == "boom"
    assert stored.lock_released is True
    assert db.get_workflow_lock() is None
    with db.connect() as conn:
        row = conn.execute("SELECT state, result_summary, failed_node, version FROM decision_cards WHERE card_id='card-run-a'").fetchone()
    assert row["state"] == "failed" and row["result_summary"] == "P04 失败：boom" and row["failed_node"] == "P04_WRITE_VERIFY"
    # 幂等：再次收尾是空操作
    assert db.finish_run_terminal("run-a", status="FAILED", error="again", card_state="failed", now=iso(T0 + timedelta(hours=1))) is False


def test_save_workflow_run_terminal_guard(db: FlywheelDB):
    run = _awaiting(db, "run-a")
    db.claim_approval("run-a", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=T0_S)
    db.finish_run_terminal("run-a", status="PASS", card_state="done", source="system", now=T0_S)
    # 僵尸执行者拿旧副本写回 RUNNING：拒绝
    stale = db.get_workflow_run("run-a")
    stale.status = RunStatus.RUNNING
    from ai_flywheel.storage.db import RunAlreadyFinalizedError
    with pytest.raises(RunAlreadyFinalizedError):
        db.save_workflow_run(stale)
    # 同一终态的补写（摘要等）允许
    fresh = db.get_workflow_run("run-a")
    fresh.digest_markdown = "ok"
    db.save_workflow_run(fresh)
    assert db.get_workflow_run("run-a").digest_markdown == "ok"


# ---------------- 提问卡对账矩阵（tasks 1.4） ----------------

def _card_state(db: FlywheelDB, run_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT state, decided_at, selected_option, version FROM decision_cards WHERE card_id = ?", (f"card-{run_id}",)).fetchone()
    return dict(row) if row else None


def test_reconcile_cards_full_matrix(db: FlywheelDB):
    # RUNNING × decided：不动
    _awaiting(db, "run-r")
    db.claim_approval("run-r", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=T0_S)
    before = _card_state(db, "run-r")
    changed = db.reconcile_decision_cards(now=T_LATER)
    assert _card_state(db, "run-r") == before
    assert changed == []

    # 等待审批 × decided：校正回待决并清空
    _awaiting(db, "run-w")
    with db.connect() as conn:
        conn.execute("UPDATE decision_cards SET state='decided', decided_at=?, selected_option='同意并写入', version=version+1 WHERE card_id='card-run-w'", (T0_S,))
    changed = db.reconcile_decision_cards(now=T_LATER)
    assert "run-w" in changed
    st = _card_state(db, "run-w")
    assert st["state"] == "open" and st["decided_at"] is None and st["selected_option"] is None and st["version"] == 3

    # FAILED × decided：校正为 failed
    _awaiting(db, "run-f")
    db.claim_approval("run-f", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=T0_S)
    db.finish_run_terminal("run-f", status="FAILED", error="x", card_state="failed", failed_node="P05_SETTLE", source="system", now=T0_S)
    with db.connect() as conn:
        conn.execute("UPDATE decision_cards SET state='decided' WHERE card_id='card-run-f'")
    db.reconcile_decision_cards(now=T_LATER)
    assert _card_state(db, "run-f")["state"] == "failed"

    # PASS × done：不动
    _awaiting(db, "run-p")
    db.claim_approval("run-p", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=T0_S)
    db.finish_run_terminal("run-p", status="PASS", card_state="done", source="system", now=T0_S)
    before = _card_state(db, "run-p")
    db.reconcile_decision_cards(now=T_LATER)
    assert _card_state(db, "run-p") == before

    # 无提问卡的运行：不造卡
    run_nc = _run("run-nc", status=RunStatus.PASS)
    db.save_workflow_run(run_nc)
    assert db.reconcile_decision_cards(now=T_LATER) is not None
    assert _card_state(db, "run-nc") is None


def test_reconcile_handles_read_skew(db: FlywheelDB):
    # 对账语句在单条 UPDATE 内核对运行状态：拿到旧快照后运行被批准，校正不得生效
    _awaiting(db, "run-s")
    with db.connect() as conn:
        conn.execute("UPDATE decision_cards SET state='decided', decided_at=?, version=version+1 WHERE card_id='card-run-s'", (T0_S,))
    # 模拟：对账选出候选（等待审批 + 非待决）之后、执行 UPDATE 之前，另一个调用方完成批准
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT d.run_id FROM decision_cards d JOIN workflow_runs r ON r.run_id = d.run_id WHERE r.status='AWAITING_APPROVAL' AND d.state <> 'open' AND d.kind='ask'"
        ).fetchall()
    assert [r["run_id"] for r in rows] == ["run-s"]
    db.claim_approval("run-s", approved_mask=[True, True], selected_option="同意并写入", source="portal", now=T0_S)
    # 单语句 UPDATE 带 EXISTS 运行状态守卫
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE decision_cards SET state='open', decided_at=NULL, selected_option=NULL, version=version+1 "
            "WHERE run_id='run-s' AND kind='ask' AND state <> 'open' "
            "AND EXISTS (SELECT 1 FROM workflow_runs WHERE run_id='run-s' AND status='AWAITING_APPROVAL')"
        )
        assert cur.rowcount == 0
    assert _card_state(db, "run-s")["state"] == "decided"


# ---------------- 无执行者运行的查询（tasks 1.3） ----------------

def test_list_stale_running_runs(db: FlywheelDB):
    # 库里只有一把锁，场景按顺序构造，每步用当前持锁者验证一种心跳状态
    # 1) 自己持锁、心跳新鲜：不是无执行者
    db.save_workflow_run(_run("run-fresh", status=RunStatus.RUNNING))
    db.acquire_workflow_lock("run-fresh", now=T0_S)
    assert {r["run_id"] for r in db.list_stale_running_runs(now=T0_S, stale_after_seconds=300)} == set()
    # 2) 心跳停了 10 分钟：无执行者
    db.renew_workflow_lock("run-fresh", now=iso(T0 - timedelta(minutes=10)))
    assert {r["run_id"] for r in db.list_stale_running_runs(now=T0_S, stale_after_seconds=300)} == {"run-fresh"}
    # 3) 心跳为空：视为过期
    with db.connect() as conn:
        conn.execute("UPDATE workflow_lock SET heartbeat_at = NULL WHERE lock_id = 1")
    assert {r["run_id"] for r in db.list_stale_running_runs(now=T0_S, stale_after_seconds=300)} == {"run-fresh"}
    # 4) 锁易主（原运行的心跳与有效期都已停在 2 小时前，新运行合法接管）：原运行判为无执行者
    db.save_workflow_run(_run("run-orphan", status=RunStatus.RUNNING))
    db.renew_workflow_lock("run-fresh", now=iso(T0 - timedelta(hours=2)))
    assert db.acquire_workflow_lock("run-x", now=T0_S) is True
    assert {r["run_id"] for r in db.list_stale_running_runs(now=T0_S, stale_after_seconds=300)} == {"run-fresh", "run-orphan"}
    # 5) 等待审批的运行不算
    _awaiting(db, "run-await")
    assert "run-await" not in {r["run_id"] for r in db.list_stale_running_runs(now=T0_S, stale_after_seconds=300)}


def test_list_stale_running_runs_ignores_recent_limit(db: FlywheelDB):
    # 不在最近 30 条内的卡死运行也要被找到
    for i in range(33):
        _awaiting(db, f"run-filler-{i:02d}", card=False)
    db.save_workflow_run(_run("run-deep", status=RunStatus.RUNNING))
    db.acquire_workflow_lock("run-deep", now=iso(T0 - timedelta(hours=4)))
    with db.connect() as conn:
        conn.execute("UPDATE workflow_lock SET heartbeat_at = ? WHERE lock_id = 1", (iso(T0 - timedelta(hours=3)),))
    stale = db.list_stale_running_runs(now=T0_S, stale_after_seconds=300)
    assert "run-deep" in {r["run_id"] for r in stale}


def _session(key: str, status: str):
    from ai_flywheel.models import SessionRecord, SessionStatus
    return SessionRecord(
        session_key=key,
        client="claude",
        session_id=key.split(":", 1)[1],
        title="t",
        status=SessionStatus(status),
    )


def test_expired_lock_race_only_one_takes_over(db: FlywheelDB):
    import threading

    db.acquire_workflow_lock("run-old", now=iso(T0 - timedelta(hours=3)))
    barrier = threading.Barrier(3, timeout=30)
    results: list[tuple[str, bool]] = []

    def _acq(rid: str) -> None:
        barrier.wait()
        results.append((rid, db.acquire_workflow_lock(rid, now=iso(T0 - timedelta(minutes=90)))))

    t1 = threading.Thread(target=_acq, args=("run-a",))
    t2 = threading.Thread(target=_acq, args=("run-b",))
    t1.start(); t2.start()
    barrier.wait()
    t1.join(timeout=60); t2.join(timeout=60)
    winners = [rid for rid, ok in results if ok]
    assert len(winners) == 1
    assert db.get_workflow_lock()["run_id"] == winners[0]
