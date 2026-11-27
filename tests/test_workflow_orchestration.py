"""P03 对已手工沉淀会话的知识文档硬过滤（feat-sediment-dual-track-and-scheduler-catchup）。

对应 `workflow-orchestration` 变更规格：

1. 纯 `governance_only` 批次：知识文档不重复写入，规则、Skill 计划、错题与分歧照常落库；
2. 混合批次：普通会话的知识文档照常进入写入计划。

另覆盖：等待审批期间才声明手工沉淀（P04 兜底）、来源为空的文档、计划被过滤成空、纯函数单测，
以及规划器被告知哪些会话已手工沉淀。模型回复全部是虚构的固定样例；知识库与 Skill 目录
都落在临时目录，不触碰任何真实 Wiki。

末尾一节属于 `feat-agy-ingest-and-high-fidelity-distill`：客户端随会话提交的结构化简报经队列与
协调器到达 P02 提示词；审查拆出的事件到达 P02 节点与 P03 提示词；事故卡在路由与审批门上的
敏感标记。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from ai_flywheel.adapters.runners import OpenAICompatibleRunner, _build_p03_prompt
from ai_flywheel.adapters.runners.prompts import P02_APPENDED_BRIEF_HEADER, P02_BRIEF_MARKER
from ai_flywheel.adapters.wiki.router import RouteDecision
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.orchestrator import WorkflowOrchestrator
from ai_flywheel.core.pipeline.helpers import (
    GOVERNANCE_ONLY_SKIP_REASON,
    GOVERNANCE_ONLY_SKIP_STATUS,
    apply_route_decision,
    filter_governance_only_docs,
    governance_only_skip_receipt,
)
from ai_flywheel.models import (
    INCIDENT_APPROVAL_REASON,
    DistillQueueItem,
    PlannedWrite,
    RegisterSessionRequest,
    RunStatus,
    SessionRecord,
    SessionStatus,
    WorkflowRunRecord,
    is_sensitive_write,
)
from ai_flywheel.runtime import FlywheelRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]

HAND = "codex:s-hand"
NORMAL = "codex:s-normal"
HAND_PATH = "lessons/2026-11-25-hand-note.md"
NORMAL_PATH = "lessons/2026-11-25-normal-note.md"


# ---------------------------------------------------------------- fixtures shaped like model replies


def _runtime(tmp_path: Path) -> tuple[FlywheelRuntime, Path]:
    """An `api` runtime whose model is replaced per test; the knowledge base is the temp dir."""
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills = tmp_path / "skills"
    skills.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills)]
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    return FlywheelRuntime(cfg), skills


def _register(rt: FlywheelRuntime, session_id: str) -> None:
    rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id=session_id,
            title="接口超时排查",
            project="demo",
            summary="记录接口超时的根因",
            content=(
                "用户：接口联调报错了怎么办。"
                "助手：先看日志，再核对连接池上限；修复后重新部署，测试验收通过。"
            ),
        )
    )


def _finding(session_key: str) -> dict[str, Any]:
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


def _review(*session_keys: str) -> dict[str, Any]:
    """Per session: one finding, one high-risk silent mistake, one dissent."""
    return {
        "findings": [_finding(key) for key in session_keys],
        "mistakes": [
            {
                "root_cause_key": "missing-null-guard",
                "severity_grade": "red_silent",
                "title": "参数校验缺失",
                "phenomenon": "入参为空时报错",
                "truth": "先校验入参再调用",
                "thinking_root_cause": "沿用了未经验证的假设",
                "caught_by": "self",
                "session_key": key,
            }
            for key in session_keys
        ],
        "dissents": [
            {
                "target_type": "rule",
                "target_id": "rule-cache-check",
                "counter_claim": "只读本地内存缓存不适用该失效键检查",
                "condition_scope": "project=demo",
                "evidence_pointer": key,
                "session_key": key,
            }
            for key in session_keys
        ],
        "repeated_workflows": [],
    }


def _doc(slug: str, *source_keys: str) -> dict[str, Any]:
    return {
        "target_kind": "knowledge_doc",
        "title": f"{slug}复盘",
        "summary": "记录接口超时的根因与修复方式",
        "relative_path": f"lessons/2026-11-25-{slug}.md",
        "action": "create",
        "source_session_keys": list(source_keys),
        "content_preview": (
            f"---\ntitle: {slug}复盘\n---\n\n# {slug}复盘\n\n## 事实回顾\n"
            "- **接口**在高峰期出现超时，根因是连接池上限过小。\n"
        ),
    }


def _rule(*source_keys: str) -> dict[str, Any]:
    return {
        "target_kind": "flywheel_rule",
        "relative_path": None,
        "action": "register_rule",
        "summary": "调用外部接口前先校验入参",
        "source_session_keys": list(source_keys),
        "rule_payload": {
            "title": "调用外部接口前先校验入参",
            "domain": "engineering",
            "projects": ["demo"],
            "triggers": ["入参", "参数校验"],
            "checklist": ["调用前校验入参非空", "校验失败时返回明确错误"],
            "forbidden_routes": ["跳过校验直接调用"],
            "root_cause_key": "missing-null-guard",
        },
    }


def _skill(slug: str, *source_keys: str) -> dict[str, Any]:
    return {
        "target_kind": "skill_improvement",
        "relative_path": f"{slug}/SKILL.md",
        "action": "create",
        "summary": "把联调前的固定检查步骤沉淀为 Skill",
        "source_session_keys": list(source_keys),
        "requires_approval": True,
        "approval_reason": "每次联调都要重复同样的检查步骤",
        "skill_route": {
            "hit": True,
            "action": "create",
            "target_skill": slug,
            "reason": "固定步骤反复执行，值得沉淀为 Skill",
        },
        "content_preview": (
            f"---\nname: {slug}\ndescription: 联调前逐项检查连接池、超时与重试配置\n---\n\n"
            "# 联调检查清单\n\n## 执行步骤\n1. 核对连接池上限与队列长度。\n"
            "2. 核对每个外部调用的超时时间。\n3. 核对重试次数与退避策略。\n"
            "4. 用一条空入参请求确认错误信息明确。\n\n## 验收\n- 以上四项逐条回读实际生效值。\n"
        ),
    }


class _FakeModel:
    """Stands in for the model gateway: P02 gets a canned review, P03 a canned plan."""

    def __init__(self, review: dict[str, Any], planned_writes: list[dict[str, Any]]) -> None:
        self.review_json = json.dumps(review, ensure_ascii=False)
        self.plan_json = json.dumps({"planned_writes": planned_writes}, ensure_ascii=False)
        self.prompts: list[str] = []

    @property
    def p03_prompts(self) -> list[str]:
        return [p for p in self.prompts if p.startswith("You are the P03 Route & Write-Plan Worker")]

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("You are the P02 Review Worker"):
            return self.review_json
        return self.plan_json


def _use_model(monkeypatch: pytest.MonkeyPatch, model: _FakeModel) -> _FakeModel:
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", model)
    return model


def _declare_hand_distilled(
    rt: FlywheelRuntime, session_key: str = HAND, *, governance_only: bool = True
) -> dict[str, Any]:
    return rt.queue.handle_session_intent(
        "hand_distilled",
        session_key=session_key,
        wiki_paths=["projects/demo/api-timeout.md"],
        governance_only=governance_only,
    )


def _enqueue(rt: FlywheelRuntime, session_key: str) -> None:
    rt.queue.handle_session_intent("enqueue", session_key=session_key, project="demo")


def _node(run: WorkflowRunRecord, node_id: str) -> dict[str, Any]:
    node = next(n for n in run.nodes if n.node_id == node_id)
    return {"status": node.status.value, "summary": node.summary, "payload": node.payload}


def _events(rt: FlywheelRuntime, run_id: str) -> list[dict[str, Any]]:
    return rt.db.list_audit_events(
        entity_type="workflow_run", entity_id=run_id, limit=100
    )


def _knowledge_files(tmp_path: Path) -> list[str]:
    root = tmp_path / "data" / "knowledge"
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md")) if root.is_dir() else []


def _write(target_kind: str, sources: list[str], path: str | None = None) -> PlannedWrite:
    """A plan item of ``target_kind`` that speaks for ``sources``."""
    return PlannedWrite(
        target_kind=target_kind,
        relative_path=path,
        action="create" if target_kind == "knowledge_doc" else "register_rule",
        title=f"{target_kind} {path or ''}".strip(),
        summary="s",
        source_session_keys=list(sources),
    )


# ---------------------------------------------------------------- the filter itself


def test_the_filter_takes_out_only_the_documents_of_hand_distilled_sessions():
    hand_doc = _write("knowledge_doc", ["s-hand"], "lessons/hand.md")
    hand_rule = _write("flywheel_rule", ["s-hand"])
    hand_skill = PlannedWrite(
        target_kind="skill_improvement",
        action="create",
        title="t",
        summary="s",
        source_session_keys=["s-hand"],
    )
    normal_doc = _write("knowledge_doc", ["s-normal"], "lessons/normal.md")
    shared_doc = _write("knowledge_doc", ["s-normal", "s-hand"], "lessons/shared.md")
    loose_doc = _write("knowledge_doc", [], "lessons/loose.md")
    plan = [hand_doc, hand_rule, hand_skill, normal_doc, shared_doc, loose_doc]

    kept, skipped = filter_governance_only_docs(plan, {"s-hand"}, ["s-normal", "s-hand"])

    # A mixed batch: only the document that speaks for the hand-distilled session alone goes.
    assert kept == [hand_rule, hand_skill, normal_doc, shared_doc, loose_doc]
    assert skipped == [
        {
            "title": hand_doc.title,
            "relative_path": "lessons/hand.md",
            "action": "create",
            "source_session_keys": ["s-hand"],
            "reason_code": "governance_only_session",
        }
    ]
    assert plan == [hand_doc, hand_rule, hand_skill, normal_doc, shared_doc, loose_doc]

    # A batch made of hand-distilled sessions only has nowhere for a sourceless document to come from.
    kept_all, skipped_all = filter_governance_only_docs(plan, {"s-hand"}, ["s-hand"])
    assert kept_all == [hand_rule, hand_skill, normal_doc, shared_doc]
    assert [doc["relative_path"] for doc in skipped_all] == ["lessons/hand.md", "lessons/loose.md"]

    # Nobody declared anything: the plan is returned as it came.
    untouched, dropped = filter_governance_only_docs(plan, set(), ["s-normal", "s-hand"])
    assert untouched == plan
    assert dropped == []
    # With nothing admitted, a sourceless document has no batch to take a mode from.
    kept_none, skipped_none = filter_governance_only_docs([loose_doc], {"s-hand"}, [])
    assert (kept_none, skipped_none) == ([loose_doc], [])


def test_the_receipt_of_a_skipped_document_says_it_was_not_written():
    receipt = governance_only_skip_receipt(
        {"title": "手工沉淀复盘", "relative_path": "lessons/hand.md", "source_session_keys": ["s-hand"]}
    )
    assert receipt == {
        "relative_path": "lessons/hand.md",
        "title": "手工沉淀复盘",
        "status": "SKIPPED_GOVERNANCE_ONLY",
        "skipped": True,
        "written": False,
        "reason_code": "governance_only_session",
        "reason": "会话已由客户端手工沉淀，知识文档不重复写入",
        "source_session_keys": ["s-hand"],
    }
    assert receipt["status"] == GOVERNANCE_ONLY_SKIP_STATUS
    assert receipt["reason_code"] == GOVERNANCE_ONLY_SKIP_REASON


# ---------------------------------------------------------------- pure governance-only batch


def test_a_hand_distilled_session_yields_rules_and_observations_but_no_second_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """规格场景：governance_only 会话不重复写 Wiki，但正常产出规则与错题。"""
    rt, skills = _runtime(tmp_path)
    _register(rt, "s-hand")
    assert _declare_hand_distilled(rt)["distill_mode"] == "governance_only"
    model = _use_model(
        monkeypatch,
        _FakeModel(
            _review(HAND),
            [_doc("hand-note", HAND), _rule(HAND), _skill("api-checklist", HAND)],
        ),
    )
    files_before = _knowledge_files(tmp_path)

    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.admitted_sessions == [HAND]

    # P01 names how each admitted session is distilled; P02 reviews it like any other.
    assert _node(run, "P01_BEGIN")["payload"]["distill_modes"] == {HAND: "governance_only"}
    assert _node(run, "P01_BEGIN")["payload"]["governance_only_session_keys"] == [HAND]
    assert "1 个已由客户端手工沉淀" in _node(run, "P01_BEGIN")["summary"]
    p02 = _node(run, "P02_REVIEW")
    assert p02["status"] == "PASS"
    assert p02["payload"]["governance_only_session_keys"] == [HAND]
    assert p02["payload"]["candidate_mistake_count"] == 1
    assert p02["payload"]["candidate_dissent_count"] == 1
    assert rt.db.list_mistake_observations() == []  # still read-only at P02

    # P03 takes the document out and keeps the rule and the Skill plan.
    p03 = _node(run, "P03_ROUTE_PLAN")
    assert p03["status"] == "PASS"
    assert [w.target_kind for w in run.planned_writes] == ["flywheel_rule", "skill_improvement"]
    assert p03["payload"]["governance_only_session_keys"] == [HAND]
    assert p03["payload"]["governance_only_wiki_skipped"] == 1
    assert p03["payload"]["governance_only_skipped_docs"] == [
        {
            "title": "hand-note复盘",
            "relative_path": HAND_PATH,
            "action": "create",
            "source_session_keys": [HAND],
            "reason_code": "governance_only_session",
        }
    ]
    assert p03["payload"]["planned_count"] == 2
    assert "1 篇笔记所属会话已手工沉淀（仅治理提炼），未列入计划" in p03["summary"]
    (skipped_event,) = [e for e in _events(rt, run.run_id) if e["event_type"] == "governance_only_wiki_skipped"]
    assert skipped_event["actor"] == "p03_plan"
    assert skipped_event["payload"]["governance_only_session_keys"] == [HAND]
    assert skipped_event["payload"]["skipped_docs"][0]["relative_path"] == HAND_PATH

    # The planner was told, before it planned, that this session's knowledge is already written.
    (prompt,) = model.p03_prompts
    payload = json.loads(prompt.rsplit("\n\n", 1)[1])
    assert payload["governance_only_session_keys"] == [HAND]
    assert "HAND-DISTILLED SESSIONS" in prompt

    # After approval: no knowledge document, but the rule, the Skill, the mistake and the dissent land.
    done = rt.orchestrator.approve_and_resume(run.run_id, source="test")
    assert done.status == RunStatus.PASS, done.error
    assert _knowledge_files(tmp_path) == files_before
    assert [r.title for r in rt.db.list_rules()] == ["调用外部接口前先校验入参"]
    assert (skills / "api-checklist" / "SKILL.md").is_file()
    observations = rt.db.list_mistake_observations()
    assert [o["root_cause_key"] for o in observations] == ["missing-null-guard"]
    assert len(rt.db.list_dissent_records()) == 1
    p04 = _node(done, "P04_WRITE_VERIFY")
    assert p04["payload"]["written_paths"] == []
    assert p04["payload"]["persisted_dissent_count"] == 1
    assert rt.db.get_session(HAND).status is SessionStatus.PROCESSED
    assert rt.db.get_workflow_lock() is None


def test_a_plan_left_empty_by_the_filter_is_an_empty_plan_not_an_unavailable_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """规划器只给了一篇知识文档：过滤后计划为空，节点照常通过，不触发人工审批。"""
    rt, _ = _runtime(tmp_path)
    _register(rt, "s-hand")
    _declare_hand_distilled(rt)
    _use_model(monkeypatch, _FakeModel(_review(HAND), [_doc("hand-note", HAND)]))

    run = rt.orchestrator.start_run(trigger="test", phase="daily")

    assert run.status == RunStatus.PASS, run.error
    assert run.planned_writes == []
    assert _node(run, "P03_ROUTE_PLAN")["status"] == "PASS"
    assert _node(run, "P03_ROUTE_PLAN")["payload"]["governance_only_wiki_skipped"] == 1
    assert _knowledge_files(tmp_path) == []
    assert len(rt.db.list_mistake_observations()) == 1
    assert "governance_only_wiki_skipped" in [e["event_type"] for e in _events(rt, run.run_id)]


def test_a_sourceless_document_is_dropped_only_when_the_whole_batch_is_hand_distilled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rt, _ = _runtime(tmp_path)
    _register(rt, "s-hand")
    _declare_hand_distilled(rt)
    _use_model(monkeypatch, _FakeModel(_review(HAND), [_doc("loose")]))

    run = rt.orchestrator.start_run(trigger="test", phase="daily")

    assert run.status == RunStatus.PASS, run.error
    assert run.planned_writes == []
    (skipped,) = _node(run, "P03_ROUTE_PLAN")["payload"]["governance_only_skipped_docs"]
    assert skipped["source_session_keys"] == []
    assert _knowledge_files(tmp_path) == []


# ---------------------------------------------------------------- mixed batch


def test_in_a_mixed_batch_the_ordinary_sessions_document_is_still_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """规格场景：混合批次中普通会话的知识文档照常生成。"""
    rt, _ = _runtime(tmp_path)
    _register(rt, "s-normal")
    _register(rt, "s-hand")
    _enqueue(rt, NORMAL)
    _declare_hand_distilled(rt)
    _use_model(
        monkeypatch,
        _FakeModel(
            _review(NORMAL, HAND),
            [_doc("hand-note", HAND), _doc("normal-note", NORMAL), _rule(HAND)],
        ),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert sorted(run.admitted_sessions) == sorted([NORMAL, HAND])

    p01 = _node(run, "P01_BEGIN")["payload"]
    assert p01["distill_modes"] == {NORMAL: "full", HAND: "governance_only"}
    assert p01["governance_only_session_keys"] == [HAND]
    planned = [(w.target_kind, w.relative_path) for w in run.planned_writes]
    assert planned == [("knowledge_doc", NORMAL_PATH), ("flywheel_rule", None)]
    p03 = _node(run, "P03_ROUTE_PLAN")["payload"]
    assert [doc["relative_path"] for doc in p03["governance_only_skipped_docs"]] == [HAND_PATH]
    assert p03["governance_only_session_keys"] == [HAND]

    done = rt.orchestrator.approve_and_resume(run.run_id, source="test")
    assert done.status == RunStatus.PASS, done.error
    assert _knowledge_files(tmp_path) == [NORMAL_PATH]
    assert len(rt.db.list_rules()) == 1
    assert len(rt.db.list_mistake_observations()) == 1
    assert _node(done, "P04_WRITE_VERIFY")["payload"]["written_paths"] == [NORMAL_PATH]


# ---------------------------------------------------------------- P04: declared while the batch waited


def test_a_session_declared_while_the_batch_waited_for_approval_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """计划可能等了几个小时审批：这期间声明手工沉淀的会话，其已批准文档在 P04 也不再写。"""
    rt, _ = _runtime(tmp_path)
    _register(rt, "s-late")
    _enqueue(rt, "codex:s-late")
    _use_model(monkeypatch, _FakeModel(_review("codex:s-late"), [_doc("late-note", "codex:s-late")]))

    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert [w.relative_path for w in run.planned_writes] == ["lessons/2026-11-25-late-note.md"]
    assert _node(run, "P03_ROUTE_PLAN")["payload"]["governance_only_skipped_docs"] == []

    _declare_hand_distilled(rt, "codex:s-late")
    done = rt.orchestrator.approve_and_resume(run.run_id, source="test")

    assert done.status == RunStatus.PASS, done.error
    assert _knowledge_files(tmp_path) == []
    p04 = _node(done, "P04_WRITE_VERIFY")
    assert p04["payload"]["written_paths"] == []
    (skipped,) = p04["payload"]["governance_only_skipped_docs"]
    assert skipped["relative_path"] == "lessons/2026-11-25-late-note.md"
    (receipt, *rest) = p04["payload"]["write_receipts"]
    assert receipt["status"] == GOVERNANCE_ONLY_SKIP_STATUS
    assert (receipt["skipped"], receipt["written"]) == (True, False)
    assert rest == []
    skipped_events = [
        e for e in _events(rt, run.run_id) if e["event_type"] == "governance_only_wiki_skipped"
    ]
    assert [e["actor"] for e in skipped_events] == ["p04_write"]
    # Its mistake still landed: the review of the session is not thrown away.
    assert len(rt.db.list_mistake_observations()) == 1


def test_a_session_taken_out_of_distillation_gets_no_document_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`governance_only=false` 的会话被显式点名进批：知识已手工写好，文档同样不再生成。"""
    rt, _ = _runtime(tmp_path)
    _register(rt, "s-gone")
    taken_out = _declare_hand_distilled(rt, "codex:s-gone", governance_only=False)
    assert (taken_out["status"], taken_out["distill_mode"]) == ("excluded", "skipped")
    _use_model(
        monkeypatch,
        _FakeModel(
            _review("codex:s-gone"),
            [_doc("gone-note", "codex:s-gone"), _rule("codex:s-gone")],
        ),
    )

    run = rt.orchestrator.start_run(
        trigger="test", phase="daily", session_keys=["codex:s-gone"], auto_approve_override=True
    )

    assert run.status == RunStatus.PASS, run.error
    assert [w.target_kind for w in run.planned_writes] == ["flywheel_rule"]
    assert _node(run, "P03_ROUTE_PLAN")["payload"]["governance_only_session_keys"] == ["codex:s-gone"]
    assert _knowledge_files(tmp_path) == []
    assert len(rt.db.list_rules()) == 1


# ---------------------------------------------------------------- batches without a hand-distilled session


def test_an_ordinary_batch_is_not_touched_by_the_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rt, _ = _runtime(tmp_path)
    _register(rt, "s-normal")
    _enqueue(rt, NORMAL)
    model = _use_model(
        monkeypatch, _FakeModel(_review(NORMAL), [_doc("normal-note", NORMAL), _rule(NORMAL)])
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL

    p01 = _node(run, "P01_BEGIN")["payload"]
    assert p01["distill_modes"] == {NORMAL: "full"}
    assert p01["governance_only_session_keys"] == []
    p03 = _node(run, "P03_ROUTE_PLAN")["payload"]
    assert p03["governance_only_session_keys"] == []
    assert p03["governance_only_skipped_docs"] == []
    assert p03["governance_only_wiki_skipped"] == 0
    assert "governance_only_wiki_skipped" not in [e["event_type"] for e in _events(rt, run.run_id)]
    assert [w.target_kind for w in run.planned_writes] == ["knowledge_doc", "flywheel_rule"]

    # The planner hears nothing about hand-distilled sessions when there are none.
    (prompt,) = model.p03_prompts
    assert "governance_only_session_keys" not in prompt
    assert "HAND-DISTILLED" not in prompt

    done = rt.orchestrator.approve_and_resume(run.run_id, source="test")
    assert done.status == RunStatus.PASS, done.error
    assert _knowledge_files(tmp_path) == [NORMAL_PATH]
    assert "governance_only_skipped_docs" not in _node(done, "P04_WRITE_VERIFY")["payload"]


def test_the_planner_prompt_only_changes_when_a_session_was_hand_distilled():
    review = {"findings": [_finding(NORMAL)], "eligible_promotions": [], "repeated_workflows": []}

    def build(keys: list[str] | None) -> str:
        findings = review if keys is None else {**review, "governance_only_session_keys": keys}
        return _build_p03_prompt(findings, [], [], 8, "lessons/", "daily")

    plain_text, plain_payload = build(None).rsplit("\n\n", 1)

    # A batch without such a session gets the prompt it always had, byte for byte.
    assert build([]) == build(None)
    assert "governance_only_session_keys" not in json.loads(plain_payload)

    told_text, told_payload = build([HAND]).rsplit("\n\n", 1)
    note = re.search(r"HAND-DISTILLED SESSIONS:[^\n]*\n", told_text)
    assert note is not None, "the planner is not told about the hand-distilled sessions"
    assert "Do NOT plan a \"knowledge_doc\"" in note.group(0)
    # Apart from that one sentence and that one input key, the prompt is the same.
    assert told_text.replace(note.group(0), "") == plain_text
    payload = json.loads(told_payload)
    assert payload.pop("governance_only_session_keys") == [HAND]
    assert payload == json.loads(plain_payload)

# ---------------------------------------------------------------- the brief, the events and the incident card through a run
# (feat-agy-ingest-and-high-fidelity-distill)


BRIEF = {
    "tables_verified": ["ledger_entries: entry_id 主键，amount 以分为单位"],
    "root_cause": "连接池上限过小，不是接口超时",
    "decisions": [{"what": "连接池上限调到 50", "why": "压测下请求排队超时"}],
}
INCIDENT_EVENT = {
    "event_id": "ev-2",
    "kind": "incident",
    "title": "事故：改错了测试库配置",
    "summary": "助手改错了测试库配置且未告知；改配置前先确认环境，改完回读。",
    "session_key": "codex:s-events",
}
LEDGER_PATH = "wiki/incidents/ai-incidents/2026-11-27-wrong-config.md"
# The runner numbers the events of one review in order, whatever ids the model used.
NUMBERED_EVENT = {**INCIDENT_EVENT, "event_id": "ev-1"}


def _long_dialogue() -> str:
    """A dialogue the queue splits into two chunks (above its 24000-character chunk limit)."""
    turn = "[assistant] " + "核对记录。" * 389
    return "\n\n".join([turn] * 14)


def test_a_structured_brief_submitted_with_a_session_reaches_the_review_on_top_of_its_first_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """规格场景（链路侧）：客户端随会话提交的结构化简报经队列到达 P02 提示词，置于第一段会话正文之前。"""
    rt, _ = _runtime(tmp_path)
    items = rt.queue.on_explicit_enqueue(
        client="codex", session_id="s-brief", title="对账复盘", content=_long_dialogue(), structured_brief=BRIEF
    )
    assert [item.chunk_index for item in items] == [1, 2]
    model = _use_model(monkeypatch, _FakeModel(_review("codex:s-brief"), [_doc("brief-note", "codex:s-brief")]))

    run = rt.orchestrator.start_run(trigger="test", phase="daily")

    assert run.status == RunStatus.AWAITING_APPROVAL, run.error
    assert run.admitted_sessions == ["codex:s-brief"]
    (prompt,) = [p for p in model.prompts if p.startswith("You are the P02 Review Worker")]
    first, second = json.loads(prompt.rsplit("\n\n", 1)[1])["sessions"]
    assert (first["session_key"], second["session_key"]) == ("codex:s-brief", "codex:s-brief")
    # The brief sits ahead of the dialogue's text, as JSON in the order the client gave its keys, and whole.
    assert list(first) == ["session_key", "client", "project", "title", "structured_brief", "text"]
    assert first["structured_brief"] == f"{P02_BRIEF_MARKER}\n{json.dumps(BRIEF, ensure_ascii=False, indent=2)}"
    assert P02_APPENDED_BRIEF_HEADER not in first["text"]  # lifted out of the chunk the queue appended it to
    assert "STRUCTURED BRIEF:" in prompt
    # The second chunk carries dialogue only: the review reads the brief once, with the first part.
    assert "structured_brief" not in second
    assert P02_APPENDED_BRIEF_HEADER not in second["text"]
    # Nothing about the brief is kept on the stored session: the copy lives as long as the batch.
    assert "structured_brief" not in (rt.db.get_session("codex:s-brief").metadata or {})


@pytest.mark.parametrize(
    "events, carried",
    [
        pytest.param([INCIDENT_EVENT], [NUMBERED_EVENT], id="events"),
        pytest.param([INCIDENT_EVENT, "incident", None], [NUMBERED_EVENT], id="anything-but-a-mapping-is-dropped"),
        pytest.param({"kind": "incident"}, [], id="a-mapping-is-no-list"),
        pytest.param(None, [], id="no-key-at-all"),
    ],
)
def test_the_events_of_a_review_reach_the_review_node_and_the_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: Any, carried: list[dict[str, Any]]
):
    """审查拆出的事件经运行器整形（顺序编号）后到达 P02 节点与 P03 提示词；没有事件时规划器不会多看到一个键。"""
    rt, _ = _runtime(tmp_path)
    _register(rt, "s-events")
    review = _review("codex:s-events") if events is None else {**_review("codex:s-events"), "events": events}
    model = _use_model(monkeypatch, _FakeModel(review, [_doc("events-note", "codex:s-events")]))

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s-events"])

    assert run.status == RunStatus.AWAITING_APPROVAL, run.error
    assert _node(run, "P02_REVIEW")["payload"]["events"] == carried
    (prompt,) = model.p03_prompts
    payload = json.loads(prompt.rsplit("\n\n", 1)[1])
    assert payload.get("events", []) == carried
    assert ("EVENTS:" in prompt) is bool(carried)


def _stored_session(**overrides: Any) -> SessionRecord:
    return SessionRecord.model_validate(
        {"session_key": "codex:s1", "client": "codex", "session_id": "s1", "title": "对账复盘", "project": "demo", **overrides}
    )


def _chunk(chunk_index: int, payload: dict[str, Any] | None = None) -> DistillQueueItem:
    return DistillQueueItem(
        queue_id=f"q{chunk_index}",
        session_key="codex:s1",
        client="codex",
        chunk_index=chunk_index,
        chunk_total=2,
        payload={"structured_brief": BRIEF} if payload is None else payload,
    )


def test_the_reviewed_session_carries_the_brief_of_the_first_chunk_and_leaves_the_stored_record_alone():
    """简报只随队列首块进入审查；会话记录本身不被改写，落库的记录与简报无关。"""
    stored = _stored_session()
    before = stored.model_dump()

    reviewed = WorkflowOrchestrator._reviewed_session(stored, _chunk(1))

    assert reviewed is not stored
    assert reviewed.metadata == {"structured_brief": BRIEF}

    def without_metadata(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "metadata"}

    assert without_metadata(reviewed.model_dump()) == without_metadata(before)
    assert stored.model_dump() == before  # the stored record is not the one that carries the brief

    # A later chunk, an item that holds no brief, and an item whose payload is not a mapping: the session as it is.
    not_a_mapping = _chunk(1)
    not_a_mapping.payload = "not a mapping"  # type: ignore[assignment]
    for item in (_chunk(2), _chunk(1, {}), _chunk(1, {"structured_brief": {}}), not_a_mapping):
        assert WorkflowOrchestrator._reviewed_session(stored, item) is stored
    # A record whose metadata is not a mapping is returned as it is, never repaired on the way.
    bare = _stored_session().model_copy(update={"metadata": None})
    assert WorkflowOrchestrator._reviewed_session(bare, _chunk(1)) is bare
    # A record that already carries a brief keeps its own: the session record wins over the item.
    own = _stored_session(metadata={"structured_brief": {"root_cause": "会话记录自带的简报"}})
    assert WorkflowOrchestrator._reviewed_session(own, _chunk(1)) is own


def _incident_write(**overrides: Any) -> PlannedWrite:
    """A plan item for an incident card that the plan itself did not flag."""
    fields: dict[str, Any] = {
        "target_kind": "knowledge_doc",
        "relative_path": LEDGER_PATH,
        "action": "create",
        "title": "改错测试库配置复盘",
        "summary": "助手改错了测试库配置且未告知。",
        "source_session_keys": ["codex:s-events"],
    }
    fields.update(overrides)
    return PlannedWrite(**fields)


def test_the_route_verdict_flags_an_incident_card_the_plan_did_not_flag():
    """路由声明之外被契约认出的事故卡，同样带上敏感情标记与统一理由；未路由的计划项不被触碰。"""
    write = _incident_write(wiki_route=None)
    assert (write.requires_approval, write.approval_reason) == (False, "")

    traces = apply_route_decision(write, RouteDecision(routed=True, source="declared", page_type="incident", sensitive=True))

    assert traces == []
    assert (write.requires_approval, write.approval_reason) == (True, INCIDENT_APPROVAL_REASON)

    # The plan's own words are kept; a reason without the flag is completed, never replaced.
    stated = _incident_write(requires_approval=True, approval_reason="模型给出的事故理由")
    assert apply_route_decision(stated, RouteDecision(routed=True, page_type="incident", sensitive=True)) == []
    assert (stated.requires_approval, stated.approval_reason) == (True, "模型给出的事故理由")
    unfinished = _incident_write(requires_approval=False, approval_reason="模型留下的一句说明。")
    apply_route_decision(unfinished, RouteDecision(routed=True, page_type="incident", sensitive=True))
    assert unfinished.approval_reason == f"模型留下的一句说明；{INCIDENT_APPROVAL_REASON}"

    # A refused route, and a routine one, leave the item exactly as the plan wrote it.
    for decision in (
        RouteDecision(routed=False, reason_code="incident_prefix_not_allowed"),
        RouteDecision(routed=True, page_type="project-practice-note", sensitive=False),
    ):
        untouched = _incident_write()
        assert apply_route_decision(untouched, decision) == []
        assert (untouched.requires_approval, untouched.approval_reason) == (False, "")


def test_one_incident_card_pauses_a_smart_batch_on_its_own():
    """smart 模式下，单独一张事故卡就让批次挂起审批，与批量阈值无关。"""
    pause = WorkflowOrchestrator._needs_manual_approval
    card = _incident_write(requires_approval=True, approval_reason=INCIDENT_APPROVAL_REASON)
    routine = _write("knowledge_doc", ["codex:s-normal"], "lessons/2026-11-25-normal-note.md")

    assert pause([card], "smart") == (True, [INCIDENT_APPROVAL_REASON])
    assert pause([card], "smart", high_write_threshold=1000) == (True, [INCIDENT_APPROVAL_REASON])
    assert pause([routine], "smart") == (False, [])
    # Six routine notes stay under the threshold; with the card the batch is over it and says both.
    assert pause([routine] * 6, "smart") == (False, [])
    assert pause([card, *([routine] * 6)], "smart") == (
        True,
        [INCIDENT_APPROVAL_REASON, "本批建议写入共 7 项（超过自动放行阈值 6）"],
    )
    # A card the plan did not flag still pauses the gate: it reads the same predicate as the card does.
    unflagged = _incident_write(wiki_route={"page_type": "incident", "project": None, "reason": "一次协作事故"})
    assert is_sensitive_write(unflagged) is True
    assert pause([unflagged], "smart") == (True, ["重要变更：改错测试库配置复盘"])
    # `always` pauses for its own reason; `auto` is the standing contract of that mode (the valve decided before).
    assert pause([card], "always") == (True, ["当前为每次人工审批模式（共 1 项）"])
    assert pause([card], "auto") == (False, [])
