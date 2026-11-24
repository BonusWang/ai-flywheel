"""P03 写入计划契约（fix-p03-silent-plan-fallback）。

夹具按真实缓存输出 `p03_plan-*.json` 的形状脱敏构造：改写提示词之后的输出普遍没有 `summary`，
更早的两个批次连 `title` 也没有；规则项只有 `rule_payload`，Skill 项带 `skill_route` 与 frontmatter。
内容全部是虚构示例，不含任何真实会话、项目或技能名。
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

from ai_flywheel.adapters.runners import (
    P03_ITEM_REQUIRED_KEYS,
    P03_REPAIR_SCHEMA_HINT,
    PLAN_SOURCE_CLI,
    PLAN_SOURCE_HEURISTIC,
    PLAN_SOURCE_MODEL,
    CliSubprocessRunner,
    DryRunHeuristicRunner,
    OpenAICompatibleRunner,
    PlanUnavailableError,
    _build_p03_prompt,
    build_runner,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import NodeExecution, NodeStatus, RegisterSessionRequest, RunStatus, SessionStatus, WorkflowRunRecord
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
