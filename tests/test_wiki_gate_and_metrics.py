from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from ai_flywheel.adapters.wiki_gate import WikiGateError
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import (
    OutcomeStatus,
    PreflightRequest,
    RecordOutcomeRequest,
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
    bash = shutil.which("bash")
    if not bash:
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
