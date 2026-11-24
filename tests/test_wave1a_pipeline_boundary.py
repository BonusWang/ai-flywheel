"""Wave 1A（W1.2）回归测试：流水线副作用边界、缓存时机、隐私前置门与写入校验。

覆盖：
- W1.2.1 P02 只读：错题候选与分歧候选留在 P02 载荷里，审批通过后才在 P04 落库；
  P03 失败、用户取消、审批挂起时，错题池与分歧表必须零改动。
- W1.2.2 缓存时机：只有通过语义校验的回复才写缓存；磁盘上已有的坏缓存仍要指名。
- W1.2.3 P03 隐私前置门（`<private>` 脱敏、密钥整项丢弃）、配额按"留下的写入数"计、
  提示词按配置上限截断、P04 对非法写入项显式报错并留下部分写入回执、整批只重建一次索引。

模型回复、文档正文与项目名全部是虚构示例。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from ai_flywheel.adapters.runners import (
    CliSubprocessRunner,
    OpenAICompatibleRunner,
    _build_p03_prompt,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import (
    NodeExecution,
    NodeStatus,
    RegisterSessionRequest,
    RunStatus,
    SessionStatus,
    WorkflowRunRecord,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.fs_store import ImmutableFileStore

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers


def _runtime(tmp_path: Path, *, approval_mode: str = "auto") -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills)]
    cfg.workflow.approval_mode = approval_mode
    # `auto` only takes effect with the safety valve open: the valve has the
    # last word, so a closed valve would run the batch as `smart`.
    cfg.safety_valves.auto_approve_writes = approval_mode == "auto"
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


def _finding(session_key: str = "codex:s1") -> dict[str, Any]:
    return {
        "session_key": session_key,
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


def _mistake(root_cause_key: str = "missing-null-guard", *, grade: str = "yellow_caught") -> dict[str, Any]:
    return {
        "root_cause_key": root_cause_key,
        "severity_grade": grade,
        "title": "参数校验缺失",
        "phenomenon": "入参为空时报错",
        "truth": "先校验入参再调用",
        "thinking_root_cause": "沿用了未经验证的假设",
        "caught_by": "self",
        "session_key": "codex:s1",
    }


def _dissent(target_id: str = "rule-cache-check") -> dict[str, Any]:
    return {
        "target_type": "rule",
        "target_id": target_id,
        "counter_claim": "只读本地内存缓存不适用该失效键检查",
        "condition_scope": "project=demo",
        "evidence_pointer": "codex:s1",
        "session_key": "codex:s1",
    }


def _review(
    *,
    mistakes: list[dict[str, Any]] | None = None,
    dissents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "findings": [_finding()],
        "mistakes": mistakes or [],
        "dissents": dissents or [],
        "repeated_workflows": [],
    }


def _doc(title: str = "接口超时排查复盘", *, slug: str = "api-timeout") -> dict[str, Any]:
    return {
        "target_kind": "knowledge_doc",
        "title": title,
        "relative_path": f"lessons/2026-11-25-{slug}.md",
        "action": "create",
        "summary": "记录接口超时的根因与修复方式",
        "content_preview": (
            "---\ntitle: 接口超时排查复盘\n---\n\n"
            f"# {title}\n\n## 事实回顾\n- **接口**在高峰期出现超时，根因是连接池上限过小。\n"
        ),
    }


class _FakeModel:
    """Stands in for the model gateway: a canned review for P02, a canned plan for P03."""

    def __init__(self, review: dict[str, Any], planned_writes: list[Any] | None = None, *, p03_raw: str | None = None) -> None:
        self.review = review
        self.plan_json = "" if planned_writes is None else json.dumps({"planned_writes": planned_writes}, ensure_ascii=False)
        self.p03_raw = p03_raw
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("You are the P02 Review Worker"):
            return json.dumps(self.review, ensure_ascii=False)
        if self.p03_raw is not None:
            # 首次回复与格式修复回复都不可用：P03 必须失败，不得回退
            return self.p03_raw
        return self.plan_json


# ---------------------------------------------------------------- W1.2.1 P02 只读


def test_p02_keeps_the_candidates_in_its_payload_without_writing_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W1.2.1: P02 只产出候选，候选列表写进 P02 载荷（审批挂起/重启后仍在）。"""
    rt = _runtime(tmp_path, approval_mode="always")
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(mistakes=[_mistake()], dissents=[_dissent()]), planned_writes=[_doc()]),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=False)

    assert run.status == RunStatus.AWAITING_APPROVAL
    p02 = _node(run, "P02_REVIEW")
    assert p02.payload["candidate_mistake_count"] == 1
    assert p02.payload["candidate_dissent_count"] == 1
    assert p02.payload["candidate_mistakes"][0]["root_cause_key"] == "missing-null-guard"
    assert p02.payload["candidate_dissents"][0]["target_id"] == "rule-cache-check"
    # 还没有落库
    assert rt.db.list_mistake_observations() == []
    assert rt.db.list_dissent_records() == []

    # 候选写进了运行记录：审批挂起后重启，候选仍在
    reloaded = rt.db.get_workflow_run(run.run_id)
    assert reloaded is not None
    reloaded_p02 = _node(reloaded, "P02_REVIEW")
    assert reloaded_p02.payload["candidate_mistakes"][0]["root_cause_key"] == "missing-null-guard"
    assert reloaded_p02.payload["candidate_dissents"][0]["target_id"] == "rule-cache-check"


def test_a_p03_failure_leaves_the_mistake_pool_and_dissent_table_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """W1.2.1: P03 失败时整批零改动。"""
    rt = _runtime(tmp_path)
    _register(rt)
    model = _FakeModel(_review(mistakes=[_mistake()], dissents=[_dissent()]), planned_writes=[])
    model.p03_raw = "这次没有返回结构化内容。"
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", model)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.FAILED
    assert rt.db.list_mistake_observations() == []
    assert rt.db.list_dissent_records() == []
    assert rt.db.get_session("codex:s1").status == SessionStatus.PENDING


def test_a_rejected_batch_leaves_the_mistake_pool_and_dissent_table_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """W1.2.1: 用户取消放行时整批零改动。"""
    rt = _runtime(tmp_path, approval_mode="always")
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(mistakes=[_mistake()], dissents=[_dissent()]), planned_writes=[_doc()]),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=False)
    assert run.status == RunStatus.AWAITING_APPROVAL

    rejected = rt.orchestrator.reject_run(run.run_id, reason="用户取消放行", source="test")

    assert rejected.status == RunStatus.CANCELLED
    assert rt.db.list_mistake_observations() == []
    assert rt.db.list_dissent_records() == []


def test_p04_persists_the_candidate_mistakes_and_dissents_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """W1.2.1: 审批通过后，P04 才把候选写进错题观察池与分歧表。"""
    rt = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(mistakes=[_mistake()], dissents=[_dissent()]), planned_writes=[_doc()]),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    observations = rt.db.list_mistake_observations()
    assert len(observations) == 1
    assert observations[0]["root_cause_key"] == "missing-null-guard"
    assert observations[0]["occurrence_count"] == 1
    assert observations[0]["status"] == "observing"

    dissents = rt.db.list_dissent_records()
    assert len(dissents) == 1
    assert dissents[0]["target_id"] == "rule-cache-check"

    p04 = _node(run, "P04_WRITE_VERIFY")
    assert p04.payload["persisted_dissent_count"] == 1
    assert p04.payload["persisted_observation_ids"] == [observations[0]["obs_id"]]


def test_a_rule_promotes_the_observation_with_the_same_root_cause_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """W1.2.1: 新注册的规则按根因键回连观察池，并把观察置为 promoted。"""
    rt = _runtime(tmp_path)
    _register(rt)
    rule_item = {
        "target_kind": "flywheel_rule",
        "action": "register_rule",
        "title": "调用前先校验入参",
        "summary": "先校验入参再调用下游",
        "relative_path": None,
        "rule_payload": {
            "title": "调用前先校验入参",
            "domain": "engineering",
            "projects": ["generic"],
            "triggers": ["入参", "校验"],
            "checklist": ["先校验入参非空"],
            "forbidden_routes": ["直接调用下游"],
            "root_cause_key": "missing-null-guard",
        },
    }
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(mistakes=[_mistake()]), planned_writes=[rule_item]),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    observation = rt.db.list_mistake_observations()[0]
    assert observation["status"] == "promoted"
    assert observation["promoted_rule_id"]
    rules = rt.db.list_rules()
    assert len(rules) == 1
    assert observation["promoted_rule_id"] == rules[0].rule_id


# ---------------------------------------------------------------- W1.2.2 缓存时机


def test_a_validated_review_is_cached_for_the_next_identical_batch(tmp_path: Path):
    """W1.2.2: 通过校验的审查会落缓存，第二次同批直接复用。"""
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    runner = OpenAICompatibleRunner(cfg, ImmutableFileStore(tmp_path / "receipts"))
    fake = _FakeModel(_review())
    runner._call_chat_completion = fake  # type: ignore[method-assign]

    runner.review_sessions(_sessions(), [])
    cache_files = list((tmp_path / "receipts" / "api_cache").glob("p02_review-*.json"))
    assert len(cache_files) == 1
    assert json.loads(cache_files[0].read_text(encoding="utf-8"))["findings"]

    runner.review_sessions(_sessions(), [])
    assert len(fake.prompts) == 1  # 第二次命中缓存，没有再问模型


def _sessions() -> list[tuple[Any, str]]:
    from ai_flywheel.models import SessionRecord

    return [
        (
            SessionRecord(
                session_key="codex:s1",
                client="codex",
                session_id="s1",
                title="接口超时排查",
                project="demo",
                summary="记录接口超时的根因",
            ),
            "用户：接口超时怎么排查。助手：先看连接池上限。",
        )
    ]


def test_a_validated_cli_plan_is_cached(tmp_path: Path):
    """W1.2.2: CLI 的有效计划同样在校验通过后才写缓存。"""
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "mock-cli"
    runner = CliSubprocessRunner(cfg, ImmutableFileStore(tmp_path / "cli-receipts"))
    runner._invoke_cli = lambda stage, prompt, **kwargs: {"planned_writes": [_doc()]}  # type: ignore[method-assign]

    outcome = runner.plan_writes_detailed(
        {"findings": [], "eligible_promotions": [], "repeated_workflows": []}, [], 8
    )

    assert len(outcome.planned_writes) == 1
    cache_files = list((tmp_path / "cli-receipts" / "cli_cache").glob("p03_plan-*.json"))
    assert len(cache_files) == 1
    assert json.loads(cache_files[0].read_text(encoding="utf-8"))["planned_writes"]


def test_an_unusable_cli_plan_leaves_no_cache_file(tmp_path: Path):
    """W1.2.2: 不可用的 CLI 输出不写缓存。"""
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "mock-cli"
    runner = CliSubprocessRunner(cfg, ImmutableFileStore(tmp_path / "cli-receipts"))
    runner._invoke_cli = lambda stage, prompt, **kwargs: {"planned_writes": [{"target_kind": "note", "action": "create", "title": "非法项"}]}  # type: ignore[method-assign]

    from ai_flywheel.adapters.runners import PlanUnavailableError

    with pytest.raises(PlanUnavailableError):
        runner.plan_writes_detailed(
            {"findings": [], "eligible_promotions": [], "repeated_workflows": []}, [], 8
        )

    assert list((tmp_path / "cli-receipts" / "cli_cache").glob("p03_plan-*.json")) == []


# ---------------------------------------------------------------- W1.2.3 隐私前置门


def test_a_plan_item_with_a_credential_is_dropped_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """W1.2.3: 计划项里的密钥整项丢弃，并留痕在 plan_items_dropped。"""
    rt = _runtime(tmp_path)
    _register(rt)
    poisoned = _doc("接口超时排查复盘", slug="api-timeout")
    poisoned["content_preview"] = (
        "---\ntitle: 接口超时排查复盘\n---\n\n"
        "# 接口超时排查复盘\n\n"
        "排障时用到的凭据是 sk-placeholder-placeholder-placeholder。\n"
    )
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(), planned_writes=[poisoned]),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    # 这一项被丢弃了，整批没有写入任何东西
    assert run.planned_writes == []
    p03 = _node(run, "P03_ROUTE_PLAN")
    dropped = p03.payload["plan_diagnostics"]["dropped_items"]
    assert [d["reasons"] for d in dropped] == [["SECRET_LEAK_BLOCKED"]]
    assert not list((rt.cfg.knowledge_root_path).rglob("*.md"))


def test_private_spans_are_redacted_in_the_approval_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W1.2.3: `<private>...</private>` 在进入 SQLite 与审批预览之前就被脱敏。"""
    rt = _runtime(tmp_path)
    _register(rt)
    doc = _doc("接口超时排查复盘", slug="api-timeout")
    doc["content_preview"] = (
        "---\ntitle: 接口超时排查复盘\n---\n\n"
        "# 接口超时排查复盘\n\n"
        "<private>客户机房地址与联系人电话</private>\n\n"
        "根因是连接池上限过小。\n"
    )
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(), planned_writes=[doc]),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    planned = run.planned_writes[0]
    assert "<private>" not in planned.content_preview
    assert "客户机房地址与联系人电话" not in planned.content_preview
    assert "[REDACTED_PRIVATE]" in planned.content_preview


# ---------------------------------------------------------------- W1.2.3 配额与提示词


def test_invalid_items_do_not_consume_the_write_quota(tmp_path: Path):
    """W1.2.3: 配额按"留下的写入数"计，非法项不吃掉后面有效项的名额。"""
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    runner = OpenAICompatibleRunner(cfg, ImmutableFileStore(tmp_path / "receipts"))
    items = [
        {"target_kind": "note", "action": "create", "title": "非法项"},
        {"target_kind": "note", "action": "create", "title": "非法项"},
        _doc("第一篇", slug="one"),
        _doc("第二篇", slug="two"),
        _doc("第三篇", slug="three"),
    ]
    runner._call_chat_completion = _FakeModel({}, planned_writes=items)  # type: ignore[method-assign]

    outcome = runner.plan_writes_detailed(
        {"findings": [], "eligible_promotions": [], "repeated_workflows": []}, [], 3
    )

    assert len(outcome.planned_writes) == 3
    assert outcome.diagnostics.kept_count == 3
    assert [d["reasons"] for d in outcome.diagnostics.dropped_items] == [
        ["UNSUPPORTED_TARGET_KIND: note"],
        ["UNSUPPORTED_TARGET_KIND: note"],
    ]


def test_the_p03_prompt_caps_the_items_at_the_configured_quota():
    """W1.2.3: 提示词里的条数上限 = 配置的配额（不是写死的 5），并留位置给经验卡。"""
    prompt = _build_p03_prompt(
        {
            "findings": [{"session_key": "codex:s-1", "worthy": True, "title": "接口超时排查", "facts": ["连接池上限过小"]}],
            "eligible_promotions": [],
            "repeated_workflows": [],
        },
        [],
        [],
        8,
        "lessons/",
        "daily",
    )
    assert "Output at most 8 total items" in prompt
    assert "knowledge_doc" in prompt
    assert "Reserve at least one slot for a worthy knowledge_doc item" in prompt

    prompt_small = _build_p03_prompt(
        {
            "findings": [{"session_key": "codex:s-1", "worthy": True, "title": "接口超时排查", "facts": ["连接池上限过小"]}],
            "eligible_promotions": [],
            "repeated_workflows": [],
        },
        [],
        [],
        1,
        "lessons/",
        "daily",
    )
    assert "Output at most 1 total items" in prompt_small


# ---------------------------------------------------------------- W1.2.3 P04 写入校验


def test_p04_raises_on_a_knowledge_doc_without_a_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W1.2.3: 缺 relative_path 的知识文档必须显式报错，不得静默跳过。"""
    rt = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(), planned_writes=[_doc()]),
    )

    # P03 会给缺路径的知识文档补一个默认路径；这里模拟一份绕过归一化的旧计划
    original = rt.orchestrator._execute_post_approval

    def _patched(run_record, simulate_fail_node=None):  # type: ignore[no-untyped-def]
        run_record.planned_writes[0].relative_path = ""
        return original(run_record, simulate_fail_node=simulate_fail_node)

    monkeypatch.setattr(rt.orchestrator, "_execute_post_approval", _patched)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.FAILED
    p04 = _node(run, "P04_WRITE_VERIFY")
    assert p04.status == NodeStatus.FAILED
    assert "relative_path" in (p04.error or "")


def test_p04_raises_on_an_unknown_target_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W1.2.3: 未知的写入目标类型必须显式报错。"""
    rt = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(
            _review(),
            planned_writes=[
                {
                    "target_kind": "knowledge_doc",
                    "title": "接口超时排查复盘",
                    "relative_path": "lessons/2026-11-25-api-timeout.md",
                    "action": "create",
                    "summary": "记录接口超时的根因与修复方式",
                    "content_preview": "---\ntitle: 接口超时排查复盘\n---\n\n# 接口超时排查复盘\n",
                }
            ],
        ),
    )

    # 直接把一个未知类型的写入项塞进已批准的计划
    original = rt.orchestrator._execute_post_approval

    def _patched(run_record, simulate_fail_node=None):  # type: ignore[no-untyped-def]
        run_record.planned_writes[0].target_kind = "wiki_page"
        return original(run_record, simulate_fail_node=simulate_fail_node)

    monkeypatch.setattr(rt.orchestrator, "_execute_post_approval", _patched)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.FAILED
    p04 = _node(run, "P04_WRITE_VERIFY")
    assert p04.status == NodeStatus.FAILED
    assert "wiki_page" in (p04.error or "")


def test_p04_records_partial_write_receipts_when_a_write_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W1.2.3: 中途失败时，已经写下的部分回执必须留在 P04 载荷里。"""
    rt = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(), planned_writes=[_doc("第一篇", slug="one"), _doc("第二篇", slug="two")]),
    )

    original_write = rt.knowledge.write_document_verified
    calls = {"n": 0}

    def _flaky(relative_path: str, content: str, action: str = "create", **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("磁盘写满")
        return original_write(relative_path, content, action=action, **kwargs)

    monkeypatch.setattr(rt.knowledge, "write_document_verified", _flaky)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.FAILED
    p04 = _node(run, "P04_WRITE_VERIFY")
    payload = p04.payload or {}
    assert payload.get("partial_written_paths")
    assert payload.get("partial_write_receipts")
    assert "磁盘写满" in str(payload.get("partial_write_error"))


def test_p04_rebuilds_the_search_index_once_for_the_whole_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W1.2.3: 整批只重建一次索引，不是每篇文档一次。"""
    rt = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(), planned_writes=[_doc("第一篇", slug="one"), _doc("第二篇", slug="two")]),
    )

    syncs = {"n": 0}
    original_sync = rt.knowledge.sync_fts_index

    def _counting_sync() -> int:
        syncs["n"] += 1
        return original_sync()

    monkeypatch.setattr(rt.knowledge, "sync_fts_index", _counting_sync)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    assert syncs["n"] == 1


def test_p04_fails_loudly_when_the_index_rebuild_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W1.2.3: 索引重建失败不得被吞掉，也不得盖掉真正的写入失败。"""
    rt = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(), planned_writes=[_doc("第一篇", slug="one")]),
    )

    def _boom() -> int:
        raise RuntimeError("索引重建失败")

    monkeypatch.setattr(rt.knowledge, "sync_fts_index", _boom)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.FAILED
    p04 = _node(run, "P04_WRITE_VERIFY")
    assert p04.status == NodeStatus.FAILED
    assert "索引重建失败" in (p04.error or "")
    payload = p04.payload or {}
    assert payload.get("partial_written_paths")
    assert "索引重建失败" in str(payload.get("partial_write_error"))
