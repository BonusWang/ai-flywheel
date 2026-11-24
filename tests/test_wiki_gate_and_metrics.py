from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import pytest
import yaml

from ai_flywheel.adapters.wiki_gate import WikiGateError, split_frontmatter
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import (
    OutcomeStatus,
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
        f"---\ntitle: {dir_name} 索引\ntype: project-practice-index\nproject_id: {project_id}\n"
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
