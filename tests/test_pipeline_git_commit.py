"""core/pipeline 包拆分与 P04 可选本地 Git 提交（refactor-orchestrator-pipeline-pkg）。

覆盖：

1. 薄门面：`core/orchestrator.py` 少于 100 行，公开符号与 `core/pipeline/*` 解析到同一对象；
   门面上的 `build_runner` 替换仍能作用到流水线（既有 monkeypatch 不受重构影响）。
2. 默认关闭：`auto_git_commit=False` 的真实 P04 运行不产生任何提交，也不改动已跟踪文件。
3. 开启：临时 git 仓库里只提交 P04 写入的文件；无关的未跟踪文件与已修改的已跟踪文件
   保持原样；提交信息含 run_id 与审批来源；写入 `p04_git_commit` 审计事件。
4. 非 git 目录：记录 `p04_git_commit_skipped`，运行仍然 `PASS`。

样例数据（会话、文档、项目名）全部是虚构的。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ai_flywheel.adapters.runners import OpenAICompatibleRunner
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import NodeExecution, RegisterSessionRequest, RunStatus, WorkflowRunRecord
from ai_flywheel.runtime import FlywheelRuntime

REPO_ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git")
DOC_RELATIVE = "data/knowledge/lessons/2026-11-25-api-timeout.md"


# ---------------------------------------------------------------- git helpers


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert GIT, "git is required for these tests"
    return subprocess.run(
        [GIT, *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.name", "Flywheel Test")
    _git(repo, "config", "user.email", "flywheel-test@example.com")


def _commit_count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip())


def _head_files(repo: Path) -> list[str]:
    out = _git(repo, "show", "--pretty=format:", "--name-only", "HEAD").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _is_git_repo(path: Path) -> bool:
    code = subprocess.run(
        [GIT or "git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).returncode
    return code == 0


# ---------------------------------------------------------------- run helpers


def _runtime(base: Path, *, auto_git_commit: bool) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", base / "profile")
    skills = base / "skills"
    skills.mkdir(exist_ok=True)
    cfg = FlywheelConfig(base_dir=base)
    cfg.capabilities.skills_dirs = [str(skills)]
    cfg.workflow.approval_mode = "auto"
    # `auto` only takes effect with the safety valve open.
    cfg.safety_valves.auto_approve_writes = True
    cfg.knowledge_sink.auto_git_commit = auto_git_commit
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    return FlywheelRuntime(cfg)


def _register(rt: FlywheelRuntime, session_id: str = "s1") -> None:
    rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id=session_id,
            title="接口超时排查",
            project="demo",
            summary="记录接口超时的根因",
            content="用户：接口联调报错了怎么办。助手：先看日志，再核对连接池上限。",
        )
    )


def _node(run: WorkflowRunRecord, node_id: str) -> NodeExecution:
    return next(n for n in run.nodes if n.node_id == node_id)


class _FakeModel:
    """One canned review for P02, one canned knowledge-document plan for P03."""

    def __call__(self, prompt: str, system_prompt: str) -> str:
        if prompt.startswith("You are the P02 Review Worker"):
            return json.dumps(
                {
                    "findings": [
                        {
                            "session_key": "codex:s1",
                            "client": "codex",
                            "project": "demo",
                            "title": "接口超时排查",
                            "worthy": True,
                            "category": "project",
                            "skip_reason": "",
                            "facts": ["连接池上限过小导致超时"],
                            "anomalies": [],
                            "tags": ["demo"],
                            "suggest_rule": False,
                            "skill_route": {"hit": False, "action": "none", "target_skill": "", "reason": "单次任务"},
                        }
                    ],
                    "mistakes": [],
                    "dissents": [],
                    "repeated_workflows": [],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "planned_writes": [
                    {
                        "target_kind": "knowledge_doc",
                        "title": "接口超时排查复盘",
                        "relative_path": "lessons/2026-11-25-api-timeout.md",
                        "action": "create",
                        "summary": "记录接口超时的根因与修复方式",
                        "content_preview": (
                            "---\ntitle: 接口超时排查复盘\n---\n\n"
                            "# 接口超时排查复盘\n\n## 事实回顾\n- **接口**在高峰期出现超时，根因是连接池上限过小。\n"
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        )


def _run_batch(rt: FlywheelRuntime, monkeypatch: pytest.MonkeyPatch) -> WorkflowRunRecord:
    _register(rt)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel())
    return rt.orchestrator.start_run(
        trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True
    )


def _audit_events(rt: FlywheelRuntime, run_id: str) -> dict[str, dict[str, Any]]:
    rows = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run_id, limit=200)
    return {row["event_type"]: row for row in rows}


# ---------------------------------------------------------------- 1. thin facade


def test_the_orchestrator_module_is_a_thin_facade_of_the_pipeline_package():
    facade_path = REPO_ROOT / "backend" / "ai_flywheel" / "core" / "orchestrator.py"
    assert len(facade_path.read_text(encoding="utf-8").splitlines()) < 100

    from ai_flywheel.core import orchestrator as facade
    from ai_flywheel.core.pipeline import constants, coordinator, events, git_commit

    assert facade.WorkflowOrchestrator is coordinator.WorkflowOrchestrator
    assert facade.EventBroker is events.EventBroker
    assert facade.NODE_TEMPLATES is constants.NODE_TEMPLATES
    assert facade.PLAN_SOURCE_LABELS is constants.PLAN_SOURCE_LABELS
    assert facade.REVIEW_SOURCE_LABELS is constants.REVIEW_SOURCE_LABELS
    assert facade.WorkflowLockError is constants.WorkflowLockError
    assert facade.ApprovalLockBusy is constants.ApprovalLockBusy
    assert facade.LockLostError is constants.LockLostError
    assert facade.commit_p04_written_files is git_commit.commit_p04_written_files


def test_a_facade_monkeypatch_of_build_runner_still_reaches_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The historical seam ``core.orchestrator.build_runner`` keeps its meaning."""
    rt = _runtime(tmp_path, auto_git_commit=False)
    _register(rt)

    def _boom(cfg: Any, fs_store: Any) -> Any:
        raise RuntimeError("sentinel-build-runner")

    from ai_flywheel.core import orchestrator as facade

    monkeypatch.setattr(facade, "build_runner", _boom)
    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"])

    assert run.status == RunStatus.FAILED
    p02 = _node(run, "P02_REVIEW")
    assert "sentinel-build-runner" in (p02.error or "")


# ---------------------------------------------------------------- 2. default off


def test_auto_git_commit_defaults_to_false_and_is_documented_in_the_example_config():
    from ai_flywheel.config import KnowledgeSinkConfig

    assert KnowledgeSinkConfig().auto_git_commit is False
    example = (REPO_ROOT / "flywheel.yaml.example").read_text(encoding="utf-8")
    assert "auto_git_commit: false" in example
    assert "git add -- <paths>" in example
    assert "is ever pushed" in example


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_auto_git_commit_off_never_creates_a_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "--", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "init")
    assert _commit_count(repo) == 1

    rt = _runtime(repo, auto_git_commit=False)
    run = _run_batch(rt, monkeypatch)

    assert run.status == RunStatus.PASS, run.error
    assert (repo / DOC_RELATIVE).is_file()
    assert _commit_count(repo) == 1, "默认关闭时 P04 不得产生任何提交"
    assert "git_commit" not in (_node(run, "P04_WRITE_VERIFY").payload or {})
    assert not [e for e in _audit_events(rt, run.run_id) if e.startswith("p04_git_commit")]
    # 写出的文档留在工作区，没有被暂存。
    status = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
    assert f"?? {DOC_RELATIVE}" in status


# ---------------------------------------------------------------- 3. opt-in commit


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_auto_git_commit_commits_only_the_p04_written_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    notes = repo / "notes"
    notes.mkdir()
    (notes / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "--", "notes/tracked.txt")
    _git(repo, "commit", "-q", "-m", "init")
    # 两个干扰项：一个已跟踪文件被修改，一个未跟踪文件；提交都不得碰到它们。
    (notes / "tracked.txt").write_text("v2\n", encoding="utf-8")
    (notes / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    assert _commit_count(repo) == 1

    rt = _runtime(repo, auto_git_commit=True)
    run = _run_batch(rt, monkeypatch)

    assert run.status == RunStatus.PASS, run.error
    assert (repo / DOC_RELATIVE).is_file()

    # 恰好一个提交，且只包含 P04 写出的那篇文档。
    assert _commit_count(repo) == 2
    assert _head_files(repo) == [DOC_RELATIVE]
    message = _git(repo, "log", "-1", "--format=%B").stdout
    assert run.run_id in message
    assert "approval_source: auto" in message
    assert DOC_RELATIVE in message
    # 配置了身份的仓库保留自己的身份，不被回退身份覆盖。
    assert _git(repo, "log", "-1", "--format=%an").stdout.strip() == "Flywheel Test"

    # 干扰项原样保留：已跟踪文件仍是未提交修改，未跟踪文件仍然未跟踪。
    status = _git(repo, "status", "--porcelain").stdout
    assert " M notes/tracked.txt" in status
    assert "?? notes/unrelated.txt" in status

    # 节点载荷与审计事件都记录了这次提交。
    payload = _node(run, "P04_WRITE_VERIFY").payload or {}
    assert payload["git_commit"]["status"] == "committed"
    assert payload["git_commit"]["commits"][0]["files"] == [DOC_RELATIVE]
    events = _audit_events(rt, run.run_id)
    assert "p04_git_commit" in events
    assert events["p04_git_commit"]["payload"]["status"] == "committed"
    assert events["p04_git_commit"]["payload"]["commits"][0]["commit"]


# ---------------------------------------------------------------- 3b. approval source


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_the_commit_records_the_source_of_the_approval_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """审批来源取自批准认领事务的审计事件，而不是调用方随手传入的字符串。"""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "--", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "init")

    rt = _runtime(repo, auto_git_commit=True)
    rt.cfg.workflow.approval_mode = "always"
    _register(rt)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel())
    paused = rt.orchestrator.start_run(
        trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=False
    )
    assert paused.status == RunStatus.AWAITING_APPROVAL

    done = rt.orchestrator.approve_and_resume(paused.run_id, source="feishu")

    assert done.status == RunStatus.PASS, done.error
    message = _git(repo, "log", "-1", "--format=%B").stdout
    assert paused.run_id in message
    assert "approval_source: feishu" in message


# ---------------------------------------------------------------- 4. outside a repo

@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_auto_git_commit_outside_a_repository_is_audited_and_does_not_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if _is_git_repo(tmp_path):
        pytest.skip("the temporary directory is inside a git repository")
    rt = _runtime(tmp_path, auto_git_commit=True)
    run = _run_batch(rt, monkeypatch)

    assert run.status == RunStatus.PASS, run.error
    payload = _node(run, "P04_WRITE_VERIFY").payload or {}
    assert payload["git_commit"]["status"] == "skipped"
    assert payload["git_commit"]["reason"] == "not_a_git_repo"
    events = _audit_events(rt, run.run_id)
    assert "p04_git_commit_skipped" in events
    assert "p04_git_commit" not in events


# ---------------------------------------------------------------- helper contract


def test_the_git_helper_never_pushes():
    source = (REPO_ROOT / "backend/ai_flywheel/core/pipeline/git_commit.py").read_text(encoding="utf-8")
    assert '"push"' not in source and "'push'" not in source


def test_the_git_helper_skips_an_empty_write_set(tmp_path: Path):
    from ai_flywheel.core.pipeline.git_commit import commit_p04_written_files

    result = commit_p04_written_files([], run_id="run-empty")
    assert result["status"] == "skipped"
    assert result["reason"] == "nothing_to_commit"
    assert result["commits"] == []
