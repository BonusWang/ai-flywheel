from __future__ import annotations

import json
import textwrap
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
import yaml

from ai_flywheel.adapters.wiki_gate import WikiGateError, split_frontmatter
from ai_flywheel.adapters.knowledge import KnowledgeWriteError
from ai_flywheel.adapters.channels.feishu.card import render_ask_card
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import (
    OutcomeStatus,
    PlannedWrite,
    PreflightRequest,
    RecordOutcomeRequest,
    RegisterSessionRequest,
    RunStatus,
)
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


def _make_runtime(tmp_path: Path, *, require_scripts: bool = False) -> FlywheelRuntime:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "wiki-知识库" / "projects-项目").mkdir(parents=True)
    (wiki_root / "raw-原始素材").mkdir(parents=True)
    (wiki_root / ".wiki-schema.md").write_text(SCHEMA, encoding="utf-8")

    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(wiki_root)
    cfg.knowledge_sink.allowed_write_prefixes = ["wiki-知识库/"]
    cfg.knowledge_sink.wiki_gate_enabled = True
    cfg.knowledge_sink.wiki_gate_require_scripts = require_scripts
    cfg.knowledge_sink.wiki_d2_postflight_script = ""
    cfg.knowledge_sink.wiki_lint_script = ""
    return FlywheelRuntime(cfg)


def _write(wiki_root: Path, rel: str, content: str) -> Path:
    target = wiki_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _practice_note(path: str, title: str, body: str, status: str = "active", sensitivity: str = "internal") -> str:
    return textwrap.dedent(
        f"""\
        ---
        title: {title}
        description: 症状：缓存雪崩；组件：Redis；环境：本地
        type: project-practice-note
        project_id: demo
        record_id: demo-2026-11-23-cache
        event_date: 2026-11-23
        load_policy: on_demand
        default_load: false
        created: 2026-11-23
        updated: 2026-11-23
        status: {status}
        tags: [缓存, redis]
        sensitivity: {sensitivity}
        ---

        # {title}

        > 背景与结论：缓存命中率骤降导致接口超时；根因是同一窗口批量过期。

        ## 好流程（照此执行）

        预热并打散过期时间。

        ## 错误示范（坑）

        全部 key 同一秒过期。

        ## 关键事项（已收口事实）

        {body}

        ## 遗留与待办

        无

        ## 证据指针

        - `{path}`

        > 分流判定：算法/口径 → 无：本次为运行态排障。
        """
    )


def _lite_note(title: str) -> str:
    return textwrap.dedent(
        f"""\
        ---
        title: {title}
        description: 症状：缓存雪崩；组件：Redis；环境：本地
        type: project-practice-lite
        project_id: demo
        record_id: demo-2026-11-23-lite
        load_policy: on_demand
        default_load: false
        created: 2026-11-23
        updated: 2026-11-23
        status: active
        tags: [缓存, redis]
        sources: ["C:/Users/example/.codex/work/evidence.json（临时产物，可能已清理）"]
        ---

        # {title}

        ## 结论

        用 `INFO keyspace` 与命中率曲线确认雪崩窗口。

        ## 适用范围

        单实例 Redis 读缓存。

        ## 来源

        [[demo 实战笔记索引]]
        """
    )


def test_fts_strict_formal_active_and_chinese_recall(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    root = rt.knowledge.root

    active = _write(
        root,
        "wiki-知识库/projects-项目/demo/实战笔记/2026-11-23-缓存雪崩.md",
        _practice_note("缓存雪崩根因定位", "缓存雪崩根因定位", "命中率曲线与过期时间打散"),
    )
    body_only = _write(
        root,
        "wiki-知识库/projects-项目/demo/实战笔记/2026-11-23-环境清单.md",
        _practice_note("环境排查清单", "环境排查清单", "缓存雪崩时先看 Redis 命中率"),
    )
    _write(
        root,
        "wiki-知识库/projects-项目/demo/archive-2026/2026-01-01-旧卡.md",
        _practice_note("旧卡", "旧卡", "缓存雪崩历史记录"),
    )
    _write(
        root,
        "wiki-知识库/projects-项目/demo/实战笔记/2026-11-23-退役.md",
        _practice_note("退役卡", "退役卡", "缓存雪崩旧方案", status="retired"),
    )
    _write(
        root,
        "wiki-知识库/projects-项目/demo/实战笔记/2026-11-23-凭据.md",
        _practice_note("凭据页", "凭据页", "缓存雪崩与密码", sensitivity="private_sensitive"),
    )
    _write(
        root,
        "raw-原始素材/2026-11-23-素材.md",
        _practice_note("素材", "素材", "缓存雪崩原始记录"),
    )

    assert rt.knowledge.sync_fts_index() == 2
    hits = rt.db.search_knowledge_fts("缓存雪崩", limit=5)
    paths = [hit["doc_path"] for hit in hits]
    assert active.relative_to(root).as_posix() in paths
    assert body_only.relative_to(root).as_posix() in paths
    assert all("archive-" not in path and "raw-原始素材" not in path for path in paths)
    assert all("退役" not in path and "凭据" not in path for path in paths)
    # Title/description metadata outranks body-only matches.
    assert paths[0] == active.relative_to(root).as_posix()


def test_gate_blocks_bad_write_and_rolls_back_update(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    rel = "wiki-知识库/projects-项目/demo/实战笔记/2026-11-23-缓存雪崩.md"

    with pytest.raises(WikiGateError) as exc:
        rt.knowledge.write_document_verified(rel, "# 没有 frontmatter 的页面\n")
    assert "FRONTMATTER_MISSING" in str(exc.value)
    assert not (rt.knowledge.root / rel).exists()

    good = _lite_note("缓存雪崩快速定位")
    result = rt.knowledge.write_document_verified(rel, good)
    assert result["verified"] is True
    assert result["wiki_gate"]["ok"] is True
    assert result["wiki_gate"]["page_type"] == "project-practice-lite"
    assert (rt.knowledge.root / rel).read_text(encoding="utf-8") == good

    bad_update = _lite_note("缓存雪崩快速定位").replace("record_id: demo-2026-11-23-lite\n", "")
    with pytest.raises(WikiGateError) as exc:
        rt.knowledge.write_document_verified(rel, bad_update, action="update")
    assert "record_id" in str(exc.value)
    # Rollback restores the exact preimage bytes.
    assert (rt.knowledge.root / rel).read_text(encoding="utf-8") == good


def test_gate_rolls_back_when_postflight_blocks(tmp_path: Path):
    from ai_flywheel.adapters.wiki_gate import resolve_bash_executable

    bash = resolve_bash_executable("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("bash is required for the D2 postflight rollback test")
    rt = _make_runtime(tmp_path, require_scripts=True)
    fake = tmp_path / "fake-d2.sh"
    fake.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
    rt.cfg.knowledge_sink.wiki_d2_postflight_script = str(fake)
    rt.cfg.knowledge_sink.wiki_bash = bash

    rel = "wiki-知识库/projects-项目/demo/实战笔记/2026-11-23-缓存雪崩.md"
    with pytest.raises(WikiGateError) as exc:
        rt.knowledge.write_document_verified(rel, _lite_note("缓存雪崩快速定位"))
    assert "D2_BLOCKED" in str(exc.value)
    assert not (rt.knowledge.root / rel).exists()


def test_gate_blocks_core_files_even_when_prefix_allows(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    rt.cfg.knowledge_sink.allowed_write_prefixes = [""]
    schema_path = rt.knowledge.root / ".wiki-schema.md"
    preimage = schema_path.read_text(encoding="utf-8")
    with pytest.raises(WikiGateError) as exc:
        rt.knowledge.write_document_verified(".wiki-schema.md", preimage + "\n")
    assert "CORE_FILE_BLOCKED" in str(exc.value)
    assert schema_path.read_text(encoding="utf-8") == preimage


def test_card_metrics_surface_cited_and_helped(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    root = rt.knowledge.root
    doc = _write(
        root,
        "wiki-知识库/projects-项目/demo/实战笔记/2026-11-23-缓存雪崩.md",
        _practice_note("缓存雪崩根因定位", "缓存雪崩根因定位", "命中率曲线与过期时间打散"),
    )
    rel = doc.relative_to(root).as_posix()
    rt.knowledge.sync_fts_index()

    pf = rt.activation.preflight_activate(
        PreflightRequest(task="缓存雪崩怎么快速定位", client="claude-code", session_id="s1")
    )
    assert any(hit["doc_path"] == rel for hit in pf.knowledge_hits)
    metrics = {row["doc_path"]: row for row in rt.db.get_knowledge_card_metrics()}
    assert metrics[rel]["surface_count"] == 1
    assert metrics[rel]["helped_count"] == 0

    rt.receipts.record_outcome(
        RecordOutcomeRequest(
            session_key="claude-code:s1",
            client="claude-code",
            preflight_receipt_id=pf.receipt_id,
            status=OutcomeStatus.PARTIAL,
            summary="按卡片定位了雪崩窗口",
            used_knowledge_paths=[rel],
        )
    )
    metrics = {row["doc_path"]: row for row in rt.db.get_knowledge_card_metrics()}
    assert metrics[rel]["helped_count"] == 1
    assert metrics[rel]["cited_count"] == 1

    # Helped without a preflight receipt still counts; cited does not.
    rt.receipts.record_outcome(
        RecordOutcomeRequest(
            session_key="claude-code:s2",
            client="claude-code",
            status=OutcomeStatus.PARTIAL,
            summary="再次复用同一张卡",
            used_knowledge_paths=[rel],
        )
    )
    metrics = {row["doc_path"]: row for row in rt.db.get_knowledge_card_metrics()}
    assert metrics[rel]["helped_count"] == 2
    assert metrics[rel]["cited_count"] == 1


def _add_project(wiki_root: Path, dir_name: str, project_id: str) -> None:
    """Register a Wiki project that hosts 实战笔记/ (the only routing candidates)."""
    practice = wiki_root / "wiki-知识库" / "projects-项目" / dir_name / "实战笔记"
    practice.mkdir(parents=True)
    (practice / "笔记索引.md").write_text(
        f"---\ntitle: {dir_name} 索引\ndescription: {dir_name} 实战经验卡索引\n"
        f"type: project-practice-index\nproject_id: {project_id}\n"
        "created: 2026-11-23\nupdated: 2026-11-23\nstatus: active\n---\n\n"
        f"# {dir_name} 索引\n",
        encoding="utf-8",
    )


_ROUTING_DRAFT = "---\ntitle: 受控写入门禁排查\ntags: [wiki]\n---\n\n# 受控写入门禁排查\n- 草稿必须在 preflight 前定稿。\n"
_ROUTING_REL = "wiki-知识库/projects-项目/2026-11-24-受控写入门禁排查.md"


def test_normalize_routes_project_on_evidence_and_never_guesses(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, "alpha-crm-Alpha客户平台", "alpha-crm")
    _add_project(rt.knowledge.root, "core-hub-Core中枢", "core-hub")
    _add_project(rt.knowledge.root, "zeta-tools-Zeta工具", "zeta-tools")
    gate = rt.knowledge.wiki_gate

    # 1) Workspace evidence: a path segment equal to the project id.
    rel, md = gate.normalize_planned_document(
        _ROUTING_REL, "受控写入门禁排查", "", _ROUTING_DRAFT, source_workspaces=["D:/work/alpha-crm"]
    )
    today = date.today().isoformat()
    assert rel == f"wiki-知识库/projects-项目/alpha-crm-Alpha客户平台/实战笔记/{today}-受控写入门禁排查.md"
    assert gate.validate_document(rel, md).ok

    # 2) Workspace evidence: a segment that contains the project id.
    rel, _ = gate.normalize_planned_document(
        _ROUTING_REL, "受控写入门禁排查", "", _ROUTING_DRAFT, source_workspaces=["D:/work/zeta-tools-admin"]
    )
    assert "/zeta-tools-Zeta工具/实战笔记/" in rel

    # 3) Workspace evidence wins over a project merely mentioned in the draft.
    rel, _ = gate.normalize_planned_document(
        _ROUTING_REL,
        "受控写入门禁排查",
        "",
        _ROUTING_DRAFT.replace("草稿必须", "zeta-tools 的草稿必须"),
        source_workspaces=["D:/work/alpha-crm"],
    )
    assert "/alpha-crm-Alpha客户平台/实战笔记/" in rel

    # 4) Content evidence: the project id appears as a standalone word.
    rel, _ = gate.normalize_planned_document(
        _ROUTING_REL,
        "受控写入门禁排查",
        "",
        _ROUTING_DRAFT.replace("草稿必须", "zeta-tools 的草稿必须"),
        project_hint="generic D:/notes",
    )
    assert "/zeta-tools-Zeta工具/实战笔记/" in rel

    # 5) A generic leading word ("core") is not evidence.
    assert (
        gate.normalize_planned_document(
            _ROUTING_REL,
            "受控写入门禁排查",
            "",
            _ROUTING_DRAFT.replace("草稿必须", "core-card 的草稿必须"),
            project_hint="generic D:/notes",
        )
        is None
    )

    # 6) No evidence at all: unrouted instead of guessed into an existing project.
    assert (
        gate.normalize_planned_document(
            _ROUTING_REL, "受控写入门禁排查", "", _ROUTING_DRAFT, project_hint="generic D:/notes"
        )
        is None
    )

    # 7) An explicit fallback project makes the catch-all visible in configuration.
    rt.cfg.knowledge_sink.practice_fallback_project = "core-hub-Core中枢"
    rel, md = gate.normalize_planned_document(_ROUTING_REL, "受控写入门禁排查", "", _ROUTING_DRAFT)
    assert "/core-hub-Core中枢/实战笔记/" in rel
    assert gate.validate_document(rel, md).ok


def test_normalize_maps_draft_sections_without_fabrication(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, "alpha-crm-Alpha客户平台", "alpha-crm")
    gate = rt.knowledge.wiki_gate
    facts = [f"事实要点{i}：步骤说明。" for i in range(1, 10)]
    anomaly = "写入工具返回成功但结果与已预检草稿的字节不一致。"
    session_key = "codex:rollout-2026-11-22T16-29-19-01a2025f"
    draft = (
        "---\ntitle: 受控写入门禁排查\ntags: [wiki]\n---\n\n# 受控写入门禁排查\n\n"
        "## 事实回顾\n" + "\n".join(f"- {fact}" for fact in facts) + "\n\n"
        f"## 异常与根因线索\n- {anomaly}\n\n"
        "## 适用前提与失效条件\n- 仅适用于带 preflight 的受控写入。\n\n"
        "## 分歧与反例\n- 暂未记录反例；后续遇到例外场景可追加记录。\n\n"
        "## 复用建议\n- 下次先定稿草稿再预检。\n"
    )

    def adapt(text: str, summary: str) -> tuple[str, str, str, dict]:
        rel, md = gate.normalize_planned_document(
            _ROUTING_REL,
            "受控写入门禁排查",
            summary,
            text,
            source_sessions=[session_key],
            source_workspaces=["D:/work/alpha-crm"],
        )
        assert rel is not None
        fm_text, body = split_frontmatter(md)
        return rel, md, body, yaml.safe_load(fm_text)

    def section(body: str, name: str) -> str:
        start = body.index(f"## {name}")
        end = body.find("\n## ", start + 1)
        return body[start : end if end != -1 else len(body)]

    rel, md, body, frontmatter = adapt(draft, f"沉淀来自 {session_key} 的协作经验卡")

    # The recorded anomaly is the mistake; no boilerplate mistake is invented.
    assert anomaly in section(body, "错误示范")
    assert "跳过前置核查或凭记忆沿用过期方案" not in body
    # Every fact survives the mapping.
    for fact in facts:
        assert fact in section(body, "好流程") or fact in section(body, "关键事项")
    assert "下次先定稿草稿再预检。" in section(body, "好流程")
    assert "适用前提与失效条件：仅适用于带 preflight 的受控写入。" in section(body, "关键事项")
    assert "分歧与反例" in section(body, "遗留与待办")
    # The description is a real statement, never the raw session key.
    assert frontmatter["description"] == anomaly
    assert session_key not in frontmatter["description"]
    assert f"> 背景与结论：{anomaly}" in body
    assert frontmatter["record_id"] == f"{date.today().isoformat()}-card-{__import__('hashlib').sha1('受控写入门禁排查'.encode('utf-8')).hexdigest()[:8]}"
    assert gate.validate_document(rel, md).ok

    # A meaningful summary is preferred over the draft body.
    _, _, _, frontmatter = adapt(draft, "写入后必须比对草稿与目标文件的哈希")
    assert frontmatter["description"] == "写入后必须比对草稿与目标文件的哈希"

    # Without a recorded anomaly the section says so instead of inventing one.
    _, _, body, frontmatter = adapt(draft.replace(f"## 异常与根因线索\n- {anomaly}\n\n", ""), "")
    assert "本次会话未记录具体的错误示范" in section(body, "错误示范")
    assert frontmatter["description"] == facts[0]


def test_run_skips_unrouted_docs_and_still_passes(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, "alpha-crm-Alpha客户平台", "alpha-crm")
    rt.cfg.workflow.approval_mode = "auto"
    rt.cfg.safety_valves.auto_approve_writes = True
    rt.register_session(
        RegisterSessionRequest(
            client="claude-code",
            session_id="alpha-1",
            title="受控写入哈希比对复盘",
            project="generic",
            workspace=str(tmp_path / "work" / "alpha-crm"),
            summary="写入后必须比对哈希，否则可能出现内容漂移。",
            content="用户：写入成功了。助手：还需要比对草稿与目标文件的哈希。",
        )
    )
    rt.register_session(
        RegisterSessionRequest(
            client="claude-code",
            session_id="notes-1",
            title="笔记整理复盘",
            project="generic",
            workspace=str(tmp_path / "notes"),
            summary="整理笔记时先分类再归档。",
            content="用户：笔记怎么整理。助手：先分类再归档。",
        )
    )

    run = rt.orchestrator.start_run(
        trigger="test",
        phase="daily",
        session_keys=["claude-code:alpha-1", "claude-code:notes-1"],
        auto_approve_override=True,
    )

    assert run.status == RunStatus.PASS, run.error
    p03 = next(n for n in run.nodes if n.node_id == "P03_ROUTE_PLAN")
    assert [doc["title"] for doc in p03.payload["unrouted_docs"]] == ["笔记整理复盘"]
    assert "1 篇笔记因无法归属项目未列入计划" in p03.summary
    docs = [w for w in run.planned_writes if w.target_kind == "knowledge_doc"]
    assert len(docs) == 1
    assert "/alpha-crm-Alpha客户平台/实战笔记/" in docs[0].relative_path
    practice_root = rt.knowledge.root / "wiki-知识库" / "projects-项目"
    cards = [p.name for p in practice_root.rglob("*.md") if p.name != "笔记索引.md"]
    assert cards == [docs[0].relative_path.rsplit("/", 1)[-1]]
    events = rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id, limit=50)
    assert any(row["event_type"] == "planned_doc_unrouted" for row in events)
    # The second session is still settled by P05; a skipped card must not strand it.
    assert rt.db.get_session("claude-code:notes-1").status.value == "processed"


def test_practice_note_and_lite_write_register_once_with_combined_postflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, "demo-project", "demo")
    practice = (
        rt.knowledge.root
        / "wiki-知识库"
        / "projects-项目"
        / "demo-project"
        / "实战笔记"
    )
    index_path = practice / "笔记索引.md"
    postflight_paths: list[list[str]] = []

    def _capture_postflight(result, paths):
        postflight_paths.append(list(paths))

    monkeypatch.setattr(rt.knowledge.wiki_gate, "run_postflight", _capture_postflight)

    note_rel = "wiki-知识库/projects-项目/demo-project/实战笔记/2026-11-23-缓存雪崩.md"
    note_md = _practice_note(note_rel, "缓存雪崩根因定位", "预热并打散 key 的过期时间")
    note_registration = rt.knowledge.wiki_gate.plan_practice_index_registration(note_rel, note_md)
    assert note_registration is not None and note_registration.status == "update"
    note_receipt = rt.knowledge.write_document_verified(
        note_rel,
        note_md,
        sync_fts=False,
        sync_practice_index=True,
        approved_side_effects=[note_registration.side_effect()],
    )

    lite_rel = "wiki-知识库/projects-项目/demo-project/实战笔记/2026-11-23-Redis命中率.md"
    lite_md = _lite_note("Redis 命中率快速定位")
    lite_registration = rt.knowledge.wiki_gate.plan_practice_index_registration(lite_rel, lite_md)
    assert lite_registration is not None and lite_registration.status == "update"
    lite_receipt = rt.knowledge.write_document_verified(
        lite_rel,
        lite_md,
        sync_fts=False,
        sync_practice_index=True,
        approved_side_effects=[lite_registration.side_effect()],
    )

    index_text = index_path.read_text(encoding="utf-8")
    assert index_text.count(note_rel.removesuffix(".md")) == 1
    assert index_text.count(lite_rel.removesuffix(".md")) == 1
    assert "## P04 自动登记" in index_text
    assert note_receipt["practice_index"]["status"] == "update"
    assert lite_receipt["practice_index"]["status"] == "update"
    assert postflight_paths == [
        [note_rel, note_registration.target_relative_path],
        [lite_rel, lite_registration.target_relative_path],
    ]

    repeat_registration = rt.knowledge.wiki_gate.plan_practice_index_registration(note_rel, note_md)
    assert repeat_registration is not None and repeat_registration.status == "already_present"
    repeat_receipt = rt.knowledge.write_document_verified(
        note_rel,
        note_md,
        sync_fts=False,
        sync_practice_index=True,
        approved_side_effects=[],
    )
    assert repeat_receipt["practice_index"]["status"] == "already_present"
    assert index_path.read_text(encoding="utf-8").count(note_rel.removesuffix(".md")) == 1


def test_practice_index_postflight_failure_rolls_back_card_and_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, "demo-project", "demo")
    practice = (
        rt.knowledge.root
        / "wiki-知识库"
        / "projects-项目"
        / "demo-project"
        / "实战笔记"
    )
    index_path = practice / "笔记索引.md"
    index_preimage = index_path.read_bytes()
    rel = "wiki-知识库/projects-项目/demo-project/实战笔记/2026-11-23-回滚验证.md"
    content = _practice_note(rel, "回滚验证", "任何一项失败都必须恢复两处 preimage")
    registration = rt.knowledge.wiki_gate.plan_practice_index_registration(rel, content)
    assert registration is not None and registration.status == "update"
    resync_calls: list[list[str]] = []

    def _block_postflight(result, paths):
        raise RuntimeError("sandbox postflight raised")

    def _resync(paths):
        resync_calls.append(list(paths))
        return {"status": "pass", "returncode": 0}

    monkeypatch.setattr(rt.knowledge.wiki_gate, "run_postflight", _block_postflight)
    monkeypatch.setattr(rt.knowledge.wiki_gate, "resync_after_rollback", _resync)

    with pytest.raises(WikiGateError) as excinfo:
        rt.knowledge.write_document_verified(
            rel,
            content,
            sync_fts=False,
            sync_practice_index=True,
            approved_side_effects=[registration.side_effect()],
        )

    assert "D2_FAILED" in str(excinfo.value)
    assert "sandbox postflight raised" in str(excinfo.value)
    assert not (rt.knowledge.root / rel).exists()
    assert index_path.read_bytes() == index_preimage
    assert resync_calls == [[rel, registration.target_relative_path]]
    evidence_files = sorted(rt.cfg.wiki_gate_evidence_path.glob("gate-*.json"))
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in evidence_files]
    post_write = [item for item in payloads if item.get("stage") == "post_write"]
    assert post_write
    rollback = post_write[-1]["rollback"]
    assert rollback["state"] == "deleted_new_file"
    assert rollback["practice_index"]["state"] == "restored_preimage"
    assert post_write[-1]["practice_index"]["status"] == "update"


def test_practice_index_update_must_be_in_the_approved_side_effect_scope(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, "demo-project", "demo")
    rel = "wiki-知识库/projects-项目/demo-project/实战笔记/2026-11-23-审批范围.md"
    content = _practice_note(rel, "审批范围", "派生索引写入必须有精确审批预览")
    registration = rt.knowledge.wiki_gate.plan_practice_index_registration(rel, content)
    assert registration is not None and registration.status == "update"
    index_path = rt.knowledge.root / registration.target_relative_path
    index_preimage = index_path.read_bytes()

    with pytest.raises(KnowledgeWriteError, match="not in the approved write scope"):
        rt.knowledge.write_document_verified(
            rel,
            content,
            sync_fts=False,
            sync_practice_index=True,
            approved_side_effects=[],
        )

    assert not (rt.knowledge.root / rel).exists()
    assert index_path.read_bytes() == index_preimage


def test_concurrent_practice_cards_on_the_same_index_preserve_both_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    rt = _make_runtime(tmp_path)
    _add_project(rt.knowledge.root, "demo-project", "demo")
    monkeypatch.setattr(rt.knowledge.wiki_gate, "run_postflight", lambda result, paths: None)
    practice_rel = "wiki-知识库/projects-项目/demo-project/实战笔记"
    cards = [
        (
            f"{practice_rel}/2026-11-23-并发条目甲.md",
            _practice_note(
                f"{practice_rel}/2026-11-23-并发条目甲.md",
                "并发条目甲",
                "索引锁内必须重新读取当前内容",
            ),
        ),
        (
            f"{practice_rel}/2026-11-23-并发条目乙.md",
            _practice_note(
                f"{practice_rel}/2026-11-23-并发条目乙.md",
                "并发条目乙",
                "同一索引的其它条目不得被覆盖",
            ),
        ),
    ]
    registrations = [
        rt.knowledge.wiki_gate.plan_practice_index_registration(rel, content)
        for rel, content in cards
    ]
    assert all(item is not None and item.status == "update" for item in registrations)

    def _write_one(index: int):
        rel, content = cards[index]
        registration = registrations[index]
        return rt.knowledge.write_document_verified(
            rel,
            content,
            sync_fts=False,
            sync_practice_index=True,
            approved_side_effects=[registration.side_effect()],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(_write_one, range(2)))

    index_path = (
        rt.knowledge.root
        / "wiki-知识库"
        / "projects-项目"
        / "demo-project"
        / "实战笔记"
        / "笔记索引.md"
    )
    index_text = index_path.read_text(encoding="utf-8")
    for rel, _content in cards:
        assert index_text.count(rel.removesuffix(".md")) == 1
    assert {receipt["practice_index"]["status"] for receipt in receipts} == {"update"}
    assert not list(index_path.parent.glob("*.lock"))


def test_p04_rejects_an_approved_physical_set_above_max_write_set(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    rt.cfg.knowledge_sink.max_write_set = 2
    _add_project(rt.knowledge.root, "demo-project", "demo")
    practice_rel = "wiki-知识库/projects-项目/demo-project/实战笔记"
    items: list[PlannedWrite] = []
    for suffix in ("甲", "乙"):
        rel = f"{practice_rel}/2026-11-23-容量{suffix}.md"
        content = _practice_note(rel, f"容量{suffix}", f"容量验证{suffix}")
        registration = rt.knowledge.wiki_gate.plan_practice_index_registration(rel, content)
        assert registration is not None and registration.status == "update"
        items.append(
            PlannedWrite(
                target_kind="knowledge_doc",
                relative_path=rel,
                action="create",
                title=f"容量{suffix}",
                summary="物理写入上限验证",
                content_preview=content,
                side_effects=[registration.side_effect()],
            )
        )

    with pytest.raises(ValueError, match="approved physical write set has 3 paths"):
        rt.orchestrator._validate_p04_knowledge_scope(items)


def test_feishu_approval_card_shows_the_exact_index_side_effect():
    entry = "| 2026-11-23 | 重卡 | [[wiki/Card|审批卡]] |"
    card = json.loads(
        render_ask_card(
            card_id="card-1",
            batch_label="2026-11-23 #01",
            reasons=["索引副作用需要审批"],
            writes=[
                {
                    "target_kind": "knowledge_doc",
                    "action": "create",
                    "relative_path": "wiki/Card.md",
                    "title": "审批卡",
                    "summary": "验证派生写入可见",
                    "approval_reason": "",
                    "content_preview": "",
                    "requires_approval": False,
                    "side_effects": [
                        {
                            "kind": "practice_index_entry",
                            "target_path": "wiki/笔记索引.md",
                            "action": "append_entry",
                            "status": "update",
                            "entry_preview": entry,
                        }
                    ],
                }
            ],
            portal_url="http://127.0.0.1/runs",
        )
    )
    serialized = json.dumps(card, ensure_ascii=False)
    assert "wiki/笔记索引.md" in serialized
    assert "审批卡" in serialized
    assert "2026-11-23" in serialized


def test_sync_fts_index_syncs_wiki_archived_status_and_protects_ledger(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    wiki_root = Path(rt.cfg.knowledge_sink.root)
    proj_dir = wiki_root / "wiki-知识库" / "projects-项目" / "demo-project" / "实战笔记"
    proj_dir.mkdir(parents=True, exist_ok=True)

    # 1. Document with status: archived in frontmatter
    doc1 = proj_dir / "2026-01-01-旧笔记.md"
    doc1_content = textwrap.dedent(
        """\
        ---
        title: 旧实战笔记
        description: 验证归档状态同步
        type: project-practice-lite
        project_id: demo-project
        record_id: rec-old-01
        load_policy: manual
        default_load: false
        created: 2026-01-01
        updated: 2026-01-01
        status: archived
        freshness_tier: evolving
        last_verified: 2026-01-01
        ---

        # 旧实战笔记
        已归档的内容
        """
    )
    doc1.write_text(doc1_content, encoding="utf-8")

    # 2. Document with status: active in frontmatter
    doc2 = proj_dir / "2026-01-01-活跃笔记.md"
    doc2_content = textwrap.dedent(
        """\
        ---
        title: 活跃实战笔记
        description: 验证活跃笔记保鲜
        type: project-practice-lite
        project_id: demo-project
        record_id: rec-act-02
        load_policy: manual
        default_load: false
        created: 2026-01-01
        updated: 2026-01-01
        status: active
        freshness_tier: evolving
        last_verified: 2026-01-01
        ---

        # 活跃实战笔记
        活跃的内容
        """
    )
    doc2.write_text(doc2_content, encoding="utf-8")

    rt.knowledge.sync_fts_index()

    rel1 = "wiki-知识库/projects-项目/demo-project/实战笔记/2026-01-01-旧笔记.md"
    rel2 = "wiki-知识库/projects-项目/demo-project/实战笔记/2026-01-01-活跃笔记.md"

    fmap = rt.db.get_knowledge_freshness_map()
    assert rel1 in fmap
    assert fmap[rel1]["freshness_state"] == "archived"
    assert fmap[rel1]["is_expired"] is False
    # Archived pages stay excluded from FTS recall even though their freshness state is synced.
    assert all(hit["doc_path"] != rel1 for hit in rt.db.search_knowledge_fts("旧实战笔记"))

    assert rel2 in fmap
    assert fmap[rel2]["freshness_state"] == "fresh"

    # Manually archive doc2 in the flywheel SQLite ledger
    rt.db.update_knowledge_freshness_action(rel2, "archive", today_ymd="2026-01-15")
    fmap_archived = rt.db.get_knowledge_freshness_map()
    assert fmap_archived[rel2]["freshness_state"] == "archived"

    # Re-sync without updating disk file: must stay archived, never overwritten to fresh
    rt.knowledge.sync_fts_index()
    fmap_after_resync = rt.db.get_knowledge_freshness_map()
    assert fmap_after_resync[rel2]["freshness_state"] == "archived"

    # Update doc2 on disk with strictly newer last_verified date
    updated_doc2_content = doc2_content.replace("last_verified: 2026-01-01", "last_verified: 2026-02-15")
    doc2.write_text(updated_doc2_content, encoding="utf-8")

    # Re-sync: must re-activate as fresh with new date
    rt.knowledge.sync_fts_index()
    fmap_reactivated = rt.db.get_knowledge_freshness_map()
    assert fmap_reactivated[rel2]["freshness_state"] == "fresh"
    assert fmap_reactivated[rel2]["last_verified_at"] == "2026-02-15"

