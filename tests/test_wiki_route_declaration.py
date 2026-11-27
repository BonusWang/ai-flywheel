"""Knowledge-document routing by the mounted Wiki contract.

A P03 planner declares the page type and the project of every knowledge document; workspace and
draft evidence only validate that declaration. These tests cover the gate side: candidate
discovery, the deterministic route verdict, and normalization landing on the declared project.

The same contract also declares project-domain pages (content_contract.deposit_routing):
the last section covers their routing, their increments, and a sandbox replay of a whole run.
"""


from __future__ import annotations


import copy
import json
import os
import re
import shutil
import textwrap
from datetime import date
from pathlib import Path
from typing import Any

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
    empty_domain_signals,
)
from ai_flywheel.adapters.wiki_gate import (
    CREATE_TARGET_EXISTS,
    DOMAIN_INVENTORY_LIMIT,
    ROUTE_DECLARED,
    ROUTE_EVIDENCE,
    ROUTE_PATH,
    SECRET_LEAK_BLOCKED,
    UNROUTED_DOC_REASON,
    UNROUTED_REASON_LABEL,
    UNROUTED_REASON_TEXT,
    UPDATE_NO_NEW_CONTENT,
    DepositRouting,
    RoutingCandidates,
    WikiGateError,
    WikiSchemaLoader,
    build_domain_page,
    deposit_routing_rules,
    discover_domain_pages,
    is_single_table_name,
    merge_domain_increment,
    preserves_existing,
    resolve_bash_executable,
    split_frontmatter,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core import orchestrator
from ai_flywheel.core.time_utils import shanghai_today_ymd
from ai_flywheel.models import RegisterSessionRequest, RunStatus, is_sensitive_write
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


# ================================================================================
# Project-domain pages: the deposit routing a Wiki declares (content_contract.deposit_routing)
# ================================================================================
#
# A Wiki may declare that what a session VERIFIED about one project is filed into that project
# domain pages (a table structure page, a configuration page, ...). Everything below uses generic
# examples: the project alpha with its `orders` table page and its `database` configuration page.
# The contract is built by `deposit_schema()`; a Wiki that does not declare the block is covered by
# every test above and keeps its behaviour unchanged.


REPO_ROOT = Path(__file__).resolve().parents[1]


PROJECTS = "wiki-知识库/projects-项目"
# (directory name, project_id, aliases) of two synthetic Wiki projects.
DEPOSIT_PROJECT = ("alpha-orders-Alpha订单系统", "alpha", ["Alpha Orders", "订单系统"])
BILLING_PROJECT = ("beta-billing-Beta计费", "beta", ["Beta Billing"])
TABLE_DIR = "数据库结构"
CONFIG_DIR = "项目配置"

_DOMAIN_FIELDS = ["title", "description", "type", "project_id", "knowledge_kind", "load_policy", "default_load", "created", "updated", "status"]
_TEMPLATES = [
    {"id": "T7", "name": "表结构", "page_type": "project-algorithm", "knowledge_kind": "ddl", "load_policy": "index_then_on_demand",
     "body_sections": ["用途一行", "字段语义表", "枚举值", "关联键", "坑与边界"]},
    {"id": "T1", "name": "业务算法", "page_type": "project-algorithm", "knowledge_kind": "algorithm", "load_policy": "index_then_on_demand",
     "body_sections": ["适用场景", "算法步骤", "坑与边界"]},
    {"id": "T2", "name": "业务口径", "page_type": "project-algorithm", "knowledge_kind": "caliber", "load_policy": "index_then_on_demand",
     "body_sections": ["口径定义", "计算规则", "坑与边界"]},
    {"id": "T3", "name": "服务卡", "page_type": "project-architecture", "knowledge_kind": "service", "load_policy": "index_then_on_demand",
     "body_sections": ["职责", "依赖关系", "坑与边界"]},
    {"id": "T4", "name": "部署架构", "page_type": "project-architecture", "knowledge_kind": "deployment", "load_policy": "index_then_on_demand",
     "body_sections": ["部署单元", "依赖关系", "坑与边界"]},
    {"id": "T6", "name": "项目配置", "page_type": "project-config", "load_policy": "explicit_or_task_required",
     "body_sections": ["用途一行", "配置矩阵表", "使用规则", "变更记录表"]},
]
_DOMAINS = [
    {"domain": "数据库结构", "trigger": "会话查证过业务表的字段、枚举、关联键或使用边界", "target": "{project}/数据库结构/", "template": "T7"},
    {"domain": "系统架构", "trigger": "会话查证过服务依赖或部署关系", "target": "{project}/系统架构/", "template": ["T3", "T4"]},
    {"domain": "业务算法", "trigger": "会话查证过业务算法或统计口径", "target": "{project}/业务算法/", "template": ["T1", "T2"]},
    {"domain": "项目配置", "trigger": "会话查证过后续会话必须记住的项目配置事实", "target": "{project}/项目配置/", "template": "T6"},
]


def deposit_schema(mutate=None) -> str:
    """The test Wiki's contract with `content_contract.deposit_routing` (version 1.1) added.

    ``mutate`` receives the content contract before it is serialised, to declare it broken.
    """
    fm_text, _ = split_frontmatter(SCHEMA)
    schema = yaml.safe_load(fm_text)
    content = schema["content_contract"]
    statuses = ["active", "deprecated", "archived"]
    content["page_types"].update(
        {
            "project-algorithm": {
                "partition": "projects", "required_fields": _DOMAIN_FIELDS,
                "path_pattern": r"^[^/]+/(业务算法|数据库结构)/[^/]+\.md$", "allowed_status": statuses,
                "knowledge_kind_values": ["ddl", "algorithm", "caliber"],
            },
            "project-architecture": {
                "partition": "projects", "required_fields": _DOMAIN_FIELDS,
                "path_pattern": r"^[^/]+/系统架构/[^/]+\.md$", "allowed_status": statuses,
            },
            "project-config": {
                "partition": "projects",
                "required_fields": [name for name in _DOMAIN_FIELDS if name != "knowledge_kind"],
                "path_pattern": r"^[^/]+/项目配置/.+\.md$", "allowed_status": statuses,
            },
        }
    )
    content["project_template_contract"] = {"version": "1.0", "templates": copy.deepcopy(_TEMPLATES)}
    content["deposit_routing"] = {
        "version": "1.1",
        "rule": "会话查证过的项目领域知识落到项目目录下：一表一页，缺页新建，已有页增量补充。",
        "domains": copy.deepcopy(_DOMAINS),
    }
    if mutate is not None:
        mutate(content)
    return f"---\n{yaml.safe_dump(schema, allow_unicode=True, sort_keys=False, width=1000)}---\n\n# Test Wiki Schema\n"


def _page(subdir: str, name: str, project: str = DEPOSIT_PROJECT[0]) -> str:
    return f"{PROJECTS}/{project}/{subdir}/{name}.md"


def _write(wiki_root: Path, rel: str, content: str) -> Path:
    target = wiki_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("utf-8"))  # LF, as the Wiki's pages are
    return target


def _register_project(
    wiki_root: Path, dir_name: str, project_id: str, aliases: tuple[str, ...] = (), *, description: str = ""
) -> None:
    """Register a Wiki project; only a directory that hosts 实战笔记/ can be a routing candidate."""
    frontmatter = {
        "title": f"{project_id} 索引",
        "description": description or f"{project_id} 实战经验卡索引",
        "type": "project-practice-index",
        "project_id": project_id,
        "created": "2026-11-20",
        "updated": "2026-11-20",
        "status": "active",
    }
    if aliases:
        frontmatter["aliases"] = list(aliases)
    _write(wiki_root, f"{PROJECTS}/{dir_name}/实战笔记/笔记索引.md",
           f"---\n{yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n\n# {project_id} 索引\n")


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
    sources:
      - ai-flywheel session: codex:old-1
    ---

    # orders

    ## 用途一行

    订单主表，一笔订单一行。

    ## 字段语义表

    | 字段 | 类型 | 含义 | 备注 |
    | --- | --- | --- | --- |
    | id | bigint | 主键 | 自增 |
    | status | tinyint | 订单状态 | 见枚举值 |

    ## 枚举值

    | 字段 | 取值 | 含义 |
    | --- | --- | --- |
    | status | 0 | 待支付 |
    | status | 1 | 已支付 |

    ## 关联键

    无

    ## 坑与边界

    - status 只在线上验证过 0 和 1 两个取值。
    """
)

# What a session verified about `orders`; the `id` row is one the page already holds.
ORDERS_INCREMENT = textwrap.dedent(
    """\
    ## 字段语义表

    | 字段 | 类型 | 含义 | 备注 |
    | --- | --- | --- | --- |
    | paid_at | datetime | 支付时间 | 未支付为 NULL |
    | id | bigint | 主键 | 自增 |

    ## 坑与边界

    - 未支付订单的 paid_at 为 NULL，不能拿它当时间过滤条件。
    """
)

DATABASE_PAGE = textwrap.dedent(
    """\
    ---
    title: database
    description: alpha 的数据源与逻辑库映射
    type: project-config
    project_id: alpha
    knowledge_kind: config
    load_policy: explicit_or_task_required
    default_load: false
    created: 2026-11-20
    updated: 2026-11-20
    status: active
    sources:
      - ai-flywheel session: codex:old-2
    ---

    # database

    ## 用途一行

    记录 alpha 服务连接数据库的方式；凭据本身不入库。

    ## 配置矩阵表

    | 配置项 | 取值 | 说明 |
    | --- | --- | --- |
    | datasource.default | jdbc:mysql://db.example.test:3306/alpha | 主库 |
    | logical_db.orders | shard_0 | 订单库路由 |

    ## 使用规则

    - 连接池上限按实例规格设置，不超过实例最大连接数的一半。

    ## 变更记录表

    | 日期 | 变更 | 说明 |
    | --- | --- | --- |
    | 2026-11-20 | 初始化 | 首次整理 |
    """
)

# A configuration fact worth remembering although nothing was changed.
DATABASE_INCREMENT = textwrap.dedent(
    """\
    ## 配置矩阵表

    | 配置项 | 取值 | 说明 |
    | --- | --- | --- |
    | logical_db.payments | shard_1 | 支付库路由 |

    ## 使用规则

    - 逻辑库 payments 与 orders 不在同一分片，跨库关联只能在应用层做。

    ## 变更记录表

    | 日期 | 变更 | 说明 |
    | --- | --- | --- |
    | 2026-11-26 | 新增 payments 逻辑库映射 | 会话查证 |
    """
)

# The draft of a page that does not exist yet: the template's sections, nothing else.
PAYMENTS_DRAFT = textwrap.dedent(
    """\
    ## 用途一行

    支付流水表：一次支付尝试一行。

    ## 字段语义表

    | 字段 | 类型 | 含义 | 备注 |
    | --- | --- | --- | --- |
    | id | bigint | 主键 | 自增 |
    | order_id | bigint | 订单号 | 逻辑关联 orders.id |
    | amount | int | 金额 | 单位为分 |
    | status | tinyint | 支付状态 | 见枚举值 |

    ## 枚举值

    | 字段 | 取值 | 含义 |
    | --- | --- | --- |
    | status | 0 | 待确认 |
    | status | 1 | 成功 |
    | status | 2 | 失败 |

    ## 关联键

    - order_id → orders.id

    ## 坑与边界

    - amount 以分为单位，展示前除以 100。
    """
)

CARD_TITLE = "订单表字段与连库排查复盘"
CARD_DRAFT = (
    "---\n"
    f"title: {CARD_TITLE}\n"
    "tags: [alpha]\n"
    "---\n\n"
    f"# {CARD_TITLE}\n"
    "- 未支付订单的 paid_at 为 NULL，不能拿它当时间过滤条件。\n"
    "- payments 与 orders 不在同一分片，跨库关联只能在应用层做。\n"
)

# ---------------------------------------------------------------- end to end: the sandbox replay

_STAND_IN_D2 = r'''"""Stand-in for the Wiki's own post-write hook: a sandbox Wiki has none.

It judges what a page must satisfy on disk after a write (frontmatter first, no BOM, LF
only, exactly one H1) and records every call, so a test can see which pages were checked.
A file named d2-verdict.txt holding "blocked" next to this script makes it block.
"""
import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wiki-root", required=True)
    parser.add_argument("--write-set", required=True)
    parser.add_argument("--today")
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--skip-validator", action="store_true")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    problems = []
    verdict = here / "d2-verdict.txt"
    if verdict.is_file() and verdict.read_text(encoding="utf-8").strip() == "blocked":
        problems.append("blocked on request")
    checked = []
    for rel in filter(None, args.write_set.split(",")):
        raw = (Path(args.wiki_root) / rel).read_bytes()
        text = raw.decode("utf-8")
        checked.append(rel)
        if not text.startswith("---"):
            problems.append(rel + ": frontmatter is not first")
        if b"\r\n" in raw:
            problems.append(rel + ": CRLF line ending")
        if b"\xef\xbb\xbf" in raw:
            problems.append(rel + ": BOM")
        if sum(1 for line in text.split("\n") if line.startswith("# ")) != 1:
            problems.append(rel + ": expected exactly one H1")
    report = {
        "status": "pass" if not problems else "blocked",
        "checked": checked,
        "problems": problems,
        "today": args.today,
        "skip_validator": args.skip_validator,
    }
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    with (here / "d2-calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not problems else 3


if __name__ == "__main__":
    sys.exit(main())
'''

SESSION_TITLE = "订单表字段与连库排查"
# A sanitised test session: one new field of an existing table, the key fields of a new
# table, and one connection pitfall of a data source.
SESSION_TEXT = (
    "用户：订单列表按支付时间筛选，结果是空的，帮我看看。\n"
    "助手：查了 orders 表结构，paid_at 是 datetime，未支付订单为 NULL，不能拿它当时间过滤条件。\n"
    "用户：支付流水在哪张表？\n"
    "助手：payments 表，字段 id / order_id / amount / status；amount 以分为单位，"
    "status 0 待确认、1 成功、2 失败。\n"
    "用户：这个库连哪个实例？\n"
    "助手：逻辑库 payments 路由到 shard_1，和 orders 不在同一分片，跨库关联只能在应用层做。\n"
)


def _sandbox(tmp_path: Path, bash: str, *, approval_mode: str = "smart") -> FlywheelRuntime:
    """An isolated Wiki (the deposit routing declared, one table page, one configuration page).

    Everything the runtime writes lands under ``tmp_path``: the Wiki, the data directory,
    the receipts, the gate evidence. No channel and no scanner is enabled, and the model
    is never called: the API runner's transport is replaced by a canned reply.
    """
    wiki_root = tmp_path / "wiki"
    (wiki_root / PROJECTS).mkdir(parents=True)
    _write(wiki_root, ".wiki-schema.md", deposit_schema())
    _register_project(wiki_root, *DEPOSIT_PROJECT)
    _write(wiki_root, _page(TABLE_DIR, "orders"), ORDERS_PAGE)
    _write(wiki_root, _page(CONFIG_DIR, "database"), DATABASE_PAGE)
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    (tmp_path / "stand_in_d2.py").write_text(_STAND_IN_D2, encoding="utf-8", newline="\n")
    hook = tmp_path / "stand-in-d2.sh"
    hook.write_text('#!/usr/bin/env bash\nexec "$PYTHON_BIN" "$STAND_IN_D2" "$@"\n', encoding="utf-8", newline="\n")

    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(wiki_root)
    cfg.knowledge_sink.allowed_write_prefixes = ["wiki-知识库/"]
    cfg.knowledge_sink.wiki_gate_require_scripts = True
    cfg.knowledge_sink.postflight_hook_script = str(hook)
    cfg.knowledge_sink.wiki_bash = bash
    cfg.workflow.approval_mode = approval_mode
    cfg.safety_valves.auto_approve_writes = False
    cfg.delivery.feishu_cli.enabled = False
    cfg.delivery.webhook.enabled = False
    cfg.delivery.git_closure.enabled = False
    cfg.sessions.scan_sources = []
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"  # never called: the model reply is canned
    cfg.runner.api.model = "mock-model"
    rt = FlywheelRuntime(cfg)

    sandbox = tmp_path.resolve()
    assert rt.cfg.data_path.is_relative_to(sandbox)
    assert rt.cfg.receipts_dir.is_relative_to(sandbox)
    assert rt.cfg.wiki_gate_evidence_path.is_relative_to(sandbox)
    assert rt.cfg.primary_skills_dir.resolve().is_relative_to(sandbox)
    assert rt.knowledge.root.resolve().is_relative_to(sandbox)
    return rt


def _register_orders_session(rt: FlywheelRuntime, workspace: Path, session_id: str = "alpha-1") -> str:
    rt.register_session(
        RegisterSessionRequest(
            client="claude-code",
            session_id=session_id,
            title=SESSION_TITLE,
            project="alpha",
            workspace=str(workspace),
            summary="核对 orders 新字段、payments 关键字段与逻辑库映射。",
            content=SESSION_TEXT,
        )
    )
    return f"claude-code:{session_id}"


def _canned_review(session_key: str) -> str:
    """The review a model returns for the test session: its findings carry `domain_signals`."""
    return json.dumps(
        {
            "findings": [
                {
                    "session_key": session_key,
                    "client": "claude-code",
                    "project": "alpha",
                    "title": SESSION_TITLE,
                    "worthy": True,
                    "category": "project",
                    "skip_reason": "",
                    "facts": ["未支付订单的 paid_at 为 NULL", "payments 与 orders 不在同一分片"],
                    "anomalies": [],
                    "tags": ["alpha"],
                    "suggest_rule": False,
                    "skill_route": {"hit": False, "action": "none", "target_skill": "", "reason": "单次排查任务"},
                    "domain_signals": {
                        "database_tables": [
                            {
                                "table": "orders",
                                "verified_fields": ["paid_at datetime 支付时间"],
                                "enums": [],
                                "join_keys": [],
                                "pitfalls": ["未支付订单的 paid_at 为 NULL"],
                            },
                            {
                                "table": "payments",
                                "verified_fields": [
                                    "id bigint 主键",
                                    "order_id bigint 订单号",
                                    "amount int 金额（分）",
                                    "status tinyint 状态",
                                ],
                                "enums": ["status 0 待确认 / 1 成功 / 2 失败"],
                                "join_keys": ["order_id → orders.id"],
                                "pitfalls": ["amount 以分为单位"],
                            },
                        ],
                        "project_configs": [
                            {
                                "topic": "database",
                                "facts": ["逻辑库 payments 路由到 shard_1"],
                                "pitfalls": ["payments 与 orders 跨库关联只能在应用层做"],
                            }
                        ],
                        "architecture_flows": [],
                        "business_calibers": [],
                    },
                }
            ],
            "mistakes": [],
            "dissents": [],
            "repeated_workflows": [],
        },
        ensure_ascii=False,
    )


def _standard_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The plan a model writes for the canned review, with the paths read off the prompt.

    The experience card comes last in the model's order: it must still keep its slot.
    """
    routing = payload["routing_candidates"]
    domains = {domain["domain"]: domain for domain in routing["deposit_routing"]["domains"]}
    project = routing["projects"][0]["project"]
    key = payload["findings"][0]["session_key"]

    def page(action: str, title: str, summary: str, domain_name: str, page: str, preview: str, reason: str) -> dict:
        domain = domains[domain_name]
        return {
            "target_kind": "knowledge_doc",
            "action": action,
            "title": title,
            "summary": summary,
            "relative_path": domain["target_path"].replace("{project}", project).replace("{page}", page),
            "source_session_keys": [key],
            "content_preview": preview,
            "wiki_route": {"page_type": domain["page_types"][0], "project": project, "reason": reason},
        }

    items = [
        page("update", "orders 表结构增量", "补记 orders 已查证的 paid_at 字段与其为 NULL 的边界。",
             "数据库结构", "orders", ORDERS_INCREMENT, "会话查证了 orders 表的新字段"),
        page("create", "payments 支付流水表", "新建 payments 表结构页，只记已查证的字段、枚举与关联键。",
             "数据库结构", "payments", PAYMENTS_DRAFT, "会话首次查证 payments 表"),
        page("update", "database 配置页增量", "补记 payments 逻辑库映射这一后续会话要记住的配置事实。",
             "项目配置", "database", DATABASE_INCREMENT, "会话查证了项目的逻辑库映射"),
        {
            "target_kind": "knowledge_doc",
            "action": "create",
            "title": CARD_TITLE,
            "summary": "沉淀订单表字段与连库边界的排查经验。",
            "relative_path": f"{PROJECTS}/2026-11-27-{CARD_TITLE}.md",
            "source_session_keys": [key],
            "content_preview": CARD_DRAFT,
            "wiki_route": {"page_type": "project-practice-note", "project": project, "reason": "本项目订单表排查的复盘"},
        },
    ]
    return items


class _CannedModel:
    """Stands in for the model gateway behind the real API runner: replies are canned, by prompt.

    ``plan`` is either a list of plan items or a function of the prompt's payload that
    returns one: a canned reply then follows the same contract a model reads.
    """

    def __init__(self, review: str, plan) -> None:
        self.review = review
        self.plan = plan
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        first_line = prompt.split("\n", 1)[0]
        self.prompts.append(prompt)
        if first_line == "You are the P02 Review Worker of AI Flywheel.":
            return self.review
        if first_line == "You are the P03 Route & Write-Plan Worker of AI Flywheel.":
            items = self.plan(_prompt_payload(prompt)) if callable(self.plan) else self.plan
            return json.dumps({"planned_writes": items}, ensure_ascii=False)
        raise AssertionError(f"unexpected prompt: {first_line}")


def _audit(rt: FlywheelRuntime, run, event_type: str) -> dict:
    return next(
        e for e in rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id, limit=100)
        if e["event_type"] == event_type
    )


ORDERS_REL = _page(TABLE_DIR, "orders")
PAYMENTS_REL = _page(TABLE_DIR, "payments")
DATABASE_REL = _page(CONFIG_DIR, "database")


def _replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan, *, approval_mode: str = "smart") -> tuple[FlywheelRuntime, Any, str]:
    """Drive a run over the sandbox Wiki: the real runtime and API runner, a canned model reply."""
    bash = resolve_bash_executable("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("bash is required to run the stand-in postflight hook")
    rt = _sandbox(tmp_path, bash, approval_mode=approval_mode)
    monkeypatch.setenv("STAND_IN_D2", str(tmp_path / "stand_in_d2.py"))
    key = _register_orders_session(rt, tmp_path / "work" / "alpha")
    model = _CannedModel(_canned_review(key), plan)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", model)
    return rt, model, key


def _d2_calls(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "d2-calls.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []


def _titles(run) -> list[str]:
    return [w.title for w in run.planned_writes]


def test_sandbox_replay_merges_updates_creates_a_page_and_holds_the_config_page_for_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rt, _model, key = _replay(tmp_path, monkeypatch, _standard_items)
    root = rt.knowledge.root
    orders_before = (root / ORDERS_REL).read_bytes()
    database_before = (root / DATABASE_REL).read_bytes()
    today = shanghai_today_ymd()

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key])

    # P02 extracted the signals, P03 planned the experience card and the three domain pages,
    # and the configuration page holds the batch for a human.
    assert run.status == RunStatus.AWAITING_APPROVAL, run.error
    assert run.runner_mode == "api:mock-model"
    (finding,) = _node(run, "P02_REVIEW").payload["findings"]
    signals = finding["domain_signals"]
    assert [table["table"] for table in signals["database_tables"]] == ["orders", "payments"]
    assert signals["project_configs"] == [
        {
            "topic": "database",
            "facts": ["逻辑库 payments 路由到 shard_1"],
            "pitfalls": ["payments 与 orders 跨库关联只能在应用层做"],
        }
    ]
    assert (signals["architecture_flows"], signals["business_calibers"]) == ([], [])
    assert [w.title for w in run.planned_writes] == [CARD_TITLE, "orders 表结构增量", "payments 支付流水表", "database 配置页增量"]
    assert [is_sensitive_write(w) for w in run.planned_writes] == [False, False, False, True]
    assert run.planned_writes[3].requires_approval is True
    # Nothing is written before the approval.
    assert (root / ORDERS_REL).read_bytes() == orders_before
    assert (root / DATABASE_REL).read_bytes() == database_before
    assert not (root / PAYMENTS_REL).exists()
    assert _audit(rt, run, "awaiting_approval")["payload"]["reasons"] == ["项目配置页属于敏感写入，需人工确认后才会落盘"]

    run = rt.orchestrator.approve_and_resume(run.run_id)

    assert run.status == RunStatus.PASS, run.error
    p03 = _node(run, "P03_ROUTE_PLAN")
    routes = {doc["relative_path"]: doc["route"] for doc in p03.payload["routed_docs"]}
    assert (
        routes[ORDERS_REL]["domain"],
        routes[ORDERS_REL]["incremental"],
        routes[ORDERS_REL]["sensitive"],
    ) == ("数据库结构", True, False)
    assert routes[ORDERS_REL]["duplicates"] == 1  # the `id` row the page already holds was dropped
    assert (routes[PAYMENTS_REL]["incremental"], routes[PAYMENTS_REL]["domain"]) == (False, "数据库结构")
    assert (routes[DATABASE_REL]["incremental"], routes[DATABASE_REL]["sensitive"]) == (True, True)
    assert "其中 3 项为项目领域页（增量更新 2、新建 1、敏感待审批 1）" in p03.summary

    # The existing table page: every original line in its order, the new field and the pitfall added.
    orders_raw = (root / ORDERS_REL).read_bytes()
    orders_text = orders_raw.decode("utf-8")
    assert b"\r" not in orders_raw and not orders_raw.startswith(b"\xef\xbb\xbf")
    assert preserves_existing(ORDERS_PAGE, orders_text)
    assert orders_text == (
        ORDERS_PAGE.replace("updated: 2026-11-20", f"updated: {today}")
        .replace(
            "| status | tinyint | 订单状态 | 见枚举值 |\n",
            "| status | tinyint | 订单状态 | 见枚举值 |\n| paid_at | datetime | 支付时间 | 未支付为 NULL |\n",
        )
        .replace(
            "- status 只在线上验证过 0 和 1 两个取值。\n",
            "- status 只在线上验证过 0 和 1 两个取值。\n- 未支付订单的 paid_at 为 NULL，不能拿它当时间过滤条件。\n",
        )
    )
    # The new table page exists and satisfies the mounted contract on its own. It is named after
    # the plan item: the draft carried neither a title nor a description of its own.
    payments_text = (root / PAYMENTS_REL).read_bytes().decode("utf-8")
    frontmatter = yaml.safe_load(split_frontmatter(payments_text)[0])
    assert frontmatter["title"] == "payments 支付流水表"
    assert frontmatter["description"] == "新建 payments 表结构页，只记已查证的字段、枚举与关联键。"
    assert "\n# payments 支付流水表\n" in payments_text
    assert frontmatter["type"] == "project-algorithm"
    assert frontmatter["knowledge_kind"] == "ddl"
    assert frontmatter["project_id"] == "alpha"
    assert frontmatter["load_policy"] == "index_then_on_demand"
    assert frontmatter["default_load"] is False
    assert frontmatter["sources"] == [f"ai-flywheel session: {key}"]
    assert rt.knowledge.wiki_gate.validate_document(PAYMENTS_REL, payments_text).ok
    # The configuration page: merged, never rewritten.
    database_text = (root / DATABASE_REL).read_bytes().decode("utf-8")
    assert preserves_existing(DATABASE_PAGE, database_text)
    assert "| logical_db.payments | shard_1 | 支付库路由 |" in database_text

    # P04: atomic write, read-back and the D2 postflight for every page, the merge receipts kept.
    p04 = _node(run, "P04_WRITE_VERIFY")
    receipts = {receipt["relative_path"]: receipt for receipt in p04.payload["write_receipts"]}
    assert sorted(receipts) == sorted([ORDERS_REL, PAYMENTS_REL, DATABASE_REL, run.planned_writes[0].relative_path])
    for receipt in receipts.values():
        assert receipt["verified"] is True
        assert receipt["wiki_gate"]["ok"] is True
        assert receipt["wiki_gate"]["postflight"]["status"] == "pass"
    assert receipts[ORDERS_REL]["merge"] == {
        "incremental": True,
        "existing_lines": len(ORDERS_PAGE.splitlines()),
        "added_lines": 2,
        # The routing already dropped the duplicate row before approval; the sink found none left.
        "duplicates_removed": 0,
        "placeholders_replaced": [],
        "existing_preserved": True,
    }
    assert receipts[DATABASE_REL]["merge"]["added_lines"] == 3
    assert "merge" not in receipts[PAYMENTS_REL]
    assert p04.payload["merged_domain_pages"] == 2
    assert next(c for c in p04.internal_checks if c["name"] == "项目领域页增量无损合并")["detail"] == {
        "merged": 2,
        "no_new_content": 0,
    }
    calls = _d2_calls(tmp_path)
    assert all(call["status"] == "pass" for call in calls)
    assert {ORDERS_REL, PAYMENTS_REL, DATABASE_REL} <= {rel for call in calls for rel in call["checked"]}
    assert rt.db.get_session(key).status.value == "processed"
    # The experience card and its index entry were written first, ahead of the domain pages.
    assert calls[0]["checked"][0] == run.planned_writes[0].relative_path


def test_run_drops_an_increment_the_page_already_holds_and_leaves_the_page_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    duplicate = {
        "target_kind": "knowledge_doc",
        "action": "update",
        "title": "orders 已有字段复核",
        "summary": "复核 orders 已记录的字段。",
        "relative_path": ORDERS_REL,
        "source_session_keys": ["claude-code:alpha-1"],
        # Every row and bullet is one the page already holds.
        "content_preview": (
            "## 字段语义表\n\n"
            "| 字段 | 类型 | 含义 | 备注 |\n| --- | --- | --- | --- |\n"
            "| id | bigint | 主键 | 自增 |\n| status | tinyint | 订单状态 | 见枚举值 |\n\n"
            "## 坑与边界\n\n- status 只在线上验证过 0 和 1 两个取值。\n"
        ),
        "wiki_route": {"page_type": "project-algorithm", "project": "alpha", "reason": "复核本项目 orders 表"},
    }
    rt, _model, key = _replay(
        tmp_path,
        monkeypatch,
        lambda payload: [duplicate, *(_item for _item in _standard_items(payload) if _item["title"] == CARD_TITLE)],
        approval_mode="always",
    )
    orders_before = (rt.knowledge.root / ORDERS_REL).read_bytes()

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key])
    assert run.status == RunStatus.AWAITING_APPROVAL, run.error
    run = rt.orchestrator.approve_and_resume(run.run_id)

    assert run.status == RunStatus.PASS, run.error
    assert _titles(run) == [CARD_TITLE]
    p03 = _node(run, "P03_ROUTE_PLAN")
    (unrouted,) = p03.payload["unrouted_docs"]
    assert (unrouted["title"], unrouted["relative_path"], unrouted["reason_code"]) == (
        "orders 已有字段复核",
        ORDERS_REL,
        "update_no_new_content",
    )
    assert unrouted["reason"].startswith(UPDATE_NO_NEW_CONTENT + "：")
    assert "增量无新增内容 1" in p03.summary
    unrouted_audit = _audit(rt, run, "planned_doc_unrouted")
    assert unrouted_audit["payload"]["unrouted_docs"][0]["reason_code"] == "update_no_new_content"
    assert "增量无新增内容 1" in unrouted_audit["summary"]
    # The page is byte for byte what it was: nothing was written, nothing was bumped.
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() == orders_before
    assert ORDERS_REL not in _node(run, "P04_WRITE_VERIFY").payload["written_paths"]
    assert not any(ORDERS_REL in call["checked"] for call in _d2_calls(tmp_path))


PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    "secret",
    [
        "密码：Hk9-Qp31x",  # the Chinese credential assignment only the strict check reads
        "password: Hk9-Qp31x",  # a credential assignment (masked elsewhere, refused here)
        PRIVATE_KEY_HEADER,  # a credential kind: dropped before it is routed
    ],
)
def test_run_never_writes_a_configuration_increment_that_carries_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret: str
):
    leak = DATABASE_INCREMENT.replace(
        "- 逻辑库 payments 与 orders 不在同一分片，跨库关联只能在应用层做。",
        f"- 逻辑库 payments 与 orders 不在同一分片，跨库关联只能在应用层做。\n- 测试库 {secret}，只在本地使用。",
    )

    def plan(payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = [item for item in _standard_items(payload) if item["title"] != "database 配置页增量"]
        leak_item = dict(_standard_items(payload)[2], content_preview=leak)
        return [*items, leak_item]

    rt, _model, key = _replay(tmp_path, monkeypatch, plan)
    database_before = (rt.knowledge.root / DATABASE_REL).read_bytes()

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key])

    # Nothing sensitive is left in the batch, so `smart` does not pause; the run completes.
    assert run.status == RunStatus.PASS, run.error
    assert "database 配置页增量" not in _titles(run)
    p03 = _node(run, "P03_ROUTE_PLAN")
    if secret == PRIVATE_KEY_HEADER:
        # A credential kind is refused when the plan item is normalised, before routing.
        (dropped,) = _audit(rt, run, "plan_items_dropped")["payload"]["dropped_items"]
        assert dropped["reasons"] == [SECRET_LEAK_BLOCKED]
        assert "database" in dropped["relative_path"]
    else:
        # A password or a key in a configuration increment is blocked by the route gate.
        (blocked,) = [doc for doc in p03.payload["unrouted_docs"] if doc["title"] == "database 配置页增量"]
        assert blocked["reason_code"] == "secret_leak_blocked"
        assert blocked["reason"].startswith(SECRET_LEAK_BLOCKED + "：")
        assert "Hk9-Qp31x" not in json.dumps(p03.payload, ensure_ascii=False)
        assert "密钥门禁阻断 1" in p03.summary
    assert secret not in json.dumps(_node(run, "P04_WRITE_VERIFY").payload, ensure_ascii=False)
    assert (rt.knowledge.root / DATABASE_REL).read_bytes() == database_before


def test_run_keeps_the_experience_cards_slot_when_domain_pages_fill_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rt, _model, key = _replay(tmp_path, monkeypatch, _standard_items)
    # The card and its index entry take two of the three physical paths a batch may write.
    rt.cfg.knowledge_sink.max_write_set = 3

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key])

    # The batch no longer holds the configuration page, so nothing is left to approve.
    assert run.status == RunStatus.PASS, run.error
    assert _titles(run) == [CARD_TITLE, "orders 表结构增量"]
    p03 = _node(run, "P03_ROUTE_PLAN")
    # At the batch cap the experience card kept its slot although the model listed it last:
    # the item left out was a domain page.
    (over_limit,) = _audit(rt, run, "plan_items_dropped")["payload"]["dropped_items"]
    assert (over_limit["title"], over_limit["index"], over_limit["reasons"]) == (
        "database 配置页增量",
        2,
        ["OVER_LIMIT"],
    )
    # At the physical limit the card and its index entry were counted before the domain pages.
    assert [(drop["title"], drop["reason"]) for drop in p03.payload["index_capacity_drops"]] == [
        ("payments 支付流水表", "WRITE_SET_TOTAL_LIMIT")
    ]
    assert "1 项超出单批上限" in p03.summary
    assert "1 篇笔记连同索引超过物理写入上限，未列入计划" in p03.summary
    # The domain page that did fit was merged.
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() != ORDERS_PAGE.encode("utf-8")
    assert not (rt.knowledge.root / PAYMENTS_REL).exists()
    assert len(_d2_calls(tmp_path)) == 2  # the card with its index, then the table page


def test_approving_only_the_routine_items_leaves_the_configuration_page_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rt, _model, key = _replay(tmp_path, monkeypatch, _standard_items)
    database_before = (rt.knowledge.root / DATABASE_REL).read_bytes()

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key])
    assert run.status == RunStatus.AWAITING_APPROVAL, run.error
    routine = [index for index, write in enumerate(run.planned_writes) if not is_sensitive_write(write)]
    assert routine == [0, 1, 2]

    run = rt.orchestrator.approve_and_resume(run.run_id, approved_indices=routine, selected_option="仅同意常规项")

    assert run.status == RunStatus.PASS, run.error
    assert sorted(_node(run, "P04_WRITE_VERIFY").payload["written_paths"]) == sorted(
        [run.planned_writes[0].relative_path, ORDERS_REL, PAYMENTS_REL]
    )
    assert (rt.knowledge.root / DATABASE_REL).read_bytes() == database_before


def test_p04_skips_an_increment_the_page_gained_after_the_plan_was_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def plan(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in _standard_items(payload) if item["title"] in {CARD_TITLE, "orders 表结构增量"}]

    rt, _model, key = _replay(tmp_path, monkeypatch, plan, approval_mode="always")
    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key])
    assert run.status == RunStatus.AWAITING_APPROVAL, run.error
    # Another writer completed the page while the batch waited (a CRLF rewrite into the bargain).
    gained = ORDERS_PAGE.replace(
        "| status | tinyint | 订单状态 | 见枚举值 |\n",
        "| status | tinyint | 订单状态 | 见枚举值 |\n| paid_at | datetime | 支付时间 | 未支付为 NULL |\n",
    ).replace(
        "- status 只在线上验证过 0 和 1 两个取值。\n",
        "- status 只在线上验证过 0 和 1 两个取值。\n- 未支付订单的 paid_at 为 NULL，不能拿它当时间过滤条件。\n",
    )
    (rt.knowledge.root / ORDERS_REL).write_bytes(gained.replace("\n", "\r\n").encode("utf-8"))

    run = rt.orchestrator.approve_and_resume(run.run_id)

    assert run.status == RunStatus.PASS, run.error
    p04 = _node(run, "P04_WRITE_VERIFY")
    (skip,) = [receipt for receipt in p04.payload["write_receipts"] if receipt["relative_path"] == ORDERS_REL]
    assert (skip["status"], skip["reason_code"], skip["skipped"], skip["written"]) == (
        UPDATE_NO_NEW_CONTENT,
        "update_no_new_content",
        True,
        False,
    )
    assert skip["reason"].startswith(UPDATE_NO_NEW_CONTENT + "：")
    assert ORDERS_REL not in p04.payload["written_paths"]
    assert "1 篇领域页增量已存在于页面，未重复写入" in p04.summary
    assert "1 篇笔记因无法归属项目跳过" not in p04.summary
    assert next(c for c in p04.internal_checks if c["name"] == "项目领域页增量无损合并")["detail"] == {
        "merged": 0,
        "no_new_content": 1,
    }
    # Not a byte of the page was touched, CRLF included.
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() == gained.replace("\n", "\r\n").encode("utf-8")


def test_p04_rolls_the_domain_page_back_and_fails_the_run_when_the_postflight_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def plan(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in _standard_items(payload) if item["title"] == "orders 表结构增量"]

    rt, _model, key = _replay(tmp_path, monkeypatch, plan, approval_mode="auto")
    orders_before = (rt.knowledge.root / ORDERS_REL).read_bytes()
    (tmp_path / "d2-verdict.txt").write_text("blocked", encoding="utf-8")

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key])

    assert run.status == RunStatus.FAILED, run.error
    p04 = _node(run, "P04_WRITE_VERIFY")
    assert "D2_BLOCKED" in (p04.error or "")
    assert p04.payload["partial_write_error"].startswith("Wiki gate blocked write")
    # The page is byte for byte its preimage, and the downstream stages were skipped.
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() == orders_before
    assert [_node(run, node_id).status.value for node_id in ("P05_SETTLE", "P06_REPORT", "P07_DELIVER")] == ["SKIPPED"] * 3
    assert rt.db.get_session(key).status.value == "pending"


def test_the_configuration_page_waits_for_a_human_unless_the_maintainer_runs_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rt, _model, key = _replay(tmp_path, monkeypatch, _standard_items, approval_mode="auto")
    rt.cfg.safety_valves.auto_approve_writes = True  # `auto` only takes effect with the valve open

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key])

    # `auto` is the maintainer's explicit choice (and needs the safety valve): nothing pauses.
    assert run.status == RunStatus.PASS, run.error
    assert "awaiting_approval" not in [e["event_type"] for e in rt.db.list_audit_events(
        entity_type="workflow_run", entity_id=run.run_id, limit=100
    )]
    assert "| logical_db.payments | shard_1 | 支付库路由 |" in (rt.knowledge.root / DATABASE_REL).read_text(encoding="utf-8")


# ---------------- the deposit routing: parsed, discovered, offered ----------------


def _deposit_runtime(tmp_path: Path, *, schema: str | None = None, pages: bool = True) -> FlywheelRuntime:
    """A mounted Wiki with the project alpha: the deposit routing declared, or the plain contract."""
    wiki_root = tmp_path / "wiki"
    (wiki_root / "wiki-知识库" / "topics-主题").mkdir(parents=True)
    _write(wiki_root, ".wiki-schema.md", schema or deposit_schema())
    _register_project(wiki_root, *DEPOSIT_PROJECT)
    if pages and schema is None:
        _write(wiki_root, ORDERS_REL, ORDERS_PAGE)
        _write(wiki_root, DATABASE_REL, DATABASE_PAGE)
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(wiki_root)
    cfg.knowledge_sink.allowed_write_prefixes = ["wiki-知识库"]
    cfg.knowledge_sink.wiki_gate_require_scripts = False
    cfg.sessions.scan_sources = []
    return FlywheelRuntime(cfg)


def _domain_route(rt: FlywheelRuntime, relative_path: str, content: str, **overrides) -> Any:
    """The route verdict on a plan item that declares a project-domain page."""
    params: dict[str, Any] = {
        "relative_path": relative_path,
        "title": "领域页",
        "summary": "",
        "content": content,
        "wiki_route": {"page_type": "project-algorithm", "project": "alpha", "reason": "会话查证了本项目自己的表"},
        "source_workspaces": [],
        "project_hint": "",
        "allow_evidence_fallback": False,
        "candidates": None,
        "action": "create",
        "source_sessions": [],
    }
    params.update(overrides)
    return rt.knowledge.wiki_gate.evaluate_route(**params)


def _schema_of(text: str) -> dict[str, Any]:
    return yaml.safe_load(split_frontmatter(text)[0])


def test_deposit_routing_is_read_off_the_contract():
    rules = deposit_routing_rules(_schema_of(deposit_schema()))

    assert [rule["domain"] for rule in rules] == ["数据库结构", "系统架构", "业务算法", "项目配置"]
    tables, architecture, algorithms, config = rules
    assert (tables["subdir"], tables["target"], tables["trigger"]) == (
        "数据库结构",
        "{project}/数据库结构/",
        "会话查证过业务表的字段、枚举、关联键或使用边界",
    )
    assert [(t["id"], t["page_type"], t["knowledge_kind"], t["load_policy"]) for t in tables["templates"]] == [
        ("T7", "project-algorithm", "ddl", "index_then_on_demand")
    ]
    assert (tables["page_types"], tables["knowledge_kinds"], tables["load_policies"]) == (
        ["project-algorithm"],
        ["ddl"],
        ["index_then_on_demand"],
    )
    assert tables["templates"][0]["body_sections"] == ["用途一行", "字段语义表", "枚举值", "关联键", "坑与边界"]
    assert (architecture["subdir"], architecture["page_types"], architecture["knowledge_kinds"]) == (
        "系统架构",
        ["project-architecture"],
        ["service", "deployment"],
    )
    assert [t["id"] for t in architecture["templates"]] == ["T3", "T4"]
    assert (algorithms["page_types"], [t["knowledge_kind"] for t in algorithms["templates"]]) == (
        ["project-algorithm"],
        ["algorithm", "caliber"],
    )
    # The project's configuration is the one sensitive domain: its template loads only on request.
    assert (config["page_types"], config["sensitive"], config["load_policies"]) == (
        ["project-config"],
        True,
        ["explicit_or_task_required"],
    )
    assert (tables["sensitive"], architecture["sensitive"], algorithms["sensitive"]) == (False, False, False)


@pytest.mark.parametrize("block", [None, {}, {"domains": None}, {"version": "1.1"}])
def test_a_contract_that_declares_no_deposit_routing_declares_no_rules(block):
    schema = _schema_of(SCHEMA)
    if block is not None:
        schema["content_contract"]["deposit_routing"] = block

    assert deposit_routing_rules(schema) == []
    assert DepositRouting.from_schema(schema) is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda content: content.update(deposit_routing=["数据库结构"]), "is not a mapping"),
        (lambda content: content["deposit_routing"].update(version="2.0"), "declares version '2.0'; this gate reads version 1.x"),
        (lambda content: content["deposit_routing"].update(domains={"数据库结构": "T7"}), "domains is not a list"),
        (lambda content: content["deposit_routing"].update(domains=["数据库结构"]), "domains entries must be mappings"),
        (lambda content: content["deposit_routing"]["domains"][0].pop("domain"), "has a domain without a name"),
        (
            lambda content: content["deposit_routing"]["domains"].append(dict(content["deposit_routing"]["domains"][0])),
            "declares domain '数据库结构' twice",
        ),
        (
            lambda content: content["deposit_routing"]["domains"][0].update(target="数据库结构/"),
            "has an unusable target 数据库结构/: expected {project}/<directory>/",
        ),
        (
            lambda content: content["deposit_routing"]["domains"][0].update(template="T9"),
            "names template 'T9', which the template contract does not declare",
        ),
        (lambda content: content["deposit_routing"]["domains"][0].pop("template"), "domain '数据库结构' names no template"),
        (
            lambda content: content["page_types"].pop("project-architecture"),
            "resolves template 'T3' to page type 'project-architecture', which the schema does not declare",
        ),
        (
            lambda content: content["page_types"]["project-config"].update(partition="nowhere"),
            "routes page type 'project-config' into a partition the schema does not resolve",
        ),
    ],
)
def test_a_deposit_routing_that_cannot_be_honoured_is_a_broken_contract(tmp_path: Path, mutate, message):
    """Half a declaration would quietly swallow every plan item meant for it: it is refused instead."""
    text = deposit_schema(mutate)

    with pytest.raises(WikiGateError, match=re.escape(message)):
        deposit_routing_rules(_schema_of(text))
    # The contract reader refuses it too, so nothing downstream reads "nothing declared".
    loader_root = tmp_path / "loader"
    loader_root.mkdir()
    (loader_root / ".wiki-schema.md").write_text(text, encoding="utf-8")
    with pytest.raises(WikiGateError, match="deposit_routing"):
        WikiSchemaLoader(loader_root).load()
    rt = _deposit_runtime(tmp_path)
    _later_revision(rt.knowledge.root / ".wiki-schema.md", text)
    gate = rt.knowledge.wiki_gate
    with pytest.raises(WikiGateError, match="deposit_routing"):
        gate.routing_candidates()
    # The write-time re-check leaves the document untouched, so the write gate reports the contract.
    assert _domain_route(rt, PAYMENTS_REL, PAYMENTS_DRAFT).passthrough is True
    assert gate.normalize_planned_document(
        PAYMENTS_REL, "领域页", "", PAYMENTS_DRAFT, wiki_route={"page_type": "project-algorithm", "project": "alpha"}
    ) == (PAYMENTS_REL, PAYMENTS_DRAFT)


def _later_revision(path: Path, text: str) -> None:
    """Rewrite a file so the loader's revision cache sees another revision."""
    path.write_text(text, encoding="utf-8")
    stamp = os.stat(path).st_mtime + 10
    os.utime(path, (stamp, stamp))


def test_run_fails_at_p03_when_the_deposit_routing_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt, _model, key = _replay(tmp_path, monkeypatch, _standard_items)
    broken = deposit_schema(lambda content: content["deposit_routing"]["domains"][0].update(template="T9"))
    _later_revision(rt.knowledge.root / ".wiki-schema.md", broken)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key])

    assert run.status == RunStatus.FAILED
    p03 = _node(run, "P03_ROUTE_PLAN")
    assert p03.status.value == "FAILED" and "deposit_routing" in (p03.error or "")
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() == ORDERS_PAGE.encode("utf-8")
    assert rt.db.get_session(key).status.value == "pending"


def test_routing_candidates_offer_the_domain_pages_and_each_projects_inventory(tmp_path: Path):
    rt = _deposit_runtime(tmp_path, pages=False)
    root = rt.knowledge.root
    _register_project(root, *BILLING_PROJECT)
    _write(root, ORDERS_REL, ORDERS_PAGE)
    _write(root, DATABASE_REL, DATABASE_PAGE)
    _write(root, _page(TABLE_DIR, "audit_log"), ORDERS_PAGE)
    _write(root, _page(TABLE_DIR, "Zebra"), ORDERS_PAGE)
    # Not pages of the domain: hidden, archived, nested, or not Markdown.
    _write(root, _page(TABLE_DIR, ".orders-draft"), ORDERS_PAGE)
    _write(root, _page(TABLE_DIR, "archive-refunds"), ORDERS_PAGE)
    _write(root, _page(f"{TABLE_DIR}/notes", "readme"), ORDERS_PAGE)
    (root / _page(TABLE_DIR, "refunds")).parent.mkdir(parents=True, exist_ok=True)
    (root / _page(TABLE_DIR, "refunds")).with_suffix(".txt").write_text("not a page", encoding="utf-8")

    candidates = rt.knowledge.wiki_gate.routing_candidates()

    # The card type stays first and the evidence fallback is offered the card type alone:
    # a domain page is reached by declaration only.
    assert candidates.page_types == ["project-practice-note", "project-algorithm", "project-architecture", "project-config"]
    assert (candidates.evidence_page_types, candidates.fallback_page_types) == (
        ["project-practice-note"],
        ["project-practice-note"],
    )
    assert candidates.routable is True
    deposit = candidates.deposit_routing
    assert deposit is not None
    assert (deposit.version, deposit.projects_root) == ("1.1", PROJECTS)
    assert deposit.page_path("数据库结构", DEPOSIT_PROJECT[0], "payments") == PAYMENTS_REL
    empty = {"数据库结构": [], "系统架构": [], "业务算法": [], "项目配置": []}
    alpha, beta = candidates.projects
    assert alpha["domain_inventory"] == {
        "数据库结构": ["audit_log", "orders", "Zebra"],
        "系统架构": [],
        "业务算法": [],
        "项目配置": ["database"],
    }
    assert beta["domain_inventory"] == empty
    payload = candidates.to_dict()
    assert [project["project"] for project in payload["projects"]] == [DEPOSIT_PROJECT[0], BILLING_PROJECT[0]]
    assert [project["domain_inventory"] for project in payload["projects"]] == [
        alpha["domain_inventory"],
        empty,
    ]
    assert "domain_page_sections" not in payload["projects"][0]
    assert payload["deposit_routing"] == deposit.to_dict()
    # The inventory is read-only and bounded: it never grows past what a prompt can carry.
    assert discover_domain_pages((root / _page(TABLE_DIR, "orders")).parent, limit=2) == ["audit_log", "orders"]
    assert DOMAIN_INVENTORY_LIMIT == 100


def test_candidates_of_a_wiki_without_deposit_routing_keep_their_shape(tmp_path: Path):
    rt = _deposit_runtime(tmp_path, schema=SCHEMA)
    gate = rt.knowledge.wiki_gate
    # Pages that happen to sit in domain-looking directories of a Wiki that declares no routing.
    _write(rt.knowledge.root, ORDERS_REL, ORDERS_PAGE)
    _write(rt.knowledge.root, DATABASE_REL, DATABASE_PAGE)

    candidates = gate.routing_candidates()

    assert candidates.deposit_routing is None
    assert candidates.to_dict() == {
        "page_types": ["project-practice-note"],
        "projects": [
            {
                "project": DEPOSIT_PROJECT[0],
                "project_id": DEPOSIT_PROJECT[1],
                "aliases": list(DEPOSIT_PROJECT[2]),
                "description": "alpha 实战经验卡索引",
            }
        ],
    }
    assert gate.domain_page_template(ORDERS_REL, ORDERS_PAGE) is None
    assert gate.is_sensitive_page(DATABASE_REL, DATABASE_INCREMENT) is False
    # A plan may not declare a page type the Wiki does not offer.
    decision = _domain_route(rt, PAYMENTS_REL, PAYMENTS_DRAFT)
    assert (decision.routed, decision.reason_code, decision.domain) == (False, "declaration_invalid", "")
    assert "domain" not in decision.to_dict()


def test_deposit_routing_serialises_what_a_planner_needs_to_name_a_page():
    deposit = DepositRouting.from_schema(_schema_of(deposit_schema()))
    assert deposit is not None
    deposit.projects_root = PROJECTS

    payload = deposit.to_dict()

    assert list(payload) == ["version", "rule", "projects_root", "domains"]
    assert (payload["version"], payload["projects_root"]) == ("1.1", PROJECTS)
    assert payload["rule"] == "会话查证过的项目领域知识落到项目目录下：一表一页，缺页新建，已有页增量补充。"
    tables, architecture, algorithms, config = payload["domains"]
    assert list(tables) == ["domain", "trigger", "target", "target_path", "page_types", "required_fields", "templates", "sensitive"]
    assert tables["target_path"] == f"{PROJECTS}/{{project}}/数据库结构/{{page}}.md"
    assert tables["templates"] == [
        {
            "id": "T7",
            "name": "表结构",
            "page_type": "project-algorithm",
            "knowledge_kind": "ddl",
            "load_policy": "index_then_on_demand",
            "body_sections": ["用途一行", "字段语义表", "枚举值", "关联键", "坑与边界"],
        }
    ]
    # The core fields first, then the page type's own; a field both name is listed once.
    assert tables["required_fields"]["project-algorithm"] == ["title", "description", "type", "created", "updated"] + [
        name for name in _DOMAIN_FIELDS if name not in {"title", "description", "type", "created", "updated"}
    ]
    assert [template["id"] for template in architecture["templates"]] == ["T3", "T4"]
    assert [template["id"] for template in algorithms["templates"]] == ["T1", "T2"]
    assert (config["target_path"], config["sensitive"], tables["sensitive"]) == (
        f"{PROJECTS}/{{project}}/项目配置/{{page}}.md",
        True,
        False,
    )
    assert [deposit.page_types, deposit.subdirs, deposit.sensitive_subdirs()] == [
        ["project-algorithm", "project-architecture", "project-config"],
        ["数据库结构", "系统架构", "业务算法", "项目配置"],
        ["项目配置"],
    ]
    assert [domain["domain"] for domain in deposit.domains_for("project-algorithm")] == ["数据库结构", "业务算法"]


# ---------------- the route verdict on a project-domain page ----------------

T7_SECTIONS = ["用途一行", "字段语义表", "枚举值", "关联键", "坑与边界"]
TABLE_ROUTE = {"page_type": "project-algorithm", "project": "alpha", "reason": "会话查证了本项目自己的表"}
CONFIG_ROUTE = {"page_type": "project-config", "project": "alpha", "reason": "会话查证了本项目的连库配置"}
# What the `orders` page already says, section by section: none of it is new.
ORDERS_REPEAT = (
    "## 字段语义表\n\n| 字段 | 类型 | 含义 | 备注 |\n| --- | --- | --- | --- |\n| id | bigint | 主键 | 自增 |\n\n"
    "## 关联键\n\n无\n\n## 坑与边界\n\n- status 只在线上验证过 0 和 1 两个取值。\n"
)


def test_a_table_page_update_is_merged_into_the_existing_page_and_passes_the_gate(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate
    today = shanghai_today_ymd()

    decision = _domain_route(rt, ORDERS_REL, ORDERS_INCREMENT, action="update")

    assert (decision.routed, decision.source, decision.page_type, decision.project_dir) == (
        True,
        ROUTE_DECLARED,
        "project-algorithm",
        DEPOSIT_PROJECT[0],
    )
    assert (decision.domain, decision.incremental, decision.sensitive, decision.relabel) == ("数据库结构", True, False, "")
    # The `id` row the page already holds was dropped before anything was written.
    assert decision.duplicates == 1
    assert decision.to_dict()["duplicates"] == 1
    # What the approver is shown, and what P04 merges, is only what the page gains.
    assert decision.increment == (
        "## 字段语义表\n\n| paid_at | datetime | 支付时间 | 未支付为 NULL |\n\n"
        "## 坑与边界\n\n- 未支付订单的 paid_at 为 NULL，不能拿它当时间过滤条件。"
    )
    assert gate.normalize_planned_document(
        ORDERS_REL, "orders 表结构增量", "补记已查证字段。", ORDERS_INCREMENT, route=decision, action="update"
    ) == (ORDERS_REL, decision.increment)
    # Handing the gate the declaration instead of a verdict lands on the same increment.
    assert gate.normalize_planned_document(
        ORDERS_REL, "orders 表结构增量", "补记已查证字段。", ORDERS_INCREMENT, wiki_route=TABLE_ROUTE, action="update"
    ) == (ORDERS_REL, decision.increment)
    # The merged page keeps every existing line in its order, gains the new ones, and passes
    # the pre-write gate. A verdict writes nothing: the page on disk is as it was.
    merged = merge_domain_increment(
        ORDERS_PAGE, decision.increment, template_sections=T7_SECTIONS, today=today
    ).merged
    assert preserves_existing(ORDERS_PAGE, merged)
    kept = [line for line in ORDERS_PAGE.splitlines() if line.strip() and line != "updated: 2026-11-20"]
    assert [line for line in merged.splitlines() if line in kept] == kept
    assert f"updated: {today}" in merged.splitlines()
    assert "| paid_at | datetime | 支付时间 | 未支付为 NULL |" in merged.splitlines()
    assert gate.validate_document(ORDERS_REL, merged).ok
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() == ORDERS_PAGE.encode("utf-8")
    # A card's verdict carries none of the domain fields.
    card_rel = f"{PROJECTS}/{DEPOSIT_PROJECT[0]}/实战笔记/2026-11-27-orders.md"
    card = _domain_route(rt, card_rel, "草稿", wiki_route={**TABLE_ROUTE, "page_type": "project-practice-note"})
    assert (card.routed, card.source) == (True, ROUTE_DECLARED)
    assert "domain" not in card.to_dict()


def test_an_update_whose_content_the_page_already_holds_is_dropped_with_a_trace(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate

    decision = _domain_route(rt, ORDERS_REL, ORDERS_REPEAT, action="update")

    # Three items, all of them on the page already: one table row, the placeholder line, one bullet.
    assert decision.routed is False and decision.reason_code == "update_no_new_content"
    assert decision.detail == f"{UPDATE_NO_NEW_CONTENT}：增量内容均已存在于目标页，没有新增实质内容（orders，3 条均已存在）"
    assert UNROUTED_REASON_LABEL["update_no_new_content"] == "增量无新增内容"
    assert (decision.incremental, decision.increment) == (False, "")
    assert gate.normalize_planned_document(ORDERS_REL, "领域页", "", ORDERS_REPEAT, route=decision) is None
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() == ORDERS_PAGE.encode("utf-8")


def test_an_update_needs_its_page_and_a_create_never_overwrites_one(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate

    missing = _domain_route(rt, PAYMENTS_REL, ORDERS_INCREMENT, action="update")
    assert (missing.routed, missing.reason_code) == (False, "update_target_missing")
    assert PAYMENTS_REL in missing.detail
    assert UNROUTED_REASON_LABEL["update_target_missing"] == "更新目标不存在"

    # A create aimed at the existing page becomes an update of it and says so: the page is never
    # overwritten, and the merge shows what it adds.
    create = _domain_route(rt, ORDERS_REL, ORDERS_INCREMENT, action="create")
    update = _domain_route(rt, ORDERS_REL, ORDERS_INCREMENT, action="update")
    assert (create.routed, create.incremental, create.relabel) == (True, True, CREATE_TARGET_EXISTS)
    assert (create.increment, create.duplicates, create.to_dict()["relabel"]) == (
        update.increment,
        update.duplicates,
        CREATE_TARGET_EXISTS,
    )
    assert gate.normalize_planned_document(
        ORDERS_REL, "领域页", "", ORDERS_INCREMENT, route=create, action="create"
    ) == (ORDERS_REL, update.increment)
    # A create with nothing to add to the page that exists is dropped like the update.
    repeated = _domain_route(rt, ORDERS_REL, ORDERS_REPEAT, action="create")
    assert (repeated.routed, repeated.reason_code) == (False, "update_no_new_content")

    # The page an update names must be of the page type the declaration names.
    _write(rt.knowledge.root, _page(TABLE_DIR, "readme"), DATABASE_PAGE)
    wrong = _domain_route(rt, _page(TABLE_DIR, "readme"), ORDERS_INCREMENT, action="update")
    assert (wrong.routed, wrong.reason_code) == (False, "update_target_type_mismatch")
    assert "project-config" in wrong.detail and "project-algorithm" in wrong.detail
    # Nor is an increment merged into a target that fails the Wiki's own gate.
    _write(
        rt.knowledge.root,
        _page(TABLE_DIR, "audit_log"),
        ORDERS_PAGE.replace("load_policy: index_then_on_demand", "load_policy: always"),
    )
    broken = _domain_route(rt, _page(TABLE_DIR, "audit_log"), ORDERS_INCREMENT, action="update")
    assert (broken.routed, broken.reason_code) == (False, "update_target_invalid")
    assert "DOMAIN_LOAD_POLICY" in broken.detail


def test_an_increment_that_cannot_enter_the_page_losslessly_is_refused(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    # A section neither on the page nor in its template, a row that does not fit its table,
    # a table header of its own, text outside any section, a section named twice, and nothing at all.
    refused = [
        ("## 索引\n\n- id 为主键。", "不在目标页中"),
        ("## 字段语义表\n\n| 1 |\n", "增量行有 1 列，该小节的表头有 4 列"),
        ("## 字段语义表\n\n| a | b |\n| --- | --- |\n| 1 |", "与目标页表头"),
        ("补一句放在小节外面的话。\n\n## 坑与边界\n\n- 新坑", "小节之前带有正文"),
        ("## 坑与边界\n\n- 甲\n\n## 坑与边界\n\n- 乙", "出现了多次"),
        ("", "增量为空"),
    ]
    for increment, note in refused:
        decision = _domain_route(rt, ORDERS_REL, increment, action="update")

        assert (decision.routed, decision.reason_code) == (False, "increment_invalid"), increment
        assert note in decision.detail, increment
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() == ORDERS_PAGE.encode("utf-8")


def test_a_new_table_page_is_built_from_the_template_and_passes_the_gate(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate
    today = shanghai_today_ymd()
    title = "payments 支付流水表"
    summary = "新建 payments 表结构页，只记已查证的字段。"

    decision = _domain_route(rt, PAYMENTS_REL, PAYMENTS_DRAFT, title=title, summary=summary, source_sessions=["codex:s-9"])

    assert (decision.routed, decision.incremental, decision.page_type) == (True, False, "project-algorithm")
    assert (decision.domain, decision.relabel, decision.sensitive) == ("数据库结构", "", False)
    assert gate.normalize_planned_document(PAYMENTS_REL, title, summary, PAYMENTS_DRAFT, route=decision) == (
        PAYMENTS_REL,
        decision.page,
    )
    page = decision.page
    assert gate.validate_document(PAYMENTS_REL, page).ok
    assert not (rt.knowledge.root / PAYMENTS_REL).exists()  # a verdict writes nothing
    # The engine's frontmatter: the draft carried none, and no value of the draft's is invented.
    frontmatter, body = split_frontmatter(page)
    assert yaml.safe_load(frontmatter) == {
        "title": title,
        "type": "project-algorithm",
        "project_id": "alpha",
        "knowledge_kind": "ddl",
        "load_policy": "index_then_on_demand",
        "default_load": False,
        "created": today,
        "updated": today,
        "description": summary,
        "status": "active",
        "sources": ["ai-flywheel session: codex:s-9"],
    }
    assert "\r" not in page and not page.startswith("\ufeff")
    # The draft's sections, in the template's order, each holding what the draft supplied.
    assert [line for line in body.splitlines() if line.startswith("#")] == [
        f"# {title}",
        "## 用途一行",
        "## 字段语义表",
        "## 枚举值",
        "## 关联键",
        "## 坑与边界",
    ]
    for line in ("支付流水表：一次支付尝试一行。", "| amount | int | 金额 | 单位为分 |", "| status | 2 | 失败 |", "- order_id → orders.id"):
        assert line in body.splitlines(), line
    assert "| 字段 | 类型 | 含义 | 备注 |" in body.splitlines()
    # A draft may state the page's own title. A plan item without a summary still gets the
    # description the contract requires, from the page title; with neither, nothing is invented.
    stated = build_domain_page(
        title="payments", summary="",
        content="---\ntitle: payments 支付流水表\n---\n\n" + PAYMENTS_DRAFT,
        page_type="project-algorithm", project_id="alpha", template=_TEMPLATES[0], today=today,
    )
    assert stated[1] == [] and stated[0].startswith("---\ntitle: payments 支付流水表\n")
    bare = build_domain_page(
        title="payments", summary="", content=PAYMENTS_DRAFT,
        page_type="project-algorithm", project_id="alpha", template=_TEMPLATES[0], today=today,
    )
    assert bare[1] == [] and "\ndescription: payments\n" in bare[0] and "\n# payments\n" in bare[0]
    unnamed = build_domain_page(
        title="", summary="", content=PAYMENTS_DRAFT,
        page_type="project-algorithm", project_id="alpha", template=_TEMPLATES[0], today=today,
    )
    assert (unnamed[1] == []) and "\ntitle: ''\n" in unnamed[0] and "\ndescription: ''\n" in unnamed[0]
    # The template's shape is the engine's to set; a draft's own values for it are not used.
    shaped = build_domain_page(
        title="payments", summary="",
        content="---\ntype: project-config\nknowledge_kind: algorithm\nload_policy: always\ncreated: 2000-01-01\nproject_id: beta\n---\n\n" + PAYMENTS_DRAFT,
        page_type="project-algorithm", project_id="alpha", template=_TEMPLATES[0], today=today, source_sessions=["codex:s-1", "codex:s-2"],
    )
    front = yaml.safe_load(split_frontmatter(shaped[0])[0])
    assert {key: front[key] for key in ("type", "knowledge_kind", "load_policy", "default_load", "created", "updated", "project_id", "sources")} == {
        "type": "project-algorithm", "knowledge_kind": "ddl", "load_policy": "index_then_on_demand",
        "default_load": False, "created": today, "updated": today, "project_id": "alpha",
        "sources": ["ai-flywheel session: codex:s-1", "ai-flywheel session: codex:s-2"],
    }
    # Two level-1 titles, a draft section outside the template, and an unusable frontmatter.
    for content, note in (
        ("# 甲\n\n" + PAYMENTS_DRAFT + "\n# 另一个标题\n", "多个一级标题"),
        (PAYMENTS_DRAFT + "\n## 性能笔记\n\n- 慢查询要带索引。\n", "不是模板 T7 声明的小节"),
        ("---\ntitle: [unclosed\n---\n\n" + PAYMENTS_DRAFT, "不是合法 YAML"),
        ("---\n- just\n- a list\n---\n\n" + PAYMENTS_DRAFT, "不是映射"),
    ):
        page, problems = build_domain_page(
            title="payments", summary="", content=content,
            page_type="project-algorithm", project_id="alpha", template=_TEMPLATES[0], today=today,
        )
        assert (page, len(problems)) == ("", 1) and note in problems[0], content


def test_a_section_the_draft_leaves_out_gets_the_placeholder_never_an_invented_fact(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    partial = PAYMENTS_DRAFT.split("## 枚举值")[0] + PAYMENTS_DRAFT[PAYMENTS_DRAFT.index("## 坑与边界") :]

    decision = _domain_route(rt, PAYMENTS_REL, partial, title="payments")

    assert decision.routed is True
    assert "## 枚举值\n\n无\n" in decision.page
    assert "## 关联键\n\n无\n" in decision.page
    # Nothing of the two sections the draft left out was carried over from anywhere.
    assert "待确认" not in decision.page and "→ orders.id" not in decision.page
    assert "支付流水表：一次支付尝试一行。" in decision.page and "- amount 以分为单位，展示前除以 100。" in decision.page
    assert rt.knowledge.wiki_gate.validate_document(PAYMENTS_REL, decision.page).ok
    # A draft section the template does not declare is refused, not filed somewhere else.
    stray = _domain_route(rt, PAYMENTS_REL, PAYMENTS_DRAFT + "\n## 性能笔记\n\n- 慢查询要带索引。\n", title="payments")
    assert (stray.routed, stray.reason_code) == (False, "domain_page_invalid")
    assert "性能笔记" in stray.detail


def test_a_create_draft_that_already_satisfies_the_contract_keeps_its_text(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate
    built = _domain_route(rt, _page(TABLE_DIR, "refunds"), PAYMENTS_DRAFT, title="refunds").page
    assert gate.validate_document(PAYMENTS_REL, built).ok

    decision = _domain_route(rt, PAYMENTS_REL, built, title="payments")

    assert (decision.routed, decision.page, decision.incremental) == (True, built, False)
    # A draft that states another page type than the declaration is refused, never corrected:
    # the declaration does not pick up the draft's type, whichever way round the two differ.
    for draft, declared, path in (
        (built, CONFIG_ROUTE, _page(CONFIG_DIR, "payments")),
        ("---\ntype: project-config\n---\n\n" + PAYMENTS_DRAFT, TABLE_ROUTE, PAYMENTS_REL),
        ("---\ntype: project-architecture\n---\n\n" + PAYMENTS_DRAFT, TABLE_ROUTE, PAYMENTS_REL),
    ):
        other = _domain_route(rt, path, draft, wiki_route=declared, title="payments")
        assert (other.routed, other.reason_code) == (False, "declaration_invalid"), (declared, path)
        assert declared["page_type"] in other.detail and "不一致" in other.detail
    # The draft's own type agreeing with the declaration, the page is built around its sections.
    agreeing = _domain_route(rt, _page(CONFIG_DIR, "payments"), "---\ntype: project-config\n---\n\n## 用途一行\n\n支付服务的配置。\n", wiki_route=CONFIG_ROUTE, title="payments")
    assert agreeing.routed is True and "## 配置矩阵表\n\n无\n" in agreeing.page
    # A draft in a language of its own (a knowledge kind the domain does not hold) is refused too.
    kind = _domain_route(rt, PAYMENTS_REL, "---\nknowledge_kind: algorithm\n---\n\n" + PAYMENTS_DRAFT, title="payments")
    assert (kind.routed, kind.reason_code) == (False, "declaration_invalid")
    assert "algorithm" in kind.detail


def test_a_domain_with_several_templates_needs_the_knowledge_kind(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate
    service_rel = _page("系统架构", "order-service")
    draft = "## 职责\n\n订单服务：接单与状态流转。\n\n## 依赖关系\n\n- 依赖 payments 服务。\n"
    declared = {"page_type": "project-architecture", "project": "alpha", "reason": "会话查证了服务依赖"}

    ambiguous = _domain_route(rt, service_rel, draft, wiki_route=declared, title="order-service")
    assert (ambiguous.routed, ambiguous.reason_code) == (False, "declaration_invalid")
    assert "有多个模板" in ambiguous.detail and "knowledge_kind" in ambiguous.detail

    chosen = _domain_route(rt, service_rel, "---\nknowledge_kind: service\n---\n\n" + draft, wiki_route=declared, title="order-service")
    assert (chosen.routed, chosen.domain, chosen.page_type) == (True, "系统架构", "project-architecture")
    assert [line for line in chosen.page.splitlines() if line.startswith("## ")] == ["## 职责", "## 依赖关系", "## 坑与边界"]
    assert "knowledge_kind: service" in chosen.page and "## 坑与边界\n\n无" in chosen.page
    assert gate.validate_document(service_rel, chosen.page).ok

    deployed = _domain_route(rt, service_rel, "---\nknowledge_kind: deployment\n---\n\n## 部署单元\n\n- order-service x2\n", wiki_route=declared, title="order-service")
    assert deployed.routed is True
    assert [line for line in deployed.page.splitlines() if line.startswith("## ")] == ["## 部署单元", "## 依赖关系", "## 坑与边界"]

    unknown = _domain_route(rt, service_rel, "---\nknowledge_kind: ledger\n---\n\n" + draft, wiki_route=declared, title="order-service")
    assert (unknown.routed, unknown.reason_code) == (False, "declaration_invalid")
    assert "ledger" in unknown.detail


@pytest.mark.parametrize(
    ("page_type", "relative_path", "code"),
    [
        # Outside every domain directory of the declared page type.
        ("project-algorithm", f"{PROJECTS}/{DEPOSIT_PROJECT[0]}/实战笔记/2026-11-27-orders.md", "domain_path_mismatch"),
        ("project-algorithm", f"{PROJECTS}/{DEPOSIT_PROJECT[0]}/项目配置/orders.md", "domain_path_mismatch"),
        ("project-algorithm", f"{PROJECTS}/2026-11-27-orders.md", "domain_path_mismatch"),
        ("project-algorithm", "", "domain_path_mismatch"),
        # Nested, hidden, archived, or not Markdown: not a page of the domain.
        ("project-algorithm", _page(f"{TABLE_DIR}/notes", "orders"), "domain_path_mismatch"),
        ("project-algorithm", _page(TABLE_DIR, ".orders"), "domain_path_mismatch"),
        ("project-algorithm", _page(TABLE_DIR, "archive-orders"), "domain_path_mismatch"),
        ("project-algorithm", f"{_page(TABLE_DIR, 'orders')}.txt", "domain_path_mismatch"),
        # A configuration page is not a table page, and a table page is not configuration.
        ("project-config", ORDERS_REL, "domain_path_mismatch"),
        ("project-architecture", ORDERS_REL, "domain_path_mismatch"),
        # One table per page.
        ("project-algorithm", _page(TABLE_DIR, "orders,payments"), "table_page_name_invalid"),
        ("project-algorithm", _page(TABLE_DIR, "orders+payments"), "table_page_name_invalid"),
        ("project-algorithm", _page(TABLE_DIR, "orders、payments"), "table_page_name_invalid"),
    ],
)
def test_a_domain_page_path_must_name_one_page_of_the_domains_directory(tmp_path: Path, page_type, relative_path, code):
    rt = _deposit_runtime(tmp_path)
    if relative_path:
        _write(rt.knowledge.root, relative_path, ORDERS_PAGE)  # an update is refused on the same grounds
    for action in ("create", "update"):
        decision = _domain_route(
            rt, relative_path, PAYMENTS_DRAFT, wiki_route={"page_type": page_type, "project": "alpha", "reason": "…"}, action=action
        )

        assert (decision.routed, decision.reason_code) == (False, code), (relative_path, action)
        assert UNROUTED_REASON_TEXT[code] in decision.detail
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() == ORDERS_PAGE.encode("utf-8")


def test_one_table_per_page_applies_to_the_table_domain_only(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    caliber_rel = _page("业务算法", "amount,rounding")
    draft = "---\nknowledge_kind: caliber\n---\n\n## 口径定义\n\n金额口径：含税。\n"

    decision = _domain_route(rt, caliber_rel, draft, title="amount,rounding")

    assert (decision.routed, decision.domain) == (True, "业务算法")
    assert UNROUTED_REASON_LABEL["table_page_name_invalid"] == "表页命名不符"
    for name, single in (("orders", True), ("orders,payments", False), ("订单表 支付表", True), ("", False)):
        assert is_single_table_name(name) is single


def test_the_path_pattern_of_the_page_type_is_kept(tmp_path: Path):
    def prefix_table_pages(content):
        content["page_types"]["project-algorithm"]["path_pattern"] = r"^[^/]+/(业务算法|数据库结构)/t_[^/]+\.md$"

    rt = _deposit_runtime(tmp_path, schema=deposit_schema(prefix_table_pages), pages=False)
    _write(rt.knowledge.root, ORDERS_REL, ORDERS_PAGE)
    gate = rt.knowledge.wiki_gate

    # A new page whose name breaks the pattern is not built.
    misplaced = _domain_route(rt, PAYMENTS_REL, PAYMENTS_DRAFT, title="payments")
    assert (misplaced.routed, misplaced.reason_code) == (False, "path_pattern_mismatch")
    assert "PATH_PATTERN_MISMATCH" in misplaced.detail
    # Nor is an increment merged into a page that breaks it: the merged page would fail the gate.
    existing = _domain_route(rt, ORDERS_REL, ORDERS_INCREMENT, action="update")
    assert (existing.routed, existing.reason_code) == (False, "update_target_invalid")
    assert "PATH_PATTERN_MISMATCH" in existing.detail
    # The same draft under the name the pattern asks for is built and passes.
    fitting = _domain_route(rt, _page(TABLE_DIR, "t_payments"), PAYMENTS_DRAFT, title="payments")
    assert fitting.routed is True
    assert gate.validate_document(_page(TABLE_DIR, "t_payments"), fitting.page).ok


def test_domain_pages_pass_the_same_project_checks_as_cards(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    _register_project(rt.knowledge.root, *BILLING_PROJECT)
    beta_draft = PAYMENTS_DRAFT.replace("逻辑关联 orders.id", "逻辑关联 beta-billing 的 orders.id")

    def route(path: str = PAYMENTS_REL, content: str = PAYMENTS_DRAFT, **overrides):
        params: dict[str, Any] = {"relative_path": path, "content": content, "wiki_route": dict(TABLE_ROUTE)}
        params.update(overrides)
        return _domain_route(rt, **params)

    # The declaration names a project exactly; an unknown one is never corrected.
    unknown = route(wiki_route={**TABLE_ROUTE, "project": "gamma"})
    assert (unknown.routed, unknown.reason_code) == (False, "declaration_invalid")
    assert "gamma" in unknown.detail
    assert route(wiki_route={**TABLE_ROUTE, "project": None}).reason_code == "declared_none"
    assert route(wiki_route={**TABLE_ROUTE, "project": "无归属"}).reason_code == "declared_none"
    # Workspace evidence settles the question when there is any. (The target path names the
    # project too, so it shows up under the content evidence.)
    conflict = route(source_workspaces=["D:/work/beta-billing"])
    assert (conflict.routed, conflict.reason_code) == (False, "workspace_mismatch")
    assert conflict.evidence == {"workspace": [BILLING_PROJECT[0]], "content": [DEPOSIT_PROJECT[0]]}
    assert BILLING_PROJECT[0] in conflict.detail
    # With no workspace, a draft (or a path) that names another project and not the declared one
    # contradicts it.
    mention = route(path=f"{_page(TABLE_DIR, 'orders', BILLING_PROJECT[0])}", content=beta_draft)
    assert (mention.routed, mention.reason_code) == (False, "content_mismatch")
    assert mention.evidence == {"workspace": [], "content": [BILLING_PROJECT[0]]}
    another_project = route(path=_page(TABLE_DIR, "orders", BILLING_PROJECT[0]))
    assert (another_project.reason_code, another_project.evidence["content"]) == ("content_mismatch", [BILLING_PROJECT[0]])
    # ...a workspace that agrees outweighs the project the draft mentions. (A path outside the
    # project directory names no project itself; the verdict then stops at the path check.)
    agreed = route(path="pending.md", content=beta_draft, source_workspaces=["D:/work/alpha-orders"])
    assert agreed.reason_code == "domain_path_mismatch"
    assert agreed.evidence == {"workspace": [DEPOSIT_PROJECT[0]], "content": [BILLING_PROJECT[0]]}
    # ...and with no workspace evidence at all the declaration alone routes.
    quiet = route()
    assert (quiet.routed, quiet.evidence["workspace"], quiet.evidence["content"]) == (True, [], [DEPOSIT_PROJECT[0]])
    # Only create and update exist for a domain page.
    for action in ("delete", "overwrite_core", ""):
        refused = route(action=action)
        assert (refused.routed, refused.reason_code) == (False, "declaration_invalid")
        assert "create 或 update" in refused.detail
    # Without a declaration nothing reaches a domain page: the evidence fallback plans cards only,
    # and a model plan without a declaration is not routed at all.
    fallback = _domain_route(rt, PAYMENTS_REL, PAYMENTS_DRAFT, wiki_route=None, project_hint="alpha", allow_evidence_fallback=True)
    assert (fallback.routed, fallback.source, fallback.page_type, fallback.domain) == (True, ROUTE_EVIDENCE, "project-practice-note", "")
    bare = _domain_route(rt, PAYMENTS_REL, PAYMENTS_DRAFT, wiki_route=None, allow_evidence_fallback=False)
    assert (bare.routed, bare.reason_code) == (False, "declaration_missing")


def test_the_configuration_page_is_a_sensitive_write_that_never_takes_a_credential(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate

    decision = _domain_route(rt, DATABASE_REL, DATABASE_INCREMENT, wiki_route=CONFIG_ROUTE, action="update")

    assert (decision.routed, decision.domain, decision.sensitive) == (True, "项目配置", True)
    assert decision.increment == (
        "## 配置矩阵表\n\n| logical_db.payments | shard_1 | 支付库路由 |\n\n"
        "## 使用规则\n\n- 逻辑库 payments 与 orders 不在同一分片，跨库关联只能在应用层做。\n\n"
        "## 变更记录表\n\n| 2026-11-26 | 新增 payments 逻辑库映射 | 会话查证 |"
    )
    # A configuration fact may say where a credential lives; it may not carry one, in any spelling.
    for rule, value in (
        ("测试库密码：Hk9-Qp31x", "Hk9-Qp31x"),
        ("测试库 password: Hk9-Qp31x，只在本地使用。", "Hk9-Qp31x"),
        ("测试库口令: correct-horse", "correct-horse"),
        ("网关 token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abc", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"),
    ):
        for action in ("update", "create"):
            blocked = _domain_route(
                rt, DATABASE_REL, f"## 使用规则\n\n- {rule}。\n", wiki_route=CONFIG_ROUTE, action=action
            )

            assert (blocked.routed, blocked.reason_code) == (False, "secret_leak_blocked"), (rule, action)
            assert blocked.detail.startswith(SECRET_LEAK_BLOCKED + "：")
            assert value not in blocked.detail  # the trace names the shape, never the credential
            assert UNROUTED_REASON_LABEL["secret_leak_blocked"] == "密钥门禁阻断"
    # A line that only mentions a credential does not hide the one after it that carries it.
    later = _domain_route(
        rt, DATABASE_REL, f"## 使用规则\n\n- 密码：******（由运维保管）\n- 回收口令：Hk9-Qp31x\n", wiki_route=CONFIG_ROUTE, action="update"
    )
    assert later.reason_code == "secret_leak_blocked"
    # Pointers, masks and phrases are configuration facts, not credentials.
    for fact in (
        "- 密码统一从环境变量读取，页面不记录口令。",
        "- 数据库密码：******",
        "- password: ${DB_PASSWORD}",
        "- 密钥：统一走环境变量注入",
        "- 使用 Bearer token 鉴权",
    ):
        allowed = _domain_route(rt, DATABASE_REL, f"## 使用规则\n\n{fact}\n", wiki_route=CONFIG_ROUTE, action="update")

        assert allowed.routed is True, fact
    # The predicate behind the verdict, on each of its three grounds.
    assert gate.is_sensitive_page(DATABASE_REL, "", existing_text=DATABASE_PAGE)
    assert gate.is_sensitive_page(_page(CONFIG_DIR, "payments"), "")
    assert gate.is_sensitive_page(f"{PROJECTS}/2026-11-27-x.md", "---\ntype: project-config\n---\n\n# x\n")
    assert gate.is_sensitive_page(ORDERS_REL, "---\ntype: project-algorithm\nsensitivity: private_sensitive\n---\n\n# orders\n")
    assert gate.is_sensitive_page(ORDERS_REL, "", existing_text=ORDERS_PAGE) is False
    assert gate.is_sensitive_page(ORDERS_REL, ORDERS_INCREMENT) is False
    assert (rt.knowledge.root / DATABASE_REL).read_bytes() == DATABASE_PAGE.encode("utf-8")


def test_an_increment_fills_the_placeholders_the_page_started_with(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    join_keys = "## 关联键\n\n- id → payments.order_id\n"

    decision = _domain_route(rt, ORDERS_REL, join_keys, action="update")

    assert (decision.routed, decision.placeholders) == (True, ["关联键"])
    assert decision.increment == join_keys.rstrip("\n")
    merged = merge_domain_increment(ORDERS_PAGE, decision.increment, template_sections=T7_SECTIONS, today="").merged
    assert "## 关联键\n\n无\n" not in merged
    assert "## 关联键\n\n- id → payments.order_id\n" in merged
    # `today` empty: a plan-stage dry check leaves the frontmatter untouched.
    assert "updated: 2026-11-20" in merged


def test_a_dry_run_plan_on_a_wiki_with_the_deposit_routing_still_writes_its_card(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)  # no runner configured: the heuristic planner, no declaration
    key = _register_orders_session(rt, tmp_path / "work" / "alpha")

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=[key], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    (finding,) = _node(run, "P02_REVIEW").payload["findings"]
    assert finding["domain_signals"] == empty_domain_signals()
    routed = _node(run, "P03_ROUTE_PLAN").payload["routed_docs"]
    assert [(doc["route"]["source"], doc["route"]["page_type"]) for doc in routed] == [(ROUTE_EVIDENCE, "project-practice-note")]
    assert "domain" not in routed[0]["route"]
    # The domain pages are not touched by a plan that holds no domain item.
    assert (rt.knowledge.root / ORDERS_REL).read_bytes() == ORDERS_PAGE.encode("utf-8")
    assert (rt.knowledge.root / DATABASE_REL).read_bytes() == DATABASE_PAGE.encode("utf-8")
    assert rt.db.get_session(key).status.value == "processed"
