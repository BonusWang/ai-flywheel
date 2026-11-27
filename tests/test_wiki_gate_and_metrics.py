from __future__ import annotations

import json
import textwrap
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pytest
import yaml

from ai_flywheel.adapters.wiki_gate import (
    UPDATE_NO_NEW_CONTENT,
    WikiGateError,
    merge_domain_increment,
    preserves_existing,
    split_frontmatter,
)
from ai_flywheel.adapters.knowledge import KnowledgeWriteError
from ai_flywheel.adapters.channels.feishu.card import render_ask_card
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.privacy import SecretLeakError
from ai_flywheel.core.time_utils import shanghai_today_ymd
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


# ---------------------------------------------------------------- project-domain pages at the write gate
# (feat-project-domain-incremental-deposit)


PROJECTS = "wiki-知识库/projects-项目"
ALPHA_DIR = "alpha-orders-Alpha订单系统"
# The two domain pages project alpha holds, and a table page it does not hold yet.
ORDERS_REL = f"{PROJECTS}/{ALPHA_DIR}/数据库结构/orders.md"
DATABASE_REL = f"{PROJECTS}/{ALPHA_DIR}/项目配置/database.md"
PAYMENTS_REL = f"{PROJECTS}/{ALPHA_DIR}/数据库结构/payments.md"
# The sections the table template declares, in their order.
TABLE_SECTIONS = ["用途一行", "字段语义表", "枚举值", "关联键", "坑与边界"]
PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"

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
      legacy_type_aliases: {}
      scope_profiles:
        formal_active:
          partition_ids: [projects]
      page_types:
        project-practice-lite:
          partition: projects
          required_fields: [title, description, type, project_id, record_id, load_policy, default_load, created, updated, status]
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

# A table page as a first session left it: two of the template's sections hold what was verified.
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

    ## 坑与边界

    无
    """
)

# The same page after an increment added two rows and the first pitfall; only `updated` moved.
MERGED_ORDERS_PAGE = textwrap.dedent(
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
    updated: 2026-11-27
    status: active
    ---

    # orders

    ## 用途一行

    订单主表，一笔订单一行。

    ## 字段语义表

    | 字段 | 类型 | 含义 | 备注 |
    | --- | --- | --- | --- |
    | id | bigint | 主键 | 自增 |
    | status | varchar(32) | 订单状态 | PAID / CLOSED |

    ## 坑与边界

    - 跨库关联只能在应用层做。
    """
)

# The configuration page declares itself private: the gate reads it to merge an increment, nothing else.
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

# A table page the Wiki does not hold yet, as the engine builds it from the template.
PAYMENTS_PAGE = textwrap.dedent(
    """\
    ---
    title: payments
    description: 支付流水表：一次支付尝试一行
    type: project-algorithm
    project_id: alpha
    knowledge_kind: ddl
    load_policy: index_then_on_demand
    default_load: false
    created: 2026-11-27
    updated: 2026-11-27
    status: active
    ---

    # payments

    ## 用途一行

    支付流水表：一次支付尝试一行。

    ## 字段语义表

    | 字段 | 类型 | 含义 | 备注 |
    | --- | --- | --- | --- |
    | payment_id | bigint | 支付主键 | 自增 |
    """
)


def _put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # LF, as the Wiki's pages are
    return path


def _deposit_runtime(tmp_path: Path, *, require_scripts: bool = False) -> FlywheelRuntime:
    """A runtime over a Wiki that declares the deposit routing; project alpha holds two domain pages."""
    root = tmp_path / "wiki"
    _put(root / ".wiki-schema.md", DEPOSIT_SCHEMA)
    _put(root / ORDERS_REL, ORDERS_PAGE)
    _put(root / DATABASE_REL, DATABASE_PAGE)
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(root)
    cfg.knowledge_sink.allowed_write_prefixes = ["wiki-知识库/"]
    cfg.knowledge_sink.wiki_gate_enabled = True
    cfg.knowledge_sink.wiki_gate_require_scripts = require_scripts
    cfg.knowledge_sink.wiki_d2_postflight_script = ""
    cfg.knowledge_sink.wiki_lint_script = ""
    return FlywheelRuntime(cfg)


# ---- the merge


def test_a_merge_adds_what_is_new_and_leaves_the_rest_of_the_page_alone():
    increment = (
        "## 字段语义表\n\n| status | varchar(32) | 订单状态 | PAID / CLOSED |\n\n"
        "## 坑与边界\n\n- 跨库关联只能在应用层做。\n"
    )

    merge = merge_domain_increment(ORDERS_PAGE, increment, template_sections=TABLE_SECTIONS, today="2026-11-27")

    assert (merge.ok, merge.has_additions) == (True, True)
    assert merge.merged == MERGED_ORDERS_PAGE
    assert (merge.added, merge.duplicates, merge.placeholders) == (2, 0, ["坑与边界"])
    assert merge.added_text == (
        "## 字段语义表\n\n| status | varchar(32) | 订单状态 | PAID / CLOSED |\n\n## 坑与边界\n\n- 跨库关联只能在应用层做。"
    )
    assert preserves_existing(ORDERS_PAGE, merge.merged) is True
    # Without a date the frontmatter is left alone, as the plan-stage dry check needs it.
    undated = merge_domain_increment(ORDERS_PAGE, increment, template_sections=TABLE_SECTIONS)
    assert undated.merged == MERGED_ORDERS_PAGE.replace("updated: 2026-11-27", "updated: 2026-11-20")


def test_a_page_or_an_increment_saved_with_crlf_merges_to_the_same_text():
    increment = "## 字段语义表\n\n| status | varchar(32) | 订单状态 | PAID / CLOSED |\n"

    lf = merge_domain_increment(ORDERS_PAGE, increment, template_sections=TABLE_SECTIONS)
    mixed = merge_domain_increment(
        ORDERS_PAGE.replace("\n", "\r\n"), increment.replace("\n", "\r\n"), template_sections=TABLE_SECTIONS
    )

    assert mixed.merged == lf.merged
    assert "\r" not in mixed.merged


def test_a_fenced_block_is_added_once_and_as_a_whole():
    increment = "## 坑与边界\n\n```sql\nselect * from orders where status = 'PAID';\n```\n"

    first = merge_domain_increment(ORDERS_PAGE, increment, template_sections=TABLE_SECTIONS)
    again = merge_domain_increment(first.merged, increment, template_sections=TABLE_SECTIONS)

    assert first.merged.endswith("## 坑与边界\n\n```sql\nselect * from orders where status = 'PAID';\n```\n")
    assert (first.added, first.placeholders) == (3, ["坑与边界"])
    assert (again.ok, again.has_additions, again.duplicates) == (True, False, 1)
    assert again.merged == first.merged  # nothing added: the page as it is


def test_a_table_the_merge_starts_carries_its_own_header():
    page = ORDERS_PAGE.replace("## 坑与边界", "## 枚举值\n\n无\n\n## 坑与边界")
    rows = "| status | PAID / CLOSED |\n| source | miniprogram / h5 |\n"

    headerless = merge_domain_increment(page, f"## 枚举值\n\n{rows}", template_sections=TABLE_SECTIONS)
    assert (headerless.ok, headerless.merged) == (False, "")
    assert headerless.problems == ["小节「枚举值」在目标页中没有表格，增量行需要先给表头行与分隔行"]

    started = merge_domain_increment(
        page, f"## 枚举值\n\n| 字段 | 取值 |\n| --- | --- |\n{rows}", template_sections=TABLE_SECTIONS
    )

    assert started.ok is True
    assert started.placeholders == ["枚举值"]
    assert (
        "## 枚举值\n\n| 字段 | 取值 |\n|---|---|\n| status | PAID / CLOSED |\n| source | miniprogram / h5 |\n\n## 坑与边界"
        in started.merged
    )


def test_a_section_the_page_lacks_takes_its_place_in_template_order():
    table = "## 字段语义表\n\n| 字段 | 类型 | 含义 | 备注 |\n| --- | --- | --- | --- |\n| id | bigint | 主键 | 自增 |\n\n"
    page = ORDERS_PAGE.replace(table, "")

    merge = merge_domain_increment(page, table, template_sections=TABLE_SECTIONS)

    assert merge.ok is True
    assert merge.merged == page.replace(
        "## 坑与边界", "## 字段语义表\n\n| 字段 | 类型 | 含义 | 备注 |\n|---|---|---|---|\n| id | bigint | 主键 | 自增 |\n\n## 坑与边界"
    )
    # The template decides which sections a page has: one it does not declare is refused, not invented.
    stranger = merge_domain_increment(page, "## 性能备注\n\n- 慢查询先看索引。\n", template_sections=TABLE_SECTIONS)
    assert (stranger.ok, stranger.problems) == (False, ["小节「性能备注」不在目标页中，也不是页面模板声明的小节"])


# One page, two environments, each with a parameter table of its own.
ENV_PAGE = textwrap.dedent(
    """\
    ---
    title: 迁移参数
    description: SIT 与 UAT 的迁移任务参数
    type: project-config
    project_id: alpha
    created: 2026-11-20
    updated: 2026-11-20
    status: active
    ---

    # 迁移参数

    ## SIT

    | 参数 | 值 |
    | --- | --- |
    | timeout | 30 |
    | host | sit-db |

    ## UAT

    | 参数 | 值 |
    | --- | --- |
    | retries | 3 |

    ## 坑与边界

    - 迁移窗口内停止写入。
    """
)
ENV_SECTIONS = ["SIT", "UAT", "坑与边界"]
ENV_TIMEOUT_ROW = "## UAT\n\n| timeout | 30 |\n"


def test_a_row_another_section_holds_is_still_added_to_its_own_section():
    """SIT and UAT share a parameter. A row SIT's table holds is not one UAT's table holds: reading the whole page as one table would swallow it for UAT."""
    merge = merge_domain_increment(ENV_PAGE, ENV_TIMEOUT_ROW, template_sections=ENV_SECTIONS, today="2026-11-28")
    again = merge_domain_increment(merge.merged, ENV_TIMEOUT_ROW, template_sections=ENV_SECTIONS, today="2026-11-28")

    assert (merge.ok, merge.has_additions, merge.duplicates) == (True, True, 0)
    assert merge.merged == ENV_PAGE.replace(
        "| retries | 3 |\n", "| retries | 3 |\n| timeout | 30 |\n"
    ).replace("updated: 2026-11-20", "updated: 2026-11-28")
    sit, uat = merge.merged.split("## SIT\n")[1].split("## UAT\n")
    assert sit.count("| timeout | 30 |") == 1 and uat.count("| timeout | 30 |") == 1
    # Merging the same increment again changes nothing: the row is one UAT now holds.
    assert (again.ok, again.has_additions, again.duplicates) == (True, False, 1)
    assert again.merged == merge.merged


@pytest.mark.parametrize(
    "held",
    [
        pytest.param("- 迁移前先停写入。\n", id="list-item"),
        pytest.param("迁移前先停写入。\n", id="paragraph"),
        pytest.param("```bash\nflyway migrate\n```\n", id="code-block"),
    ],
)
def test_a_line_another_section_holds_is_still_added_to_its_own_section(held: str):
    page = ENV_PAGE.replace(
        "## 坑与边界\n\n- 迁移窗口内停止写入。\n", f"## 坑与边界\n\n- 迁移窗口内停止写入。\n\n{held}"
    )
    increment = f"## UAT\n\n{held}"

    merge = merge_domain_increment(page, increment, template_sections=ENV_SECTIONS)
    again = merge_domain_increment(merge.merged, increment, template_sections=ENV_SECTIONS)

    assert (merge.ok, merge.has_additions, merge.duplicates) == (True, True, 0)
    assert held.strip() in merge.merged.split("## UAT\n")[1].split("## 坑与边界")[0]
    assert merge.merged.count(held.strip()) == 2  # once where it was, once in UAT
    assert (again.ok, again.has_additions, again.duplicates) == (True, False, 1)
    assert again.merged == merge.merged


def test_what_the_target_section_itself_holds_is_still_not_written_twice():
    row = merge_domain_increment(ENV_PAGE, "## SIT\n\n| timeout | 30 |\n", template_sections=ENV_SECTIONS)
    pitfall = merge_domain_increment(ENV_PAGE, "## 坑与边界\n\n- 迁移窗口内停止写入。\n", template_sections=ENV_SECTIONS)
    block_page = ENV_PAGE.replace(
        "## 坑与边界\n\n- 迁移窗口内停止写入。\n",
        "## 坑与边界\n\n- 迁移窗口内停止写入。\n\n```bash\nflyway migrate\n```\n",
    )
    block = merge_domain_increment(block_page, "## 坑与边界\n\n```bash\nflyway migrate\n```\n", template_sections=ENV_SECTIONS)

    for merge in (row, pitfall, block):
        assert (merge.ok, merge.has_additions, merge.added, merge.duplicates) == (True, False, 0, 1)


def test_a_row_is_read_against_the_table_it_joins():
    """A section with a request table and a response table: a field the request table holds is still news for the response table."""
    page = ENV_PAGE.replace(
        "## UAT\n\n| 参数 | 值 |\n| --- | --- |\n| retries | 3 |\n",
        "## UAT\n\n### 请求\n\n| 字段 | 类型 |\n| --- | --- |\n| id | string |\n\n"
        "### 响应\n\n| 字段 | 类型 |\n| --- | --- |\n| ok | boolean |\n",
    )
    increment = "## UAT\n\n| 字段 | 类型 |\n| --- | --- |\n| id | string |\n"

    merge = merge_domain_increment(page, increment, template_sections=ENV_SECTIONS)
    again = merge_domain_increment(merge.merged, increment, template_sections=ENV_SECTIONS)

    assert (merge.ok, merge.has_additions, merge.duplicates) == (True, True, 0)
    assert merge.merged.split("## UAT\n")[1].split("## 坑与边界")[0] == (
        "\n### 请求\n\n| 字段 | 类型 |\n| --- | --- |\n| id | string |\n\n"
        "### 响应\n\n| 字段 | 类型 |\n| --- | --- |\n| ok | boolean |\n| id | string |\n\n"
    )
    assert (again.ok, again.has_additions, again.duplicates) == (True, False, 1)
    assert again.merged == merge.merged


def test_preserves_existing_reads_every_meaningful_line_in_order():
    page = "---\ntitle: orders\nupdated: 2026-11-20\n---\n\n## 字段语义表\n\n| id | bigint |\n\n## 坑与边界\n\n无\n"

    # A new date, a placeholder that gave way and blank lines are not a loss.
    grown = page.replace("updated: 2026-11-20", "updated: 2026-11-27").replace("\n\n无", "\n\n- 一条坑")
    assert preserves_existing(page, grown) is True
    # A line of the page gone is, and so is a line moved out of its order.
    assert preserves_existing(page, "| id | bigint |\n") is False
    assert preserves_existing(page, page.replace("| id | bigint |", "| status | varchar |")) is False
    assert preserves_existing(page, page.replace("| id | bigint |", "| status | varchar |").replace("无", "| id | bigint |")) is False


# ---- the write path


def test_an_increment_is_merged_into_the_domain_page_and_loses_no_line(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    increment = (
        "## 字段语义表\n\n| status | varchar(32) | 订单状态 | PAID / CLOSED |\n"
        "| customer_id | bigint | 下单客户 | 关联 customers.customer_id |\n\n"
        "## 坑与边界\n\n- 跨库关联只能在应用层做。\n"
    )

    result = rt.knowledge.write_document_verified(ORDERS_REL, increment, action="update", merge_increment=True)

    after = target.read_bytes()
    assert b"\r" not in after  # LF whatever line endings the page or the increment used
    assert (result["verified"], result["wiki_gate"]["ok"]) == (True, True)
    assert result["merge"] == {
        "incremental": True,
        "existing_lines": len(ORDERS_PAGE.splitlines()),
        "added_lines": 3,
        "duplicates_removed": 0,
        "placeholders_replaced": ["坑与边界"],
        "existing_preserved": True,
    }
    lines = after.decode("utf-8").splitlines()
    position = 0
    for line in ORDERS_PAGE.splitlines():  # every line the page held, in its order
        if not line.startswith("updated:") and line.strip() != "无":  # the date moved; the placeholder gave way
            position = lines.index(line, position) + 1
    id_row = lines.index("| id | bigint | 主键 | 自增 |")
    assert lines[id_row + 1 : id_row + 3] == [
        "| status | varchar(32) | 订单状态 | PAID / CLOSED |",
        "| customer_id | bigint | 下单客户 | 关联 customers.customer_id |",
    ]
    assert lines[-3:] == ["## 坑与边界", "", "- 跨库关联只能在应用层做。"]


def test_a_page_saved_with_crlf_is_written_back_with_lf_and_the_same_lines(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    target.write_bytes(ORDERS_PAGE.replace("\n", "\r\n").encode("utf-8"))
    increment = "## 字段语义表\n\n| status | varchar(32) | 订单状态 | PAID / CLOSED |\n"

    rt.knowledge.write_document_verified(
        ORDERS_REL, increment.replace("\n", "\r\n"), action="update", merge_increment=True
    )

    after = target.read_bytes()
    assert b"\r" not in after
    assert after.decode("utf-8") == merge_domain_increment(
        ORDERS_PAGE, increment, template_sections=TABLE_SECTIONS, today=shanghai_today_ymd()
    ).merged


def test_rows_the_page_already_holds_are_not_written_twice(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL

    result = rt.knowledge.write_document_verified(
        ORDERS_REL,
        "## 字段语义表\n\n| id | bigint | 主键 | 自增 |\n| status | varchar(32) | 订单状态 | PAID / CLOSED |\n",
        action="update",
        merge_increment=True,
    )

    text = target.read_text(encoding="utf-8")
    assert text.count("| id | bigint | 主键 | 自增 |") == 1
    assert text.count("| status | varchar(32) | 订单状态 | PAID / CLOSED |") == 1
    assert (result["merge"]["duplicates_removed"], result["merge"]["added_lines"]) == (1, 1)


def test_a_row_another_section_of_the_page_holds_is_written_into_its_own_section(tmp_path: Path):
    """The 关联键 table already names customer_id. The 字段语义表 does not: its increment is real news there, and must be written, not read as a duplicate of the other table's row."""
    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    keys_table = (
        "## 关联键\n\n| 字段 | 类型 | 含义 | 备注 |\n| --- | --- | --- | --- |\n"
        "| customer_id | bigint | 下单客户 | 关联 customers.customer_id |\n\n"
    )
    _put(target, ORDERS_PAGE.replace("## 坑与边界", keys_table + "## 坑与边界"))

    result = rt.knowledge.write_document_verified(
        ORDERS_REL, CUSTOMER_ROW, action="update", merge_increment=True
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    assert result["verified"] is True
    assert (result["merge"]["added_lines"], result["merge"]["duplicates_removed"]) == (1, 0)
    semantics = [line for line in lines[lines.index("## 字段语义表") : lines.index("## 关联键")] if line.strip()]
    assert semantics[-2:] == [
        "| id | bigint | 主键 | 自增 |",
        "| customer_id | bigint | 下单客户 | 关联 customers.customer_id |",
    ]
    keys = lines[lines.index("## 关联键") : lines.index("## 坑与边界")]
    assert keys.count("| customer_id | bigint | 下单客户 | 关联 customers.customer_id |") == 1
    replay = rt.knowledge.write_document_verified(
        ORDERS_REL, CUSTOMER_ROW, action="update", merge_increment=True
    )
    assert (replay["status"], replay["written"]) == (UPDATE_NO_NEW_CONTENT, False)
    assert target.read_text(encoding="utf-8").splitlines() == lines


def test_an_increment_the_page_already_holds_writes_nothing(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    before = target.read_bytes()

    result = rt.knowledge.write_document_verified(
        ORDERS_REL, "## 字段语义表\n\n| id | bigint | 主键 | 自增 |\n", action="update", merge_increment=True
    )

    assert result == {
        "relative_path": ORDERS_REL,
        "action": "update",
        "status": UPDATE_NO_NEW_CONTENT,
        "skipped": True,
        "written": False,
        "reason": "增量内容均已存在于目标页，没有新增实质内容",
    }
    assert target.read_bytes() == before


def test_an_increment_only_enters_an_existing_page_of_a_declared_domain(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    note_rel = "wiki-知识库/projects-项目/demo/实战笔记/2026-11-23-缓存雪崩.md"
    note = _write(rt.knowledge.root, note_rel, _lite_note("缓存雪崩快速定位"))
    before = note.read_bytes()

    with pytest.raises(KnowledgeWriteError) as exc:
        rt.knowledge.write_document_verified(
            PAYMENTS_REL, "## 字段语义表\n\n| payment_id | bigint | 支付主键 | 自增 |\n", action="update", merge_increment=True
        )
    assert "Domain update target is missing" in str(exc.value)
    assert not (rt.knowledge.root / PAYMENTS_REL).exists()

    with pytest.raises(KnowledgeWriteError) as exc:
        rt.knowledge.write_document_verified(note_rel, "## 结论\n\n- 第二条结论。\n", action="update", merge_increment=True)
    assert "not a project-domain page the Wiki contract declares" in str(exc.value)
    assert note.read_bytes() == before


@pytest.mark.parametrize(
    "defect, message",
    [
        pytest.param("a line of the page lost", "Domain merge would not preserve the existing page:", id="page-lost-a-line"),
        pytest.param("an increment that no longer fits", "Domain increment cannot be merged without loss into", id="increment-does-not-fit"),
    ],
)
def test_the_sink_merges_again_and_stops_when_the_result_cannot_be_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str, message: str
):
    """The route gate merged the increment when the plan was made. The sink merges again against the page as it is on disk and does not trust the result: a page that lost a line, or an increment that no longer fits, raises before anything is written."""
    from dataclasses import replace

    from ai_flywheel.adapters import knowledge as knowledge_module
    from ai_flywheel.adapters.wiki_gate import MergeResult

    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    before = target.read_bytes()
    real_merge = knowledge_module.merge_domain_increment

    def merge_with_the_defect(existing: str, increment: str, **kwargs: object) -> MergeResult:
        merge = real_merge(existing, increment, **kwargs)
        if defect == "a line of the page lost":
            return replace(merge, merged=merge.merged.replace("| id | bigint | 主键 | 自增 |\n", ""))
        return replace(merge, problems=["小节「字段语义表」在目标页中没有表格，增量行需要先给表头行与分隔行"])

    monkeypatch.setattr(knowledge_module, "merge_domain_increment", merge_with_the_defect)

    with pytest.raises(KnowledgeWriteError) as exc:
        rt.knowledge.write_document_verified(
            ORDERS_REL,
            "## 字段语义表\n\n| status | varchar(32) | 订单状态 | PAID / CLOSED |\n",
            action="update",
            merge_increment=True,
        )

    assert str(exc.value).startswith(message)
    assert target.read_bytes() == before


# ---- a page that changes under the merge


STATUS_ROW = "## 字段语义表\n\n| status | varchar(32) | 订单状态 | PAID / CLOSED |\n"
CUSTOMER_ROW = "## 字段语义表\n\n| customer_id | bigint | 下单客户 | 关联 customers.customer_id |\n"
# Somebody else's edit of the same page: a row the table gains and the first pitfall.
EXTERNAL_EDIT = (
    "## 字段语义表\n\n| channel | varchar(16) | 下单渠道 | mini / h5 |\n\n"
    "## 坑与边界\n\n- 分库键只认 customer_id。\n"
)


def _page_after(text: str, increment: str) -> str:
    return merge_domain_increment(text, increment, template_sections=TABLE_SECTIONS, today=shanghai_today_ymd()).merged


def test_a_page_edited_after_the_merge_was_computed_is_merged_again_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The merge is computed, and before the page is replaced somebody else edits it (a person, an editor that rewrites files, another process). The write must not put a merge of the older page over that edit: it merges again into the page that is there now, so the edit and the increment both land."""
    import hashlib

    from ai_flywheel.adapters import knowledge as knowledge_module

    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    real_merge = knowledge_module.merge_domain_increment
    merged_on: list[str] = []
    gated: list[str] = []
    real_validate = rt.knowledge.wiki_gate.validate_document

    def merge_while_someone_edits(existing: str, increment: str, **kwargs: object):
        merge = real_merge(existing, increment, **kwargs)
        if not merged_on:  # once: the edit lands after the first merge and before its write
            merged_on.append(existing)
            target.write_bytes(_page_after(ORDERS_PAGE, EXTERNAL_EDIT).encode("utf-8"))
        return merge

    def validate(rel: str, content: str, *args: object, **kwargs: object):
        gated.append(content)
        return real_validate(rel, content, *args, **kwargs)

    monkeypatch.setattr(knowledge_module, "merge_domain_increment", merge_while_someone_edits)
    monkeypatch.setattr(rt.knowledge.wiki_gate, "validate_document", validate)

    result = rt.knowledge.write_document_verified(ORDERS_REL, STATUS_ROW, action="update", merge_increment=True)

    edited = _page_after(ORDERS_PAGE, EXTERNAL_EDIT)
    edited_bytes = edited.encode("utf-8")
    final = target.read_text(encoding="utf-8")
    # The first merge was computed on the page as it was before the edit.
    assert merged_on == [ORDERS_PAGE]
    assert len(gated) == 2 and "| channel | varchar(16) | 下单渠道 | mini / h5 |" in gated[-1]
    assert (result["verified"], result["merge"]["remerged"]) == (True, 1)
    assert (result["merge"]["added_lines"], result["merge"]["duplicates_removed"]) == (1, 0)
    # The write was made against the edited page, and the page holds the edit and the increment.
    assert result["preimage_sha256"] == hashlib.sha256(edited_bytes).hexdigest()
    assert final.count("| channel | varchar(16) | 下单渠道 | mini / h5 |") == 1
    assert "- 分库键只认 customer_id。" in final
    assert final.count("| status | varchar(32) | 订单状态 | PAID / CLOSED |") == 1
    assert preserves_existing(edited, final) is True
    assert preserves_existing(ORDERS_PAGE, final) is True


def test_a_page_that_keeps_changing_under_the_merge_is_left_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Every merge is overtaken by another edit. None of them is written: the page ends as the last edit left it, and the write fails."""
    from ai_flywheel.adapters import knowledge as knowledge_module
    from ai_flywheel.adapters.knowledge import MERGE_ATTEMPTS

    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    real_merge = knowledge_module.merge_domain_increment
    merged_on: list[str] = []

    def merge_while_someone_keeps_editing(existing: str, increment: str, **kwargs: object):
        merge = real_merge(existing, increment, **kwargs)
        merged_on.append(existing)
        edited = existing.rstrip("\n") + f"\n- 外部备注 {len(merged_on)}：重建索引要避开高峰。\n"
        target.write_bytes(edited.encode("utf-8"))
        return merge

    monkeypatch.setattr(knowledge_module, "merge_domain_increment", merge_while_someone_keeps_editing)

    with pytest.raises(KnowledgeWriteError) as exc:
        rt.knowledge.write_document_verified(ORDERS_REL, STATUS_ROW, action="update", merge_increment=True)

    assert "kept changing under the merge" in str(exc.value)
    assert len(merged_on) == MERGE_ATTEMPTS
    final = target.read_text(encoding="utf-8")
    assert final == merged_on[-1].rstrip("\n") + "\n- 外部备注 3：重建索引要避开高峰。\n"
    assert [final.count(f"外部备注 {number}：") for number in (1, 2, 3)] == [1, 1, 1]
    assert "| status | varchar(32) | 订单状态 | PAID / CLOSED |" not in final  # no stale merge reached the page


def test_a_remerge_whose_result_cannot_be_trusted_leaves_the_edited_page_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The gates run again on the page as it is now. A second merge that cannot be trusted fails closed: the other person's edit stays, the increment is not written."""
    from dataclasses import replace

    from ai_flywheel.adapters import knowledge as knowledge_module

    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    real_merge = knowledge_module.merge_domain_increment
    calls: list[str] = []

    def merge_then_an_edit_then_a_defect(existing: str, increment: str, **kwargs: object):
        merge = real_merge(existing, increment, **kwargs)
        calls.append(existing)
        if len(calls) == 1:
            target.write_bytes(_page_after(ORDERS_PAGE, EXTERNAL_EDIT).encode("utf-8"))
            return merge
        return replace(merge, merged=merge.merged.replace("| id | bigint | 主键 | 自增 |\n", ""))

    monkeypatch.setattr(knowledge_module, "merge_domain_increment", merge_then_an_edit_then_a_defect)

    with pytest.raises(KnowledgeWriteError) as exc:
        rt.knowledge.write_document_verified(ORDERS_REL, STATUS_ROW, action="update", merge_increment=True)

    assert str(exc.value).startswith("Domain merge would not preserve the existing page:")
    assert len(calls) == 2  # the second merge read the edited page
    assert target.read_text(encoding="utf-8") == _page_after(ORDERS_PAGE, EXTERNAL_EDIT)
    assert "| status | varchar(32) | 订单状态 | PAID / CLOSED |" not in target.read_text(encoding="utf-8")


def test_two_increments_for_the_same_page_written_at_once_both_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two writes merge the same page from two threads. The first has computed its merge and not yet written; the second starts. Each must merge into the page the other left, or the later write silently drops the earlier one."""
    import threading

    from ai_flywheel.adapters import knowledge as knowledge_module

    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    real_merge = knowledge_module.merge_domain_increment
    first_computed = threading.Event()
    second_computing = threading.Event()
    release_first = threading.Event()

    def gated_merge(existing: str, increment: str, **kwargs: object):
        merge = real_merge(existing, increment, **kwargs)
        if "customer_id" in increment:
            first_computed.set()
            assert release_first.wait(10)
        else:
            second_computing.set()
        return merge

    monkeypatch.setattr(knowledge_module, "merge_domain_increment", gated_merge)
    outcomes: dict[str, object] = {}

    def write(name: str, increment: str) -> None:
        try:
            outcomes[name] = rt.knowledge.write_document_verified(
                ORDERS_REL, increment, action="update", merge_increment=True, sync_fts=False
            )["merge"]["added_lines"]
        except Exception as exc:  # reported by the assertions below
            outcomes[name] = exc

    first = threading.Thread(target=write, args=("first", CUSTOMER_ROW))
    second = threading.Thread(target=write, args=("second", STATUS_ROW))
    first.start()
    assert first_computed.wait(10)
    second.start()
    second_computing.wait(0.5)  # without the page lock the second merge runs here, on the page as it was
    release_first.set()
    first.join(30)
    second.join(30)

    assert outcomes == {"first": 1, "second": 1}
    final = target.read_text(encoding="utf-8")
    assert final.count("| customer_id | bigint | 下单客户 | 关联 customers.customer_id |") == 1
    assert final.count("| status | varchar(32) | 订单状态 | PAID / CLOSED |") == 1
    assert preserves_existing(ORDERS_PAGE, final) is True


def test_d2_postflight_failure_rolls_back_a_domain_increment_and_a_new_page(tmp_path: Path):
    from ai_flywheel.adapters.wiki_gate import resolve_bash_executable

    bash = resolve_bash_executable("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("bash is required for the D2 postflight rollback test")
    rt = _deposit_runtime(tmp_path, require_scripts=True)
    fake = tmp_path / "fake-d2.sh"
    fake.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
    rt.cfg.knowledge_sink.wiki_d2_postflight_script = str(fake)
    rt.cfg.knowledge_sink.wiki_bash = bash
    orders = rt.knowledge.root / ORDERS_REL
    payments = rt.knowledge.root / PAYMENTS_REL
    # The page as a Windows editor may have saved it: the rollback must bring back these very bytes.
    preimage = ORDERS_PAGE.replace("\n", "\r\n").encode("utf-8")
    orders.write_bytes(preimage)

    with pytest.raises(WikiGateError) as exc:
        rt.knowledge.write_document_verified(
            ORDERS_REL,
            "## 字段语义表\n\n| status | varchar(32) | 订单状态 | PAID / CLOSED |\n",
            action="update",
            merge_increment=True,
        )
    assert "D2_BLOCKED" in str(exc.value)
    assert orders.read_bytes() == preimage

    with pytest.raises(WikiGateError) as exc:
        rt.knowledge.write_document_verified(PAYMENTS_REL, PAYMENTS_PAGE, action="create")
    assert "D2_BLOCKED" in str(exc.value)
    assert not payments.exists()


# ---- credentials and the private page


def test_a_credential_kind_is_refused_on_any_domain_page(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / ORDERS_REL
    before = target.read_bytes()

    with pytest.raises(SecretLeakError) as exc:
        rt.knowledge.write_document_verified(
            ORDERS_REL, f"## 坑与边界\n\n- 旧文档里贴过 {PRIVATE_KEY_HEADER}，已作废。\n", action="update", merge_increment=True
        )

    assert "Private Key Block" in str(exc.value)
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("- 测试库 password: Hk9-Qp31x，只在本地使用。", id="assignment"),
        pytest.param("- 数据库密码：Hk9-Qp31x，只在本地使用。", id="chinese-assignment"),
    ],
)
def test_a_password_in_a_configuration_increment_never_reaches_the_page(tmp_path: Path, line: str):
    """These two shapes are only masked on the ordinary write path. A configuration page is read
    by the strict privacy check, so the same increment is refused before anything is written."""
    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / DATABASE_REL
    before = target.read_bytes()

    with pytest.raises(SecretLeakError) as exc:
        rt.knowledge.write_document_verified(
            DATABASE_REL, f"## 使用规则\n\n{line}\n", action="update", merge_increment=True
        )

    assert "Credential Assignment" in str(exc.value)
    assert "Hk9-Qp31x" not in str(exc.value)  # the refusal never echoes the credential
    assert target.read_bytes() == before


def test_a_clean_configuration_increment_is_merged_and_the_page_stays_private(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    target = rt.knowledge.root / DATABASE_REL

    result = rt.knowledge.write_document_verified(
        DATABASE_REL, "## 使用规则\n\n- 跨库关联只能在应用层做。\n", action="update", merge_increment=True
    )

    text = target.read_text(encoding="utf-8")
    assert result["merge"]["added_lines"] == 1
    assert text.endswith("- 跨库关联只能在应用层做。\n")
    for declared in ("sensitivity: private_sensitive", "access_scope: maintainers", "default_load: false"):
        assert declared in text


def test_the_gate_reads_a_domain_page_as_it_is_even_when_it_is_private(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate

    assert gate.read_domain_page(DATABASE_REL) == DATABASE_PAGE
    assert gate.read_domain_page(ORDERS_REL) == ORDERS_PAGE
    assert gate.read_domain_page(PAYMENTS_REL) is None  # the page does not exist
    assert gate.read_domain_page("") is None
    assert gate.read_domain_page("../outside.md") is None  # never a file outside the Wiki


def test_a_domain_page_is_searchable_and_a_private_one_stays_out_of_the_index(tmp_path: Path):
    rt = _deposit_runtime(tmp_path)
    rt.knowledge.sync_fts_index()

    def hits(query: str) -> set[str]:
        return {hit["doc_path"] for hit in rt.db.search_knowledge_fts(query, limit=10)}

    assert ORDERS_REL in hits("订单主表")
    assert DATABASE_REL not in hits("数据源")

    # ...and an increment changes neither: what it adds to a private page is not indexed either.
    rt.knowledge.write_document_verified(
        ORDERS_REL, "## 坑与边界\n\n- 跨库关联只能在应用层做。\n", action="update", merge_increment=True
    )
    rt.knowledge.write_document_verified(
        DATABASE_REL, "## 使用规则\n\n- 连接前先确认逻辑库所在分片。\n", action="update", merge_increment=True
    )

    assert ORDERS_REL in hits("跨库关联")
    assert DATABASE_REL not in hits("所在分片")


# ---- the validator


def test_a_domain_page_follows_its_domain_in_placement_kind_and_load_policy(tmp_path: Path):
    gate = _deposit_runtime(tmp_path).knowledge.wiki_gate

    def issues(relative_path: str, text: str) -> dict[str, str]:
        return {issue["code"]: issue["field"] for issue in gate.validate_document(relative_path, text).issues}

    assert issues(ORDERS_REL, ORDERS_PAGE) == {}
    assert issues(DATABASE_REL, DATABASE_PAGE) == {}

    # Placed under another domain's directory
    assert issues(f"{PROJECTS}/{ALPHA_DIR}/系统架构/orders.md", ORDERS_PAGE)["DOMAIN_PLACEMENT"] == "type"
    # A knowledge kind, or a load policy, other than the domain's templates declare
    assert issues(ORDERS_REL, ORDERS_PAGE.replace("knowledge_kind: ddl", "knowledge_kind: algorithm"))["DOMAIN_KNOWLEDGE_KIND"] == "knowledge_kind"
    assert issues(ORDERS_REL, ORDERS_PAGE.replace("load_policy: index_then_on_demand", "load_policy: on_demand"))["DOMAIN_LOAD_POLICY"] == "load_policy"
    # A configuration page is loaded only on explicit request
    assert issues(DATABASE_REL, DATABASE_PAGE.replace("default_load: false", "default_load: true"))["DOMAIN_DEFAULT_LOAD"] == "default_load"


def test_a_wiki_that_declares_no_deposit_routing_has_no_domain_rule(tmp_path: Path):
    gate = _make_runtime(tmp_path / "plain").knowledge.wiki_gate

    result = gate.validate_document(ORDERS_REL, ORDERS_PAGE)

    assert not [issue for issue in result.issues if issue["code"].startswith("DOMAIN_")]
