"""Wave 4 架构不变量与一次性验收闸门（W4.2）。

六类不变量，每一类都写成一条会失败的测试，而不是一条注释：

1. **公开仓卫生**：非忽略文件里没有个人路径 / 身份 / 凭据，`.py` 没有 UTF-8 BOM，
   没有 `_unused_` 方法、没有 `if False:` 死分支、`backend/` 没有生产用 hold/park 钩子，
   CHANGELOG 在一个发布小节内没有重复标题。
2. **提示词骨架**：P03 的两个修复骨架必须是合法 JSON，且带齐每个必填键
   （带路由的变体还要带 `wiki_route`）。
3. **跨层契约**：版本号四处一致、示例配置与运行器默认值一致、前端状态联合与后端枚举一致、
   前端标签覆盖全部节点与信号种类、飞书卡片与后端对"敏感情项"的判定一致。
4. **真实 serve 角色**：以 `process_role="serve"` 起服务，`/health`、核心索引卡、
   `flywheel-recovery` 线程都要真的活着，退出时不报错。
5. **Windows 契约**：反斜杠工作区路径要切干净，写出的文档必须是 LF。
6. **流水线副作用与缓存**：被拒绝的批次不留下错题与分歧，不可解析的模型回复不写缓存。

所有样例数据都是虚构的。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import shutil
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_flywheel import __version__
from ai_flywheel.adapters.runners import (
    P03_ITEM_REQUIRED_KEYS,
    P03_REPAIR_SCHEMA_HINT,
    P03_REPAIR_SCHEMA_HINT_WITH_ROUTE,
    OpenAICompatibleRunner,
)
from ai_flywheel.adapters.wiki import _workspace_segments
from ai_flywheel.app import create_app
from ai_flywheel.config import ApiRunnerConfig, FlywheelConfig
from ai_flywheel.core.feishu_card import is_sensitive
from ai_flywheel.core.orchestrator import NODE_TEMPLATES
from ai_flywheel.models import (
    IngressChannel,
    QueueStatus,
    RuleStatus,
    RunStatus,
    SignalKind,
    is_sensitive_write,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.db import FlywheelDB

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"


# ---------------------------------------------------------------- helpers


def _load_verify_repo() -> Any:
    """The verification script is the single implementation of the public-repo rules."""
    spec = importlib.util.spec_from_file_location(
        "flywheel_verify_repo", REPO_ROOT / "scripts" / "verify_repo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The module has to be in `sys.modules` before it runs: `@dataclass` looks its own
    # module up by name while it is being created.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify_repo = _load_verify_repo()


def _join(*parts: str) -> str:
    """Join path fragments, so this file does not contain the path it is testing for."""
    return "".join(parts)


def _runtime(tmp_path: Path, *, approval_mode: str = "always") -> FlywheelRuntime:
    """A runtime with the real profile copied in, so the publication layer has a site."""
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills)]
    cfg.workflow.approval_mode = approval_mode
    cfg.safety_valves.auto_approve_writes = approval_mode == "auto"
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    return FlywheelRuntime(cfg)


def _register(rt: FlywheelRuntime, session_id: str = "s1") -> None:
    from ai_flywheel.models import RegisterSessionRequest

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


def _review() -> dict[str, Any]:
    return {
        "findings": [
            {
                "session_key": "codex:s1",
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
        ],
        "mistakes": [
            {
                "root_cause_key": "missing-null-guard",
                "severity_grade": "yellow_caught",
                "title": "参数校验缺失",
                "phenomenon": "入参为空时报错",
                "truth": "先校验入参再调用",
                "thinking_root_cause": "沿用了未经验证的假设",
                "caught_by": "self",
                "session_key": "codex:s1",
            }
        ],
        "dissents": [
            {
                "target_type": "rule",
                "target_id": "rule-cache-check",
                "counter_claim": "只读本地内存缓存不适用该失效键检查",
                "condition_scope": "project=demo",
                "evidence_pointer": "codex:s1",
                "session_key": "codex:s1",
            }
        ],
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

    def __init__(
        self,
        review: dict[str, Any] | None = None,
        planned_writes: list[Any] | None = None,
        *,
        p03_raw: str | None = None,
    ) -> None:
        self.review = review if review is not None else _review()
        self.plan_json = "" if planned_writes is None else json.dumps(
            {"planned_writes": planned_writes}, ensure_ascii=False
        )
        self.p03_raw = p03_raw
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("You are the P02 Review Worker"):
            return json.dumps(self.review, ensure_ascii=False)
        if self.p03_raw is not None:
            return self.p03_raw
        return self.plan_json


class _GarbageModel:
    """Every reply is unusable: not a JSON object at all."""

    def __call__(self, prompt: str, system_prompt: str) -> str:
        return "这次没有返回结构化内容，只有一段散文。"


# ---------------------------------------------------------------- 1. 公开仓卫生


def test_the_public_tree_carries_no_personal_identifier_path_or_credential():
    """非忽略文件里不得出现个人路径、个人身份或凭据。"""
    violations = verify_repo.hygiene_violations()
    assert violations == [], "\n".join(v.render() for v in violations)


def test_the_hygiene_guard_actually_catches_a_leak():
    """卫生闸门必须真的会响：一个人 home 目录要被抓到，占位符要被放过。"""
    real_home = _join("C:", "\\", "Users", "\\", "realperson", "\\notes")
    placeholder_home = _join("C:", "\\", "Users", "\\", "example", "\\notes")
    mac_home = _join("/Users/", "realperson")

    def _matches(text: str) -> bool:
        return any(pattern.search(text) for pattern, _ in verify_repo.PERSONAL_PATH_PATTERNS)

    assert _matches(real_home)
    assert _matches(mac_home)
    assert not _matches(placeholder_home)
    assert not _matches("lessons/2026-11-25-card.md")


def test_the_changelog_has_no_duplicate_heading_inside_a_release_section():
    """一个发布小节里每个 `###` 标题只能出现一次。"""
    violations = verify_repo.changelog_violations()
    assert violations == [], "\n".join(v.render() for v in violations)


def test_the_changelog_release_sections_use_the_keep_a_changelog_headings():
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = {line[4:].strip() for line in text.splitlines() if line.startswith("### ")}
    assert headings <= set(verify_repo.CHANGELOG_SECTION_HEADINGS)


def test_no_python_source_carries_a_utf8_bom():
    """UTF-8 BOM 会让 Python 把第一行当���名字，Windows 上的编辑器还爱偷偷加。"""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "backend").rglob("*.py")) + sorted((REPO_ROOT / "tests").rglob("*.py")):
        if path.read_bytes().startswith(b"\xef\xbb\xbf"):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []


def test_no_unused_method_marker_and_no_dead_branch_survive():
    """`_unused_` 方法与 `if False:` 分支都是死代码的形状。"""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "backend").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if verify_repo.UNUSED_METHOD_RE.search(text):
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: unused method")
        if verify_repo.DEAD_BRANCH_RE.search(text):
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: dead branch")
    assert offenders == []


def test_no_production_hold_or_park_hook_survives_in_the_backend():
    """这两个环境变量能把真实批次冻在半路，绝不能进生产代码。"""
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "backend").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in verify_repo.HOLD_HOOK_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {token}")
    assert offenders == []


# ---------------------------------------------------------------- 2. 提示词骨架


def test_the_repair_skeletons_are_valid_json():
    """修复骨架要能被 `json.loads` 直接读懂，模型才可能照着改对。"""
    for hint in (P03_REPAIR_SCHEMA_HINT, P03_REPAIR_SCHEMA_HINT_WITH_ROUTE):
        parsed = json.loads(hint)
        assert isinstance(parsed, dict)
        assert isinstance(parsed["planned_writes"], list)
        assert len(parsed["planned_writes"]) == 1


def test_the_repair_skeletons_carry_every_required_item_key():
    """骨架里必须带齐 `P03_ITEM_REQUIRED_KEYS` 的每一个键。"""
    for hint in (P03_REPAIR_SCHEMA_HINT, P03_REPAIR_SCHEMA_HINT_WITH_ROUTE):
        item = json.loads(hint)["planned_writes"][0]
        for key in P03_ITEM_REQUIRED_KEYS:
            assert key in item, f"{key} missing from the repair skeleton"


def test_the_routed_skeleton_adds_the_wiki_route_to_the_item():
    """带路由的变体必须把 `wiki_route` 放在条目里，而不是条目旁边。"""
    plain = json.loads(P03_REPAIR_SCHEMA_HINT)["planned_writes"][0]
    routed = json.loads(P03_REPAIR_SCHEMA_HINT_WITH_ROUTE)["planned_writes"][0]
    assert "wiki_route" not in plain
    assert "wiki_route" in routed
    assert set(routed["wiki_route"]) == {"page_type", "project", "reason"}
    # The routed variant only adds the route: every other key is unchanged.
    assert {k: v for k, v in routed.items() if k != "wiki_route"} == plain


# ---------------------------------------------------------------- 3. 跨层契约


def test_the_version_is_the_same_in_every_layer():
    import tomllib

    import yaml

    assert (REPO_ROOT / "backend" / "ai_flywheel" / "__init__.py").read_text(encoding="utf-8").count(
        f'__version__ = "{__version__}"'
    ) == 1

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__

    site = yaml.safe_load((REPO_ROOT / "profile" / "site.yaml").read_text(encoding="utf-8"))
    assert site["brand"]["version"] == __version__

    package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == __version__


def test_the_example_config_matches_the_api_runner_default_timeout():
    """示例配置里的超时必须与代码默认值一致，否则文档会教出错误的心智模型。"""
    import yaml

    example = yaml.safe_load((REPO_ROOT / "flywheel.yaml.example").read_text(encoding="utf-8"))
    assert example["runner"]["api"]["timeout_seconds"] == ApiRunnerConfig().timeout_seconds


def test_the_frontend_run_status_union_matches_the_backend_enum():
    """前端的 `RunStatus` 联合必须与后端枚举逐值对应。"""
    types = (FRONTEND_SRC / "types.ts").read_text(encoding="utf-8")
    block = re.search(r"export interface WorkflowRunRecord \{(.*?)\n\}", types, re.S)
    assert block is not None
    line = re.search(r"^\s*status:\s*(.+)$", block.group(1), re.M)
    assert line is not None
    frontend = set(re.findall(r"'([^']+)'", line.group(1)))
    assert frontend == {status.value for status in RunStatus}


def test_the_frontend_labels_cover_every_node_id_and_signal_kind():
    """每个后端节点与信号种类都必须有前端标签，否则门户只能显示原始枚举。"""
    labels = (FRONTEND_SRC / "lib" / "labels.ts").read_text(encoding="utf-8")

    def _keys(name: str) -> set[str]:
        match = re.search(rf"export const {name}: Record<string, string> = \{{(.*?)\n\}}", labels, re.S)
        assert match is not None, f"{name} is missing from labels.ts"
        return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", match.group(1), re.M))

    node_ids = {node_id for node_id, _name, _actor in NODE_TEMPLATES}
    assert node_ids <= _keys("STAGE_PLAIN_NAMES")
    assert {kind.value for kind in SignalKind} <= _keys("RETRO_KIND_LABELS")


def test_the_feishu_card_and_the_backend_agree_on_what_is_sensitive():
    """卡片、审批门与门户必须对"敏感情项"给出同一个答案。"""
    target_kinds = ("knowledge_doc", "flywheel_rule", "skill_improvement")
    actions = ("create", "update", "register_rule", "delete", "overwrite_core")
    for target_kind in target_kinds:
        for action in actions:
            for requires_approval in (False, True):
                write = {
                    "target_kind": target_kind,
                    "action": action,
                    "requires_approval": requires_approval,
                    "title": "t",
                    "summary": "s",
                }
                assert is_sensitive(write) == is_sensitive_write(write), write
    # A Skill change and a destructive action are always sensitive; a routine note is not.
    assert is_sensitive({"target_kind": "skill_improvement", "action": "create", "requires_approval": False})
    assert is_sensitive({"target_kind": "knowledge_doc", "action": "delete", "requires_approval": False})
    assert not is_sensitive({"target_kind": "knowledge_doc", "action": "create", "requires_approval": False})


# ---------------------------------------------------------------- 4. 真实 serve 角色


def test_the_serve_role_runs_the_recovery_loop_and_shuts_down_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """以 `serve` 角色真的起一次服务：核心索引卡在、恢复线程活着、退出不报错。"""
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = FlywheelConfig(base_dir=tmp_path)
    rt = FlywheelRuntime(cfg, process_role="serve")

    first_pass = threading.Event()
    original = rt.orchestrator.recover_interrupted_runs

    def _spy(*args: Any, **kwargs: Any) -> Any:
        first_pass.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(rt.orchestrator, "recover_interrupted_runs", _spy)

    with caplog.at_level(logging.ERROR, logger="ai_flywheel.recovery"):
        with TestClient(create_app(runtime=rt)) as client:
            response = client.get("/health")
            assert response.status_code == 200
            assert response.json()["version"] == __version__

            # The derived core card is written at startup, not only on demand.
            assert (tmp_path / "data" / "FLYWHEEL-CORE.md").is_file()

            recovery = [t for t in threading.enumerate() if t.name == "flywheel-recovery"]
            assert recovery, "the serve role must start the recovery thread"
            assert recovery[0].is_alive()

            # The loop body really ran: a missing import would raise here and be logged.
            assert first_pass.wait(timeout=10), "the recovery loop never ran"

        for thread in recovery:
            thread.join(timeout=10)

    assert not any(t.is_alive() for t in recovery), "the recovery thread must stop with the app"
    failures = [record for record in caplog.records if record.name == "ai_flywheel.recovery"]
    assert failures == [], [record.getMessage() for record in failures]


# ---------------------------------------------------------------- 5. Windows 契约


def test_a_windows_workspace_path_splits_into_clean_segments():
    """反斜杠工作区路径要按段切开，否则项目名只会作为子串命中。"""
    backslash = _join("D:", "\\", "work", "\\", "alpha-team")
    forward = _join("D:", "/", "work", "/", "beta-team")
    assert _workspace_segments([backslash]) == ["d:", "work", "alpha-team"]
    assert _workspace_segments([forward]) == ["d:", "work", "beta-team"]
    # Mixed separators and trailing separators never produce empty segments.
    mixed = _join("D:", "\\", "work/", "\\alpha-team")
    assert _workspace_segments([mixed]) == ["d:", "work", "alpha-team"]
    assert "" not in _workspace_segments([backslash + "\\"])


def test_a_written_document_uses_lf_line_endings(tmp_path: Path):
    """Windows 上的写入必须是 LF：LF 治理的知识库不能被 CRLF 污染。"""
    rt = _runtime(tmp_path)
    content = "---\ntitle: 换行契约\n---\n\n第一行\r\n第二行\r\n"
    rt.knowledge.write_document_verified("lessons/2026-11-25-eol.md", content)
    raw = (tmp_path / "data" / "knowledge" / "lessons" / "2026-11-25-eol.md").read_bytes()
    assert b"\r\n" not in raw
    assert b"\r" not in raw


# ---------------------------------------------------------------- 6. 流水线副作用与缓存


def test_a_rejected_batch_leaves_the_mistake_pool_and_the_dissent_table_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """用户取消放行时，整批零改动：错题池与分歧表都必须是空的。"""
    rt = _runtime(tmp_path, approval_mode="always")
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(), planned_writes=[_doc()]),
    )

    run = rt.orchestrator.start_run(
        trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=False
    )
    assert run.status == "AWAITING_APPROVAL"

    rejected = rt.orchestrator.reject_run(run.run_id, reason="用户取消放行", source="test")
    assert rejected.status == "CANCELLED"
    assert rt.db.list_mistake_observations() == []
    assert rt.db.list_dissent_records() == []


def test_an_unparsable_model_reply_creates_no_cache_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """不可解析的回复不写缓存：坏输出不得污染下一次运行。"""
    rt = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _GarbageModel())

    run = rt.orchestrator.start_run(
        trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True
    )
    assert run.status == "FAILED"

    cache_dir = tmp_path / "data" / "receipts" / "api_cache"
    cached = sorted(p.name for p in cache_dir.glob("*.json")) if cache_dir.is_dir() else []
    assert cached == []


def test_a_usable_model_reply_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """闸门不能是空转的：通过校验的回复必须真的被缓存下来。"""
    rt = _runtime(tmp_path, approval_mode="always")
    _register(rt)
    monkeypatch.setattr(
        OpenAICompatibleRunner,
        "_call_chat_completion",
        _FakeModel(_review(), planned_writes=[_doc()]),
    )

    run = rt.orchestrator.start_run(
        trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=False
    )
    assert run.status == "AWAITING_APPROVAL"

    cache_dir = tmp_path / "data" / "receipts" / "api_cache"
    cached = sorted(p.name for p in cache_dir.glob("*.json"))
    assert any(name.startswith("p02_review-") for name in cached), cached
    assert any(name.startswith("p03_plan-") for name in cached), cached


# ---------------------------------------------------------------- 7. 数据底座建模


def _schema_snapshot(path: Path) -> dict[str, set[str]]:
    """Every user table and its column set, minus SQLite internals and FTS shadow tables."""
    conn = sqlite3.connect(str(path))
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        kept = sorted(
            table
            for table in tables
            if not table.startswith("sqlite_") and not table.startswith("knowledge_fts_")
        )
        return {
            table: {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")}
            for table in kept
        }
    finally:
        conn.close()


def test_the_authoritative_ddl_matches_the_runtime_schema(tmp_path: Path):
    """`docs/schema.sql` 与运行时 `FlywheelDB` 的表集合、逐表列集合必须完全一致。"""
    ddl = (REPO_ROOT / "docs" / "schema.sql").read_text(encoding="utf-8")
    reference_db = tmp_path / "reference.db"
    conn = sqlite3.connect(str(reference_db))
    try:
        conn.executescript(ddl)
    finally:
        conn.close()

    FlywheelDB(tmp_path / "runtime.db")

    documented = _schema_snapshot(reference_db)
    runtime = _schema_snapshot(tmp_path / "runtime.db")
    assert set(documented) == set(runtime), {
        "documented_only": sorted(set(documented) - set(runtime)),
        "runtime_only": sorted(set(runtime) - set(documented)),
    }
    for table in sorted(documented):
        assert documented[table] == runtime[table], {
            "table": table,
            "documented_only": sorted(documented[table] - runtime[table]),
            "runtime_only": sorted(runtime[table] - documented[table]),
        }


def test_the_builtin_dictionary_covers_every_execution_enum_value(tmp_path: Path):
    """五个执行枚举的每个值都必须在 `sys_dict_items` 里有启用记录。"""
    db = FlywheelDB(tmp_path / "dicts.db")
    coverage = {
        "queue_status": QueueStatus,
        "ingress_channel": IngressChannel,
        "run_status": RunStatus,
        "rule_status": RuleStatus,
        "signal_kind": SignalKind,
    }
    for dict_type, enum_cls in coverage.items():
        enabled = {item.code for item in db.list_dicts(dict_type=dict_type) if item.enabled}
        missing = {member.value for member in enum_cls} - enabled
        assert not missing, f"{dict_type} is missing {sorted(missing)}"


def test_the_frontend_queue_types_and_labels_match_the_backend_enums():
    """前端 `QueueStatus` / `IngressChannel` 联合与标签必须与后端枚举逐值一致。"""
    types = (FRONTEND_SRC / "types.ts").read_text(encoding="utf-8")

    def _union(name: str) -> set[str]:
        line = re.search(rf"export type {name} = ([^\n]+)", types)
        assert line is not None, f"{name} union is missing from types.ts"
        return set(re.findall(r"'([^']+)'", line.group(1)))

    assert _union("QueueStatus") == {status.value for status in QueueStatus}
    assert _union("IngressChannel") == {channel.value for channel in IngressChannel}

    labels = (FRONTEND_SRC / "lib" / "labels.ts").read_text(encoding="utf-8")

    def _label_keys(name: str) -> set[str]:
        match = re.search(
            rf"export const {name}: Record<string, string> = \{{(.*?)\n\}}", labels, re.S
        )
        assert match is not None, f"{name} is missing from labels.ts"
        return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", match.group(1), re.M))

    assert {status.value for status in QueueStatus} <= _label_keys("QUEUE_STATUS_LABELS")
    assert {channel.value for channel in IngressChannel} <= _label_keys("INGRESS_CHANNEL_LABELS")


def test_the_hygiene_scan_covers_the_authoritative_ddl():
    """权威 DDL 也是公开仓文本，必须纳入卫生扫描后缀。"""
    assert ".sql" in verify_repo.SCAN_SUFFIXES
    assert (REPO_ROOT / "docs" / "schema.sql").is_file()
