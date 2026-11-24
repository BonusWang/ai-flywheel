"""Wave 3 回归测试：死代码清理、安全阀语义、版本单一来源与跨层契约对齐。

覆盖：
- W3.1 死代码与版本单一来源：`__version__` 是唯一的版本事实来源；被删掉的符号
  确实没有调用方；前端死文件与陈旧标签确实没有引用。
- W3.2 安全阀：`safety_valves.auto_approve_writes=False` 必须否决
  `workflow.approval_mode="auto"`；模型执行阀有���个说真话的名字。
- W3.3 计数与导出：门户报的数字来自 COUNT(*)，不来自某一页；公开导出剥掉本地
  路径与密钥；只有生效中的规则被当作"生效中"展示。
- W3.4 CLI 相位：`flywheel run --phase` 的取值与后端一致。

所有样例数据都是虚构的。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_flywheel import __version__
from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.adapters.runners.clients import resolve_runner_mode
from ai_flywheel.cli import main as cli_main
from ai_flywheel.config import FlywheelConfig, SafetyValvesConfig, load_config
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.models import (
    OutcomeCheck,
    OutcomeOrigin,
    OutcomeRecord,
    OutcomeStatus,
    PlannedWrite,
    RetroItem,
    RetroStatus,
    RuleRecord,
    RuleStatus,
    SessionRecord,
    SessionStatus,
    SignalKind,
    VerificationKind,
    WorkflowRunRecord,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.db import FlywheelDB

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend" / "ai_flywheel"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"


# ---------------- helpers ----------------


def _runtime(tmp_path: Path) -> FlywheelRuntime:
    """A runtime with the real profile copied in, so the publication layer has a site."""
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = FlywheelConfig(base_dir=tmp_path)
    return FlywheelRuntime(cfg)


def _session(index: int, status: SessionStatus = SessionStatus.PENDING) -> SessionRecord:
    return SessionRecord(
        session_key=f"sess-{index:03d}",
        client="claude-code",
        session_id=f"id-{index:03d}",
        title=f"会话 {index}",
        project="generic",
        status=status,
    )


def _rule(index: int, status: RuleStatus) -> RuleRecord:
    return RuleRecord(
        rule_id=f"rule-{index:03d}",
        title=f"规则 {index}",
        status=status,
    )


# ---------------- W3.1 版本单一来源 ----------------


def test_version_is_declared_in_one_place_and_agrees_everywhere():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__

    site = yaml.safe_load((REPO_ROOT / "profile" / "site.yaml").read_text(encoding="utf-8"))
    assert site["brand"]["version"] == __version__

    package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == __version__


def test_no_module_hardcodes_the_old_version():
    """The version lives in `ai_flywheel.__version__`; no module may repeat it."""
    stale = ("0.1.0", "0.2.0", "0.3.0")
    offenders: list[str] = []
    for path in BACKEND_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in stale:
            if f'"{needle}"' in text or f"'{needle}'" in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {needle}")
    assert offenders == []


def test_runtime_surfaces_the_version(tmp_path: Path):
    from fastapi.testclient import TestClient

    rt = _runtime(tmp_path)
    from ai_flywheel.app import create_app

    with TestClient(create_app(runtime=rt)) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["version"] == __version__


# ---------------- W3.1 死代码确实没有调用方 ----------------


def test_removed_backend_symbols_are_gone():
    assert not hasattr(PolicyEngine, "load_taxonomy")
    assert not hasattr(FlywheelDB, "update_decision_card_state")

    from ai_flywheel.core import feishu_card, feishu_listen, triage
    from ai_flywheel.storage import clock

    assert not hasattr(feishu_card, "FIELD_LIMITS")
    assert not hasattr(feishu_listen, "_PERMANENT_MARKERS")
    assert not hasattr(triage, "_RAW_ID_TITLE_RE")

    fake = clock.FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "_monotonic" not in fake.__dict__
    fake.advance(5)
    assert fake.now_iso().startswith("2026-01-01T00:00:05")


def test_removed_files_are_gone():
    assert not (REPO_ROOT / "profile" / "taxonomy.yaml").exists()
    assert not (FRONTEND_SRC / "components" / "PipelineDagCanvas.tsx").exists()
    assert not (FRONTEND_SRC / "components" / "FlywheelLoopDiagram.tsx").exists()


def test_no_frontend_module_imports_the_deleted_components():
    """A deleted file that is still imported would break the build silently."""
    for path in FRONTEND_SRC.rglob("*.ts*"):
        text = path.read_text(encoding="utf-8")
        assert "PipelineDagCanvas" not in text, path
        assert "FlywheelLoopDiagram" not in text, path
        assert "LoopStep" not in text, path


def test_frontend_labels_have_no_stale_keys():
    labels = (FRONTEND_SRC / "lib" / "labels.ts").read_text(encoding="utf-8")
    assert "PRIORITY_LABELS" not in labels
    assert "P01_DISCOVER" not in labels
    assert "P03_PLAN" not in labels


# ---------------- W3.2 安全阀矩阵 ----------------


def test_default_config_keeps_every_write_under_review():
    cfg = FlywheelConfig()
    assert cfg.safety_valves.auto_approve_writes is False
    assert cfg.effective_auto_approve is False
    assert cfg.effective_approval_mode == "always"


def test_closed_valve_vetoes_auto_approval_mode():
    cfg = FlywheelConfig()
    cfg.workflow.approval_mode = "auto"
    cfg.safety_valves.auto_approve_writes = False
    # The valve has the last word: no auto-approval ...
    assert cfg.effective_auto_approve is False
    # ... and the mode is downgraded to `smart`, never to a silent bypass.
    assert cfg.effective_approval_mode == "smart"


def test_open_valve_lets_auto_approval_mode_through():
    cfg = FlywheelConfig()
    cfg.workflow.approval_mode = "auto"
    cfg.safety_valves.auto_approve_writes = True
    assert cfg.effective_auto_approve is True
    assert cfg.effective_approval_mode == "auto"


def test_closed_valve_vetoes_the_auto_approve_writes_flag():
    cfg = FlywheelConfig()
    cfg.workflow.auto_approve_writes = True
    cfg.workflow.approval_mode = "always"
    cfg.safety_valves.auto_approve_writes = False
    assert cfg.effective_auto_approve is False
    assert cfg.effective_approval_mode == "always"


def test_open_valve_lets_the_auto_approve_writes_flag_through():
    cfg = FlywheelConfig()
    cfg.workflow.auto_approve_writes = True
    cfg.workflow.approval_mode = "always"
    cfg.safety_valves.auto_approve_writes = True
    assert cfg.effective_auto_approve is True
    assert cfg.effective_approval_mode == "auto"


def test_smart_and_always_modes_are_untouched_by_the_valve():
    for mode in ("smart", "always"):
        cfg = FlywheelConfig()
        cfg.workflow.approval_mode = mode
        for valve in (False, True):
            cfg.safety_valves.auto_approve_writes = valve
            assert cfg.effective_approval_mode == mode


def test_unknown_approval_mode_falls_back_to_always():
    cfg = FlywheelConfig()
    cfg.workflow.approval_mode = "nonsense"
    assert cfg.effective_approval_mode == "always"


def test_a_vetoed_auto_mode_still_pauses_for_a_sensitive_write(tmp_path: Path):
    """The downgrade to `smart` must not turn into "write everything"."""
    rt = _runtime(tmp_path)
    rt.cfg.workflow.approval_mode = "auto"
    rt.cfg.safety_valves.auto_approve_writes = False

    from ai_flywheel.core.orchestrator import WorkflowOrchestrator

    writes = [
        PlannedWrite(
            target_kind="skill_improvement",
            action="update",
            title="改一个 Skill",
            summary="需要人看一眼",
        )
    ]
    should_pause, reasons = WorkflowOrchestrator._needs_manual_approval(
        writes, rt.cfg.effective_approval_mode
    )
    assert should_pause is True
    assert reasons


def test_the_run_itself_is_gated_by_the_valve(tmp_path: Path):
    """The valve is not only a config value: the batch records what really applies."""
    closed = _runtime(tmp_path / "closed")
    closed.cfg.workflow.approval_mode = "auto"
    closed.cfg.safety_valves.auto_approve_writes = False
    closed_run = closed.orchestrator.start_run(trigger="test", phase="daily")
    assert closed_run.approval_mode == "smart"

    opened = _runtime(tmp_path / "opened")
    opened.cfg.workflow.approval_mode = "auto"
    opened.cfg.safety_valves.auto_approve_writes = True
    opened_run = opened.orchestrator.start_run(trigger="test", phase="daily")
    assert opened_run.approval_mode == "auto"


# ---------------- W3.2 模型执行阀 ----------------


def test_model_execution_valve_defaults_to_the_legacy_switch():
    valves = SafetyValvesConfig()
    assert valves.model_execution_enabled is None
    assert valves.effective_model_execution_enabled is True

    valves.cli_execution_enabled = False
    assert valves.effective_model_execution_enabled is False


def test_model_execution_valve_overrides_the_legacy_switch():
    valves = SafetyValvesConfig(cli_execution_enabled=True, model_execution_enabled=False)
    assert valves.effective_model_execution_enabled is False

    valves = SafetyValvesConfig(cli_execution_enabled=False, model_execution_enabled=True)
    assert valves.effective_model_execution_enabled is True


def test_model_execution_valve_reads_its_environment_variable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLYWHEEL_MODEL_EXECUTION_ENABLED", "false")
    cfg = load_config(base_dir=tmp_path)
    assert cfg.safety_valves.model_execution_enabled is False
    assert cfg.safety_valves.effective_model_execution_enabled is False

    monkeypatch.setenv("FLYWHEEL_MODEL_EXECUTION_ENABLED", "true")
    cfg = load_config(base_dir=tmp_path)
    assert cfg.safety_valves.effective_model_execution_enabled is True


def test_a_closed_model_execution_valve_downgrades_the_runner_to_dry_run(tmp_path: Path):
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9/v1"
    cfg.runner.api.model = "some-model"
    assert resolve_runner_mode(cfg) == "api:some-model"

    cfg.safety_valves.model_execution_enabled = False
    assert resolve_runner_mode(cfg) == "dry_run"


# ---------------- W3.2 交付与认领命名 ----------------


def test_send_feishu_text_notice_is_the_real_name_and_the_old_one_aliases_it():
    cfg = FlywheelConfig()
    cfg.delivery.feishu_cli.enabled = True
    cfg.delivery.feishu_cli.user_id = "ou_test"
    adapter = DeliveryAdapter(cfg)

    seen: list[dict[str, Any]] = []

    class _Result:
        status = "sent"
        message_id = "om_1"
        error = None

    def _send_text(**kwargs: Any):
        seen.append(kwargs)
        return _Result()

    adapter._lark = type("_Lark", (), {"send_text": staticmethod(_send_text)})()
    out = adapter.send_feishu_text_notice(title="标题", markdown="正文")
    assert out["status"] == "sent"
    assert seen and seen[0]["text"].startswith("【标题】")

    # The legacy name still works and reaches the same implementation.
    seen.clear()
    alias_out = adapter.send_feishu_card(title="标题", markdown="正文")
    assert alias_out["status"] == "sent"
    assert seen and seen[0]["text"].startswith("【标题】")


def test_claim_approval_names_the_lock_state_and_keeps_the_legacy_key(tmp_path: Path):
    db = FlywheelDB(tmp_path / "flywheel.db")
    run = WorkflowRunRecord(run_id="run-x", status="AWAITING_APPROVAL")
    run.planned_writes = [
        PlannedWrite(target_kind="knowledge_doc", action="create", title="t", summary="s")
    ]
    db.save_workflow_run(run)
    db.acquire_workflow_lock("run-x")

    res = db.claim_approval(
        "run-x", approved_mask=[True], selected_option="同意并写入", source="portal"
    )
    assert res["claimed"] is True
    # The lock was held continuously by this very run.
    assert res["lock_continuously_held"] is True
    # The historical key is kept, with the same value, so nothing downstream breaks.
    assert res["reacquired"] is True

    events = [
        e for e in db.list_audit_events(entity_type="workflow_run", entity_id="run-x")
        if e["event_type"] == "approved"
    ]
    payload = events[0]["payload"]
    assert payload["lock_continuously_held"] is True
    assert payload["reacquired"] is True


# ---------------- W3.3 精确计数 ----------------


def test_counts_are_exact_and_never_cap_at_a_page(tmp_path: Path):
    db = FlywheelDB(tmp_path / "flywheel.db")
    for i in range(25):
        db.upsert_session(_session(i))
    for i in range(5):
        db.upsert_session(_session(100 + i, SessionStatus.PROCESSED))

    assert db.count_sessions() == 30
    assert db.count_sessions(status=SessionStatus.PENDING.value) == 25
    assert db.count_sessions(status=SessionStatus.PROCESSED.value) == 5


def test_workflow_run_count_is_exact_and_skips_the_internal_run(tmp_path: Path):
    db = FlywheelDB(tmp_path / "flywheel.db")
    for i in range(23):
        db.save_workflow_run(WorkflowRunRecord(run_id=f"run-{i:03d}", trigger="api"))
    db.ensure_placeholder_run("card-internal")

    assert db.count_workflow_runs() == 23
    # The internal placeholder is not a batch the portal shows.
    assert all(r.run_id != "card-internal" for r in db.list_workflow_runs(limit=100))


def test_outcome_and_retro_counts_are_exact(tmp_path: Path):
    db = FlywheelDB(tmp_path / "flywheel.db")
    for i in range(22):
        db.save_outcome(
            OutcomeRecord(
                outcome_id=f"o-{i:03d}",
                receipt_hash="h",
                session_key="s",
                client="claude-code",
                preflight_receipt_id="",
                origin=OutcomeOrigin.NATURAL,
                status=OutcomeStatus.SUCCESS,
                summary="s",
                checks=[
                    OutcomeCheck(
                        name="real-1",
                        result="pass",
                        kind=VerificationKind.REAL,
                        evidence=["e"],
                    )
                ],
                applied_rule_ids=[],
                artifacts=[],
            )
        )
    db.save_outcome(
        OutcomeRecord(
            outcome_id="o-replay",
            receipt_hash="h",
            session_key="s",
            client="claude-code",
            preflight_receipt_id="",
            origin=OutcomeOrigin.REPLAY,
            status=OutcomeStatus.SUCCESS,
            summary="s",
            checks=[],
            applied_rule_ids=[],
            artifacts=[],
        )
    )

    assert db.count_outcomes() == 23
    assert db.count_outcomes(origin=OutcomeOrigin.NATURAL.value) == 22
    assert db.count_real_verified_natural_outcomes() == 22

    for i in range(12):
        db.enqueue_retro(
            RetroItem(
                retro_id=f"r-{i:03d}",
                session_key="s",
                client="claude-code",
                kind=SignalKind.CUSTOM,
                severity="warning",
                summary="一条复盘",
            )
        )
    assert db.count_retro_items() == 12
    assert db.count_retro_items(status=RetroStatus.OPEN.value) == 12


def test_overview_metrics_report_the_exact_numbers(tmp_path: Path):
    rt = _runtime(tmp_path)
    for i in range(25):
        rt.db.upsert_session(_session(i))
    for i in range(23):
        rt.db.save_workflow_run(WorkflowRunRecord(run_id=f"run-{i:03d}", trigger="api"))

    metrics = rt.publication.get_overview()["metrics"]
    assert metrics["sessions_total"] == 25
    assert metrics["sessions_pending"] == 25
    assert metrics["workflow_runs_total"] == 23


# ---------------- W3.3 只展示生效中的规则 ----------------


def test_only_active_rules_are_listed_as_recent_rules(tmp_path: Path):
    rt = _runtime(tmp_path)
    for i, status in enumerate(
        [
            RuleStatus.VERIFIED_ACTIVE,
            RuleStatus.UNVERIFIED_ACTIVE,
            RuleStatus.NEEDS_REVISION,
            RuleStatus.DORMANT,
            RuleStatus.RETIRED,
        ]
    ):
        rt.db.upsert_rule(_rule(i, status))

    overview = rt.publication.get_overview()
    statuses = {r["status"] for r in overview["recent_rules"]}
    assert statuses == {"verified_active", "unverified_active"}
    assert len(overview["recent_rules"]) == 2

    about = rt.publication.get_about_payload()
    assert {r["status"] for r in about["recent_rules"]} == {"verified_active", "unverified_active"}

    # The dormant state has its own counter instead of being folded into another.
    counts = overview["metrics"]["rules_by_status"]
    assert counts["dormant"] == 1
    assert counts["retired"] == 1
    assert counts["needs_revision"] == 1


# ---------------- W3.3 公开导出脱敏 ----------------


def test_public_export_strips_local_paths_and_credentials(tmp_path: Path):
    rt = _runtime(tmp_path)
    run = WorkflowRunRecord(run_id="run-x", trigger="api", digest_markdown="token=sk-abcdefghijklmnop")
    run.planned_writes = [
        PlannedWrite(
            target_kind="knowledge_doc",
            action="create",
            title="卡片",
            summary="摘要",
            relative_path="C:\\Users\\someone\\kb\\lessons\\card.md",
        )
    ]
    run.nodes = []
    from ai_flywheel.models import NodeExecution

    run.nodes = [
        NodeExecution(
            node_id="P04_WRITE_VERIFY",
            name="写入核验",
            actor="orchestrator",
            artifacts=["D:\\private\\kb\\lessons\\card.md"],
            payload={"written_to": "C:\\Users\\someone\\kb\\lessons\\card.md"},
        )
    ]
    rt.db.save_workflow_run(run)

    snapshot = rt.publication.export_public_snapshot()
    dumped = json.dumps(snapshot, ensure_ascii=False)

    assert "someone" not in dumped
    assert "private" not in dumped
    assert "sk-abcdefghijklmnop" not in dumped

    exported_run = snapshot["runs"][0]
    assert exported_run["planned_writes"][0]["relative_path"] == "card.md"
    assert exported_run["nodes"][0]["artifacts"] == ["card.md"]
    assert exported_run["nodes"][0]["payload"]["written_to"] == "card.md"


def test_the_public_export_keeps_relative_paths_and_urls(tmp_path: Path):
    """Only absolute paths are stripped: a relative path and a URL stay readable."""
    rt = _runtime(tmp_path)
    run = WorkflowRunRecord(run_id="run-x", trigger="api")
    run.planned_writes = [
        PlannedWrite(
            target_kind="knowledge_doc",
            action="create",
            title="卡片",
            summary="摘要",
            relative_path="lessons/card.md",
        )
    ]
    run.nodes = []
    from ai_flywheel.models import NodeExecution

    run.nodes = [
        NodeExecution(
            node_id="P06_REPORT",
            name="生成日报",
            actor="orchestrator",
            artifacts=["lessons/card.md"],
            payload={"source": "https://example.com/docs/card.md"},
        )
    ]
    rt.db.save_workflow_run(run)

    exported = rt.publication.export_public_snapshot()["runs"][0]
    assert exported["planned_writes"][0]["relative_path"] == "lessons/card.md"
    assert exported["nodes"][0]["artifacts"] == ["lessons/card.md"]
    assert exported["nodes"][0]["payload"]["source"] == "https://example.com/docs/card.md"


def test_the_private_view_keeps_the_full_paths(tmp_path: Path):
    """Only the public export is sanitized; the local portal still shows everything."""
    rt = _runtime(tmp_path)
    run = WorkflowRunRecord(run_id="run-x", trigger="api")
    run.planned_writes = [
        PlannedWrite(
            target_kind="knowledge_doc",
            action="create",
            title="卡片",
            summary="摘要",
            relative_path="C:\\Users\\someone\\kb\\lessons\\card.md",
        )
    ]
    rt.db.save_workflow_run(run)

    latest = rt.publication.get_overview(public_mode=False)["latest_run"]
    assert latest["planned_writes"][0]["relative_path"] == "C:\\Users\\someone\\kb\\lessons\\card.md"


# ---------------- W3.4 CLI 相位 ----------------


def test_run_phase_choices_match_the_backend_phases(capsys):
    """The CLI offers exactly the phases the orchestrator understands."""
    with pytest.raises(SystemExit):
        cli_main(["run", "--phase", "weekly_exception"])
    err = capsys.readouterr().err
    assert "weekly_exception" in err  # the rejected value is named
    assert "{auto,daily,weekly,both}" in err


def test_run_phase_accepts_the_backend_phases():
    """`auto`, `daily`, `weekly` and `both` are the phases the orchestrator knows."""
    from ai_flywheel.core.orchestrator import WorkflowOrchestrator

    assert WorkflowOrchestrator.resolve_phase("daily") == "daily"
    assert WorkflowOrchestrator.resolve_phase("weekly") == "weekly"
    assert WorkflowOrchestrator.resolve_phase("both") == "both"
    assert WorkflowOrchestrator.resolve_phase("auto") in {"daily", "both"}


def test_hook_forward_reaches_the_network_layer(tmp_path: Path, monkeypatch):
    """`flywheel hook-forward` used to swallow its own UnboundLocalError.

    The inner `import urllib.error` shadowed the module-level `urllib.request`,
    so every hook-forward call fell into the fail-open branch and returned
    `{"continue": true}` without ever contacting the server.
    """
    seen: dict[str, Any] = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"forwarded": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    def _urlopen(req: Any, timeout: float | None = None) -> Any:
        seen["url"] = req.full_url
        seen["data"] = req.data
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    monkeypatch.setattr(
        sys,
        "stdin",
        type("_Stdin", (), {"read": staticmethod(lambda: '{"client": "codex"}')})(),
    )
    assert cli_main(["hook-forward", "codex", "--url", "http://127.0.0.1:8765"]) == 0
    assert seen["url"].endswith("/api/hooks/codex")
    assert seen["data"] == b'{"client": "codex"}'


# ---------------- W3.1 交付闭环的远程与分支 ----------------


def test_git_closure_reports_the_configured_remote_and_branch(tmp_path: Path):
    """The configured remote/branch are wired: the push is attempted and reported."""
    kb = tmp_path / "kb"
    kb.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(kb), capture_output=True, check=False)
    (kb / "a.md").write_text("x", encoding="utf-8")

    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(kb)
    cfg.delivery.git_closure.enabled = True
    cfg.delivery.git_closure.remote = "origin"
    cfg.delivery.git_closure.branch = "main"

    adapter = DeliveryAdapter(cfg)
    # No `origin` remote exists in this clone: the push is skipped, not fatal.
    out = adapter._git_closure(["a.md"], "run-1")
    assert "committed exact write-set" in out
    assert "push skipped" in out


def test_git_closure_skips_a_non_git_knowledge_root(tmp_path: Path):
    kb = tmp_path / "not-a-repo"
    kb.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(kb)
    adapter = DeliveryAdapter(cfg)
    assert adapter._git_closure(["a.md"], "run-1") == "SKIPPED (not a git repo)"


# ---------------- 外部 --config 启动时的路径与控制台回退 ----------------


def test_load_config_respects_explicit_base_dir_with_external_config(tmp_path: Path):
    """Explicit --base-dir must not be clobbered by an external --config path."""
    cfg_dir = tmp_path / "external"
    base = tmp_path / "repo"
    cfg_dir.mkdir()
    base.mkdir()
    cfg_file = cfg_dir / "flywheel.yaml"
    cfg_file.write_text("server:\n  port: 9911\n", encoding="utf-8")

    cfg = load_config(config_path=cfg_file, base_dir=base)
    assert cfg.base_dir == base.resolve()
    assert cfg.config_path == cfg_file.resolve()
    assert cfg.server.port == 9911


def test_external_config_falls_back_to_bundled_profile_and_spa(tmp_path: Path):
    """Running with an external --config still resolves the bundled profile/ and frontend/dist."""
    from fastapi.testclient import TestClient

    from ai_flywheel.app import create_app
    from ai_flywheel.runtime import FlywheelRuntime

    cfg_file = tmp_path / "flywheel.yaml"
    cfg_file.write_text(
        f'server:\n  data_dir: "{(tmp_path / "data").as_posix()}"\n'
        'sessions:\n  scan_sources: []\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path=cfg_file)
    assert cfg.default_profile_path == (REPO_ROOT / "profile").resolve()

    rt = FlywheelRuntime.create(config_path=cfg_file, skip_fts_sync=True)
    app = create_app(runtime=rt)
    if (REPO_ROOT / "frontend" / "dist" / "index.html").is_file():
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert '<div id="root">' in resp.text
