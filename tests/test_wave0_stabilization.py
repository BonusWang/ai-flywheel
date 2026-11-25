"""Wave 0（P0 稳定化）回归测试：启动引导、路径与行尾、SQLite 并发、门禁与公开性。

覆盖：
- W0.1 启动引导：默认初始化 FTS、登记本地 Skill、生成 FLYWHEEL-CORE.md；skip_fts_sync=True 时全部跳过。
- W0.2 Windows 路径分段与 LF 行尾：反斜杠工作区按目录精确匹配；写入永远是 LF；BOM 显式识别。
- W0.3 SQLite 并发：WAL + busy_timeout + BEGIN IMMEDIATE 下并发认领只有一个赢家，且没有 "database is locked"。
- W0.5 门禁与公开性：bash 解析、Hook 只读判定、读侧护栏、模型错误脱敏。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest

from ai_flywheel.adapters.knowledge import KnowledgeWriteError
from ai_flywheel.adapters.runners import OpenAICompatibleRunner
from ai_flywheel.adapters.wiki_gate import (
    WikiGate,
    resolve_bash_executable,
    split_frontmatter,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import (
    NodeExecution,
    NodeStatus,
    PlannedWrite,
    RunStatus,
    WorkflowRunRecord,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import ImmutableFileStore

REPO_ROOT = Path(__file__).resolve().parent.parent

WIKI_SCHEMA = """---
title: Test Wiki Schema
description: Minimal schema for the wave-0 tests
type: protocol
created: 2026-01-01
updated: 2026-01-01
status: active
load_policy: always
core_contract:
  version: "2.1"
  core_files:
    - .wiki-schema.md
  required_fields: [title, description, type, created, updated]
partition_contract:
  version: "1.0"
  partitions:
    projects:
      root: wiki-知识库/projects-项目
content_contract:
  version: "1.4.1"
  legacy_type_aliases: {}
  page_types:
    project-practice-index:
      partition: projects
      required_fields: [title, description, type, project_id, created, updated, status]
      path_pattern: "^[^/]+/实战笔记/笔记索引\\\\.md$"
      allowed_status: [active]
    project-practice-note:
      partition: projects
      required_fields: [title, description, type, project_id, record_id, event_date, load_policy, default_load, created, updated, status]
      path_pattern: "^[^/]+/实战笔记/[^/]+\\\\.md$"
      allowed_status: [active]
---
"""


# ---------------- W0.1 启动引导 ----------------

def _runtime(tmp_path: Path, *, skip_fts_sync: bool = False) -> FlywheelRuntime:
    cfg = FlywheelConfig(base_dir=tmp_path)
    skills = tmp_path / "skills"
    skill_dir = skills / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: 演示技能\n---\n\n正文\n", encoding="utf-8"
    )
    cfg.capabilities.skills_dirs = [str(skills)]
    return FlywheelRuntime(cfg, skip_fts_sync=skip_fts_sync)


def _seed_lesson(tmp_path: Path) -> None:
    lesson = tmp_path / "data" / "knowledge" / "lessons"
    lesson.mkdir(parents=True)
    (lesson / "cache.md").write_text(
        "---\ntitle: 缓存雪崩定位\ndescription: 命中率骤降的排查\n---\n\n缓存雪崩窗口\n",
        encoding="utf-8",
    )


def test_startup_bootstraps_fts_skills_and_core_card(tmp_path: Path):
    _seed_lesson(tmp_path)
    rt = _runtime(tmp_path)

    # FTS 索引已建立并可检索
    hits = rt.db.search_knowledge_fts("缓存雪崩", limit=5)
    assert any("cache.md" in hit["doc_path"] for hit in hits)

    # 探测到的本地 Skill 已登记
    assets = {row["skill_name"] for row in rt.db.list_skill_assets()}
    assert "demo-skill" in assets

    # 核心卡已生成
    core = rt.cfg.data_path / "FLYWHEEL-CORE.md"
    assert core.is_file()
    assert core.read_text(encoding="utf-8").strip()


def test_startup_can_skip_the_bootstrap(tmp_path: Path):
    _seed_lesson(tmp_path)
    rt = _runtime(tmp_path, skip_fts_sync=True)

    assert rt.db.search_knowledge_fts("缓存雪崩", limit=5) == []
    assert rt.db.list_skill_assets() == []
    assert not (rt.cfg.data_path / "FLYWHEEL-CORE.md").exists()


# ---------------- W0.2 Windows 路径分段与 LF 行尾 ----------------

def _wiki_runtime(tmp_path: Path) -> FlywheelRuntime:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "wiki-知识库" / "projects-项目").mkdir(parents=True)
    (wiki_root / ".wiki-schema.md").write_text(WIKI_SCHEMA, encoding="utf-8")
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(wiki_root)
    cfg.knowledge_sink.allowed_write_prefixes = ["wiki-知识库/"]
    return FlywheelRuntime(cfg)


def _add_project(wiki_root: Path, dir_name: str, project_id: str) -> None:
    practice = wiki_root / "wiki-知识库" / "projects-项目" / dir_name / "实战笔记"
    practice.mkdir(parents=True)
    (practice / "笔记索引.md").write_text(
        f"---\ntitle: {dir_name} 索引\ndescription: {dir_name} 实战经验卡索引\n"
        f"type: project-practice-index\nproject_id: {project_id}\n"
        "created: 2026-11-23\nupdated: 2026-11-23\nstatus: active\n---\n\n"
        f"# {dir_name} 索引\n",
        encoding="utf-8",
    )


DRAFT = "---\ntitle: 受控写入门禁排查\ntags: [wiki]\n---\n\n# 受控写入门禁排查\n- 草稿必须在 preflight 前定稿。\n"


def test_windows_workspace_path_splits_on_backslashes_and_prefers_the_exact_folder(tmp_path: Path):
    rt = _wiki_runtime(tmp_path)
    _add_project(rt.knowledge.root, "alpha-crm-Alpha客户平台", "alpha-crm")
    _add_project(rt.knowledge.root, "alpha-crm-tools-工具", "alpha-crm-tools")
    _add_project(rt.knowledge.root, "core-hub-Core中枢", "core-hub")
    gate = rt.knowledge.wiki_gate

    # 反斜杠路径必须按目录分段：否则整个路径只算一段，精确匹配失效。
    decision = gate.evaluate_route(
        relative_path="",
        title="受控写入门禁排查",
        summary="",
        content=DRAFT,
        source_workspaces=["X:\\demo\\alpha-crm-tools"],
    )
    assert decision.routed is True
    assert decision.project_dir == "alpha-crm-tools-工具"
    # 精确命中的目录排在包含式命中之前
    assert decision.evidence["workspace"][0] == "alpha-crm-tools-工具"

    # 正斜杠路径得到同样的结论
    posix = gate.evaluate_route(
        relative_path="",
        title="受控写入门禁排查",
        summary="",
        content=DRAFT,
        source_workspaces=["X:/demo/alpha-crm-tools"],
    )
    assert posix.project_dir == "alpha-crm-tools-工具"

    # 声明与工作区冲突时按工作区证据判冲突
    conflict = gate.evaluate_route(
        relative_path="",
        title="受控写入门禁排查",
        summary="",
        content=DRAFT,
        wiki_route={"page_type": "project-practice-note", "project": "core-hub"},
        source_workspaces=["X:\\demo\\alpha-crm-tools"],
    )
    assert conflict.routed is False
    assert conflict.reason_code == "workspace_mismatch"


def test_bom_is_detected_explicitly(tmp_path: Path):
    rt = _wiki_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate
    bom = chr(0xFEFF)

    # BOM 不会被当成 frontmatter 的一部分
    fm_text, body = split_frontmatter(bom + "---\ntitle: x\n---\n\n正文\n")
    assert fm_text is not None and "title: x" in fm_text

    # 门禁把 BOM 当作错误报出来
    result = gate.validate_document("wiki-知识库/x.md", bom + "---\ntitle: x\n---\n\n正文\n")
    codes = [issue["code"] for issue in result.issues]
    assert "BOM_PRESENT" in codes


def test_write_normalizes_crlf_to_lf(tmp_path: Path):
    # 不挂载 Schema 的知识库：这一条只关心写入路径的行尾。
    cfg = FlywheelConfig(base_dir=tmp_path)
    rt = FlywheelRuntime(cfg)
    rel = "lessons/crlf.md"
    content = "---\ntitle: 行尾\r\n---\r\n\r\n第一行\r\n第二行\r\n"
    rt.knowledge.write_document_verified(rel, content)

    raw = (rt.knowledge.root / rel).read_bytes()
    assert b"\r\n" not in raw
    assert b"\r" not in raw
    assert raw.count(b"\n") == 6

    # 再写一次同样的内容：行尾归一后不得再被判成"有变化"
    with pytest.raises(KnowledgeWriteError):
        rt.knowledge.write_document_verified(rel, content, action="update")


# ---------------- W0.3 SQLite 并发 ----------------

def _awaiting_run(run_id: str = "run-conc") -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=run_id,
        trigger="test",
        approval_mode="always",
        status=RunStatus.AWAITING_APPROVAL,
        batch_date="2026-11-24",
        batch_seq=1,
        planned_writes=[
            PlannedWrite(target_kind="knowledge_doc", action="create", title="t0", summary="s0"),
            PlannedWrite(target_kind="knowledge_doc", action="create", title="t1", summary="s1"),
        ],
        nodes=[
            NodeExecution(node_id=f"P0{i}", name=f"P0{i}", actor="x", status=NodeStatus.PASS)
            for i in range(8)
        ],
    )


def _seed_awaiting_run(db: FlywheelDB, run_id: str = "run-conc") -> None:
    db.save_workflow_run(_awaiting_run(run_id))
    db.save_decision_card(
        card_id=f"card-{run_id}",
        run_id=run_id,
        kind="ask",
        state="open",
        title="t",
        body_markdown="b",
        options=["同意并写入", "取消本次写入"],
    )


def test_sqlite_is_configured_for_concurrency(tmp_path: Path):
    db = FlywheelDB(tmp_path / "flywheel.db")
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_concurrent_claim_and_reject_have_exactly_one_winner(tmp_path: Path):
    db = FlywheelDB(tmp_path / "flywheel.db")
    _seed_awaiting_run(db)
    assert db.acquire_workflow_lock("run-conc") is True

    errors: list[BaseException] = []
    approvals: list[bool] = []
    rejections: list[bool] = []
    renewals: list[bool] = []
    barrier = threading.Barrier(8, timeout=60)

    def _approve() -> None:
        barrier.wait()
        try:
            res = db.claim_approval(
                "run-conc", approved_mask=[True, True], selected_option="同意并写入", source="test"
            )
            approvals.append(bool(res.get("claimed")))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _reject() -> None:
        barrier.wait()
        try:
            rejections.append(
                bool(
                    db.claim_rejection(
                        "run-conc",
                        reason="取消",
                        source="test",
                        nodes_json="[]",
                        digest_markdown="",
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _renew() -> None:
        barrier.wait()
        try:
            renewals.append(bool(db.renew_workflow_lock("run-conc")))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_approve),
        threading.Thread(target=_approve),
        threading.Thread(target=_approve),
        threading.Thread(target=_reject),
        threading.Thread(target=_reject),
        threading.Thread(target=_reject),
        threading.Thread(target=_renew),
        threading.Thread(target=_renew),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
        assert not t.is_alive()

    assert not errors, errors
    assert not any(isinstance(exc, sqlite3.OperationalError) for exc in errors)
    # 批准与取消合计只有一个赢家
    assert sum(approvals) + sum(rejections) == 1
    assert len(approvals) == 3 and len(rejections) == 3
    # 运行落到唯一那个终态上
    run = db.get_workflow_run("run-conc")
    assert run.status in (RunStatus.RUNNING, RunStatus.CANCELLED)


def test_concurrent_lock_acquire_has_exactly_one_winner(tmp_path: Path):
    db = FlywheelDB(tmp_path / "flywheel.db")
    winners: list[bool] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8, timeout=60)

    def _worker(index: int) -> None:
        barrier.wait()
        try:
            winners.append(bool(db.acquire_workflow_lock(f"run-{index}")))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, errors
    assert sum(winners) == 1


# ---------------- W0.5 门禁与公开性 ----------------

def _runtime_with_profile(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    return FlywheelRuntime(FlywheelConfig(base_dir=tmp_path))


def _retry_counter_count(rt: FlywheelRuntime) -> int:
    with rt.db.connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM retry_counters").fetchone()[0])


def _pre_tool(rt: FlywheelRuntime, tool_name: str, tool_input: dict) -> dict:
    return rt.hooks.handle(
        "claude-code",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "turn_id": "t1",
            "tool_name": tool_name,
            "tool_input": tool_input,
        },
    )


def test_hooks_treat_apply_patch_as_a_mutation(tmp_path: Path):
    rt = _runtime_with_profile(tmp_path)
    out = _pre_tool(rt, "apply_patch", {"command": "apply_patch <<EOF"})
    assert out["continue"] is True
    # 写工具不再被当作只读工具直接放行：它进入风险与死循环检查
    assert _retry_counter_count(rt) == 1


def test_hooks_still_skip_readonly_tools(tmp_path: Path):
    rt = _runtime_with_profile(tmp_path)
    out = _pre_tool(rt, "Read", {"file_path": "x.md"})
    assert out["continue"] is True
    assert _retry_counter_count(rt) == 0


def test_hooks_read_the_command_out_of_a_dict_tool_input(tmp_path: Path):
    rt = _runtime_with_profile(tmp_path)
    read_only = _pre_tool(rt, "Bash", {"command": "git status"})
    assert read_only["continue"] is True
    # 只读命令直接放行：不进入风险检查
    assert _retry_counter_count(rt) == 0

    risky = _pre_tool(rt, "Bash", {"command": "rm -rf /tmp/x"})
    assert "hookSpecificOutput" in risky
    assert "高危" in json.dumps(risky, ensure_ascii=False)


def test_read_document_refuses_excluded_schema_and_sensitive_pages(tmp_path: Path):
    rt = _wiki_runtime(tmp_path)
    root = rt.knowledge.root
    excluded = root / "raw-原始素材" / "x.md"
    excluded.parent.mkdir(parents=True)
    excluded.write_text("---\ntitle: 素材\n---\n\n正文\n", encoding="utf-8")
    sensitive = root / "wiki-知识库" / "secret.md"
    sensitive.parent.mkdir(parents=True, exist_ok=True)
    sensitive.write_text(
        "---\ntitle: 凭据\nsensitivity: private_sensitive\n---\n\n密码\n", encoding="utf-8"
    )
    normal = root / "wiki-知识库" / "ok.md"
    normal.write_text("---\ntitle: 正常\n---\n\n正文\n", encoding="utf-8")

    assert rt.knowledge.read_document("raw-原始素材/x.md") is None
    assert rt.knowledge.read_document(".wiki-schema.md") is None
    assert rt.knowledge.read_document("wiki-知识库/secret.md") is None
    assert rt.knowledge.read_document("wiki-知识库/ok.md") is not None


def test_model_api_http_error_is_sanitized(tmp_path: Path):
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.api.base_url = "https://models.internal.example/v1"
    cfg.runner.api.model = "demo-model"
    runner = OpenAICompatibleRunner(cfg, ImmutableFileStore(cfg.receipts_dir))

    def _boom(*args, **kwargs):
        request = httpx.Request("POST", "https://models.internal.example/v1/chat/completions")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("server error", request=request, response=response)

    original = httpx.post
    httpx.post = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError) as exc:
            runner._call_chat_completion("prompt", "system")
    finally:
        httpx.post = original  # type: ignore[assignment]

    message = str(exc.value)
    assert "HTTP 503" in message
    assert "models.internal.example" not in message
    assert "chat/completions" not in message


def _wiki_package_source() -> str:
    """The source of the Wiki gate package (the gate itself moved into a package)."""
    package = REPO_ROOT / "backend" / "ai_flywheel" / "adapters" / "wiki"
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py"))
    )


def test_public_repo_has_no_personal_paths_in_the_workflow_root_env():
    """公开仓库不再把私有的工作根环境变量名写死在代码里。"""
    source = _wiki_package_source()
    assert "AI_HUB_WORK_ROOT" not in source
    assert "FLYWHEEL_WORK_ROOT" in source


def test_bash_helper_is_used_by_the_postflight_paths():
    """D2 脚本用的是解析后的 bash，而不是裸的 'bash'。"""
    source = (REPO_ROOT / "backend" / "ai_flywheel" / "adapters" / "wiki" / "postflight.py").read_text(
        encoding="utf-8"
    )
    assert source.count("resolve_bash_executable(") >= 3
    assert 'self.sink.wiki_bash or "bash"' not in source


def test_resolve_bash_executable_keeps_explicit_commands():
    assert resolve_bash_executable("/usr/local/bin/bash") == "/usr/local/bin/bash"
    assert resolve_bash_executable("C:/tools/bash.exe") == "C:/tools/bash.exe"
    resolved = resolve_bash_executable("bash")
    assert resolved
    if Path(resolved).is_absolute():
        assert Path(resolved).is_file()


def test_wiki_gate_instance_is_reachable_from_the_runtime(tmp_path: Path):
    rt = _wiki_runtime(tmp_path)
    assert isinstance(rt.knowledge.wiki_gate, WikiGate)
