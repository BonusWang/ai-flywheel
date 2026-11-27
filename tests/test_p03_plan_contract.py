"""P03 写入计划契约（fix-p03-silent-plan-fallback）。

夹具按真实缓存输出 `p03_plan-*.json` 的形状脱敏构造：改写提示词之后的输出普遍没有 `summary`，
更早的两个批次连 `title` 也没有；规则项只有 `rule_payload`，Skill 项带 `skill_route` 与 frontmatter。
内容全部是虚构示例，不含任何真实会话、项目或技能名。

最后一节覆盖挂载 Wiki 声明 `content_contract.deposit_routing` 时的 P03 输入与领域页计划项
（feat-project-domain-incremental-deposit）：示例项目 alpha 的 `orders` 表结构页与 `database` 配置页。

再往后的几节覆盖文档深度、事件分卡与领域页独立预算（feat-agy-ingest-and-high-fidelity-distill）：
知识文档不再限 220-450 字、审查的 `events` 进入计划提示词、`max_domain_writes_per_run` 单独计数，
以及事故卡在计划规范化时就被标记为敏感写入。同样只用虚构示例，不发出任何真实模型请求。

「缓存与隐私门禁」一节覆盖回包落盘的那条线：隐私门禁拒绝的计划项仍在整份回包里，所以这样的回包
在 API 与 CLI 两条通道上都不写缓存；干净回包照常缓存。合成密钥在运行时拼出，文件里不出现完整形状。
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai_flywheel.adapters.runners import (
    P03_ITEM_REQUIRED_KEYS,
    P03_REPAIR_SCHEMA_HINT,
    P03_REPAIR_SCHEMA_HINT_WITH_ROUTE,
    PLAN_SOURCE_CLI,
    PLAN_SOURCE_HEURISTIC,
    PLAN_SOURCE_MODEL,
    CliSubprocessRunner,
    DryRunHeuristicRunner,
    OpenAICompatibleRunner,
    PlanDiagnostics,
    PlanUnavailableError,
    _build_p03_prompt,
    _plan_from_items,
    _plan_from_runner_output,
    build_runner,
    deposit_page_types,
)
from ai_flywheel.adapters.runners.clients import _plan_write_caps
from ai_flywheel.adapters.wiki.router import INCIDENT_SECTIONS, IncidentRouting, write_prefix_allows
from ai_flywheel.adapters.wiki_gate import RoutingCandidates, WikiGate, page_section_sketch, preserves_existing
from ai_flywheel.config import FlywheelConfig, WorkflowConfig
from ai_flywheel.core.pipeline.helpers import domain_signal_names
from ai_flywheel.models import (
    NodeExecution,
    NodeStatus,
    RegisterSessionRequest,
    RunStatus,
    SessionStatus,
    WorkflowRunRecord,
    is_sensitive_write,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.fs_store import ImmutableFileStore

REPO_ROOT = Path(__file__).resolve().parents[1]

NO_FINDINGS = {"findings": [], "eligible_promotions": [], "repeated_workflows": []}

RUN_LOGGER = "ai_flywheel"

DOWNSTREAM_NODES = ("P04_WRITE_VERIFY", "P05_SETTLE", "P06_REPORT", "P07_DELIVER")


# ---------------------------------------------------------------- fixtures shaped like the cached outputs


def _doc(title: str = "接口超时排查复盘", *, slug: str = "api-timeout", with_summary: bool = False) -> dict[str, Any]:
    """No `summary`; the draft opens with frontmatter, an H1 and an emphasised list item."""
    item: dict[str, Any] = {
        "target_kind": "knowledge_doc",
        "title": title,
        "relative_path": f"lessons/2026-11-24-{slug}.md",
        "action": "create",
        "content_preview": (
            "---\ntitle: 接口超时排查复盘\nfreshness_tier: evolving\nlast_verified: 2026-11-24\nvalid_until: 2027-02-22\n---\n\n"
            f"# {title}\n\n## 事实回顾\n- **接口**在高峰期出现超时，根因是连接池上限过小。\n"
        ),
    }
    if with_summary:
        item["summary"] = "记录接口超时的根因与修复方式"
    return item


def _rule_titleless() -> dict[str, Any]:
    """Neither `title` nor `summary`: everything sits in `rule_payload`."""
    return {
        "target_kind": "flywheel_rule",
        "relative_path": None,
        "action": "register_rule",
        "rule_payload": {
            "title": "改配置前先核对生效范围",
            "domain": "engineering",
            "projects": ["generic"],
            "triggers": ["配置", "config"],
            "checklist": ["确认配置只作用于目标环境", "变更后回读实际生效值"],
            "forbidden_routes": ["直接改全局配置"],
        },
    }


def _skill(
    slug: str = "release-checklist", *, action: str = "update", heading: bool = True, with_summary: bool = False
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "target_kind": "skill_improvement",
        "relative_path": f"{slug}/SKILL.md",
        "action": action,
        "requires_approval": True,
        "approval_reason": "每次发版都要重复同样的检查步骤",
        "skill_route": {
            "hit": True,
            "action": action,
            "target_skill": slug,
            "reason": "固定步骤反复执行，值得沉淀为 Skill",
        },
        "content_preview": (
            f"---\nname: {slug}\ndescription: 发版前逐项检查版本号与构建产物\n---\n\n"
            + ("# 发版检查清单\n\n" if heading else "")
            + "## 执行步骤\n1. 核对版本号与变更记录。\n"
        ),
    }
    if with_summary:
        item["summary"] = "把发版前的固定检查步骤沉淀为 Skill"
    return item


_P02_FINDING = {
    "session_key": "codex:plan-1",
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


class _FakeModel:
    """Stands in for the model gateway: P02 gets a canned review, P03 a canned plan or raw text."""

    def __init__(self, planned_writes: list[Any] | None, *, p03_raw: str | None = None) -> None:
        self.plan_json = "" if planned_writes is None else json.dumps({"planned_writes": planned_writes}, ensure_ascii=False)
        self.p03_raw = p03_raw
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("You are the P02 Review Worker"):
            # One finding for the session under test: a review without any finding fails P02.
            return json.dumps({"findings": [_P02_FINDING], "mistakes": [], "dissents": [], "repeated_workflows": []}, ensure_ascii=False)
        if prompt.startswith("The previous output was malformed JSON"):
            return self.plan_json
        return self.plan_json if self.p03_raw is None else self.p03_raw


def _runtime(tmp_path: Path, *, mode: str = "api", approval_mode: str = "auto") -> tuple[FlywheelRuntime, Path]:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills_dir)]
    cfg.workflow.approval_mode = approval_mode
    # `auto` only takes effect with the safety valve open: the valve has the
    # last word, so a closed valve would run the batch as `smart`.
    cfg.safety_valves.auto_approve_writes = approval_mode == "auto"
    cfg.runner.mode = mode
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    return FlywheelRuntime(cfg), skills_dir


def _existing_skill(skills_dir: Path, slug: str) -> None:
    target = skills_dir / slug / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        f"---\nname: {slug}\ndescription: 人工维护的现有 Skill\n---\n\n# 现有 Skill\n\n"
        "人工维护的正文内容：这一段用于验证更新时原有内容逐字节保留。\n",
        encoding="utf-8",
    )


def _register(rt: FlywheelRuntime, session_id: str = "plan-1") -> None:
    rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id=session_id,
            title="接口超时排查",
            project="demo",
            summary="记录接口超时的根因",
            content="用户：接口超时怎么排查。助手：先看连接池上限。",
        )
    )


def _node(run: WorkflowRunRecord, node_id: str) -> NodeExecution:
    return next(n for n in run.nodes if n.node_id == node_id)


def _event_types(rt: FlywheelRuntime, run: WorkflowRunRecord) -> list[str]:
    return [e["event_type"] for e in rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id, limit=50)]


def _payload_of(rt: FlywheelRuntime, run: WorkflowRunRecord, event_type: str) -> dict[str, Any]:
    return next(e for e in rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id, limit=50) if e["event_type"] == event_type)["payload"]


@pytest.fixture()
def heuristic_planner_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """In api/cli mode the heuristic planner must never produce the write plan."""

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the heuristic planner must not be used in api/cli mode")

    monkeypatch.setattr(DryRunHeuristicRunner, "plan_writes", _forbidden)
    monkeypatch.setattr(DryRunHeuristicRunner, "plan_writes_detailed", _forbidden)


# ---------------------------------------------------------------- baseline (current behaviour kept)


def test_baseline_runner_selection_default_path_and_prompt_inputs(tmp_path: Path):
    # api mode without a model never reaches the network
    unconfigured = FlywheelConfig()
    unconfigured.runner.mode = "api"
    assert isinstance(build_runner(unconfigured, ImmutableFileStore(tmp_path / "receipts")), DryRunHeuristicRunner)

    # a knowledge_doc without a path gets the default-prefixed path
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    doc = _doc(with_summary=True)
    del doc["relative_path"]
    runner._call_chat_completion = _FakeModel([doc])
    (planned,) = runner.plan_writes_detailed(NO_FINDINGS, [], 8).planned_writes
    assert planned.relative_path is not None
    assert planned.relative_path.startswith("lessons/") and planned.relative_path.endswith("-接口超时排查复盘.md")

    prompt = _build_p03_prompt(
        {
            "findings": [{"session_key": "codex:s-1", "worthy": True, "title": "接口超时排查", "facts": ["连接池上限过小"]}],
            "eligible_promotions": [],
            "repeated_workflows": [],
        },
        [],
        [{"name": "release-checklist", "description": "发版检查"}],
        5,
        "lessons/",
        "daily",
    )
    payload = json.loads(prompt.rsplit("\n\n", 1)[1])
    assert set(payload) == {
        "phase",
        "findings",
        "eligible_promotions",
        "repeated_workflows",
        "existing_rules",
        "existing_skill_names",
        "existing_skill_sections",
    }
    assert payload["existing_skill_names"] == ["release-checklist"]
    # 没有重复流程候选时就没有要对照的现有 Skill
    assert payload["existing_skill_sections"] == {}


# ---------------------------------------------------------------- field completion


def test_cached_shapes_missing_fields_are_completed_not_dropped(tmp_path: Path):
    rt, skills_dir = _runtime(tmp_path)
    _existing_skill(skills_dir, "release-checklist")
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel(
        [
            _doc(),  # no summary
            _doc("发布流程复盘", slug="release"),  # no summary
            _rule_titleless(),  # no title, no summary
            _skill("release-checklist", heading=False),  # no title, no summary, no H1
            _skill("incident-drill", action="create", with_summary=True),  # no title
        ]
    )

    outcome = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert [w.target_kind for w in outcome.planned_writes] == [
        "knowledge_doc",
        "knowledge_doc",
        "flywheel_rule",
        "skill_improvement",
        "skill_improvement",
    ]
    diag = outcome.diagnostics
    assert diag.source == PLAN_SOURCE_MODEL
    assert (diag.returned_count, diag.kept_count) == (5, 5)
    assert diag.dropped_items == []
    assert diag.completed_fields == {"summary": 4, "title": 3}
    # the first sentence loses the list marker and the emphasis marks
    assert outcome.planned_writes[0].summary == "接口在高峰期出现超时，根因是连接池上限过小"
    assert outcome.planned_writes[2].title == "改配置前先核对生效范围"
    assert outcome.planned_writes[2].summary == "确认配置只作用于目标环境"
    assert outcome.planned_writes[3].title == "release-checklist"
    assert outcome.planned_writes[3].summary == "发版前逐项检查版本号与构建产物"
    assert diag.missing_source_session_keys == 5
    # an update on an existing Skill keeps the model's reason and states what the writer does
    existing_lines = len(
        (skills_dir / "release-checklist" / "SKILL.md").read_text(encoding="utf-8").splitlines()
    )
    assert outcome.planned_writes[3].approval_reason == (
        "每次发版都要重复同样的检查步骤；"
        f"在现有文件末尾追加小节，原有 {existing_lines} 行完整保留，原文件自动备份"
    )
    # 更新项的预览就是将要追加的那一小节
    assert outcome.planned_writes[3].content_preview.startswith("## 执行步骤")


def test_titleless_skill_takes_its_heading_and_body_first_sentence_has_no_markup(tmp_path: Path):
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel([_skill("incident-drill", action="create")])
    (skill,) = runner.plan_writes_detailed(NO_FINDINGS, [], 8).planned_writes
    assert skill.title == "发版检查清单"
    assert skill.summary == "发版前逐项检查版本号与构建产物"


def test_item_without_any_complete_sentence_falls_back_to_title(tmp_path: Path):
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    doc = _doc(title="只有标题的草稿", slug="title-only")
    doc["content_preview"] = f"# 只有标题的草稿\n\n## 小节\n- 短句。\n"
    runner._call_chat_completion = _FakeModel([doc])
    (planned,) = runner.plan_writes_detailed(NO_FINDINGS, [], 8).planned_writes
    assert planned.summary == "只有标题的草稿"


def test_diagnostics_report_completions_and_missing_source_sessions(tmp_path: Path):
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel([_doc(slug=f"note-{i}") for i in range(5)])

    outcome = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert len(outcome.planned_writes) == 5
    assert outcome.diagnostics.source == PLAN_SOURCE_MODEL
    assert outcome.diagnostics.completed_fields == {"summary": 5}
    assert outcome.diagnostics.missing_source_session_keys == 5


def test_second_identical_call_is_served_from_cache(tmp_path: Path):
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    fake = _FakeModel([_doc(with_summary=True)])
    runner._call_chat_completion = fake

    first = runner.plan_writes_detailed(NO_FINDINGS, [], 8)
    second = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert [w.relative_path for w in first.planned_writes] == [w.relative_path for w in second.planned_writes]
    assert len(fake.prompts) == 1
    assert second.diagnostics.cache_hit is True and first.diagnostics.cache_hit is False
    assert second.diagnostics.cache_file and second.diagnostics.cache_file.startswith("p03_plan-")


# ---------------------------------------------------------------- drops and traces


def test_unsupported_and_empty_items_are_dropped_with_reasons_and_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    empty_doc = _doc(title="空草稿", slug="empty")
    empty_doc["content_preview"] = ""
    empty_skill = _skill("hollow-skill", action="create", with_summary=True)
    empty_skill["content_preview"] = ""
    secret_body = "接口密钥泄露的正文不得进入诊断"
    runner._call_chat_completion = _FakeModel(
        [
            _doc(),
            "not an object",
            {"target_kind": "note", "action": "create", "title": "未知类型", "content_preview": secret_body},
            empty_doc,
            empty_skill,
            {"target_kind": "knowledge_doc", "action": "create"},  # nothing to complete title / summary from
        ]
    )

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        outcome = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert len(outcome.planned_writes) == 1
    diag = outcome.diagnostics
    assert (diag.returned_count, diag.kept_count) == (6, 1)
    assert [(d["index"], d["reasons"]) for d in diag.dropped_items] == [
        (1, ["NOT_AN_OBJECT"]),
        (2, ["UNSUPPORTED_TARGET_KIND: note"]),
        (3, ["EMPTY_CONTENT"]),
        (4, ["EMPTY_CONTENT"]),
        (5, ["FIELD_MISSING: title", "FIELD_MISSING: summary"]),
    ]
    # a draft without content is dropped, never replaced by template prose
    assert all(w.target_kind == "knowledge_doc" for w in outcome.planned_writes)
    assert secret_body not in json.dumps(diag.to_dict(), ensure_ascii=False)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert sum("P03 plan item dropped" in message for message in warnings) == 5


def test_items_beyond_max_write_set_are_counted(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel([_doc(slug=f"note-{i}") for i in range(4)])

    with caplog.at_level(logging.INFO, logger=RUN_LOGGER):
        outcome = runner.plan_writes_detailed(NO_FINDINGS, [], 2)

    assert len(outcome.planned_writes) == 2
    assert outcome.diagnostics.over_limit_count == 2
    assert [(d["index"], d["reasons"]) for d in outcome.diagnostics.dropped_items] == [(2, ["OVER_LIMIT"]), (3, ["OVER_LIMIT"])]
    assert sum("P03 plan item not planned" in r.getMessage() for r in caplog.records) == 2
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)  # staying within the cap is not a defect


def test_partial_drop_is_traced_in_summary_payload_audit_and_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, heuristic_planner_forbidden: None
):
    rt, _ = _runtime(tmp_path)
    _register(rt)
    items = [_doc(slug=f"note-{i}") for i in range(4)] + [{"target_kind": "note", "action": "create", "title": "未知类型"}]
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel(items))

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:plan-1"])

    assert run.status == RunStatus.PASS, run.error
    assert len(run.planned_writes) == 4
    p03 = _node(run, "P03_ROUTE_PLAN")
    assert p03.status == NodeStatus.PASS
    assert "计划来源：API 模型" in p03.summary
    assert "计划项共 5 项，1 项未列入（1 项未通过校验）" in p03.summary
    assert p03.payload["plan_source"] == "model"
    assert p03.payload["plan_diagnostics"]["dropped_items"] == [
        {"index": 4, "target_kind": "note", "action": "create", "relative_path": None, "title": "未知类型", "reasons": ["UNSUPPORTED_TARGET_KIND: note"]}
    ]
    check = next(c for c in p03.internal_checks if c["name"] == "写入计划项校验")
    assert check["status"] == "WARN"
    assert check["detail"]["completed_fields"] == {"summary": 4}
    assert _event_types(rt, run).count("plan_items_dropped") == 1
    assert _payload_of(rt, run, "plan_items_dropped")["dropped_items"][0]["reasons"] == ["UNSUPPORTED_TARGET_KIND: note"]
    assert any("planned-write item(s) not planned" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


# ---------------------------------------------------------------- all invalid: fail, never fall back


def test_all_invalid_plan_fails_p03_instead_of_falling_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, heuristic_planner_forbidden: None
):
    rt, skills_dir = _runtime(tmp_path)
    _register(rt)
    knowledge_root = rt.cfg.knowledge_root_path
    files_before = {p for p in knowledge_root.rglob("*")} if knowledge_root.exists() else set()
    items = [{"target_kind": "note", "action": "create", "title": f"笔记 {i}"} for i in range(5)]
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel(items))

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:plan-1"], auto_approve_override=True)

    assert run.status == RunStatus.FAILED
    p03 = _node(run, "P03_ROUTE_PLAN")
    assert p03.status == NodeStatus.FAILED
    assert "模型返回的写入计划 5 项全部未列入（5 项未通过校验（UNSUPPORTED_TARGET_KIND）），本批不写入" in p03.error
    # W1.2: 校验没过的计划不写缓存，错误里也就没有缓存文件可指
    assert "runner.mode 切到 dry_run" in p03.error
    assert "该输出缓存在" not in p03.error
    assert p03.payload["plan_diagnostics"]["cache_file"] is None
    assert list((tmp_path / "receipts" / "api_cache").glob("p03_plan-*.json")) == []
    assert [_node(run, node_id).status for node_id in DOWNSTREAM_NODES] == [NodeStatus.SKIPPED] * 4
    assert run.planned_writes == []
    assert [d["index"] for d in p03.payload["plan_diagnostics"]["dropped_items"]] == [0, 1, 2, 3, 4]
    assert p03.payload["plan_source"] == "model"
    assert rt.db.get_session("codex:plan-1").status == SessionStatus.PENDING
    assert run.lock_released is True
    types = _event_types(rt, run)
    assert "plan_items_dropped" in types and "run_failed" in types
    assert {p for p in knowledge_root.rglob("*")} == files_before
    assert not list(skills_dir.rglob("SKILL.md"))
    assert any("P03 write plan unavailable" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def test_unparsable_output_fails_p03(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heuristic_planner_forbidden: None):
    rt, _ = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel(None, p03_raw="这次没有返回结构化内容。"))

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:plan-1"], auto_approve_override=True)

    assert run.status == RunStatus.FAILED
    assert "未返回可解析的写入计划" in _node(run, "P03_ROUTE_PLAN").error
    assert run.planned_writes == []


def test_empty_model_plan_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heuristic_planner_forbidden: None):
    rt, _ = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel([]))

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:plan-1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    assert run.planned_writes == []
    p03 = _node(run, "P03_ROUTE_PLAN")
    assert "计划来源：API 模型" in p03.summary
    assert p03.payload["plan_source"] == "model"
    assert p03.payload["plan_diagnostics"]["returned_count"] == 0
    assert "plan_items_dropped" not in _event_types(rt, run)


def test_cli_runner_applies_the_same_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heuristic_planner_forbidden: None):
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "mock-cli"
    rt = FlywheelRuntime(cfg, skip_fts_sync=True)
    runner = CliSubprocessRunner(cfg, rt.fs_store)
    monkeypatch.setattr(runner, "_invoke_cli", lambda stage, prompt, reply_trace=None: {"planned_writes": [_doc()]})

    outcome = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert outcome.diagnostics.source == PLAN_SOURCE_CLI
    assert len(outcome.planned_writes) == 1 and outcome.diagnostics.completed_fields == {"summary": 1}

    monkeypatch.setattr(
        runner,
        "_invoke_cli",
        lambda stage, prompt, reply_trace=None: {
            "planned_writes": [{"target_kind": "note", "action": "create", "title": "未知类型", "summary": "摘要"}]
        },
    )
    with pytest.raises(PlanUnavailableError) as excinfo:
        runner.plan_writes(NO_FINDINGS, [], 8)
    assert "本地 CLI 返回的写入计划 1 项全部未列入" in str(excinfo.value)


# ---------------------------------------------------------------- the cache and the privacy gate
# A reply the privacy gate refused an item of is still one reply, and the refused item is still in it.
# The cache is a plain file on disk: such a reply must never be written there, on either channel.


# Built here, so the credential shape never appears whole in this file. It opens nothing.
SYNTHETIC_KEY = "sk-" + "9" * 32
# A credential assignment: only the strict check reads it, so it is not a reason to drop an item.
SYNTHETIC_ASSIGNMENT = "临时说明 password: Hk9-Qp31x"


def _leaky_doc(slug: str = "leaky") -> dict[str, Any]:
    """A document whose draft carries a key: the privacy gate refuses it before it is planned."""
    item = _doc(title="联调凭据复盘", slug=slug, with_summary=True)
    item["content_preview"] += f"- 联调时随手贴的密钥 {SYNTHETIC_KEY}，不要外传。\n"
    return item


class _ReplyModel:
    """Stands in for the model gateway and always answers with the same reply, whatever the prompt."""

    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = json.dumps(reply, ensure_ascii=False)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        return self.reply


def _files_under(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def _cli_runner(base: Path, reply: dict[str, Any]) -> tuple[CliSubprocessRunner, Path]:
    """A CLI runner whose "local CLI" is this interpreter: it reads the prompt, prints a canned reply.

    The second value is a log that gains one character per call, so a test can count the calls.
    """
    base.mkdir(parents=True, exist_ok=True)
    cfg = FlywheelConfig(base_dir=base)
    cfg.runner.mode = "cli"
    reply_file = base / "cli-reply.json"
    reply_file.write_text(json.dumps(reply, ensure_ascii=True), encoding="ascii")
    calls = base / "cli-calls.log"
    script = (
        "import sys, pathlib; "
        "pathlib.Path(%r).open('a', encoding='ascii').write(sys.stdin.read()[:1]); "
        "sys.stdout.write(pathlib.Path(%r).read_text(encoding='ascii'))"
    ) % (str(calls), str(reply_file))
    cfg.runner.cli.command = sys.executable
    cfg.runner.cli.args = ["-c", script]
    return CliSubprocessRunner(cfg, ImmutableFileStore(base / "cli-receipts")), calls


def _api_runner(tmp_path: Path) -> OpenAICompatibleRunner:
    """An API runner whose receipts root — and so whose `api_cache/` — sits under the test directory."""
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    assert runner.fs_store.root == tmp_path / "data" / "receipts"
    return runner


def _assert_only_the_clean_item_is_planned(outcome: PlanOutcome) -> None:
    assert [w.title for w in outcome.planned_writes] == ["接口超时排查复盘"]
    assert [(d["index"], d["reasons"]) for d in outcome.diagnostics.dropped_items] == [(1, ["SECRET_LEAK_BLOCKED"])]
    assert outcome.diagnostics.source in {PLAN_SOURCE_MODEL, PLAN_SOURCE_CLI}


def _assert_nothing_was_cached(root: Path, *outcomes: PlanOutcome) -> None:
    """No file under the receipts root, and no outcome claims a cache file or a cache hit."""
    assert _files_under(root) == []
    for outcome in outcomes:
        assert (outcome.diagnostics.cache_hit, outcome.diagnostics.cache_file) == (False, None)


MIXED_REPLY = {"planned_writes": [_doc(with_summary=True), _leaky_doc()]}


def test_a_reply_with_an_item_the_privacy_gate_refused_is_never_cached_on_the_api_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, heuristic_planner_forbidden: None
):
    runner = _api_runner(tmp_path)
    fake = _FakeModel(MIXED_REPLY["planned_writes"])
    runner._call_chat_completion = fake

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        first = runner.plan_writes_detailed(NO_FINDINGS, [], 8)
        second = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    # The mixed reply: the usable item is planned, the one the gate refused is not.
    _assert_only_the_clean_item_is_planned(first)
    assert first.diagnostics.source == PLAN_SOURCE_MODEL
    _assert_only_the_clean_item_is_planned(second)
    # The refused item is still in the reply, so the reply stayed off the disk: the identical
    # second call asks the model again instead of being served the file.
    assert len(fake.prompts) == 2
    _assert_nothing_was_cached(runner.fs_store.root, first, second)
    assert not (runner.fs_store.root / "api_cache").exists()
    # The diagnostics and the log never carry the key; the log names the kind and the file.
    assert SYNTHETIC_KEY not in json.dumps(first.diagnostics.to_dict(), ensure_ascii=False)
    assert SYNTHETIC_KEY not in caplog.text
    refusals = [r.getMessage() for r in caplog.records if "Reply not cached" in r.getMessage()]
    assert len(refusals) == 2  # one per call
    assert all(
        message.startswith("Reply not cached (p03_plan-") and message.endswith(
            "): a credential shape is in the reply (OpenAI/Anthropic API Key). "
            "The next identical batch asks again."
        )
        for message in refusals
    )


def test_a_reply_with_an_item_the_privacy_gate_refused_is_never_cached_on_the_cli_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, heuristic_planner_forbidden: None
):
    runner, calls = _cli_runner(tmp_path, MIXED_REPLY)

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        first = runner.plan_writes_detailed(NO_FINDINGS, [], 8)
        second = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    _assert_only_the_clean_item_is_planned(first)
    assert first.diagnostics.source == PLAN_SOURCE_CLI
    _assert_only_the_clean_item_is_planned(second)
    # The local CLI really ran, twice: nothing was cached to spare the second call.
    assert len(calls.read_text(encoding="ascii")) == 2
    _assert_nothing_was_cached(runner.fs_store.root, first, second)
    assert not (runner.fs_store.root / "cli_cache").exists()
    assert SYNTHETIC_KEY not in caplog.text
    assert SYNTHETIC_KEY not in json.dumps(first.diagnostics.to_dict(), ensure_ascii=False)


def test_a_clean_reply_is_cached_on_both_channels_so_the_gate_is_not_an_empty_switch(tmp_path: Path):
    """The control: a clean reply is still cached and served, on both channels."""
    api = _api_runner(tmp_path)
    fake = _FakeModel([_doc(with_summary=True)])
    api._call_chat_completion = fake

    first = api.plan_writes_detailed(NO_FINDINGS, [], 8)
    second = api.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert len(fake.prompts) == 1
    assert (first.diagnostics.cache_hit, second.diagnostics.cache_hit) == (False, True)
    assert _files_under(api.fs_store.root) == [f"api_cache/{first.diagnostics.cache_file}"]

    cli, calls = _cli_runner(tmp_path / "cli", {"planned_writes": [_doc(with_summary=True)]})
    first = cli.plan_writes_detailed(NO_FINDINGS, [], 8)
    second = cli.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert len(calls.read_text(encoding="ascii")) == 1
    assert (first.diagnostics.cache_hit, second.diagnostics.cache_hit) == (False, True)
    assert _files_under(cli.fs_store.root) == [f"cli_cache/{first.diagnostics.cache_file}"]
    assert json.loads((cli.fs_store.root / "cli_cache" / first.diagnostics.cache_file).read_text(encoding="utf-8")) == {
        "planned_writes": [_doc(with_summary=True)]
    }


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        pytest.param(MIXED_REPLY, "OpenAI/Anthropic API Key", id="an-item-the-privacy-gate-refused"),
        pytest.param(
            {
                "planned_writes": [
                    _doc(with_summary=True),
                    {**_doc(slug="leaky-summary", with_summary=True), "summary": f"记录联调密钥 {SYNTHETIC_KEY}"},
                ]
            },
            "OpenAI/Anthropic API Key",
            id="the-summary-of-an-item",
        ),
        pytest.param(
            {
                "planned_writes": [
                    _doc(with_summary=True),
                    {"target_kind": "note", "action": "create", "title": f"未知类型 {SYNTHETIC_KEY}", "summary": "摘要"},
                ]
            },
            "OpenAI/Anthropic API Key",
            id="an-item-dropped-for-another-reason",
        ),
        pytest.param(
            {"planned_writes": [_doc(with_summary=True)], "notes": SYNTHETIC_ASSIGNMENT},
            "Credential Assignment",
            id="free-text-beside-the-items",
        ),
    ],
)
def test_a_credential_anywhere_in_a_reply_keeps_the_whole_reply_out_of_the_cache(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    heuristic_planner_forbidden: None,
    reply: dict[str, Any],
    kind: str,
):
    """The privacy gate reads items one by one, the cache reads the whole reply.

    A credential may sit where the gate never looks (a summary, a title, a field beside the
    items), or in an item dropped for another reason. The plan keeps the usable item; the
    reply as a whole stays off the disk. Only the strict check reads a credential assignment.
    """
    runner = _api_runner(tmp_path)
    fake = _ReplyModel(reply)
    runner._call_chat_completion = fake

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        first = runner.plan_writes_detailed(NO_FINDINGS, [], 8)
        second = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert [w.title for w in first.planned_writes][:1] == ["接口超时排查复盘"]
    assert len(fake.prompts) == 2
    _assert_nothing_was_cached(runner.fs_store.root, first, second)
    assert SYNTHETIC_KEY not in caplog.text
    assert SYNTHETIC_KEY not in json.dumps(first.diagnostics.to_dict(), ensure_ascii=False)
    (refusal,) = [r.getMessage() for r in caplog.records if "Reply not cached" in r.getMessage()][:1]
    assert refusal.endswith(f"): a credential shape is in the reply ({kind}). The next identical batch asks again.")


def test_an_item_the_privacy_gate_did_not_refuse_keeps_its_own_reason(
    tmp_path: Path, heuristic_planner_forbidden: None
):
    """The control for the case above: a credential in a dropped item's title is not what dropped it.

    So the cache cannot learn about that credential from the plan's drop reasons; it has to
    read the reply itself.
    """
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _ReplyModel(
        {
            "planned_writes": [
                _doc(with_summary=True),
                {"target_kind": "note", "action": "create", "title": f"未知类型 {SYNTHETIC_KEY}", "summary": "摘要"},
            ]
        }
    )

    outcome = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    (dropped,) = outcome.diagnostics.dropped_items
    assert dropped["index"] == 1 and dropped["reasons"] == ["UNSUPPORTED_TARGET_KIND: note"]
    assert outcome.diagnostics.cache_file is None


def test_a_cache_file_from_before_the_cache_refused_credentials_is_discarded_not_served(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """A file written before this rule is not served, and does not stay on the disk."""
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _FakeModel([_doc(with_summary=True)])
    cached = runner.plan_writes_detailed(NO_FINDINGS, [], 8)
    (cache_file,) = (runner.fs_store.root / "api_cache").glob("p03_plan-*.json")
    assert cached.diagnostics.cache_file == cache_file.name

    # The file a reply the gate refused part of left behind.
    cache_file.write_text(json.dumps(MIXED_REPLY, ensure_ascii=False), encoding="utf-8")
    asked = _FakeModel([_doc(with_summary=True)])
    runner._call_chat_completion = asked
    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        again = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert len(asked.prompts) == 1  # the file was not served
    assert [w.title for w in again.planned_writes] == ["接口超时排查复盘"]
    assert again.diagnostics.cache_hit is False  # and is not reported as a hit
    assert json.loads(cache_file.read_text(encoding="utf-8")) == {"planned_writes": [_doc(with_summary=True)]}
    assert SYNTHETIC_KEY not in cache_file.read_text(encoding="utf-8")
    assert SYNTHETIC_KEY not in caplog.text
    (discarded,) = [r.getMessage() for r in caplog.records if "Cached reply discarded" in r.getMessage()]
    assert discarded == (
        f"Cached reply discarded ({cache_file.name}): a credential shape is in it "
        "(OpenAI/Anthropic API Key). The run asks again."
    )


# ---------------------------------------------------------------- no template Skills


def test_skill_action_must_match_the_target_directory(tmp_path: Path):
    rt, skills_dir = _runtime(tmp_path)
    _existing_skill(skills_dir, "release-checklist")
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    runner._call_chat_completion = _FakeModel(
        [
            _skill("ghost-skill", action="update", with_summary=True),  # update, but the target does not exist
            _skill("release-checklist", action="create", with_summary=True),  # create, but the target exists
            _skill("incident-drill", action="create"),  # a genuinely new skill
        ]
    )

    outcome = runner.plan_writes_detailed(NO_FINDINGS, [], 8)

    assert [(w.relative_path, w.action) for w in outcome.planned_writes] == [
        ("release-checklist/SKILL.md", "update"),
        ("incident-drill/SKILL.md", "create"),
    ]
    assert [d["reasons"] for d in outcome.diagnostics.dropped_items] == [["UPDATE_TARGET_MISSING: ghost-skill"]]
    assert outcome.diagnostics.relabeled_skill_actions == ["CREATE_TARGET_EXISTS: release-checklist"]
    # whoever approves is told what an update does to the existing file; a create replaces nothing
    updates = [w for w in outcome.planned_writes if w.action == "update"]
    assert len(updates) == 1 and "在现有文件末尾追加小节" in updates[0].approval_reason
    assert "目标 Skill 已存在，按更新处理" in updates[0].approval_reason
    assert "整文件替换" not in outcome.planned_writes[1].approval_reason
    assert sorted(p.name for p in skills_dir.iterdir()) == ["release-checklist"]


def test_heuristic_planner_reports_skill_candidates_without_writing():
    review = {
        "findings": [
            {"session_key": "codex:w-1", "title": "发版检查复盘", "project": "demo", "facts": ["每次发版都要手工检查版本号"], "anomalies": [], "tags": ["release"]}
        ],
        "repeated_workflows": [
            {
                "session_key": "codex:w-1",
                "name_hint": "release-checklist-v2",
                "title": "发版检查复盘",
                "existing_skill_match": "release-checklist",
                "action": "update",
                "reason": "固定步骤反复执行",
            },
            {"session_key": "codex:w-1", "name_hint": "incident-drill", "title": "演练流程", "existing_skill_match": None, "action": "create", "reason": "值得封装"},
        ],
    }

    outcome = DryRunHeuristicRunner().plan_writes_detailed(review, [], 8)

    assert [w.target_kind for w in outcome.planned_writes] == ["knowledge_doc"]
    assert outcome.diagnostics.source == PLAN_SOURCE_HEURISTIC
    assert outcome.diagnostics.deferred_skill_candidates == [
        {"session_key": "codex:w-1", "title": "发版检查复盘", "action": "update", "target_skill": "release-checklist", "reason": "固定步骤反复执行"},
        {"session_key": "codex:w-1", "title": "演练流程", "action": "create", "target_skill": "incident-drill", "reason": "值得封装"},
    ]
    assert DryRunHeuristicRunner().plan_writes_detailed(NO_FINDINGS, [], 8).diagnostics.deferred_skill_candidates == []


def test_dry_run_run_states_the_source_and_defers_skill_candidates(tmp_path: Path):
    rt, skills_dir = _runtime(tmp_path, mode="dry_run", approval_mode="smart")
    rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id="wf-1",
            title="发版检查固定步骤封装skill",
            project="ops",
            summary="每次都要按固定步骤检查版本号、构建并校验产物，值得封装成重复流程 Skill",
            content="用户：这个重复流程每次都要手工跑 4 步，请封装 skill。助手：整理为固定步骤。",
        )
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily")

    assert run.status == RunStatus.PASS, run.error
    assert [w.target_kind for w in run.planned_writes] == ["knowledge_doc"]
    p03 = _node(run, "P03_ROUTE_PLAN")
    assert "计划来源：本地启发式计划器" in p03.summary
    assert "1 个重复流程候选未生成 Skill 计划" in p03.summary
    assert p03.payload["plan_source"] == "heuristic"
    assert p03.payload["plan_diagnostics"]["deferred_skill_candidates"][0]["session_key"] == "codex:wf-1"
    assert _event_types(rt, run).count("skill_candidates_deferred") == 1
    assert not list(skills_dir.rglob("SKILL.md"))


def test_model_skill_item_reaches_approval_and_the_write_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt, skills_dir = _runtime(tmp_path, approval_mode="smart")
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner, "_call_chat_completion", _FakeModel([_skill("release-checklist", action="create", with_summary=True)])
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:plan-1"])

    assert run.status == RunStatus.AWAITING_APPROVAL
    assert [(w.target_kind, w.action) for w in run.planned_writes] == [("skill_improvement", "create")]
    finished = rt.orchestrator.approve_and_resume(run.run_id)
    assert finished.status == RunStatus.PASS, finished.error
    assert (skills_dir / "release-checklist" / "SKILL.md").is_file()
    assert any(a["origin"] == "flywheel_created" for a in rt.db.list_skill_assets())


# ---------------------------------------------------------------- output contract


def test_p03_prompt_lists_the_required_fields_and_constraints():
    prompt = _build_p03_prompt(NO_FINDINGS, [], [{"name": "release-checklist", "description": "发版检查"}], 5, "lessons/", "daily")

    skeleton = prompt.split("Return STRICT JSON ONLY:\n", 1)[1].split("\n\n", 1)[0]
    for key in P03_ITEM_REQUIRED_KEYS:
        assert f'"{key}"' in skeleton
    assert 'If nothing is worth writing, return {"planned_writes": []}' in prompt
    assert 'For action="update" the directory MUST be the name of an existing skill from existing_skill_names' in prompt
    assert "never a new directory" in prompt


def test_json_repair_retry_receives_the_same_required_fields(tmp_path: Path):
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    repair_prompts: list[str] = []

    def chat(prompt: str, system_prompt: str) -> str:
        if prompt.startswith("The previous output was malformed JSON"):
            repair_prompts.append(prompt)
            return '{"planned_writes": []}'
        return '{"planned_writes": [ BROKEN'

    runner._call_chat_completion = chat
    assert runner.plan_writes(NO_FINDINGS, [], 5) == []

    assert len(repair_prompts) == 1
    for key in P03_ITEM_REQUIRED_KEYS:
        assert f'"{key}"' in repair_prompts[0]
    assert P03_REPAIR_SCHEMA_HINT in repair_prompts[0]


# ---------------------------------------------------------------- project-domain pages in the write plan
# (feat-project-domain-incremental-deposit)


PROJECTS = "wiki-知识库/projects-项目"
# The one project of the deposit Wiki, and the two domain pages it already holds.
ALPHA_DIR = "alpha-orders-Alpha订单系统"
ORDERS_REL = f"{PROJECTS}/{ALPHA_DIR}/数据库结构/orders.md"
DATABASE_REL = f"{PROJECTS}/{ALPHA_DIR}/项目配置/database.md"

# The contract of a Wiki that declares the four-domain deposit routing (content_contract.deposit_routing).
DEPOSIT_SCHEMA = textwrap.dedent(
    """\
    ---
    core_contract:
      required_fields: [title, description, type, created, updated]
    partition_contract:
      partitions:
        projects:
          root: wiki-知识库/projects-项目
    content_contract:
      page_types:
        project-practice-note:
          partition: projects
          path_pattern: "^[^/]+/实战笔记/[^/]+\\\\.md$"
          allowed_status: [active, deprecated, archived]
        project-algorithm:
          partition: projects
          required_fields: [title, description, type, project_id, knowledge_kind, load_policy, default_load, created, updated, status]
          path_pattern: "^[^/]+/(业务算法|数据库结构)/[^/]+\\\\.md$"
          allowed_status: [active, deprecated, archived]
          knowledge_kind_values: [ddl, algorithm, caliber]
        project-architecture:
          partition: projects
          required_fields: [title, description, type, project_id, knowledge_kind, load_policy, default_load, created, updated, status]
          path_pattern: "^[^/]+/系统架构/[^/]+\\\\.md$"
          allowed_status: [active, deprecated, archived]
        project-config:
          partition: projects
          required_fields: [title, description, type, project_id, load_policy, default_load, created, updated, status]
          path_pattern: "^[^/]+/项目配置/.+\\\\.md$"
          allowed_status: [active, deprecated, archived]
      project_template_contract:
        templates:
          - id: T7
            name: 表结构
            page_type: project-algorithm
            knowledge_kind: ddl
            load_policy: index_then_on_demand
            body_sections: [用途一行, 字段语义表, 枚举值, 关联键, 坑与边界]
          - id: T1
            name: 业务算法
            page_type: project-algorithm
            knowledge_kind: algorithm
            load_policy: index_then_on_demand
            body_sections: [适用场景, 算法步骤, 坑与边界]
          - id: T3
            name: 服务卡
            page_type: project-architecture
            knowledge_kind: service
            load_policy: index_then_on_demand
            body_sections: [职责, 依赖关系, 坑与边界]
          - id: T6
            name: 项目配置
            page_type: project-config
            load_policy: explicit_or_task_required
            body_sections: [用途一行, 配置矩阵表, 使用规则, 变更记录表]
      deposit_routing:
        version: "1.1"
        rule: 会话查证过的项目领域知识落到项目目录下：一表一页，缺页新建，已有页增量补充。
        domains:
          - domain: 数据库结构
            trigger: 会话查证过业务表的字段、枚举、关联键或使用边界
            target: "{project}/数据库结构/"
            template: T7
          - domain: 系统架构
            trigger: 会话查证过服务依赖或部署关系
            target: "{project}/系统架构/"
            template: T3
          - domain: 业务算法
            trigger: 会话查证过业务算法或统计口径
            target: "{project}/业务算法/"
            template: T1
          - domain: 项目配置
            trigger: 会话查证过后续会话必须记住的项目配置事实
            target: "{project}/项目配置/"
            template: T6
    ---

    # Test Wiki Schema
    """
)

ORDERS_PAGE = textwrap.dedent(
    """\
    ---
    title: orders
    description: 订单主表：一笔订单一行
    type: project-algorithm
    project_id: alpha
    knowledge_kind: ddl
    load_policy: index_then_on_demand
    default_load: false
    created: 2026-11-20
    updated: 2026-11-20
    status: active
    ---

    # orders

    ## 用途一行

    订单主表，一笔订单一行。

    ## 字段语义表

    | 字段 | 类型 | 含义 | 备注 |
    | --- | --- | --- | --- |
    | id | bigint | 主键 | 自增 |
    """
)

# The configuration page declares itself private: the planner may learn its layout, nothing else.
DATABASE_PAGE = textwrap.dedent(
    """\
    ---
    title: database
    description: alpha 的数据源与逻辑库映射
    type: project-config
    project_id: alpha
    load_policy: explicit_or_task_required
    default_load: false
    sensitivity: private_sensitive
    access_scope: maintainers
    created: 2026-11-20
    updated: 2026-11-20
    status: active
    ---

    # database

    ## 用途一行

    记录 alpha 服务连接数据库的方式；凭据本身不入库。

    ## 配置矩阵表

    | 配置项 | 取值 | 说明 |
    | --- | --- | --- |
    | logical_db.orders | shard_0 | 订单库路由 |

    ## 使用规则

    - 连接池上限按实例规格设置，不超过实例最大连接数的一半。
    """
)


def _put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # LF, as the Wiki's pages are
    return path


def _deposit_wiki(tmp_path: Path) -> Path:
    """A mounted Wiki that declares the deposit routing; project alpha is its only project.

    Alpha holds the table page `orders` (next to a hidden and an archived draft, which are
    not pages of the domain) and the private configuration page `database`.
    """
    wiki = tmp_path / "deposit-wiki"
    _put(wiki / ".wiki-schema.md", DEPOSIT_SCHEMA)
    _put(
        wiki / PROJECTS / ALPHA_DIR / "实战笔记" / "笔记索引.md",
        "---\ntitle: alpha 索引\ndescription: alpha 实战经验卡索引\ntype: project-practice-index\nproject_id: alpha\n"
        "aliases: [Alpha Orders]\ncreated: 2026-11-20\nupdated: 2026-11-20\nstatus: active\n---\n\n# alpha 索引\n",
    )
    _put(wiki / ORDERS_REL, ORDERS_PAGE)
    _put(wiki / f"{PROJECTS}/{ALPHA_DIR}/数据库结构/.draft-payments.md", ORDERS_PAGE)
    _put(wiki / f"{PROJECTS}/{ALPHA_DIR}/数据库结构/archive-refunds.md", ORDERS_PAGE)
    _put(wiki / DATABASE_REL, DATABASE_PAGE)
    return wiki


def _deposit_config(tmp_path: Path) -> FlywheelConfig:
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(tmp_path / "deposit-wiki")
    cfg.knowledge_sink.allowed_write_prefixes = ["wiki-知识库"]
    cfg.knowledge_sink.wiki_gate_require_scripts = False
    return cfg


def _deposit_gate(tmp_path: Path, *, project: bool = True) -> WikiGate:
    """The gate of a deposit Wiki; without ``project`` the contract declares domains no page can reach."""
    if project:
        _deposit_wiki(tmp_path)
    else:
        _put(tmp_path / "deposit-wiki" / ".wiki-schema.md", DEPOSIT_SCHEMA)
    return WikiGate(_deposit_config(tmp_path))


def _deposit_runtime(tmp_path: Path, *, approval_mode: str = "smart") -> FlywheelRuntime:
    _deposit_wiki(tmp_path)
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    (tmp_path / "skills").mkdir()
    cfg = _deposit_config(tmp_path)
    cfg.capabilities.skills_dirs = [str(tmp_path / "skills")]
    cfg.workflow.approval_mode = approval_mode
    # `auto` only takes effect with the safety valve open: the valve has the last word.
    cfg.safety_valves.auto_approve_writes = approval_mode == "auto"
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    return FlywheelRuntime(cfg)


def _signalled_review(session_key: str = "codex:s-1") -> dict[str, Any]:
    """A review whose finding verified the `orders` table and a connection fact of project alpha."""
    return {
        "findings": [
            {
                "session_key": session_key,
                "client": "codex",
                "project": "alpha",
                "title": "订单表字段排查",
                "worthy": True,
                "category": "project",
                "skip_reason": "",
                "facts": ["查证了 orders 表的 status 枚举与 customer_id 关联键"],
                "anomalies": [],
                "tags": ["alpha"],
                "suggest_rule": False,
                "skill_route": {"hit": False, "action": "none", "target_skill": "", "reason": "单次任务"},
                "domain_signals": {
                    "database_tables": [
                        {
                            "table": "orders",
                            "verified_fields": ["status varchar(32)"],
                            "enums": ["status：PAID / CLOSED"],
                            "join_keys": ["customer_id -> customers.customer_id"],
                            "pitfalls": [],
                        }
                    ],
                    "project_configs": [
                        {
                            "topic": "database",
                            "facts": ["逻辑库 payments 与 orders 不在同一分片"],
                            "pitfalls": ["跨库关联只能在应用层做"],
                        }
                    ],
                    "architecture_flows": [],
                    "business_calibers": [],
                },
            }
        ],
        "eligible_promotions": [],
        "repeated_workflows": [],
    }


def _planner_input(review: dict[str, Any], candidates: RoutingCandidates | None) -> tuple[str, dict[str, Any]]:
    prompt = _build_p03_prompt(review, [], [], 5, "lessons/", "daily", routing_candidates=candidates)
    return prompt, json.loads(prompt.rsplit("\n\n", 1)[1])


def _card_only_candidates() -> RoutingCandidates:
    """The candidates of a contract-governed Wiki that declares no deposit routing."""
    return RoutingCandidates(
        page_types=["project-practice-note"],
        projects=[{"dir_name": ALPHA_DIR, "project_id": "alpha", "aliases": ["Alpha Orders"], "description": ""}],
        evidence_page_types=["project-practice-note"],
    )


def _table_item() -> dict[str, Any]:
    """A plan item for a table page that does not exist yet: the template's sections."""
    return {
        "target_kind": "knowledge_doc",
        "action": "create",
        "title": "payments 支付流水表",
        "summary": "新表 payments 的实战已证字段",
        "relative_path": f"{PROJECTS}/{ALPHA_DIR}/数据库结构/payments.md",
        "source_session_keys": ["codex:s-1"],
        "content_preview": (
            "## 用途一行\n\n支付流水表：一次支付尝试一行。\n\n"
            "## 字段语义表\n\n| 字段 | 类型 | 含义 | 备注 |\n| --- | --- | --- | --- |\n| payment_id | bigint | 支付主键 | 自增 |\n"
        ),
        "wiki_route": {"page_type": "project-algorithm", "project": "alpha", "reason": "会话查证了本项目自己的表"},
    }


def _service_item() -> dict[str, Any]:
    return {
        "target_kind": "knowledge_doc",
        "action": "create",
        "title": "payment-service 服务卡",
        "summary": "支付服务的职责与依赖",
        "relative_path": f"{PROJECTS}/{ALPHA_DIR}/系统架构/payment-service.md",
        "source_session_keys": ["codex:s-1"],
        "content_preview": "## 职责\n\n- 订单受理与状态推进。\n",
        "wiki_route": {"page_type": "project-architecture", "project": "alpha", "reason": "会话核实了本服务的依赖"},
    }


def _config_item(**overrides: Any) -> dict[str, Any]:
    """A plan item that adds one verified configuration fact to the existing configuration page."""
    item: dict[str, Any] = {
        "target_kind": "knowledge_doc",
        "action": "update",
        "title": "database 配置页增量",
        "summary": "补充逻辑库分片与连库注意事项",
        "relative_path": DATABASE_REL,
        "source_session_keys": ["codex:plan-1"],
        "content_preview": "## 使用规则\n\n- 逻辑库 payments 与 orders 不在同一分片，跨库关联只能在应用层做。\n",
        "wiki_route": {"page_type": "project-config", "project": "alpha", "reason": "会话查证了本项目的连库配置"},
    }
    item.update(overrides)
    return item


def _plan(items: list[Any], *, max_write_set: int = 8, domain_page_types: frozenset[str] = frozenset()):
    return _plan_from_items(
        items,
        source=PLAN_SOURCE_MODEL,
        max_write_set=max_write_set,
        default_doc_prefix="lessons/",
        skills_root=None,
        domain_page_types=domain_page_types,
    )


class _DepositModel:
    """Stands in for the model gateway: the review carries domain signals, the plan is canned."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("You are the P02 Review Worker"):
            return json.dumps(_signalled_review("codex:plan-1"), ensure_ascii=False)
        return json.dumps({"planned_writes": self.items}, ensure_ascii=False)


def _register_troubleshooting(rt: FlywheelRuntime) -> None:
    rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id="plan-1",
            title="订单表字段与连库排查",
            project="alpha",
            summary="记录订单表已证字段与连库配置事实",
            content=(
                "用户：订单跨库关联查不出来。"
                "助手：查了 orders 表的 status 枚举；逻辑库 payments 与 orders 不在同一分片，跨库关联只能在应用层做。"
            ),
        )
    )


# ---- the planning input


def test_a_wiki_that_declares_the_deposit_routing_enters_the_p03_input(tmp_path: Path):
    gate = _deposit_gate(tmp_path)
    candidates = gate.routing_candidates()
    gate.sketch_domain_pages(candidates, ["orders", "database"])
    assert candidates.routable is True

    prompt, payload = _planner_input(_signalled_review(), candidates)

    assert prompt.startswith("You are the P03 Route & Write-Plan Worker of AI Flywheel.\n")
    offered = payload["routing_candidates"]
    deposit = offered["deposit_routing"]
    # The contract's rules, read off the Wiki: every domain with its trigger, target and page types.
    assert deposit["version"] == "1.1"
    assert deposit["rule"] == "会话查证过的项目领域知识落到项目目录下：一表一页，缺页新建，已有页增量补充。"
    assert deposit["projects_root"] == PROJECTS
    assert [(domain["domain"], domain["target_path"], domain["page_types"]) for domain in deposit["domains"]] == [
        ("数据库结构", f"{PROJECTS}/{{project}}/数据库结构/{{page}}.md", ["project-algorithm"]),
        ("系统架构", f"{PROJECTS}/{{project}}/系统架构/{{page}}.md", ["project-architecture"]),
        ("业务算法", f"{PROJECTS}/{{project}}/业务算法/{{page}}.md", ["project-algorithm"]),
        ("项目配置", f"{PROJECTS}/{{project}}/项目配置/{{page}}.md", ["project-config"]),
    ]
    assert deposit["domains"][3]["trigger"] == "会话查证过后续会话必须记住的项目配置事实"
    assert [domain["sensitive"] for domain in deposit["domains"]] == [False, False, False, True]
    table_template = deposit["domains"][0]["templates"][0]
    assert (table_template["id"], table_template["knowledge_kind"]) == ("T7", "ddl")
    assert table_template["body_sections"] == ["用途一行", "字段语义表", "枚举值", "关联键", "坑与边界"]
    assert deposit["domains"][0]["required_fields"]["project-algorithm"][:5] == ["title", "description", "type", "created", "updated"]
    # The card types stay routable next to the domain page types
    assert offered["page_types"] == ["project-practice-note", "project-algorithm", "project-architecture", "project-config"]

    # Each candidate project carries the domain pages it already holds: alpha holds `orders` and `database`.
    (alpha,) = offered["projects"]
    assert alpha["project"] == ALPHA_DIR
    assert alpha["domain_inventory"] == {"数据库结构": ["orders"], "系统架构": [], "业务算法": [], "项目配置": ["database"]}

    # The review's signals reach the planner with their finding
    signals = payload["findings"][0]["domain_signals"]
    assert signals["database_tables"][0]["table"] == "orders"
    assert signals["project_configs"][0]["topic"] == "database"

    # ...and so do the planning rules: what the spec asks of a plan for these pages.
    rules = prompt[prompt.index("PROJECT DOMAIN PAGES:") : prompt.index("Allowed target_kind values:")]
    for needle in (
        '"routing_candidates.deposit_routing" declares them',
        '"domain_inventory" lists the domain pages it',
        "GRADUAL COMPLETION RULES:",
        "Keep at least one slot for the main experience card of rule 1: a domain page must not crowd it out",
        "Only inside the item cap",
        "only for a fact the session verified",
        "Never plan a domain page from a guess",
        "2. database_tables, ONE TABLE PER PAGE",
        'when the table name is listed in that project\'s domain_inventory, action="update" and relative_path is that existing page',
        'otherwise action="create" with a page named after the table alone',
        "Record only the verified fields, enum values, join keys and pitfalls: a full DDL is never required",
        "3. project_configs: a verified configuration fact that a later session must remember is planned even when nothing was changed",
        'never skip it as "no config change"',
        'When the configuration page exists, prefer action="update"',
        "NEVER output a password, token or private key, not even partly masked",
        "SECRET_LEAK_BLOCKED",
        "Name where the credential is kept instead",
        "4. architecture_flows and business_calibers follow the same rule for the pages of their domain",
    ):
        assert needle in rules, needle
    # The domain item keeps the shape of every other knowledge document
    assert '"wiki_route"={"page_type": <one page_types value of that domain>' in rules
    assert "Reserve at least one slot for a worthy knowledge_doc item" in prompt  # the rule it had
    assert set(payload) == {
        "phase",
        "findings",
        "eligible_promotions",
        "repeated_workflows",
        "existing_rules",
        "existing_skill_names",
        "existing_skill_sections",
        "routing_candidates",
    }


@pytest.mark.parametrize("candidates", [None, _card_only_candidates()], ids=["no-contract", "cards-only"])
def test_a_wiki_that_declares_no_deposit_routing_keeps_the_p03_prompt_it_had(candidates):
    signalled = _signalled_review()
    plain = {**signalled, "findings": [{key: value for key, value in signalled["findings"][0].items() if key != "domain_signals"}]}

    # A review that carries `domain_signals` yields the prompt a review without them always yielded.
    for review in (signalled, plain):
        prompt, payload = _planner_input(review, candidates)
        assert prompt == _planner_input(plain, candidates)[0]
        assert "domain_signals" not in prompt
        assert "deposit_routing" not in json.dumps(payload, ensure_ascii=False)
        assert "domain_inventory" not in json.dumps(payload, ensure_ascii=False)
        for absent in ("PROJECT DOMAIN PAGES", "GRADUAL COMPLETION RULES", "ONE TABLE PER PAGE", "SECRET_LEAK_BLOCKED", "no config change"):
            assert absent not in prompt, absent
    # The output contract is the one it had: `wiki_route` exactly when the Wiki routes cards.
    skeleton = prompt.split("Return STRICT JSON ONLY:\n", 1)[1].split("\n\n", 1)[0]
    assert ('"wiki_route":{"page_type":"...","project":"... or null","reason":"..."}' in skeleton) is (candidates is not None)


def test_the_rules_stay_out_when_the_wiki_offers_nothing_to_write(tmp_path: Path):
    gate = _deposit_gate(tmp_path, project=False)
    candidates = gate.routing_candidates()
    assert (candidates.routable, candidates.deposit_routing is not None) == (False, True)

    prompt, _ = _planner_input(_signalled_review(), candidates)

    assert 'Do not output any "knowledge_doc" item' in prompt
    assert "PROJECT DOMAIN PAGES" not in prompt
    assert "domain_signals" not in prompt


def test_the_api_runner_hands_the_planner_the_declared_routing(tmp_path: Path):
    gate = _deposit_gate(tmp_path)
    candidates = gate.routing_candidates()
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    seen: list[str] = []

    def chat(prompt: str, system_prompt: str) -> str:
        seen.append(prompt)
        return '{"planned_writes": []}'

    runner._call_chat_completion = chat

    assert runner.plan_writes_detailed(_signalled_review(), [], 8, routing_candidates=candidates).planned_writes == []

    (prompt,) = seen
    payload = json.loads(prompt.rsplit("\n\n", 1)[1])
    assert payload["routing_candidates"]["deposit_routing"]["version"] == "1.1"
    assert payload["findings"][0]["domain_signals"]["database_tables"][0]["table"] == "orders"
    assert "PROJECT DOMAIN PAGES:" in prompt


# ---- what the planner is shown of the pages it may update


def test_the_planner_sees_the_layout_of_the_pages_a_plan_may_update(tmp_path: Path):
    gate = _deposit_gate(tmp_path)
    candidates = gate.routing_candidates()
    gate.sketch_domain_pages(candidates, ["orders", "database"])
    (alpha,) = candidates.projects

    # Section by section: the header row of a table, None where the section holds text.
    assert alpha["domain_page_sections"] == {
        "orders": {"用途一行": None, "字段语义表": ["字段", "类型", "含义", "备注"]},
        "database": {"用途一行": None, "配置矩阵表": ["配置项", "取值", "说明"], "使用规则": None},
    }

    # A private page lends its layout and nothing else: no value it states leaves it.
    prompt, _ = _planner_input(_signalled_review(), candidates)
    assert '"domain_page_sections"' in prompt
    for value in ("shard_0", "订单库路由", "凭据本身不入库", "自增", "订单主表，一笔订单一行"):
        assert value not in prompt, value


def test_only_the_pages_the_review_signalled_are_sketched(tmp_path: Path):
    gate = _deposit_gate(tmp_path)
    candidates = gate.routing_candidates()

    gate.sketch_domain_pages(candidates, ["payments"])
    assert "domain_page_sections" not in candidates.projects[0]
    gate.sketch_domain_pages(candidates, [])
    assert "domain_page_sections" not in candidates.projects[0]
    # A page that is not on disk any more has no layout to show
    gate.sketch_domain_pages(candidates, ["payments", "orders"])
    assert list(candidates.projects[0]["domain_page_sections"]) == ["orders"]


def test_a_page_sketch_shows_table_headers_and_no_data_row():
    page = (
        "---\ntype: project-config\n---\n\n# database\n\n"
        "## 配置矩阵表\n\n| 配置项 | 取值 |\n| --- | --- |\n| logical_db.orders | shard_0 |\n\n"
        "## 使用规则\n\n- 密码走环境变量注入\n\n"
        "## 变更记录表\n\n| 2026-11-20 | 建页 |\n| 2026-11-21 | 补一行 |\n\n"
        "## 示例\n\n```sql\nselect 1;\n```\n"
    ).replace("\n", "\r\n")  # a page saved with CRLF endings reads the same

    assert page_section_sketch(page) == {
        "配置矩阵表": ["配置项", "取值"],
        "使用规则": None,  # text
        "变更记录表": None,  # rows without a separator row under the first one: no header to show
        "示例": None,  # a fenced block
    }
    assert page_section_sketch("") == {}
    assert page_section_sketch("## 配置矩阵表\n\n无\n") == {"配置矩阵表": None}


def test_the_review_names_the_pages_a_plan_may_touch():
    assert domain_signal_names(_signalled_review()["findings"]) == ["orders", "database"]
    assert domain_signal_names(None) == []
    assert domain_signal_names("not a list") == []
    assert domain_signal_names([{"facts": ["no domain clue"]}, {"domain_signals": {}}]) == []
    # Each name once, in the order of first mention; the other two domains name no page.
    findings = [
        {"domain_signals": {"database_tables": [{"table": "orders"}, {"table": "payments"}], "project_configs": [{"topic": "database"}]}},
        {
            "domain_signals": {
                "database_tables": [{"table": "orders"}],
                "project_configs": [{"topic": "database"}],
                "architecture_flows": ["订单服务 -> 支付服务"],
                "business_calibers": ["客单价按已支付金额统计"],
            }
        },
    ]
    assert domain_signal_names(findings) == ["orders", "payments", "database"]


# ---- the plan items of a project-domain page


def test_a_configuration_page_is_flagged_sensitive_before_anyone_approves_it():
    (write,) = _plan([_config_item()]).planned_writes
    assert write.requires_approval is True
    assert write.approval_reason == "项目配置页属于敏感写入，需人工确认后才会落盘"
    assert is_sensitive_write(write) is True

    # The model's own reason is kept; the flag is set all the same.
    (kept,) = _plan([_config_item(approval_reason="补充逻辑库分片映射")]).planned_writes
    assert (kept.requires_approval, kept.approval_reason) == (True, "补充逻辑库分片映射")

    # A new configuration page is as sensitive as an increment into an existing one.
    (created,) = _plan([_config_item(action="create", relative_path=f"{PROJECTS}/{ALPHA_DIR}/项目配置/redis.md")]).planned_writes
    assert (created.action, created.requires_approval, is_sensitive_write(created)) == ("create", True, True)


@pytest.mark.parametrize("page_type", ["project-practice-note", "project-algorithm", "project-architecture"])
def test_every_other_page_stays_routine(page_type: str):
    route = {"page_type": page_type, "project": "alpha", "reason": "会话查证了本项目自己的表"}
    (write,) = _plan([_config_item(wiki_route=route)]).planned_writes
    assert (write.requires_approval, write.approval_reason, is_sensitive_write(write)) == (False, "", False)


PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param("sk-" + "9" * 32, id="api-key"),
        pytest.param(PRIVATE_KEY_HEADER, id="private-key"),
    ],
)
def test_a_credential_in_a_domain_item_is_dropped_before_it_is_planned(secret: str):
    """A credential kind is refused by the privacy gate every knowledge document passes."""
    draft = f"## 使用规则\n\n- 测试库 {secret}，只在本地使用。\n"

    outcome = _plan([_table_item(), _config_item(content_preview=draft)])

    assert [write.title for write in outcome.planned_writes] == ["payments 支付流水表"]
    assert [(dropped["index"], dropped["reasons"]) for dropped in outcome.diagnostics.dropped_items] == [(1, ["SECRET_LEAK_BLOCKED"])]
    assert secret not in json.dumps(outcome.diagnostics.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("- 测试库 password: Hk9-Qp31x，只在本地使用。", id="assignment"),
        pytest.param("- 数据库密码：Hk9-Qp31x，只在本地使用。", id="chinese-assignment"),
    ],
)
def test_a_password_in_an_increment_is_left_to_the_wiki_gate(tmp_path: Path, line: str):
    """A credential assignment is only masked on the ordinary write path: an experience card may
    mention one. A configuration page is held to the strict reading, so the Wiki gate refuses it."""
    draft = f"## 使用规则\n\n{line}\n"

    (write,) = _plan([_config_item(content_preview=draft)]).planned_writes

    # Sensitive, so it waits for a human either way: the route gate refuses it before that.
    assert (write.requires_approval, "Hk9-Qp31x" in write.content_preview) == (True, True)
    gate = _deposit_gate(tmp_path)
    decision = gate.evaluate_route(
        relative_path=DATABASE_REL,
        title=write.title,
        summary=write.summary,
        content=write.content_preview,
        wiki_route=write.wiki_route,
        action="update",
    )

    assert (decision.routed, decision.reason_code) == (False, "secret_leak_blocked")
    assert decision.detail.startswith("SECRET_LEAK_BLOCKED：")
    assert (tmp_path / "deposit-wiki" / DATABASE_REL).read_bytes() == DATABASE_PAGE.encode("utf-8")


def test_the_contract_decides_which_pages_are_sensitive(tmp_path: Path):
    gate = _deposit_gate(tmp_path)
    orders = (tmp_path / "deposit-wiki" / ORDERS_REL).read_text(encoding="utf-8")
    increment = "## 用途一行\n\n- 补充一行。\n"

    # The page type, a declaration of the page, and the domain the contract marks sensitive.
    assert gate.is_sensitive_page(ORDERS_REL, orders) is False
    assert gate.is_sensitive_page(ORDERS_REL, increment, existing_text=orders) is False
    assert gate.is_sensitive_page(DATABASE_REL, increment) is True
    assert gate.is_sensitive_page(DATABASE_REL, DATABASE_PAGE) is True
    assert gate.is_sensitive_page(f"{PROJECTS}/{ALPHA_DIR}/项目配置/redis.md", increment) is True
    assert gate.is_sensitive_page(f"{PROJECTS}/{ALPHA_DIR}/系统架构/payment-service.md", increment) is False


@pytest.mark.parametrize(
    "write, sensitive",
    [
        pytest.param(
            {
                "target_kind": "knowledge_doc",
                "action": "update",
                "wiki_route": {"page_type": "project-config", "project": "alpha"},
                "content_preview": "## 使用规则\n\n- 逻辑库 payments 与 orders 不在同一分片。\n",
            },
            True,
            id="configuration-page-update",
        ),
        pytest.param(
            {
                "target_kind": "knowledge_doc",
                "action": "create",
                "wiki_route": {"page_type": "Project-Config", "project": "alpha"},
                "content_preview": "## 用途一行\n\n记录数据源映射。\n",
            },
            True,
            id="configuration-page-create-spelled-in-any-case",
        ),
        pytest.param(
            {
                "target_kind": "knowledge_doc",
                "action": "update",
                "wiki_route": {"page_type": "project-practice-note", "project": "alpha"},
                "content_preview": "---\nsensitivity: private_sensitive\n---\n\n# database\n",
            },
            True,
            id="draft-declares-itself-private",
        ),
        pytest.param(
            {
                "target_kind": "knowledge_doc",
                "action": "update",
                "wiki_route": {"page_type": "project-practice-note", "project": "alpha"},
                "content_preview": "---\ncontains_credentials: true\n---\n\n# database\n",
            },
            True,
            id="draft-declares-credentials",
        ),
        pytest.param(
            {
                "target_kind": "knowledge_doc",
                "action": "update",
                "wiki_route": {"page_type": "project-practice-note", "project": "alpha"},
                "content_preview": "## 关键事项\n\n- 正文里引用 sensitivity: private_sensitive 不算声明。\n",
            },
            False,
            id="a-mention-in-the-body-is-not-a-declaration",
        ),
        pytest.param(
            {
                "target_kind": "knowledge_doc",
                "action": "update",
                "wiki_route": {"page_type": "project-algorithm", "project": "alpha"},
                "content_preview": "## 坑与边界\n\n- status 只在线上验证过两种取值。\n",
            },
            False,
            id="table-page",
        ),
        pytest.param({"target_kind": "flywheel_rule", "action": "register_rule"}, False, id="rule"),
    ],
)
def test_what_counts_as_a_sensitive_write_for_the_portal_and_the_card(write: dict[str, Any], sensitive: bool):
    """`is_sensitive_write` also takes the plain dict a card renders: both sides read one answer."""
    assert is_sensitive_write(write) is sensitive


def test_domain_pages_claim_the_batch_capacity_after_everything_else(tmp_path: Path):
    card = _doc(slug="card", with_summary=True)
    items = [_table_item(), _config_item(), _service_item(), card]  # the reply puts the domain pages first

    offered = deposit_page_types(_deposit_gate(tmp_path).routing_candidates())
    assert offered == frozenset({"project-algorithm", "project-architecture", "project-config"})
    assert deposit_page_types(_card_only_candidates()) == frozenset()
    assert deposit_page_types(None) == frozenset()

    # Three of four fit: the experience card keeps its slot, the last domain page waits for another batch.
    outcome = _plan(items, max_write_set=3, domain_page_types=offered)
    assert [write.title for write in outcome.planned_writes] == [card["title"], items[0]["title"], items[1]["title"]]
    assert (outcome.diagnostics.kept_count, outcome.diagnostics.over_limit_count) == (3, 1)
    assert [(dropped["index"], dropped["reasons"]) for dropped in outcome.diagnostics.dropped_items] == [(2, ["OVER_LIMIT"])]

    # A Wiki that declares no routing reads the reply in the order the model used, as before.
    plain = _plan(items, max_write_set=3)
    assert [write.title for write in plain.planned_writes] == [item["title"] for item in items[:3]]


# ---- the run, on a Wiki that declares the routing


def test_a_configuration_increment_pauses_the_run_for_a_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _deposit_runtime(tmp_path)  # under `smart` only a sensitive write pauses the run, and this is one
    _register_troubleshooting(rt)
    target = tmp_path / "deposit-wiki" / DATABASE_REL
    before = target.read_bytes()
    model = _DepositModel([_config_item()])
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", model)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:plan-1"])

    assert run.status == RunStatus.AWAITING_APPROVAL, run.error
    (write,) = run.planned_writes
    assert (write.action, write.relative_path, write.requires_approval) == ("update", DATABASE_REL, True)
    assert is_sensitive_write(write) is True
    assert "项目配置页属于敏感写入" in write.approval_reason
    assert target.read_bytes() == before  # nothing is written while the run waits

    # What the review saw, what the planner was told and what it planned line up.
    review = _node(run, "P02_REVIEW").payload["findings"][0]["domain_signals"]
    assert (review["database_tables"][0]["table"], review["project_configs"][0]["topic"]) == ("orders", "database")
    assert model.prompts[0].startswith("You are the P02 Review Worker")
    assert '"deposit_routing"' in model.prompts[1] and '"domain_page_sections"' in model.prompts[1]
    (routed,) = _payload_of(rt, run, "planned_doc_routed")["routed_docs"]
    assert (routed["route"]["domain"], routed["route"]["incremental"], routed["route"]["sensitive"]) == ("项目配置", True, True)
    assert "其中 1 项为项目领域页（增量更新 1、新建 0、敏感待审批 1）" in _node(run, "P03_ROUTE_PLAN").summary

    finished = rt.orchestrator.approve_and_resume(run.run_id)

    assert finished.status == RunStatus.PASS, finished.error
    after = target.read_bytes()
    assert b"\r" not in after
    assert "- 逻辑库 payments 与 orders 不在同一分片，跨库关联只能在应用层做。" in after.decode("utf-8")
    assert preserves_existing(before.decode("utf-8"), after.decode("utf-8"))
    assert rt.db.get_session("codex:plan-1").status == SessionStatus.PROCESSED


def test_a_rejected_configuration_increment_leaves_the_page_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _deposit_runtime(tmp_path)
    _register_troubleshooting(rt)
    target = tmp_path / "deposit-wiki" / DATABASE_REL
    before = target.read_bytes()
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _DepositModel([_config_item()]))

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:plan-1"])
    assert run.status == RunStatus.AWAITING_APPROVAL, run.error

    rejected = rt.orchestrator.reject_run(run.run_id, "配置事实待核对")

    assert rejected.status == RunStatus.CANCELLED
    assert target.read_bytes() == before
    assert rt.db.get_session("codex:plan-1").status == SessionStatus.PENDING

# ---------------------------------------------------------------- document depth, events and the two quotas
# (feat-agy-ingest-and-high-fidelity-distill)

INCIDENT_ROOT = "wiki-知识库/incidents-事故台账"
INCIDENT_DIRECTORY = f"{INCIDENT_ROOT}/ai-incidents"
DOMAIN_PAGE_TYPES = frozenset({"project-algorithm", "project-architecture", "project-config"})


def _event(kind: str, title: str, session_key: str = "codex:s-1") -> dict[str, str]:
    return {"event_id": f"ev-{kind}", "kind": kind, "title": title, "summary": f"{title}：做了什么、确立了什么。", "session_key": session_key}


EVENTS = [
    _event("delivery", "交付：对账任务"),
    _event("incident", "事故：静默改错测试库配置"),
    _event("decision", "决策：金额以分为单位"),
    _event("domain_fact", "事实：ledger_entries 主键"),
]


def _ledger(writable: bool = True) -> RoutingCandidates:
    """The candidates of a Wiki whose contract declares the incident ledger; closed unless ``writable``."""
    return RoutingCandidates(
        page_types=["project-practice-note", *(["incident"] if writable else [])],
        projects=[],
        evidence_page_types=["project-practice-note"],
        incident=IncidentRouting(directory=INCIDENT_DIRECTORY, writable=writable),
    )


def _candidates_named(name: str, tmp_path: Path) -> RoutingCandidates | None:
    """Three Wikis a plan may be written for: no contract, cards only, and one that declares domain pages."""
    if name == "no-contract":
        return None
    if name == "cards-only":
        return _card_only_candidates()
    return _deposit_gate(tmp_path).routing_candidates()


# ---- the planning contract


@pytest.mark.parametrize("wiki", ["no-contract", "cards-only", "deposit"])
def test_the_plan_asks_for_a_complete_document_and_never_a_shallow_summary(tmp_path: Path, wiki: str):
    candidates = _candidates_named(wiki, tmp_path)

    prompt, _ = _planner_input({**_signalled_review(), "events": EVENTS}, candidates)

    assert "220-450" not in prompt
    depth = prompt[prompt.index("DOCUMENT DEPTH:") : prompt.index("Allowed target_kind values:")]
    for needle in (
        "written for a reader who was not in the session",
        "must be able to act without the transcript",
        "the complete structured body of the document",
        "every name, value, step, condition and boundary the session verified, each stated once",
        "no restatement of the review, no filler",
        "Its length follows its content",
        "a project experience card (实战经验卡) stays within 80 lines and a lite card (轻记卡) within 20 lines",
        "only the difference the session verified (已证差量)",
    ):
        assert needle in depth, needle
    assert "content_preview=the complete structured markdown body of the document (see DOCUMENT DEPTH)" in prompt
    # The format repair carries the same contract.
    for hint in (P03_REPAIR_SCHEMA_HINT, P03_REPAIR_SCHEMA_HINT_WITH_ROUTE):
        assert "220-450" not in hint


def test_the_events_of_a_review_enter_the_plan_prompt_only_when_there_are_any():
    candidates = _card_only_candidates()
    plain = _signalled_review()
    assert set(plain) == {"findings", "eligible_promotions", "repeated_workflows"}  # a review may hold no event at all

    baseline, baseline_payload = _planner_input(plain, candidates)
    assert "events" not in baseline_payload and "EVENTS:" not in baseline

    prompt, payload = _planner_input({**plain, "events": EVENTS}, candidates)

    assert payload["events"] == EVENTS
    assert {key: value for key, value in payload.items() if key != "events"} == baseline_payload
    paragraph = prompt[prompt.index("EVENTS:") : prompt.index("DOCUMENT DEPTH:")]
    for needle in (
        '"events" in the input: event_id, kind, title, summary, session_key',
        "Plan by event, not by session",
        "a delivery or a decision event that deserves a document gets an experience card of its own, built from that event alone",
        "a domain_fact event is recorded once",
        "Two events are never merged into one card",
        "an incident card must not swallow the business a session delivered",
        "a delivery's card must not carry the incident that happened on the way",
        'Copy the event\'s session_key into "source_session_keys"',
        "name the event in the title",
    ):
        assert needle in paragraph, needle
    # The paragraph is all the events add to the instructions.
    assert prompt.replace(paragraph, "", 1).rsplit("\n\n", 1)[0] == baseline.rsplit("\n\n", 1)[0]

    # Nothing usable in the field: the prompt is the one a review without the key always yielded, byte for byte.
    for nothing in (None, [], ["not an object", 3, None]):
        assert _planner_input({**plain, "events": nothing}, candidates) == (baseline, baseline_payload)


@pytest.mark.parametrize("ledger", [None, False, True], ids=["no-ledger", "ledger-closed", "ledger-open"])
def test_an_incident_event_is_planned_by_whether_the_ledger_is_open(ledger):
    candidates = _card_only_candidates()
    if ledger is not None:
        candidates.incident = IncidentRouting(directory=INCIDENT_DIRECTORY, writable=ledger)
        if ledger:
            candidates.page_types.append("incident")
    prompt, _ = _planner_input({**_signalled_review(), "events": [_event("incident", "事故：静默改错测试库配置")]}, candidates)
    said = "and name the event in the title."
    clause = prompt[prompt.index(said) + len(said) :]
    clause = clause[: clause.index("\n")]

    if ledger:
        assert clause == (
            " An incident event becomes one incident card of its own, as INCIDENT PAGES describes, in addition to "
            "whatever else its session deserves."
        )
        assert "INCIDENT PAGES:" in prompt
    else:
        assert clause == (
            ' This Wiki offers no incident page (see routing_candidates.page_types): an incident stays in the '
            "review's mistakes, and no knowledge_doc may claim the page type \"incident\"."
        )
        assert "INCIDENT PAGES" not in prompt
    # A Wiki without a contract at all is told the same.
    (without_contract, _) = _planner_input({**_signalled_review(), "events": EVENTS[:1]}, None)
    assert "EVENTS:" in without_contract and "INCIDENT PAGES" not in without_contract


def test_the_plan_is_told_where_the_ledger_lies_only_while_it_is_open():
    prompt, payload = _planner_input(_signalled_review(), _ledger())

    assert payload["routing_candidates"] == {
        "page_types": ["project-practice-note", "incident"],
        "projects": [],
        "incident_pages": {
            "page_type": "incident",
            "directory": f"{INCIDENT_DIRECTORY}/",
            "file_name": "<YYYY-MM-DD>-<slug>.md",
            "sections": list(INCIDENT_SECTIONS),
        },
    }
    paragraph = prompt[prompt.index("INCIDENT PAGES:") : prompt.index("DOCUMENT DEPTH:")]
    for needle in (
        f'"relative_path" is "{INCIDENT_DIRECTORY}/<YYYY-MM-DD>-<slug>.md"',
        '"NNN-YYYY-MM-DD-<slug>.md"',
        '"wiki_route" is {"page_type": "incident", "project": null, "reason": "..."}',
        "the one case in which a null project is routed",
        'whose "action" is "create"',
        'no "# " title',
        "frontmatter is optional",
        "the engine completes what the contract requires and keeps a value the draft states",
        "exactly these level-2 sections, in this order",
        '"## 定性"',
        '"## 事实核验"',
        '"## 根因"',
        '"## 治本规则"',
        '"## 证据指针"',
        "never a credential",
        "State only what the session shows",
        "say so instead of guessing",
        "leave a section you cannot fill empty rather than inventing it",
        "An incident page is a sensitive write: it is flagged for a human approval.",
    ):
        assert needle in paragraph, needle
    assert paragraph.index('"## 定性"') < paragraph.index('"## 事实核验"') < paragraph.index('"## 根因"') < paragraph.index('"## 治本规则"') < paragraph.index('"## 证据指针"')
    assert '"routing_candidates.incident_pages"' in paragraph
    assert prompt.index("WIKI ROUTING:") < prompt.index("INCIDENT PAGES:") < prompt.index("DOCUMENT DEPTH:") < prompt.index("Allowed target_kind values:")

    # A closed ledger leaves no trace in the prompt: the Wiki reads as one that declares no incident page type.
    closed_prompt, closed_payload = _planner_input(_signalled_review(), _ledger(writable=False))
    assert "INCIDENT PAGES" not in closed_prompt
    assert "incident_pages" not in json.dumps(closed_payload, ensure_ascii=False)
    assert closed_payload["routing_candidates"]["page_types"] == ["project-practice-note"]
    no_ledger = RoutingCandidates(page_types=["project-practice-note"], evidence_page_types=["project-practice-note"])
    assert closed_prompt == _planner_input(_signalled_review(), no_ledger)[0]


def test_the_paragraphs_keep_their_order_on_a_wiki_that_offers_everything(tmp_path: Path):
    candidates = _deposit_gate(tmp_path).routing_candidates()
    candidates.incident = IncidentRouting(directory=INCIDENT_DIRECTORY, writable=True)
    candidates.page_types = [*candidates.page_types, "incident"]

    prompt, payload = _planner_input({**_signalled_review(), "events": EVENTS}, candidates)

    order = [prompt.index(marker) for marker in ("WIKI ROUTING:", "PROJECT DOMAIN PAGES:", "INCIDENT PAGES:", "EVENTS:", "DOCUMENT DEPTH:", "Allowed target_kind values:")]
    assert order == sorted(order)
    assert payload["routing_candidates"]["page_types"] == ["project-practice-note", "project-algorithm", "project-architecture", "project-config", "incident"]
    assert payload["routing_candidates"]["incident_pages"]["directory"] == f"{INCIDENT_DIRECTORY}/"
    # No cap of its own passed in (a caller that names none): the single cap the prompt always had.
    assert "IMPORTANT: Output at most 5 total items. " in prompt
    assert "Only inside the item cap, plan one" in prompt


# ---- the two quotas


def _table_page(table: str, session_key: str = "codex:s-1") -> dict[str, Any]:
    """A plan item for a table page of project alpha that does not exist yet."""
    item = _table_item()
    item.update(
        title=f"{table} 表结构",
        summary=f"新表 {table} 的实战已证字段",
        relative_path=f"{PROJECTS}/{ALPHA_DIR}/数据库结构/{table}.md",
        source_session_keys=[session_key],
        content_preview=(
            f"## 用途一行\n\n{table} 表：一行一条记录。\n\n"
            "## 字段语义表\n\n| 字段 | 类型 | 含义 | 备注 |\n| --- | --- | --- | --- |\n| id | bigint | 主键 | 自增 |\n"
        ),
    )
    return item


def _card(number: int) -> dict[str, Any]:
    return _doc(title=f"经验卡{number}", slug=f"card-{number}", with_summary=True)


def _card_item(number: int) -> dict[str, Any]:
    """A routable experience card of project alpha in the deposit Wiki."""
    title = f"连库排查复盘{number}"
    return {
        "target_kind": "knowledge_doc",
        "action": "create",
        "title": title,
        "summary": f"沉淀第 {number} 次连库排查的经验。",
        "relative_path": f"{PROJECTS}/2026-11-27-{title}.md",
        "source_session_keys": ["codex:plan-1"],
        "content_preview": (
            f"---\ntitle: {title}\ntags: [alpha]\n---\n\n# {title}\n"
            "- 未支付订单的 paid_at 为 NULL，不能拿它当时间过滤条件。\n"
            "- payments 与 orders 不在同一分片，跨库关联只能在应用层做。\n"
        ),
        "wiki_route": {"page_type": "project-practice-note", "project": ALPHA_DIR, "reason": "本项目订单表排查的复盘"},
    }


def _plan_of(items: list[Any], *, other: int, page_types: frozenset[str], domain: int | None = None, total: int | None = None):
    """Read a reply under the given caps: the two quotas when ``domain`` is given, the single cap otherwise."""
    return _plan_from_items(
        items,
        source=PLAN_SOURCE_MODEL,
        max_write_set=other,
        default_doc_prefix="lessons/",
        skills_root=None,
        domain_page_types=page_types,
        max_domain_writes=domain,
        total_write_set=total,
        model="strong-plan-model" if domain is not None else "",
    )


def _split_plan(items: list[Any], *, other: int = 4, domain: int = 6, total: int = 12):
    return _plan_of(items, other=other, page_types=DOMAIN_PAGE_TYPES, domain=domain, total=total)


def _titles(outcome: Any) -> list[str]:
    return [write.title for write in outcome.planned_writes]


def _dropped(outcome: Any) -> list[tuple[int, list[str]]]:
    return [(dropped["index"], dropped["reasons"]) for dropped in outcome.diagnostics.dropped_items]


def test_a_card_and_four_table_pages_are_all_planned_under_the_two_quotas():
    """规格场景：1 张经验卡 + 4 张表结构页，other=4、domain=6、total=12，全部进入计划。"""
    tables = [_table_page(table) for table in ("payments", "invoices", "ledger_entries", "payouts")]
    items = [*tables, _card(1)]  # the reply puts the domain pages first

    outcome = _split_plan(items)

    assert _titles(outcome) == ["经验卡1", *(item["title"] for item in tables)]
    assert (outcome.diagnostics.kept_count, outcome.diagnostics.over_limit_count) == (5, 0)
    assert outcome.diagnostics.dropped_items == []
    assert outcome.diagnostics.write_caps == {"other": 4, "domain": 6, "total": 12}
    assert outcome.diagnostics.model == "strong-plan-model"
    assert [str((write.wiki_route or {}).get("page_type") or "") for write in outcome.planned_writes] == ["", *["project-algorithm"] * 4]

    # Under the one cap this reply loses a page, and the plan states no caps of its own.
    single = _plan_of(items, other=4, page_types=DOMAIN_PAGE_TYPES)
    assert _titles(single) == ["经验卡1", *(item["title"] for item in tables[:3])]
    assert (single.diagnostics.kept_count, single.diagnostics.over_limit_count) == (4, 1)
    assert _dropped(single) == [(3, ["OVER_LIMIT"])]
    assert single.diagnostics.write_caps is None
    assert single.diagnostics.model == ""


def test_each_quota_counts_its_own_kind_and_the_whole_plan_stays_within_the_write_set():
    tables = [_table_page(table) for table in ("payments", "invoices", "settlements", "payouts", "refunds", "ledger_entries", "fees")]
    cards = [_card(number) for number in range(1, 7)]

    # Everything else over its own cap: the table pages are not pushed out by the cards.
    outcome = _split_plan([*cards, tables[0], tables[1]])
    assert _titles(outcome) == [card["title"] for card in cards[:4]] + [tables[0]["title"], tables[1]["title"]]
    assert _dropped(outcome) == [(4, ["OVER_LIMIT"]), (5, ["OVER_LIMIT"])]

    # Domain pages over theirs: the card keeps its slot and the seventh table page waits for another batch.
    outcome = _split_plan([*tables, cards[0]])
    assert _titles(outcome) == [cards[0]["title"], *(item["title"] for item in tables[:6])]
    assert _dropped(outcome) == [(6, ["OVER_LIMIT"])]

    # The bound over both kinds together: four cards, then two of the six table pages.
    outcome = _split_plan([*cards, *tables[:6]], total=6)
    assert _titles(outcome) == [card["title"] for card in cards[:4]] + [item["title"] for item in tables[:2]]
    assert [dropped[0] for dropped in _dropped(outcome)] == [4, 5, 8, 9, 10, 11]
    assert {tuple(dropped[1]) for dropped in _dropped(outcome)} == {("OVER_LIMIT",)}
    assert len(outcome.planned_writes) == outcome.diagnostics.write_caps["total"] == 6

    # A domain cap of 0 plans no domain page at all; the other items are untouched by it.
    outcome = _split_plan([tables[0], cards[0], tables[1]], domain=0)
    assert _titles(outcome) == [cards[0]["title"]]
    assert _dropped(outcome) == [(0, ["OVER_LIMIT"]), (2, ["OVER_LIMIT"])]
    assert outcome.diagnostics.write_caps == {"other": 4, "domain": 0, "total": 12}


def test_a_reply_of_domain_pages_only_fails_the_plan_when_their_cap_is_zero():
    """The standing contract: a plan of which nothing is listed fails the node, it never passes as an empty one."""
    tables = [_table_page("payments"), _table_page("invoices")]

    with pytest.raises(PlanUnavailableError) as refused:
        _split_plan(tables, domain=0)

    assert "模型返回的写入计划 2 项全部未列入（2 项超出单批上限），本批不写入。" in str(refused.value)
    assert (refused.value.diagnostics.over_limit_count, refused.value.diagnostics.kept_count) == (2, 0)
    assert refused.value.diagnostics.write_caps == {"other": 4, "domain": 0, "total": 12}

    # With room for one, the plan holds one and says which page waited.
    outcome = _split_plan(tables, domain=1)
    assert _titles(outcome) == ["payments 表结构"]
    assert _dropped(outcome) == [(1, ["OVER_LIMIT"])]


def test_an_invalid_item_spends_no_slot_of_either_quota():
    invalid = {"target_kind": "knowledge_doc", "action": "create"}  # no title and no summary to complete them from
    unusable = ["FIELD_MISSING: title", "FIELD_MISSING: summary"]
    invalid_page = {**invalid, "wiki_route": {"page_type": "project-algorithm", "project": "alpha"}}

    # Two slots for everything else, an invalid item ahead of each good one.
    outcome = _split_plan([invalid, _card(1), invalid, _card(2)], other=2)
    assert _titles(outcome) == ["经验卡1", "经验卡2"]
    assert _dropped(outcome) == [(0, unusable), (2, unusable)]
    assert outcome.diagnostics.over_limit_count == 0

    # The same for the domain quota.
    outcome = _split_plan([invalid_page, _table_page("payments"), invalid_page, _table_page("invoices")], domain=2)
    assert _titles(outcome) == ["payments 表结构", "invoices 表结构"]
    assert [dropped[1] for dropped in _dropped(outcome)] == [unusable, unusable]
    assert outcome.diagnostics.over_limit_count == 0

    # A third good item does meet the quota the invalid items left untouched.
    outcome = _split_plan([invalid, _card(1), _card(2), _card(3)], other=2)
    assert _titles(outcome) == ["经验卡1", "经验卡2"]
    assert _dropped(outcome) == [(0, unusable), (3, ["OVER_LIMIT"])]


def test_the_runner_output_path_counts_under_the_same_two_quotas():
    parsed = {"planned_writes": [_table_page("payments"), _table_page("invoices"), _table_page("payouts"), _table_page("fees"), _card(1)]}

    outcome = _plan_from_runner_output(
        parsed,
        source=PLAN_SOURCE_MODEL,
        max_write_set=4,
        default_doc_prefix="lessons/",
        skills_root=None,
        domain_page_types=DOMAIN_PAGE_TYPES,
        max_domain_writes=6,
        total_write_set=12,
        model="strong-plan-model",
    )

    assert len(outcome.planned_writes) == 5
    stated = outcome.diagnostics.to_dict()
    assert (stated["write_caps"], stated["model"]) == ({"other": 4, "domain": 6, "total": 12}, "strong-plan-model")

    # The page types the Wiki declares decide what counts as a domain page: without them one cap covers all.
    plain = _plan_from_runner_output(parsed, source=PLAN_SOURCE_MODEL, max_write_set=4, default_doc_prefix="lessons/", skills_root=None)
    assert _titles(plain) == [item["title"] for item in parsed["planned_writes"][:4]]
    assert plain.diagnostics.write_caps is None


def test_plan_diagnostics_name_the_model_and_the_caps_they_counted_under():
    stated = PlanDiagnostics(source=PLAN_SOURCE_MODEL, model="strong-plan-model", write_caps={"other": 4, "domain": 6, "total": 12}).to_dict()
    assert (stated["model"], stated["write_caps"]) == ("strong-plan-model", {"other": 4, "domain": 6, "total": 12})

    bare = PlanDiagnostics(source=PLAN_SOURCE_MODEL).to_dict()
    assert (bare["model"], bare["write_caps"]) == ("", None)


def test_the_caps_come_from_the_configuration_and_never_exceed_the_write_set(tmp_path: Path):
    assert WorkflowConfig().max_domain_writes_per_run == 8
    for out_of_range in (-1, 33):
        with pytest.raises(ValidationError):
            WorkflowConfig(max_domain_writes_per_run=out_of_range)

    cfg = _deposit_config(tmp_path)
    offered = _deposit_gate(tmp_path).routing_candidates()

    assert _plan_write_caps(cfg, offered) == (8, 8)  # the defaults: eight domain pages inside a write set of eight
    cfg.workflow.max_domain_writes_per_run = 20
    assert _plan_write_caps(cfg, offered) == (8, 8)  # cut to the sink's bound
    cfg.knowledge_sink.max_write_set = 3
    assert _plan_write_caps(cfg, offered) == (3, 3)
    cfg.workflow.max_domain_writes_per_run = 0
    assert _plan_write_caps(cfg, offered) == (0, 3)
    # A plan that can hold no domain page is counted by the single cap it always had.
    for without_domain_pages in (None, _card_only_candidates(), RoutingCandidates()):
        assert _plan_write_caps(cfg, without_domain_pages) == (None, None)


def test_both_runners_tell_the_planner_the_caps_they_count_under(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    offered = _deposit_gate(tmp_path).routing_candidates()
    rt, _ = _runtime(tmp_path)
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    seen: list[str] = []

    def chat(prompt: str, system_prompt: str) -> str:
        seen.append(prompt)
        return '{"planned_writes": []}'

    runner._call_chat_completion = chat
    rt.cfg.workflow.max_domain_writes_per_run = 6
    rt.cfg.knowledge_sink.max_write_set = 12
    runner.plan_writes_detailed(_signalled_review(), [], 4, routing_candidates=offered)

    (prompt,) = seen
    both = "IMPORTANT: Output at most 4 items that are not project-domain pages, and at most 6 project-domain pages (12 items in all). "
    assert both in prompt
    assert "Domain pages are counted under a cap of their own, apart from every other item (see IMPORTANT below). " in prompt
    assert 'Plan one "knowledge_doc" per domain page that the findings\' "domain_signals" name, at most 6 of them, and only for a fact the session verified.' in prompt

    # A Wiki that declares no domain page keeps the one sentence it had.
    seen.clear()
    runner.plan_writes_detailed(_signalled_review(), [], 4, routing_candidates=_card_only_candidates())
    assert "IMPORTANT: Output at most 4 total items. " in seen[0]
    assert "project-domain pages, and at most" not in seen[0]

    # A domain cap of 0 is said in so many words.
    seen.clear()
    rt.cfg.workflow.max_domain_writes_per_run = 0
    runner.plan_writes_detailed(_signalled_review(), [], 4, routing_candidates=offered)
    assert "IMPORTANT: Output at most 4 items that are not project-domain pages, and at most 0 project-domain pages (12 items in all). " in seen[0]
    assert "No domain page is planned in this batch: their cap is 0." in seen[0]

    # The local CLI runner plans under the same caps.
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "mock-cli"
    cfg.workflow.max_domain_writes_per_run = 6
    cfg.knowledge_sink.max_write_set = 12
    cli = CliSubprocessRunner(cfg, ImmutableFileStore(tmp_path / "cli-receipts"))
    cli_prompts: list[str] = []
    reply = {"planned_writes": [_table_page(table) for table in ("payments", "invoices", "payouts", "fees", "refunds")] + [_card(1)]}

    def invoke(stage: str, prompt: str, reply_trace: dict[str, Any] | None = None) -> dict[str, Any]:
        cli_prompts.append(prompt)
        return reply

    monkeypatch.setattr(cli, "_invoke_cli", invoke)
    outcome = cli.plan_writes_detailed(_signalled_review(), [], 4, routing_candidates=offered)

    (cli_prompt,) = cli_prompts
    assert both in cli_prompt
    assert len(outcome.planned_writes) == 6  # one card under its cap, five table pages under theirs
    assert outcome.diagnostics.write_caps == {"other": 4, "domain": 6, "total": 12}
    assert outcome.diagnostics.source == "cli"


def test_an_incident_card_is_flagged_for_a_human_before_anyone_approves_it():
    def incident_item(**overrides: Any) -> dict[str, Any]:
        item = _doc(title="静默改错测试库配置", slug="wrong-config", with_summary=True)
        item.update(
            relative_path=f"{INCIDENT_DIRECTORY}/2026-11-20-wrong-config.md",
            wiki_route={"page_type": "incident", "project": None, "reason": "一次协作事故"},
        )
        item.update(overrides)
        return item

    def plan(item: dict[str, Any]):
        return _plan_from_items([item], source=PLAN_SOURCE_MODEL, max_write_set=8, default_doc_prefix="lessons/", skills_root=None).planned_writes

    (card,) = plan(incident_item())
    assert (card.requires_approval, card.approval_reason) == (True, "事故卡属于敏感写入，需人工确认后才会落盘")
    assert is_sensitive_write(card) is True

    # The model's own reason is kept; the flag is set all the same, however the page type is spelled.
    (kept,) = plan(incident_item(approval_reason="改错了测试库配置，未告知"))
    assert (kept.requires_approval, kept.approval_reason) == (True, "改错了测试库配置，未告知")
    (spelled,) = plan(incident_item(wiki_route={"page_type": " Incident ", "project": None, "reason": "一次协作事故"}))
    assert (spelled.requires_approval, is_sensitive_write(spelled)) == (True, True)

    # A card of any other type is not touched.
    (routine,) = plan(_doc(with_summary=True))
    assert (routine.requires_approval, routine.approval_reason, is_sensitive_write(routine)) == (False, "", False)


def test_write_prefixes_open_a_directory_only_by_name():
    ledger = f"{INCIDENT_ROOT}/ai-incidents"

    assert write_prefix_allows(ledger, ["wiki-知识库"]) is True
    assert write_prefix_allows(ledger, ["wiki-知识库/incidents-事故台账"]) is True
    assert write_prefix_allows(ledger, ["/wiki-知识库\\incidents-事故台账"]) is True
    assert write_prefix_allows(ledger, ["wiki-知识库/incidents-事故台账/ai-incidents"]) is True
    assert write_prefix_allows(ledger, ["wiki-知识库/incidents-事故台账/ai-incidents-archive"]) is False  # a sibling is not the ledger
    assert write_prefix_allows(ledger, ["wiki-知识库/projects-项目"]) is False
    assert write_prefix_allows(ledger, []) is False  # an empty list restrains nothing, and so opens nothing by name
    assert write_prefix_allows(ledger, None) is False


# ---- the run, under both quotas

# The project index page, as the deposit contract of this file does not declare it: a card
# is written together with its entry there, so an approved card needs the page type.
INDEX_PAGE_TYPE = (
    "    project-practice-index:\n"
    "      partition: projects\n"
    '      path_pattern: "^[^/]+/实战笔记/笔记索引\\\\.md$"\n'
    "      allowed_status: [active, deprecated, archived]\n"
)


def _runtime_whose_index_page_is_declared(tmp_path: Path) -> FlywheelRuntime:
    """The deposit runtime, under a contract that also declares the project index page.

    An experience card is written together with its entry in the project index. The plain
    deposit contract declares no such page type, so an approved card could never land.
    """
    rt = _deposit_runtime(tmp_path)
    marker = "    project-practice-note:\n"
    extended = DEPOSIT_SCHEMA.replace(marker, INDEX_PAGE_TYPE + marker, 1)
    assert extended != DEPOSIT_SCHEMA
    (tmp_path / "deposit-wiki" / ".wiki-schema.md").write_bytes(extended.encode("utf-8"))  # read at the first use
    return rt


def test_a_card_and_four_table_pages_fill_the_two_quotas_and_still_ask_for_a_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """规格场景：max_writes_per_run=4、max_domain_writes_per_run=6、knowledge_sink.max_write_set=12 时，
    1 张经验卡 + 4 张表结构页全部进入计划，高写入阈值按最终总数触发。"""
    rt = _runtime_whose_index_page_is_declared(tmp_path)  # `smart`: only a sensitive write or a large batch pauses the run
    rt.cfg.workflow.max_writes_per_run = 4
    rt.cfg.workflow.max_domain_writes_per_run = 6
    rt.cfg.knowledge_sink.max_write_set = 12
    rt.cfg.workflow.high_write_count_threshold = 4
    _register_troubleshooting(rt)
    project_dir = tmp_path / "deposit-wiki" / PROJECTS / ALPHA_DIR
    table_dir = project_dir / "数据库结构"
    pages = ["payments", "invoices", "ledger_entries", "payouts"]
    model = _DepositModel([*(_table_page(table, "codex:plan-1") for table in pages), _card_item(1)])
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", model)
    before = sorted(path.name for path in table_dir.iterdir())
    assert before == [".draft-payments.md", "archive-refunds.md", "orders.md"]

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:plan-1"])

    assert run.status == RunStatus.AWAITING_APPROVAL, run.error
    assert [write.title for write in run.planned_writes] == [_card_item(1)["title"], *(_table_page(page)["title"] for page in pages)]
    assert all(not is_sensitive_write(write) for write in run.planned_writes)  # the count alone holds the batch
    assert _payload_of(rt, run, "awaiting_approval")["reasons"] == ["本批建议写入共 5 项（超过自动放行阈值 4）"]
    p03 = _node(run, "P03_ROUTE_PLAN")
    assert p03.status == NodeStatus.PASS
    assert p03.payload["plan_diagnostics"]["write_caps"] == {"other": 4, "domain": 6, "total": 12}
    assert "其中 4 项为项目领域页（增量更新 0、新建 4、敏感待审批 0）" in p03.summary
    assert "IMPORTANT: Output at most 4 items that are not project-domain pages, and at most 6 project-domain pages (12 items in all). " in model.prompts[1]
    assert sorted(path.name for path in table_dir.iterdir()) == before  # nothing while it waits

    finished = rt.orchestrator.approve_and_resume(run.run_id)

    assert finished.status == RunStatus.PASS, finished.error
    assert sorted(path.name for path in table_dir.iterdir()) == sorted([*before, *(f"{page}.md" for page in pages)])
    practice = project_dir / "实战笔记"
    (card,) = [path for path in practice.iterdir() if path.name != "笔记索引.md"]
    assert card.name.endswith("-连库排查复盘1.md")  # the card landed under the day it was written
    assert "连库排查复盘1" in (practice / "笔记索引.md").read_text(encoding="utf-8")  # and its entry joined the index
    assert rt.db.get_session("codex:plan-1").status == SessionStatus.PROCESSED


def test_p03_drops_credential_in_metadata_and_never_calls_cache_persist_or_caches_private_blocks(
    tmp_path: Path, heuristic_planner_forbidden: None
):
    """P03 must block secrets in PlannedWrite metadata (`title`/`summary`/`wiki_route`/`rule_payload`),
    never invoke cache persist when refused, and never cache replies carrying `<private>`."""
    from ai_flywheel.adapters.runners.clients import _cache_plan_reply

    runner = _api_runner(tmp_path)
    persist_calls: list[Path] = []
    real_persist = runner._persist_cache

    def _spy_persist(cache_file: Path, parsed: Any) -> bool:
        persist_calls.append(cache_file)
        return real_persist(cache_file, parsed)

    runner._persist_cache = _spy_persist  # type: ignore[method-assign]
    reply = {
        "planned_writes": [
            _doc(with_summary=True),
            {**_doc(slug="leaky-title", with_summary=True), "title": f"标题含密钥 {SYNTHETIC_KEY}"},
            {**_doc(slug="leaky-summary", with_summary=True), "summary": f"摘要含密钥 {SYNTHETIC_KEY}"},
            {
                "target_kind": "flywheel_rule",
                "action": "register_rule",
                "title": "规则标题",
                "summary": "规则摘要",
                "content_preview": "- 核对配置",
                "rule_payload": {
                    "title": "规则标题",
                    "checklist": ["核对配置"],
                    "forbidden_routes": [f"禁止 {SYNTHETIC_KEY}"],
                },
            },
        ]
    }
    runner._call_chat_completion = _ReplyModel(reply)
    outcome = runner.plan_writes_detailed(NO_FINDINGS, [], 8)
    assert len(outcome.planned_writes) == 1
    assert outcome.planned_writes[0].title == "接口超时排查复盘"
    assert outcome.diagnostics.cache_file is None
    assert persist_calls == []
    assert _files_under(runner.fs_store.root) == []

    # A reply with `<private>...</private>` is stripped in the planned write and never cached.
    private_doc = {
        **_doc(slug="with-private", with_summary=True),
        "content_preview": "---\ntitle: t\n---\n\n# t\n\n正文 <private>绝密段</private>\n",
    }
    runner._call_chat_completion = _ReplyModel({"planned_writes": [private_doc]})
    priv_outcome = runner.plan_writes_detailed(NO_FINDINGS, [], 8)
    assert len(priv_outcome.planned_writes) == 1
    assert "绝密段" not in priv_outcome.planned_writes[0].content_preview
    assert priv_outcome.diagnostics.cache_file is None
    assert persist_calls == []
    assert _files_under(runner.fs_store.root) == []
    # Direct unit guard: `_cache_plan_reply` short-circuits when diagnostics already refused a secret.
    assert _cache_plan_reply(_spy_persist, tmp_path / "x.json", {"planned_writes": []}, outcome) is False
    assert persist_calls == []
