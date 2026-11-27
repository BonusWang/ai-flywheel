"""P02 会话审查契约（fix-p02-silent-review-fallback）。

桩模型按提示词首行分流 P02、格式修复与 P03。会话、审查内容与模型回复全部是虚构示例，
不含任何真实会话、项目或技能名。

最后一节覆盖 finding 的可选字段 `domain_signals`（feat-project-domain-incremental-deposit）：
会话查证过的表结构、配置、架构链路与业务口径线索，以及没有线索时的缺省。
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_flywheel.adapters.runners import (
    P02_MAX_SESSIONS,
    REVIEW_EMPTY_FINDINGS,
    REVIEW_EMPTY_REPLY,
    REVIEW_FINDINGS_NOT_LIST,
    REVIEW_SOURCE_CLI,
    REVIEW_SOURCE_HEURISTIC,
    REVIEW_SOURCE_MODEL,
    REVIEW_SOURCE_UNDECLARED,
    REVIEW_UNPARSABLE_REPLY,
    CliSubprocessRunner,
    DryRunHeuristicRunner,
    OpenAICompatibleRunner,
    PlanDiagnostics,
    PlanOutcome,
    ReviewUnavailableError,
    _build_p02_prompt,
    build_runner,
    empty_domain_signals,
    normalize_domain_signals,
)
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core import orchestrator as orchestrator_mod
from ai_flywheel.models import NodeExecution, NodeStatus, RegisterSessionRequest, RunStatus, SessionRecord, SessionStatus, WorkflowRunRecord
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.fs_store import ImmutableFileStore

REPO_ROOT = Path(__file__).resolve().parents[1]

RUN_LOGGER = "ai_flywheel"

DOWNSTREAM_NODES = ("P03_ROUTE_PLAN", "P04_WRITE_VERIFY", "P05_SETTLE", "P06_REPORT", "P07_DELIVER")

SESSION_IDS = ("s1", "s2", "s3")


# ---------------------------------------------------------------- fixtures


def _finding(session_id: str) -> dict[str, Any]:
    return {
        "session_key": f"codex:{session_id}",
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


def _review(*session_ids: str) -> str:
    """A model review with one finding per session and nothing else to persist."""
    return json.dumps(
        {"findings": [_finding(sid) for sid in session_ids], "mistakes": [], "dissents": [], "repeated_workflows": []},
        ensure_ascii=False,
    )


class _FakeModel:
    """Stands in for the model gateway: P02 gets a canned reply (a review or raw text), the format repair another, P03 an empty plan."""

    def __init__(self, p02_reply: str, *, repair_reply: str = "") -> None:
        self.p02_reply = p02_reply
        self.repair_reply = repair_reply
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt.split("\n", 1)[0])
        if prompt.startswith("You are the P02 Review Worker"):
            return self.p02_reply
        if prompt.startswith("The previous output was malformed JSON"):
            return self.repair_reply
        return '{"planned_writes": []}'


def _sessions(*session_ids: str) -> list[tuple[SessionRecord, str]]:
    return [
        (
            SessionRecord(
                session_key=f"codex:{sid}",
                client="codex",
                session_id=sid,
                title="接口超时排查",
                project="demo",
                summary="记录接口超时的根因",
            ),
            "用户：接口超时怎么排查。助手：先看连接池上限。",
        )
        for sid in session_ids
    ]


def _runtime(tmp_path: Path, *, mode: str = "api", approval_mode: str = "auto") -> tuple[FlywheelRuntime, Path]:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills_dir)]
    cfg.workflow.approval_mode = approval_mode
    # `auto` only takes effect with the safety valve open: the valve has the
    # last word, so a closed valve would run the batch as `smart`.
    cfg.safety_valves.auto_approve_writes = approval_mode == "auto"
    cfg.runner.mode = mode
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    return FlywheelRuntime(cfg), skills_dir


def _api_runner(tmp_path: Path) -> OpenAICompatibleRunner:
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    return OpenAICompatibleRunner(cfg, ImmutableFileStore(tmp_path / "receipts"))


def _api_cache_dir(tmp_path: Path) -> Path:
    """Where the API runner persists a validated reply."""
    return tmp_path / "receipts" / "api_cache"


def _register(rt: FlywheelRuntime, session_id: str = "s1") -> None:
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


def _node(run: WorkflowRunRecord, node_id: str) -> NodeExecution:
    return next(n for n in run.nodes if n.node_id == node_id)


def _event_types(rt: FlywheelRuntime, run: WorkflowRunRecord) -> list[str]:
    return [e["event_type"] for e in rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id, limit=50)]


def _payload_of(rt: FlywheelRuntime, run: WorkflowRunRecord, event_type: str) -> dict[str, Any]:
    return next(e for e in rt.db.list_audit_events(entity_type="workflow_run", entity_id=run.run_id, limit=50) if e["event_type"] == event_type)["payload"]


@pytest.fixture()
def heuristic_reviewer_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """In api/cli mode the heuristic reviewer must never review the sessions."""

    def _forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the heuristic reviewer must not be used in api/cli mode")

    monkeypatch.setattr(DryRunHeuristicRunner, "review_sessions", _forbidden)


# ---------------------------------------------------------------- baseline (current behaviour kept)


def test_baseline_runner_selection_prompt_inputs_and_review_limits(tmp_path: Path):
    # api mode without a model never reaches the network
    unconfigured = FlywheelConfig()
    unconfigured.runner.mode = "api"
    assert isinstance(build_runner(unconfigured, ImmutableFileStore(tmp_path / "receipts")), DryRunHeuristicRunner)

    sessions = [(record, f"正文{i}" * 2000) for i, (record, _) in enumerate(_sessions("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"))]
    prompt = _build_p02_prompt(sessions, [], [{"name": "release-checklist", "description": "发版检查"}], "both")

    assert "FRIDAY WEEKLY REVIEW" in prompt
    for section in ('"findings"', '"mistakes"', '"dissents"', '"repeated_workflows"'):
        assert section in prompt
    payload = json.loads(prompt.rsplit("\n\n", 1)[1])
    assert set(payload) == {"phase", "sessions", "open_retros", "existing_skills"}
    assert payload["phase"] == "both"
    assert payload["existing_skills"] == [{"name": "release-checklist", "description": "发版检查"}]
    # the prompt carries the first sessions only, each as a head+tail excerpt
    assert len(payload["sessions"]) == P02_MAX_SESSIONS == 6
    assert {len(item["text"]) for item in payload["sessions"]} == {2924}
    bodies = {
        record.session_key: f"正文{i}" * 2000
        for i, (record, _) in enumerate(_sessions("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"))
    }
    for item in payload["sessions"]:
        body = bodies[item["session_key"]]
        assert item["text"] == body[:1450] + "\n...[middle omitted]...\n" + body[-1450:]
        assert len(item["text"]) <= 3000


def test_second_identical_review_is_served_from_cache(tmp_path: Path):
    runner = _api_runner(tmp_path)
    fake = _FakeModel(_review(*SESSION_IDS))
    runner._call_chat_completion = fake

    first = runner.review_sessions(_sessions(*SESSION_IDS), [])
    second = runner.review_sessions(_sessions(*SESSION_IDS), [])

    assert first["findings"] == second["findings"]
    assert len(fake.prompts) == 1
    assert first["review_diagnostics"]["cache_hit"] is False
    assert second["review_diagnostics"]["cache_hit"] is True
    assert second["review_diagnostics"]["cache_file"].startswith("p02_review-")


def test_json_repair_retry_runs_once_and_feeds_the_review(tmp_path: Path):
    runner = _api_runner(tmp_path)
    fake = _FakeModel('{"findings": [ BROKEN JSON', repair_reply=_review("s1", "s2", "s3"))
    runner._call_chat_completion = fake

    result = runner.review_sessions(_sessions(*SESSION_IDS), [])

    assert [f["session_key"] for f in result["findings"]] == ["codex:s1", "codex:s2", "codex:s3"]
    assert [prompt.split(".")[0] for prompt in fake.prompts] == [
        "You are the P02 Review Worker of AI Flywheel",
        "The previous output was malformed JSON",
    ]
    assert result["review_diagnostics"]["repair_attempted"] is True
    assert result["review_diagnostics"]["reason_code"] is None


# ---------------------------------------------------------------- where a review comes from


def test_each_runner_states_the_source_of_its_review(tmp_path: Path):
    api = _api_runner(tmp_path)
    api._call_chat_completion = _FakeModel(_review(*SESSION_IDS))
    result = api.review_sessions(_sessions(*SESSION_IDS), [])
    assert result["review_source"] == REVIEW_SOURCE_MODEL
    assert result["runner"] == "api:mock-model"
    diagnostics = result["review_diagnostics"]
    assert diagnostics["cache_file"] and diagnostics["cache_file"].startswith("p02_review-")  # a parsed reply is cached
    assert {key: value for key, value in diagnostics.items() if key != "cache_file"} == {
        "source": REVIEW_SOURCE_MODEL,
        "sessions_sent": 3,
        "finding_count": 3,
        "reason_code": None,
        "output_chars": len(_review(*SESSION_IDS)),
        "repair_attempted": False,
        "parse_error": None,
        "repair_parse_error": None,
        "repair_error": None,
        # 桩模型没有真实调用：没有用量可记
        "call_telemetry": None,
        "cache_hit": False,
    }
    # missing sections are defaulted, as before
    assert result["mistakes"] == [] and result["dissents"] == [] and result["repeated_workflows"] == []

    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "mock-cli"
    cli = CliSubprocessRunner(cfg, ImmutableFileStore(tmp_path / "cli-receipts"))
    cli._invoke_cli = lambda stage, prompt, **kwargs: json.loads(_review("s1"))  # type: ignore[method-assign]
    assert cli.review_sessions(_sessions("s1"), [])["review_source"] == REVIEW_SOURCE_CLI

    heuristic = DryRunHeuristicRunner().review_sessions(_sessions(*SESSION_IDS), [])
    assert heuristic["review_source"] == REVIEW_SOURCE_HEURISTIC
    assert heuristic["runner"] == "dry_run"
    assert (heuristic["review_diagnostics"]["sessions_sent"], heuristic["review_diagnostics"]["finding_count"]) == (3, 3)


# ---------------------------------------------------------------- unusable reviews: reported, never replaced


def test_unparsable_reply_is_reported_with_the_reply_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heuristic_reviewer_forbidden: None
):
    runner = _api_runner(tmp_path)
    reply = "抱歉，这次没有返回结构化内容。"
    fake = _FakeModel(reply, repair_reply="仍然不是 JSON。")
    monkeypatch.setattr(runner, "_call_chat_completion", fake)

    with pytest.raises(ReviewUnavailableError) as excinfo:
        runner.review_sessions(_sessions(*SESSION_IDS), [])

    assert "模型返回的审查结果不可用（回复不是 JSON 对象），本批不审查、不写入" in str(excinfo.value)
    assert "需要本地启发式审查时把 runner.mode 切到 dry_run" in str(excinfo.value)
    diagnostics = excinfo.value.diagnostics
    assert diagnostics.reason_code == REVIEW_UNPARSABLE_REPLY
    assert (diagnostics.source, diagnostics.sessions_sent) == (REVIEW_SOURCE_MODEL, 3)
    assert diagnostics.output_chars == len(reply)
    assert diagnostics.repair_attempted is True
    assert diagnostics.parse_error == "NO_BRACES" and "回复里没有 JSON 对象" in str(excinfo.value)
    assert diagnostics.repair_parse_error == "NO_BRACES"
    assert diagnostics.cache_file is None
    assert len(fake.prompts) == 2  # the review and one format repair, nothing else


def test_unparsable_json_is_reported_with_the_decoder_error_not_the_reply(tmp_path: Path, heuristic_reviewer_forbidden: None):
    runner = _api_runner(tmp_path)
    secret = "接口密钥泄露的回复片段不得进入诊断"

    def failing(reply: str, repair_reply: str = "") -> ReviewUnavailableError:
        runner._call_chat_completion = _FakeModel(reply, repair_reply=repair_reply)
        with pytest.raises(ReviewUnavailableError) as excinfo:
            runner.review_sessions(_sessions(*SESSION_IDS), [])
        assert secret not in json.dumps(excinfo.value.diagnostics.to_dict(), ensure_ascii=False)
        assert secret not in str(excinfo.value)
        return excinfo.value

    # cut after one complete object: the decoder says where it gave up
    cut = failing('{"findings": [{"title": "接口超时排查"}, {"title": "' + secret, repair_reply='{"findings": [ BROKEN')
    assert cut.diagnostics.parse_error.startswith("Expecting ',' delimiter: line 1 column ")
    assert cut.diagnostics.repair_parse_error.startswith("UNTERMINATED_OBJECT (Expecting value")
    assert "JSON 解码报错：Expecting ',' delimiter" in str(cut)

    # cut inside a string, nothing closes the first brace: where the reply broke off is kept
    unclosed = failing('{"findings": [{"title": "' + secret)
    assert unclosed.diagnostics.parse_error.startswith("UNTERMINATED_OBJECT (Unterminated string starting at: line 1 column ")
    assert "JSON 对象没有闭合，回复像是被截断" in str(unclosed)

    # complete, but not valid JSON (a trailing comma)
    trailing = failing('{"findings": [], "mistakes": [],}')
    assert trailing.diagnostics.parse_error.startswith("Expecting property name enclosed in double quotes")


def test_empty_reply_is_reported_without_a_repair(tmp_path: Path, heuristic_reviewer_forbidden: None):
    runner = _api_runner(tmp_path)
    fake = _FakeModel("")
    runner._call_chat_completion = fake

    with pytest.raises(ReviewUnavailableError) as excinfo:
        runner.review_sessions(_sessions(*SESSION_IDS), [])

    assert excinfo.value.diagnostics.reason_code == REVIEW_EMPTY_REPLY
    assert excinfo.value.diagnostics.output_chars == 0
    assert excinfo.value.diagnostics.repair_attempted is False
    assert "回复为空" in str(excinfo.value)
    assert len(fake.prompts) == 1


def test_failed_repair_call_is_reported_as_such_not_as_a_repaired_reply(tmp_path: Path, heuristic_reviewer_forbidden: None):
    runner = _api_runner(tmp_path)

    def chat(prompt: str, system_prompt: str) -> str:
        if prompt.startswith("The previous output was malformed JSON"):
            # a transport error carries the gateway address: only the exception type may reach the diagnostics
            raise httpx.ReadTimeout(
                "timed out", request=httpx.Request("POST", "http://private-gateway.invalid/v1/chat/completions")
            )
        return "这次没有返回结构化内容。"

    runner._call_chat_completion = chat

    with pytest.raises(ReviewUnavailableError) as excinfo:
        runner.review_sessions(_sessions(*SESSION_IDS), [])

    diagnostics = excinfo.value.diagnostics
    assert diagnostics.reason_code == REVIEW_UNPARSABLE_REPLY
    assert (diagnostics.repair_attempted, diagnostics.repair_parse_error) == (True, None)
    assert diagnostics.repair_error == "ReadTimeout"
    assert "格式修复的调用没有完成（ReadTimeout）" in str(excinfo.value)
    assert "已做过一次格式修复" not in str(excinfo.value)
    assert "private-gateway" not in json.dumps(diagnostics.to_dict(), ensure_ascii=False)
    assert "private-gateway" not in str(excinfo.value)


def test_unusable_reply_is_not_cached_and_names_no_cache_file(
    tmp_path: Path, heuristic_reviewer_forbidden: None
):
    """W1.2: 缓存只在语义校验通过后写入，不可用的回复不得留下缓存文件。"""
    runner = _api_runner(tmp_path)
    fake = _FakeModel('{"reviews": []}')
    runner._call_chat_completion = fake

    with pytest.raises(ReviewUnavailableError) as first:
        runner.review_sessions(_sessions(*SESSION_IDS), [])

    assert first.value.diagnostics.reason_code == REVIEW_FINDINGS_NOT_LIST
    assert "没有 findings 列表" in str(first.value)
    # 没有通过校验的回复不会被缓存：错误里也就没有可指向的缓存文件
    assert first.value.diagnostics.cache_file is None
    assert "该输出缓存在" not in str(first.value)
    assert list(_api_cache_dir(tmp_path).glob("p02_review-*.json")) == []

    # 同样的批次不会命中缓存：模型会被再次询问，失败原因保持一致
    with pytest.raises(ReviewUnavailableError) as second:
        runner.review_sessions(_sessions(*SESSION_IDS), [])
    assert second.value.diagnostics.reason_code == REVIEW_FINDINGS_NOT_LIST
    assert second.value.diagnostics.cache_hit is False
    assert second.value.diagnostics.cache_file is None
    assert len(fake.prompts) == 2


def test_a_bad_cache_file_already_on_disk_is_named_in_the_error(
    tmp_path: Path, heuristic_reviewer_forbidden: None
):
    """W1.2: 磁盘上已存在的坏缓存文件仍要被指名，用户才能删掉它重答。"""
    runner = _api_runner(tmp_path)
    cache_dir = _api_cache_dir(tmp_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    stale = cache_dir / "p02_review-stale.json"
    stale.write_text(json.dumps({"reviews": []}), encoding="utf-8")
    runner._call_chat_completion = _FakeModel(_review(*SESSION_IDS))

    # 缓存文件的真实名字由提示词哈希决定，这里直接构造一个会被读到的坏缓存
    runner._cache_file = lambda stage, prompt: stale  # type: ignore[method-assign]

    with pytest.raises(ReviewUnavailableError) as excinfo:
        runner.review_sessions(_sessions(*SESSION_IDS), [])

    assert excinfo.value.diagnostics.reason_code == REVIEW_FINDINGS_NOT_LIST
    assert excinfo.value.diagnostics.cache_file == "p02_review-stale.json"
    assert "该输出缓存在 p02_review-stale.json" in str(excinfo.value)
    assert "删除它可让模型重答" in str(excinfo.value)


def test_empty_findings_with_sessions_sent_is_unavailable(tmp_path: Path, heuristic_reviewer_forbidden: None):
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _FakeModel('{"findings": [], "mistakes": [], "dissents": [], "repeated_workflows": []}')

    with pytest.raises(ReviewUnavailableError) as excinfo:
        runner.review_sessions(_sessions(*SESSION_IDS), [])

    assert excinfo.value.diagnostics.reason_code == REVIEW_EMPTY_FINDINGS
    assert "对 3 个会话的审查没有任何发现" in str(excinfo.value)


def test_empty_findings_without_sessions_is_accepted(tmp_path: Path):
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _FakeModel('{"findings": []}')

    result = runner.review_sessions([], [])

    assert result["findings"] == [] and result["review_diagnostics"]["finding_count"] == 0
    assert result["review_diagnostics"]["sessions_sent"] == 0


def test_json_array_reply_is_not_accepted_as_an_empty_review(tmp_path: Path, heuristic_reviewer_forbidden: None):
    # the brace slice turns the array into its only object, whose findings list is empty
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _FakeModel('[{"findings": []}]')

    with pytest.raises(ReviewUnavailableError) as excinfo:
        runner.review_sessions(_sessions(*SESSION_IDS), [])

    assert excinfo.value.diagnostics.reason_code == REVIEW_EMPTY_FINDINGS


def test_diagnostics_never_carry_the_reply_body(tmp_path: Path):
    runner = _api_runner(tmp_path)
    secret = "接口密钥泄露的回复片段不得进入诊断"
    runner._call_chat_completion = _FakeModel(f"无法解析的回复：{secret}", repair_reply=secret)

    with pytest.raises(ReviewUnavailableError) as excinfo:
        runner.review_sessions(_sessions("s1"), [])

    assert secret not in json.dumps(excinfo.value.diagnostics.to_dict(), ensure_ascii=False)
    assert secret not in str(excinfo.value)


def test_cli_runner_reports_an_unusable_output_and_traces_its_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heuristic_reviewer_forbidden: None):
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "mock-cli"
    runner = CliSubprocessRunner(cfg, ImmutableFileStore(tmp_path / "receipts"))
    monkeypatch.setattr(runner, "_invoke_cli", lambda stage, prompt, **kwargs: None)

    with pytest.raises(ReviewUnavailableError) as excinfo:
        runner.review_sessions(_sessions(*SESSION_IDS), [])
    assert excinfo.value.diagnostics.reason_code == REVIEW_UNPARSABLE_REPLY
    assert "本地 CLI 返回的审查结果不可用" in str(excinfo.value)
    assert excinfo.value.diagnostics.output_chars is None  # the stub never shows its output

    # a real local command whose output carries no JSON object: its length reaches the diagnostics
    cfg.runner.cli.command = sys.executable
    cfg.runner.cli.args = ["-c", "import sys; sys.stdin.read(); sys.stdout.write('no json here')"]
    real_runner = CliSubprocessRunner(cfg, ImmutableFileStore(tmp_path / "real-receipts"))
    with pytest.raises(ReviewUnavailableError) as real:
        real_runner.review_sessions(_sessions(*SESSION_IDS), [])
    assert real.value.diagnostics.reason_code == REVIEW_UNPARSABLE_REPLY
    assert real.value.diagnostics.output_chars == len("no json here")
    assert real.value.diagnostics.parse_error == "NO_BRACES"
    assert real.value.diagnostics.repair_attempted is False  # only the API runner repairs JSON


# ---------------------------------------------------------------- the run


def test_unavailable_review_fails_p02_instead_of_falling_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, heuristic_reviewer_forbidden: None
):
    rt, skills_dir = _runtime(tmp_path)
    _register(rt)  # the heuristic reviewer would turn "报错" into a mistake observation
    knowledge_root = rt.cfg.knowledge_root_path
    files_before = {p for p in knowledge_root.rglob("*")} if knowledge_root.exists() else set()
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel("这次没有返回结构化内容。", repair_reply="还是没有。"))

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.FAILED
    p02 = _node(run, "P02_REVIEW")
    assert p02.status == NodeStatus.FAILED
    assert p02.error.startswith("模型返回的审查结果不可用（回复不是 JSON 对象），本批不审查、不写入")
    assert "runner.mode 切到 dry_run" in p02.error
    assert p02.summary == f"节点执行失败：{p02.error}"
    assert [_node(run, node_id).status for node_id in DOWNSTREAM_NODES] == [NodeStatus.SKIPPED] * 5
    assert p02.payload["review_source"] == REVIEW_SOURCE_MODEL
    assert p02.payload["review_error"] == p02.error
    diagnostics = p02.payload["review_diagnostics"]
    assert diagnostics["reason_code"] == REVIEW_UNPARSABLE_REPLY
    assert diagnostics["parse_error"] == "NO_BRACES"
    assert (diagnostics["repair_attempted"], diagnostics["cache_file"]) == (True, None)
    assert run.planned_writes == []
    assert rt.db.get_session("codex:s1").status == SessionStatus.PENDING
    assert run.lock_released is True
    assert sorted(_event_types(rt, run)) == ["review_unavailable", "run_failed", "run_started"]
    event = _payload_of(rt, run, "review_unavailable")
    assert event["reason_code"] == REVIEW_UNPARSABLE_REPLY and event["source"] == REVIEW_SOURCE_MODEL
    assert "p02_review" not in event["message"]  # nothing was cached for an unparsable reply
    assert rt.db.list_mistake_observations() == []
    assert {p for p in knowledge_root.rglob("*")} == files_before
    assert not list(skills_dir.rglob("SKILL.md"))
    assert any("P02 review unavailable" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def test_empty_findings_fail_p02_and_cache_no_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heuristic_reviewer_forbidden: None
):
    rt, _ = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel('{"findings": []}'))

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.FAILED
    p02 = _node(run, "P02_REVIEW")
    assert p02.payload["review_diagnostics"]["reason_code"] == REVIEW_EMPTY_FINDINGS
    assert "对 1 个会话的审查没有任何发现" in p02.error
    # W1.2: 校验没过的回复不写缓存，错误里也就没有缓存文件可指
    assert p02.payload["review_diagnostics"]["cache_file"] is None
    assert "该输出缓存在" not in p02.error
    assert list(_api_cache_dir(tmp_path).glob("p02_review-*.json")) == []
    assert rt.db.get_session("codex:s1").status == SessionStatus.PENDING
    assert "review_unavailable" in _event_types(rt, run)


def test_model_review_states_its_source(tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch):
    rt, _ = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel(_review("s1")))

    with caplog.at_level(logging.INFO, logger=RUN_LOGGER):
        run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    p02 = _node(run, "P02_REVIEW")
    assert p02.status == NodeStatus.PASS
    assert p02.summary.startswith("审查来源：API 模型；完成 1 段对话回顾")
    assert p02.payload["review_source"] == REVIEW_SOURCE_MODEL
    assert p02.payload["review_diagnostics"]["sessions_sent"] == 1
    assert p02.payload["review_diagnostics"]["finding_count"] == 1
    assert p02.payload["findings"][0]["facts"] == ["连接池上限过小导致超时"]
    assert rt.db.get_session("codex:s1").status == SessionStatus.PROCESSED
    assert "review_unavailable" not in _event_types(rt, run)
    assert any("P02 review" in r.getMessage() and "model" in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)


def test_run_without_admitted_sessions_accepts_an_empty_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt, _ = _runtime(tmp_path)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel('{"findings": []}'))

    run = rt.orchestrator.start_run(trigger="test", phase="daily", auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    p02 = _node(run, "P02_REVIEW")
    assert p02.status == NodeStatus.PASS
    assert p02.summary.startswith("审查来源：API 模型；完成 0 段对话回顾")


def test_dry_run_run_states_the_heuristic_source(tmp_path: Path):
    rt, _ = _runtime(tmp_path, mode="dry_run")
    _register(rt)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    p02 = _node(run, "P02_REVIEW")
    assert p02.summary.startswith("审查来源：本地启发式审查器；完成 1 段对话回顾")
    assert p02.payload["review_source"] == REVIEW_SOURCE_HEURISTIC


def test_cli_run_states_its_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = FlywheelConfig(base_dir=tmp_path)
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg.capabilities.skills_dirs = [str(tmp_path / "skills")]
    (tmp_path / "skills").mkdir()
    cfg.workflow.approval_mode = "auto"
    cfg.safety_valves.auto_approve_writes = True
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "mock-cli"
    rt = FlywheelRuntime(cfg)
    _register(rt)
    monkeypatch.setattr(CliSubprocessRunner, "_invoke_cli", lambda self, stage, prompt, **kwargs: json.loads('{"planned_writes": []}' if stage == "p03_plan" else _review("s1")))

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    p02 = _node(run, "P02_REVIEW")
    assert p02.summary.startswith("审查来源：本地 CLI；完成 1 段对话回顾")
    assert p02.payload["review_source"] == REVIEW_SOURCE_CLI


def test_runner_without_a_declared_source_is_labelled_undeclared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt, _ = _runtime(tmp_path)
    _register(rt)

    class _SilentRunner:
        """Reviews without saying where the review came from."""

        def review_sessions(self, sessions, open_retros, *, existing_skills=None, phase="daily") -> dict:
            return {"findings": [], "mistakes": [], "dissents": [], "repeated_workflows": []}

        def plan_writes_detailed(self, *args: Any, **kwargs: Any) -> PlanOutcome:
            return PlanOutcome([], PlanDiagnostics(source=REVIEW_SOURCE_HEURISTIC))

    monkeypatch.setattr(orchestrator_mod, "build_runner", lambda cfg, fs_store: _SilentRunner())

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    p02 = _node(run, "P02_REVIEW")
    assert p02.summary.startswith("审查来源：未声明；完成 0 段对话回顾")
    assert p02.payload["review_source"] == REVIEW_SOURCE_UNDECLARED


# ---------------------------------------------------------------- the optional `domain_signals` of a finding
# (feat-project-domain-incremental-deposit)


_OMITTED = object()

EMPTY_SIGNALS = {"database_tables": [], "project_configs": [], "architecture_flows": [], "business_calibers": []}


def _domain_signals() -> dict[str, Any]:
    """What a reviewer records for a session that verified the `orders` table and its connection constraint."""
    return {
        "database_tables": [
            {
                "table": "orders",
                "verified_fields": ["order_id bigint 主键", "status varchar(32)"],
                "enums": ["status：PAID / CLOSED"],
                "join_keys": ["customer_id -> customers.customer_id"],
                "pitfalls": ["status 没有空值"],
            }
        ],
        "project_configs": [
            {
                "topic": "database",
                "facts": ["逻辑库 orders 落在实例 db-orders-1", "跨库关联只能在应用层做"],
                "pitfalls": ["测试库与生产库不是同一实例"],
            }
        ],
        "architecture_flows": ["订单服务 -> 支付服务（同步回调）"],
        "business_calibers": ["客单价按已支付金额统计"],
    }


def _signals_review() -> str:
    """A model review whose finding carries the domain signals of its session."""
    return json.dumps(
        {"findings": [dict(_finding("s1"), domain_signals=_domain_signals())], "mistakes": [], "dissents": [], "repeated_workflows": []},
        ensure_ascii=False,
    )


def test_p02_prompt_asks_for_verified_domain_signals_and_keeps_its_input():
    sessions = [(record, f"正文{i}") for i, (record, _) in enumerate(_sessions("s1", "s2"))]
    prompt = _build_p02_prompt(sessions, [], None, "daily")

    assert prompt.startswith("You are the P02 Review Worker of AI Flywheel.\n")
    # Only what the session itself verified, an untouched domain stays empty, and nothing is inferred.
    for needle in (
        "DOMAIN SIGNALS",
        "it holds ONLY what this session itself queried, read in the code or verified",
        "database_tables",
        "project_configs",
        "architecture_flows",
        "business_calibers",
        "a full DDL is never needed",
        "Leave a domain the session did not touch empty ([])",
        "never infer, generalise or pad it",
        "omit the whole field when a finding has no such clue",
        "NEVER write a password, token or private key into project_configs",
    ):
        assert needle in prompt, needle
    # The output contract shows the four keys and the shape of their entries.
    contract = prompt.split("Return STRICT JSON ONLY:\n", 1)[1].split("\n\n", 1)[0]
    for shape in (
        '"domain_signals":{"database_tables":[{"table":"table_name","verified_fields":["..."],"enums":["..."],',
        '"join_keys":["..."],"pitfalls":["..."]}],"project_configs":[{"topic":"config_topic","facts":["..."],',
        '"pitfalls":["..."]}],"architecture_flows":["..."],"business_calibers":["..."]}',
    ):
        assert shape in contract, shape
    # The input JSON is what it was: the reviewer is told nothing about the Wiki or its projects.
    payload = json.loads(prompt.rsplit("\n\n", 1)[1])
    assert set(payload) == {"phase", "sessions", "open_retros", "existing_skills"}
    assert empty_domain_signals() == EMPTY_SIGNALS


def test_a_troubleshooting_session_yields_its_table_and_configuration_clues(tmp_path: Path):
    runner = _api_runner(tmp_path)
    # The session under review mentions a credential; a finding records where it is kept, never its value.
    password = "Hk9-Qp31x"
    session_text = (
        "用户：订单状态对不上，跨库关联也查不出来。"
        f"助手：查了 orders 表的 status 枚举和 customer_id；逻辑库 orders 在实例 db-orders-1；数据库密码：{password}。"
    )
    runner._call_chat_completion = _FakeModel(_signals_review())

    result = runner.review_sessions([(_sessions("s1")[0][0], session_text)], [])

    (finding,) = result["findings"]
    assert finding["domain_signals"] == _domain_signals()
    # The rest of the finding is the finding the model wrote
    assert {key: value for key, value in finding.items() if key != "domain_signals"} == _finding("s1")
    # The configuration clues carry the facts a later session must remember, and no password
    assert finding["domain_signals"]["project_configs"][0]["facts"] == ["逻辑库 orders 落在实例 db-orders-1", "跨库关联只能在应用层做"]
    assert password not in json.dumps(result, ensure_ascii=False)
    assert result["review_diagnostics"]["reason_code"] is None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_OMITTED, id="omitted"),
        pytest.param(None, id="null"),
        pytest.param("", id="empty-text"),
        pytest.param([], id="list"),
        pytest.param("orders", id="text"),
        pytest.param({"database_tables": {"table": "orders"}}, id="wrong-shape"),
    ],
)
def test_a_finding_without_domain_signals_is_completed_and_never_fails_the_review(tmp_path: Path, raw: Any):
    finding = _finding("s1")
    if raw is not _OMITTED:
        finding["domain_signals"] = raw
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _FakeModel(json.dumps({"findings": [finding, _finding("s2")]}, ensure_ascii=False))

    result = runner.review_sessions(_sessions("s1", "s2"), [])

    # Neither finding is dropped, the review does not fail, and each finding carries the four empty keys.
    assert [f["session_key"] for f in result["findings"]] == ["codex:s1", "codex:s2"]
    assert [f["domain_signals"] for f in result["findings"]] == [EMPTY_SIGNALS, EMPTY_SIGNALS]
    assert result["findings"][0]["facts"] == ["连接池上限过小导致超时"]
    assert result["review_diagnostics"]["finding_count"] == 2


def test_a_run_shows_the_domain_signals_in_the_review_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt, _ = _runtime(tmp_path)
    _register(rt)
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", _FakeModel(_signals_review()))

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    (finding,) = _node(run, "P02_REVIEW").payload["findings"]
    assert finding["domain_signals"] == _domain_signals()


def test_malformed_domain_signals_are_tidied_and_never_fail_the_review(tmp_path: Path):
    finding = _finding("s1")
    finding["domain_signals"] = {
        "database_tables": [
            "payments",  # a bare table name
            {"table": "orders", "verified_fields": "order_id"},  # one field given as text
            {"table": "orders", "verified_fields": ["order_id"], "enums": []},  # the same table again
            {"verified_fields": ["orphan"]},  # no table named
            42,  # not an entry
        ],
        "project_configs": {"topic": "database"},  # not a list
        "architecture_flows": ["订单服务 -> 支付服务", "订单服务 -> 支付服务", " ", None],
        "business_calibers": [f"口径{i}" for i in range(25)],
        "something_else": ["not one of the four domains"],
    }
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _FakeModel(json.dumps({"findings": [finding]}, ensure_ascii=False))

    result = runner.review_sessions(_sessions("s1"), [])

    # What can be read is kept in the one shape; what cannot be is dropped; the review itself is fine.
    assert result["findings"][0]["domain_signals"] == {
        "database_tables": [
            {"table": "payments", "verified_fields": [], "enums": [], "join_keys": [], "pitfalls": []},
            {"table": "orders", "verified_fields": ["order_id"], "enums": [], "join_keys": [], "pitfalls": []},
        ],
        "project_configs": [],
        "architecture_flows": ["订单服务 -> 支付服务"],
        "business_calibers": [f"口径{i}" for i in range(20)],
    }
    assert result["review_diagnostics"]["reason_code"] is None


def test_domain_signals_are_bounded():
    long_caliber = "口径" * 120  # 240 characters
    signals = normalize_domain_signals({"business_calibers": ["统计口径", "统计口径", " ", None, long_caliber]})
    assert signals == {**EMPTY_SIGNALS, "business_calibers": ["统计口径", long_caliber[:160]]}

    tables = normalize_domain_signals({"database_tables": [f"table_{i}" for i in range(25)]})["database_tables"]
    assert [table["table"] for table in tables] == [f"table_{i}" for i in range(20)]

    fields = normalize_domain_signals({"database_tables": [{"table": "orders", "verified_fields": [f"f{i}" for i in range(15)]}]})
    assert fields["database_tables"] == [
        {"table": "orders", "verified_fields": [f"f{i}" for i in range(12)], "enums": [], "join_keys": [], "pitfalls": []}
    ]


def test_a_credential_inside_a_domain_signal_is_masked(tmp_path: Path):
    finding = _finding("s1")
    finding["domain_signals"] = {
        "project_configs": [
            {"topic": "database", "facts": ["数据库密码：Hk9-Qp31x", "密码走环境变量注入，仓库里不落明文"]},
        ],
    }
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _FakeModel(json.dumps({"findings": [finding]}, ensure_ascii=False))

    result = runner.review_sessions(_sessions("s1"), [])

    # A password never travels from a review into a plan: the line is kept, the value is not.
    assert result["findings"][0]["domain_signals"] == {
        **EMPTY_SIGNALS,
        "project_configs": [
            {
                "topic": "database",
                "facts": ["数据库密码：[REDACTED]", "密码走环境变量注入，仓库里不落明文"],
                "pitfalls": [],
            }
        ],
    }
    assert "Hk9-Qp31x" not in json.dumps(result, ensure_ascii=False)


def test_every_reviewer_states_the_four_keys(tmp_path: Path):
    heuristic = DryRunHeuristicRunner().review_sessions(_sessions(*SESSION_IDS), [])
    # The local reviewer reads no table, configuration or architecture: it states the empty structure.
    assert [f["domain_signals"] for f in heuristic["findings"]] == [EMPTY_SIGNALS] * 3

    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "mock-cli"
    cli = CliSubprocessRunner(cfg, ImmutableFileStore(tmp_path / "cli-receipts"))
    cli._invoke_cli = lambda stage, prompt, **kwargs: json.loads(_signals_review())  # type: ignore[method-assign]
    (finding,) = cli.review_sessions(_sessions("s1"), [])["findings"]
    assert finding["domain_signals"] == _domain_signals()


def test_a_cached_review_carries_its_domain_signals(tmp_path: Path):
    runner = _api_runner(tmp_path)
    fake = _FakeModel(_signals_review())
    runner._call_chat_completion = fake

    first = runner.review_sessions(_sessions("s1"), [])
    second = runner.review_sessions(_sessions("s1"), [])

    assert len(fake.prompts) == 1
    assert (first["review_diagnostics"]["cache_hit"], second["review_diagnostics"]["cache_hit"]) == (False, True)
    assert second["findings"] == first["findings"]


def test_a_reply_cached_before_the_field_existed_is_completed_and_served(tmp_path: Path):
    runner = _api_runner(tmp_path)
    stale = _api_cache_dir(tmp_path) / "p02_review-before-signals.json"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(_review("s1", "s2"), encoding="utf-8")  # a valid review that predates the optional field
    runner._cache_file = lambda stage, prompt: stale  # type: ignore[method-assign]
    fake = _FakeModel(_signals_review())
    runner._call_chat_completion = fake

    result = runner.review_sessions(_sessions("s1", "s2"), [])

    assert fake.prompts == []  # served from the cache, the model is not asked again
    assert result["review_diagnostics"]["cache_hit"] is True
    assert [f["domain_signals"] for f in result["findings"]] == [EMPTY_SIGNALS, EMPTY_SIGNALS]
