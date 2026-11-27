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

发版前整改补上的三节：`auto` 撞上关闭的安全阀时，配置、命令行与门户两个入口都按 `smart` 走，
事故卡在任何模式下都必须人工审批（P0-1）；批次运行途中被移出沉淀的会话，恢复后零文档、零规则、
零错题、零分歧（P1-3）；来源键拼错的文档与写入前一刻的意图变化都拦在落盘之前（P1-4）。
"""
from __future__ import annotations

import json
import re
import shutil
import time
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
    HAND_DISTILLED_SKIP_REASON,
    HAND_DISTILLED_SKIP_STATUS,
    NO_SESSION_REASON,
    NO_SESSION_STATUS,
    apply_route_decision,
    filter_hand_distilled_plan,
    governance_only_skip_receipt,
    left_out_item_receipt,
)
from ai_flywheel.core.pipeline.coordinator import resolve_approval_mode
from ai_flywheel.core.queue_service import HAND_DISTILLED_SKIP
from ai_flywheel.models import (
    INCIDENT_APPROVAL_REASON,
    DistillQueueItem,
    PlannedWrite,
    QueueStatus,
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


def _filter(
    plan: list[PlannedWrite],
    admitted: list[str],
    *,
    governance_only: tuple[str, ...] = (),
    taken_out: tuple[str, ...] = (),
):
    """The plan with what the hand-distilled sessions of the batch may not write taken out."""
    return filter_hand_distilled_plan(
        plan,
        governance_only_keys=set(governance_only),
        taken_out_keys=set(taken_out),
        admitted_keys=admitted,
    )


def _left_out(
    write: PlannedWrite, sources: list[str], reason_code: str, *, with_kind: bool = False
) -> dict[str, Any]:
    """What is recorded about ``write`` — as the plan cited it in ``sources`` — when it is not written."""
    return {
        "title": write.title,
        **({"target_kind": write.target_kind} if with_kind else {}),
        "relative_path": write.relative_path,
        "action": write.action,
        "source_session_keys": sources,
        "reason_code": reason_code,
    }


def _hand_distilled_items(source: str) -> tuple[list[PlannedWrite], PlannedWrite, PlannedWrite, PlannedWrite]:
    """A rule, a Skill plan and a document that each speak for ``source`` alone."""
    rule = _write("flywheel_rule", [source], None)
    skill = PlannedWrite(
        target_kind="skill_improvement",
        action="create",
        title="t",
        summary="s",
        source_session_keys=[source],
    )
    doc = _write("knowledge_doc", [source], "lessons/hand.md")
    return [rule, skill, doc], rule, skill, doc


def test_the_filter_takes_out_only_the_documents_of_hand_distilled_sessions():
    hand_items, hand_rule, hand_skill, hand_doc = _hand_distilled_items("s-hand")
    normal_doc = _write("knowledge_doc", ["s-normal"], "lessons/normal.md")
    shared_doc = _write("knowledge_doc", ["s-normal", "s-hand"], "lessons/shared.md")
    loose_doc = _write("knowledge_doc", [], "lessons/loose.md")
    plan = [hand_doc, hand_rule, hand_skill, normal_doc, shared_doc, loose_doc]

    mixed = _filter(plan, ["s-normal", "s-hand"], governance_only=("s-hand",))

    # A mixed batch: only the document that speaks for the hand-distilled session alone goes.
    assert mixed.kept == [hand_rule, hand_skill, normal_doc, shared_doc, loose_doc]
    assert mixed.documents_left_out == [_left_out(hand_doc, ["s-hand"], "governance_only_session")]
    assert (mixed.items_left_out, mixed.unknown_source_keys) == ([], [])
    assert plan == [hand_doc, hand_rule, hand_skill, normal_doc, shared_doc, loose_doc]
    # A batch that speaks only of its own sessions has nothing to strip: every item cites what it did.
    assert [w.source_session_keys for w in mixed.kept] == [["s-hand"], ["s-hand"], ["s-normal"], ["s-normal", "s-hand"], []]

    # A batch made of hand-distilled sessions only: there is no ordinary session a document could
    # speak for, so every knowledge document is left out whatever keys it cites.
    hand_only = _filter(plan, ["s-hand"], governance_only=("s-hand",))
    assert hand_only.kept == [hand_rule, hand_skill]
    assert hand_only.documents_left_out == [
        _left_out(write, sources, "governance_only_session")
        for write, sources in (
            (hand_doc, ["s-hand"]),
            (normal_doc, ["s-normal"]),
            (shared_doc, ["s-normal", "s-hand"]),
            (loose_doc, []),
        )
    ]
    assert hand_only.unknown_source_keys == [
        {
            "title": normal_doc.title,
            "target_kind": "knowledge_doc",
            "source_session_keys": ["s-normal"],
            "unknown_source_keys": ["s-normal"],
        },
        {
            "title": shared_doc.title,
            "target_kind": "knowledge_doc",
            "source_session_keys": ["s-normal", "s-hand"],
            "unknown_source_keys": ["s-normal"],
        },
    ]

    # Nobody declared anything: the plan is returned as it came.
    untouched = _filter(plan, ["s-normal", "s-hand"])
    assert untouched.kept == plan
    assert (untouched.documents_left_out, untouched.items_left_out, untouched.unknown_source_keys) == ([], [], [])
    # With nothing admitted there is no batch to take a mode from: every item is left out, of any kind.
    kept_none = _filter([loose_doc, hand_rule], [], governance_only=("s-hand",))
    assert (kept_none.kept, kept_none.documents_left_out) == ([], [])
    assert [(item["target_kind"], item["reason_code"]) for item in kept_none.items_left_out] == [
        ("knowledge_doc", NO_SESSION_REASON),
        ("flywheel_rule", NO_SESSION_REASON),
    ]


def test_a_key_that_names_no_session_of_the_batch_never_stands_in_for_an_ordinary_one():
    """拼错或编造的来源键不等于「有一个普通会话」：有效来源仍全属手工沉淀会话时，文档照样过滤。"""
    misspelt = _write("knowledge_doc", ["s-hand", "s-hnad"], "lessons/misspelt.md")
    made_up = _write("knowledge_doc", ["s-normal", "s-made-up"], "lessons/made-up.md")
    stray = _write("knowledge_doc", ["s-made-up"], "lessons/stray.md")
    hand_rule = _write("flywheel_rule", ["s-hand", "s-hnad"], None)
    plan = [misspelt, made_up, stray, hand_rule]

    mixed = _filter(plan, ["s-normal", "s-hand"], governance_only=("s-hand",))

    # `s-hnad` is no session of the batch: the document stands for the hand-distilled session alone.
    assert mixed.kept == [made_up, stray, hand_rule]
    assert mixed.documents_left_out == [_left_out(misspelt, ["s-hand", "s-hnad"], "governance_only_session")]
    assert [item["unknown_source_keys"] for item in mixed.unknown_source_keys] == [
        ["s-hnad"],
        ["s-made-up"],
        ["s-made-up"],
        ["s-hnad"],  # the rule is kept — a governance-only session is queued for its rules — and still reported
    ]
    assert mixed.unknown_source_keys[0]["source_session_keys"] == ["s-hand", "s-hnad"]
    # The unknown key is taken off what is kept: a document never names a session of the planner's invention.
    assert [w.source_session_keys for w in mixed.kept] == [["s-normal"], [], ["s-hand"]]

    hand_only = _filter(plan, ["s-hand"], governance_only=("s-hand",))
    assert hand_only.kept == [hand_rule]
    assert [doc["relative_path"] for doc in hand_only.documents_left_out] == [
        "lessons/misspelt.md",
        "lessons/made-up.md",
        "lessons/stray.md",
    ]


def test_a_session_taken_out_of_distillation_takes_its_items_of_every_kind_with_it():
    """`governance_only=false` 的会话不产出任何内容：文档、规则、Skill 计划一并排除，不只文档。"""
    _, _, _, hand_doc = _hand_distilled_items("s-hand")
    _, gone_rule, gone_skill, gone_doc = _hand_distilled_items("s-gone")
    normal_doc = _write("knowledge_doc", ["s-normal"], "lessons/normal.md")
    shared_doc = _write("knowledge_doc", ["s-normal", "s-gone"], "lessons/shared.md")
    plan = [gone_doc, gone_rule, gone_skill, normal_doc, shared_doc]

    result = _filter(plan, ["s-normal", "s-gone"], taken_out=("s-gone",))

    assert result.kept == [normal_doc, shared_doc]
    assert result.items_left_out == [
        _left_out(write, ["s-gone"], HAND_DISTILLED_SKIP_REASON, with_kind=True)
        for write in (gone_doc, gone_rule, gone_skill)
    ]
    assert (result.documents_left_out, result.unknown_source_keys) == ([], [])
    # The ordinary session is still named behind the document it shares with the taken-out one.
    assert shared_doc.source_session_keys == ["s-normal", "s-gone"]

    # Such a session is not admitted by the batch, and an item that cites it is still left out.
    not_admitted = _filter([hand_doc, normal_doc], ["s-normal"], taken_out=("s-hand",))
    assert not_admitted.kept == [normal_doc]
    assert [item["source_session_keys"] for item in not_admitted.items_left_out] == [["s-hand"]]

    # The rule and the Skill plan of a governance-only session are what it was queued for.
    governance = _filter([hand_doc, gone_rule, gone_skill, normal_doc], ["s-normal", "s-hand"], governance_only=("s-hand",))
    assert governance.kept == [gone_rule, gone_skill, normal_doc]
    assert governance.items_left_out == []


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


@pytest.mark.parametrize(
    "reason_code, status, reason",
    [
        (HAND_DISTILLED_SKIP_REASON, HAND_DISTILLED_SKIP_STATUS, "会话已被声明 hand_distilled skip（完全跳过），本批不产出任何内容"),
        (NO_SESSION_REASON, NO_SESSION_STATUS, "本批没有准入任何会话，该项没有可依托的送审会话，未写入"),
    ],
)
def test_the_receipt_of_an_item_left_out_says_which_kind_and_why(reason_code: str, status: str, reason: str):
    receipt = left_out_item_receipt(
        {
            "title": "gone-note复盘",
            "relative_path": "lessons/gone.md",
            "target_kind": "knowledge_doc",
            "source_session_keys": ["s-gone"],
            "reason_code": reason_code,
        }
    )
    assert receipt == {
        "relative_path": "lessons/gone.md",
        "title": "gone-note复盘",
        "target_kind": "knowledge_doc",
        "status": status,
        "skipped": True,
        "written": False,
        "reason_code": reason_code,
        "reason": reason,
        "source_session_keys": ["s-gone"],
    }
    # An item that does not say why was left out is reported as the hand-distilled kind, never as written.
    bare = left_out_item_receipt({"title": "t", "relative_path": None, "target_kind": "flywheel_rule"})
    assert (bare["status"], bare["reason_code"], bare["written"]) == (HAND_DISTILLED_SKIP_STATUS, HAND_DISTILLED_SKIP_REASON, False)


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


@pytest.mark.parametrize(
    "explicit_as, leftover_queued",
    [
        pytest.param("session_keys", False, id="session_keys"),
        pytest.param("queue_ids", False, id="queue_ids-already-excluded"),
        pytest.param("queue_ids", True, id="queue_ids-leftover-queued"),
        pytest.param(None, True, id="ordinary-batch-leftover-queued"),
    ],
)
def test_a_session_declared_skip_is_not_admitted_even_when_a_batch_names_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_as: str | None,
    leftover_queued: bool,
):
    """`governance_only=false` 的会话被显式点名进批：默认不得准入，本批不产出它的任何内容，并留审计。"""
    rt, skills = _runtime(tmp_path)
    gone = "codex:s-gone"
    _register(rt, "s-gone")
    (queued,) = rt.queue.on_explicit_enqueue(
        client="codex",
        session_id="s-gone",
        title="接口超时排查",
        project="demo",
        content="用户：接口联调报错了怎么办。助手：先看日志，再核对连接池上限。",
    )
    taken_out = _declare_hand_distilled(rt, gone, governance_only=False)
    assert (taken_out["status"], taken_out["distill_mode"]) == ("excluded", "skipped")
    assert rt.db.get_queue_item(queued.queue_id).status is QueueStatus.EXCLUDED
    if leftover_queued:
        # Left in the queue by a run that ended after the declaration was made.
        rt.db.update_queue_item_status(
            queued.queue_id, QueueStatus.QUEUED, actor="test", summary="left behind by a run that ended"
        )
    # The model is not told the session was left out: whatever it still puts behind it, or behind a
    # key of its own invention, is dropped — the batch admitted nothing that could stand behind it.
    _use_model(
        monkeypatch,
        _FakeModel(
            _review(gone),
            [
                _doc("gone-note", gone),
                _doc("stray-note", "codex:s-made-up"),
                _rule(gone),
                _skill("gone-checklist", gone),
            ],
        ),
    )

    named = {}
    if explicit_as == "session_keys":
        named = {"session_keys": [gone]}
    elif explicit_as == "queue_ids":
        named = {"queue_ids": [queued.queue_id]}
    run = rt.orchestrator.start_run(trigger="test", phase="daily", **named)

    assert run.status == RunStatus.PASS, run.error
    assert run.admitted_sessions == []
    assert run.planned_writes == []
    p01 = _node(run, "P01_BEGIN")
    assert p01["payload"]["hand_distilled_skipped_session_keys"] == [gone]
    assert p01["payload"]["admitted_sessions"] == []
    assert p01["payload"]["hand_distilled_excluded_queue_ids"] == ([queued.queue_id] if leftover_queued else [])
    assert "准入 0 个会话单元" in p01["summary"]
    assert "1 个会话已声明 hand_distilled skip（完全跳过），未准入" in p01["summary"]
    (not_admitted,) = [e for e in _events(rt, run.run_id) if e["event_type"] == "hand_distilled_skip_not_admitted"]
    assert (not_admitted["actor"], not_admitted["payload"]["session_keys"]) == ("p01_begin", [gone])
    p03 = _node(run, "P03_ROUTE_PLAN")["payload"]
    assert [(item["target_kind"], item["reason_code"]) for item in p03["items_left_out"]] == [
        ("knowledge_doc", HAND_DISTILLED_SKIP_REASON),
        ("knowledge_doc", NO_SESSION_REASON),
        ("flywheel_rule", HAND_DISTILLED_SKIP_REASON),
        ("skill_improvement", HAND_DISTILLED_SKIP_REASON),
    ]
    assert p03["plan_item_unknown_source_keys"] == [
        {
            "title": "stray-note复盘",
            "target_kind": "knowledge_doc",
            "source_session_keys": ["codex:s-made-up"],
            "unknown_source_keys": ["codex:s-made-up"],
        }
    ]

    # Nothing of this session was written: no document, rule, Skill, mistake or dissent.
    assert _knowledge_files(tmp_path) == []
    assert rt.db.list_rules() == []
    assert rt.db.list_mistake_observations() == []
    assert rt.db.list_dissent_records() == []
    assert list(skills.iterdir()) == []
    # The item is left in the end state the declaration gave it, not claimed by the run.
    item = rt.db.get_queue_item(queued.queue_id)
    assert (item.status, item.exclude_reason_code, item.run_id) == (QueueStatus.EXCLUDED, HAND_DISTILLED_SKIP, None)


# ---------------------------------------------------------------- P1-3: taken out of distillation while the batch ran


def test_a_session_declared_skip_while_the_batch_waited_for_approval_produces_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """等待审批期间声明 `governance_only=false` 再批准恢复：该会话零文档、零规则、零 Skill、零错题、零分歧。"""
    rt, skills = _runtime(tmp_path)
    late = "codex:s-late"
    late_path = "lessons/2026-11-25-late-note.md"
    _register(rt, "s-late")
    _register(rt, "s-normal")
    _enqueue(rt, late)
    _enqueue(rt, NORMAL)
    late_rule = {
        **_rule(late),
        "summary": "改测试库配置前先确认环境",
        "rule_payload": {
            "title": "改测试库配置前先确认环境",
            "domain": "engineering",
            "projects": ["demo"],
            "triggers": ["测试库", "改配置"],
            "checklist": ["改前确认环境名", "改完回读生效值"],
            "forbidden_routes": ["凭记忆直接改配置"],
            "root_cause_key": "wrong-env-config",
        },
    }
    def _review_of(session_key: str, *, root_cause_key: str, dissent_target: str) -> dict[str, Any]:
        """The standard review of one session, with a mistake and a dissent that are its own."""
        base = _review(session_key)
        return {
            **base,
            "mistakes": [{**base["mistakes"][0], "root_cause_key": root_cause_key}],
            "dissents": [{**base["dissents"][0], "target_id": dissent_target}],
        }

    late_review = _review_of(late, root_cause_key="wrong-env-config", dissent_target="rule-env-confirm")
    normal_review = _review_of(NORMAL, root_cause_key="missing-null-guard", dissent_target="rule-cache-check")
    _use_model(
        monkeypatch,
        _FakeModel(
            {
                **normal_review,
                "findings": [*late_review["findings"], *normal_review["findings"]],
                "mistakes": [*late_review["mistakes"], *normal_review["mistakes"]],
                "dissents": [*late_review["dissents"], *normal_review["dissents"]],
            },
            [
                _doc("late-note", late),
                late_rule,
                _skill("late-checklist", late),
                _doc("normal-note", NORMAL),
                _rule(NORMAL),
            ],
        ),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert sorted(run.admitted_sessions) == sorted([late, NORMAL])
    assert [w.target_kind for w in run.planned_writes] == [
        "knowledge_doc",
        "flywheel_rule",
        "skill_improvement",
        "knowledge_doc",
        "flywheel_rule",
    ]
    assert _node(run, "P03_ROUTE_PLAN")["payload"]["items_left_out"] == []
    (late_item,) = rt.db.list_session_queue_items(late, statuses=[QueueStatus.PROCESSING])

    _declare_hand_distilled(rt, late, governance_only=False)
    done = rt.orchestrator.approve_and_resume(run.run_id, source="test")

    assert done.status == RunStatus.PASS, done.error
    # The other session of the batch is not touched: its document, rule, mistake and dissent land.
    assert _knowledge_files(tmp_path) == [NORMAL_PATH]
    assert [r.title for r in rt.db.list_rules()] == ["调用外部接口前先校验入参"]
    assert sorted(path.name for path in skills.iterdir()) == []
    assert [o["root_cause_key"] for o in rt.db.list_mistake_observations()] == ["missing-null-guard"]
    assert [d["session_key"] for d in rt.db.list_dissent_records()] == [NORMAL]

    p04 = _node(done, "P04_WRITE_VERIFY")
    assert p04["payload"]["written_paths"] == [NORMAL_PATH]
    assert p04["payload"]["review_candidates_left_out"] == {"mistakes": 1, "dissents": 1}
    assert [(i["target_kind"], i["relative_path"]) for i in p04["payload"]["items_left_out"]] == [
        ("knowledge_doc", late_path),
        ("flywheel_rule", None),
        ("skill_improvement", "late-checklist/SKILL.md"),
    ]
    receipts = [r for r in p04["payload"]["write_receipts"] if r.get("status") == HAND_DISTILLED_SKIP_STATUS]
    assert [(r["target_kind"], r["written"]) for r in receipts] == [
        ("knowledge_doc", False),
        ("flywheel_rule", False),
        ("skill_improvement", False),
    ]
    assert "3 项计划因所属会话已被声明 hand_distilled skip（完全跳过）或本批未准入任何会话，未写入" in p04["summary"]
    assert "2 条审查候选因所属会话已被声明完全跳过，未入池" in p04["summary"]
    left_out = [e for e in _events(rt, run.run_id) if e["event_type"] == "plan_items_left_out"]
    assert [(e["actor"], len(e["payload"]["items"])) for e in left_out] == [("p04_write", 3)]
    (candidates,) = [e for e in _events(rt, run.run_id) if e["event_type"] == "review_candidates_left_out"]
    assert (candidates["actor"], candidates["payload"]) == (
        "p04_write",
        {
            "mistakes": 1,
            "dissents": 1,
            "taken_out_session_keys": [late],
            "admitted_session_keys": run.admitted_sessions,
        },
    )

    # P05: the claimed item of the session ends in a clear end state, and the session is not settled as distilled.
    p05 = _node(done, "P05_SETTLE")
    assert p05["payload"]["hand_distilled_skipped_sessions"] == [late]
    assert p05["payload"]["settled_sessions"] == [NORMAL]
    assert p05["payload"]["excluded_queue_ids"] == [late_item.queue_id]
    assert p05["payload"]["settled_queue_ids"] == [
        item.queue_id for item in rt.db.list_session_queue_items(NORMAL, statuses=[QueueStatus.PROCESSED])
    ]
    settled = rt.db.get_queue_item(late_item.queue_id)
    assert (settled.status, settled.exclude_reason_code) == (QueueStatus.EXCLUDED, HAND_DISTILLED_SKIP)
    assert "hand-distilled by the client while the batch ran" in (settled.exclude_reason_detail or "")
    assert rt.db.get_session(late).status is SessionStatus.ARCHIVED
    assert rt.db.get_session(NORMAL).status is SessionStatus.PROCESSED
    assert rt.db.get_session(late).last_consumed_turn == 0  # the watermark did not move over it
    assert "1 个会话已声明 hand_distilled skip（未沉淀，队列项标记 excluded）" in p05["summary"]
    (settled_event,) = [e for e in _events(rt, run.run_id) if e["event_type"] == "hand_distilled_skip_settled"]
    assert (settled_event["actor"], settled_event["payload"]) == (
        "p05_settle",
        {"session_keys": [late], "excluded_queue_ids": [late_item.queue_id], "reason_code": HAND_DISTILLED_SKIP},
    )
    assert "1 段已声明 hand_distilled skip，未沉淀" in done.digest_markdown
    assert rt.db.get_workflow_lock() is None


# ---------------------------------------------------------------- P1-4: unknown source keys, and the race right before a write


def test_in_a_batch_of_hand_distilled_sessions_only_a_document_citing_an_unknown_key_is_still_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """全批 governance_only + 拼错/编造的 `source_session_keys`：文档照样过滤，未知来源键剥离并留审计。"""
    rt, _ = _runtime(tmp_path)
    _register(rt, "s-hand")
    _declare_hand_distilled(rt)
    _use_model(
        monkeypatch,
        _FakeModel(
            _review(HAND),
            [_doc("misspelt", "codex:s-hnad"), _doc("loose", "codex:s-made-up"), _rule(HAND)],
        ),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert [w.target_kind for w in run.planned_writes] == ["flywheel_rule"]
    p03 = _node(run, "P03_ROUTE_PLAN")["payload"]
    assert [(doc["title"], doc["source_session_keys"]) for doc in p03["governance_only_skipped_docs"]] == [
        ("misspelt复盘", ["codex:s-hnad"]),
        ("loose复盘", ["codex:s-made-up"]),
    ]
    assert p03["plan_item_unknown_source_keys"] == [
        {
            "title": "misspelt复盘",
            "target_kind": "knowledge_doc",
            "source_session_keys": ["codex:s-hnad"],
            "unknown_source_keys": ["codex:s-hnad"],
        },
        {
            "title": "loose复盘",
            "target_kind": "knowledge_doc",
            "source_session_keys": ["codex:s-made-up"],
            "unknown_source_keys": ["codex:s-made-up"],
        },
    ]
    (reported,) = [e for e in _events(rt, run.run_id) if e["event_type"] == "plan_item_unknown_source_keys"]
    assert (reported["actor"], reported["payload"]["admitted_session_keys"]) == ("p03_plan", [HAND])
    assert len(reported["payload"]["items"]) == 2

    done = rt.orchestrator.approve_and_resume(run.run_id, source="test")

    assert done.status == RunStatus.PASS, done.error
    assert _knowledge_files(tmp_path) == []
    assert [r.title for r in rt.db.list_rules()] == ["调用外部接口前先校验入参"]


def test_in_a_mixed_batch_an_unknown_source_key_is_stripped_and_the_ordinary_session_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """混合批：未知键先剥离再判断；有效来源里仍有普通会话的文档照常进入计划。"""
    rt, _ = _runtime(tmp_path)
    _register(rt, "s-normal")
    _register(rt, "s-hand")
    _enqueue(rt, NORMAL)
    _declare_hand_distilled(rt)
    _use_model(
        monkeypatch,
        _FakeModel(
            _review(NORMAL, HAND),
            [
                _doc("normal-note", NORMAL, "codex:s-hnad"),
                _doc("hand-note", "codex:s-hnad", HAND),
                _doc("stray-note", "codex:s-made-up"),
            ],
        ),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert sorted(run.admitted_sessions) == sorted([NORMAL, HAND])
    planned = [(w.target_kind, w.relative_path, w.source_session_keys) for w in run.planned_writes]
    # The document that stands for the ordinary session is planned; the one behind the hand-distilled
    # session alone (its other key is no session of the batch) is not.
    assert [(kind, path) for kind, path, _ in planned] == [
        ("knowledge_doc", NORMAL_PATH),
        ("knowledge_doc", "lessons/2026-11-25-stray-note.md"),
    ]
    assert all("codex:s-hnad" not in sources for _, _, sources in planned)
    p03 = _node(run, "P03_ROUTE_PLAN")["payload"]
    assert [doc["relative_path"] for doc in p03["governance_only_skipped_docs"]] == [HAND_PATH]
    assert [item["unknown_source_keys"] for item in p03["plan_item_unknown_source_keys"]] == [
        ["codex:s-hnad"],
        ["codex:s-hnad"],
        ["codex:s-made-up"],
    ]


def test_a_session_declared_hand_distilled_while_an_earlier_item_was_being_written_is_not_written_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """P04 写入竞态：写第一篇文档期间，第二篇的来源会话被声明手工沉淀 → 第二篇写前复查，不再落盘。"""
    rt, _ = _runtime(tmp_path)
    second = "codex:s-second"
    first_path = "lessons/2026-11-25-first-note.md"
    second_path = "lessons/2026-11-25-second-note.md"
    _register(rt, "s-first")
    _register(rt, "s-second")
    _enqueue(rt, "codex:s-first")
    _enqueue(rt, second)
    _use_model(
        monkeypatch,
        _FakeModel(_review("codex:s-first", second), [_doc("first-note", "codex:s-first"), _doc("second-note", second)]),
    )

    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert [w.relative_path for w in run.planned_writes] == [first_path, second_path]

    real_write = rt.knowledge.write_document_verified
    written: list[str] = []

    def _write_then_declare(rel: str, content: str, **kwargs: Any) -> dict[str, Any]:
        res = real_write(rel, content, **kwargs)
        written.append(rel)
        if rel == first_path:
            # The client writes the other session's knowledge up by hand while this batch is writing.
            _declare_hand_distilled(rt, second)
        return res

    monkeypatch.setattr(rt.knowledge, "write_document_verified", _write_then_declare)
    done = rt.orchestrator.approve_and_resume(run.run_id, source="test")

    assert done.status == RunStatus.PASS, done.error
    assert written == [first_path]
    assert _knowledge_files(tmp_path) == [first_path]
    p04 = _node(done, "P04_WRITE_VERIFY")
    assert p04["payload"]["written_paths"] == [first_path]
    (receipt, *rest) = [r for r in p04["payload"]["write_receipts"] if r.get("status") == GOVERNANCE_ONLY_SKIP_STATUS]
    assert (receipt["relative_path"], receipt["skipped"], receipt["written"]) == (second_path, True, False)
    assert rest == []
    assert p04["payload"]["governance_only_skipped_docs"] == [
        {
            "title": "second-note复盘",
            "relative_path": second_path,
            "action": "create",
            "source_session_keys": [second],
            "reason_code": GOVERNANCE_ONLY_SKIP_REASON,
        }
    ]
    (late_event,) = [e for e in _events(rt, run.run_id) if e["event_type"] == "governance_only_wiki_skipped"]
    assert late_event["actor"] == "p04_write"
    assert "写入前被声明手工沉淀" in late_event["summary"]


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
    # `always` pauses for its own reason; `auto` releases routine writes only — never an incident card.
    assert pause([card], "always") == (True, ["当前为每次人工审批模式（共 1 项）"])
    assert pause([card], "auto") == (True, [INCIDENT_APPROVAL_REASON])
    assert pause([card], "auto", high_write_threshold=1000) == (True, [INCIDENT_APPROVAL_REASON])
    assert pause([routine], "auto") == (False, [])
    assert pause([routine] * 20, "auto") == (False, [])  # the size threshold is a `smart` rule only
    assert pause([card, routine], "auto") == (True, [INCIDENT_APPROVAL_REASON])
    # Under `auto` too, a card the plan did not flag is held, and it carries the one standing reason.
    assert pause([unflagged], "auto") == (True, [INCIDENT_APPROVAL_REASON])
    bare = _incident_write(requires_approval=True)
    assert pause([bare], "auto") == (True, [INCIDENT_APPROVAL_REASON])
    # A routine document that merely asks for approval is not an incident card: `auto` releases it.
    asking = _write("knowledge_doc", ["codex:s-normal"], "lessons/2026-11-25-normal-note.md")
    asking.requires_approval = True
    asking.approval_reason = "配置页属于敏感写入，需人工确认后才会落盘"
    assert pause([asking], "auto") == (False, [])
    assert pause([card, asking], "auto") == (True, [INCIDENT_APPROVAL_REASON])
    assert pause([], "auto") == (False, [])


# ---------------------------------------------------------------- the safety valve and `auto` (P0-1)


CARD_PATH = "lessons/ai-incidents/2026-11-27-wrong-config.md"


def _card(source: str = NORMAL) -> dict[str, Any]:
    """An incident card in a plan: known by the directory it is written to, flagged or not."""
    return {**_doc("wrong-config", source), "relative_path": CARD_PATH, "title": "改错测试库配置复盘"}


@pytest.mark.parametrize(
    "configured, valve, asked, expected",
    [
        pytest.param("smart", False, None, "smart", id="configured-smart"),
        pytest.param("always", False, None, "always", id="configured-always"),
        pytest.param("auto", False, None, "smart", id="configured-auto-closed-valve"),
        pytest.param("auto", True, None, "auto", id="configured-auto-open-valve"),
        pytest.param("smart", False, False, "always", id="asked-always"),
        pytest.param("auto", True, False, "always", id="asked-always-wins"),
        pytest.param("smart", True, True, "auto", id="asked-auto-open-valve"),
        pytest.param("always", True, True, "auto", id="asked-auto-open-valve-over-always"),
        pytest.param("smart", False, True, "smart", id="asked-auto-closed-valve"),
        pytest.param("auto", False, True, "smart", id="asked-auto-closed-valve-configured-auto"),
    ],
)
def test_the_approval_mode_of_a_batch_is_decided_once_and_the_valve_has_the_last_word(
    configured: str, valve: bool, asked: bool | None, expected: str
):
    """入口只有一个口径：配置、CLI、门户同步/后台传来的 auto 都受安全阀约束，与 `effective_approval_mode` 一致。"""
    cfg = FlywheelConfig()
    cfg.workflow.approval_mode = configured
    cfg.safety_valves.auto_approve_writes = valve

    assert resolve_approval_mode(cfg, asked) == expected
    # Asked for nothing, the batch runs the configured mode exactly as the configuration reads it.
    assert resolve_approval_mode(cfg, None) == cfg.effective_approval_mode
    if asked is True and not valve:
        # What the valve does to a configured `auto` is what it does to a requested one.
        cfg.workflow.approval_mode = "auto"
        assert resolve_approval_mode(cfg, True) == resolve_approval_mode(cfg, None) == "smart"


def _awaiting_run(rt: FlywheelRuntime, run_id: str) -> WorkflowRunRecord:
    """The run a worker thread is executing, once it exists and has left `RUNNING`."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = rt.db.get_workflow_run(run_id)
        if run is not None and run.status is not RunStatus.RUNNING:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never left RUNNING")


@pytest.mark.parametrize("entry", ["direct", "portal-sync", "portal-background"])
def test_auto_approve_cannot_get_past_a_closed_safety_valve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
):
    """安全阀关闭时，任何入口请求的自动放行都降为 smart：敏感写入（Skill）照样挂起 AWAITING_APPROVAL，并留审计。

    The CLI flag and the background trigger call `start_run` with the same argument as the
    `direct` case; the portal has two roads of its own, both exercised here.
    """
    from fastapi.testclient import TestClient

    from ai_flywheel.app import create_app

    rt, skills = _runtime(tmp_path)
    assert rt.cfg.safety_valves.auto_approve_writes is False
    _register(rt, "s-normal")
    _enqueue(rt, NORMAL)
    _use_model(
        monkeypatch,
        _FakeModel(_review(NORMAL), [_doc("normal-note", NORMAL), _skill("api-checklist", NORMAL)]),
    )

    if entry == "direct":
        run_id = rt.orchestrator.start_run(trigger="test", phase="daily", auto_approve_override=True).run_id
    else:
        client = TestClient(create_app(runtime=rt))
        if entry == "portal-sync":
            response = client.post("/api/site/runs", json={"phase": "daily", "auto_approve": True})
            assert response.status_code == 200, response.text
            run_id = response.json()["run"]["run_id"]
            assert response.json()["run"]["status"] == "AWAITING_APPROVAL"
        else:
            response = client.post(
                "/api/site/runs", json={"phase": "daily", "auto_approve": True}, params={"background": "true"}
            )
            assert response.status_code == 200, response.text
            assert response.json()["background"] is True
            run_id = response.json()["run_id"]
    run = _awaiting_run(rt, run_id)

    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.approval_mode == "smart"
    assert [w.target_kind for w in run.planned_writes] == ["knowledge_doc", "skill_improvement"]
    (vetoed,) = [e for e in _events(rt, run_id) if e["event_type"] == "auto_approve_vetoed"]
    assert (vetoed["actor"], vetoed["payload"]) == ("safety_valve", {"requested": "auto", "effective": "smart"})
    (awaiting,) = [e for e in _events(rt, run_id) if e["event_type"] == "awaiting_approval"]
    assert awaiting["payload"]["reasons"] == ["每次联调都要重复同样的检查步骤"]
    # Nothing was written: the batch is held, not waved through.
    assert _knowledge_files(tmp_path) == []
    assert list(skills.iterdir()) == []
    assert rt.db.list_mistake_observations() == []


def test_a_configured_auto_runs_smart_while_the_valve_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """配置写的是 auto、安全阀关闭：批次按 smart 运行，事故卡照常挂起。"""
    rt, _ = _runtime(tmp_path)
    rt.cfg.workflow.approval_mode = "auto"
    assert rt.cfg.effective_approval_mode == "smart"
    _register(rt, "s-normal")
    _enqueue(rt, NORMAL)
    _use_model(monkeypatch, _FakeModel(_review(NORMAL), [_doc("normal-note", NORMAL), _card()]))

    run = rt.orchestrator.start_run(trigger="scheduled", phase="daily")

    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.approval_mode == "smart"
    (awaiting,) = [e for e in _events(rt, run.run_id) if e["event_type"] == "awaiting_approval"]
    assert awaiting["payload"]["reasons"] == ["重要变更：改错测试库配置复盘"]  # smart names the sensitive card
    assert "auto_approve_vetoed" not in [e["event_type"] for e in _events(rt, run.run_id)]
    assert _knowledge_files(tmp_path) == []


def test_with_the_valve_open_auto_releases_routine_writes_but_never_an_incident_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """合法 auto（安全阀打开）：常规写入照常放行；计划里有一张事故卡就挂起，理由是统一的事故卡理由。"""
    rt, _ = _runtime(tmp_path)
    rt.cfg.workflow.approval_mode = "auto"
    rt.cfg.safety_valves.auto_approve_writes = True
    _register(rt, "s-routine")
    _register(rt, "s-card")
    routine, card_session = "codex:s-routine", "codex:s-card"
    _enqueue(rt, routine)
    _use_model(monkeypatch, _FakeModel(_review(routine), [_doc("routine-note", routine), _rule(routine)]))

    released = rt.orchestrator.start_run(trigger="test", phase="daily")

    assert released.status == RunStatus.PASS, released.error
    assert released.approval_mode == "auto"
    assert _knowledge_files(tmp_path) == ["lessons/2026-11-25-routine-note.md"]
    assert len(rt.db.list_rules()) == 1
    (passed,) = [e for e in _events(rt, released.run_id) if e["event_type"] == "auto_approved"]
    assert passed["payload"] == {"approval_mode": "auto", "planned_count": 2}

    # The same legitimate mode, a plan with one incident card in it: held for a human, nothing written.
    _enqueue(rt, card_session)
    _use_model(
        monkeypatch,
        _FakeModel(_review(card_session), [_doc("card-note", card_session), _card(card_session)]),
    )
    held = rt.orchestrator.start_run(trigger="test", phase="daily", auto_approve_override=True)

    assert held.status == RunStatus.AWAITING_APPROVAL
    assert held.approval_mode == "auto"
    assert [w.relative_path for w in held.planned_writes] == ["lessons/2026-11-25-card-note.md", CARD_PATH]
    (awaiting,) = [e for e in _events(rt, held.run_id) if e["event_type"] == "awaiting_approval"]
    assert awaiting["payload"]["reasons"] == [INCIDENT_APPROVAL_REASON]
    assert _knowledge_files(tmp_path) == ["lessons/2026-11-25-routine-note.md"]
    assert "auto_approve_vetoed" not in [e["event_type"] for e in _events(rt, held.run_id)]

    # A human lets the batch through: the card and the note land, and only then.
    done = rt.orchestrator.approve_and_resume(held.run_id, source="test")
    assert done.status == RunStatus.PASS, done.error
    assert _knowledge_files(tmp_path) == sorted(
        ["lessons/2026-11-25-routine-note.md", "lessons/2026-11-25-card-note.md", CARD_PATH]
    )
