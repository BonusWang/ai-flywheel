"""Knowledge-document routing by the mounted Wiki contract.

A P03 planner declares the page type and the project of every knowledge document; workspace and
draft evidence only validate that declaration. These tests cover the gate side: candidate
discovery, the deterministic route verdict, and normalization landing on the declared project.
"""

from __future__ import annotations

import json
import re
import shutil
import textwrap
from datetime import date
from pathlib import Path

import pytest
import yaml

from ai_flywheel.adapters.runners import (
    PLAN_SOURCE_MODEL,
    P03_REPAIR_SCHEMA_HINT,
    P03_REPAIR_SCHEMA_HINT_WITH_ROUTE,
    DryRunHeuristicRunner,
    OpenAICompatibleRunner,
    PlanOutcome,
    _build_p03_prompt,
    _plan_from_items,
    _plan_from_runner_output,
)
from ai_flywheel.adapters.wiki_gate import (
    ROUTE_DECLARED,
    ROUTE_EVIDENCE,
    ROUTE_PATH,
    UNROUTED_DOC_REASON,
    UNROUTED_REASON_LABEL,
    UNROUTED_REASON_TEXT,
    RoutingCandidates,
    WikiGateError,
    split_frontmatter,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core import orchestrator
from ai_flywheel.models import RegisterSessionRequest, RunStatus
from ai_flywheel.runtime import FlywheelRuntime

SCHEMA = textwrap.dedent(
    """\
    ---
    title: Test Wiki Schema
    description: Minimal schema for gate tests
    type: protocol
    created: 2026-01-01
    updated: 2026-01-01
    status: active
    authority: test
    load_policy: always
    core_contract:
      version: "2.1"
      core_files:
        - .wiki-schema.md
        - index-索引.md
      required_fields: [title, description, type, created, updated]
    partition_contract:
      version: "1.0"
      partitions:
        projects:
          root: wiki-知识库/projects-项目
        topics:
          root: wiki-知识库/topics-主题
    content_contract:
      schema_id: test-content-contract
      version: "1.4.1"
      legacy_type_aliases: {}
      scope_profiles:
        formal_active:
          partition_ids: [projects, topics]
      page_types:
        project-practice-index:
          partition: projects
          required_fields: [title, description, type, project_id, created, updated, status]
          path_pattern: "^[^/]+/实战笔记/笔记索引\\\\.md$"
          allowed_status: [active, deprecated, archived]
        project-practice-note:
          partition: projects
          required_fields: [title, description, type, project_id, record_id, event_date, load_policy, default_load, created, updated, status]
          path_pattern: "^[^/]+/实战笔记/([^/]+|(原文|提炼)/.+)\\\\.md$"
          allowed_status: [active, deprecated, archived]
        project-practice-lite:
          partition: projects
          required_fields: [title, description, type, project_id, record_id, load_policy, default_load, created, updated, status]
          path_pattern: "^[^/]+/实战笔记/[^/]+\\\\.md$"
          allowed_status: [active, deprecated, archived]
        topic:
          partition: topics
          required_fields: [title, description, type, tags, sources, confidence, created, updated, status]
          file_pattern: "^[^/]+\\\\.md$"
          allowed_status: [active]
    ---

    # Test Wiki Schema
    """
)

_UNSET = object()
TITLE = "受控写入门禁排查"
DRAFT = "---\ntitle: 受控写入门禁排查\ntags: [wiki]\n---\n\n# 受控写入门禁排查\n- 草稿必须在 preflight 前定稿。\n"
REL = "wiki-知识库/projects-项目/2026-11-24-受控写入门禁排查.md"

# (directory name, project_id, aliases) of two synthetic Wiki projects.
ALPHA = ("alpha-crm-Alpha客户平台", "alpha-crm", ["Alpha CRM", "客户平台"])
BETA = ("beta-bill-Beta账单", "beta-bill", ["Beta 账单"])


def _make_runtime(tmp_path: Path, *, allowed_prefixes: tuple[str, ...] = ("wiki-知识库",)) -> FlywheelRuntime:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "wiki-知识库" / "projects-项目").mkdir(parents=True)
    (wiki_root / "wiki-知识库" / "topics-主题").mkdir(parents=True)
    (wiki_root / ".wiki-schema.md").write_text(SCHEMA, encoding="utf-8")
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(wiki_root)
    cfg.knowledge_sink.allowed_write_prefixes = list(allowed_prefixes)
    cfg.knowledge_sink.wiki_gate_require_scripts = False
    return FlywheelRuntime(cfg)


def _add_project(
    wiki_root: Path,
    dir_name: str,
    project_id: str,
    aliases: tuple[str, ...] = (),
    *,
    description: str = "",
    with_practice: bool = True,
) -> None:
    """Register a Wiki project; only a directory that hosts 实战笔记/ can be a routing candidate."""
    project = wiki_root / "wiki-知识库" / "projects-项目" / dir_name
    if not with_practice:
        project.mkdir(parents=True)
        return
    practice = project / "实战笔记"
    practice.mkdir(parents=True)
    frontmatter = {
        "title": f"{project_id} 索引",
        "description": description or f"{project_id} 实战经验卡索引",
        "type": "project-practice-index",
        "project_id": project_id,
        "created": "2026-11-23",
        "updated": "2026-11-23",
        "status": "active",
    }
    if aliases:
        frontmatter["aliases"] = list(aliases)
    (practice / "笔记索引.md").write_text(
        f"---\n{yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n\n# {project_id} 索引\n",
        encoding="utf-8",
    )


def _declare(project: str | None, page_type: str = "project-practice-note", reason: str = "本篇是该项目的受控写入复盘"):
    """A planner routing declaration as it arrives on a PlannedWrite."""
    return {"page_type": page_type, "project": project, "reason": reason}


def _route(rt: FlywheelRuntime, **kwargs):
    params = {
        "relative_path": REL,
        "title": TITLE,
        "summary": "",
        "content": DRAFT,
        "wiki_route": None,
        "source_workspaces": [],
        "project_hint": "",
        "allow_evidence_fallback": True,
        "candidates": None,
    }
    params.update(kwargs)
    return rt.knowledge.wiki_gate.evaluate_route(**params)


def _lite_card(project_id: str) -> str:
    return textwrap.dedent(
        f"""\
        ---
        title: 受控写入门禁速查
        description: 症状：写入后字节不一致；组件：写入器；环境：本地
        type: project-practice-lite
        project_id: {project_id}
        record_id: lite-2026-11-24
        load_policy: on_demand
        default_load: false
        created: 2026-11-23
        updated: 2026-11-23
        status: active
        ---

        # 受控写入门禁速查

        ## 结论

        写入后比对哈希。

        ## 适用范围

        受控写入。

        ## 来源

        [[实战笔记索引]]
        """
    )


# ---------------- candidate discovery ----------------


def test_routing_candidates_follow_the_mounted_contract(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA, description="alpha-crm 的经验卡导航")
    _add_project(rt.knowledge.root, *BETA)
    _add_project(rt.knowledge.root, "gamma-ops", "gamma-ops", with_practice=False)

    candidates = rt.knowledge.wiki_gate.routing_candidates()

    # T5 is materialisable and its partition is writable; lite and topic are declared but not offered.
    assert candidates.page_types == ["project-practice-note"]
    assert [(p["dir_name"], p["project_id"], p["aliases"], p["description"]) for p in candidates.projects] == [
        (ALPHA[0], ALPHA[1], list(ALPHA[2]), "alpha-crm 的经验卡导航"),
            (BETA[0], BETA[1], list(BETA[2]), "beta-bill 实战经验卡索引"),
    ]
    assert candidates.routable is True
    assert candidates.to_dict() == {
        "page_types": ["project-practice-note"],
        "projects": [
            {
                "project": ALPHA[0],
                "project_id": ALPHA[1],
                "aliases": list(ALPHA[2]),
                "description": "alpha-crm 的经验卡导航",
            },
                {
                    "project": BETA[0],
                    "project_id": BETA[1],
                    "aliases": list(BETA[2]),
                    "description": "beta-bill 实战经验卡索引",
                },
        ],
    }


def test_routing_candidates_exclude_a_type_whose_partition_the_schema_cannot_resolve(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    schema_path = rt.knowledge.root / ".wiki-schema.md"
    schema_path.write_text(
        re.sub(r"(project-practice-note:\n\s+)partition: projects", r"\1partition: nowhere", SCHEMA),
        encoding="utf-8",
    )

    candidates = rt.knowledge.wiki_gate.routing_candidates()

    assert candidates.page_types == []
    assert candidates.routable is False


def test_routing_candidates_ignore_the_sink_write_prefixes(tmp_path: Path):
    # Write prefixes are configuration, not contract: a mismatch must still fail loudly in P04.
    rt = _make_runtime(tmp_path, allowed_prefixes=("wiki-知识库/topics-主题",))
    _add_project(rt.knowledge.root, *ALPHA)

    assert rt.knowledge.wiki_gate.routing_candidates().routable is True


def test_routing_candidates_are_empty_when_the_contract_lacks_a_materialisable_type(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    schema_path = rt.knowledge.root / ".wiki-schema.md"
    schema_path.write_text(SCHEMA.replace("project-practice-note:", "project-practice-heavy:"), encoding="utf-8")

    candidates = rt.knowledge.wiki_gate.routing_candidates()

    assert candidates.page_types == []
    assert candidates.routable is False


def test_routing_candidates_follow_project_directories(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    gate = rt.knowledge.wiki_gate
    projects_root = rt.knowledge.root / "wiki-知识库" / "projects-项目"

    assert [p["dir_name"] for p in gate.routing_candidates().projects] == [ALPHA[0]]

    # Hidden and archived directories that happen to host 实战笔记/ are not candidates.
    for name in (".hidden-tool", "archive-2026-old-tool"):
        (projects_root / name / "实战笔记").mkdir(parents=True)
    assert [p["dir_name"] for p in gate.routing_candidates().projects] == [ALPHA[0]]

    _add_project(rt.knowledge.root, *BETA)
    assert [p["dir_name"] for p in gate.routing_candidates().projects] == sorted([ALPHA[0], BETA[0]])

    shutil.rmtree(projects_root / ALPHA[0])
    assert [p["dir_name"] for p in gate.routing_candidates().projects] == [BETA[0]]


# ---------------- route verdict on a declaration ----------------


def test_declaration_agreeing_with_evidence_is_routed(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    decision = _route(rt, wiki_route=_declare("alpha-crm"), source_workspaces=["D:/work/alpha-crm"])

    assert (decision.routed, decision.source, decision.page_type, decision.project_dir) == (
        True,
        ROUTE_DECLARED,
        "project-practice-note",
        ALPHA[0],
    )
    assert decision.reason_code == ""
    assert decision.reason == "本篇是该项目的受控写入复盘"
    assert decision.evidence == {"workspace": [ALPHA[0]], "content": []}


def test_declaration_without_any_evidence_is_still_routed(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    # The draft names no project and the workspace matches none: no evidence, hence no conflict.
    decision = _route(rt, wiki_route=_declare("alpha-crm"))

    assert decision.routed is True
    assert decision.project_dir == ALPHA[0]
    assert decision.evidence == {"workspace": [], "content": []}


def test_several_content_hits_including_the_declared_project_are_not_a_conflict(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    decision = _route(
        rt,
        wiki_route=_declare("alpha-crm"),
        content=DRAFT.replace("草稿必须", "alpha-crm 与 beta-bill 的草稿必须"),
    )

    assert decision.routed is True
    assert sorted(decision.evidence["content"]) == sorted([ALPHA[0], BETA[0]])


@pytest.mark.parametrize("project", [None, "", "none", "None", "无归属"])
def test_declared_no_attribution_is_not_routed_and_keeps_the_reason(tmp_path: Path, project):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    # A workspace hit never turns "no attribution" into a conflict.
    decision = _route(
        rt,
        wiki_route=_declare(project, reason="跨项目治理审计，不属于单一项目"),
        source_workspaces=["D:/work/alpha-crm"],
    )

    assert (decision.routed, decision.reason_code, decision.reason) == (
        False,
        "declared_none",
        "跨项目治理审计，不属于单一项目",
    )


def test_declaration_outside_the_candidate_set_is_not_corrected(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    # Unknown project.
    assert _route(rt, wiki_route=_declare("gamma-ops")).reason_code == "declaration_invalid"
    # A prefix of a real identifier is not an identifier, even when the draft mentions alpha.
    assert (
        _route(
            rt,
            wiki_route=_declare("alpha"),
            content=DRAFT.replace("草稿必须", "alpha-crm 的草稿必须"),
        ).reason_code
        == "declaration_invalid"
    )
    # Page types the landing adapter cannot materialise are not offered.
    for page_type in ("topic", "project-practice-lite", "", None):
        assert _route(rt, wiki_route=_declare("alpha-crm", page_type=page_type)).reason_code == "declaration_invalid"


def test_declaration_matches_project_identifiers_exactly(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    for name in ("alpha-crm", "ALPHA-CRM", ALPHA[0], "「Alpha CRM」", " 客户平台 "):
        assert _route(rt, wiki_route=_declare(name)).project_dir == ALPHA[0]


def test_declaration_matching_several_candidates_is_invalid(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, ALPHA[0], ALPHA[1], ("shared-tool",))
    _add_project(rt.knowledge.root, BETA[0], BETA[1], ("shared-tool",))

    assert _route(rt, wiki_route=_declare("shared-tool")).reason_code == "declaration_invalid"


def test_declaration_contradicting_the_workspace_is_not_routed(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    decision = _route(rt, wiki_route=_declare("beta-bill"), source_workspaces=["D:/work/alpha-crm"])

    assert (decision.routed, decision.reason_code) == (False, "workspace_mismatch")
    assert decision.evidence == {"workspace": [ALPHA[0]], "content": []}
    assert decision.declared["project"] == "beta-bill"


def test_declaration_contradicting_the_draft_is_not_routed(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    decision = _route(
        rt,
        wiki_route=_declare("beta-bill"),
        content=DRAFT.replace("草稿必须", "alpha-crm 的草稿必须"),
    )

    assert (decision.routed, decision.reason_code) == (False, "content_mismatch")
    assert decision.evidence == {"workspace": [], "content": [ALPHA[0]]}


def test_workspace_agreement_outweighs_a_project_mentioned_in_the_draft(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    decision = _route(
        rt,
        wiki_route=_declare("alpha-crm"),
        source_workspaces=["D:/work/alpha-crm"],
        content=DRAFT.replace("草稿必须", "beta-bill 的草稿必须"),
    )

    assert decision.routed is True
    assert decision.evidence == {"workspace": [ALPHA[0]], "content": [BETA[0]]}


def test_content_evidence_scans_the_whole_draft(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)
    # The only project identifier sits well beyond the first 600 characters.
    draft = DRAFT + ("- 无关铺垫。" * 120) + "- alpha-crm 的要点。\n"

    decision = _route(rt, wiki_route=_declare("beta-bill"), content=draft)

    assert decision.reason_code == "content_mismatch"


# ---------------- plans without a declaration ----------------


def test_plan_without_declaration_from_a_model_runner_is_not_routed(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)

    # Workspace evidence would have selected alpha; it must not route an undeclared model plan.
    decision = _route(rt, source_workspaces=["D:/work/alpha-crm"], allow_evidence_fallback=False)

    assert (decision.routed, decision.reason_code, decision.source) == (False, "declaration_missing", "")


def test_dry_run_plan_without_declaration_keeps_evidence_routing(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)

    decision = _route(rt, source_workspaces=["D:/work/alpha-crm"])
    assert (decision.routed, decision.source, decision.page_type, decision.project_dir) == (
        True,
        ROUTE_EVIDENCE,
        "project-practice-note",
        ALPHA[0],
    )

    decision = _route(rt, project_hint="generic D:/notes")
    assert (decision.routed, decision.reason_code, decision.detail) == (False, "no_evidence", UNROUTED_DOC_REASON)

    rt.cfg.knowledge_sink.practice_fallback_project = BETA[0]
    decision = _route(rt, project_hint="generic D:/notes")
    assert (decision.routed, decision.source, decision.project_dir) == (True, ROUTE_EVIDENCE, BETA[0])


def test_empty_candidate_set_routes_nothing(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    alpha = {"dir_name": ALPHA[0], "project_id": ALPHA[1]}
    no_page_types = RoutingCandidates(page_types=[], projects=[alpha])
    no_projects = RoutingCandidates(page_types=["project-practice-note"], projects=[])

    for candidates in (no_page_types, no_projects):
        for wiki_route in (None, _declare("alpha-crm"), _declare(None)):
            assert _route(rt, wiki_route=wiki_route, candidates=candidates).reason_code == "no_routable_target"


# ---------------- a contract the gate cannot read ----------------


def _break_schema(rt: FlywheelRuntime) -> None:
    (rt.knowledge.root / ".wiki-schema.md").write_text(
        SCHEMA.replace("\ncontent_contract:\n", "\ncontent_contracts:\n"), encoding="utf-8"
    )


def test_unreadable_contract_is_reported_not_treated_as_nothing_routable(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _break_schema(rt)
    gate = rt.knowledge.wiki_gate

    # "Nothing routable" would quietly plan zero documents; a broken contract must fail the run instead.
    with pytest.raises(WikiGateError, match="content_contract"):
        gate.routing_candidates()
    # The write-time re-check leaves the document untouched, so the write gate reports the broken contract.
    assert _route(rt, wiki_route=_declare("alpha-crm")).passthrough is True
    assert gate.normalize_planned_document(REL, TITLE, "", DRAFT, wiki_route=_declare("alpha-crm")) == (REL, DRAFT)


# ---------------- drafts that already satisfy the schema ----------------


def _valid_card(rt: FlywheelRuntime) -> tuple[str, str]:
    decision = _route(rt, wiki_route=_declare("alpha-crm"))
    assert decision.routed is True
    return rt.knowledge.wiki_gate.normalize_planned_document(REL, TITLE, "", DRAFT, route=decision)


def test_compliant_card_keeps_its_path_when_the_declaration_agrees(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)
    rel, card = _valid_card(rt)

    decision = _route(rt, wiki_route=_declare("alpha-crm"), relative_path=rel, content=card)

    assert (decision.routed, decision.source, decision.page_type, decision.project_dir) == (
        True,
        ROUTE_DECLARED,
        "project-practice-note",
        ALPHA[0],
    )
    assert rt.knowledge.wiki_gate.normalize_planned_document(rel, TITLE, "", card, wiki_route=_declare("alpha-crm")) == (rel, card)
    # Without a declaration the path itself is the verdict (dry-run plans re-checked in P04).
    assert rt.knowledge.wiki_gate.normalize_planned_document(rel, TITLE, "", card) == (rel, card)
    assert _route(rt, relative_path=rel, content=card).source == ROUTE_PATH


def test_compliant_card_from_a_model_runner_still_needs_a_declaration(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    rel, card = _valid_card(rt)
    gate = rt.knowledge.wiki_gate

    # Placement alone is not a declaration: a model plan must state where the note belongs.
    decision = _route(rt, relative_path=rel, content=card, allow_evidence_fallback=False)

    assert (decision.routed, decision.reason_code) == (False, "declaration_missing")
    assert gate.normalize_planned_document(rel, TITLE, "", card, allow_evidence_fallback=False) is None


def test_compliant_card_is_checked_against_evidence_like_any_other_declaration(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)
    rel, card = _valid_card(rt)

    decision = _route(
        rt,
        wiki_route=_declare("alpha-crm"),
        relative_path=rel,
        content=card,
        source_workspaces=["D:/work/beta-bill"],
    )

    assert (decision.routed, decision.reason_code) == (False, "workspace_mismatch")
    assert rt.knowledge.wiki_gate.normalize_planned_document(
        rel, TITLE, "", card, wiki_route=_declare("alpha-crm"), source_workspaces=["D:/work/beta-bill"]
    ) is None


def test_compliant_card_is_not_routed_when_the_declaration_disagrees_with_its_path(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)
    rel, card = _valid_card(rt)
    gate = rt.knowledge.wiki_gate

    assert _route(rt, wiki_route=_declare("beta-bill"), relative_path=rel, content=card).reason_code == "path_mismatch"
    assert _route(rt, wiki_route=_declare(None), relative_path=rel, content=card).reason_code == "declared_none"
    assert gate.normalize_planned_document(rel, TITLE, "", card, wiki_route=_declare("beta-bill")) is None


def test_compliant_card_of_a_non_routable_type_is_not_routed(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    rel = f"wiki-知识库/projects-项目/{ALPHA[0]}/实战笔记/2026-11-24-受控写入门禁速查.md"
    card = _lite_card(ALPHA[1])
    gate = rt.knowledge.wiki_gate
    assert gate.validate_document(rel, card).ok

    decision = _route(rt, wiki_route=_declare("alpha-crm", page_type="project-practice-lite"), relative_path=rel, content=card)

    assert (decision.routed, decision.reason_code) == (False, "page_type_not_routable")
    assert gate.normalize_planned_document(rel, TITLE, "", card, wiki_route=_declare("alpha-crm")) is None


# ---------------- normalization lands on the route verdict ----------------


def test_normalize_lands_on_the_declared_project(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)
    gate = rt.knowledge.wiki_gate
    today = date.today().isoformat()

    rel, card = gate.normalize_planned_document(REL, TITLE, "", DRAFT, route=_route(rt, wiki_route=_declare("alpha-crm")))

    assert rel == f"wiki-知识库/projects-项目/{ALPHA[0]}/实战笔记/{today}-受控写入门禁排查.md"
    assert gate.validate_document(rel, card).ok
    frontmatter = yaml.safe_load(split_frontmatter(card)[0])
    assert frontmatter["type"] == "project-practice-note"
    assert frontmatter["project_id"] == ALPHA[1]
    # Handing over the declaration instead of a verdict lands on the same card.
    assert gate.normalize_planned_document(REL, TITLE, "", DRAFT, wiki_route=_declare("alpha-crm")) == (rel, card)


@pytest.mark.parametrize(
    "wiki_route",
    [
        None,
        _declare("alpha-crm", page_type="topic"),
        _declare("gamma-ops"),
        _declare(None, reason="跨项目治理审计"),
    ],
)
def test_normalize_returns_no_path_for_an_unrouted_decision(tmp_path: Path, wiki_route):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    gate = rt.knowledge.wiki_gate

    assert gate.normalize_planned_document(REL, TITLE, "", DRAFT, wiki_route=wiki_route, allow_evidence_fallback=False) is None


# ---------------- P03 prompt and plan items ----------------


def _p03_prompt(routing_candidates) -> str:
    return _build_p03_prompt(
        {"findings": [{"session_key": "codex:s1", "title": "受控写入复盘", "worthy": True}]},
        [],
        [],
        8,
        "wiki-知识库/projects-项目/",
        "daily",
        routing_candidates=routing_candidates,
    )


def _prompt_payload(prompt: str) -> dict:
    return json.loads(prompt[prompt.rindex('\n{"phase":') + 1 :])


def test_p03_prompt_carries_the_candidates_and_the_declaration_requirement(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA, description="alpha-crm 的经验卡导航")
    _add_project(rt.knowledge.root, *BETA)
    candidates = rt.knowledge.wiki_gate.routing_candidates()

    prompt = _p03_prompt(candidates)

    assert _prompt_payload(prompt)["routing_candidates"] == candidates.to_dict(description_limit=120)
    for needle in (
        "routing_candidates.page_types",
        "routing_candidates.projects",
        'EVERY "knowledge_doc" item MUST also carry "wiki_route"',
        '{"page_type": <one value copied from routing_candidates.page_types>',
        '"reason": "<= 40 Chinese characters',
        'set both "page_type" and "project" to null',
        "merely mentions a project in passing",
        "Never invent a page type or project outside routing_candidates",
        '"skill_route":{...},"wiki_route":{"page_type":"...","project":"... or null","reason":"..."}}]}',
    ):
        assert needle in prompt, needle
    # The requirement is attached to the knowledge_doc kind itself.
    assert next(line for line in prompt.splitlines() if line.startswith('1. "knowledge_doc"')).endswith(
        ' MUST also carry "wiki_route" (see WIKI ROUTING above).'
    )


def test_p03_prompt_tells_the_model_not_to_write_knowledge_docs_when_nothing_is_routable(tmp_path: Path):
    rt = _make_runtime(tmp_path)  # mounted Wiki without any project directory
    candidates = rt.knowledge.wiki_gate.routing_candidates()
    assert candidates.routable is False

    prompt = _p03_prompt(candidates)

    assert _prompt_payload(prompt)["routing_candidates"] == {"page_types": ["project-practice-note"], "projects": []}
    assert 'Do not output any "knowledge_doc" item' in prompt
    assert '"wiki_route"' not in prompt


def test_repair_hints_describe_the_same_item_contract():
    for hint in (P03_REPAIR_SCHEMA_HINT, P03_REPAIR_SCHEMA_HINT_WITH_ROUTE):
        assert hint.count("{") == hint.count("}")
        for key in ("target_kind", "action", "title", "summary", "relative_path", "source_session_keys"):
            assert f'"{key}"' in hint
    assert P03_REPAIR_SCHEMA_HINT_WITH_ROUTE.startswith(P03_REPAIR_SCHEMA_HINT[:-3])
    assert P03_REPAIR_SCHEMA_HINT_WITH_ROUTE.endswith(
        ',"wiki_route":{"page_type":"...","project":"... or null","reason":"..."}}]}'
    )


def test_api_runner_passes_candidates_into_the_prompt_and_the_repair_hint(tmp_path: Path, monkeypatch):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    candidates = rt.knowledge.wiki_gate.routing_candidates()
    runner = OpenAICompatibleRunner(rt.cfg, rt.fs_store)
    seen: dict = {}

    # Takes whatever keywords the runner passes: siblings keep adding tracing arguments to `_invoke_api`.
    def fake_invoke(stage, prompt, **kwargs):
        seen["prompt"] = prompt
        seen["repair"] = kwargs.get("repair_schema_hint")
        return {"planned_writes": []}

    monkeypatch.setattr(runner, "_invoke_api", fake_invoke)
    outcome = runner.plan_writes_detailed(
        {"findings": []},
        [],
        8,
        "wiki-知识库/projects-项目/",
        existing_skills=[],
        phase="daily",
        routing_candidates=candidates,
    )

    assert _prompt_payload(seen["prompt"])["routing_candidates"] == candidates.to_dict(description_limit=120)
    assert seen["repair"] == P03_REPAIR_SCHEMA_HINT_WITH_ROUTE
    assert outcome.diagnostics.source == PLAN_SOURCE_MODEL

    # Without candidates (not a contract-governed Wiki) the prompt and the repair hint stay as before.
    runner.plan_writes_detailed({"findings": []}, [], 8, "wiki-知识库/projects-项目/", existing_skills=[], phase="daily")
    assert "routing_candidates" not in seen["prompt"]
    assert seen["repair"] == P03_REPAIR_SCHEMA_HINT


def test_every_reason_code_has_a_label_and_a_sentence():
    assert set(UNROUTED_REASON_LABEL) == set(UNROUTED_REASON_TEXT)


def test_p03_prompt_caps_candidate_descriptions(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA, description="很长的项目说明。" * 40)

    candidates = _prompt_payload(_p03_prompt(rt.knowledge.wiki_gate.routing_candidates()))

    assert len(candidates["routing_candidates"]["projects"][0]["description"]) == 120


def test_p03_prompt_has_no_routing_block_without_candidates():
    prompt = _p03_prompt(None)

    assert "routing_candidates" not in prompt
    assert "wiki_route" not in prompt


def _plan_item(wiki_route=_UNSET) -> dict:
    item = {
        "target_kind": "knowledge_doc",
        "action": "create",
        "title": TITLE,
        "summary": "写入后必须比对草稿与目标文件的哈希。",
        "relative_path": REL,
        "source_session_keys": ["codex:s1"],
        "content_preview": DRAFT,
    }
    if wiki_route is not _UNSET:
        item["wiki_route"] = wiki_route
    return item


def _plan(wiki_route=_UNSET):
    return _plan_from_items(
        [_plan_item(wiki_route)],
        source=PLAN_SOURCE_MODEL,
        max_write_set=8,
        default_doc_prefix="wiki-知识库/projects-项目/",
        skills_root=None,
    ).planned_writes[0]


def test_plan_item_carries_the_routing_declaration_untouched():
    declaration = _declare("Alpha CRM", reason="本篇是该项目的受控写入复盘")

    assert _plan(declaration).wiki_route == declaration
    assert _plan().wiki_route is None


@pytest.mark.parametrize("malformed", ["alpha-crm", ["alpha-crm"], 42, True])
def test_malformed_declaration_is_carried_as_absent_not_guessed(malformed):
    # The item is kept; routing then reports the declaration as missing instead of guessing.
    assert _plan(malformed).wiki_route is None


# ---------------- end to end ----------------


def _api_runtime(tmp_path: Path) -> FlywheelRuntime:
    """Runtime whose plan must come from a model runner (the runner itself is replaced by a stub)."""
    rt = _make_runtime(tmp_path)
    rt.cfg.runner.mode = "api"
    rt.cfg.runner.api.base_url = "http://127.0.0.1:9/v1"  # never called: build_runner is replaced
    rt.cfg.runner.api.model = "test-model"
    rt.cfg.workflow.approval_mode = "auto"
    rt.cfg.safety_valves.auto_approve_writes = True
    return rt


def _register(rt: FlywheelRuntime, session_id: str, workspace: Path) -> str:
    rt.register_session(
        RegisterSessionRequest(
            client="claude-code",
            session_id=session_id,
            title=f"{session_id} 复盘",
            project="generic",
            workspace=str(workspace),
            summary="写入后必须比对哈希，否则可能出现内容漂移。",
            content="用户：写入成功了。助手：还需要比对草稿与目标文件的哈希。",
        )
    )
    return f"claude-code:{session_id}"


def _doc(title: str, session_key: str, wiki_route=_UNSET) -> dict:
    item = {
        "target_kind": "knowledge_doc",
        "action": "create",
        "title": title,
        "summary": f"{title}：写入后比对哈希。",
        "relative_path": f"wiki-知识库/projects-项目/2026-11-24-{title}.md",
        "source_session_keys": [session_key],
        "content_preview": DRAFT.replace(TITLE, title),
    }
    if wiki_route is not _UNSET:
        item["wiki_route"] = wiki_route
    return item


class _ModelPlanRunner:
    """Stands in for the configured model runner: replays a prepared plan, no network."""

    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.routing_candidates = None

    def review_sessions(self, sessions, open_retros, *, existing_skills=None, phase="daily") -> dict:
        return DryRunHeuristicRunner().review_sessions(
            sessions, open_retros, existing_skills=existing_skills, phase=phase
        )

    def plan_writes_detailed(
        self,
        review_findings,
        existing_rules,
        max_write_set,
        default_doc_prefix="lessons/",
        *,
        existing_skills=None,
        phase="daily",
        routing_candidates=None,
    ) -> PlanOutcome:
        self.routing_candidates = routing_candidates
        return _plan_from_runner_output(
            {"planned_writes": self.items},
            source=PLAN_SOURCE_MODEL,
            max_write_set=max_write_set,
            default_doc_prefix=default_doc_prefix,
            skills_root=None,
        )


def _node(run, node_id: str):
    return next(n for n in run.nodes if n.node_id == node_id)


def _cards(rt: FlywheelRuntime) -> list[str]:
    practice_root = rt.knowledge.root / "wiki-知识库" / "projects-项目"
    return sorted(
        p.relative_to(practice_root).as_posix() for p in practice_root.rglob("*.md") if p.name != "笔记索引.md"
    )


def test_run_routes_knowledge_docs_by_declaration_and_traces_every_verdict(tmp_path: Path, monkeypatch):
    rt = _api_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _add_project(rt.knowledge.root, *BETA)
    alpha_key = _register(rt, "alpha-1", tmp_path / "work" / "alpha-crm")  # workspace evidence: alpha
    notes_key = _register(rt, "notes-1", tmp_path / "notes")  # workspace evidence: none
    runner = _ModelPlanRunner(
        [
            _doc("工作区一致复盘", alpha_key, _declare("alpha-crm")),
            _doc("草稿无项目标识复盘", notes_key, _declare("alpha-crm")),  # the note that used to be dropped
            _doc("跨项目治理审计", notes_key, _declare(None, reason="跨项目治理审计，不属于单一项目")),
            _doc("声明与工作区冲突复盘", alpha_key, _declare("beta-bill")),
            _doc("缺少声明复盘", notes_key),
        ]
    )
    monkeypatch.setattr(orchestrator, "build_runner", lambda cfg, fs_store: runner)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[alpha_key, notes_key])

    assert run.status == RunStatus.PASS, run.error
    assert runner.routing_candidates.page_types == ["project-practice-note"]
    p03 = _node(run, "P03_ROUTE_PLAN")
    routed = {doc["title"]: doc for doc in p03.payload["routed_docs"]}
    unrouted = {doc["title"]: doc for doc in p03.payload["unrouted_docs"]}

    assert sorted(routed) == ["工作区一致复盘", "草稿无项目标识复盘"]
    assert routed["工作区一致复盘"]["route"]["source"] == ROUTE_DECLARED
    assert routed["工作区一致复盘"]["route"]["project"] == ALPHA[0]
    # The workspace path is also part of the search text, so it shows up under both kinds of evidence.
    assert routed["工作区一致复盘"]["route"]["evidence"] == {"workspace": [ALPHA[0]], "content": [ALPHA[0]]}
    # No identifier anywhere and a workspace that matches no project: the declaration alone routes it.
    assert routed["草稿无项目标识复盘"]["route"]["evidence"] == {"workspace": [], "content": []}
    assert routed["草稿无项目标识复盘"]["route"]["reason"] == "本篇是该项目的受控写入复盘"

    assert unrouted["跨项目治理审计"]["reason_code"] == "declared_none"
    assert unrouted["跨项目治理审计"]["declared"]["reason"] == "跨项目治理审计，不属于单一项目"
    assert unrouted["声明与工作区冲突复盘"]["reason_code"] == "workspace_mismatch"
    assert unrouted["声明与工作区冲突复盘"]["declared"]["project"] == "beta-bill"
    assert unrouted["声明与工作区冲突复盘"]["evidence"] == {"workspace": [ALPHA[0]], "content": [ALPHA[0]]}
    assert unrouted["缺少声明复盘"]["reason_code"] == "declaration_missing"
    assert set(unrouted["缺少声明复盘"]) >= {"title", "relative_path", "source_session_keys", "reason", "reason_code"}

    planned = [w for w in run.planned_writes if w.target_kind == "knowledge_doc"]
    assert sorted(w.title for w in planned) == sorted(routed)
    assert all(f"/{ALPHA[0]}/实战笔记/" in w.relative_path for w in planned)
    assert all(
        w.side_effects
        and w.side_effects[0]["kind"] == "practice_index_entry"
        and w.side_effects[0]["target_path"].endswith("/实战笔记/笔记索引.md")
        for w in planned
    )
    assert len(_cards(rt)) == 2
    alpha_index = (
        rt.knowledge.root
        / "wiki-知识库"
        / "projects-项目"
        / ALPHA[0]
        / "实战笔记"
        / "笔记索引.md"
    ).read_text(encoding="utf-8")
    for item in planned:
        assert alpha_index.count(item.relative_path.removesuffix(".md")) == 1
    assert rt.db.get_session(notes_key).status.value == "processed"

    events = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id, limit=50)
    by_type = {row["event_type"]: row for row in events}
    assert "3 篇笔记因无法归属项目未列入计划" in p03.summary
    assert "planned_doc_routed" in by_type and "planned_doc_unrouted" in by_type
    unrouted_payload = by_type["planned_doc_unrouted"]["payload"]["unrouted_docs"]
    assert sorted(doc["reason_code"] for doc in unrouted_payload) == [
        "declaration_missing",
        "declared_none",
        "workspace_mismatch",
    ]
    assert "3 篇笔记未列入写入计划" in by_type["planned_doc_unrouted"]["summary"]
    assert "2 篇笔记已确定归属" in by_type["planned_doc_routed"]["summary"]


def test_dry_run_run_keeps_evidence_routing_and_traces_its_source(tmp_path: Path):
    rt = _make_runtime(tmp_path)  # no runner configured: the heuristic planner, no declaration
    _add_project(rt.knowledge.root, *ALPHA)
    alpha_key = _register(rt, "alpha-1", tmp_path / "work" / "alpha-crm")

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[alpha_key], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    routed = _node(run, "P03_ROUTE_PLAN").payload["routed_docs"]
    assert [doc["route"]["source"] for doc in routed] == [ROUTE_EVIDENCE]
    assert routed[0]["route"]["project"] == ALPHA[0]
    assert len(_cards(rt)) == 1


def test_p03_counts_cards_and_shared_index_against_the_physical_write_limit(
    tmp_path: Path,
    monkeypatch,
):
    rt = _api_runtime(tmp_path)
    rt.cfg.knowledge_sink.max_write_set = 2
    _add_project(rt.knowledge.root, *ALPHA)
    alpha_key = _register(rt, "alpha-limit", tmp_path / "work" / "alpha-crm")
    runner = _ModelPlanRunner(
        [
            _doc("容量甲", alpha_key, _declare("alpha-crm")),
            _doc("容量乙", alpha_key, _declare("alpha-crm")),
        ]
    )
    monkeypatch.setattr(orchestrator, "build_runner", lambda cfg, fs_store: runner)

    run = rt.orchestrator.start_run(
        trigger="test",
        phase="daily",
        session_keys=[alpha_key],
    )

    assert run.status == RunStatus.PASS, run.error
    p03 = _node(run, "P03_ROUTE_PLAN")
    assert len(run.planned_writes) == 1
    assert len(p03.payload["index_capacity_drops"]) == 1
    assert p03.payload["index_capacity_drops"][0]["reason"] == "WRITE_SET_TOTAL_LIMIT"
    plan_check = next(
        item for item in p03.internal_checks if item["name"] == "写入计划项校验"
    )
    assert plan_check["detail"]["invalid"] == 0
    assert "1 篇笔记连同索引超过物理写入上限" in p03.summary
    assert len(_cards(rt)) == 1


def test_run_fails_at_p03_when_the_contract_cannot_be_read(tmp_path: Path, monkeypatch):
    rt = _api_runtime(tmp_path)
    _add_project(rt.knowledge.root, *ALPHA)
    _break_schema(rt)
    notes_key = _register(rt, "notes-1", tmp_path / "notes")
    runner = _ModelPlanRunner([_doc("合同损坏复盘", notes_key, _declare("alpha-crm"))])
    monkeypatch.setattr(orchestrator, "build_runner", lambda cfg, fs_store: runner)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[notes_key])

    assert run.status == RunStatus.FAILED
    p03 = _node(run, "P03_ROUTE_PLAN")
    assert p03.status.value == "FAILED" and "content_contract" in (p03.error or "")
    assert _cards(rt) == []
    assert rt.db.get_session(notes_key).status.value == "pending"


def test_p04_skips_a_stale_plan_item_that_no_longer_routes(tmp_path: Path, monkeypatch):
    rt = _api_runtime(tmp_path)
    rt.cfg.workflow.approval_mode = "always"
    _add_project(rt.knowledge.root, *ALPHA)
    notes_key = _register(rt, "notes-1", tmp_path / "notes")
    runner = _ModelPlanRunner([_doc("待审批复盘", notes_key, _declare("alpha-crm"))])
    monkeypatch.setattr(orchestrator, "build_runner", lambda cfg, fs_store: runner)
    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[notes_key])
    assert run.status == RunStatus.AWAITING_APPROVAL

    # A plan persisted before routing existed: generic draft, no declaration.
    stale = run.planned_writes[0]
    stale.relative_path = REL
    stale.content_preview = DRAFT
    stale.wiki_route = None
    rt.db.save_workflow_run(run)

    run = rt.orchestrator.approve_and_resume(run.run_id)

    assert run.status == RunStatus.PASS, run.error
    receipts = _node(run, "P04_WRITE_VERIFY").payload["write_receipts"]
    assert [(r["status"], r["reason_code"]) for r in receipts] == [("SKIPPED_UNROUTED_PROJECT", "declaration_missing")]
    assert _cards(rt) == []


def test_awaiting_run_persists_index_side_effect_and_legacy_plan_fails_closed(
    tmp_path: Path,
    monkeypatch,
):
    rt = _api_runtime(tmp_path)
    rt.cfg.workflow.approval_mode = "always"
    _add_project(rt.knowledge.root, *ALPHA)
    notes_key = _register(rt, "notes-1", tmp_path / "notes")
    runner = _ModelPlanRunner([_doc("审批副作用重载", notes_key, _declare("alpha-crm"))])
    monkeypatch.setattr(orchestrator, "build_runner", lambda cfg, fs_store: runner)

    run = rt.orchestrator.start_run(
        trigger="test",
        phase="daily",
        session_keys=[notes_key],
    )
    assert run.status == RunStatus.AWAITING_APPROVAL
    planned = run.planned_writes[0]
    assert planned.side_effects and planned.side_effects[0]["status"] == "update"

    reloaded = rt.db.get_workflow_run(run.run_id)
    assert reloaded is not None
    assert reloaded.planned_writes[0].side_effects == planned.side_effects

    # A plan persisted by an older release has no approved derived-write record.
    reloaded.planned_writes[0].side_effects = []
    rt.db.save_workflow_run(reloaded)
    run = rt.orchestrator.approve_and_resume(run.run_id)

    assert run.status == RunStatus.FAILED
    p04 = _node(run, "P04_WRITE_VERIFY")
    assert "outside the approved scope" in (p04.error or "")
    assert _cards(rt) == []
    assert rt.db.get_session(notes_key).status.value == "pending"
    assert _node(run, "P05_SETTLE").status.value == "SKIPPED"


def test_p04_fails_closed_when_the_approved_index_disappears_before_write(
    tmp_path: Path,
    monkeypatch,
):
    rt = _api_runtime(tmp_path)
    rt.cfg.workflow.approval_mode = "always"
    _add_project(rt.knowledge.root, *ALPHA)
    notes_key = _register(rt, "notes-2", tmp_path / "notes")
    runner = _ModelPlanRunner([_doc("索引消失失败关闭", notes_key, _declare("alpha-crm"))])
    monkeypatch.setattr(orchestrator, "build_runner", lambda cfg, fs_store: runner)
    run = rt.orchestrator.start_run(
        trigger="test",
        phase="daily",
        session_keys=[notes_key],
    )
    assert run.status == RunStatus.AWAITING_APPROVAL
    target = run.planned_writes[0].side_effects[0]["target_path"]
    (rt.knowledge.root / target).unlink()

    run = rt.orchestrator.approve_and_resume(run.run_id)

    assert run.status == RunStatus.FAILED
    p04 = _node(run, "P04_WRITE_VERIFY")
    assert "has no project notes index" in (p04.error or "")
    assert _cards(rt) == []
    assert rt.db.get_session(notes_key).status.value == "pending"
