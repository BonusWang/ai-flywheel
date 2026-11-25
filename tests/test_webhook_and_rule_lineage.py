"""通用 Webhook 通道与轻量规则沿革（feat-webhook-approval-and-rule-lineage）。

断言分两组，每组都对应一条会失败的契约：

1. Webhook：签名对应原始 body、外发字符串先脱敏、`notify_on_fyi` 只控制 `fyi`、
   HTTP 500 / 超时不抛出、配置导出不泄露密钥、`doctor` 对空 URL 报 FAIL；
2. 规则沿革：旧库幂等补齐三列且不丢数据、替代即停用并审计、生效窗口随状态流转、
   自动休眠关闭窗口。

所有 HTTP 都走 `httpx.MockTransport`，不产生任何真实外网请求；所有数据库都是
`tmp_path` 下的临时文件。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_flywheel.adapters.channels.webhook import WebhookChannel
from ai_flywheel.adapters.delivery import DeliveryAdapter
from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig, WebhookDeliveryConfig, load_config
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.doctor import run_doctor
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.models import (
    CreateRuleRequest,
    PlannedWrite,
    RuleRecord,
    RuleStatus,
    RunStatus,
    WorkflowRunRecord,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import ImmutableFileStore


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config loading must not pick up the developer's own FLYWHEEL_* overrides."""
    for name in list(os.environ):
        if name.startswith("FLYWHEEL_"):
            monkeypatch.delenv(name, raising=False)


def _webhook_cfg(**overrides: object) -> WebhookDeliveryConfig:
    values: dict[str, object] = {
        "enabled": True,
        "url": "https://hook.example.test/flywheel",
        "secret": "unit-test-signing-secret",
    }
    values.update(overrides)
    return WebhookDeliveryConfig(**values)


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _key_like() -> str:
    """An API-key-shaped fixture; 'placeholder' keeps the public-repo hygiene gate happy."""
    return "sk-" + "placeholder" + "A" * 16


# ---------------------------------------------------------------- webhook channel


def test_webhook_signs_the_raw_body_and_sanitizes_every_string():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    secret = "unit-test-signing-secret"
    channel = WebhookChannel(_webhook_cfg(secret=secret), client=_mock_client(handler))
    key_like = _key_like()
    run = WorkflowRunRecord(
        run_id="run-webhook-1",
        batch_date="2026-11-25",
        status=RunStatus.AWAITING_APPROVAL,
    )

    result = channel.notify(
        event_type="awaiting_approval",
        run=run,
        title=f"待审批 {key_like}",
        summary=f"<private>内部备注</private> 摘要里也有 {key_like}",
        portal_url="http://127.0.0.1:8765/runs",
        reasons=[f"原因 {key_like}"],
        planned_writes=[
            PlannedWrite(
                target_kind="skill_improvement",
                action="update",
                title=f"Skill {key_like}",
                summary="Skill 变更",
                relative_path="skills/demo/SKILL.md",
                requires_approval=True,
            ),
            PlannedWrite(
                target_kind="knowledge_doc",
                action="create",
                title="一条普通笔记",
                summary="笔记",
                relative_path="topics/demo.md",
            ),
        ],
    )

    assert result == {"channel": "webhook", "sent": True, "status_code": 200, "reason": ""}
    assert len(captured) == 1
    request = captured[0]
    body = request.content

    expected_signature = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert request.headers["X-Flywheel-Signature"] == expected_signature
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["User-Agent"].startswith("ai-flywheel/")

    payload = json.loads(body.decode("utf-8"))
    assert payload["event_type"] == "awaiting_approval"
    assert payload["run_id"] == "run-webhook-1"
    assert payload["status"] == "AWAITING_APPROVAL"
    assert payload["portal_url"] == "http://127.0.0.1:8765/runs"
    assert payload["sent_at"]

    wire = json.dumps(payload, ensure_ascii=False)
    assert "<private>" not in wire
    assert "内部备注" not in wire
    assert key_like not in wire
    assert "[REDACTED]" in wire

    skill_write, note_write = payload["planned_writes"]
    assert {"action", "target_kind", "title", "relative_path", "sensitive"} <= set(skill_write)
    assert skill_write["sensitive"] is True
    assert skill_write["relative_path"] == "skills/demo/SKILL.md"
    assert note_write["sensitive"] is False


def test_webhook_omits_signature_when_no_secret_is_configured():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    channel = WebhookChannel(_webhook_cfg(secret=""), client=_mock_client(handler))
    result = channel.notify(event_type="run_completed", run_id="run-2", summary="完成")

    assert result["sent"] is True
    assert result["status_code"] == 204
    assert "X-Flywheel-Signature" not in captured[0].headers


def test_notify_on_fyi_gates_only_routine_notices():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    cfg = _webhook_cfg(notify_on_fyi=False)
    channel = WebhookChannel(cfg, client=_mock_client(handler))

    skipped = channel.notify(event_type="fyi", title="日常", summary="例行通知")
    assert skipped["sent"] is False
    assert "notify_on_fyi" in skipped["reason"]
    assert captured == []

    assert channel.notify(event_type="awaiting_approval", summary="等待审批")["sent"] is True
    assert channel.notify(event_type="run_failed", summary="失败")["sent"] is True
    assert len(captured) == 2

    cfg.notify_on_fyi = True
    assert channel.notify(event_type="fyi", title="日常", summary="例行通知")["sent"] is True
    assert len(captured) == 3
    assert json.loads(captured[2].content.decode("utf-8"))["event_type"] == "fyi"


def test_webhook_reports_http_500_and_timeout_without_raising():
    server_error = WebhookChannel(
        _webhook_cfg(),
        client=_mock_client(lambda request: httpx.Response(500, json={"error": "boom"})),
    )
    result = server_error.notify(event_type="run_failed", summary="失败原因")
    assert result["sent"] is False
    assert result["status_code"] == 500
    assert "500" in result["reason"]

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    timing_out = WebhookChannel(_webhook_cfg(), client=_mock_client(timeout_handler))
    result = timing_out.notify(event_type="run_failed", summary="失败原因")
    assert result["sent"] is False
    assert result["status_code"] is None
    assert "ReadTimeout" in result["reason"]


def test_webhook_is_a_no_op_when_disabled_or_without_url():
    called: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200)

    disabled = WebhookChannel(_webhook_cfg(enabled=False), client=_mock_client(handler))
    assert disabled.notify(event_type="run_failed", summary="x")["reason"] == "webhook disabled"

    no_url = WebhookChannel(_webhook_cfg(url=""), client=_mock_client(handler))
    assert no_url.notify(event_type="run_failed", summary="x")["reason"] == "webhook url is empty"

    assert called == []


def test_delivery_adapter_notify_fyi_respects_the_gate():
    cfg = FlywheelConfig()
    cfg.delivery.webhook.enabled = True
    cfg.delivery.webhook.url = "https://hook.example.test/flywheel"
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    adapter = DeliveryAdapter(
        cfg,
        webhook_channel=WebhookChannel(cfg.delivery.webhook, client=_mock_client(handler)),
    )

    assert adapter.notify_fyi(title="日常", summary="例行摘要")["sent"] is False
    assert captured == []

    cfg.delivery.webhook.notify_on_fyi = True
    assert adapter.notify_fyi(title="日常", summary="例行摘要")["sent"] is True
    assert json.loads(captured[0].content.decode("utf-8"))["event_type"] == "fyi"


def test_delivery_adapter_reports_a_failed_webhook_without_failing_the_run():
    cfg = FlywheelConfig()
    cfg.delivery.webhook.enabled = True
    cfg.delivery.webhook.url = "https://hook.example.test/flywheel"
    adapter = DeliveryAdapter(
        cfg,
        webhook_channel=WebhookChannel(
            cfg.delivery.webhook,
            client=_mock_client(lambda request: httpx.Response(503)),
        ),
    )
    run = WorkflowRunRecord(run_id="run-delivery", status=RunStatus.PASS)

    results = adapter.deliver(run, [])

    assert results["webhook"].startswith("FAILED")
    assert "503" in results["webhook"]


def test_webhook_url_and_secret_come_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLYWHEEL_WEBHOOK_URL", "https://hook.example.test/from-env")
    monkeypatch.setenv("FLYWHEEL_WEBHOOK_SECRET", "env-signing-secret")

    cfg = load_config(base_dir=tmp_path)

    assert cfg.delivery.webhook.url == "https://hook.example.test/from-env"
    assert cfg.delivery.webhook.secret == "env-signing-secret"


def test_site_connect_never_returns_the_webhook_secret(tmp_path: Path):
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.delivery.webhook.enabled = True
    cfg.delivery.webhook.url = "https://hook.example.test/flywheel"
    cfg.delivery.webhook.secret = "mask-me-please"
    runtime = FlywheelRuntime(cfg, skip_fts_sync=True)

    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/site/connect")

    assert response.status_code == 200
    webhook = response.json()["config"]["delivery"]["webhook"]
    assert webhook["secret"] == ""
    assert webhook["secret_configured"] is True
    assert "mask-me-please" not in response.text


def test_doctor_flags_a_webhook_without_url(tmp_path: Path):
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.delivery.webhook.enabled = True
    cfg.delivery.webhook.url = ""
    cfg.delivery.webhook.secret = "hidden-signing-value"

    channels = next(check for check in run_doctor(cfg)["checks"] if check["name"] == "channels")
    assert channels["status"] == "FAIL"
    assert "webhook" in channels["summary"].lower()
    assert "hidden-signing-value" not in channels["detail"]

    cfg.delivery.webhook.url = "https://hook.example.test/flywheel"
    channels = next(check for check in run_doctor(cfg)["checks"] if check["name"] == "channels")
    assert channels["status"] == "PASS"
    assert "signature=on" in channels["detail"]
    assert "hidden-signing-value" not in channels["detail"]


# ---------------------------------------------------------------- rule lineage


def _activation(tmp_path: Path) -> tuple[ActivationEngine, FlywheelDB]:
    cfg = FlywheelConfig(base_dir=tmp_path)
    db = FlywheelDB(tmp_path / "flywheel.db")
    fs_store = ImmutableFileStore(tmp_path / "receipts")
    policies = PolicyEngine(cfg, db)
    return ActivationEngine(cfg, db, fs_store, policies), db


def _legacy_rules_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE rules (
            rule_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            domain TEXT NOT NULL DEFAULT 'general',
            projects_json TEXT NOT NULL DEFAULT '["generic"]',
            triggers_json TEXT NOT NULL DEFAULT '[]',
            checklist_json TEXT NOT NULL DEFAULT '[]',
            forbidden_routes_json TEXT NOT NULL DEFAULT '[]',
            applicable_skills_json TEXT NOT NULL DEFAULT '[]',
            source_pointers_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'unverified_active',
            version INTEGER NOT NULL DEFAULT 1,
            origin_session_key TEXT,
            hit_count INTEGER NOT NULL DEFAULT 0,
            natural_pass_count INTEGER NOT NULL DEFAULT 0,
            recurrence_count INTEGER NOT NULL DEFAULT 0,
            revalidation_history_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO rules (rule_id, title, status, created_at, updated_at)
        VALUES ('rule-legacy', '旧规则', 'verified_active',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()


def test_legacy_rules_table_upgrades_without_data_loss(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    _legacy_rules_db(db_path)

    db = FlywheelDB(db_path)
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(rules)").fetchall()}

    assert {"supersedes_rule_id", "effective_from", "effective_to"} <= columns
    rule = db.get_rule("rule-legacy")
    assert rule is not None
    assert rule.title == "旧规则"
    assert rule.status == RuleStatus.VERIFIED_ACTIVE
    assert rule.supersedes_rule_id is None
    assert rule.effective_from is None
    assert rule.effective_to is None

    # Re-opening the same file is idempotent: no migration error, no data change.
    FlywheelDB(db_path)
    reopened = FlywheelDB(db_path).get_rule("rule-legacy")
    assert reopened is not None
    assert reopened.title == "旧规则"


def test_superseding_a_rule_retires_it_and_audits(tmp_path: Path):
    engine, db = _activation(tmp_path)
    old = engine.register_rule(
        CreateRuleRequest(rule_id="rule-old", title="旧规则", triggers=["发布"])
    )
    assert old.effective_from is not None
    assert old.effective_to is None

    new = engine.register_rule(
        CreateRuleRequest(
            rule_id="rule-new",
            title="新规则",
            triggers=["发布"],
            supersedes_rule_id="rule-old",
        )
    )
    assert new.supersedes_rule_id == "rule-old"
    assert new.effective_from is not None
    assert new.effective_to is None

    retired = db.get_rule("rule-old")
    assert retired is not None
    assert retired.status == RuleStatus.RETIRED
    assert retired.effective_to is not None
    assert any(
        entry.get("event") == "superseded" and entry.get("superseded_by") == "rule-new"
        for entry in retired.revalidation_history
    )

    audits = db.list_audit_events(entity_type="rule", entity_id="rule-old", limit=20)
    assert [event["event_type"] for event in audits].count("rule_superseded") == 1

    # A second rule pointing at an already-retired rule does not duplicate evidence.
    engine.register_rule(
        CreateRuleRequest(rule_id="rule-newer", title="更新规则", supersedes_rule_id="rule-old")
    )
    audits = db.list_audit_events(entity_type="rule", entity_id="rule-old", limit=20)
    assert [event["event_type"] for event in audits].count("rule_superseded") == 1
    still_retired = db.get_rule("rule-old")
    assert still_retired is not None
    assert still_retired.effective_to == retired.effective_to


def test_effective_window_follows_status_transitions(tmp_path: Path):
    engine, _ = _activation(tmp_path)
    rule = engine.register_rule(
        CreateRuleRequest(rule_id="rule-window", title="窗口规则", triggers=["部署"])
    )
    first_from = rule.effective_from
    assert first_from is not None

    dormant = engine.transition_rule_status("rule-window", RuleStatus.DORMANT, note="暂时停用")
    assert dormant is not None
    assert dormant.effective_to is not None
    assert dormant.effective_from == first_from

    awake = engine.transition_rule_status("rule-window", RuleStatus.UNVERIFIED_ACTIVE, note="重新启用")
    assert awake is not None
    assert awake.effective_to is None
    assert awake.effective_from is not None
    assert awake.effective_from >= first_from

    retired = engine.transition_rule_status("rule-window", RuleStatus.RETIRED)
    assert retired is not None
    assert retired.effective_to is not None

    revived = engine.transition_rule_status("rule-window", RuleStatus.VERIFIED_ACTIVE)
    assert revived is not None
    assert revived.effective_to is None
    assert revived.effective_from is not None
    assert revived.effective_from >= retired.effective_from


def test_auto_dormancy_closes_the_effective_window(tmp_path: Path):
    _, db = _activation(tmp_path)
    old_created = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    db.upsert_rule(
        RuleRecord(
            rule_id="rule-stale",
            title="长期零命中的规则",
            created_at=old_created,
            updated_at=old_created,
        )
    )

    demoted = db.mark_dormant_stale_rules(days=14)

    assert [item["rule_id"] for item in demoted] == ["rule-stale"]
    stored = db.get_rule("rule-stale")
    assert stored is not None
    assert stored.status == RuleStatus.DORMANT
    assert stored.effective_to is not None
