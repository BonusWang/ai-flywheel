"""Wave 2B（服务层）回归测试：取消态、敏感情项唯一来源、后台触发、配置深合并与只读核心卡。

覆盖：
- W2.3 终态：拒绝放行把运行置为 `CANCELLED`，不再借用 `PARTIAL`；对账把取消的提问卡关掉。
- W2.3 敏感情项：`is_sensitive_write` 是选项、审批门禁与卡片渲染共用的唯一判定。
- W2.5 服务层：后台触发先答后跑、配置深合并、核心卡只读、观测升级下沉到引擎。
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig, load_config
from ai_flywheel.core.core_card import MAX_LINES, core_card_path
from ai_flywheel.models import (
    CreateRuleRequest,
    PlannedWrite,
    RegisterSessionRequest,
    ReportSignalRequest,
    RunStatus,
    SignalKind,
    is_sensitive_write,
)
from ai_flywheel.runtime import FlywheelRuntime

REPO_ROOT = Path(__file__).resolve().parent.parent


def _runtime(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = load_config(base_dir=tmp_path)
    return FlywheelRuntime(cfg)


def _seed_session(rt: FlywheelRuntime, key: str = "claude:wave2b-01") -> None:
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


def _wait_for_status(rt: FlywheelRuntime, run_id: str, wanted: set[RunStatus], timeout: float = 60.0):
    deadline = time.time() + timeout
    stored = None
    while time.time() < deadline:
        stored = rt.db.get_workflow_run(run_id)
        if stored is not None and stored.status in wanted:
            return stored
        time.sleep(0.2)
    return stored


# ---------------- W2.3 敏感情项：唯一判定 ----------------


def test_the_sensitivity_predicate_has_one_source():
    from ai_flywheel.adapters.channels.feishu.card import is_sensitive

    skill = PlannedWrite(target_kind="skill_improvement", action="create", title="t", summary="s")
    note = PlannedWrite(target_kind="knowledge_doc", action="create", title="t", summary="s")
    flagged = PlannedWrite(
        target_kind="knowledge_doc", action="create", title="t", summary="s", requires_approval=True
    )
    destroy = PlannedWrite(target_kind="knowledge_doc", action="delete", title="t", summary="s")

    assert is_sensitive_write(skill) is True
    assert is_sensitive_write(note) is False
    assert is_sensitive_write(flagged) is True
    assert is_sensitive_write(destroy) is True

    # 卡片与门禁读到的就是字典，判定必须一致。
    assert is_sensitive_write({"target_kind": "skill_improvement", "action": "create"}) is True
    assert is_sensitive_write({"target_kind": "knowledge_doc", "action": "create"}) is False
    assert is_sensitive_write({"target_kind": "knowledge_doc", "action": "overwrite_core"}) is True

    # 卡片不再自己判断，它委托同一个谓词。
    assert is_sensitive({"target_kind": "skill_improvement", "action": "create"}) is True
    assert is_sensitive({"target_kind": "knowledge_doc", "action": "create"}) is False


def test_the_approval_gate_uses_the_same_sensitivity_predicate():
    from ai_flywheel.core.orchestrator import WorkflowOrchestrator

    ordinary = PlannedWrite(target_kind="knowledge_doc", action="create", title="笔记", summary="s")
    skill = PlannedWrite(target_kind="skill_improvement", action="update", title="Skill", summary="s")

    pause, reasons = WorkflowOrchestrator._needs_manual_approval([ordinary], "smart")
    assert pause is False and reasons == []

    pause, reasons = WorkflowOrchestrator._needs_manual_approval([skill], "smart")
    assert pause is True and reasons


# ---------------- W2.3 取消态 ----------------


def test_a_rejected_run_is_cancelled_not_partial(tmp_path: Path):
    rt = _runtime(tmp_path)
    _seed_session(rt)
    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL

    cancelled = rt.orchestrator.reject_run(run.run_id, reason="用户取消本次写入")
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.status != RunStatus.PARTIAL
    assert rt.db.get_workflow_run(run.run_id).status == RunStatus.CANCELLED


def test_the_reconciliation_closes_the_card_of_a_cancelled_run(tmp_path: Path):
    rt = _runtime(tmp_path)
    _seed_session(rt)
    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    rt.orchestrator.reject_run(run.run_id, reason="用户取消本次写入")

    with rt.db.connect() as conn:
        row = conn.execute(
            "SELECT state FROM decision_cards WHERE run_id = ?", (run.run_id,)
        ).fetchone()
    assert row["state"] == "closed"

    # 对账再跑一次也不能把它改回待决。
    rt.db.reconcile_decision_cards()
    with rt.db.connect() as conn:
        row = conn.execute(
            "SELECT state FROM decision_cards WHERE run_id = ?", (run.run_id,)
        ).fetchone()
    assert row["state"] == "closed"


# ---------------- W2.4 存储拆分：迁移路径仍然走得通 ----------------


def test_the_v2_backfill_survives_a_restart_on_an_older_database(tmp_path: Path):
    """一个还没迁移过的旧库必须能被迁移：回填路径要读运行里的 JSON 快照。"""
    from ai_flywheel.models import NodeExecution, NodeStatus, WorkflowRunRecord
    from ai_flywheel.storage.db import FlywheelDB

    db_path = tmp_path / "flywheel.db"
    db = FlywheelDB(db_path)
    db.save_workflow_run(
        WorkflowRunRecord(
            run_id="run-old",
            trigger="cli",
            phase="daily",
            runner_mode="dry_run",
            status=RunStatus.PASS,
            nodes=[
                NodeExecution(
                    node_id="P00_PREPARE", name="准备", actor="cli", status=NodeStatus.PASS
                )
            ],
        )
    )
    # 抹掉 v2 迁移标记，模拟一个还没迁移过的旧库。
    with db.connect() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = 2")
    del db

    reopened = FlywheelDB(db_path)
    with reopened.connect() as conn:
        mig = conn.execute("SELECT version FROM schema_migrations WHERE version = 2").fetchone()
        assert mig is not None
        nodes = conn.execute(
            "SELECT COUNT(1) AS c FROM workflow_nodes WHERE run_id = ?", ("run-old",)
        ).fetchone()["c"]
        assert nodes == 1


# ---------------- W2.5 后台触发与批准 ----------------


def test_the_background_trigger_answers_before_the_pipeline_finishes(tmp_path: Path):
    rt = _runtime(tmp_path)
    _seed_session(rt)
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        resp = client.post("/api/site/runs?background=true", json={"phase": "daily"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["background"] is True
        assert body["run"] is None
        run_id = body["run_id"]
        assert run_id.startswith("run-")

    stored = _wait_for_status(rt, run_id, {RunStatus.AWAITING_APPROVAL, RunStatus.PASS, RunStatus.FAILED})
    assert stored is not None and stored.status == RunStatus.AWAITING_APPROVAL


def test_the_background_trigger_reports_a_busy_lock_before_it_answers(tmp_path: Path):
    rt = _runtime(tmp_path)
    assert rt.db.acquire_workflow_lock("run-other") is True
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        resp = client.post("/api/site/runs?background=true", json={"phase": "daily"})
        assert resp.status_code == 409
        assert "lock" in resp.json()["detail"].lower()
    # 别人的锁没有被后台触发动过。
    assert rt.db.get_workflow_lock()["run_id"] == "run-other"


def test_the_background_approval_claims_before_it_answers(tmp_path: Path):
    rt = _runtime(tmp_path)
    _seed_session(rt)
    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        resp = client.post(
            f"/api/site/runs/{run.run_id}/approve?background=true",
            json={"approved_indices": None},
        )
        assert resp.status_code == 200
        assert resp.json()["background"] is True
        # 认领已经生效：运行不再等待审批。
        assert rt.db.get_workflow_run(run.run_id).status != RunStatus.AWAITING_APPROVAL

    stored = _wait_for_status(rt, run.run_id, {RunStatus.PASS, RunStatus.FAILED})
    assert stored is not None and stored.status == RunStatus.PASS


def test_the_synchronous_trigger_still_answers_with_the_run(tmp_path: Path):
    """默认仍是同步：没有 background 参数时，响应里带着完整的运行记录。"""
    rt = _runtime(tmp_path)
    _seed_session(rt)
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        resp = client.post("/api/site/runs", json={"phase": "daily"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["run"]["status"] == "AWAITING_APPROVAL"
        assert body["run"]["run_id"].startswith("run-")


# ---------------- W2.5 配置深合并 ----------------


def test_a_nested_config_patch_keeps_its_siblings(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.update_runtime_config({"sessions": {"triage": {"min_chars": 1234}}})
    assert rt.cfg.sessions.triage.min_chars == 1234
    # 同级字段没被覆盖。
    assert rt.cfg.sessions.triage.enabled is True
    assert rt.cfg.sessions.triage.min_turns == 2


def test_the_redacted_api_key_never_overwrites_the_configured_one(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.update_runtime_config({"runner": {"api": {"api_key": "sk-configured", "model": "m1"}}})
    assert rt.cfg.runner.api.api_key == "sk-configured"

    rt.update_runtime_config({"runner": {"api": {"api_key": "[REDACTED]", "model": "m2"}}})
    assert rt.cfg.runner.api.api_key == "sk-configured"
    assert rt.cfg.runner.api.model == "m2"


def test_the_config_update_reports_the_settings_that_need_a_restart(tmp_path: Path):
    rt = _runtime(tmp_path)
    result = rt.update_runtime_config({"sessions": {"triage": {"min_chars": 999}}})
    assert result["restart_required"] is False
    assert result["restart_reasons"] == []

    # Schedule settings hot-reload into AutomationManager without a restart.
    result = rt.update_runtime_config({"workflow": {"daily_cron": "30 21 * * *"}})
    assert result["restart_required"] is False
    assert result["restart_reasons"] == []

    result = rt.update_runtime_config({"server": {"port": 9876}})
    assert result["restart_required"] is True
    assert "server.port" in result["restart_reasons"]

    result = rt.update_runtime_config({"delivery": {"feishu_cli": {"identity": "user"}}})
    assert result["restart_required"] is True
    assert "delivery.feishu_cli.identity" in result["restart_reasons"]


def test_the_config_endpoint_reports_the_restart_metadata(tmp_path: Path):
    rt = _runtime(tmp_path)
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        resp = client.put("/api/site/config", json={"delivery": {"feishu_cli": {"identity": "user"}}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["restart_required"] is True
        assert "delivery.feishu_cli.identity" in body["restart_reasons"]


# ---------------- W2.5 核心卡只读 ----------------


def test_reading_the_core_card_does_not_rewrite_it(tmp_path: Path):
    rt = _runtime(tmp_path)
    path = core_card_path(rt.cfg)
    assert path.exists()
    stamp = path.stat().st_mtime_ns
    content = path.read_bytes()

    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        resp = client.get("/api/site/core-card")
        assert resp.status_code == 200
        assert resp.json()["lines"] <= MAX_LINES
        # 只读：文件既没有被改写，也没有被换掉。
        assert path.stat().st_mtime_ns == stamp
        assert path.read_bytes() == content

        # 显式刷新才会重写。
        refreshed = client.post("/api/site/core-card/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["lines"] <= MAX_LINES


def test_a_signal_refreshes_the_derived_core_card(tmp_path: Path):
    rt = _runtime(tmp_path)
    rule = rt.activation.register_rule(
        CreateRuleRequest(
            title="避坑规则：先验证再写",
            domain="engineering",
            projects=["generic"],
            triggers=["缓存"],
            checklist=["跑一次真实执行"],
            forbidden_routes=["盲改"],
        )
    )
    path = core_card_path(rt.cfg)
    rt.refresh_core_card()
    before = path.read_text(encoding="utf-8")
    assert "先验证再写" in before

    rt.receipts.report_signal(
        ReportSignalRequest(
            kind=SignalKind.RULE_RECURRENCE,
            rule_id=rule.rule_id,
            summary="同一个坑又踩了一次",
        )
    )
    after = path.read_text(encoding="utf-8")
    # 规则被降级，不再出现在生效规则里，卡片也跟着变。
    assert "先验证再写" not in after


# ---------------- W2.5 观测升级下沉到引擎 ----------------


def test_the_observation_promotion_lives_in_the_engine(tmp_path: Path):
    rt = _runtime(tmp_path)
    obs, created = rt.db.upsert_mistake_observation(
        root_cause_key="cache-invalidation",
        title="构建缓存失效导致部署报错",
        severity_grade="yellow_caught",
        truth="先清缓存再验证",
    )
    assert created

    promoted = rt.activation.promote_observation(obs["obs_id"])
    assert promoted is not None
    updated, rule = promoted
    assert updated["status"] == "promoted"
    assert updated["promoted_rule_id"] == rule.rule_id
    assert rule.title.startswith("避坑规则：")
    assert rule.triggers  # 触发词从标题里取出来
    assert rule.source_pointers == []

    events = rt.db.list_audit_events(entity_type="mistake_observation", entity_id=obs["obs_id"])
    assert any(e["event_type"] == "manual_promoted" for e in events)

    # 不存在的观测不编造一条。
    assert rt.activation.promote_observation("obs-does-not-exist") is None


# ---------------- W2.5 一次解析的 lark-cli ----------------


def test_the_lark_runner_carries_the_identity(tmp_path: Path):
    from ai_flywheel.adapters.lark_cli import LarkCliRunner

    exe = tmp_path / "fake_lark.py"
    exe.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    runner = LarkCliRunner(str(exe), ["--flag"], identity="user")
    assert runner.identity == "user"

    captured: dict = {}

    def _fake_run(args, **kwargs):
        captured["args"] = args
        return 0, '{"data": {"message_id": "om_1"}}', ""

    runner.run = _fake_run  # type: ignore[method-assign]
    runner.send_text(user_id="ou_1", text="hi")
    assert "--as" in captured["args"]
    assert captured["args"][captured["args"].index("--as") + 1] == "user"


def test_the_delivery_adapter_shares_the_resolved_runner(tmp_path: Path):
    from ai_flywheel.adapters.delivery import DeliveryAdapter

    adapter = DeliveryAdapter(FlywheelConfig(base_dir=tmp_path))
    sentinel = object()
    adapter.set_lark_runner(sentinel)  # type: ignore[arg-type]
    assert adapter.lark is sentinel


# ---------------- W2.3 心跳的线程安全 ----------------


def test_the_heartbeat_start_and_stop_survive_a_race():
    from ai_flywheel.core.heartbeat import HeartbeatManager

    def _renew(run_id: str) -> bool:
        return True

    hm = HeartbeatManager(_renew, interval_seconds=0.05)
    errors: list[Exception] = []

    def _churn() -> None:
        try:
            for _ in range(20):
                hm.start("run-x")
                hm.stop()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def _stopper() -> None:
        try:
            for _ in range(40):
                hm.stop()
                time.sleep(0.005)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_churn)
    t2 = threading.Thread(target=_stopper)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not errors
