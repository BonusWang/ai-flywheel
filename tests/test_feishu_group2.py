"""feat-feishu-approval-card-loop 第 2-5 组测试：清洗、渲染、发件箱、通道与点击受理。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_flywheel.adapters.lark_cli import LarkResult
from ai_flywheel.config import FeishuCliConfig, FlywheelConfig
from ai_flywheel.adapters.channels.feishu.card import (
    is_sensitive,
    render_ask_card,
    render_state_card,
    sanitize,
    with_notice,
)
from ai_flywheel.adapters.channels.feishu.channel import FeishuChannel
from ai_flywheel.core.orchestrator import ApprovalLockBusy
from ai_flywheel.models import NodeExecution, NodeStatus, PlannedWrite, RunStatus, WorkflowRunRecord
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.outbox import OutboxDB

T0 = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


def iso(minutes: float = 0) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


# ---------------- 清洗与渲染（2.4） ----------------

def test_sanitize_strips_and_fullwidths_and_masks():
    assert sanitize("a‮b​c") == "abc"  # 双向与零宽
    assert sanitize("x y\tz\r\nw") == "x y z w"
    assert sanitize("a&b|c<d>e^f%g!h") == "a＆b｜c＜d＞e＾f％g！h"
    assert sanitize("看看 http://127.0.0.1:8765/runs") .count("：/") == 1
    assert "sk-abcdef12345678" not in sanitize("key sk-abcdef12345678 here")
    assert "secret" not in sanitize("<private>secret stuff</private>ok")
    assert sanitize("") == "（空）"
    assert sanitize("a" * 20, limit=5).endswith("…")


def test_is_sensitive_predicate():
    assert is_sensitive({"target_kind": "skill_improvement"})
    assert is_sensitive({"target_kind": "knowledge_doc", "action": "delete"})
    assert is_sensitive({"target_kind": "knowledge_doc", "requires_approval": True})
    assert not is_sensitive({"target_kind": "knowledge_doc", "action": "create"})


def _write(kind="knowledge_doc", action="create", title="t", summary="s", preview=""):
    return {"target_kind": kind, "action": action, "relative_path": "lessons/x.md", "title": title, "summary": summary, "approval_reason": "", "content_preview": preview}


def test_render_ask_card_buttons_and_items():
    mixed = render_ask_card(card_id="c1", batch_label="2026-11-24 #01", reasons=["r"], writes=[_write(), _write(kind="skill_improvement", action="update", preview="p" * 900)], portal_url="http://127.0.0.1:8765/runs")
    assert mixed.count('"behaviors"') == 3  # 三个操作
    assert "仅同意常规项" in mixed
    sens_only = render_ask_card(card_id="c2", batch_label="b", reasons=["r"], writes=[_write(kind="skill_improvement")], portal_url="u")
    assert sens_only.count('"behaviors"') == 2
    assert "仅同意常规项" not in sens_only
    # 摘录与哈希存在、且注明 ≠ 全文
    assert "≠全文" in mixed and all(len(x) == 12 for x in __import__("re").findall(r'内容校验：([0-9a-f]{12})', mixed))
    # 体积预算：12 个超长敏感情项也不超 24KB，且一项不丢
    big = render_ask_card(card_id="c3", batch_label="b", reasons=["r"], writes=[_write(kind="skill_improvement", preview="长" * 5000) for _ in range(12)], portal_url="u")
    assert len(big.encode("utf-8")) <= 24 * 1024
    assert big.count("Skill · 新建") == 12  # 一项不丢


def test_render_state_card_no_buttons_and_busy_hint():
    card = render_state_card(card_id="c", state="done", result_summary="3 篇")
    assert "behaviors" not in card and "执行完成" in card
    busy = render_state_card(card_id="c", state="decided", busy_hint="另一批次正在执行，可稍后再点")
    assert "另一批次" in busy
    unavail = render_state_card(card_id="c", state="closed", unavailable=True)
    assert "请在电脑前" in unavail and "behaviors" not in unavail


# ---------------- 发件箱（2.1/2.2） ----------------

@pytest.fixture()
def db(tmp_path: Path) -> FlywheelDB:
    return FlywheelDB(tmp_path / "ob.db")


def _ensure_run(db: FlywheelDB, run_id: str) -> None:
    """A decision card points at a run, so the run row has to exist first."""
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO workflow_runs (run_id, trigger_source, phase, runner_mode, status, started_at)
            VALUES (?, 'test', 'daily', 'dry_run', 'AWAITING_APPROVAL', ?)
            ON CONFLICT(run_id) DO NOTHING
            """,
            (run_id, iso()),
        )


def _card(db: FlywheelDB, card_id="card-run-x", state="open", version=1) -> None:
    _ensure_run(db, "run-x")
    db.save_decision_card(card_id=card_id, run_id="run-x", kind="ask", state=state, title="t", body_markdown="b", options=["同意并写入", "取消本次写入"])
    if version > 1:
        with db.connect() as conn:
            conn.execute("UPDATE decision_cards SET version = ? WHERE card_id = ?", (version, card_id))


def test_outbox_claim_lease_and_backoff(db: FlywheelDB):
    ob = OutboxDB(db)
    _card(db)
    ob.register(card_id="card-run-x", channel="feishu_cli")
    assert ob.claim_for_send(delivery_id="del-card-run-x-feishu_cli", now=iso())
    assert not ob.claim_for_send(delivery_id="del-card-run-x-feishu_cli", now=iso())  # 占位互斥
    ob.mark_result(delivery_id="del-card-run-x-feishu_cli", status="retrying", now=iso(), error="e1")
    row = ob.get(card_id="card-run-x", channel="feishu_cli")
    assert row["tries"] == 1 and row["status"] == "retrying"
    assert row["next_attempt_at"] > iso()  # 退避在未来
    # 第 9 次后放弃
    for i in range(2, 10):
        ob.mark_result(delivery_id="del-card-run-x-feishu_cli", status="retrying", now=iso(i), error=f"e{i}")
    row = ob.get(card_id="card-run-x", channel="feishu_cli")
    assert row["status"] == "given_up" and row["tries"] == 9
    # 启动重排：卡片仍待决且没有可达行 → 重置
    assert ob.requeue_abandoned(now=iso(70)) == 1
    row = ob.get(card_id="card-run-x", channel="feishu_cli")
    assert row["status"] == "pending" and row["tries"] == 0 and row["first_try_at"] is None


def test_outbox_requeue_skips_when_reachable_row_exists(db: FlywheelDB):
    ob = OutboxDB(db)
    _card(db, "card-run-y")
    ob.register(card_id="card-run-y", channel="feishu_cli")
    ob.register(card_id="card-run-y", channel="feishu_text")
    ob.claim_for_send(delivery_id="del-card-run-y-feishu_text", now=iso())
    ob.mark_result(delivery_id="del-card-run-y-feishu_text", status="sent", now=iso(), external_ref="om_ok")
    for i in range(1, 10):
        ob.mark_result(delivery_id="del-card-run-y-feishu_cli", status="retrying", now=iso(i), error="e")
    assert ob.get(card_id="card-run-y", channel="feishu_cli")["status"] == "given_up"
    assert ob.requeue_abandoned(now=iso(70)) == 0  # 已有可达的文本行


def test_outbox_list_due_only_open_cards(db: FlywheelDB):
    ob = OutboxDB(db)
    _card(db, "card-open")
    _card(db, "card-decided", state="decided")
    ob.register(card_id="card-open", channel="feishu_cli")
    ob.register(card_id="card-decided", channel="feishu_cli")
    due = {r["card_id"] for r in ob.list_due(now=iso())}
    assert due == {"card-open"}


# ---------------- 通道（2.2/2.3/2.5/5.2） ----------------

class FakeRunner:
    def __init__(self):
        self.sends: list[tuple[str, str]] = []  # (kind, body)
        self.patches: list[str] = []
        self.send_status = "sent"
        self.fail = False

    def send_interactive(self, *, user_id, card_json, idempotency_key):
        self.sends.append(("interactive", card_json))
        if self.fail:
            return LarkResult("failed", error="boom")
        return LarkResult(self.send_status, message_id="om_i1")

    def send_text(self, *, user_id="", chat_id="", text, idempotency_key=""):
        self.sends.append(("text", text))
        if self.fail:
            return LarkResult("failed", error="boom")
        return LarkResult(self.send_status, message_id="om_t1")

    def patch_card(self, *, message_id, card_json):
        self.patches.append(card_json)
        return LarkResult("sent")


class FakeListener:
    def __init__(self, state="ready"):
        self.state = {"state": state, "reason": "", "ready": state == "ready"}


class FakeCheck:
    def __init__(self):
        self.status_value = {"state": "passed", "valid": True}

    def status(self):
        return dict(self.status_value)

    def handle_click(self, event):
        return True


def _channel(db: FlywheelDB, runner, listener_state="ready", *, role="serve", check=None, approve_fn=None, reject_fn=None, on_after_interactive_send=None) -> FeishuChannel:
    cfg = FlywheelConfig()
    cfg.delivery.feishu_cli = FeishuCliConfig(enabled=True, interactive=True, user_id="ou_me", approver_open_ids=["ou_me"])
    ob = OutboxDB(db)
    return FeishuChannel(
        cfg, db, ob, runner,
        listener=FakeListener(listener_state), check=check or FakeCheck(),
        approve_fn=approve_fn or (lambda *a, **k: None), reject_fn=reject_fn or (lambda *a, **k: None),
        process_role=role,
        on_after_interactive_send=on_after_interactive_send,
    )


def _run(run_id="run-x", writes=2) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=run_id, trigger="test", approval_mode="always", status=RunStatus.AWAITING_APPROVAL,
        batch_date="2026-11-24", batch_seq=1,
        planned_writes=[
            PlannedWrite(target_kind=("skill_improvement" if i == 0 else "knowledge_doc"), action="create", title=f"t{i}", summary=f"s{i}")
            for i in range(writes)
        ],
        nodes=[NodeExecution(node_id=f"P0{i}", name=f"P0{i}", actor="x", status=NodeStatus.PENDING) for i in range(8)],
    )


def test_first_send_interactive_then_degrade(db: FlywheelDB):
    runner = FakeRunner()
    db.save_workflow_run(_run())
    _card(db)
    ch = _channel(db, runner)
    ch.first_send(card_id="card-run-x", run=_run(), reasons=["r"], portal_url="http://h/runs")
    assert runner.sends[0][0] == "interactive"
    assert ch.outbox.get(card_id="card-run-x", channel="feishu_cli")["status"] == "sent"
    # down 状态：降级文本，交互行记 degraded
    runner2 = FakeRunner()
    _card(db, "card-run-d")
    db.save_workflow_run(_run())
    ch2 = _channel(db, runner2, listener_state="stopped")
    ch2.first_send(card_id="card-run-d", run=_run(), reasons=["r"], portal_url="http://h/runs")
    kinds = [k for k, _ in runner2.sends]
    assert kinds == ["text"]
    assert ch2.outbox.get(card_id="card-run-d", channel="feishu_cli")["status"] == "degraded"
    assert ch2.outbox.get(card_id="card-run-d", channel="feishu_text")["status"] == "sent"
    # 非 serve 进程：一律文本
    runner3 = FakeRunner()
    _card(db, "card-run-c")
    ch3 = _channel(db, runner3, role="cli")
    ch3.first_send(card_id="card-run-c", run=_run(), reasons=["r"], portal_url="http://h/runs")
    assert [k for k, _ in runner3.sends] == ["text"]


def test_first_send_transient_defers_without_try(db: FlywheelDB):
    runner = FakeRunner()
    _card(db)
    ch = _channel(db, runner, listener_state="backoff")
    ch.first_send(card_id="card-run-x", run=_run(), reasons=["r"], portal_url="u")
    assert runner.sends == []  # 暂缓：不发也不计尝试
    row = ch.outbox.get(card_id="card-run-x", channel="feishu_cli")
    assert row["status"] == "pending" and row["tries"] == 0 and row["next_attempt_at"] > ch.db._now_iso()


def test_click_answer_paths(db: FlywheelDB):
    runner = FakeRunner()
    run = _run()
    db.save_workflow_run(run)
    _card(db)
    ch = _channel(db, runner)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    calls: list[tuple] = []

    def approve(run_id, approved_indices=None, source="portal", selected_option=""):
        calls.append((run_id, approved_indices, source, selected_option))

    ch.approve_fn = approve
    # 全部同意
    ch.handle_click({"event_id": "e1", "operator_id": "ou_me", "message_id": "om_i1", "action_value": {"card": "card-run-x", "action": "approve_all"}})
    import time as _t
    _deadline = _t.time() + 5
    while _t.time() < _deadline and not calls:
        _t.sleep(0.05)
    assert calls == [("run-x", None, "feishu", "同意并写入")]
    # 仅同意常规项：下标为非敏感情项（本例 1..n-1）
    ch.handle_click({"event_id": "e2", "operator_id": "ou_me", "message_id": "om_i1", "action_value": {"card": "card-run-x", "action": "approve_routine"}})
    _deadline = _t.time() + 5
    while _t.time() < _deadline and len(calls) < 2:
        _t.sleep(0.05)
    assert calls[-1][1] == [1]
    # 非审批人 / 引用不符：拒绝并留审计，不执行
    ch.handle_click({"event_id": "e3", "operator_id": "ou_stranger", "message_id": "om_i1", "action_value": {"card": "card-run-x", "action": "approve_all"}})
    ch.handle_click({"event_id": "e4", "operator_id": "ou_me", "message_id": "om_other", "action_value": {"card": "card-run-x", "action": "approve_all"}})
    assert len(calls) == 2
    kinds = [e["event_type"] for e in db.list_audit_events(entity_type="decision_card", entity_id="card-run-x")]
    assert "answer_denied" in kinds
    # 重复事件按 event_id 去重（监听层已测；此处验证同一 event 不重复执行）
    # 去重发生在监听层（event_id），通道层见 tests/test_feishu_group1.py


def test_click_busy_hint_and_late(db: FlywheelDB):
    runner = FakeRunner()
    run = _run()
    db.save_workflow_run(run)
    _card(db)

    def busy(run_id, approved_indices=None, source="portal", selected_option=""):
        raise ApprovalLockBusy("另一运行持锁", lock_run_id="run-other", lock_expires_at=iso(30))

    ch = _channel(db, runner, approve_fn=busy)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    ch.handle_click({"event_id": "e1", "operator_id": "ou_me", "message_id": "om_i1", "action_value": {"card": "card-run-x", "action": "approve_all"}})
    import time as _t
    _deadline = _t.time() + 5
    while _t.time() < _deadline and not any("另一批次正在执行" in p for p in runner.patches):
        _t.sleep(0.05)
    assert any("另一批次正在执行" in p for p in runner.patches)
    # 提示写在待决卡上：按钮必须还在，且不拿锁到期时间当预计可重试时间
    assert any("另一批次正在执行" in p and '"type": "callback"' in p for p in runner.patches)
    assert "锁预计" not in "".join(runner.patches)
    kinds = [e["event_type"] for e in db.list_audit_events(entity_type="decision_card", entity_id="card-run-x")]
    assert "answer_busy" in kinds
    # 迟到回答：卡片已关
    with db.connect() as conn:
        conn.execute("UPDATE decision_cards SET state = 'closed' WHERE card_id = 'card-run-x'")
    ch.handle_click({"event_id": "e2", "operator_id": "ou_me", "message_id": "om_i1", "action_value": {"card": "card-run-x", "action": "approve_all"}})
    kinds = [e["event_type"] for e in db.list_audit_events(entity_type="decision_card", entity_id="card-run-x")]
    assert "answer_late" in kinds


def test_sync_after_terminal(db: FlywheelDB):
    runner = FakeRunner()
    run = _run()
    db.save_workflow_run(run)
    _card(db)
    ch = _channel(db, runner)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    # 终态：卡片版本加一（变更 A 的终态入口行为）
    db.claim_approval("run-x", approved_mask=[True] * 2, selected_option="同意并写入", source="portal", now=iso())
    db.finish_run_terminal("run-x", status="PASS", card_state="done", source="orchestrator", audit_event=("run_completed", "ok", {}), now=iso())
    ch._sync_cards()
    assert any("执行完成" in p for p in runner.patches)
    row = ch.outbox.get(card_id="card-run-x", channel="feishu_cli")
    card = ch._card_row("card-run-x")
    assert row["synced_version"] == card["version"]
    # 版本未变时不重复改卡
    n = len(runner.patches)
    ch._sync_cards()
    assert len(runner.patches) == n


# ---------------- 回写不得覆盖待决卡（真实验证暴露的缺陷） ----------------

def test_sync_never_overwrites_freshly_delivered_open_card(db: FlywheelDB):
    runner = FakeRunner()
    run = _run()
    db.save_workflow_run(run)
    _card(db)
    ch = _channel(db, runner)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    assert runner.sends and runner.sends[0][0] == "interactive"
    # 发件箱线程随后的每一轮都不得动这张待决卡：按钮必须留到有人点击
    for _ in range(3):
        ch.tick()
    assert runner.patches == []
    row = ch.outbox.get(card_id="card-run-x", channel="feishu_cli")
    assert row["synced_version"] == 1 and row["synced_avail"] == "ok"
    assert "behaviors" in row["ask_card_json"]


def test_listener_outage_removes_buttons_and_recovery_restores_them(db: FlywheelDB):
    runner = FakeRunner()
    run = _run()
    db.save_workflow_run(run)
    _card(db)
    ch = _channel(db, runner)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    assert runner.patches == []
    ch.listener.state = {"state": "stopped", "reason": "permanent failure", "ready": False}
    ch.tick()
    assert len(runner.patches) == 1
    assert "请在电脑前" in runner.patches[0] and "behaviors" not in runner.patches[0]
    ch.tick()
    assert len(runner.patches) == 1  # 状态不变不重复改卡
    ch.listener.state = {"state": "ready", "reason": "", "ready": True}
    ch.tick()
    assert len(runner.patches) == 2 and "behaviors" in runner.patches[1]  # 按钮恢复
    ch.tick()
    assert len(runner.patches) == 2


def test_transient_listener_state_keeps_buttons(db: FlywheelDB):
    runner = FakeRunner()
    run = _run()
    db.save_workflow_run(run)
    _card(db)
    ch = _channel(db, runner)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    ch.listener.state = {"state": "backoff", "reason": "transient", "ready": False}
    ch.tick()
    assert runner.patches == []  # 临时退避不算不可用


class _PatchFails(FakeRunner):
    def patch_card(self, *, message_id, card_json):
        self.patches.append(card_json)
        return LarkResult("failed", error="boom")


def test_sync_gives_up_after_nine_failures(db: FlywheelDB):
    runner = _PatchFails()
    run = _run()
    db.save_workflow_run(run)
    _card(db)
    ch = _channel(db, runner)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    with db.connect() as conn:
        conn.execute("UPDATE decision_cards SET state = 'closed', version = version + 1 WHERE card_id = 'card-run-x'")
    for _ in range(12):
        ch.tick()
    assert len(runner.patches) == 9
    assert ch.outbox.get(card_id="card-run-x", channel="feishu_cli")["given_up"] == 1


def test_legacy_outbox_migration_runs_once(tmp_path: Path):
    db = FlywheelDB(tmp_path / "m.db")
    _card(db, "card-open")
    _card(db, "card-closed", state="closed")
    # 旧的一次性投递路径写下的行：一条失败（卡片仍待决）、一条已送达（卡片已关闭）
    db.record_card_delivery(delivery_id="del-card-open-feishu", card_id="card-open", channel="feishu_cli", status="failed")
    db.record_card_delivery(
        delivery_id="del-card-closed-feishu", card_id="card-closed", channel="feishu_cli", status="sent", external_ref="om_old"
    )
    ob = OutboxDB(db)
    assert ob.get(card_id="card-open", channel="feishu_text")["status"] == "pending"  # 重排为文本
    assert ob.get(card_id="card-closed", channel="feishu_text")["status"] == "sent"  # 按文本处理且视为已同步
    assert ob.get(card_id="card-open", channel="feishu_cli") is None
    # 新代码写入的交互行：再次初始化不得被改写
    db.save_workflow_run(_run("run-y"))
    db.save_decision_card(card_id="card-run-y", run_id="run-y", kind="ask", state="open", title="t", body_markdown="b", options=["a", "b"])
    ob.register(card_id="card-run-y", channel="feishu_cli")
    assert ob.claim_for_send(delivery_id="del-card-run-y-feishu_cli")
    ob.mark_result(delivery_id="del-card-run-y-feishu_cli", status="sent", external_ref="om_new")
    with db.connect() as conn:
        conn.execute("UPDATE decision_cards SET state = 'closed' WHERE card_id = 'card-run-y'")
    ob_again = OutboxDB(db)
    row = ob_again.get(card_id="card-run-y", channel="feishu_cli")
    assert row is not None and row["status"] == "sent" and row["external_ref"] == "om_new"


# ---------------- 版式：分组、留白、按钮主次（飞书卡片排版规范） ----------------

def _item(kind="knowledge_doc", action="create", title="t", summary="s", path="lessons/x.md", reason="", preview=""):
    return {"target_kind": kind, "action": action, "relative_path": path, "title": title, "summary": summary, "approval_reason": reason, "content_preview": preview}


def test_ask_card_layout_groups_items_and_orders_actions():
    card = json.loads(
        render_ask_card(
            card_id="c9",
            batch_label="2026-11-24 #04",
            subtitle="批次 2026-11-24 #04 · 每次人工审批",
            reasons=["当前为每次人工审批模式（共 2 项）"],
            writes=[
                _item(title="笔记甲", summary="摘要甲", path="lessons/a.md"),
                _item(kind="skill_improvement", action="update", title="Skill 乙", preview="正文" * 50, reason="需要确认"),
            ],
            portal_url="http://127.0.0.1:8765/runs",
        )
    )
    header = card["header"]
    assert header["title"]["content"] == "AI 飞轮待审批"
    assert header["subtitle"]["content"].startswith("批次 2026-11-24 #04")
    assert header["icon"]["token"] == "approve_colorful"
    assert header["text_tag_list"][0]["text"]["content"] == "待审批"
    assert card["body"]["padding"] == "12px 12px 20px 12px"
    elements = card["body"]["elements"]
    tags = [e["tag"] for e in elements]
    # 每条写入是独立的描边块，不再挤成一行
    assert tags.count("interactive_container") == 2
    # 主操作独占一行撑满；其余两个并排；操作始终在最后
    assert tags[-2:] == ["button", "column_set"]
    main = elements[-2]
    assert main["type"] == "primary_filled" and main["width"] == "fill" and main["size"] == "large"
    side = [c["elements"][0] for c in elements[-1]["columns"]]
    assert [b["type"] for b in side] == ["default", "danger"]
    assert all(b["width"] == "fill" for b in side)
    # 块内：第一行是加粗的可信标签，其后全是纯文本
    first_block = next(e for e in elements if e["tag"] == "interactive_container")
    inner = first_block["elements"]
    assert inner[0]["tag"] == "markdown" and "Skill · 更新" in inner[0]["content"]
    assert all(e["text"]["tag"] == "plain_text" for e in inner[1:])
    dumped = json.dumps(card, ensure_ascii=False)
    assert "需你确认 · 1 项" in dumped and "常规项 · 1 项" in dumped
    assert "路径：lessons/a.md" in dumped and "摘要：摘要甲" in dumped
    assert "摘录（≠全文" in dumped and "理由：需要确认" in dumped


def test_ask_card_omits_empty_fields():
    card = render_ask_card(
        card_id="c",
        batch_label="b",
        reasons=["r"],
        writes=[_item(title="只有标题", summary="", path="lessons/x.md")],
        portal_url="u",
    )
    assert "（空）" not in card and "理由：" not in card and "摘要：" not in card
    assert "路径：lessons/x.md" in card


def test_two_actions_stack_full_width():
    card = json.loads(render_ask_card(card_id="c", batch_label="b", reasons=["r"], writes=[_item()], portal_url="u"))
    tail = card["body"]["elements"][-2:]
    assert [e["tag"] for e in tail] == ["button", "button"]
    assert [e["type"] for e in tail] == ["primary_filled", "danger"]
    assert all(e["width"] == "fill" for e in tail)


def test_with_notice_keeps_buttons_and_places_notice_above_actions():
    base = render_ask_card(card_id="c", batch_label="b", reasons=["r"], writes=[_item()], portal_url="u")
    with_n = json.loads(with_notice(base, "另一批次正在执行，请等它结束后再点。"))
    elements = with_n["body"]["elements"]
    first_action = next(i for i, e in enumerate(elements) if e["tag"] in ("button", "column_set"))
    assert "另一批次正在执行" in json.dumps(elements[first_action - 1], ensure_ascii=False)  # 紧贴在操作上方
    assert elements[-1]["tag"] == "button"  # 操作仍在最后
    assert sum(e["tag"] == "button" for e in elements) == 2


def test_check_card_uses_the_same_action_style():
    from ai_flywheel.adapters.channels.feishu.check import render_check_card

    waiting = json.loads(render_check_card("fcheck-x", state="waiting"))
    btn = waiting["body"]["elements"][-1]
    assert btn["type"] == "primary_filled" and btn["width"] == "fill"
    assert waiting["header"]["icon"]["token"] == "approve_colorful"
    settled = json.loads(render_check_card("fcheck-x", state="received"))
    assert all(e["tag"] != "button" for e in settled["body"]["elements"])


# ---------------- 回答后的即时反馈 ----------------

def test_text_fallback_is_sanitized(db: FlywheelDB):
    runner = FakeRunner()
    run = _run()
    run.planned_writes[1].title = "a&b <private>秘密</private>"
    run.planned_writes[1].summary = "x<y"
    _card(db)
    ch = _channel(db, runner, role="cli")  # 非 serve 进程：一律文本
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    assert runner.sends[0][0] == "text"
    text = runner.sends[0][1]
    assert "a＆b" in text and "x＜y" in text
    assert "<private>" not in text and "秘密" not in text


def test_answer_outcome_is_written_back_immediately(db: FlywheelDB):
    import time as _t

    runner = FakeRunner()
    run = _run()
    db.save_workflow_run(run)
    _card(db)

    def reject(run_id, reason="", source="portal"):
        db.claim_rejection(run_id, reason=reason, source=source, nodes_json="[]", digest_markdown="")

    ch = _channel(db, runner, reject_fn=reject)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    ch.handle_click(
        {"event_id": "e9", "operator_id": "ou_me", "message_id": "om_i1", "action_value": {"card": "card-run-x", "action": "reject"}}
    )
    deadline = _t.time() + 5
    while _t.time() < deadline and not any("已取消本次写入" in p for p in runner.patches):
        _t.sleep(0.05)
    # 不等 30 秒的发件箱周期：结论卡立刻回到同一张卡，且不带按钮
    assert any("已取消本次写入" in p and '"type": "callback"' not in p for p in runner.patches)
    events = db.list_audit_events(entity_type="workflow_run", entity_id="run-x")
    assert [e for e in events if e["event_type"] == "rejected"][0]["payload"]["source"] == "feishu"


def test_park_after_send_hook_leaves_row_unrecorded_until_released(db: FlywheelDB, tmp_path: Path):
    """发送成功后、消息编号记录前停住——投递行必须保持 sending 且无引用。"""
    import threading
    import time as _t

    park = tmp_path / "park.flag"
    released = threading.Event()

    def _hook(card_id: str, message_id: str) -> None:
        park.write_text(message_id, encoding="utf-8")
        released.wait(timeout=10)

    runner = FakeRunner()
    run = _run()
    db.save_workflow_run(run)
    _card(db)
    ch = _channel(db, runner, on_after_interactive_send=_hook)
    th = threading.Thread(target=lambda: ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u"))
    th.start()
    deadline = _t.time() + 5
    while _t.time() < deadline and not (park.exists() and park.read_text(encoding="utf-8")):
        _t.sleep(0.05)
    assert park.read_text(encoding="utf-8") == "om_i1"
    row = ch.outbox.get(card_id="card-run-x", channel="feishu_cli")
    assert row["status"] == "sending" and not row["external_ref"]
    released.set()
    th.join(timeout=5)
    row = ch.outbox.get(card_id="card-run-x", channel="feishu_cli")
    assert row["status"] == "sent" and row["external_ref"] == "om_i1"


# ---------------- W0.4：敏感情项、选项透传、文本退回重试与启动重排 ----------------

REPO_ROOT = Path(__file__).resolve().parent.parent


def _mixed_run(run_id="run-x") -> WorkflowRunRecord:
    """一个常规知识文档 + 一个需要审批的规则（敏感情项）。"""
    return WorkflowRunRecord(
        run_id=run_id, trigger="test", approval_mode="always", status=RunStatus.AWAITING_APPROVAL,
        batch_date="2026-11-24", batch_seq=1,
        planned_writes=[
            PlannedWrite(target_kind="knowledge_doc", action="create", title="常规笔记", summary="s0"),
            PlannedWrite(
                target_kind="flywheel_rule", action="create", title="规则", summary="s1",
                requires_approval=True, approval_reason="改变策略",
            ),
        ],
        nodes=[NodeExecution(node_id=f"P0{i}", name=f"P0{i}", actor="x", status=NodeStatus.PENDING) for i in range(8)],
    )


def test_card_writes_carry_the_requires_approval_flag(db: FlywheelDB):
    run = _mixed_run()
    ch = _channel(db, FakeRunner())
    writes = ch._card_writes(run)
    assert writes[0]["requires_approval"] is False
    assert writes[1]["requires_approval"] is True
    # 敏感情项谓词因此看得见需要审批的规则
    assert is_sensitive(writes[1]) is True
    assert is_sensitive(writes[0]) is False


def test_routine_approval_never_covers_sensitive_items(db: FlywheelDB):
    runner = FakeRunner()
    run = _mixed_run()
    db.save_workflow_run(run)
    _card(db)
    ch = _channel(db, runner)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    calls: list[tuple] = []

    def approve(run_id, approved_indices=None, source="portal", selected_option=""):
        calls.append((run_id, approved_indices, source, selected_option))

    ch.approve_fn = approve
    ch.handle_click(
        {"event_id": "e-routine", "operator_id": "ou_me", "message_id": "om_i1",
         "action_value": {"card": "card-run-x", "action": "approve_routine"}}
    )
    import time as _t
    _deadline = _t.time() + 5
    while _t.time() < _deadline and not calls:
        _t.sleep(0.05)
    assert calls, "点击必须被执行"
    run_id, indices, source, selected = calls[-1]
    assert run_id == "run-x"
    assert indices == [0], "需要审批的规则不得被『仅同意常规项』放行"
    assert source == "feishu"
    assert selected == "仅同意常规项"


def test_approve_all_passes_the_selected_option_through(db: FlywheelDB):
    runner = FakeRunner()
    run = _mixed_run()
    db.save_workflow_run(run)
    _card(db)
    ch = _channel(db, runner)
    ch.first_send(card_id="card-run-x", run=run, reasons=["r"], portal_url="u")
    calls: list[tuple] = []

    def approve(run_id, approved_indices=None, source="portal", selected_option=""):
        calls.append((run_id, approved_indices, source, selected_option))

    ch.approve_fn = approve
    ch.handle_click(
        {"event_id": "e-all", "operator_id": "ou_me", "message_id": "om_i1",
         "action_value": {"card": "card-run-x", "action": "approve_all"}}
    )
    import time as _t
    _deadline = _t.time() + 5
    while _t.time() < _deadline and not calls:
        _t.sleep(0.05)
    assert calls[-1][1] is None
    assert calls[-1][3] == "同意并写入"


def test_failed_text_fallback_is_retried_and_eventually_given_up(db: FlywheelDB):
    class _FailText(FakeRunner):
        def send_text(self, *, user_id="", chat_id="", text, idempotency_key=""):
            self.sends.append(("text", text))
            return LarkResult("failed", error="boom")

    runner = _FailText()
    _card(db)
    ch = _channel(db, runner, role="cli")  # 非 serve 进程：一律文本
    ch.first_send(card_id="card-run-x", run=_run(), reasons=["r"], portal_url="u")
    row = ch.outbox.get(card_id="card-run-x", channel="feishu_text")
    assert row["status"] == "retrying", "失败的文本退回必须进入重试，而不是停在 failed"
    assert row["tries"] == 1

    # 窗口耗尽后干净地放弃
    from datetime import datetime, timedelta, timezone as _tz

    later = (datetime.now(_tz.utc) + timedelta(minutes=90)).isoformat()
    ch.outbox.mark_result(
        delivery_id="del-card-run-x-feishu_text", status="retrying", now=later, error="boom"
    )
    row = ch.outbox.get(card_id="card-run-x", channel="feishu_text")
    assert row["status"] == "given_up" and row["given_up"] == 1


def test_abandoned_rows_are_requeued_once_at_startup(db: FlywheelDB):
    ch = _channel(db, FakeRunner())
    calls: list[int] = []
    original = ch.outbox.requeue_abandoned

    def _spy(**kwargs):
        calls.append(1)
        return original(**kwargs)

    ch.outbox.requeue_abandoned = _spy  # type: ignore[method-assign]
    ch.tick()
    assert calls == [], "每一轮 tick 不得重排：57 分钟窗口必须被尊重"
    ch.start()
    try:
        assert len(calls) == 1, "启动时重排一次"
    finally:
        ch.stop()


def test_portal_row_does_not_block_the_requeue_of_an_abandoned_feishu_delivery(db: FlywheelDB):
    ob = OutboxDB(db)
    _card(db)
    ob.register(card_id="card-run-x", channel="feishu_cli")
    # 门户行始终存在且已送达：它不是飞书投递，不得挡住飞书行的重排
    db.record_card_delivery(
        delivery_id="del-card-run-x-portal", card_id="card-run-x", channel="portal", status="sent"
    )
    for i in range(1, 10):
        ob.mark_result(delivery_id="del-card-run-x-feishu_cli", status="retrying", now=iso(i), error="e")
    assert ob.get(card_id="card-run-x", channel="feishu_cli")["status"] == "given_up"
    assert ob.requeue_abandoned(now=iso(70)) == 1
    assert ob.get(card_id="card-run-x", channel="feishu_cli")["status"] == "pending"


def test_legacy_feishu_delivery_uses_the_registered_delivery_id(tmp_path: Path):
    """没有飞书通道时的一次性投递，登记的投递编号必须与 OutboxDB.register 的约定一致。"""
    import shutil as _shutil

    from ai_flywheel.config import load_config
    from ai_flywheel.models import RegisterSessionRequest
    from ai_flywheel.runtime import FlywheelRuntime

    _shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = load_config(base_dir=tmp_path)
    # 交互模式关闭（身份不是 bot）：走一次性投递的旧分支
    cfg.delivery.feishu_cli = FeishuCliConfig(enabled=True, identity="user", user_id="ou_me")
    rt = FlywheelRuntime(cfg)
    assert rt.feishu_channel is None
    # The orchestrator sends the one-shot notice through `send_feishu_text_notice`.
    rt.delivery.send_feishu_text_notice = lambda **kwargs: {  # type: ignore[method-assign]
        "status": "sent", "external_ref": "om_legacy", "error": None,
    }
    rt.register_session(
        RegisterSessionRequest(
            client="claude-code", session_id="legacy-1", title="旧分支投递", project="demo",
            summary="摘要", content="User: a. Assistant: b.",
        )
    )
    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    with rt.db.connect() as conn:
        rows = conn.execute("SELECT delivery_id, channel FROM card_deliveries").fetchall()
    ids = {row["delivery_id"] for row in rows}
    assert f"del-card-{run.run_id}-feishu_cli" in ids
    assert f"del-card-{run.run_id}-feishu" not in ids


def test_orchestrator_records_the_selected_option_on_the_card(tmp_path: Path):
    """卡片上记下的必须是用户按下的那个选项，而不是写死的『同意并写入』。"""
    import shutil as _shutil

    from ai_flywheel.config import load_config
    from ai_flywheel.models import RegisterSessionRequest
    from ai_flywheel.runtime import FlywheelRuntime

    _shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = load_config(base_dir=tmp_path)
    rt = FlywheelRuntime(cfg)
    rt.register_session(
        RegisterSessionRequest(
            client="claude-code", session_id="opt-1", title="选项透传", project="demo",
            summary="摘要", content="User: a. Assistant: b.",
        )
    )
    run = rt.orchestrator.start_run(trigger="test", phase="daily")
    assert run.status == RunStatus.AWAITING_APPROVAL
    rt.orchestrator.approve_and_resume(run.run_id, source="feishu", selected_option="仅同意常规项")
    with rt.db.connect() as conn:
        row = conn.execute(
            "SELECT selected_option FROM decision_cards WHERE run_id = ? AND kind = 'ask'",
            (run.run_id,),
        ).fetchone()
    assert row["selected_option"] == "仅同意常规项"


# ---------------- 飞书交互审批闭环与纯文本降级修复回归 ----------------


def test_text_fallback_sanitizes_reasons_punctuation_and_chinese_kind_labels(db: FlywheelDB):
    """纯文本降级消息必须清洗 reasons、消除『。；』叠加并使用中文类型标签。"""
    runner = FakeRunner()
    run = _run()
    run.planned_writes.append(
        PlannedWrite(
            target_kind="flywheel_rule",
            relative_path=None,
            action="register_rule",
            title="路由规则",
            summary="固化边界",
            content_preview="规则正文",
        )
    )
    _card(db)
    ch = _channel(db, runner, role="serve", listener_state="ready")
    ch.cfg.delivery.feishu_cli.interactive = False
    secret_like = "sk-" + "9" * 32
    ch.first_send(
        card_id="card-run-x",
        run=run,
        reasons=[
            f"原因一含秘密 <private>内部隐私</private> {secret_like}。",
            "原因二尾部带句号。；追加说明。",
        ],
        portal_url="http://127.0.0.1:8765/runs",
    )
    assert runner.sends[0][0] == "text"
    text = runner.sends[0][1]
    assert "<private>" not in text and "内部隐私" not in text
    assert secret_like not in text and "疑似密钥已掩码" in text
    assert "。；" not in text
    assert "[笔记]" in text and "[Skill]" in text and "[规则]" in text
    assert "[knowledge_doc]" not in text and "[skill_improvement]" not in text and "[flywheel_rule]" not in text
    assert "当前未开启飞书交互卡片审批" in text
    assert "飞书按钮暂不可用" not in text


def test_text_fallback_footer_distinguishes_unverified_and_unavailable(db: FlywheelDB):
    """未通过自检与监听不可用时，纯文本尾注必须分别如实提示。"""
    from types import SimpleNamespace

    runner1 = FakeRunner()
    _card(db, "card-run-unverified", "run-unverified")
    run1 = _run("run-unverified")
    unverified_check = SimpleNamespace(status=lambda: {"valid": False})
    ch1 = _channel(db, runner1, role="serve", listener_state="ready", check=unverified_check)
    ch1.first_send(card_id="card-run-unverified", run=run1, reasons=["需审批"], portal_url="http://127.0.0.1:8765/runs")
    assert runner1.sends[0][0] == "text"
    assert "尚未通过链路自检" in runner1.sends[0][1]
    assert "飞书按钮暂不可用" not in runner1.sends[0][1]

    runner2 = FakeRunner()
    _card(db, "card-run-down", "run-down")
    run2 = _run("run-down")
    ch2 = _channel(db, runner2, role="serve", listener_state="stopped")
    ch2.first_send(card_id="card-run-down", run=run2, reasons=["需审批"], portal_url="http://127.0.0.1:8765/runs")
    assert runner2.sends[0][0] == "text"
    assert "飞书按钮暂不可用" in runner2.sends[0][1]


def test_normalizer_append_reason_avoids_double_punctuation():
    from ai_flywheel.adapters.runners.normalizers import _append_reason

    assert _append_reason("已有技能覆盖主体流程。", "在现有文件末尾追加小节") == "已有技能覆盖主体流程；在现有文件末尾追加小节"
    assert _append_reason("原因A；", "；原因B") == "原因A；原因B"
    assert _append_reason("", "补充说明") == "补充说明"


def test_feishu_check_fails_fast_when_interactive_off(tmp_path: Path):
    import time as _t

    from ai_flywheel.adapters.channels.feishu.check import FeishuCheckService

    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.delivery.feishu_cli = FeishuCliConfig(enabled=True, interactive=False, user_id="ou_me")
    db = FlywheelDB(tmp_path / "f.db")
    runner = FakeRunner()
    runner.target_path = str(tmp_path / "fake.exe")
    runner.version = lambda: "1.0.68"
    svc = FeishuCheckService(cfg, runner, {"state": "stopped", "reason": "interactive mode off", "ready": False}, db=db)
    t0 = _t.monotonic()
    res = svc.run(wait_seconds=1.0)
    elapsed = _t.monotonic() - t0
    assert res["state"] == "failed"
    assert "interactive mode is off" in res["reason"]
    assert elapsed < 2.0


def test_feishu_check_api_metadata_and_400_when_interactive_off(tmp_path: Path):
    import shutil as _shutil
    import sys

    from fastapi.testclient import TestClient

    from ai_flywheel.app import create_app
    from ai_flywheel.config import load_config
    from ai_flywheel.runtime import FlywheelRuntime

    _shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = load_config(base_dir=tmp_path)
    cfg.delivery.feishu_cli = FeishuCliConfig(
        enabled=True,
        interactive=False,
        identity="bot",
        user_id="ou_me",
        command=sys.executable,
    )
    rt = FlywheelRuntime(cfg, skip_fts_sync=True)
    client = TestClient(create_app(rt))

    r_get = client.get("/api/site/feishu-check")
    assert r_get.status_code == 200
    body = r_get.json()
    assert body["enabled"] is True
    assert body["interactive"] is False
    assert body["interaction_ready"] is False
    assert body["availability"] == "down:config"

    r_post = client.post("/api/site/feishu-check", json={"wait_seconds": 10})
    assert r_post.status_code == 400
    assert "交互" in r_post.json()["detail"]

    r_cfg = client.put(
        "/api/site/config",
        json={"delivery": {"feishu_cli": {"interactive": True, "approver_open_ids": ["ou_other"]}}},
    )
    assert r_cfg.status_code == 200
    r_get2 = client.get("/api/site/feishu-check")
    assert r_get2.status_code == 200
    body2 = r_get2.json()
    assert body2["interactive"] is True
    assert body2["interaction_ready"] is True


def test_feishu_listener_wake_responds_immediately_on_config_enable():
    import time as _t

    from ai_flywheel.adapters.channels.feishu.listen import FeishuClickListener

    enabled = [False]
    started = []

    class DummyRunner:
        def consume_args(self):
            return ["consume"]

        def popen(self, args):
            started.append(args)
            raise RuntimeError("stop-after-start")

    listener = FeishuClickListener(
        DummyRunner(),  # type: ignore[arg-type]
        on_click=lambda ev: None,
        should_run=lambda: enabled[0],
        transient_backoff=5.0,
    )
    listener.start()
    try:
        deadline = _t.monotonic() + 2.0
        while _t.monotonic() < deadline and listener.state["reason"] != "interactive mode off":
            _t.sleep(0.02)
        assert listener.state["reason"] == "interactive mode off"

        t0 = _t.monotonic()
        enabled[0] = True
        listener.wake()
        deadline = _t.monotonic() + 1.5
        while _t.monotonic() < deadline and not started:
            _t.sleep(0.02)
        assert started and (_t.monotonic() - t0) < 1.5
    finally:
        listener.stop()
