"""feat-distill-queue-and-inventory：沉淀队列状态机、入库触发与 P00–P07 消费闭环。

覆盖：
1. Codex 严格归档制：活跃目录不入队、归档入队、移出撤回、增长生成增量 revision；
2. Claude Code 退出触发制：Stop 不入队、SessionEnd 入队、resume / 新提示撤回；
3. MCP `flywheel_session_intent`：显式入队绕过初筛、opt_out 一票否决、withdraw 可再入队；
4. 超大上下文 0-token 瘦身与分片：尾部结论不丢；
5. P00–P07 队列消费：认领、结算、水位线推进与轨迹查询；
6. 失败重试、死信隔离与审批取消零惩罚退回；
7. 规则重筛 dry_run 只读预览与实际应用；
8. 队列控制面 REST API 全链路；
9. `hand_distilled` 双轨意图、结构化摘要与受控写入派单拦截。

会话、项目与技能名全部是虚构示例。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig, ScanSourceConfig, SessionTriageConfig
from ai_flywheel.core.pipeline.events import EventBroker
from ai_flywheel.core.queue_service import compute_content_hash
from ai_flywheel.core.triage import is_controlled_write_dispatch, triage_session
from ai_flywheel.models import (
    DistillQueueItem,
    IngressChannel,
    QueueStatus,
    RegisterSessionRequest,
    RunStatus,
    SessionRecord,
    SessionStatus,
)
from ai_flywheel.runtime import FlywheelRuntime

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A realistic conversation: a deploy failure, its diagnosis and the verified fix.
CONTENT = (
    "用户：部署时报错，连接池超时，请排查。"
    "助手：先看日志，再核对连接池上限，修复后重新部署，测试验收通过。"
)


# ---------------------------------------------------------------- helpers


def _runtime(tmp_path: Path, *, retries: int | None = None) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills = tmp_path / "skills"
    skills.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills)]
    # Keep the deterministic triage gate easy to satisfy: the scenarios below
    # target the queue state machine, not the noise thresholds.
    cfg.sessions.triage.min_chars = 20
    cfg.sessions.triage.gray_workspace_min_chars = 40
    if retries is not None:
        cfg.sessions.queue_max_retries = retries
    return FlywheelRuntime(cfg, skip_fts_sync=True)


def _write_jsonl(path: Path, turns: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "type": "message",
                "message": {"role": role, "content": [{"type": "text", "text": text}]},
            }
        )
        for role, text in turns
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _enqueue(
    rt: FlywheelRuntime,
    session_id: str = "q-1",
    *,
    client: str = "claude-code",
    project: str = "demo",
    title: str = "部署报错修复",
    content: str = CONTENT,
    priority: str = "P0_CORE",
) -> DistillQueueItem:
    items = rt.queue.on_explicit_enqueue(
        client=client,
        session_id=session_id,
        project=project,
        title=title,
        content=content,
        priority=priority,
    )
    assert len(items) == 1, items
    return items[0]


def _statuses(rt: FlywheelRuntime, session_key: str) -> list[str]:
    return [item.status.value for item in rt.db.list_session_queue_items(session_key)]


def _big_dialogue(conclusion: str, rounds: int = 16) -> str:
    """A transcript far beyond one item: long tool output plus a final conclusion."""
    parts: list[str] = []
    for i in range(rounds):
        parts.append(f"[user] 第 {i} 轮：请排查这个报错并给出修复方案。" + "补充上下文说明。" * 40)
        verbose = "".join(
            f"tool-output-line-{i}-{j:04d} 构建日志与堆栈信息\n" for j in range(120)
        )
        parts.append(f"[assistant] {verbose}中间结论：修复第 {i} 步。")
    parts.append("[user] 最后请给出最终结论。")
    parts.append(f"[assistant] {conclusion}")
    return "\n\n".join(parts)


class _RecordingBroker(EventBroker):
    """An event broker that remembers what the pipeline published."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict]] = []

    def publish(self, event_type: str, data: dict) -> None:  # type: ignore[override]
        self.events.append((event_type, data))
        super().publish(event_type, data)


# ---------------------------------------------------------------- 1. Codex 严格归档制


def test_codex_strict_archive_enqueue_withdraw_and_incremental_revision(tmp_path: Path):
    """活跃会话不入队；进了归档目录才入队，移出即撤回，写回后生成 rev 2。"""
    rt = _runtime(tmp_path)
    active = tmp_path / "active_sessions"
    archive = tmp_path / "archived_sessions"
    active.mkdir()
    archive.mkdir()
    rt.cfg.sessions.codex_archive_sources = [
        ScanSourceConfig(client="codex", path=str(archive))
    ]

    first_turns = [
        ("user", "排查报错：部署时报错，连接池超时。"),
        ("assistant", "先看日志，再核对连接池上限，修复后重新部署验证通过。"),
    ]
    _write_jsonl(active / "roll-1.jsonl", first_turns)

    # An active, unarchived session is never queued by the strict archive sync.
    report = rt.queue.reconcile_codex_archives()
    assert report["scanned"] == 0
    assert report["enqueued"] == 0
    assert rt.db.list_session_queue_items("codex:roll-1") == []

    # Appearing in the archive directory enqueues a codex_archive item.
    shutil.move(str(active / "roll-1.jsonl"), str(archive / "roll-1.jsonl"))
    report = rt.queue.reconcile_codex_archives()
    assert report["enqueued"] == 1
    queued = rt.db.list_session_queue_items("codex:roll-1", statuses=[QueueStatus.QUEUED])
    assert len(queued) == 1
    assert queued[0].ingress_channel is IngressChannel.CODEX_ARCHIVE
    assert queued[0].revision == 1
    assert "连接池超时" in rt.queue.load_queue_item_text(queued[0])

    # Removing the file before consumption withdraws the still-queued item.
    shutil.move(str(archive / "roll-1.jsonl"), str(active / "roll-1.jsonl"))
    report = rt.queue.reconcile_codex_archives()
    assert report["withdrawn"] == 1
    assert _statuses(rt, "codex:roll-1") == ["withdrawn"]
    withdrawn = rt.db.list_session_queue_items("codex:roll-1")[0]
    assert withdrawn.revision == 1
    events = rt.db.list_audit_events(
        entity_type="queue_item", entity_id=withdrawn.queue_id, limit=50
    )
    assert any(
        e["to_status"] == "withdrawn" and e["reason_code"] == "CODEX_UNARCHIVED" for e in events
    )

    # A grown conversation that is archived again becomes revision 2; revision 1
    # stays withdrawn and is never consumed.
    _write_jsonl(
        active / "roll-1.jsonl",
        [
            *first_turns,
            ("user", "补充：后续又出现了缓存异常的报错。"),
            ("assistant", "最终结论：缓存失效策略需要加上 TTL 校验，部署验收通过。"),
        ],
    )
    shutil.move(str(active / "roll-1.jsonl"), str(archive / "roll-1.jsonl"))
    report = rt.queue.reconcile_codex_archives()
    assert report["enqueued"] == 1
    items = rt.db.list_session_queue_items("codex:roll-1")
    assert [item.revision for item in items] == [2, 1]
    assert [item.status for item in items] == [QueueStatus.QUEUED, QueueStatus.WITHDRAWN]
    assert "TTL 校验" in rt.queue.load_queue_item_text(items[0])


def test_codex_archive_sync_reports_a_missing_source_without_failing(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.cfg.sessions.codex_archive_sources = [
        ScanSourceConfig(client="codex", path=str(tmp_path / "not-there"))
    ]
    report = rt.queue.reconcile_codex_archives()
    assert report["enqueued"] == 0
    assert any(error.startswith("archive_source_missing:") for error in report["errors"])


# ---------------------------------------------------------------- 2. Claude Code 退出触发制


def test_claude_code_queues_on_session_end_not_stop_and_withdraws_on_resume(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:cc-1",
            client="claude-code",
            session_id="cc-1",
            title="部署报错修复",
            project="demo",
            content=CONTENT,
            status=SessionStatus.PENDING,
        )
    )

    # `Stop` fires after every reply: it must never enqueue.
    rt.hooks.handle("claude-code", {"hook_event_name": "Stop", "session_id": "cc-1"})
    assert rt.db.list_session_queue_items("claude-code:cc-1") == []

    # `SessionEnd` queues the finished conversation as a claude_exit item.
    rt.hooks.handle("claude-code", {"hook_event_name": "SessionEnd", "session_id": "cc-1"})
    queued = rt.db.list_session_queue_items(
        "claude-code:cc-1", statuses=[QueueStatus.QUEUED]
    )
    assert len(queued) == 1
    assert queued[0].ingress_channel is IngressChannel.CLAUDE_EXIT

    # Resuming the conversation withdraws the not-yet-consumed item.
    rt.hooks.handle(
        "claude-code",
        {"hook_event_name": "SessionStart", "session_id": "cc-1", "source": "resume"},
    )
    assert _statuses(rt, "claude-code:cc-1") == ["withdrawn"]

    # The conversation grows again, so the next exit queues revision 2 (revision
    # 1 stays withdrawn); a following prompt withdraws the new item too.
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:cc-1",
            client="claude-code",
            session_id="cc-1",
            title="部署报错修复",
            project="demo",
            content=CONTENT + "用户：补充缓存异常。助手：最终结论是补上 TTL 校验并通过验收。",
            status=SessionStatus.PENDING,
        )
    )
    rt.hooks.handle("claude-code", {"hook_event_name": "SessionEnd", "session_id": "cc-1"})
    assert _statuses(rt, "claude-code:cc-1") == ["queued", "withdrawn"]
    prompt = rt.hooks.handle(
        "claude-code",
        {"hook_event_name": "UserPromptSubmit", "session_id": "cc-1", "prompt": "继续排查这个报错"},
    )
    assert _statuses(rt, "claude-code:cc-1") == ["withdrawn", "withdrawn"]

    # `UserPromptSubmit` injects the canonical session key into the context header.
    header = prompt["hookSpecificOutput"]["additionalContext"].splitlines()[0]
    assert "session=claude-code:cc-1" in header


def test_a_resume_event_without_the_resume_source_does_not_withdraw(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:cc-2",
            client="claude-code",
            session_id="cc-2",
            title="部署报错修复",
            project="demo",
            content=CONTENT,
            status=SessionStatus.PENDING,
        )
    )
    rt.hooks.handle("claude-code", {"hook_event_name": "SessionEnd", "session_id": "cc-2"})
    rt.hooks.handle(
        "claude-code",
        {"hook_event_name": "SessionStart", "session_id": "cc-2", "source": "startup"},
    )
    assert _statuses(rt, "claude-code:cc-2") == ["queued"]


# ---------------------------------------------------------------- 3. MCP 显式声明


@pytest.mark.asyncio
async def test_mcp_session_intent_enqueue_opt_out_withdraw_and_session_key_alignment(
    tmp_path: Path,
):
    rt = _runtime(tmp_path)
    rt.cfg.sessions.triage.min_chars = 200

    # The heuristic triage would drop this short transcript as low signal...
    tiny = tmp_path / "tiny-session.jsonl"
    _write_jsonl(tiny, [("user", "你好"), ("assistant", "你好")])
    short = "用户：你好。助手：你好。"
    vetoed = rt.queue.on_archive(
        "claude-code", "gray-1", project="generic", transcript_path=str(tiny)
    )
    assert vetoed["action"] == "excluded"
    assert vetoed["reason_code"] == "TOO_SHORT"

    # ...but an explicit MCP enqueue bypasses the noise filter at P0.
    enqueued = await rt.mcp.call_tool(
        "flywheel_session_intent",
        {
            "action": "enqueue",
            "session_key": "claude-code:gray-1",
            "title": "手工沉淀请求",
            "project": "generic",
            "content": short,
        },
    )
    assert enqueued is not None
    queued = rt.db.list_session_queue_items(
        "claude-code:gray-1", statuses=[QueueStatus.QUEUED]
    )
    assert len(queued) == 1
    assert queued[0].priority == "P0_CORE"
    assert queued[0].ingress_channel is IngressChannel.MCP_EXPLICIT

    # `opt_out` vetoes every future ARCHIVE / SessionEnd as USER_OPT_OUT.
    await rt.mcp.call_tool(
        "flywheel_session_intent",
        {
            "action": "opt_out",
            "session_key": "claude-code:gray-1",
            "reason": "本会话不沉淀",
        },
    )
    assert rt.db.get_session("claude-code:gray-1").opt_out is True
    statuses = _statuses(rt, "claude-code:gray-1")
    assert QueueStatus.QUEUED.value not in statuses
    assert QueueStatus.EXCLUDED.value in statuses
    archived_after_veto = rt.queue.on_archive(
        "claude-code", "gray-1", project="demo", content=CONTENT + "#no-flywheel"
    )
    assert archived_after_veto["action"] == "excluded"
    assert archived_after_veto["reason_code"] == "USER_OPT_OUT"
    assert QueueStatus.QUEUED.value not in _statuses(rt, "claude-code:gray-1")

    # `withdraw` pauses a queued item without banning a later re-enqueue.
    _enqueue(rt, "w-1")
    assert _statuses(rt, "claude-code:w-1") == ["queued"]
    await rt.mcp.call_tool(
        "flywheel_session_intent",
        {"action": "withdraw", "session_key": "claude-code:w-1"},
    )
    assert _statuses(rt, "claude-code:w-1") == ["withdrawn"]
    await rt.mcp.call_tool(
        "flywheel_session_intent",
        {
            "action": "enqueue",
            "session_key": "claude-code:w-1",
            "project": "demo",
            "content": CONTENT,
        },
    )
    assert _statuses(rt, "claude-code:w-1") == ["queued", "withdrawn"]

    # The hook header carries the canonical session key the model passes back to
    # MCP, so both surfaces hit exactly the same session record.
    prompt = rt.hooks.handle(
        "claude-code",
        {"hook_event_name": "UserPromptSubmit", "session_id": "align-1", "prompt": "排查部署报错"},
    )
    header = prompt["hookSpecificOutput"]["additionalContext"].splitlines()[0]
    aligned_key = next(
        token[len("session=") :] for token in header.split(" · ") if token.startswith("session=")
    )
    assert aligned_key == "claude-code:align-1"
    await rt.mcp.call_tool(
        "flywheel_session_intent",
        {"action": "opt_out", "session_key": aligned_key, "reason": "联调验证"},
    )
    aligned = rt.db.get_session("claude-code:align-1")
    assert aligned is not None and aligned.opt_out is True


def test_an_explicit_opt_out_marker_vetoes_archiving_with_an_audit_trail(tmp_path: Path):
    rt = _runtime(tmp_path)
    result = rt.queue.on_archive(
        "claude-code",
        "veto-1",
        project="demo",
        content=CONTENT + "\n用户：#no-flywheel 本会话不沉淀。",
    )
    assert result["action"] == "excluded"
    assert result["reason_code"] == "USER_OPT_OUT"
    assert not rt.db.list_session_queue_items(
        "claude-code:veto-1", statuses=[QueueStatus.QUEUED]
    )
    assert rt.db.get_session("claude-code:veto-1").opt_out is True
    excluded = rt.db.list_session_queue_items("claude-code:veto-1")[0]
    assert excluded.status is QueueStatus.EXCLUDED
    events = rt.db.list_audit_events(
        entity_type="queue_item", entity_id=excluded.queue_id, limit=50
    )
    assert any(
        e["to_status"] == "excluded" and e["reason_code"] == "USER_OPT_OUT" for e in events
    )


# ---------------------------------------------------------------- 4. 超大上下文


def test_oversized_transcript_is_compacted_and_split_without_losing_the_tail(tmp_path: Path):
    rt = _runtime(tmp_path)
    conclusion = "FINAL-CONCLUSION：根因是缓存失效策略缺失，修复后验收通过。"
    transcript = _big_dialogue(conclusion)
    assert len(transcript) > 60_000

    result = rt.queue.on_archive(
        "claude-code", "big-1", project="demo", title="超大上下文排查", content=transcript
    )
    assert result["action"] == "enqueued"
    items = result["items"]
    assert len(items) > 1
    assert [item.chunk_index for item in items] == list(range(1, len(items) + 1))
    assert all(item.chunk_total == len(items) for item in items)
    assert all(item.is_oversized for item in items)
    assert items[0].total_chars > 60_000

    loaded = [rt.queue.load_queue_item_text(item) for item in items]
    # The verbose tool output was compacted deterministically, without a model call.
    assert any("chars compacted" in text for text in loaded)
    assert all(len(text) <= rt.cfg.sessions.queue_max_item_chars for text in loaded)
    # The final conclusion lives in the last chunk: nothing is silently truncated.
    assert conclusion in loaded[-1]


# ---------------------------------------------------------------- 5. P00–P07 队列消费


def test_pipeline_claims_settles_and_advances_the_consumption_watermark(tmp_path: Path):
    rt = _runtime(tmp_path)
    item = _enqueue(rt, "pipe-1")
    broker = _RecordingBroker()
    rt.broker = broker  # type: ignore[assignment]
    rt.orchestrator.broker = broker  # type: ignore[assignment]
    rt.orchestrator.queue.broker = broker  # type: ignore[assignment]

    run = rt.orchestrator.start_run(
        trigger="test", queue_ids=[item.queue_id], auto_approve_override=True
    )
    assert run.status == RunStatus.PASS, run.error

    p00 = next(n for n in run.nodes if n.node_id == "P00_PREPARE")
    assert "archive_sync" in p00.payload
    assert p00.payload["bridged_to_queue"] == 0
    assert p00.payload["queue_summary"]["by_status"]["queued"] == 1

    p01 = next(n for n in run.nodes if n.node_id == "P01_BEGIN")
    assert p01.payload["claimed_queue_ids"] == [item.queue_id]
    assert run.admitted_sessions == ["claude-code:pipe-1"]

    settled = rt.db.get_queue_item(item.queue_id)
    assert settled.status is QueueStatus.PROCESSED
    assert settled.run_id == run.run_id
    assert settled.completed_at

    p05 = next(n for n in run.nodes if n.node_id == "P05_SETTLE")
    assert p05.payload["settled_queue_ids"] == [item.queue_id]
    assert p05.payload["settled_sessions"] == ["claude-code:pipe-1"]

    session = rt.db.get_session("claude-code:pipe-1")
    assert session.status is SessionStatus.PROCESSED
    assert session.last_consumed_turn == settled.consumed_to_turn
    assert session.last_consumed_hash == settled.content_hash

    assert any(
        event_type == "queue_updated" and data.get("source") == "p01_claim"
        for event_type, data in broker.events
    )
    assert any(
        event_type == "queue_updated" and data.get("source") == "p05_settle"
        for event_type, data in broker.events
    )

    # The trail is queryable through the queue control plane.
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        events = client.get(f"/api/site/queue/{item.queue_id}/events").json()["events"]
    transitions = {(e["from_status"], e["to_status"], e["reason_code"]) for e in events}
    assert ("queued", "processing", "RUN_CLAIMED") in transitions
    assert ("processing", "processed", "RUN_SETTLED") in transitions


def test_p00_bridges_pending_registrations_and_a_normal_batch_consumes_them(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.register_session(
        RegisterSessionRequest(
            client="claude",
            session_id="legacy-1",
            title="部署报错修复",
            project="demo",
            summary="定位部署报错并总结排查步骤",
            content=CONTENT,
        )
    )
    assert rt.db.get_latest_queue_revision("claude:legacy-1") == 0

    run = rt.orchestrator.start_run(trigger="test", auto_approve_override=True)
    assert run.status == RunStatus.PASS, run.error

    p00 = next(n for n in run.nodes if n.node_id == "P00_PREPARE")
    assert p00.payload["bridged_to_queue"] == 1
    queued_items = rt.db.list_session_queue_items("claude:legacy-1")
    assert len(queued_items) == 1
    assert queued_items[0].ingress_channel is IngressChannel.LEGACY_SCAN
    assert queued_items[0].status is QueueStatus.PROCESSED
    assert queued_items[0].run_id == run.run_id


def test_a_normal_batch_claims_the_prioritized_backlog_first(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.cfg.workflow.max_sessions_per_run = 1
    general = _enqueue(rt, "p1-1", priority="P1_GENERAL")
    core = _enqueue(rt, "p0-1", priority="P0_CORE")

    run = rt.orchestrator.start_run(trigger="test", auto_approve_override=True)
    assert run.status == RunStatus.PASS, run.error

    p01 = next(n for n in run.nodes if n.node_id == "P01_BEGIN")
    assert p01.payload["claimed_queue_ids"] == [core.queue_id]
    assert run.admitted_sessions == ["claude-code:p0-1"]
    assert rt.db.get_queue_item(core.queue_id).status is QueueStatus.PROCESSED
    assert rt.db.get_queue_item(general.queue_id).status is QueueStatus.QUEUED


def test_an_explicit_session_keys_batch_claims_the_matching_queue_item(tmp_path: Path):
    rt = _runtime(tmp_path)
    item = _enqueue(rt, "keys-1")

    run = rt.orchestrator.start_run(
        trigger="test", session_keys=["claude-code:keys-1"], auto_approve_override=True
    )
    assert run.status == RunStatus.PASS, run.error

    p01 = next(n for n in run.nodes if n.node_id == "P01_BEGIN")
    assert p01.payload["claimed_queue_ids"] == [item.queue_id]
    assert rt.db.get_queue_item(item.queue_id).status is QueueStatus.PROCESSED
    assert rt.db.get_session("claude-code:keys-1").status is SessionStatus.PROCESSED


def test_approval_resume_settles_the_items_claimed_before_the_pause(tmp_path: Path):
    rt = _runtime(tmp_path)
    item = _enqueue(rt, "approve-1")

    paused = rt.orchestrator.start_run(trigger="test", queue_ids=[item.queue_id])
    assert paused.status is RunStatus.AWAITING_APPROVAL
    assert rt.db.get_queue_item(item.queue_id).status is QueueStatus.PROCESSING
    assert rt.db.get_queue_item(item.queue_id).run_id == paused.run_id

    completed = rt.orchestrator.approve_and_resume(paused.run_id)
    assert completed.status is RunStatus.PASS, completed.error
    settled = rt.db.get_queue_item(item.queue_id)
    assert settled.status is QueueStatus.PROCESSED
    assert settled.run_id == paused.run_id
    assert rt.db.get_session("claude-code:approve-1").status is SessionStatus.PROCESSED


def test_unreviewed_items_return_to_the_queue_and_their_session_stays_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rt = _runtime(tmp_path)
    first = _enqueue(rt, "unrev-1")
    second = _enqueue(rt, "unrev-2")

    from ai_flywheel.adapters.runners import DryRunHeuristicRunner

    original = DryRunHeuristicRunner.review_sessions

    def _review_only_first(self, sessions, open_retros, **kwargs):  # type: ignore[no-untyped-def]
        result = original(self, sessions, open_retros, **kwargs)
        result["sent_session_keys"] = ["claude-code:unrev-1"]
        return result

    monkeypatch.setattr(DryRunHeuristicRunner, "review_sessions", _review_only_first)

    run = rt.orchestrator.start_run(
        trigger="test", queue_ids=[first.queue_id, second.queue_id], auto_approve_override=True
    )
    assert run.status is RunStatus.PASS, run.error

    assert rt.db.get_queue_item(first.queue_id).status is QueueStatus.PROCESSED
    returned = rt.db.get_queue_item(second.queue_id)
    assert returned.status is QueueStatus.QUEUED
    assert returned.run_id is None
    p05 = next(n for n in run.nodes if n.node_id == "P05_SETTLE")
    assert p05.payload["requeued_queue_ids"] == [second.queue_id]
    assert rt.db.get_session("claude-code:unrev-2").status is SessionStatus.PENDING


def test_a_multi_chunk_session_stays_pending_until_its_last_chunk_is_settled(tmp_path: Path):
    rt = _runtime(tmp_path)
    result = rt.queue.on_archive(
        "claude-code",
        "multi-1",
        project="demo",
        title="超大上下文排查",
        content=_big_dialogue("FINAL-CONCLUSION：根因是缓存失效策略缺失。"),
    )
    items = result["items"]
    assert len(items) >= 2

    run = rt.orchestrator.start_run(
        trigger="test", queue_ids=[items[0].queue_id], auto_approve_override=True
    )
    assert run.status is RunStatus.PASS, run.error

    assert rt.db.get_queue_item(items[0].queue_id).status is QueueStatus.PROCESSED
    remaining = [
        item
        for item in rt.db.list_session_queue_items("claude-code:multi-1")
        if item.chunk_index > 1
    ]
    assert remaining and all(item.status is QueueStatus.QUEUED for item in remaining)
    assert rt.db.get_session("claude-code:multi-1").status is SessionStatus.PENDING
    p05 = next(n for n in run.nodes if n.node_id == "P05_SETTLE")
    assert p05.payload["deferred_sessions"] == ["claude-code:multi-1"]


# ---------------------------------------------------------------- 6. 重试 / DLQ / 取消


def test_failing_runs_increment_the_retry_counter_and_end_in_the_dead_letter_state(tmp_path: Path):
    rt = _runtime(tmp_path, retries=2)
    item = _enqueue(rt, "dlq-1")

    first = rt.orchestrator.start_run(
        trigger="test",
        queue_ids=[item.queue_id],
        auto_approve_override=True,
        simulate_fail_node="P04_WRITE_VERIFY",
    )
    assert first.status is RunStatus.FAILED
    after_first = rt.db.get_queue_item(item.queue_id)
    assert after_first.status is QueueStatus.QUEUED
    assert after_first.retry_count == 1
    assert after_first.run_id is None
    assert "Simulated P04" in after_first.last_error

    second = rt.orchestrator.start_run(
        trigger="test",
        queue_ids=[item.queue_id],
        auto_approve_override=True,
        simulate_fail_node="P04_WRITE_VERIFY",
    )
    assert second.status is RunStatus.FAILED
    dead = rt.db.get_queue_item(item.queue_id)
    assert dead.status is QueueStatus.DEAD_LETTER
    assert dead.retry_count == 2
    assert dead.last_error
    events = rt.db.list_audit_events(
        entity_type="queue_item", entity_id=item.queue_id, limit=50
    )
    assert any(
        e["to_status"] == "dead_letter" and e["reason_code"] == "MAX_RETRIES_EXCEEDED"
        for e in events
    )

    # A dead-letter item is never claimed by a later automatic batch.
    third = rt.orchestrator.start_run(trigger="test", auto_approve_override=True)
    assert third.status is RunStatus.PASS, third.error
    assert rt.db.get_queue_item(item.queue_id).status is QueueStatus.DEAD_LETTER


def test_the_requeue_action_resets_the_retry_counter(tmp_path: Path):
    rt = _runtime(tmp_path, retries=2)
    item = _enqueue(rt, "requeue-1")
    rt.orchestrator.start_run(
        trigger="test",
        queue_ids=[item.queue_id],
        auto_approve_override=True,
        simulate_fail_node="P04_WRITE_VERIFY",
    )
    assert rt.db.get_queue_item(item.queue_id).retry_count == 1

    requeued = rt.queue.apply_item_action(item.queue_id, "requeue")
    assert requeued is not None
    assert requeued.status is QueueStatus.QUEUED
    assert requeued.retry_count == 0
    assert requeued.run_id is None


def test_rejecting_a_run_returns_the_claimed_items_without_a_retry_penalty(tmp_path: Path):
    rt = _runtime(tmp_path, retries=3)
    item = _enqueue(rt, "reject-1")

    paused = rt.orchestrator.start_run(trigger="test", queue_ids=[item.queue_id])
    assert paused.status is RunStatus.AWAITING_APPROVAL
    assert rt.db.get_queue_item(item.queue_id).status is QueueStatus.PROCESSING

    cancelled = rt.orchestrator.reject_run(paused.run_id, reason="用户取消本次写入")
    assert cancelled.status is RunStatus.CANCELLED

    returned = rt.db.get_queue_item(item.queue_id)
    assert returned.status is QueueStatus.QUEUED
    assert returned.retry_count == 0
    assert returned.run_id is None
    events = rt.db.list_audit_events(
        entity_type="queue_item", entity_id=item.queue_id, limit=50
    )
    assert any(
        e["to_status"] == "queued" and e["reason_code"] == "APPROVAL_REJECTED"
        and e["actor"] == "approval_gate"
        for e in events
    )


def test_the_reaper_releases_queue_items_bound_to_a_dead_run(tmp_path: Path):
    rt = _runtime(tmp_path, retries=1)
    item = _enqueue(rt, "reap-1")

    paused = rt.orchestrator.start_run(trigger="test", queue_ids=[item.queue_id])
    assert paused.status is RunStatus.AWAITING_APPROVAL
    rt.db.claim_approval(
        paused.run_id,
        approved_mask=[True] * len(paused.planned_writes),
        selected_option="同意并写入",
        source="portal",
    )
    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    rt.db.renew_workflow_lock(paused.run_id, now=stale_at)

    recovered = rt.orchestrator.recover_interrupted_runs()
    assert paused.run_id in recovered

    released = rt.db.get_queue_item(item.queue_id)
    # max_retries=1: the first failure already exhausts the budget.
    assert released.status is QueueStatus.DEAD_LETTER
    assert released.retry_count == 1
    assert released.last_error


# ---------------------------------------------------------------- 7. 规则重筛


def _seed_low_signal_queued_item(rt: FlywheelRuntime) -> DistillQueueItem:
    """A queued item whose session the current triage rules reject."""
    rt.db.upsert_session(
        SessionRecord(
            session_key="codex:low-signal",
            client="codex",
            session_id="low-signal",
            title="xv",
            project="generic",
            content=None,
            status=SessionStatus.PENDING,
            metadata={"discovered_by": "p00_reconcile_scan"},
        )
    )
    item = DistillQueueItem(
        queue_id="q-low-signal",
        session_key="codex:low-signal",
        client="codex",
        status=QueueStatus.QUEUED,
        title="xv",
        payload={"chunk_text": "hi"},
    )
    return rt.db.upsert_queue_item(item)


def _seed_admittable_excluded_item(rt: FlywheelRuntime) -> DistillQueueItem:
    """An excluded item the current rules would now admit."""
    rt.db.upsert_session(
        SessionRecord(
            session_key="codex:now-rich",
            client="codex",
            session_id="now-rich",
            title="部署报错修复",
            project="demo",
            content=CONTENT,
            status=SessionStatus.PENDING,
        )
    )
    item = DistillQueueItem(
        queue_id="q-now-rich",
        session_key="codex:now-rich",
        client="codex",
        status=QueueStatus.EXCLUDED,
        exclude_reason_code="TOO_SHORT",
        title="部署报错修复",
        payload={"chunk_text": CONTENT},
    )
    return rt.db.upsert_queue_item(item)


def test_re_triage_dry_run_leaves_the_inventory_untouched(tmp_path: Path):
    rt = _runtime(tmp_path)
    low = _seed_low_signal_queued_item(rt)
    rich = _seed_admittable_excluded_item(rt)

    preview = rt.queue.re_triage_queue(dry_run=True)
    assert preview["dry_run"] is True
    assert preview["change_count"] == 2
    changes = {change["queue_id"]: change for change in preview["changes"]}
    assert changes[low.queue_id]["to_status"] == "excluded"
    assert changes[low.queue_id]["reason_code"] == "TOO_SHORT"
    assert changes[rich.queue_id]["to_status"] == "queued"

    # A dry run writes nothing at all.
    assert rt.db.get_queue_item(low.queue_id).status is QueueStatus.QUEUED
    assert rt.db.get_queue_item(rich.queue_id).status is QueueStatus.EXCLUDED
    assert rt.db.list_audit_events(entity_type="queue_item", entity_id=low.queue_id) == []

    applied = rt.queue.re_triage_queue(dry_run=False)
    assert applied["dry_run"] is False
    assert applied["change_count"] == 2
    excluded = rt.db.get_queue_item(low.queue_id)
    assert excluded.status is QueueStatus.EXCLUDED
    assert excluded.exclude_reason_code == "TOO_SHORT"
    assert rt.db.get_queue_item(rich.queue_id).status is QueueStatus.QUEUED
    trail = rt.db.list_audit_events(entity_type="queue_item", entity_id=low.queue_id)
    assert any(e["from_status"] == "queued" and e["to_status"] == "excluded" for e in trail)


# ---------------------------------------------------------------- 8. REST 控制面


def test_queue_control_plane_rest_api_end_to_end(tmp_path: Path):
    rt = _runtime(tmp_path)
    first = _enqueue(rt, "rest-1", title="部署报错修复")
    second = _enqueue(rt, "rest-2", client="codex", title="缓存异常排查")

    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        # Inventory: summary + items + exact total.
        listing = client.get("/api/site/queue")
        assert listing.status_code == 200
        body = listing.json()
        assert body["total"] == 2
        assert body["summary"]["by_status"]["queued"] == 2
        assert body["summary"]["estimated_batches"] >= 1
        assert {item["queue_id"] for item in body["items"]} == {first.queue_id, second.queue_id}

        # Keyword and status filters.
        filtered = client.get("/api/site/queue", params={"q": "缓存"})
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["items"][0]["session_key"] == "codex:rest-2"
        by_client = client.get("/api/site/queue", params={"client": "codex"})
        assert by_client.json()["total"] == 1
        paged = client.get("/api/site/queue", params={"limit": 1, "offset": 1})
        assert paged.json()["total"] == 2 and len(paged.json()["items"]) == 1

        # Trajectory of one item.
        events = client.get(f"/api/site/queue/{first.queue_id}/events")
        assert events.status_code == 200
        assert any(e["to_status"] == "queued" for e in events.json()["events"])

        # Archive sync + legacy bridge.
        sync = client.post("/api/site/queue/sync")
        assert sync.status_code == 200
        sync_body = sync.json()
        assert set(sync_body) == {"sync", "bridged", "summary"}
        assert sync_body["sync"]["sources"] == []

        # Single-item action, invalid action, unknown id.
        action = client.post(
            f"/api/site/queue/{first.queue_id}/action",
            json={"action": "withdraw", "reason": "暂时撤回"},
        )
        assert action.status_code == 200
        assert action.json()["item"]["status"] == "withdrawn"
        assert client.post(
            f"/api/site/queue/{first.queue_id}/action", json={"action": "nonsense"}
        ).status_code == 400
        assert client.post(
            "/api/site/queue/no-such-item/action", json={"action": "requeue"}
        ).status_code == 404

        # Bulk action applies to every id it can.
        bulk = client.post(
            "/api/site/queue/bulk-action",
            json={"queue_ids": [first.queue_id, second.queue_id], "action": "requeue"},
        )
        assert bulk.status_code == 200
        assert bulk.json()["updated"] == 2
        assert rt.db.get_queue_item(first.queue_id).status is QueueStatus.QUEUED

        # Rules re-triage preview never mutates the inventory.
        retriage = client.post("/api/site/queue/re-triage", json={"dry_run": True})
        assert retriage.status_code == 200
        assert retriage.json()["dry_run"] is True
        assert "summary" in retriage.json()
        assert rt.db.get_queue_item(second.queue_id).status is QueueStatus.QUEUED

        # A batch triggered through the API with queue_ids consumes the queue.
        started = client.post(
            "/api/site/runs", json={"queue_ids": [second.queue_id], "auto_approve": True}
        )
        assert started.status_code == 200
        run = started.json()["run"]
        assert run["status"] == "PASS"
        assert run["admitted_sessions"] == ["codex:rest-2"]

    settled = rt.db.get_queue_item(second.queue_id)
    assert settled.status is QueueStatus.PROCESSED
    assert settled.run_id == run["run_id"]


def test_the_queue_api_rejects_an_unsupported_bulk_action(tmp_path: Path):
    rt = _runtime(tmp_path)
    item = _enqueue(rt, "bulk-1")
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        response = client.post(
            "/api/site/queue/bulk-action",
            json={"queue_ids": [item.queue_id], "action": "explode"},
        )
    assert response.status_code == 400


def test_an_automatic_batch_never_admits_an_opted_out_session(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:vetoed-1",
            client="claude-code",
            session_id="vetoed-1",
            title="部署报错修复",
            project="demo",
            content=CONTENT,
            status=SessionStatus.PENDING,
            opt_out=True,
            opt_out_reason="本会话不沉淀",
        )
    )

    run = rt.orchestrator.start_run(trigger="test", auto_approve_override=True)
    assert run.status is RunStatus.PASS, run.error
    assert run.admitted_sessions == []
    assert rt.db.get_session("claude-code:vetoed-1").status is SessionStatus.PENDING
    assert rt.db.list_session_queue_items(
        "claude-code:vetoed-1", statuses=[QueueStatus.QUEUED]
    ) == []


def test_jsonl_extractor_strips_claude_code_user_tool_result_blocks(tmp_path: Path):
    from ai_flywheel.core.queue_service import read_full_transcript

    jsonl = tmp_path / "claude-tool-heavy.jsonl"
    records = [
        {
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "HUMAN_GOAL_MARKER: 排查数据库慢查询根因"},
                    {"type": "tool_result", "content": "HUGE_TOOL_DUMP " * 8000},
                ],
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "text": "INTERNAL_THINKING_BLOCK"},
                    {"type": "tool_use", "name": "Bash", "content": "ls -la"},
                    {"type": "text", "text": "ASSISTANT_CONCLUSION_MARKER: 补齐复合索引后恢复正常"},
                ],
            }
        },
    ]
    jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    extracted = read_full_transcript(jsonl)
    assert "HUMAN_GOAL_MARKER" in extracted
    assert "ASSISTANT_CONCLUSION_MARKER" in extracted
    assert "HUGE_TOOL_DUMP" not in extracted
    assert "INTERNAL_THINKING_BLOCK" not in extracted


def test_oversized_session_chunks_are_capped_preserving_head_and_tail():
    from ai_flywheel.core.queue_service import MAX_CHUNKS_PER_SESSION, prepare_queue_chunks

    turns = ["[user] OPENING_GOAL_MARKER: 启动架构重构排查"]
    for i in range(90):
        turns.append(f"[user] 第 {i + 1} 轮跟进：" + ("排查数据库与接口边界细节。" * 45))
        turns.append(f"[assistant] 第 {i + 1} 轮回复：" + ("整理日志与回归测试结果。" * 60))
    turns.append("[assistant] FINAL_TAIL_CONCLUSION_MARKER: 根因确认并完成回归验收。")
    prepared = prepare_queue_chunks("\n\n".join(turns), max_item_chars=8000)
    chunks = prepared["chunks"]
    assert len(chunks) <= MAX_CHUNKS_PER_SESSION
    assert chunks[0]["chunk_index"] == 1
    assert chunks[-1]["chunk_index"] == len(chunks)
    assert "OPENING_GOAL_MARKER" in chunks[0]["chunk_text"]
    assert "FINAL_TAIL_CONCLUSION_MARKER" in chunks[-1]["chunk_text"]


def test_queue_sync_endpoint_does_not_flood_with_legacy_pending_sessions(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.db.upsert_session(
        SessionRecord(
            session_key="codex:tiny-stub-1",
            client="codex",
            session_id="tiny-stub-1",
            title="tiny",
            project="generic",
            content="",
            status=SessionStatus.PENDING,
            metadata={"discovered_by": "p00_reconcile_scan"},
        )
    )
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        res = client.post("/api/site/queue/sync")
        assert res.status_code == 200
        body = res.json()
        assert body["bridged"] == 0
        assert body["summary"]["by_status"]["queued"] == 0

    # Even if bridge_pending_sessions_to_queue is called explicitly, a tiny stub is excluded by triage.
    bridged = rt.queue.bridge_pending_sessions_to_queue()
    assert bridged == 0
    excluded = rt.db.list_queue_items(status=QueueStatus.EXCLUDED)
    assert len(excluded) == 1
    assert excluded[0].session_key == "codex:tiny-stub-1"


def test_codex_identity_normalization_and_deduplication(tmp_path: Path):
    from ai_flywheel.config import ScanSourceConfig

    archive_dir = tmp_path / "codex_archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    rt = _runtime(tmp_path)
    rt.cfg.sessions.codex_archive_sources = [
        ScanSourceConfig(client="codex", path=str(archive_dir), glob="*.jsonl")
    ]

    canonical_uuid = "01934e8f-724d-7a63-b295-886ec55c6ce0"
    canonical_key = f"codex:{canonical_uuid}"

    # 1. MCP enqueues with canonical UUID key first
    mcp_items = rt.queue.on_explicit_enqueue(
        client="codex",
        session_id=canonical_uuid,
        title="初始架构设计与选型",
        project="demo-repo",
        workspace=str(tmp_path / "demo-repo"),
        content="用户目标：确认分布式缓存与数据库选型方案。\n助手建议：采用标准两级缓存架构。",
    )
    assert len(mcp_items) == 1
    assert mcp_items[0].session_key == canonical_key

    # 2. Codex archive sync discovers rollout file for the same UUID
    jsonl_file = archive_dir / f"rollout-2026-11-20T08-00-00-{canonical_uuid}.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": canonical_uuid,
                "cwd": str(tmp_path / "demo-repo"),
            },
        },
        {
            "role": "user",
            "content": "用户目标：确认分布式缓存与数据库选型方案。",
        },
        {
            "role": "assistant",
            "content": "助手建议：采用标准两级缓存架构并完成压测验证。",
        },
    ]
    jsonl_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")

    report = rt.queue.reconcile_codex_archives()
    assert report["scanned"] == 1
    # Should update existing session without manufacturing a duplicate rollout session
    assert rt.db.get_session(f"codex:{jsonl_file.stem}") is None
    canonical_session = rt.db.get_session(canonical_key)
    assert canonical_session is not None
    assert canonical_session.session_key == canonical_key


def test_subagent_parent_hierarchy_and_automatic_claim_isolation(tmp_path: Path):
    rt = _runtime(tmp_path)
    parent_uuid = "01934e8f-0000-0000-0000-000000000001"
    child_uuid = "01934e8f-0000-0000-0000-000000000002"
    parent_key = f"codex:{parent_uuid}"
    child_key = f"codex:{child_uuid}"

    parent_item = rt.queue.on_explicit_enqueue(
        client="codex",
        session_id=parent_uuid,
        title="主会话：系统重构协调",
        project="demo-project",
        content="[user] 协调主任务目标设计。\n[assistant] 已启动任务拆解并派发子代理。",
    )[0]

    # Enqueue subagent session with parent linkage
    child_records = rt.queue.on_archive(
        client="codex",
        session_id=child_uuid,
        title="子任务：数据持久层迁移",
        project="demo-project",
        content="[user] 执行持久层迁移脚本编写。\n[assistant] 数据库迁移脚本已完成测试验证。",
        parent_session_key=parent_key,
        is_subagent=True,
    )
    assert child_records["action"] == "enqueued"
    child_item = child_records["items"][0]
    assert child_item.parent_session_key == parent_key
    assert child_item.is_subagent is True

    # 1. Verify prioritized list defaults to excluding subagents
    auto_candidates = rt.db.list_queued_items_prioritized(limit=10, include_subagents=False)
    assert any(i.queue_id == parent_item.queue_id for i in auto_candidates)
    assert not any(i.queue_id == child_item.queue_id for i in auto_candidates)

    # 2. Including subagents returns both
    all_candidates = rt.db.list_queued_items_prioritized(limit=10, include_subagents=True)
    assert any(i.queue_id == parent_item.queue_id for i in all_candidates)
    assert any(i.queue_id == child_item.queue_id for i in all_candidates)

    # 3. Explicit queue_ids run claims the subagent item
    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        res = client.post(
            "/api/site/runs",
            json={"trigger_source": "manual_portal", "queue_ids": [child_item.queue_id]},
        )
        assert res.status_code == 200
        run_data = res.json()["run"]
        p01 = next(n for n in run_data["nodes"] if n["node_id"] == "P01_BEGIN")
        assert child_item.queue_id in p01["payload"]["claimed_queue_ids"]


def test_readable_title_precedence_and_no_rollout_prefix_collision(tmp_path: Path):
    from ai_flywheel.core.triage import derive_readable_title

    # Tier 1: Explicit valid title
    t1 = derive_readable_title("codex", "uuid-123", explicit_title="自定义清晰任务标题")
    assert t1 == "自定义清晰任务标题"

    # Old placeholder titles are bypassed
    t_ph = derive_readable_title(
        "codex",
        "01934e8f-724d-7a63-b295-886ec55c6ce0",
        explicit_title="codex session rollout-2026-11-20T08",
        custom_title="Claude设置的高优标题",
    )
    assert t_ph == "Claude设置的高优标题"

    # Tier 3: First valid user message, stripping tags & skill brackets
    noisy_dialogue = (
        "[user] <system-reminder>系统注入提示</system-reminder>\n"
        "<app-context>环境变量配置</app-context>\n"
        "[$skill](file:///path/to/skill) 核心业务：排查交易订单分布式事务一致性\n"
        "[assistant] 正在分析事务回滚日志。"
    )
    t3 = derive_readable_title(
        "codex",
        "01934e8f-724d-7a63-b295-886ec55c6ce0",
        text=noisy_dialogue,
        workspace="E:/repos/trade-service",
    )
    assert "核心业务：排查交易订单分布式事务一致性" in t3
    assert "system-reminder" not in t3

    # Tier 4: Fallback directory and tail-8 UUID
    t4 = derive_readable_title(
        "codex",
        "01934e8f-724d-7a63-b295-886ec55c6ce0",
        text="",
        workspace="E:/repos/trade-service",
    )
    assert t4 == "[trade-service] codex · 55c6ce0" or t4 == "[trade-service] codex · 886ec55c6ce0" or t4.endswith("codex · 55c6ce0") or t4.startswith("[trade-service] codex · ")


def test_headless_script_filtering_by_prefix_and_no_tools(tmp_path: Path):
    rt = _runtime(tmp_path)
    rt.cfg.sessions.triage.exclude_content_prefixes = ["[headless-batch]"]

    # 1. Content prefix match -> excluded with SUBAGENT_FRAGMENT
    res_prefix = rt.queue.on_archive(
        client="codex",
        session_id="script-prefix-001",
        content="[user] [headless-batch] 执行定时对账脚本巡检任务",
    )
    assert res_prefix["action"] == "excluded"
    assert res_prefix["reason_code"] == "SUBAGENT_FRAGMENT"

    # 2. No-tool headless batch match (<= 2 turns and no tools)
    res_notool = rt.queue.on_archive(
        client="codex",
        session_id="script-notool-002",
        content="[user] 所有工具已关闭，仅回显状态代码。\n[assistant] OK 状态就绪。",
    )
    assert res_notool["action"] == "excluded"
    assert res_notool["reason_code"] == "SUBAGENT_FRAGMENT"

    # 3. Attached subagent with parent linkage is admitted to queued
    res_attached = rt.queue.on_archive(
        client="codex",
        session_id="subagent-child-003",
        content="[user] 派生子任务：处理本地数据切片缓存\n[assistant] 切片缓存已生成。",
        parent_session_key="codex:parent-001",
        is_subagent=True,
    )
    assert res_attached["action"] == "enqueued"
    assert res_attached["items"][0].status == QueueStatus.QUEUED
    assert res_attached["items"][0].is_subagent is True


def test_re_triage_dry_run_zero_write_and_superseded_cleanup(tmp_path: Path):
    rt = _runtime(tmp_path)
    canonical_uuid = "01934e8f-1111-2222-3333-444444444444"
    canonical_key = f"codex:{canonical_uuid}"

    # 1. Enqueue canonical item
    canonical_item = rt.queue.on_explicit_enqueue(
        client="codex",
        session_id=canonical_uuid,
        title="标准主会话架构",
        content="[user] 规范主会话核心内容。\n[assistant] 架构设计已完成。",
    )[0]

    # 2. Seed a legacy duplicate rollout item in QUEUED
    rollout_key = f"codex:rollout-2026-11-20T08-00-00-{canonical_uuid}"
    rollout_session = SessionRecord(
        session_key=rollout_key,
        client="codex",
        session_id=f"rollout-2026-11-20T08-00-00-{canonical_uuid}",
        title="codex session rollout-2026-11-",
        project="generic",
        content="[user] 规范主会话核心内容。\n[assistant] 架构设计已完成。",
        status=SessionStatus.PENDING,
    )
    rt.db.upsert_session(rollout_session)
    rollout_item = DistillQueueItem(
        queue_id="queue-legacy-rollout-1",
        session_key=rollout_key,
        client="codex",
        title="codex session rollout-2026-11-",
        status=QueueStatus.QUEUED,
        revision=1,
        chunk_index=1,
        chunk_total=1,
    )
    rt.db.upsert_queue_item(rollout_item)

    # 3. Dry run test: zero write
    preview = rt.queue.re_triage_queue(dry_run=True)
    assert preview["dry_run"] is True
    assert preview["metadata_refresh_count"] >= 1
    assert any(
        c["queue_id"] == "queue-legacy-rollout-1" and c["reason_code"] == "SUPERSEDED"
        for c in preview["would_exclude"]
    )
    # Check that in DB it is still QUEUED and title unchanged
    item_in_db = rt.db.get_queue_item("queue-legacy-rollout-1")
    assert item_in_db is not None
    assert item_in_db.status == QueueStatus.QUEUED
    assert item_in_db.title == "codex session rollout-2026-11-"

    # 4. Apply re-triage: applies cleanup
    applied = rt.queue.re_triage_queue(dry_run=False)
    assert applied["dry_run"] is False
    item_after = rt.db.get_queue_item("queue-legacy-rollout-1")
    assert item_after is not None
    assert item_after.status == QueueStatus.EXCLUDED
    assert item_after.exclude_reason_code == "SUPERSEDED"


def test_end_to_end_real_jsonl_fastapi_client_playback(tmp_path: Path):
    archive_dir = tmp_path / "codex_archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    rt = _runtime(tmp_path)
    rt.cfg.sessions.triage.min_chars = 10
    rt.cfg.sessions.triage.gray_workspace_min_chars = 10
    rt.cfg.sessions.triage.temp_workspace_patterns = []
    from ai_flywheel.config import ScanSourceConfig

    rt.cfg.sessions.codex_archive_sources = [
        ScanSourceConfig(client="codex", path=str(archive_dir), glob="*.jsonl")
    ]

    # Create real JSONL files for parent and child subagent
    parent_uuid = "01934e8f-7777-8888-9999-000000000001"
    child_uuid = "01934e8f-7777-8888-9999-000000000002"

    parent_jsonl = archive_dir / f"rollout-2026-11-20T10-00-00-{parent_uuid}.jsonl"
    parent_lines = [
        {"type": "session_meta", "payload": {"id": parent_uuid, "cwd": str(tmp_path / "my-project")}},
        {"role": "user", "content": "端到端目标：验证全流程重筛与批次调度"},
        {"role": "assistant", "content": "主会话处理完成。"},
    ]
    parent_jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in parent_lines) + "\n", encoding="utf-8")

    child_jsonl = archive_dir / f"rollout-2026-11-20T10-05-00-{child_uuid}.jsonl"
    child_lines = [
        {
            "type": "session_meta",
            "payload": {
                "id": child_uuid,
                "parent_thread_id": parent_uuid,
                "thread_source": "subagent",
                "agent_nickname": "worker-c1",
                "agent_role": "tester",
                "cwd": str(tmp_path / "my-project"),
            },
        },
        {"role": "user", "content": "子代理目标：执行自动化验证套件"},
        {"role": "assistant", "content": "验证套件全部执行通过。"},
    ]
    child_jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in child_lines) + "\n", encoding="utf-8")

    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        # 1. Sync queue
        sync_res = client.post("/api/site/queue/sync")
        assert sync_res.status_code == 200
        sync_body = sync_res.json()
        assert sync_body["sync"]["enqueued"] == 2

        # 2. List queue
        list_res = client.get("/api/site/queue")
        assert list_res.status_code == 200
        items = list_res.json()["items"]
        assert len(items) == 2

        parent_item = next(i for i in items if i["session_key"] == f"codex:{parent_uuid}")
        child_item = next(i for i in items if i["session_key"] == f"codex:{child_uuid}")

        assert parent_item["is_subagent"] is False
        assert child_item["is_subagent"] is True
        assert child_item["parent_session_key"] == f"codex:{parent_uuid}"
        assert child_item["payload"]["subagent_nickname"] == "worker-c1"
        assert child_item["payload"]["subagent_role"] == "tester"

        # 3. Re-triage dry-run
        dry_res = client.post("/api/site/queue/re-triage", json={"dry_run": True})
        assert dry_res.status_code == 200
        dry_body = dry_res.json()
        assert dry_body["dry_run"] is True
        assert "metadata_refresh_count" in dry_body

        # 4. Trigger run explicitly claiming child subagent
        run_res = client.post(
            "/api/site/runs",
            json={"trigger_source": "manual_portal", "queue_ids": [child_item["queue_id"]]},
        )
        assert run_res.status_code == 200
        p01 = next(n for n in run_res.json()["run"]["nodes"] if n["node_id"] == "P01_BEGIN")
        assert child_item["queue_id"] in p01["payload"]["claimed_queue_ids"]


def test_codex_response_item_msg_id_and_agents_boilerplate_never_corrupt_metadata(tmp_path: Path):
    """Codex response_item payload.id='msg_...' and '# AGENTS.md instructions' boilerplate must be ignored."""
    from ai_flywheel.core.triage import extract_jsonl_metadata

    sess_uuid = "11112222-3333-4444-5555-666677778888"
    ckpt_uuid = "99998888-7777-6666-5555-444433332222"
    rollout = tmp_path / f"rollout-2026-11-26T10-00-00-{sess_uuid}_{ckpt_uuid}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": sess_uuid, "session_id": sess_uuid, "cwd": "/projects/demo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "role": "user",
                "content": "# AGENTS.md instructions for /projects/demo <INSTRUCTIONS> rules",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "msg_ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee",
                "role": "user",
                "content": "排查分布式缓存雪崩的根因与限流方案",
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")

    meta = extract_jsonl_metadata(rollout)
    assert meta["canonical_session_id"] == sess_uuid
    assert meta["first_user_message"] == "排查分布式缓存雪崩的根因与限流方案"

    # Re-triage preserves an explicit human title on canonical item and marks the legacy rollout duplicate SUPERSEDED.
    rt = _runtime(tmp_path)
    rt.queue.on_explicit_enqueue(
        client="codex",
        session_id=sess_uuid,
        title="人工确认的显式标题",
        workspace="/projects/demo",
        content="[user] 排查分布式缓存雪崩的根因与限流方案\n\n[assistant] 已通过布隆过滤器与互斥锁修复。",
    )
    legacy_sid = rollout.stem
    rt.db.upsert_session(
        SessionRecord(
            session_key=f"codex:{legacy_sid}",
            client="codex",
            session_id=legacy_sid,
            title="codex session rollout-2026-11-",
            workspace="/projects/demo",
            transcript_path=str(rollout),
            status=SessionStatus.PENDING,
        )
    )
    rt.db.upsert_queue_item(
        DistillQueueItem(
            queue_id="q-legacy-dup",
            session_key=f"codex:{legacy_sid}",
            client="codex",
            workspace="/projects/demo",
            title="codex session rollout-2026-11-",
            ingress_channel=IngressChannel.LEGACY_SCAN,
            status=QueueStatus.QUEUED,
            source_path=str(rollout),
        )
    )

    res = rt.queue.re_triage_queue(dry_run=False)
    assert any(c["queue_id"] == "q-legacy-dup" and c["reason_code"] == "SUPERSEDED" for c in res["changes"])
    canonical_items = rt.db.list_session_queue_items(f"codex:{sess_uuid}")
    assert canonical_items[0].title == "人工确认的显式标题"


# ---------------------------------------------------------------- 9. 双轨意图 / 结构化摘要 / 写入派单
#
# feat-sediment-dual-track-and-scheduler-catchup. Both briefs below are fictional: each
# stands for every brief of its kind, whichever installation wrote the original.


#: A controlled knowledge-base write dispatch, as an orchestrator hands it to a writer
#: subagent: one long instruction that hands over an approved write-set and names the
#: write contract the result has to pass.
DISPATCH_BRIEF = "\n".join(
    [
        "请把本轮排查的结论沉淀成一篇知识经验卡。这是一次受控写入，按下面的派单执行，不要扩大范围。",
        "",
        "## 背景",
        "- 现象：灰度发布后，订单查询接口的 P99 延迟从 80 ms 升到 1.4 s，错误率没有变化。",
        "- 根因：本地缓存在同一时刻集中过期，回源流量把连接池占满，请求排队等待连接。",
        "- 修复：过期时间加上随机抖动，并给回源加互斥锁；修复后压测与线上指标均已恢复。",
        "- 证据：修复前后各一轮压测报告、监控面板截图、发布记录中的变更单号。",
        "",
        "## 已批准的 write-set",
        "- lessons/cache-stampede-prevention.md（新建）",
        "- lessons/README.md（只追加一条索引，不改动已有条目）",
        "",
        "## 来源材料",
        "- 排查会话的结论段落：现象、根因与修复的原始描述。",
        "- 两轮压测报告：修复前的基线，以及修复后的对比。",
        "- 监控面板截图：接口延迟、连接池使用率与回源并发三条曲线。",
        "- 发布记录：变更单号、发布批次与回滚预案。",
        "",
        "## 交付对象",
        "- 读者：维护订单服务与网关的后端工程师。",
        "- 用途：发布评审与故障复盘时的参考。",
        "",
        "## 写前门禁",
        "1. 写入前读取 `.wiki-schema.md`，确认目标目录允许新建经验卡，并记下必填段落。",
        "2. 标题、日期与来源段落按 schema 给出的模板填写，日期使用上海时区。",
        "3. 正文只写已经验证过的结论；推测性内容必须标注「待验证」，并说明还缺什么证据。",
        "4. 引用监控数据时只写指标名、时间点与数值，不粘贴原始日志，不出现任何主机名或账号。",
        "5. 涉及配置项时写出配置键与取值范围，不写出具体环境里的真实取值。",
        "",
        "## 正文结构",
        "- 现象：一句话说明用户看到了什么，附上时间点。",
        "- 根因：从现象推到根因的链条，每一步给出支撑它的证据。",
        "- 修复：改了什么、为什么这样改、还有哪些备选方案被放弃。",
        "- 验证：压测与线上指标的前后对比。",
        "- 复用建议：其他服务满足哪些条件时可以照此处理。",
        "- 关联阅读：指向已有的连接池与限流经验卡。",
        "",
        "## 术语约定",
        "- 「过期抖动」指给每条缓存的过期时间加上一个随机偏移。",
        "- 「回源互斥」指同一个键在同一时刻只允许一个请求回源。",
        "- 文档内首次出现的缩写先写全称，再写缩写。",
        "",
        "## 写入步骤",
        "1. 先读取已有的连接池经验卡，确认与本次结论既不重复也不冲突。",
        "2. 新建经验卡正文，按上面的结构逐段填写。",
        "3. 在索引文件末尾追加一条指向新卡的条目。",
        "4. 本地运行一次链接检查，确认没有失效链接。",
        "5. 提交写后核验，等待钩子给出结论。",
        "6. 把钩子的结论整理成回执回报。",
        "",
        "## 禁止事项",
        "- 不要把排查过程中的试错步骤写进正文，它们属于过程，不属于结论。",
        "- 不要复制日志中的原始堆栈，只保留能说明根因的最小片段。",
        "- 不要给经验卡起含日期或版本号的文件名，文件名只表达主题。",
        "",
        "## 写后核验",
        "- 写后钩子会校验链接与 frontmatter，任何一项不通过就回滚本次写入并报告原因。",
        "- 完成后在回执中列出实际写入的路径与每一项校验的结果。",
        "- 若有回滚，写明触发回滚的校验项与回滚后的现场状态。",
        "- 不要修改 write-set 之外的任何文件，不要提交或推送远端，也不要追加解释性长文。",
    ]
)

#: The same length and layout, but not a write dispatch: a subagent asked to verify a
#: release and report back. It names a write-set only to say there is none.
VERIFICATION_BRIEF = "\n".join(
    [
        "请执行本次发布的自动化验证并回报结果。这是一次只读验证，不执行任何受控写入，也不涉及 write-set。",
        "",
        "## 范围",
        "- 对象：订单服务本次发布的全部变更分支。",
        "- 环境：预发环境，数据库使用只读副本。",
        "- 时间窗：北京时间 02:00 到 04:00，期间不要触发任何写操作。",
        "- 证据：每个步骤的输出摘要，以及对应报告的链接。",
        "",
        "## 准备",
        "1. 拉取最新代码，确认依赖版本与发布单一致，并记录实际使用的提交号。",
        "2. 核对配置项与预发环境的基线一致，只比对配置键，不读取任何真实取值。",
        "3. 确认只读副本的延迟低于一秒，否则先等待再继续。",
        "4. 准备测试数据集，所有数据均为虚构样例，不包含任何真实记录。",
        "5. 通知值班的同事验证即将开始，并说明预计持续时间。",
        "",
        "## 步骤",
        "1. 运行单元测试与集成测试，记录失败用例的名称与断言。",
        "2. 运行接口回归，覆盖订单创建、查询、取消三条主链路。",
        "3. 运行并发压测五分钟，观察接口延迟与错误率是否越过基线。",
        "4. 检查日志中是否出现新的告警级别记录。",
        "5. 重启一次服务实例，确认启动自检全部通过。",
        "6. 重复步骤一至五，确认两轮结果一致。",
        "7. 把两轮结果整理成对照表，标出任何不一致的条目。",
        "",
        "## 判定标准",
        "- 单元测试与集成测试全部通过，没有任何跳过的用例。",
        "- 接口回归的成功率为百分之百，响应时间不超过基线的一倍。",
        "- 压测期间没有出现新的告警，连接池使用率低于八成。",
        "- 重启后自检全部通过，健康检查在一分钟内恢复。",
        "",
        "## 异常处理",
        "- 任一步骤失败时立即停止后续步骤，保留现场。",
        "- 不要自行修复代码或配置，只记录现象与复现步骤。",
        "- 失败信息中不要出现主机名、账号或任何真实数据。",
        "",
        "## 回报格式",
        "- 第一行写明验证通过或失败。",
        "- 随后逐条列出每个步骤的结论，以及支撑它的证据链接。",
        "- 若有失败，写明失败用例、复现步骤与初步判断。",
        "- 不要修改任何代码与配置，不要提交或推送，也不要追加解释性长文。",
    ]
)


async def _mcp(rt: FlywheelRuntime, tool: str, arguments: dict) -> dict:
    """Call one MCP tool in-process and return its structured result."""
    result = await rt.mcp.call_tool(tool, arguments)
    assert result.is_error is False, result
    assert isinstance(result.structured_content, dict), result
    return result.structured_content


def _declare_hand_distilled(
    rt: FlywheelRuntime, session_key: str, *, wiki_paths: list[str] | None = None, **fields
) -> dict:
    """``flywheel_session_intent(action="hand_distilled")`` at the service level."""
    return rt.queue.handle_session_intent(
        "hand_distilled", session_key=session_key, wiki_paths=wiki_paths, **fields
    )


def _declaration_events(rt: FlywheelRuntime, queue_id: str) -> list[dict]:
    """The ``hand_distilled_declared`` audit events of one queue item, newest first."""
    return [
        event
        for event in rt.db.list_audit_events(entity_type="queue_item", entity_id=queue_id)
        if event["event_type"] == "hand_distilled_declared"
    ]


def _render_turns(turns: list[tuple[str, str]]) -> str:
    """Dialogue turns as inline session content."""
    return "\n\n".join(f"[{role}] {text}" for role, text in turns)


def _dispatch_turns(brief: str = DISPATCH_BRIEF) -> list[tuple[str, str]]:
    """A writer subagent's transcript: the dispatch, then the work done under it."""
    return [
        ("user", brief),
        ("assistant", "已按派单完成知识经验卡与索引的写入，写后核验全部通过。"),
    ]


def _write_subagent_jsonl(
    path: Path, *, session_id: str, parent_id: str, turns: list[tuple[str, str]]
) -> None:
    """A flagged Codex subagent rollout (an orphan when ``parent_id`` is empty)."""
    meta: dict = {
        "id": session_id,
        "thread_source": "subagent",
        "agent_nickname": "writer-c1",
        "agent_role": "writer",
        "cwd": "/projects/demo",
    }
    if parent_id:
        meta["parent_thread_id"] = parent_id
    lines: list[dict] = [{"type": "session_meta", "payload": meta}]
    lines.extend(
        {
            "type": "message",
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }
        for role, text in turns
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


def _seed_subagent_session(
    rt: FlywheelRuntime,
    transcript: Path,
    *,
    session_id: str,
    parent_id: str,
    title: str,
) -> str:
    """Register a flagged subagent transcript the way the scan does; return its key."""
    metadata: dict = {"discovered_by": "p00_reconcile_scan", "is_subagent": True}
    if parent_id:
        metadata["parent_session_key"] = f"codex:{parent_id}"
    rt.db.upsert_session(
        SessionRecord(
            session_key=f"codex:{session_id}",
            client="codex",
            session_id=session_id,
            title=title,
            project="demo",
            workspace="/projects/demo",
            transcript_path=str(transcript),
            status=SessionStatus.PENDING,
            metadata=metadata,
        )
    )
    return f"codex:{session_id}"


# ---- 9.1 hand_distilled：治理专属入队 / 幂等替换 / 整体跳过


@pytest.mark.asyncio
async def test_hand_distilled_queues_a_governance_only_item_with_its_paths_and_an_audit_trail(
    tmp_path: Path,
):
    """规格场景：手工沉淀后回调 hand_distilled → 一条 P0_CORE 治理专属队列项，并留审计。"""
    rt = _runtime(tmp_path)
    result = await _mcp(
        rt,
        "flywheel_session_intent",
        {
            "action": "hand_distilled",
            "session_key": "codex:s-101",
            "wiki_paths": ["projects/demo/note.md"],
            "governance_only": True,
            "title": "缓存击穿排查",
            "project": "demo",
            "content": CONTENT,
        },
    )
    assert result["status"] == "queued"
    assert result["action"] == "hand_distilled"
    assert result["session_key"] == "codex:s-101"
    assert result["distill_mode"] == "governance_only"
    assert result["hand_distilled_paths"] == ["projects/demo/note.md"]
    assert len(result["queue_ids"]) == 1

    item = rt.db.get_queue_item(result["queue_ids"][0])
    assert item.status is QueueStatus.QUEUED
    assert item.priority == "P0_CORE"
    assert item.project == "demo"
    assert item.ingress_channel is IngressChannel.MCP_EXPLICIT
    assert item.payload["distill_mode"] == "governance_only"
    assert item.payload["hand_distilled_paths"] == ["projects/demo/note.md"]

    # The declaration is mirrored onto the session, and the queue history is what decides.
    session = rt.db.get_session("codex:s-101")
    assert session.metadata["distill_mode"] == "governance_only"
    assert session.metadata["hand_distilled_paths"] == ["projects/demo/note.md"]
    assert rt.queue.distill_mode_of(session) == "governance_only"

    trail = rt.db.list_audit_events(entity_type="queue_item", entity_id=item.queue_id)
    declared = [event for event in trail if event["event_type"] == "hand_distilled_declared"]
    assert len(declared) == 1
    assert declared[0]["actor"] == "mcp"
    assert declared[0]["to_status"] == "queued"
    assert declared[0]["payload"]["distill_mode"] == "governance_only"
    assert declared[0]["payload"]["hand_distilled_paths"] == ["projects/demo/note.md"]
    created = [event for event in trail if event["event_type"] == "queue_item_created"]
    assert created[0]["payload"]["distill_mode"] == "governance_only"


def test_hand_distilled_replaces_the_queued_revision_and_repeats_idempotently(tmp_path: Path):
    """幂等替换：同内容重复声明不新增队列项；新路径并入原项；内容增长后的修订沿用治理模式。"""
    rt = _runtime(tmp_path)
    plain = _enqueue(rt, "h-2", client="codex")
    assert plain.payload["distill_mode"] == "full"

    # A credential shape inside a reported path is redacted; a repeated path is dropped.
    first = _declare_hand_distilled(
        rt,
        "codex:h-2",
        wiki_paths=[
            "projects/demo/a.md",
            "projects/demo/ref?token=hunter2hunter2",
            "projects/demo/a.md",
        ],
    )
    first_paths = first["hand_distilled_paths"]
    assert first_paths == ["projects/demo/a.md", "projects/demo/ref?token=[REDACTED]"]
    governance = rt.db.get_queue_item(first["queue_ids"][0])
    assert governance.status is QueueStatus.QUEUED
    assert governance.revision == plain.revision + 1
    assert governance.payload["hand_distilled_paths"] == first_paths
    # The ordinary item it replaced stays visible, and so does the replacement.
    assert _statuses(rt, "codex:h-2") == ["queued", "excluded"]
    assert rt.db.get_queue_item(plain.queue_id).exclude_reason_code == "SUPERSEDED"
    assert len(_declaration_events(rt, plain.queue_id)) == 1
    assert len(_declaration_events(rt, governance.queue_id)) == 1

    # The same declaration again is the same submission: no revision, no audit event.
    again = _declare_hand_distilled(rt, "codex:h-2", wiki_paths=["projects/demo/a.md"])
    assert again["queue_ids"] == first["queue_ids"]
    assert again["hand_distilled_paths"] == first_paths
    assert len(rt.db.list_session_queue_items("codex:h-2")) == 2
    assert len(_declaration_events(rt, governance.queue_id)) == 1

    # A second write-up adds its path to the same item; it opens no other.
    extended = _declare_hand_distilled(rt, "codex:h-2", wiki_paths=["projects/demo/b.md"])
    assert extended["queue_ids"] == first["queue_ids"]
    assert extended["hand_distilled_paths"] == ["projects/demo/b.md", *first_paths]
    merged = rt.db.get_queue_item(governance.queue_id)
    assert merged.status is QueueStatus.QUEUED
    assert merged.revision == governance.revision
    assert merged.payload["hand_distilled_paths"] == extended["hand_distilled_paths"]
    assert rt.db.get_session("codex:h-2").metadata["hand_distilled_paths"] == (
        extended["hand_distilled_paths"]
    )
    assert len(_declaration_events(rt, governance.queue_id)) == 2

    # The conversation grows afterwards: the next revision inherits mode and paths alike.
    grown = rt.queue.on_archive(
        "codex",
        "h-2",
        project="demo",
        content=CONTENT + "\n用户：补充一轮结论。\n助手：已补充。",
    )
    assert grown["action"] == "enqueued"
    assert _statuses(rt, "codex:h-2") == ["queued", "excluded", "excluded"]
    latest = rt.db.list_session_queue_items("codex:h-2", statuses=[QueueStatus.QUEUED])[0]
    assert latest.payload["distill_mode"] == "governance_only"
    assert latest.payload["hand_distilled_paths"] == extended["hand_distilled_paths"]


@pytest.mark.asyncio
async def test_hand_distilled_skip_takes_the_session_out_until_an_explicit_enqueue(tmp_path: Path):
    """governance_only=false：未消费的排队项置为 excluded，此后归档不再触发任何分析。"""
    rt = _runtime(tmp_path)
    queued = _enqueue(rt, "k-1", client="codex")
    result = await _mcp(
        rt,
        "flywheel_session_intent",
        {
            "action": "hand_distilled",
            "session_key": "codex:k-1",
            "wiki_paths": ["projects/demo/k-1.md"],
            "governance_only": False,
        },
    )
    assert result["status"] == "excluded"
    assert result["distill_mode"] == "skipped"
    assert result["reason_code"] == "HAND_DISTILLED_SKIP"
    assert result["hand_distilled_paths"] == ["projects/demo/k-1.md"]
    assert result["queue_ids"] == [queued.queue_id]

    item = rt.db.get_queue_item(queued.queue_id)
    assert item.status is QueueStatus.EXCLUDED
    assert item.exclude_reason_code == "HAND_DISTILLED_SKIP"
    assert item.payload["distill_mode"] == "skipped"
    assert item.payload["hand_distilled_paths"] == ["projects/demo/k-1.md"]
    session = rt.db.get_session("codex:k-1")
    assert session.metadata["distill_mode"] == "skipped"
    assert session.metadata["hand_distilled_skip"] is True
    declared = _declaration_events(rt, item.queue_id)
    assert len(declared) == 1
    assert declared[0]["reason_code"] == "HAND_DISTILLED_SKIP"
    assert declared[0]["payload"]["distill_mode"] == "skipped"

    # However much the conversation grows, a passive archive event never re-opens it.
    grown = rt.queue.on_archive(
        "codex",
        "k-1",
        project="demo",
        content=CONTENT + "\n用户：继续补充结论。\n助手：已补充。",
    )
    assert grown["action"] == "excluded"
    assert grown["reason_code"] == "HAND_DISTILLED_SKIP"
    assert not rt.db.list_session_queue_items("codex:k-1", statuses=[QueueStatus.QUEUED])
    assert len(rt.db.list_session_queue_items("codex:k-1")) == 1
    assert rt.queue.distill_mode_of(rt.db.get_session("codex:k-1")) == "skipped"

    # Only an explicit enqueue lifts the declaration.
    back = await _mcp(
        rt,
        "flywheel_session_intent",
        {
            "action": "enqueue",
            "session_key": "codex:k-1",
            "project": "demo",
            "content": CONTENT,
        },
    )
    assert back["status"] == "queued"
    assert _statuses(rt, "codex:k-1") == ["queued", "excluded"]
    restored = rt.db.get_session("codex:k-1")
    assert rt.queue.distill_mode_of(restored) == "full"
    assert "distill_mode" not in restored.metadata
    assert "hand_distilled_skip" not in restored.metadata


def test_hand_distilled_skip_on_a_session_that_was_never_queued_leaves_a_visible_record(
    tmp_path: Path,
):
    """从没入过队的会话被整体跳过：留一条可见的 excluded 记录，而不是无影无踪。"""
    rt = _runtime(tmp_path)
    result = _declare_hand_distilled(rt, "codex:k-2", governance_only=False)
    assert result["status"] == "excluded"
    assert len(result["queue_ids"]) == 1
    item = rt.db.get_queue_item(result["queue_ids"][0])
    assert item.status is QueueStatus.EXCLUDED
    assert item.exclude_reason_code == "HAND_DISTILLED_SKIP"
    assert item.payload["distill_mode"] == "skipped"
    assert rt.db.get_session("codex:k-2").metadata["hand_distilled_skip"] is True

    later = rt.queue.on_archive("codex", "k-2", project="demo", content=CONTENT)
    assert later["action"] == "excluded"
    assert later["reason_code"] == "HAND_DISTILLED_SKIP"
    assert len(rt.db.list_session_queue_items("codex:k-2")) == 1


# ---- 9.2 hand_distilled：被动归档不覆盖


def test_archive_sync_never_demotes_a_hand_distilled_session_back_to_a_plain_item(
    tmp_path: Path,
):
    """规格场景：被动归档同步不覆盖已登记的 hand_distilled 模式。"""
    rt = _runtime(tmp_path)
    archive = tmp_path / "archived_sessions"
    sid = "01934e8f-7777-8888-9999-000000000021"
    rollout = archive / f"rollout-2026-11-25T10-00-00-{sid}.jsonl"
    turns = [
        ("user", "排查缓存集中过期导致的连接池耗尽，并给出修复方案。"),
        ("assistant", "已定位根因，给过期时间加随机抖动并补上回源互斥，压测通过。"),
    ]
    _write_jsonl(rollout, turns)
    rt.cfg.sessions.codex_archive_sources = [ScanSourceConfig(client="codex", path=str(archive))]
    key = f"codex:{sid}"

    assert rt.queue.reconcile_codex_archives()["enqueued"] == 1
    plain = rt.db.list_session_queue_items(key, statuses=[QueueStatus.QUEUED])[0]
    assert plain.ingress_channel is IngressChannel.CODEX_ARCHIVE
    # An ordinary archive item is distilled in full and carries no mode of its own.
    assert "distill_mode" not in plain.payload

    # The client writes the knowledge up by hand and says so.
    declared = _declare_hand_distilled(rt, key, wiki_paths=["projects/demo/note.md"])
    governance = rt.db.get_queue_item(declared["queue_ids"][0])
    assert governance.status is QueueStatus.QUEUED
    assert governance.revision == plain.revision + 1
    assert governance.payload["distill_mode"] == "governance_only"
    assert _statuses(rt, key) == ["queued", "excluded"]

    # The archive sync sees the same file again: nothing new, and nothing demoted.
    same = rt.queue.reconcile_codex_archives()
    assert (same["enqueued"], same["excluded"], same["unchanged"]) == (0, 0, 1)
    assert [item.revision for item in rt.db.list_session_queue_items(key)] == [2, 1]
    assert rt.db.get_queue_item(governance.queue_id).status is QueueStatus.QUEUED

    # The conversation continues after the declaration: the next revision inherits the
    # mode instead of queueing a second plain write.
    _write_jsonl(
        rollout,
        [
            *turns,
            ("user", "请把回源超时的兜底策略也补充进去。"),
            ("assistant", "已补充：回源超时直接返回旧值，并记录一条降级日志。"),
        ],
    )
    assert rt.queue.reconcile_codex_archives()["enqueued"] == 1
    assert _statuses(rt, key) == ["queued", "excluded", "excluded"]
    latest = rt.db.list_session_queue_items(key, statuses=[QueueStatus.QUEUED])[0]
    assert latest.ingress_channel is IngressChannel.CODEX_ARCHIVE
    assert latest.payload["distill_mode"] == "governance_only"
    assert latest.payload["hand_distilled_paths"] == ["projects/demo/note.md"]
    session = rt.db.get_session(key)
    assert session.metadata["distill_mode"] == "governance_only"
    assert rt.queue.distill_mode_of(session) == "governance_only"


def test_a_hand_distilled_declaration_outlives_the_metadata_it_was_mirrored_into(
    tmp_path: Path,
):
    """队列历史才是记录：一次整体替换会话 metadata 的登记不会抹掉声明。"""
    rt = _runtime(tmp_path)
    _declare_hand_distilled(rt, "claude-code:w-9", wiki_paths=["projects/demo/w-9.md"], content=CONTENT)

    # A registration that stores its own metadata wholesale, as `upsert_session` does
    # for a session no lifecycle hook protects.
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:w-9",
            client="claude-code",
            session_id="w-9",
            title="部署报错修复",
            project="demo",
            content=CONTENT,
            status=SessionStatus.PENDING,
        )
    )
    session = rt.db.get_session("claude-code:w-9")
    assert "distill_mode" not in session.metadata
    assert rt.queue.distill_mode_of(session) == "governance_only"

    grown = rt.queue.on_archive(
        "claude-code",
        "w-9",
        project="demo",
        content=CONTENT + "\n用户：补充一轮结论。\n助手：已补充。",
    )
    assert grown["action"] == "enqueued"
    latest = grown["items"][0]
    assert latest.payload["distill_mode"] == "governance_only"
    assert latest.payload["hand_distilled_paths"] == ["projects/demo/w-9.md"]
    assert rt.db.get_session("claude-code:w-9").metadata["distill_mode"] == "governance_only"


# ---- 9.3 主动入队的结构化摘要


@pytest.mark.asyncio
async def test_ingest_session_persists_a_redacted_structured_brief_and_counts_it_into_the_hash(
    tmp_path: Path,
):
    """规格场景：通过 MCP 传入结构化摘要，脱敏后随队列项持久化，并计入内容哈希。"""
    rt = _runtime(tmp_path)
    mask = "[REDACTED]"
    # Shaped like an API key, assembled here so the fixture is obviously not one.
    fake_key = "sk-" + "dummy" * 6
    brief = {
        "facts": ["连接池上限应大于并发工作线程数", f"调用示例里用的是 {fake_key}"],
        "decisions": [
            {"decision": "过期时间加随机抖动", "password": "hunter2"},
            {
                "decision": "凭据改为环境变量注入",
                "db_password": "hunter2hunter2",
                "note": "仓库里不落明文",
            },
        ],
        "domain_entities": ["连接池", "本地缓存"],
        "incidents": "灰度发布后订单查询接口延迟升高",
    }
    ingest = {
        "client": "claude",
        "session_id": "brief-1",
        "title": "缓存击穿排查",
        "summary": CONTENT,
    }
    first = await _mcp(rt, "flywheel_ingest_session", {**ingest, "structured_brief": brief})
    assert first["has_structured_brief"] is True
    assert first["queue_items"] == 1

    item = rt.db.get_queue_item(first["queue_ids"][0])
    stored = item.payload["structured_brief"]
    assert stored == {
        "facts": ["连接池上限应大于并发工作线程数", f"调用示例里用的是 {mask}"],
        "decisions": [
            {"decision": "过期时间加随机抖动", "password": mask},
            {"decision": "凭据改为环境变量注入", "db_password": mask, "note": "仓库里不落明文"},
        ],
        "domain_entities": ["连接池", "本地缓存"],
        "incidents": "灰度发布后订单查询接口延迟升高",
    }
    text = rt.sessions.load_session_text(rt.db.get_session("claude:brief-1"), max_chars=500_000)
    assert item.content_hash == compute_content_hash(text, structured_brief=stored)
    assert item.payload["text_hash"] == compute_content_hash(text)

    # The review reads the brief after the dialogue it belongs to, never as part of it.
    rendered = rt.queue.load_queue_item_text(item)
    marker = "flywheel-brief: structured brief submitted by the client "
    assert marker in rendered
    assert rendered.index(CONTENT) < rendered.index(marker)
    brief_block = rendered[rendered.index(marker):]
    assert "  - facts: 连接池上限应大于并发工作线程数" in brief_block
    assert "  - facts: 调用示例里用的是 [REDACTED]" in brief_block
    assert "  - domain_entities: 本地缓存" in brief_block
    assert "  - incidents: 灰度发布后订单查询接口延迟升高" in brief_block
    assert all(line.startswith("  - ") for line in brief_block.splitlines()[1:])

    # The same submission twice is one item; a changed brief is a new revision.
    again = await _mcp(rt, "flywheel_ingest_session", {**ingest, "structured_brief": brief})
    assert again["queue_ids"] == first["queue_ids"]
    assert len(rt.db.list_session_queue_items("claude:brief-1")) == 1
    changed = await _mcp(
        rt,
        "flywheel_ingest_session",
        {
            **ingest,
            "structured_brief": {**brief, "facts": [*brief["facts"], "回源需要互斥"]},
        },
    )
    assert changed["queue_ids"] != first["queue_ids"]
    assert _statuses(rt, "claude:brief-1") == ["queued", "excluded"]
    assert rt.db.get_queue_item(changed["queue_ids"][0]).revision == 2

    # An archive event for the same conversation recognises the transcript it already
    # holds: the brief must not make it look like new content.
    same = rt.queue.on_archive(
        "claude", "brief-1", project="demo", summary=CONTENT, content=CONTENT
    )
    assert same["action"] == "unchanged"

    # Without a brief nothing changes: no brief keys, and the fingerprint the queue
    # has always used.
    plain = await _mcp(rt, "flywheel_ingest_session", {**ingest, "session_id": "brief-2"})
    assert plain["has_structured_brief"] is False
    plain_item = rt.db.get_queue_item(plain["queue_ids"][0])
    assert "structured_brief" not in plain_item.payload
    assert "text_hash" not in plain_item.payload
    plain_text = rt.sessions.load_session_text(
        rt.db.get_session("claude:brief-2"), max_chars=500_000
    )
    assert plain_item.content_hash == compute_content_hash(plain_text)


# ---- 9.4 受控写入派单：规则本身


def test_the_dispatch_rule_reads_the_dialogue_and_skips_the_machine_bookkeeping():
    """规则只看用户自己的那一轮指令；编排器转达的消息不是对话，也不打断「只有一条派单」。"""
    cfg = SessionTriageConfig()
    relay = "The coordinator sent a message while you were working:\n请按原派单继续，完成后回报。"
    question = "请排查缓存集中过期导致连接池耗尽的问题。"
    dispatch = _render_turns(_dispatch_turns())

    # A bare dispatch: the instruction, then the writer working through it.
    assert is_controlled_write_dispatch(dispatch, cfg) is True
    # A message relayed while the writer was running is bookkeeping: still that one dispatch.
    assert (
        is_controlled_write_dispatch(f"{dispatch}\n\n[user] {relay}\n\n[assistant] 已继续执行。", cfg)
        is True
    )
    # A person's own question before or after it makes the session a conversation.
    assert (
        is_controlled_write_dispatch(
            f"[user] {question}\n\n[assistant] 好的，先看监控。\n\n[user] {relay}\n\n[assistant] 已完成。",
            cfg,
        )
        is False
    )
    assert (
        is_controlled_write_dispatch(
            f"{dispatch}\n\n[user] 根因那段请压缩到三行以内。\n\n[assistant] 已压缩。", cfg
        )
        is False
    )
    # Talking or asking about a controlled write is not performing one.
    assert (
        is_controlled_write_dispatch(
            "[user] 已批准的 write-set 超过 8 条时规划器会怎么处理？写前门禁会拦吗？\n\n"
            "[assistant] 会被要求收缩到预算以内。",
            cfg,
        )
        is False
    )
    verification = _render_turns([("user", VERIFICATION_BRIEF), ("assistant", "验证全部通过。")])
    assert is_controlled_write_dispatch(verification, cfg) is False


def test_a_persons_explicit_submission_is_kept_but_a_subagents_dispatch_is_not(tmp_path: Path):
    """规则次序：用户显式提交永远先放行；子代理记录不是用户的请求，先回答它是不是派单。"""
    rt = _runtime(tmp_path)
    cfg = rt.cfg.sessions.triage
    dispatch = _render_turns(_dispatch_turns())
    main = SessionRecord(
        session_key="claude-code:s-main",
        client="claude-code",
        session_id="s-main",
        title="知识经验卡写入",
        project="demo",
        content=dispatch,
        status=SessionStatus.PENDING,
        metadata={"explicit_ingest": True},
    )
    verdict = triage_session(main, dispatch, cfg)
    assert (verdict["admit"], verdict["tier"]) == (True, "explicit")

    subagent = main.model_copy(
        update={
            "session_key": "claude-code:s-writer",
            "session_id": "s-writer",
            "metadata": {
                "explicit_ingest": True,
                "is_subagent": True,
                "parent_session_key": "claude-code:s-main",
            },
        }
    )
    verdict = triage_session(subagent, dispatch, cfg)
    assert verdict["admit"] is False
    assert verdict["reason"] == "controlled_write_dispatch"
    assert verdict["reason_code"] == "SUBAGENT_FRAGMENT"

    # A short dispatch the built-in rule cannot recognise is admitted ...
    short = "[user] 测试派单标记：把上面的结论写成 lessons/short-note.md。\n\n[assistant] 已写入。"
    passive = SessionRecord(
        session_key="claude-code:s-short",
        client="claude-code",
        session_id="s-short",
        title="短派单",
        project="demo",
        status=SessionStatus.PENDING,
        metadata={"discovered_by": "on_archive"},
    )
    assert is_controlled_write_dispatch(short, cfg) is False
    assert triage_session(passive, short, cfg)["admit"] is True
    # ... until the installation configures the phrase its own dispatches carry.
    cfg.controlled_write_markers = ["测试派单标记"]
    assert is_controlled_write_dispatch(short, cfg) is True
    verdict = triage_session(passive, short, cfg)
    assert verdict["admit"] is False
    assert verdict["reason_code"] == "SUBAGENT_FRAGMENT"


# ---- 9.5 受控写入派单：入队拦截


def test_a_controlled_write_dispatch_is_excluded_at_every_passive_ingress(tmp_path: Path):
    """标记子代理、未标记的转录、找不到父会话的子代理、内联内容：入口处一律 excluded。"""
    rt = _runtime(tmp_path)
    archive = tmp_path / "archived_sessions"
    parent = "01934e8f-7777-8888-9999-000000000051"
    child = "01934e8f-7777-8888-9999-000000000052"
    orphan = "01934e8f-7777-8888-9999-000000000053"

    plain = tmp_path / "dispatch-plain.jsonl"
    _write_jsonl(plain, _dispatch_turns())
    attached = archive / f"rollout-2026-11-25T10-05-00-{child}.jsonl"
    _write_subagent_jsonl(attached, session_id=child, parent_id=parent, turns=_dispatch_turns())
    lost = archive / f"rollout-2026-11-25T10-06-00-{orphan}.jsonl"
    _write_subagent_jsonl(lost, session_id=orphan, parent_id="", turns=_dispatch_turns())

    cases = [
        ("claude-code", "dispatch-plain", {"transcript_path": str(plain)}),
        ("codex", child, {"transcript_path": str(attached)}),
        ("codex", orphan, {"transcript_path": str(lost)}),
        ("codex", "dispatch-inline", {"content": _render_turns(_dispatch_turns())}),
    ]
    for client, session_id, fields in cases:
        result = rt.queue.on_archive(client, session_id, project="demo", **fields)
        assert result["action"] == "excluded", (session_id, result)
        assert result["reason_code"] == "SUBAGENT_FRAGMENT", (session_id, result)
        [item] = result["items"]
        assert item.status is QueueStatus.EXCLUDED
        assert item.exclude_reason_code == "SUBAGENT_FRAGMENT"
        assert item.exclude_reason_detail == "controlled_write_dispatch"
        assert not rt.db.list_session_queue_items(item.session_key, statuses=[QueueStatus.QUEUED])
        trail = rt.db.list_audit_events(entity_type="queue_item", entity_id=item.queue_id)
        assert any(
            event["to_status"] == "excluded" and event["reason_code"] == "SUBAGENT_FRAGMENT"
            for event in trail
        )
    assert rt.db.list_queued_items_prioritized(include_subagents=True) == []


def test_a_long_brief_that_is_not_a_write_dispatch_is_still_queued(tmp_path: Path):
    """同样的长度与版式，只是没有交接已批准的 write-set：照常入队。"""
    rt = _runtime(tmp_path)
    parent = "01934e8f-7777-8888-9999-000000000061"
    child = "01934e8f-7777-8888-9999-000000000062"
    turns = [("user", VERIFICATION_BRIEF), ("assistant", "验证全部通过，两轮结果一致。")]

    transcript = tmp_path / "archived_sessions" / f"rollout-2026-11-25T11-00-00-{child}.jsonl"
    _write_subagent_jsonl(transcript, session_id=child, parent_id=parent, turns=turns)
    attached = rt.queue.on_archive("codex", child, project="demo", transcript_path=str(transcript))
    assert attached["action"] == "enqueued"
    [item] = attached["items"]
    assert item.status is QueueStatus.QUEUED
    assert item.is_subagent is True
    assert item.parent_session_key == f"codex:{parent}"

    plain = tmp_path / "verification-plain.jsonl"
    _write_jsonl(plain, turns)
    assert rt.queue.on_archive(
        "claude-code", "verification-plain", project="demo", transcript_path=str(plain)
    )["action"] == "enqueued"


def test_a_flagged_subagent_that_lost_its_parent_is_a_fragment_even_when_it_is_no_dispatch(
    tmp_path: Path,
):
    """规则 3 的加固：元数据标记的子代理找不到父会话就没有独立上下文；此前只有文件名命中的才算碎片。"""
    rt = _runtime(tmp_path)
    archive = tmp_path / "archived_sessions"
    orphan = "01934e8f-7777-8888-9999-000000000063"
    turns = [("user", VERIFICATION_BRIEF), ("assistant", "验证全部通过，两轮结果一致。")]
    transcript = archive / f"rollout-2026-11-25T11-30-00-{orphan}.jsonl"
    _write_subagent_jsonl(transcript, session_id=orphan, parent_id="", turns=turns)

    # The same brief with a parent is queued (the test above); it is no write dispatch.
    assert is_controlled_write_dispatch(_render_turns(turns), rt.cfg.sessions.triage) is False
    result = rt.queue.on_archive("codex", orphan, project="demo", transcript_path=str(transcript))
    assert result["action"] == "excluded"
    assert result["reason_code"] == "SUBAGENT_FRAGMENT"
    [item] = result["items"]
    assert item.status is QueueStatus.EXCLUDED
    assert item.exclude_reason_code == "SUBAGENT_FRAGMENT"
    assert item.exclude_reason_detail == "subagent_or_system_fragment"
    assert rt.db.list_queued_items_prioritized(include_subagents=True) == []


def test_the_pending_sweep_archives_a_dispatch_session_as_a_write_dispatch(tmp_path: Path):
    """P00 的待处理初筛走同一条规则：派单会话归档，普通会话留给审查。"""
    rt = _runtime(tmp_path)
    dispatch = tmp_path / "dispatch-sweep.jsonl"
    _write_jsonl(dispatch, _dispatch_turns())
    ordinary = tmp_path / "ordinary-sweep.jsonl"
    _write_jsonl(
        ordinary,
        [
            ("user", "请排查缓存集中过期导致连接池耗尽的问题，并给出修复方案。"),
            ("assistant", "已定位根因，给过期时间加随机抖动并补上回源互斥，压测通过。"),
        ],
    )
    for session_id, transcript in (("dispatch-sweep", dispatch), ("ordinary-sweep", ordinary)):
        rt.db.upsert_session(
            SessionRecord(
                session_key=f"claude-code:{session_id}",
                client="claude-code",
                session_id=session_id,
                title="缓存击穿排查",
                project="demo",
                transcript_path=str(transcript),
                status=SessionStatus.PENDING,
                metadata={"discovered_by": "p00_reconcile_scan"},
            )
        )

    report = rt.sessions.sweep_pending_triage()
    assert (report["checked"], report["skipped"], report["admitted"]) == (2, 1, 1)
    assert report["reasons"] == {"controlled_write_dispatch": 1}
    archived = rt.db.get_session("claude-code:dispatch-sweep")
    assert archived.status is SessionStatus.ARCHIVED
    assert archived.metadata["triage_reason"] == "controlled_write_dispatch"
    assert rt.db.get_session("claude-code:ordinary-sweep").status is SessionStatus.PENDING


# ---- 9.6 受控写入派单：存量清洗与用户决定优先


def _seed_legacy_queued_item(
    rt: FlywheelRuntime,
    key: str,
    *,
    parent_key: str,
    transcript: Path,
    title: str,
    payload: dict,
    queue_id: str,
) -> DistillQueueItem:
    """A queued row from before the dispatch rule existed."""
    return rt.db.upsert_queue_item(
        DistillQueueItem(
            queue_id=queue_id,
            session_key=key,
            parent_session_key=parent_key,
            is_subagent=True,
            client="codex",
            project="demo",
            workspace="/projects/demo",
            title=title,
            ingress_channel=IngressChannel.CODEX_ARCHIVE,
            status=QueueStatus.QUEUED,
            source_path=str(transcript),
            payload=payload,
        )
    )


def _claimable(rt: FlywheelRuntime) -> set[str]:
    """The queued items an automatic batch can claim, subagents included."""
    return {
        item.queue_id for item in rt.db.list_queued_items_prioritized(include_subagents=True)
    }


def test_re_triage_moves_a_legacy_controlled_write_subagent_item_out_of_the_queue(
    tmp_path: Path,
):
    """规格场景：存量受控写入子代理排队项在重筛后移出队列，不再被自动批次认领。"""
    rt = _runtime(tmp_path)
    parent = "01934e8f-7777-8888-9999-000000000071"
    child = "01934e8f-7777-8888-9999-000000000072"
    transcript = tmp_path / "archived_sessions" / f"rollout-2026-11-25T10-05-00-{child}.jsonl"
    _write_subagent_jsonl(transcript, session_id=child, parent_id=parent, turns=_dispatch_turns())
    key = _seed_subagent_session(
        rt, transcript, session_id=child, parent_id=parent, title="知识经验卡写入"
    )
    # Only the writer's trailing output is left of this row: the instruction that makes
    # it a dispatch survives in the transcript alone.
    legacy = _seed_legacy_queued_item(
        rt,
        key,
        parent_key=f"codex:{parent}",
        transcript=transcript,
        title="知识经验卡写入",
        queue_id="q-legacy-dispatch",
        payload={
            "chunk_text": "[assistant] 已按派单完成知识经验卡与索引的写入，写后核验全部通过。",
        },
    )
    assert legacy.queue_id in _claimable(rt)

    # A dry run names the change and writes nothing.
    preview = rt.queue.re_triage_queue(dry_run=True)
    change = next(c for c in preview["changes"] if c["queue_id"] == legacy.queue_id)
    assert (change["from_status"], change["to_status"]) == ("queued", "excluded")
    assert change["reason_code"] == "SUBAGENT_FRAGMENT"
    assert change["detail"] == "controlled_write_dispatch"
    assert rt.db.get_queue_item(legacy.queue_id).status is QueueStatus.QUEUED
    assert rt.db.list_audit_events(entity_type="queue_item", entity_id=legacy.queue_id) == []

    rt.queue.re_triage_queue(dry_run=False)
    excluded = rt.db.get_queue_item(legacy.queue_id)
    assert excluded.status is QueueStatus.EXCLUDED
    assert excluded.exclude_reason_code == "SUBAGENT_FRAGMENT"
    assert excluded.exclude_reason_detail == "controlled_write_dispatch"
    assert legacy.queue_id not in _claimable(rt)
    trail = rt.db.list_audit_events(entity_type="queue_item", entity_id=legacy.queue_id)
    assert any(
        event["from_status"] == "queued"
        and event["to_status"] == "excluded"
        and event["reason_code"] == "SUBAGENT_FRAGMENT"
        for event in trail
    )
    # The rule and the inventory now agree: a second pass has nothing left to change.
    again = rt.queue.re_triage_queue(dry_run=True)
    assert all(c["queue_id"] != legacy.queue_id for c in again["changes"])


def test_re_triage_keeps_a_writer_session_once_a_person_joined_the_conversation(tmp_path: Path):
    """派单之后用户又追问过：它已经是对话，不再是没有独立业务上下文的派单。"""
    rt = _runtime(tmp_path)
    parent = "01934e8f-7777-8888-9999-000000000081"
    child = "01934e8f-7777-8888-9999-000000000082"
    dialogue = [
        ("user", DISPATCH_BRIEF),
        ("assistant", "已写入初稿。"),
        ("user", "根因那段能不能再补一条连接池等待时间的证据？"),
        ("assistant", "已补充监控曲线的说明。"),
    ]
    transcript = tmp_path / "archived_sessions" / f"rollout-2026-11-25T10-05-00-{child}.jsonl"
    _write_subagent_jsonl(transcript, session_id=child, parent_id=parent, turns=dialogue)
    key = _seed_subagent_session(
        rt, transcript, session_id=child, parent_id=parent, title="知识经验卡写入"
    )
    legacy = _seed_legacy_queued_item(
        rt,
        key,
        parent_key=f"codex:{parent}",
        transcript=transcript,
        title="知识经验卡写入",
        queue_id="q-legacy-dialogue",
        payload={"chunk_text": _render_turns(dialogue)},
    )

    preview = rt.queue.re_triage_queue(dry_run=True)
    assert all(c["queue_id"] != legacy.queue_id for c in preview["changes"])
    rt.queue.re_triage_queue(dry_run=False)
    assert rt.db.get_queue_item(legacy.queue_id).status is QueueStatus.QUEUED
    assert legacy.queue_id in _claimable(rt)


def test_re_triage_never_overrides_a_hand_distilled_declaration(tmp_path: Path):
    """重筛是规则判定，不是用户决定：治理专属项不被丢掉，手工跳过的会话不再入队。"""
    rt = _runtime(tmp_path)
    parent = "01934e8f-7777-8888-9999-000000000091"
    child = "01934e8f-7777-8888-9999-000000000092"
    transcript = tmp_path / "archived_sessions" / f"rollout-2026-11-25T10-05-00-{child}.jsonl"
    _write_subagent_jsonl(transcript, session_id=child, parent_id=parent, turns=_dispatch_turns())
    key = _seed_subagent_session(
        rt, transcript, session_id=child, parent_id=parent, title="知识经验卡写入"
    )

    # 1. Governance only, declared on a writer session: the dispatch rule would drop it,
    #    but a person said this session is wanted for its rules.
    governance = _declare_hand_distilled(rt, key, wiki_paths=["projects/demo/note.md"])
    assert governance["status"] == "queued"
    # 2. Taken out by hand: it stays out.
    skipped = _enqueue(rt, "k-3", client="codex")
    _declare_hand_distilled(rt, "codex:k-3", governance_only=False)
    # 3. A row still queued under a skipped declaration.
    rt.db.upsert_session(
        SessionRecord(
            session_key="codex:k-4",
            client="codex",
            session_id="k-4",
            title="部署报错修复",
            project="demo",
            content=CONTENT,
            status=SessionStatus.PENDING,
            metadata={"distill_mode": "skipped", "hand_distilled_skip": True},
        )
    )
    rt.db.upsert_queue_item(
        DistillQueueItem(
            queue_id="q-legacy-skipped",
            session_key="codex:k-4",
            client="codex",
            status=QueueStatus.QUEUED,
            title="部署报错修复",
            payload={"chunk_text": CONTENT, "distill_mode": "skipped"},
        )
    )

    preview = rt.queue.re_triage_queue(dry_run=True)
    changes = {change["queue_id"]: change for change in preview["changes"]}
    assert set(changes) == {"q-legacy-skipped"}
    assert changes["q-legacy-skipped"]["reason_code"] == "HAND_DISTILLED_SKIP"
    assert rt.db.get_queue_item("q-legacy-skipped").status is QueueStatus.QUEUED

    rt.queue.re_triage_queue(dry_run=False)
    assert rt.db.get_queue_item(governance["queue_ids"][0]).status is QueueStatus.QUEUED
    assert governance["queue_ids"][0] in _claimable(rt)
    assert _statuses(rt, "codex:k-3") == ["excluded"]
    assert rt.db.get_queue_item(skipped.queue_id).exclude_reason_code == "HAND_DISTILLED_SKIP"
    legacy = rt.db.get_queue_item("q-legacy-skipped")
    assert legacy.status is QueueStatus.EXCLUDED
    assert legacy.exclude_reason_code == "HAND_DISTILLED_SKIP"


@pytest.mark.asyncio
async def test_the_mcp_surface_and_the_dictionary_know_the_new_intents(tmp_path: Path):
    """工具总数不变，新参数全部可选；排除原因字典能给出新码的名称。"""
    rt = _runtime(tmp_path)
    tools = {tool.name: tool for tool in await rt.mcp.list_tools()}
    assert len(tools) == 7
    intent = tools["flywheel_session_intent"].input_schema
    assert {"wiki_paths", "governance_only"} <= set(intent["properties"])
    ingest = tools["flywheel_ingest_session"].input_schema
    assert "structured_brief" in ingest["properties"]
    # A client that sends none of them gets the contract it always had.
    assert not {"wiki_paths", "governance_only"} & set(intent.get("required", []))
    assert "structured_brief" not in ingest.get("required", [])

    labels = {item.code: item.label_zh for item in rt.db.list_dicts(dict_type="exclude_reason")}
    assert labels["HAND_DISTILLED_SKIP"] == "客户端已手工沉淀"
