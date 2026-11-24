"""Wave 1A（W1.0 / W1.1）回归测试：事务回滚、会话守恒、Hook 生命周期保护与结算。

覆盖：
- W1.0 `connect()` 的块内异常必须回滚该块已写入的数据；`write_tx` 仍显式走手动事务。
- W1.1.1 P00 的优先级排序落在 SQL 里；显式指定的会话逐个取回，不被最近窗口挤掉。
- W1.1.2 迟到的生命周期 Hook 不得改写终态会话的状态，标签取并集、元数据合并。
- W1.1.3 P01 准入上限守恒；P02 提示词是头尾摘录；P05 只结算真正被审查过的会话。
- W1.1.4 被结算会话的待复盘项随本批关闭并挂上 run_id。
- W1.1.5 一次失败校验只记一次复发计数。

会话、项目、技能名全部是虚构示例。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ai_flywheel.adapters.runners import (
    P02_MAX_SESSIONS,
    excerpt_session_text,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import (
    CheckResult,
    NodeExecution,
    OutcomeCheck,
    OutcomeOrigin,
    OutcomeStatus,
    RecordOutcomeRequest,
    RegisterSessionRequest,
    ReportSignalRequest,
    RetroStatus,
    RunStatus,
    SessionRecord,
    SessionStatus,
    SignalKind,
    VerificationKind,
    WorkflowRunRecord,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.db import FlywheelDB

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------- helpers ----------------


def _runtime(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills)]
    return FlywheelRuntime(cfg)


def _register(rt: FlywheelRuntime, session_id: str, *, tags: list[str] | None = None, **kwargs) -> SessionRecord:
    return rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id=session_id,
            title=kwargs.pop("title", "接口超时排查"),
            project=kwargs.pop("project", "demo"),
            summary=kwargs.pop("summary", "记录一次超时排查"),
            content=kwargs.pop("content", "用户：接口联调报错了怎么办。助手：先看日志，再核对连接池上限。"),
            tags=tags or [],
            **kwargs,
        )
    )


def _node(run: WorkflowRunRecord, node_id: str) -> NodeExecution:
    return next(n for n in run.nodes if n.node_id == node_id)


def _finding(session_key: str) -> dict:
    return {
        "session_key": session_key,
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


def _review_for(*session_keys: str) -> dict:
    return {
        "findings": [_finding(sk) for sk in session_keys],
        "mistakes": [],
        "dissents": [],
        "repeated_workflows": [],
    }


class _FakeModel:
    """A stub model gateway: a canned review for P02, an empty plan for P03."""

    def __init__(self, review: dict) -> None:
        self.review = review
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        import json

        self.prompts.append(prompt.split("\n", 1)[0])
        if prompt.startswith("You are the P02 Review Worker"):
            return json.dumps(self.review, ensure_ascii=False)
        return '{"planned_writes": []}'


def _fake_model(review: dict) -> _FakeModel:
    return _FakeModel(review)


def _only_reviewing(monkeypatch: pytest.MonkeyPatch, keep: list[str]) -> None:
    """Make the API runner report that it only read part of the batch.

    A reviewer that did not read a session must not let P05 settle it.
    """
    from ai_flywheel.adapters.runners import OpenAICompatibleRunner

    original = OpenAICompatibleRunner.review_sessions

    def _review(self, sessions, open_retros, **kwargs):  # type: ignore[no-untyped-def]
        result = original(self, sessions, open_retros, **kwargs)
        result["sent_session_keys"] = [k for k in result.get("sent_session_keys", []) if k in keep]
        return result

    monkeypatch.setattr(OpenAICompatibleRunner, "review_sessions", _review)


# ---------------- W1.0 事务回滚 ----------------


def test_an_exception_inside_a_connect_block_rolls_back_its_writes(tmp_path: Path):
    """W1.0: `connect()` 必须是延迟事务，块内异常要把该块已写入的行回滚掉。"""
    db = FlywheelDB(tmp_path / "flywheel.db")

    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (session_key, client, session_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("codex:rolled-back", "codex", "rolled-back", "2026-11-25T00:00:00Z", "2026-11-25T00:00:00Z"),
            )
            raise RuntimeError("boom")

    assert db.get_session("codex:rolled-back") is None
    with db.connect() as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM sessions WHERE session_key = ?", ("codex:rolled-back",)).fetchone()[0])
    assert count == 0


def test_a_connect_block_that_finishes_commits_its_writes(tmp_path: Path):
    db = FlywheelDB(tmp_path / "flywheel.db")

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sessions (session_key, client, session_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("codex:kept", "codex", "kept", "2026-11-25T00:00:00Z", "2026-11-25T00:00:00Z"),
        )

    assert db.get_session("codex:kept") is not None


def test_write_tx_still_rolls_back_on_an_exception(tmp_path: Path):
    """W1.0: `write_tx` 显式使用 `isolation_level=None` 并自��管理 BEGIN/COMMIT/ROLLBACK。"""
    db = FlywheelDB(tmp_path / "flywheel.db")

    with pytest.raises(RuntimeError):
        with db.write_tx() as conn:
            conn.execute(
                "INSERT INTO sessions (session_key, client, session_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("codex:tx-rolled-back", "codex", "tx-rolled-back", "2026-11-25T00:00:00Z", "2026-11-25T00:00:00Z"),
            )
            raise RuntimeError("boom")

    assert db.get_session("codex:tx-rolled-back") is None


# ---------------- W1.1.1 P00 优先级与显式会话 ----------------


def test_pending_sessions_are_prioritized_inside_sql(tmp_path: Path):
    """W1.1.1: 优先级排序必须在 SQL 里，否则 P0_CORE 会被最近窗口挤掉。"""
    db = FlywheelDB(tmp_path / "flywheel.db")
    now = "2026-11-25T00:00:00Z"
    with db.connect() as conn:
        # 60 条普通会话都比核心会话“新”，但核心会话排在第 55 行
        for i in range(60):
            conn.execute(
                "INSERT INTO sessions (session_key, client, session_id, priority, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"codex:general-{i}", "codex", f"general-{i}", "P1_GENERAL", SessionStatus.PENDING.value, now, f"2026-11-25T01:{i:02d}:00Z"),
            )
        conn.execute(
            "INSERT INTO sessions (session_key, client, session_id, priority, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("codex:core-1", "codex", "core-1", "P0_CORE", SessionStatus.PENDING.value, now, "2026-11-24T00:00:00Z"),
        )

    prioritized = db.list_pending_sessions_prioritized(limit=50)
    assert len(prioritized) == 50
    # 核心会话即使是最旧的一条，也在最前面
    assert prioritized[0].session_key == "codex:core-1"
    assert "codex:core-1" in {s.session_key for s in prioritized}


def test_explicit_session_keys_are_fetched_one_by_one_not_from_a_limited_page(tmp_path: Path):
    """W1.1.1: 显式点名的会话必须逐个取回，不能靠过滤一页最近会话。"""
    rt = _runtime(tmp_path)
    target = _register(rt, "target-1")
    for i in range(220):
        _register(rt, f"noise-{i}")

    run = rt.orchestrator.start_run(
        trigger="test",
        phase="daily",
        session_keys=["codex:target-1"],
        auto_approve_override=True,
    )

    assert run.status == RunStatus.PASS, run.error
    assert run.admitted_sessions == ["codex:target-1"]
    assert rt.db.get_session(target.session_key).status == SessionStatus.PROCESSED


# ---------------- W1.1.2 Hook 生命周期保护 ----------------


def test_a_late_lifecycle_hook_does_not_reopen_a_settled_session(tmp_path: Path):
    """W1.1.2: Stop / SessionEnd 这类迟到的 Hook 不得把已结算的会话改回 pending。"""
    rt = _runtime(tmp_path)
    _register(rt, "s1", tags=["core"])
    rt.db.update_session_status("codex:s1", SessionStatus.PROCESSED)
    rt.db.upsert_session(
        SessionRecord(
            session_key="codex:s1",
            client="codex",
            session_id="s1",
            title="接口超时排查",
            status=SessionStatus.PROCESSED,
            tags=["core"],
            metadata={"decided": "kept"},
        )
    )

    out = rt.hooks.handle(
        "codex",
        {
            "hook_event_name": "SessionEnd",
            "session_id": "s1",
            "turn_id": "t9",
            "title": "接口超时排查（迟到的 Hook）",
        },
    )
    assert out["continue"] is True

    record = rt.db.get_session("codex:s1")
    assert record.status == SessionStatus.PROCESSED
    assert record.metadata.get("decided") == "kept"
    assert record.metadata.get("last_hook_event") == "SessionEnd"
    assert "core" in record.tags


def test_a_lifecycle_hook_unions_the_tags_of_a_settled_session(tmp_path: Path):
    rt = _runtime(tmp_path)
    _register(rt, "s1", tags=["core"])
    rt.db.update_session_status("codex:s1", SessionStatus.ARCHIVED)

    rt.hooks.handle(
        "codex",
        {"hook_event_name": "Stop", "session_id": "s1", "turn_id": "t1", "title": "x"},
    )

    record = rt.db.get_session("codex:s1")
    assert record.status == SessionStatus.ARCHIVED
    assert record.tags == ["core"]


def test_an_explicit_ingestion_may_still_requeue_a_processed_session(tmp_path: Path):
    """W1.1.2: 显式的人工/MCP 录入不受生命周期保护：新内容仍然可以重新入队。"""
    rt = _runtime(tmp_path)
    _register(rt, "s1", content="第一版正文")
    rt.db.update_session_status("codex:s1", SessionStatus.PROCESSED)

    _register(rt, "s1", content="第二版正文，补了新的排障步骤")

    record = rt.db.get_session("codex:s1")
    assert record.status == SessionStatus.PENDING
    assert record.content == "第二版正文，补了新的排障步骤"


# ---------------- W1.1.3 会话守恒 ----------------


def test_p01_caps_the_admitted_sessions_at_the_review_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """W1.1.3: 模型审查只看得到前 P02_MAX_SESSIONS 段，多出来的必须留在 pending。"""
    rt = _runtime(tmp_path)
    rt.cfg.runner.mode = "api"
    rt.cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    rt.cfg.runner.api.model = "mock-model"
    rt.cfg.workflow.max_sessions_per_run = 20
    for i in range(10):
        _register(rt, f"s{i}")

    from ai_flywheel.adapters.runners import OpenAICompatibleRunner

    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _fake_model(_review_for(10)))

    run = rt.orchestrator.start_run(trigger="test", phase="daily", auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    assert len(run.admitted_sessions) == P02_MAX_SESSIONS
    # 多出来的会话仍然是 pending，等下一批
    pending = [
        s.session_key
        for s in rt.db.list_pending_sessions_prioritized(limit=50)
        if s.session_key.startswith("codex:s")
    ]
    assert len(pending) == 10 - P02_MAX_SESSIONS


def test_the_p02_prompt_keeps_the_head_and_the_tail_of_a_long_session():
    """W1.1.3: 长会话取头尾摘录，中间明确标注省略，不超过 3000 字符。"""
    body = "".join(f"{i:04d}" for i in range(2000))  # 8000 字符
    excerpt = excerpt_session_text(body)

    assert len(excerpt) <= 3000
    assert excerpt.startswith(body[:1450])
    assert excerpt.endswith(body[-1450:])
    assert "\n...[middle omitted]...\n" in excerpt


def test_a_short_session_is_passed_through_unchanged():
    body = "短会话正文"
    assert excerpt_session_text(body) == body


def test_p05_settles_only_the_sessions_that_were_reviewed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W1.1.3: 没被送进审查的会话退回 pending，不得被当成已沉淀结算掉。"""
    rt = _runtime(tmp_path)
    rt.cfg.runner.mode = "api"
    rt.cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    rt.cfg.runner.api.model = "mock-model"
    for i in range(3):
        _register(rt, f"s{i}")

    from ai_flywheel.adapters.runners import OpenAICompatibleRunner

    monkeypatch.setattr(
        OpenAICompatibleRunner, "_call_chat_completion", _fake_model(_review_for("codex:s0"))
    )
    _only_reviewing(monkeypatch, ["codex:s0"])

    run = rt.orchestrator.start_run(trigger="test", phase="daily", auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    # 只有真正被审查过的那一段被结算
    assert rt.db.get_session("codex:s0").status == SessionStatus.PROCESSED
    assert rt.db.get_session("codex:s1").status == SessionStatus.PENDING
    assert rt.db.get_session("codex:s2").status == SessionStatus.PENDING

    p05 = _node(run, "P05_SETTLE")
    assert p05.payload["settled_sessions"] == ["codex:s0"]
    assert sorted(p05.payload["returned_sessions"]) == ["codex:s1", "codex:s2"]


# ---------------- W1.1.4 待复盘项结算 ----------------


def test_settling_a_session_closes_its_open_retro_items(tmp_path: Path):
    """W1.1.4: 被结算会话的 OPEN 待复盘项随本批关闭，并挂上 run_id。"""
    rt = _runtime(tmp_path)
    _register(rt, "s1")
    rt.receipts.report_signal(
        ReportSignalRequest(
            session_key="codex:s1",
            client="codex",
            session_id="s1",
            kind=SignalKind.HIGH_RISK_TOOL,
            severity="high",
            summary="检测到高危命令模式",
        )
    )
    assert len(rt.db.list_retro_items(status=RetroStatus.OPEN.value)) == 1

    run = rt.orchestrator.start_run(
        trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True
    )
    assert run.status == RunStatus.PASS, run.error

    resolved = rt.db.list_retro_items(status=RetroStatus.RESOLVED.value)
    assert len(resolved) == 1
    assert resolved[0].session_key == "codex:s1"
    with rt.db.connect() as conn:
        row = conn.execute(
            "SELECT linked_run_id FROM retro_queue WHERE retro_id = ?", (resolved[0].retro_id,)
        ).fetchone()
    assert row["linked_run_id"] == run.run_id

    p05 = _node(run, "P05_SETTLE")
    assert p05.payload["settled_retro_count"] == 1


def test_the_retro_items_of_returned_sessions_stay_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W1.1.4: 没有被审查的会话，它的待复盘项必须继续开着。"""
    rt = _runtime(tmp_path)
    rt.cfg.runner.mode = "api"
    rt.cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    rt.cfg.runner.api.model = "mock-model"
    _register(rt, "s0")
    _register(rt, "s1")
    rt.receipts.report_signal(
        ReportSignalRequest(
            session_key="codex:s1",
            client="codex",
            session_id="s1",
            kind=SignalKind.HIGH_RISK_TOOL,
            severity="high",
            summary="检测到高危命令模式",
        )
    )

    from ai_flywheel.adapters.runners import OpenAICompatibleRunner

    monkeypatch.setattr(
        OpenAICompatibleRunner, "_call_chat_completion", _fake_model(_review_for("codex:s0"))
    )
    _only_reviewing(monkeypatch, ["codex:s0"])

    run = rt.orchestrator.start_run(trigger="test", phase="daily", auto_approve_override=True)
    assert run.status == RunStatus.PASS, run.error

    assert len(rt.db.list_retro_items(status=RetroStatus.OPEN.value)) == 1
    assert rt.db.list_retro_items(status=RetroStatus.RESOLVED.value) == []
    assert rt.db.get_session("codex:s1").status == SessionStatus.PENDING


# ---------------- W1.1.5 复发计数只记一次 ----------------


def test_one_failed_check_counts_one_recurrence(tmp_path: Path):
    """W1.1.5: `revalidate_from_outcome` 已经计过复发，发信号时不能再加一次。"""
    rt = _runtime(tmp_path)
    rule = rt.activation.register_rule(
        _rule_request("rule-cache-check", origin_session_key="codex:s0")
    )

    rt.receipts.record_outcome(
        RecordOutcomeRequest(
            session_key="codex:s1",
            client="codex",
            origin=OutcomeOrigin.NATURAL,
            status=OutcomeStatus.FAILED,
            summary="只读本地内存缓存不适用该失效键检查",
            applied_rule_ids=[rule.rule_id],
            checks=[
                OutcomeCheck(
                    name="cache-check",
                    result=CheckResult.FAIL,
                    kind=VerificationKind.REAL,
                    rule_id=rule.rule_id,
                    evidence=["日志行 42"],
                    note="缓存键未失效",
                )
            ],
        )
    )

    updated = rt.db.get_rule(rule.rule_id)
    assert updated.recurrence_count == 1
    # 待复盘项仍然入队了：只是不再重复计数
    retros = rt.db.list_retro_items(status=RetroStatus.OPEN.value)
    assert len(retros) == 1
    assert retros[0].rule_id == rule.rule_id


def test_a_signal_without_an_outcome_still_counts_one_recurrence(tmp_path: Path):
    """没有 outcome 兜底时，信号自己计一次复发——不多不少。"""
    rt = _runtime(tmp_path)
    rule = rt.activation.register_rule(
        _rule_request("rule-cache-check", origin_session_key="codex:s0")
    )

    rt.receipts.report_signal(
        ReportSignalRequest(
            session_key="codex:s2",
            client="codex",
            session_id="s2",
            kind=SignalKind.RULE_RECURRENCE,
            severity="high",
            summary="规则再次失效",
            rule_id=rule.rule_id,
        )
    )

    assert rt.db.get_rule(rule.rule_id).recurrence_count == 1


def _rule_request(rule_id: str, *, origin_session_key: str = ""):
    from ai_flywheel.models import CreateRuleRequest

    return CreateRuleRequest(
        rule_id=rule_id,
        title="缓存变更前必须校验失效策略",
        triggers=["缓存", "cache"],
        checklist=["确认 TTL 与失效键"],
        origin_session_key=origin_session_key,
    )
