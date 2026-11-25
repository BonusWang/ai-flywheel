"""Tests for hook noise filtering, normalized retro grouping, and profile-aware advisory."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.retro_advisory import (
    advise_retro_item,
    build_retro_group_key,
    is_benign_tool,
    is_text_edit_tool,
    normalize_retro_summary,
)
from ai_flywheel.models import ReportSignalRequest, RetroItem, SignalKind
from ai_flywheel.runtime import FlywheelRuntime

REPO_ROOT = Path(__file__).resolve().parent.parent


def _runtime(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = FlywheelConfig(base_dir=tmp_path)
    return FlywheelRuntime(cfg)


def test_retry_loop_normalization_merges_attempt_3_and_attempt_4():
    item_a = RetroItem(
        retro_id="r1",
        session_key="claude-code:s1",
        client="claude-code",
        kind=SignalKind.RETRY_LOOP,
        severity="warning",
        summary="同一工具 `mcp_ccd_turn__update_plan` 相同输入在本轮已连续尝试 3 次，触发反偷懒死循环保护。",
        tool_name="mcp_ccd_turn__update_plan",
        detail={"attempt": 3},
    )
    item_b = RetroItem(
        retro_id="r2",
        session_key="claude-code:s2",
        client="claude-code",
        kind=SignalKind.RETRY_LOOP,
        severity="warning",
        summary="同一工具 `mcp_ccd_turn__update_plan` 相同输入在本轮已连续尝试 4 次，触发反偷懒死循环保护。",
        tool_name="mcp_ccd_turn__update_plan",
        detail={"attempt": 4},
    )
    assert build_retro_group_key(item_a) == build_retro_group_key(item_b) == "retry_loop::mcp_ccd_turn__update_plan"
    assert normalize_retro_summary(item_a.summary) == normalize_retro_summary(item_b.summary)


def test_advisory_classifies_noise_vs_high_risk_vs_evidence_redline():
    prefs = {
        "non_negotiables": [
            "根因优先：先查清为什么，不做治标不治本的临时绕过",
            "先读权威源再动手，禁止凭印象臆测现有实现",
        ]
    }
    plan_loop = RetroItem(
        retro_id="r1",
        session_key="claude-code:s1",
        client="claude-code",
        kind=SignalKind.RETRY_LOOP,
        severity="warning",
        summary="同一工具 `mcp_ccd_turn__update_plan` 相同输入在本轮已连续尝试 4 次",
        tool_name="mcp_ccd_turn__update_plan",
        detail={"attempt": 4},
    )
    edit_sql = RetroItem(
        retro_id="r2",
        session_key="claude-code:s1",
        client="claude-code",
        kind=SignalKind.HIGH_RISK_TOOL,
        severity="high",
        summary="检测到高危命令模式 `DELETE FROM`（工具: Edit）",
        tool_name="Edit",
        detail={"pattern": "DELETE FROM"},
    )
    bash_rm = RetroItem(
        retro_id="r3",
        session_key="claude-code:s1",
        client="claude-code",
        kind=SignalKind.HIGH_RISK_TOOL,
        severity="high",
        summary="检测到高危命令模式 `rm -rf`（工具: Bash）",
        tool_name="Bash",
        detail={"pattern": "rm -rf"},
    )
    fact_fix = RetroItem(
        retro_id="r4",
        session_key="generic:s1",
        client="generic",
        kind=SignalKind.CUSTOM,
        severity="warning",
        summary="更正先前一条未经验证的断言，经实测证据推翻",
    )

    adv_plan = advise_retro_item(plan_loop, user_prefs=prefs)
    assert adv_plan["verdict"] == "ignore"
    assert adv_plan["attempt"] == 4

    adv_edit = advise_retro_item(edit_sql, user_prefs=prefs)
    assert adv_edit["verdict"] == "ignore"
    assert "编辑误报" in adv_edit["verdict_label"]

    adv_bash = advise_retro_item(bash_rm, user_prefs=prefs)
    assert adv_bash["verdict"] == "review"

    adv_fact = advise_retro_item(fact_fix, user_prefs=prefs)
    assert adv_fact["verdict"] == "keep"
    assert "权威源" in adv_fact["matched_preference"]


def test_pre_tool_hook_suppresses_benign_tools_and_text_edit_high_risk(tmp_path: Path):
    rt = _runtime(tmp_path)

    # 1. Benign MCP plan/list tools never trigger retry_loop even after 5 identical calls
    for _ in range(5):
        res = rt.hooks.handle(
            "claude-code",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "turn_id": "t1",
                "tool_name": "mcp_ccd_turn__update_plan",
                "tool_input": {"steps": ["a", "b"]},
            },
        )
        assert "hookSpecificOutput" not in res
    assert is_benign_tool("mcp_ccd_session_mgmt__list_sessions")
    assert is_text_edit_tool("Edit")

    # 2. Edit tool containing SQL text DELETE FROM does NOT fire high_risk_tool
    edit_res = rt.hooks.handle(
        "claude-code",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "turn_id": "t1",
            "tool_name": "Edit",
            "tool_input": {"old_string": "SELECT 1", "new_string": "DELETE FROM temp_items WHERE id = 1"},
        },
    )
    assert "hookSpecificOutput" not in edit_res
    assert rt.db.list_retro_items() == []

    # 3. Genuine Bash DELETE FROM DOES fire high_risk_tool
    bash_res = rt.hooks.handle(
        "claude-code",
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "turn_id": "t1",
            "tool_name": "Bash",
            "tool_input": {"command": "psql -c 'DELETE FROM orders'"},
        },
    )
    assert "hookSpecificOutput" in bash_res
    assert len(rt.db.list_retro_items()) == 1


def test_site_retro_endpoint_attaches_profile_advisory(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.receipts.report_signal(
        ReportSignalRequest(
            client="claude-code",
            session_id="s1",
            kind=SignalKind.RETRY_LOOP,
            severity="warning",
            summary="同一工具 `Bash` 相同输入在本轮已连续尝试 3 次",
            tool_name="Bash",
            detail={"attempt": 3},
        )
    )
    with TestClient(create_app(runtime=rt, process_role="cli")) as client:
        resp = client.get("/api/site/retro")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        adv = items[0]["advisory"]
        assert adv["group_key"] == "retry_loop::bash"
        assert adv["verdict"] == "review"
        assert adv["attempt"] == 3
        assert "根因" in adv["matched_preference"]
