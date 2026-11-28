from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig, load_config
from ai_flywheel.core.receipts import OutcomeGateError
from ai_flywheel.mcp_server import build_mcp_server
from ai_flywheel.models import (
    CheckResult,
    CreateRuleRequest,
    OutcomeCheck,
    OutcomeOrigin,
    OutcomeStatus,
    PreflightRequest,
    QueueStatus,
    RecordOutcomeRequest,
    RegisterSessionRequest,
    RuleStatus,
    RunStatus,
    SessionStatus,
    VerificationKind,
)
from ai_flywheel.runtime import FlywheelRuntime


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def isolated_runtime(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = load_config(base_dir=tmp_path)
    return FlywheelRuntime(cfg)


def test_policy_decoupling_and_three_tier_override(tmp_path: Path):
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    home_override = tmp_path / "home-repo-profile"
    home_override.mkdir()
    (home_override / "user_preferences.yaml").write_text(
        "policy_id: user_preferences\naddress_as: Maintainer\n",
        encoding="utf-8",
    )

    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.profile.override_dir = str(home_override)
    rt = FlywheelRuntime(cfg)

    prefs = rt.policies.get_policy("user_preferences")
    assert prefs is not None
    assert prefs["address_as"] == "Maintainer"
    assert prefs["_source"] == "profile_override"

    # UI edit overrides Home Repo override, and reset restores Home Repo override
    rt.policies.update_policy("user_preferences", {"address_as": "Maintainer-Custom"})
    assert rt.policies.get_policy("user_preferences")["address_as"] == "Maintainer-Custom"
    rt.policies.reset_policy("user_preferences")
    assert rt.policies.get_policy("user_preferences")["address_as"] == "Maintainer"


def test_outcome_hard_gates_and_two_tier_activation(isolated_runtime: FlywheelRuntime):
    rt = isolated_runtime
    rule = rt.activation.register_rule(
        CreateRuleRequest(
            rule_id="rule-test-verify",
            title="接口改动必须真实验证",
            triggers=["api", "接口"],
            checklist=["真实发请求验证状态码"],
            origin_session_key="codex:origin-1",
        )
    )
    assert rule.status == RuleStatus.UNVERIFIED_ACTIVE

    # 1. Hard gate: claiming applied_rule_ids without matching evidence check must fail closed
    with pytest.raises(OutcomeGateError):
        rt.receipts.record_outcome(
            RecordOutcomeRequest(
                session_key="claude-code:sess-2",
                client="claude-code",
                status=OutcomeStatus.SUCCESS,
                summary="forgot check",
                checks=[],
                applied_rule_ids=["rule-test-verify"],
            )
        )

    # 2. Hard gate: claiming real pass with empty evidence must fail closed
    with pytest.raises(OutcomeGateError):
        rt.receipts.record_outcome(
            RecordOutcomeRequest(
                session_key="claude-code:sess-2",
                client="claude-code",
                status=OutcomeStatus.SUCCESS,
                summary="empty evidence",
                checks=[
                    OutcomeCheck(
                        name="curl check",
                        result=CheckResult.PASS,
                        kind=VerificationKind.REAL,
                        evidence=[],
                        rule_id="rule-test-verify",
                    )
                ],
                applied_rule_ids=["rule-test-verify"],
            )
        )

    # 3. Natural real pass on a different session promotes unverified_active -> verified_active
    pf = rt.activation.preflight_activate(
        PreflightRequest(task="修改用户 API 接口并测试", client="claude-code", session_id="sess-2")
    )
    assert any(r.rule_id == "rule-test-verify" for r in pf.matched_rules)

    rec = rt.receipts.record_outcome(
        RecordOutcomeRequest(
            session_key="claude-code:sess-2",
            client="claude-code",
            preflight_receipt_id=pf.receipt_id,
            origin=OutcomeOrigin.NATURAL,
            status=OutcomeStatus.SUCCESS,
            summary="API verified",
            checks=[
                OutcomeCheck(
                    name="HTTP 200 real check",
                    result=CheckResult.PASS,
                    kind=VerificationKind.REAL,
                    evidence=["GET /health -> 200 OK"],
                    rule_id="rule-test-verify",
                )
            ],
            applied_rule_ids=["rule-test-verify"],
        )
    )
    assert rec.revalidation_effects[0]["to"] == "verified_active"
    assert rt.db.get_rule("rule-test-verify").status == RuleStatus.VERIFIED_ACTIVE

    # 4. Recurrence fail downgrades verified_active -> needs_revision & enqueues retro
    rt.receipts.record_outcome(
        RecordOutcomeRequest(
            session_key="codex:sess-3",
            client="codex",
            origin=OutcomeOrigin.NATURAL,
            status=OutcomeStatus.FAILED,
            summary="regression happened",
            checks=[
                OutcomeCheck(
                    name="HTTP 200 real check",
                    result=CheckResult.FAIL,
                    kind=VerificationKind.REAL,
                    evidence=["GET /health -> 500"],
                    rule_id="rule-test-verify",
                )
            ],
            applied_rule_ids=["rule-test-verify"],
        )
    )
    assert rt.db.get_rule("rule-test-verify").status == RuleStatus.NEEDS_REVISION
    assert len(rt.db.list_retro_items()) >= 1


def test_thin_hooks_fail_open_and_retry_breaker(isolated_runtime: FlywheelRuntime):
    rt = isolated_runtime
    res = rt.hooks.handle(
        "codex",
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "turn_id": "t1",
            "prompt": "修复 SQL 查询性能并测试",
        },
    )
    assert res["continue"] is True
    assert res["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "[反偷懒·执行]" in res["hookSpecificOutput"]["additionalContext"]

    # Read-only tool stays silent
    ro = rt.hooks.handle(
        "claude-code",
        {"hook_event_name": "PreToolUse", "session_id": "s1", "turn_id": "t1", "tool_name": "Read", "tool_input": "a.py"},
    )
    assert ro == {"continue": True}

    # Identical failing tool call alerts on attempt 3 with hookSpecificOutput envelope
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "turn_id": "t1",
        "tool_name": "Bash",
        "tool_input": "python -c 'bad'",
    }
    assert "hookSpecificOutput" not in rt.hooks.handle("claude-code", payload)
    assert "hookSpecificOutput" not in rt.hooks.handle("claude-code", payload)
    third = rt.hooks.handle("claude-code", payload)
    assert third["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "死循环保护" in third["hookSpecificOutput"]["additionalContext"]
    assert len(rt.db.list_retro_items()) == 1


def test_p00_p07_approval_auto_and_fail_run_lock_safety(isolated_runtime: FlywheelRuntime):
    rt = isolated_runtime
    rt.register_session(
        RegisterSessionRequest(
            client="claude",
            session_id="desk-01",
            title="修复构建缓存失效导致部署报错",
            project="demo",
            summary="定位缓存报错并总结排查步骤",
            content="User: 构建报错修复。Assistant: 清理构建缓存并验证通过。",
            tags=["build", "fix"],
        )
    )

    # 1. Default mode pauses at AWAITING_APPROVAL after P03
    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.lock_released is False
    assert len(run.planned_writes) >= 1

    # 2. Approving resumes P04-P07, writes verified markdown, settles session, releases lock
    completed = rt.orchestrator.approve_and_resume(run.run_id)
    assert completed.status == RunStatus.PASS
    assert completed.lock_released is True
    assert rt.db.get_workflow_lock() is None
    assert rt.db.get_session("claude:desk-01").status == SessionStatus.PROCESSED
    assert len(rt.knowledge.list_documents()) >= 1

    # 3. Simulated P04 failure must mark P04 FAILED, P05-P07 SKIPPED, and release lock
    rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id="codex-02",
            title="故障演练会话",
            summary="测试 fail-run 释锁",
            content="模拟 P04 失败",
        )
    )
    failed = rt.orchestrator.start_run(
        trigger="test",
        auto_approve_override=True,
        simulate_fail_node="P04_WRITE_VERIFY",
    )
    assert failed.status == RunStatus.FAILED
    assert failed.lock_released is True
    assert rt.db.get_workflow_lock() is None
    statuses = {n.node_id: n.status.value for n in failed.nodes}
    assert statuses["P04_WRITE_VERIFY"] == "FAILED"
    assert statuses["P05_SETTLE"] == "SKIPPED"
    assert statuses["P06_REPORT"] == "SKIPPED"
    assert statuses["P07_DELIVER"] == "SKIPPED"


@pytest.mark.asyncio
async def test_mcp_tools_and_fastapi_endpoints(isolated_runtime: FlywheelRuntime):
    rt = isolated_runtime
    mcp = build_mcp_server(rt)
    # Direct MCP tool verification via official MCPServer
    tools = await mcp.list_tools()
    tool_map = {t.name: t for t in tools}
    tool_names = set(tool_map.keys())
    assert {
        "flywheel_register_session",
        "flywheel_ingest_session",
        "flywheel_preflight_activate",
        "flywheel_report_signal",
        "flywheel_record_outcome",
        "flywheel_session_intent",
        "flywheel_status",
    }.issubset(tool_names)
    # G-4 regression gate: record_outcome schema must expose strongly-typed OutcomeCheck definitions
    outcome_schema = json.dumps(tool_map["flywheel_record_outcome"].input_schema)
    assert "OutcomeCheck" in outcome_schema and "VerificationKind" in outcome_schema

    reg_res = await mcp.call_tool(
        "flywheel_ingest_session",
        {
            "client": "claude",
            "session_id": "mcp-001",
            "title": "MCP 联调测试",
            "summary": "验证 MCP 工具闭环",
        },
    )
    assert reg_res is not None
    assert rt.db.get_session("claude:mcp-001") is not None
    # The ingest tool also lands a real distill-queue item, not only a profile row.
    assert rt.db.list_session_queue_items(
        "claude:mcp-001", statuses=[QueueStatus.QUEUED]
    )

    intent_res = await mcp.call_tool(
        "flywheel_session_intent",
        {
            "action": "opt_out",
            "session_key": "claude:mcp-001",
            "reason": "联调验证显式不沉淀",
        },
    )
    assert intent_res is not None
    opted = rt.db.get_session("claude:mcp-001")
    assert opted is not None and opted.opt_out is True
    assert not rt.db.list_session_queue_items(
        "claude:mcp-001", statuses=[QueueStatus.QUEUED]
    )

    # FastAPI endpoints & MCP Streamable HTTP route
    app = create_app(runtime=rt)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        llms_text = client.get("/llms.txt").text
        assert "AI Flywheel" in llms_text
        ov = client.get("/api/site/overview")
        assert ov.status_code == 200
        assert ov.json()["metrics"]["sessions_total"] >= 1

        about_resp = client.get("/api/site/about")
        assert about_resp.status_code == 200
        about_data = about_resp.json()
        assert about_data["llms_txt"] == llms_text
        assert len(about_data["mcp_tools"]) == 7
        assert client.get("/api/v1/about").status_code == 200

        conn_resp = client.get("/api/site/connect")
        assert conn_resp.status_code == 200
        conn_json = conn_resp.json()
        client_ids = [c["id"] for c in conn_json["guides"]["clients"]]
        assert client_ids == ["codex", "claude-code", "claude"]
        assert len(conn_json["mcp_tools"]) == 7
        assert conn_json["config"]["runner"]["api"]["api_key"] == ""

        hook_resp = client.post(
            "/api/hooks/codex",
            json={"hook_event_name": "UserPromptSubmit", "session_id": "c1", "prompt": "debug mcp server"},
        )
        assert hook_resp.status_code == 200
        assert hook_resp.json()["continue"] is True

        # Real JSON-RPC initialize over Streamable HTTP /mcp (no trailing slash!)
        mcp_init = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest-client", "version": "1.0.0"},
                },
            },
        )
        assert mcp_init.status_code == 200
        body = json.loads(mcp_init.text)
        assert body["result"]["serverInfo"]["name"] == "ai-flywheel"
