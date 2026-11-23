from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from ai_flywheel.adapters.runners import normalize_rule_payload
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.orchestrator import WorkflowOrchestrator
from ai_flywheel.models import PlannedWrite, RegisterSessionRequest, RunStatus
from ai_flywheel.runtime import FlywheelRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_runtime(tmp_path: Path, *, approval_mode: str = "smart") -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills_dir)]
    cfg.workflow.approval_mode = approval_mode
    return FlywheelRuntime(cfg)


def test_v2_schema_audit_columns_and_normalized_tables(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    with rt.db.connect() as conn:
        mig = conn.execute("SELECT version FROM schema_migrations WHERE version = 2").fetchone()
        assert mig is not None

        for table in (
            "sessions",
            "rules",
            "workflow_runs",
            "workflow_nodes",
            "run_sessions",
            "planned_writes",
            "decision_cards",
            "card_deliveries",
            "skill_assets",
            "system_settings",
            "policy_overrides",
            "workflow_lock",
        ):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "created_at" in cols, f"{table} missing created_at"
            assert "updated_at" in cols, f"{table} missing updated_at"


def test_p04_malformed_llm_rule_payload_never_crashes(tmp_path: Path):
    # Reproduces the exact production error: LLM omitted 'title' and used custom keys like 'scope'
    pw = PlannedWrite(
        target_kind="flywheel_rule",
        relative_path=None,
        action="register_rule",
        title="批处理合同前先检查类型字段",
        summary="避免因缺少类型检查导致批量任务异常",
        rule_payload={"scope": "文档批处理场景执行前检查", "requirement": "先核对目标类型再执行"},
        source_session_keys=["codex:s-malformed"],
    )
    normalized = normalize_rule_payload(pw)
    assert normalized["title"] == "批处理合同前先检查类型字段"
    assert any("适用范围：" in c for c in normalized["checklist"])
    assert len(normalized["triggers"]) >= 1


def test_smart_approval_auto_passes_routine_and_pauses_for_skill(tmp_path: Path):
    rt = _make_runtime(tmp_path, approval_mode="smart")

    # 1. Routine session (note + rule, NO skill workflow) -> auto-passes in smart mode!
    rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id="routine-01",
            title="修复接口超时报错",
            project="demo",
            summary="增加超时重试并验证通过",
            content="User: 接口报错。Assistant: 增加重试并实测通过。",
            tags=["api", "fix"],
        )
    )
    routine_run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert routine_run.status == RunStatus.PASS
    assert routine_run.lock_released is True
    assert routine_run.batch_date != ""
    assert routine_run.batch_seq == 1
    assert len(rt.db.list_audit_events(entity_type="workflow_run", entity_id=routine_run.run_id)) >= 2

    # 2. Session with repeated workflow -> proposes skill_improvement -> pauses for approval!
    rt.register_session(
        RegisterSessionRequest(
            client="claude-code",
            session_id="skill-02",
            title="release-check 固定步骤封装skill",
            project="ops",
            summary="每次都要按固定步骤检查版本号、构建并校验产物，值得封装成重复流程 Skill",
            content="User: 这个重复流程每次都要手工跑 4 步，请封装 skill。Assistant: 整理为固定步骤。",
            tags=["release", "workflow"],
        )
    )
    skill_run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert skill_run.status == RunStatus.AWAITING_APPROVAL
    assert any(w.target_kind == "skill_improvement" for w in skill_run.planned_writes)

    # Approve -> P04 writes validated SKILL.md into skills_dirs[0] and registers skill_assets!
    finished = rt.orchestrator.approve_and_resume(skill_run.run_id)
    assert finished.status == RunStatus.PASS, f"Run failed with error: {finished.error}"
    skill_files = list((tmp_path / "skills").glob("*/SKILL.md"))
    assert len(skill_files) == 1
    assert "---" in skill_files[0].read_text(encoding="utf-8")
    assert any(a["origin"] == "flywheel_created" for a in rt.db.list_skill_assets())


def test_friday_auto_phase_resolution():
    # 2026-11-20 20:00 Asia/Shanghai == 2026-11-20 12:00 UTC (Friday)
    friday_utc = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    thursday_utc = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    assert WorkflowOrchestrator.resolve_phase("auto", now_utc=friday_utc) == "both"
    assert WorkflowOrchestrator.resolve_phase("auto", now_utc=thursday_utc) == "daily"
