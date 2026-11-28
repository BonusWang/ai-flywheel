"""Wave 4 架构不变量与一次性验收闸门（W4.2）。

六类不变量，每一类都写成一条会失败的测试，而不是一条注释：

1. **公开仓卫生**：非忽略文件里没有个人路径 / 身份 / 凭据，`.py` 没有 UTF-8 BOM，
   没有 `_unused_` 方法、没有 `if False:` 死分支、`backend/` 没有生产用 hold/park 钩子，
   CHANGELOG 在一个发布小节内没有重复标题。
2. **提示词骨架**：P03 的两个修复骨架必须是合法 JSON，且带齐每个必填键
   （带路由的变体还要带 `wiki_route`）。
3. **跨层契约**：版本号四处一致、示例配置与运行器默认值一致、前端状态联合与后端枚举一致、
   前端标签覆盖全部节点与信号种类、飞书卡片与后端对"敏感情项"的判定一致、事故卡在三处
   同为敏感且计划 / 路由 / 审批门给出同一条理由。
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
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_flywheel import __version__
from ai_flywheel.adapters.runners import (
    P03_ITEM_REQUIRED_KEYS,
    P03_REPAIR_SCHEMA_HINT,
    P03_REPAIR_SCHEMA_HINT_WITH_ROUTE,
    PLAN_SOURCE_MODEL,
    OpenAICompatibleRunner,
    _plan_from_items,
)
from ai_flywheel.adapters.wiki import _workspace_segments
from ai_flywheel.adapters.wiki.router import RouteDecision
from ai_flywheel.app import create_app
from ai_flywheel.config import ApiRunnerConfig, FlywheelConfig
from ai_flywheel.adapters.channels.feishu.card import is_sensitive
from ai_flywheel.core.orchestrator import NODE_TEMPLATES, WorkflowOrchestrator
from ai_flywheel.core.pipeline.helpers import apply_route_decision
from ai_flywheel.models import (
    INCIDENT_APPROVAL_REASON,
    IngressChannel,
    PlannedWrite,
    QueueStatus,
    RuleStatus,
    RunStatus,
    SignalKind,
    is_incident_write,
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


def test_the_example_config_matches_the_workflow_schedule_defaults():
    """示例配置里的调度宽限期与漏跑补跑开关必须与代码默认值一致。"""
    import yaml

    from ai_flywheel.config import WorkflowConfig

    example = FlywheelConfig.model_validate(
        yaml.safe_load((REPO_ROOT / "flywheel.yaml.example").read_text(encoding="utf-8"))
    )
    defaults = WorkflowConfig()
    assert defaults.misfire_grace_seconds == 14400
    assert example.workflow.misfire_grace_seconds == defaults.misfire_grace_seconds
    assert defaults.catchup_missed_schedule is True
    assert example.workflow.catchup_missed_schedule is defaults.catchup_missed_schedule
    # A wording only one installation uses is private configuration: the public
    # template documents the key and ships an empty list.
    assert example.sessions.triage.controlled_write_markers == []
    with pytest.raises(ValueError):
        WorkflowConfig(misfire_grace_seconds=59)


async def test_the_mcp_server_keeps_exactly_seven_tools_and_four_resources(tmp_path: Path):
    """新能力只扩展现有工具的可选参数：工具与资源的总数都不变。"""
    from ai_flywheel.mcp_server import build_mcp_server

    rt = _runtime(tmp_path)
    mcp = build_mcp_server(rt)
    tools = sorted(tool.name for tool in await mcp.list_tools())
    assert tools == [
        "flywheel_ingest_session",
        "flywheel_preflight_activate",
        "flywheel_record_outcome",
        "flywheel_register_session",
        "flywheel_report_signal",
        "flywheel_session_intent",
        "flywheel_status",
    ]
    assert len(await mcp.list_resources()) == 4
    schemas = {tool.name: tool.input_schema for tool in await mcp.list_tools()}
    for name, key in (
        ("flywheel_session_intent", "wiki_paths"),
        ("flywheel_session_intent", "governance_only"),
        ("flywheel_ingest_session", "structured_brief"),
    ):
        assert key in schemas[name]["properties"], (name, key)
        assert key not in schemas[name].get("required", []), (name, key)


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


PRIVATE_DRAFT = "---\ntitle: database\nsensitivity: private_sensitive\n---\n\n# database\n"
CREDENTIAL_DRAFT = "---\ntitle: database\ncontains_credentials: true\n---\n\n# database\n"


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
    # A project configuration page, and a draft whose frontmatter declares it a private page,
    # are sensitive on their own: a card must offer no way to pass them as routine items.
    for action in ("create", "update"):
        for declared in (
            {"wiki_route": {"page_type": "project-config", "project": "alpha"}},
            {"wiki_route": {"page_type": "project-practice-note", "project": "alpha"}, "content_preview": PRIVATE_DRAFT},
            {"wiki_route": {"page_type": "project-practice-note", "project": "alpha"}, "content_preview": CREDENTIAL_DRAFT},
        ):
            write = {"target_kind": "knowledge_doc", "action": action, "requires_approval": False, "title": "t", "summary": "s", **declared}
            assert is_sensitive(write) is True, write
            assert is_sensitive_write(write) is True, write
    # Any other page of a project is a routine write, and a mention of privacy in the body is not a declaration.
    for preview in ("## 用途一行\n\n- 补充一行。\n", "## 关键事项\n\n- 该页标了 sensitivity: private_sensitive，不要外传。"):
        routine = {
            "target_kind": "knowledge_doc",
            "action": "update",
            "requires_approval": False,
            "title": "t",
            "summary": "s",
            "wiki_route": {"page_type": "project-algorithm", "project": "alpha"},
            "content_preview": preview,
        }
        assert is_sensitive(routine) is False, routine
        assert is_sensitive_write(routine) is False, routine


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


def test_only_the_serve_role_checks_for_a_missed_schedule_and_health_says_when_it_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """`/health.started_at` 是「调度时刻服务在不在」的外部证据；漏跑检查只挂在 serve 的巡检循环上。"""
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    serve = FlywheelRuntime(FlywheelConfig(base_dir=tmp_path), process_role="serve")

    checked = threading.Event()
    original = serve.automation.check_missed_schedule_catchup

    def _catchup_spy(*args: Any, **kwargs: Any) -> Any:
        checked.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(serve.automation, "check_missed_schedule_catchup", _catchup_spy)

    with caplog.at_level(logging.ERROR, logger="ai_flywheel.recovery"):
        with TestClient(create_app(runtime=serve)) as client:
            health = client.get("/health")
            assert health.status_code == 200
            # A Shanghai instant, the same one the automation snapshot reports.
            assert health.json()["started_at"] == serve.automation.started_at
            started = datetime.fromisoformat(health.json()["started_at"])
            assert started.utcoffset() == timedelta(hours=8)
            assert serve.automation.get_snapshot()["scheduler"]["started_at"] == (
                health.json()["started_at"]
            )
            assert checked.wait(timeout=10), "the recovery loop never checked for a missed schedule"
        for thread in (t for t in threading.enumerate() if t.name == "flywheel-recovery"):
            thread.join(timeout=10)
    failures = [record for record in caplog.records if record.name == "ai_flywheel.recovery"]
    assert failures == [], [record.getMessage() for record in failures]

    # Any other role starts no daemon and never runs the check, however long it is up.
    calls: list[str] = []
    cli_dir = tmp_path / "cli"
    shutil.copytree(REPO_ROOT / "profile", cli_dir / "profile")
    cli = FlywheelRuntime(FlywheelConfig(base_dir=cli_dir), process_role="cli")
    monkeypatch.setattr(
        cli.automation,
        "check_missed_schedule_catchup",
        lambda *args, **kwargs: calls.append("checked"),
    )
    threads_before = set(threading.enumerate())
    with TestClient(create_app(runtime=cli)) as client:
        assert client.get("/health").json()["started_at"] == cli.automation.started_at
        time.sleep(0.3)
        assert not {
            t.name for t in set(threading.enumerate()) - threads_before
        } & {"flywheel-recovery", "flywheel-catchup"}
    assert calls == []


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

# ---------------------------------------------------------------- the incident ledger and the new review and plan settings
# (feat-agy-ingest-and-high-fidelity-distill)


LEDGER_PATH = "wiki/incidents/ai-incidents/2026-11-27-wrong-config.md"
# The three grounds on which a planned write is a page of the incident ledger; any one is enough.
INCIDENT_GROUNDS = {
    "the routing declaration names the page type": {"wiki_route": {"page_type": "Incident", "project": None}},
    "the item's own payload names it": {"page_type": "incident"},
    "the target path lies in a ledger directory": {"relative_path": LEDGER_PATH},
}


def test_an_incident_card_is_sensitive_on_every_surface_by_any_of_its_three_grounds():
    """事故卡在卡片、审批门与门户三处是同一个答案：三条依据任一成立即为敏感。"""
    for ground, fields in INCIDENT_GROUNDS.items():
        for action in ("create", "update"):
            write: dict[str, Any] = {
                "target_kind": "knowledge_doc",
                "action": action,
                "requires_approval": False,
                "title": "改错测试库配置复盘",
                "summary": "s",
                **fields,
            }
            assert (is_incident_write(write), is_sensitive(write), is_sensitive_write(write)) == (True, True, True), (ground, write)
            if "page_type" in fields:
                continue  # the item's own top-level declaration is a shape only the card's dict carries
            # The shape the pipeline carries reads the same answer.
            as_model = PlannedWrite(**write)
            assert (is_incident_write(as_model), is_sensitive(as_model), is_sensitive_write(as_model)) == (True, True, True), (ground, as_model)

    # The path ground holds whatever the spelling of the path, with no declaration anywhere.
    for path in (r"wiki\incidents\AI-Incidents\2026-11-27-x.md", "wiki/incidents/ai-incidents-2026/x.md"):
        assert is_incident_write({"target_kind": "knowledge_doc", "relative_path": path}) is True

    # A note that only talks about the ledger is a routine note; so is a page outside it.
    for routine in (
        {
            "target_kind": "knowledge_doc",
            "action": "create",
            "requires_approval": False,
            "title": "事故复盘",
            "summary": "s",
            "relative_path": "lessons/2026-11-27-incident-note.md",
            "content_preview": "## 事实\n\n- 这次事故记入了 ai-incidents 台账。\n",
        },
        {
            "target_kind": "knowledge_doc",
            "action": "create",
            "requires_approval": False,
            "title": "事故复盘",
            "summary": "s",
            "relative_path": "wiki/incidents/2026-11-27-x.md",
            "wiki_route": {"page_type": "project-practice-note", "project": "alpha"},
        },
    ):
        assert (is_incident_write(routine), is_sensitive(routine), is_sensitive_write(routine)) == (False, False, False), routine


def test_the_plan_the_route_and_the_approval_gate_name_the_same_reason_for_an_incident_card():
    """计划、路由、审批门三处对事故卡给出同一条理由，门户与卡片上不会出现两种说法。"""
    item = {
        "target_kind": "knowledge_doc",
        "action": "create",
        "title": "改错测试库配置复盘",
        "summary": "助手改错了测试库配置且未告知。",
        "relative_path": LEDGER_PATH,
        "source_session_keys": ["codex:s-1"],
        "content_preview": "## 定性\n\n严重：助手改错了测试库配置且未告知。\n",
        "wiki_route": {"page_type": "incident", "project": None, "reason": "一次协作事故"},
    }

    (planned,) = _plan_from_items(
        [item], source=PLAN_SOURCE_MODEL, max_write_set=8, default_doc_prefix="lessons/", skills_root=None
    ).planned_writes
    assert (planned.requires_approval, planned.approval_reason) == (True, INCIDENT_APPROVAL_REASON)

    # A card the plan did not flag gets the same words when the route recognises it.
    unflagged = planned.model_copy(update={"requires_approval": False, "approval_reason": ""})
    assert apply_route_decision(unflagged, RouteDecision(routed=True, page_type="incident", sensitive=True)) == []
    assert (unflagged.requires_approval, unflagged.approval_reason) == (True, INCIDENT_APPROVAL_REASON)

    # The gate repeats that reason to the human, and nothing else.
    assert WorkflowOrchestrator._needs_manual_approval([planned], "smart") == (True, [INCIDENT_APPROVAL_REASON])


def test_the_example_config_matches_the_defaults_of_the_new_review_and_plan_settings():
    """示例配置必须写全新增的键（会话文本预算、分阶段模型、领域页配额），并与代码默认值一致。"""
    import yaml

    from ai_flywheel.config import WorkflowConfig

    example = FlywheelConfig.model_validate(
        yaml.safe_load((REPO_ROOT / "flywheel.yaml.example").read_text(encoding="utf-8"))
    )
    api, workflow = ApiRunnerConfig(), WorkflowConfig()

    assert api.p02_session_char_limit == 16000
    assert example.runner.api.p02_session_char_limit == api.p02_session_char_limit
    # Empty means the stage runs on the configured `model` / `reasoning_effort`: a template must not pick a model.
    for key in ("review_model", "review_reasoning_effort", "plan_model", "plan_reasoning_effort"):
        assert getattr(api, key) == "", key
        assert getattr(example.runner.api, key) == "", key
    assert workflow.max_domain_writes_per_run == 8
    assert example.workflow.max_domain_writes_per_run == workflow.max_domain_writes_per_run
    # The bounds: a budget too small to review with or too large to send, and a quota beyond the write set.
    for rejected in ({"p02_session_char_limit": 1999}, {"p02_session_char_limit": 64001}):
        with pytest.raises(ValueError):
            ApiRunnerConfig(**rejected)
    with pytest.raises(ValueError):
        WorkflowConfig(max_domain_writes_per_run=33)


# ---------------------------------------------------------------- 8. 结构与依赖不变量


def test_no_class_defines_duplicate_methods_and_no_db_mixins_collide():
    """任何类不得重复定义同名方法；FlywheelDB 的仓储 mixin 不得存在同名方法遮蔽。"""
    import ast
    from collections import defaultdict

    backend_root = REPO_ROOT / "backend" / "ai_flywheel"
    dup_methods: list[str] = []
    for path in sorted(backend_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(backend_root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            seen: dict[str, list[int]] = defaultdict(list)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_setter = any(
                        isinstance(d, ast.Attribute) and d.attr == "setter"
                        for d in item.decorator_list
                    )
                    if not is_setter:
                        seen[item.name].append(item.lineno)
            for name, lines in sorted(seen.items()):
                if len(lines) > 1:
                    dup_methods.append(f"{rel}::{node.name}.{name} at lines {lines}")
    assert dup_methods == [], f"Duplicate method definitions found: {dup_methods}"

    db_dir = backend_root / "storage" / "db"
    mixin_methods: dict[str, list[str]] = defaultdict(list)
    for path in sorted(db_dir.glob("*_repo.py")) + [db_dir / "migrations.py"]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        mixin_methods[item.name].append(f"{path.name}:{node.name}:{item.lineno}")
    collisions = {name: locs for name, locs in sorted(mixin_methods.items()) if len(locs) > 1}
    assert collisions == {}, f"FlywheelDB mixin method collisions: {collisions}"


_SHARED_LEAF_PRIMITIVES = frozenset(
    {
        "ai_flywheel.models",
        "ai_flywheel.config",
        "ai_flywheel.core.time_utils",
        "ai_flywheel.core.privacy",
        "ai_flywheel.core.markdown_utils",
        "ai_flywheel.core.approval_channel",
    }
)

_DEPRECATED_SHIM_MODULES = frozenset(
    {
        "ai_flywheel.adapters.wiki_gate",
        "ai_flywheel.core.feishu_card",
        "ai_flywheel.core.feishu_channel",
        "ai_flywheel.core.feishu_listen",
        "ai_flywheel.core.feishu_check",
    }
)

# Zero upward layer exceptions across the backend (PD-002 resolved in B10).
_KNOWN_LAYER_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset()


def _backend_runtime_import_graph() -> dict[str, set[str]]:
    """Runtime-only internal import graph of ``ai_flywheel`` (skipping ``if TYPE_CHECKING:``)."""
    import ast
    from collections import defaultdict

    backend_root = REPO_ROOT / "backend" / "ai_flywheel"
    edges: dict[str, set[str]] = defaultdict(set)

    def _module_name(path: Path) -> str:
        rel = path.relative_to(backend_root).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return "ai_flywheel" + ("." + ".".join(parts) if parts else "")

    for path in sorted(backend_root.rglob("*.py")):
        mod = _module_name(path)
        edges.setdefault(mod, set())
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        class _Visitor(ast.NodeVisitor):
            def visit_If(self, node: ast.If) -> None:
                if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                    return
                self.generic_visit(node)

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    if alias.name.startswith("ai_flywheel"):
                        edges[mod].add(alias.name)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                if node.module and node.module.startswith("ai_flywheel"):
                    edges[mod].add(node.module)
                elif node.level > 0:
                    base = mod.split(".")
                    if path.name != "__init__.py":
                        base = base[:-1]
                    up = node.level - 1
                    if up > 0:
                        base = base[:-up]
                    target = ".".join(base + ([node.module] if node.module else []))
                    edges[mod].add(target)

        _Visitor().visit(tree)

    return edges


def test_no_runtime_circular_imports_in_backend():
    """后端运行时模块依赖图必须是有向无环图（DAG），禁止引入循环依赖。"""
    edges = _backend_runtime_import_graph()
    idx = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    def _strongconnect(v: str) -> None:
        nonlocal idx
        indices[v] = low[v] = idx
        idx += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(edges.get(v, ())):
            if w not in indices:
                _strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                cycles.append(sorted(comp))

    for node in sorted(set(edges) | {t for ts in edges.values() for t in ts}):
        if node not in indices:
            _strongconnect(node)

    assert cycles == [], f"Runtime circular imports detected: {cycles}"


def test_no_production_code_imports_legacy_shims():
    """已清理的 5 个历史兼容壳不得回流到代码库，且任何后端或测试代码不得再引用它们。"""
    import ast

    backend_root = REPO_ROOT / "backend" / "ai_flywheel"
    expected_gone = (
        backend_root / "adapters" / "wiki_gate.py",
        backend_root / "core" / "feishu_card.py",
        backend_root / "core" / "feishu_channel.py",
        backend_root / "core" / "feishu_listen.py",
        backend_root / "core" / "feishu_check.py",
    )
    surviving = [p.relative_to(REPO_ROOT).as_posix() for p in expected_gone if p.exists()]
    assert surviving == [], f"Deleted legacy shim file(s) reappeared: {surviving}"

    offenders: list[str] = []
    for root_dir in (backend_root, REPO_ROOT / "tests"):
        for path in sorted(root_dir.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in _DEPRECATED_SHIM_MODULES:
                            offenders.append(f"{rel} -> {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module in _DEPRECATED_SHIM_MODULES:
                        offenders.append(f"{rel} -> {node.module}")
                    for alias in node.names:
                        cand = f"{node.module}.{alias.name}"
                        if cand in _DEPRECATED_SHIM_MODULES:
                            offenders.append(f"{rel} -> {cand}")
    assert offenders == [], f"Legacy shim import(s) found: {offenders}"


def test_shared_leaf_primitives_have_no_upward_dependencies():
    """共享叶子原语（models/config/time_utils/privacy/markdown_utils/approval_channel）不得向上依赖。"""
    edges = _backend_runtime_import_graph()
    allowed = _SHARED_LEAF_PRIMITIVES | {"ai_flywheel"}
    offenders: list[str] = []
    for mod in sorted(_SHARED_LEAF_PRIMITIVES):
        for target in sorted(edges.get(mod, ())):
            if target not in allowed:
                offenders.append(f"{mod} -> {target}")
    assert offenders == [], f"Shared leaf primitive has upward dependency: {offenders}"


def test_backend_respects_layered_dependency_direction():
    """依赖方向必须服从 entry -> runtime -> core -> adapters -> storage -> leaf primitives。"""
    edges = _backend_runtime_import_graph()
    rank = {"entry": 5, "runtime": 4, "core": 3, "adapters": 2, "storage": 1, "leaf": 0}

    def _layer(mod: str) -> str:
        if mod == "ai_flywheel" or mod in _SHARED_LEAF_PRIMITIVES:
            return "leaf"
        parts = mod.split(".")
        top = parts[1] if len(parts) > 1 else ""
        if top in {"app", "cli", "mcp_server", "routes"}:
            return "entry"
        if top in {"runtime", "core", "adapters", "storage"}:
            return top
        return "leaf"

    violations: list[str] = []
    for src, targets in sorted(edges.items()):
        if src in _DEPRECATED_SHIM_MODULES:
            continue
        sl = _layer(src)
        for target in sorted(targets):
            tl = _layer(target)
            if tl == "leaf" or sl == "leaf":
                continue
            if rank[sl] < rank[tl] and (src, target) not in _KNOWN_LAYER_EXCEPTIONS:
                violations.append(f"[{sl}->{tl}] {src} -> {target}")
    assert violations == [], f"New layer dependency violations: {violations}"


def test_no_ad_hoc_frontmatter_splitting_outside_markdown_utils():
    """Markdown frontmatter 解析必须走 core.markdown_utils，禁止再散落 .split('---', 2)。"""
    backend_root = REPO_ROOT / "backend" / "ai_flywheel"
    offenders: list[str] = []
    pattern = re.compile(r'\.split\(\s*["\']---["\']\s*,\s*2\s*\)')
    for path in sorted(backend_root.rglob("*.py")):
        rel = path.relative_to(backend_root).as_posix()
        if rel == "core/markdown_utils.py":
            continue
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(rel)
    assert offenders == [], f"Ad-hoc frontmatter split('---', 2) found in: {offenders}"


def test_frontend_http_transport_is_centralized():
    """前端 HTTP 请求必须统一收口在 api.ts，SSE 连接统一收口在 App.tsx。"""
    fetch_offenders: list[str] = []
    sse_offenders: list[str] = []
    for path in sorted(FRONTEND_SRC.rglob("*.ts*")):
        rel = path.relative_to(FRONTEND_SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        if rel != "api.ts" and re.search(r"\bfetch\s*\(", text):
            fetch_offenders.append(rel)
        if rel != "App.tsx" and re.search(r"\bnew\s+EventSource\s*\(", text):
            sse_offenders.append(rel)
    assert fetch_offenders == [], f"Direct fetch() outside api.ts: {fetch_offenders}"
    assert sse_offenders == [], f"Direct EventSource() outside App.tsx: {sse_offenders}"
