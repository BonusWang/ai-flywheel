"""Wave 2A（拆分）回归测试：模块边界、运行时配置生效与执行器选择。

覆盖：
- W2.0 时间与脱敏：`time_utils` 是日期与脱敏的唯一来源，UTC 主机不误判未来日期。
- W2.1 执行器：运行模式字符串只有一个来源（`resolve_runner_mode`），且与实际执行器一致。
- W2.2 Wiki 门禁：运行时配置改了知识落点，门禁立刻按新配置判断；项目分区根由契约决定。
"""
from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path

from ai_flywheel.adapters.runners import (
    CliSubprocessRunner,
    DryRunHeuristicRunner,
    OpenAICompatibleRunner,
    build_runner,
    resolve_runner_mode,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.privacy import redact_secrets
from ai_flywheel.core.time_utils import (
    compute_valid_until,
    shanghai_iso_week_key,
    shanghai_today_ymd,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.fs_store import ImmutableFileStore

REPO_ROOT = Path(__file__).resolve().parent.parent

# A Wiki whose projects partition root is NOT the historical default: the adapter must
# read it off the contract instead of hardcoding a path.
SCHEMA = textwrap.dedent(
    """\
    ---
    title: Test Wiki Schema
    description: Schema whose projects partition lives outside the default root
    type: protocol
    created: 2026-01-01
    updated: 2026-01-01
    status: active
    load_policy: always
    core_contract:
      version: "2.1"
      core_files:
        - .wiki-schema.md
      required_fields: [title, description, type, created, updated]
    partition_contract:
      version: "1.0"
      partitions:
        projects:
          root: 知识库/项目分区
    content_contract:
      schema_id: wave2a-content-contract
      version: "1.4.1"
      legacy_type_aliases: {}
      scope_profiles:
        formal_active:
          partition_ids: [projects]
      page_types:
        project-practice-note:
          partition: projects
          required_fields: [title, description, type, project_id, record_id, event_date, load_policy, default_load, created, updated, status]
          path_pattern: "^[^/]+/实战笔记/[^/]+\\\\.md$"
          allowed_status: [active]
    ---
    """
)

NOTE = """---
title: 缓存雪崩定位
description: 命中率骤降的排查路径
type: project-practice-note
status: active
created: 2026-01-01
updated: 2026-01-01
---

# 缓存雪崩定位

命中率骤降时先看窗口与过期时间。
"""


def _wiki_runtime(tmp_path: Path) -> FlywheelRuntime:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "知识库" / "项目分区" / "alpha" / "实战笔记").mkdir(parents=True)
    (wiki_root / ".wiki-schema.md").write_text(SCHEMA, encoding="utf-8")
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(wiki_root)
    cfg.knowledge_sink.allowed_write_prefixes = ["知识库/"]
    cfg.knowledge_sink.wiki_gate_require_scripts = False
    return FlywheelRuntime(cfg)


# ---------------- W2.0 时间与脱敏 ----------------


def test_time_utils_is_the_single_source_of_the_product_dates():
    # 2026-01-01 16:30 UTC is already 2026-01-02 in Shanghai.
    utc = datetime(2026, 1, 1, 16, 30, tzinfo=timezone.utc)
    assert shanghai_today_ymd(utc) == "2026-01-02"
    assert compute_valid_until("evolving", "2026-01-02") == "2026-04-02"
    assert shanghai_iso_week_key(utc) == "2026-W01"


def test_the_privacy_gate_still_refuses_a_credential_but_masks_a_mention():
    from ai_flywheel.core.privacy import SecretLeakError, sanitize_and_check_secrets

    credential = "sk-" + "a" * 24
    try:
        sanitize_and_check_secrets(f"key {credential}", context="test")
    except SecretLeakError as exc:
        assert "API Key" in str(exc)
    else:  # pragma: no cover - the gate must refuse this
        raise AssertionError("a credential must be refused")

    # A mere mention of an assignment is masked, not refused.
    assert "hunter2" not in redact_secrets("password: hunter2")
    assert redact_secrets("password: hunter2") == "password: [REDACTED]"


# ---------------- W2.1 执行器选择 ----------------


def test_resolve_runner_mode_matches_build_runner(tmp_path: Path):
    cfg = FlywheelConfig(base_dir=tmp_path)
    fs_store = ImmutableFileStore(tmp_path / "receipts")

    # dry_run is the default and the fallback for every incomplete configuration.
    assert resolve_runner_mode(cfg) == "dry_run"
    assert isinstance(build_runner(cfg, fs_store), DryRunHeuristicRunner)

    # api
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://gateway.invalid"
    cfg.runner.api.model = "some-model"
    assert resolve_runner_mode(cfg) == "api:some-model"
    assert isinstance(build_runner(cfg, fs_store), OpenAICompatibleRunner)

    # cli
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "echo"
    assert resolve_runner_mode(cfg) == "cli"
    assert isinstance(build_runner(cfg, fs_store), CliSubprocessRunner)

    # A closed valve turns every mode back into the local planner.
    cfg.safety_valves.cli_execution_enabled = False
    assert resolve_runner_mode(cfg) == "dry_run"
    assert isinstance(build_runner(cfg, fs_store), DryRunHeuristicRunner)

    # An api mode without an endpoint or a model is not an api runner either.
    cfg.safety_valves.cli_execution_enabled = True
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = ""
    assert resolve_runner_mode(cfg) == "dry_run"
    assert isinstance(build_runner(cfg, fs_store), DryRunHeuristicRunner)


# ---------------- W2.2 Wiki 门禁 ----------------


def test_the_gate_follows_a_runtime_knowledge_sink_update(tmp_path: Path):
    rt = _wiki_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate

    assert gate.applicable is True
    assert gate.sink is rt.cfg.knowledge_sink

    rt.update_runtime_config({"knowledge_sink": {"wiki_gate_enabled": False}})

    # The gate reads the current configuration, not the one it was built with.
    assert gate.sink is rt.cfg.knowledge_sink
    assert gate.sink.wiki_gate_enabled is False
    assert gate.applicable is False


def test_the_knowledge_adapter_reads_the_project_partition_root_from_the_contract(tmp_path: Path):
    rt = _wiki_runtime(tmp_path)
    note = rt.knowledge.root / "知识库" / "项目分区" / "alpha" / "实战笔记" / "2026-01-01-缓存雪崩定位.md"
    note.write_text(NOTE, encoding="utf-8")

    rt.knowledge.sync_fts_index()

    hits = rt.db.search_knowledge_fts("缓存雪崩", limit=5)
    matched = [hit for hit in hits if hit["doc_path"].endswith(note.name)]
    assert matched, f"the note under the declared partition must be indexed: {hits}"
    # The project id comes from the path, resolved through the contract's partition root.
    assert matched[0]["project_id"] == "alpha"


def test_the_gate_judges_dates_against_the_shanghai_day(monkeypatch, tmp_path: Path):
    """A host that is still on the previous day must not make today's card look futuristic."""
    rt = _wiki_runtime(tmp_path)
    gate = rt.knowledge.wiki_gate
    # The Shanghai day is what the gate compares against; the host date may be a day behind.
    monkeypatch.setattr(
        "ai_flywheel.adapters.wiki.validator.shanghai_today_ymd", lambda: "2026-03-10"
    )

    def _note(created: str) -> str:
        return (
            "---\n"
            "title: 日期检查\n"
            "description: 今天的卡片\n"
            "type: project-practice-note\n"
            "project_id: alpha\n"
            "record_id: alpha-note\n"
            "event_date: " + created + "\n"
            "load_policy: on_demand\n"
            "default_load: false\n"
            f"created: {created}\n"
            f"updated: {created}\n"
            "status: active\n"
            "---\n\n"
            "# 日期检查\n"
        )

    rel = "知识库/项目分区/alpha/实战笔记/2026-03-10-note.md"
    today_codes = [issue["code"] for issue in gate.validate_document(rel, _note("2026-03-10")).issues]
    assert "DATE_IN_FUTURE" not in today_codes

    tomorrow_codes = [issue["code"] for issue in gate.validate_document(rel, _note("2026-03-11")).issues]
    assert "DATE_IN_FUTURE" in tomorrow_codes
