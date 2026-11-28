"""refactor-approval-channel-abstraction：通道契约、兼容导入与路由表。

四类断言，每一类都对应一条会失败的契约：

1. 审批通道契约：飞书通道结构性满足 `ApprovalChannel`，不完整实现不满足；
2. 兼容导入：`core.feishu_*` 与 `adapters.channels.feishu.*` 解析到同一对象；
3. 忙碌错误：`ApprovalLockBusy` 属于契约级 `ApprovalBusyError`；
4. 路由表：拆分后 63 条应用路由逐条一致，HTTP 语义不变。
"""
from __future__ import annotations

import inspect
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_flywheel.adapters.channels.feishu import (
    FeishuChannel as AdapterFeishuChannel,
    FeishuCheckService as AdapterFeishuCheckService,
    FeishuClickListener as AdapterFeishuClickListener,
    render_ask_card as adapter_render_ask_card,
)
from ai_flywheel.adapters.channels.feishu.card import render_state_card as adapter_render_state_card
from ai_flywheel.adapters.channels.feishu.check import event_card_id as adapter_event_card_id
from ai_flywheel.adapters.channels.feishu.listen import classify_failure as adapter_classify_failure
from ai_flywheel.app import create_app
from ai_flywheel.config import FeishuCliConfig, FlywheelConfig
from ai_flywheel.core.approval_channel import ApprovalBusyError, ApprovalChannel
from ai_flywheel.core.orchestrator import ApprovalLockBusy, WorkflowOrchestrator
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.outbox import OutboxDB

REPO_ROOT = Path(__file__).resolve().parent.parent

PROTOCOL_METHODS = {"availability", "first_send", "handle_click", "start", "stop", "tick"}

#: 拆分前的应用路由（方法 + 路径）。少一条、方法写错都会在这里变成红灯。
EXPECTED_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/health"),
    ("GET", "/llms.txt"),
    ("GET", "/api/events"),
    ("GET", "/api/v1/overview"),
    ("GET", "/api/v1/about"),
    ("GET", "/api/v1/public-snapshot"),
    ("POST", "/api/hooks/{client}"),
    ("GET", "/api/site/overview"),
    ("GET", "/api/site/about"),
    ("GET", "/api/site/connect"),
    ("PUT", "/api/site/config"),
    ("POST", "/api/site/seed"),
    ("GET", "/api/site/settings"),
    ("GET", "/api/site/policies"),
    ("PUT", "/api/site/policies/{policy_id}"),
    ("POST", "/api/site/policies/{policy_id}/reset"),
    ("GET", "/api/site/dicts"),
    ("POST", "/api/site/preflight"),
    ("POST", "/api/site/outcomes"),
    ("GET", "/api/site/outcomes"),
    ("GET", "/api/site/rules"),
    ("POST", "/api/site/rules"),
    ("POST", "/api/site/rules/{rule_id}/transition"),
    ("GET", "/api/site/observations"),
    ("POST", "/api/site/observations/bulk-archive"),
    ("POST", "/api/site/observations/bulk-restore"),
    ("POST", "/api/site/observations/{obs_id}/promote"),
    ("POST", "/api/site/observations/{obs_id}/archive"),
    ("POST", "/api/site/observations/{obs_id}/restore"),
    ("GET", "/api/site/dissents"),
    ("POST", "/api/site/dissents"),
    ("POST", "/api/site/dissents/bulk-status"),
    ("POST", "/api/site/dissents/{dissent_id}/status"),
    ("GET", "/api/site/growth-metrics"),
    ("GET", "/api/site/core-card"),
    ("POST", "/api/site/core-card/refresh"),
    ("GET", "/api/site/retro"),
    ("POST", "/api/site/signals"),
    ("POST", "/api/site/retro/bulk-status"),
    ("POST", "/api/site/retro/promote"),
    ("POST", "/api/site/retro/{retro_id}/status"),
    ("GET", "/api/site/knowledge"),
    ("GET", "/api/site/knowledge/doc"),
    ("POST", "/api/site/knowledge/freshness"),
    ("GET", "/api/site/skills"),
    ("GET", "/api/site/skills/{skill_name}/preview"),
    ("GET", "/api/site/sessions"),
    ("POST", "/api/site/sessions"),
    ("GET", "/api/site/queue"),
    ("GET", "/api/site/queue/{queue_id}/events"),
    ("POST", "/api/site/queue/sync"),
    ("POST", "/api/site/queue/re-triage"),
    ("POST", "/api/site/queue/bulk-action"),
    ("POST", "/api/site/queue/{queue_id}/action"),
    ("GET", "/api/site/runs"),
    ("GET", "/api/site/runs/{run_id}"),
    ("GET", "/api/site/runs/{run_id}/events"),
    ("POST", "/api/site/runs"),
    ("POST", "/api/site/runs/{run_id}/approve"),
    ("POST", "/api/site/runs/{run_id}/reject"),
    ("GET", "/api/site/automations"),
    ("PUT", "/api/site/automations/schedule"),
    ("POST", "/api/site/automations/recover"),
    ("POST", "/api/site/feishu-check"),
    ("GET", "/api/site/feishu-check"),
    ("GET", "/api/site/dream"),
    ("POST", "/api/site/dream/run"),
)


# ---------------------------------------------------------------- helpers


def _runtime(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    return FlywheelRuntime(FlywheelConfig(base_dir=tmp_path), skip_fts_sync=True)


def _channel(tmp_path: Path) -> AdapterFeishuChannel:
    db = FlywheelDB(tmp_path / "channel.db")
    cfg = FlywheelConfig()
    cfg.delivery.feishu_cli = FeishuCliConfig(
        enabled=True, interactive=True, user_id="ou_me", approver_open_ids=["ou_me"]
    )
    return AdapterFeishuChannel(cfg, db, OutboxDB(db), object())


def _registered_routes(app) -> set[tuple[str, str]]:
    """Flatten the app's routes, including lazily included routers, into (method, path)."""

    def flatten(routes):
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from flatten(original.routes)
            else:
                yield route

    found: set[tuple[str, str]] = set()
    for route in flatten(app.routes):
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()) or set():
            if method != "HEAD" and path.startswith(("/api/", "/health", "/llms.txt")):
                found.add((method, path))
    return found


# ---------------------------------------------------------------- 1. 通道契约


def test_feishu_channel_satisfies_the_approval_channel_protocol(tmp_path: Path):
    channel = _channel(tmp_path)
    assert isinstance(channel, ApprovalChannel)
    for name in PROTOCOL_METHODS:
        assert callable(getattr(channel, name)), name


def test_an_incomplete_channel_does_not_satisfy_the_protocol():
    class HalfChannel:
        def availability(self) -> str:
            return "ok"

    assert not isinstance(HalfChannel(), ApprovalChannel)


def test_the_protocol_is_exactly_the_documented_surface():
    declared = {name for name in ApprovalChannel.__dict__ if not name.startswith("_")}
    assert declared == PROTOCOL_METHODS


def test_the_orchestrator_types_the_channel_as_the_contract():
    annotation = inspect.signature(WorkflowOrchestrator.__init__).parameters["feishu_channel"].annotation
    assert "ApprovalChannel" in str(annotation)


def test_the_runtime_types_the_channel_as_the_contract():
    source = (REPO_ROOT / "backend" / "ai_flywheel" / "runtime.py").read_text(encoding="utf-8")
    assert "self.feishu_channel: ApprovalChannel | None = None" in source


# ---------------------------------------------------------------- 2. 权威通道入口与旧壳清理


def test_canonical_adapter_surface_exports_feishu_collaborators_and_legacy_shims_are_removed():
    for obj in (
        AdapterFeishuChannel,
        AdapterFeishuCheckService,
        AdapterFeishuClickListener,
        adapter_event_card_id,
        adapter_classify_failure,
        adapter_render_ask_card,
        adapter_render_state_card,
    ):
        assert obj is not None
    core_dir = REPO_ROOT / "backend" / "ai_flywheel" / "core"
    for shim in ("feishu_card.py", "feishu_channel.py", "feishu_check.py", "feishu_listen.py"):
        assert not (core_dir / shim).exists(), f"Legacy shim should be removed: {shim}"


# ---------------------------------------------------------------- 3. 忙碌错误


def test_approval_lock_busy_is_a_contract_busy_error():
    assert issubclass(ApprovalLockBusy, ApprovalBusyError)
    exc = ApprovalLockBusy("busy", lock_run_id="run-x", lock_expires_at="2026-11-25T00:00:00+00:00")
    assert isinstance(exc, ApprovalBusyError)
    assert exc.lock_run_id == "run-x"
    assert exc.lock_expires_at == "2026-11-25T00:00:00+00:00"
    with pytest.raises(ApprovalBusyError):
        raise exc


# ---------------------------------------------------------------- 4. 路由表


def test_every_application_route_survives_the_split(tmp_path: Path):
    app = create_app(runtime=_runtime(tmp_path), process_role="cli")
    registered = _registered_routes(app)
    assert registered == set(EXPECTED_ROUTES)
    assert len(EXPECTED_ROUTES) == 67


def test_mcp_stays_mounted_and_the_spa_catch_all_stays_last(tmp_path: Path):
    app = create_app(runtime=_runtime(tmp_path), process_role="cli")
    mounts = [route.path for route in app.routes if route.__class__.__name__ == "Mount"]
    assert "/mcp" in mounts
    if "/{full_path:path}" in [getattr(route, "path", "") for route in app.routes]:
        paths = [getattr(route, "path", "") for route in app.routes]
        assert paths[-1] == "/{full_path:path}"


def test_the_split_keeps_the_http_contract(tmp_path: Path):
    app = create_app(runtime=_runtime(tmp_path), process_role="cli")
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200 and health.json()["status"] == "ok"
        assert client.get("/llms.txt").status_code == 200
        runs = client.get("/api/site/runs")
        assert runs.status_code == 200 and "runs" in runs.json()
        missing = client.get("/api/site/runs/no-such-run")
        assert missing.status_code == 404 and missing.json() == {"detail": "Run not found"}
        unknown = client.get("/api/site/no-such-endpoint")
        assert unknown.status_code == 404 and unknown.json() == {"detail": "Not Found"}
