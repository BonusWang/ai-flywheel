"""P02 会话审查契约（fix-p02-silent-review-fallback）。

桩模型按提示词首行分流 P02、格式修复与 P03。会话、审查内容与模型回复全部是虚构示例，
不含任何真实会话、项目或技能名。

最后一节覆盖 finding 的可选字段 `domain_signals`（feat-project-domain-incremental-deposit）：
会话查证过的表结构、配置、架构链路与业务口径线索，以及没有线索时的缺省。

再往后的三节覆盖高保真输入、事件拆分与分阶段模型（feat-agy-ingest-and-high-fidelity-distill）：
16000 字会话预算、置顶的结构化摘要、`events` 契约，以及 P02 / P03 各自的 `review_model`、
`plan_model` 与推理强度。模型网关用记录请求体的桩代替，不发出任何真实请求。

末尾三节覆盖发版前整改的三条：引用了凭据的审查照常可用，但回包在 API 与 CLI 两条通道上都不写
缓存；`model` 留空、只给两个阶段各配模型时仍是 API 配置，P02 / P03 各问各的模型；两个批次在同一个
Runner 上交错进入、退出 P02 与 P03 时，每次调用的模型、推理强度与调用统计都只属于自己那个阶段。
合成密钥在运行时拼出，文件里不出现完整形状。
"""
from __future__ import annotations

import inspect
import json
import logging
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from ai_flywheel.adapters.runners import (
    P02_MAX_SESSIONS,
    P02_MIDDLE_OMITTED,
    P02_SESSION_HEAD_CHARS,
    P02_SESSION_TAIL_CHARS,
    P02_SESSION_TEXT_LIMIT,
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
    ModelCallError,
    OpenAICompatibleRunner,
    PlanDiagnostics,
    PLAN_SOURCE_MODEL,
    PlanOutcome,
    PlanUnavailableError,
    ReviewUnavailableError,
    _build_p02_prompt,
    build_runner,
    empty_domain_signals,
    excerpt_session_text,
    normalize_domain_signals,
)
from ai_flywheel.adapters.runners import clients as clients_mod
from ai_flywheel.adapters.runners import prompts as prompts_mod
from ai_flywheel.adapters.runners.clients import P02_STAGE, P03_STAGE
from ai_flywheel.adapters.runners.normalizers import REVIEW_EVENT_KINDS, normalize_review_events
from ai_flywheel.adapters.runners.prompts import P02_APPENDED_BRIEF_HEADER, P02_BRIEF_MARKER
from ai_flywheel.config import ApiRunnerConfig, FlywheelConfig
from ai_flywheel.core import orchestrator as orchestrator_mod
from ai_flywheel.core.queue_service import render_structured_brief
from ai_flywheel.models import NodeExecution, NodeStatus, RegisterSessionRequest, RunStatus, SessionRecord, SessionStatus, WorkflowRunRecord
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.fs_store import ImmutableFileStore, canonical_hash

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

    # Every body is 20000 characters: over the review's budget for one session, so each is excerpted.
    sessions = [(record, f"正文{i}" * 10000) for i, (record, _) in enumerate(_sessions("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"))]
    prompt = _build_p02_prompt(sessions, [], [{"name": "release-checklist", "description": "发版检查"}], "both")

    assert "FRIDAY WEEKLY REVIEW" in prompt
    for section in ('"findings"', '"mistakes"', '"dissents"', '"repeated_workflows"'):
        assert section in prompt
    payload = json.loads(prompt.rsplit("\n\n", 1)[1])
    assert set(payload) == {"phase", "sessions", "open_retros", "existing_skills"}
    assert payload["phase"] == "both"
    assert payload["existing_skills"] == [{"name": "release-checklist", "description": "发版检查"}]
    # the prompt carries the first sessions only, each as a head+tail excerpt inside the budget
    assert len(payload["sessions"]) == P02_MAX_SESSIONS == 6
    assert P02_SESSION_TEXT_LIMIT == 16000
    assert {len(item["text"]) for item in payload["sessions"]} == {
        P02_SESSION_HEAD_CHARS + len(P02_MIDDLE_OMITTED) + P02_SESSION_TAIL_CHARS
    }
    bodies = {
        record.session_key: f"正文{i}" * 10000
        for i, (record, _) in enumerate(_sessions("s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"))
    }
    for item in payload["sessions"]:
        body = bodies[item["session_key"]]
        assert item["text"] == body[:P02_SESSION_HEAD_CHARS] + P02_MIDDLE_OMITTED + body[-P02_SESSION_TAIL_CHARS :]
        assert len(item["text"]) <= P02_SESSION_TEXT_LIMIT


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

# ---------------------------------------------------------------- high-fidelity input, events and per-stage models
# (feat-agy-ingest-and-high-fidelity-distill)


def _dialogue(chars: int, *middle: str) -> str:
    """A dialogue of exactly ``chars`` characters, with ``middle`` laid at its centre."""
    filler = "用户：继续核对下一个字段。助手：好，接着看。\n"
    inner = "".join(middle)
    lead = (chars - len(inner)) // 2
    head = (filler * (chars // len(filler) + 2))[:lead]
    tail = (filler * (chars // len(filler) + 2))[: chars - lead - len(inner)]
    body = head + inner + tail
    assert len(body) == chars
    return body


def _sessions_of(prompt: str) -> list[dict[str, str]]:
    """The session entries of a P02 prompt's input."""
    return json.loads(prompt.rsplit("\n\n", 1)[1])["sessions"]


def _event(kind: str = "delivery", *, key: str = "codex:s1", title: str = "交付：对账任务", summary: str = "完成对账任务，金额以分为单位。", **extra: Any) -> dict[str, Any]:
    return {"event_id": "ev-9", "kind": kind, "title": title, "summary": summary, "session_key": key, **extra}


def _events_review(*events: dict[str, Any], session_id: str = "s1") -> str:
    """A model review with the given events and nothing else to persist."""
    return json.dumps(
        {"findings": [_finding(session_id)], "events": list(events), "mistakes": [], "dissents": [], "repeated_workflows": []},
        ensure_ascii=False,
    )


BRIEF = {
    "tables_verified": ["ledger_entries: entry_id 主键，amount 以分为单位"],
    "root_cause": "连接池上限过小，不是接口超时",
    "decisions": [{"what": "连接池上限调到 50", "why": "压测下请求排队超时"}],
}


def test_a_long_session_within_the_budget_is_carried_whole():
    """规格场景：12000 字的会话在 16000 字预算内完整进入审查，中段的表名与根因都还在。"""
    table = "助手：查了 ledger_entries 表，主键 entry_id，amount 以分为单位。\n"
    root_cause = "用户：所以根因是连接池上限配小了？助手：是，根因是连接池上限过小。\n"
    body = _dialogue(12000, table, root_cause)
    # Both sit in the middle, where the head + tail excerpt of the old 3000-character budget ended.
    assert body.index(table) > 1450
    assert body.index(root_cause) + len(root_cause) < 12000 - 1450
    record, _ = _sessions("s1")[0]

    (entry,) = _sessions_of(_build_p02_prompt([(record, body)], [], None, "daily"))

    assert entry["text"] == body
    assert P02_MIDDLE_OMITTED not in entry["text"]
    assert table.strip() in entry["text"] and root_cause.strip() in entry["text"]
    assert list(entry) == ["session_key", "client", "project", "title", "text"]


@pytest.mark.parametrize("limit", [P02_SESSION_TEXT_LIMIT, 3000, 2000, 64000])
def test_a_session_over_the_budget_keeps_its_head_and_tail_inside_the_budget(limit):
    body = _dialogue(limit + 4000)
    excerpt = excerpt_session_text(body, limit)
    whole = P02_SESSION_HEAD_CHARS + len(P02_MIDDLE_OMITTED) + P02_SESSION_TAIL_CHARS

    assert len(excerpt) == min(limit, whole)
    head, _, tail = excerpt.partition(P02_MIDDLE_OMITTED)
    assert head == body[: len(head)]
    assert tail == body[len(body) - len(tail) :]
    # A session exactly at the budget is carried whole; one character more is excerpted.
    assert excerpt_session_text("x" * limit, limit) == "x" * limit
    assert P02_MIDDLE_OMITTED in excerpt_session_text("x" * (limit + 1), limit)

    record, _ = _sessions("s1")[0]
    (entry,) = _sessions_of(_build_p02_prompt([(record, body)], [], None, "daily", session_char_limit=limit))
    assert entry["text"] == excerpt


def test_the_session_budget_is_a_configured_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert ApiRunnerConfig().p02_session_char_limit == P02_SESSION_TEXT_LIMIT == 16000
    for out_of_range in (1999, 64001):
        with pytest.raises(ValidationError):
            ApiRunnerConfig(p02_session_char_limit=out_of_range)

    runner = _api_runner(tmp_path)
    runner.cfg.runner.api.p02_session_char_limit = 3000
    seen: list[int | None] = []
    real = prompts_mod._build_p02_prompt

    def spy(sessions, open_retros, existing_skills, phase, *, session_char_limit=None):
        seen.append(session_char_limit)
        return real(sessions, open_retros, existing_skills, phase, session_char_limit=session_char_limit)

    monkeypatch.setattr(clients_mod, "_build_p02_prompt", spy)
    runner._call_chat_completion = _FakeModel(_review("s1"))
    runner.review_sessions(_sessions("s1"), [])

    cli = CliSubprocessRunner(runner.cfg, ImmutableFileStore(tmp_path / "cli-receipts"))
    cli.cfg.runner.cli.command = "mock-cli"
    cli._invoke_cli = lambda stage, prompt, **kwargs: json.loads(_review("s1"))  # type: ignore[method-assign]
    cli.review_sessions(_sessions("s1"), [])

    # Both runners hand the configured budget to the prompt; neither invents another.
    assert seen == [3000, 3000]


def test_the_structured_brief_comes_before_the_dialogue_and_is_never_excerpted():
    record, short = _sessions("s1")[0]
    briefed = record.model_copy(update={"metadata": {"structured_brief": BRIEF}})

    (entry,) = _sessions_of(_build_p02_prompt([(briefed, _dialogue(40000))], [], None, "daily"))

    assert list(entry) == ["session_key", "client", "project", "title", "structured_brief", "text"]
    assert entry["structured_brief"].startswith(P02_BRIEF_MARKER + "\n")
    assert json.loads(entry["structured_brief"].split("\n", 1)[1]) == BRIEF  # whole, in the client's key order
    # Only the dialogue is excerpted, exactly as it would be without the brief.
    assert entry["text"] == excerpt_session_text(_dialogue(40000))
    assert P02_MIDDLE_OMITTED in entry["text"]
    # A session without a brief gets the entry it always had.
    (plain,) = _sessions_of(_build_p02_prompt([(record, short)], [], None, "daily"))
    assert list(plain) == ["session_key", "client", "project", "title", "text"]
    assert plain["text"] == short


def test_a_brief_the_queue_appended_is_lifted_out_of_the_dialogue():
    record, _ = _sessions("s1")[0]
    dialogue = _dialogue(40000)
    appended = f"{dialogue}\n\n{render_structured_brief(BRIEF)}"

    entry = _sessions_of(_build_p02_prompt([(record, appended)], [], None, "daily"))[0]

    assert entry["structured_brief"] == f"{P02_BRIEF_MARKER}\n{render_structured_brief(BRIEF)}"
    # The dialogue alone is excerpted: the brief is neither cut nor counted against the budget.
    assert entry["text"] == excerpt_session_text(dialogue)
    assert P02_APPENDED_BRIEF_HEADER not in entry["text"]
    # A short dialogue comes back exactly as it was, without the trailing block.
    short = "用户：接口超时怎么排查。\n助手：先看连接池上限。\n"
    brief_block = render_structured_brief(BRIEF)
    short_entry = _sessions_of(_build_p02_prompt([(record, f"{short}\n\n{brief_block}")], [], None, "daily"))[0]
    assert short_entry["text"] == short.rstrip("\n")
    assert short_entry["structured_brief"] == f"{P02_BRIEF_MARKER}\n{brief_block}"


def test_a_brief_given_twice_is_carried_once_and_the_session_record_wins():
    dialogue = _dialogue(500)
    appended = f"{dialogue}\n\n{render_structured_brief({'from': 'queue'})}"
    record = _sessions("s1")[0][0].model_copy(update={"metadata": {"structured_brief": {"from": "record"}}})

    entry = _sessions_of(_build_p02_prompt([(record, appended)], [], None, "daily"))[0]

    assert entry["structured_brief"] == f"{P02_BRIEF_MARKER}\n" + '{\n  "from": "record"\n}'
    assert entry["text"] == dialogue
    assert entry["structured_brief"].count(P02_BRIEF_MARKER) == 1
    assert "queue" not in entry["structured_brief"] + entry["text"]


def test_only_a_usable_brief_is_carried_and_a_credential_in_it_is_masked():
    record, short = _sessions("s1")[0]

    def entry(brief: Any) -> dict[str, str]:
        briefed = record.model_copy(update={"metadata": {"structured_brief": brief}})
        return _sessions_of(_build_p02_prompt([(briefed, short)], [], None, "daily"))[0]

    for unusable in ({}, None, [], "", 0):
        assert "structured_brief" not in entry(unusable), unusable
    text_brief = "表：ledger_entries；根因：连接池上限过小"
    assert entry(text_brief)["structured_brief"] == f"{P02_BRIEF_MARKER}\n{text_brief}"

    # The queue redacts a brief when it is submitted; the prompt is the second net, on the same patterns:
    # an assignment inside a text, and the value of a key that names a credential.
    leaky = {
        "数据库密码": "Hk9-Qp31x",
        "note": "数据库密码：Hk9-Qp31x",
        "token": "abc123def456",
        "kept": "数据库密码：******",
        "also_fine": "密码走环境变量注入",
    }
    from_record = entry(leaky)["structured_brief"]
    from_queue = _sessions_of(_build_p02_prompt([(record, f"{short}\n\n{render_structured_brief(leaky)}")], [], None, "daily"))[0]["structured_brief"]
    for text in (from_record, from_queue):
        assert "Hk9-Qp31x" not in text and "abc123def456" not in text
        assert text.count("[REDACTED]") == 3, text
        assert "数据库密码：******" in text
        assert "密码走环境变量注入" in text


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param(
            f"{P02_APPENDED_BRIEF_HEADER}\n用户：这一行不是条目，说明上面那句只是对话里提到的一句话。\n",
            id="header-then-a-dialogue-line",
        ),
        pytest.param("flywheel-brief: structured brief submitted by the client\n  - key: value\n", id="another-header"),
        pytest.param(f"助手：客户端会提交 `{P02_APPENDED_BRIEF_HEADER}` 这样的一行。\n", id="header-inside-a-line"),
    ],
)
def test_a_dialogue_that_only_resembles_the_appended_brief_is_left_whole(tail):
    record, _ = _sessions("s1")[0]
    body = "用户：先看日志。\n" + tail

    entry = _sessions_of(_build_p02_prompt([(record, body)], [], None, "daily"))[0]

    assert "structured_brief" not in entry
    assert entry["text"] == body


def test_the_prompt_and_the_queue_name_the_appended_brief_alike():
    """The runners may not import the queue service, so the header is written in two places."""
    rendered = render_structured_brief(BRIEF)

    assert rendered.split("\n", 1)[0] == P02_APPENDED_BRIEF_HEADER
    assert all(line.startswith("  - ") for line in rendered.split("\n")[1:])
    assert prompts_mod.split_appended_brief(f"用户：先看日志。\n\n{rendered}")[1] == rendered


def _queue_dialogue(turns: int = 14) -> str:
    """A dialogue of many short turns, the way a transcript reader hands it to the queue.

    The queue folds a single long turn in the middle before it chunks, so a dialogue
    meant to span two chunks has to be made of turns it keeps verbatim.
    """
    turn = "[assistant] " + "核对记录。" * 389
    assert len(turn) == 1957
    return "\n\n".join([turn] * turns)


def test_the_queue_appends_the_brief_to_the_first_chunk_and_the_prompt_lifts_it(tmp_path: Path):
    rt, _ = _runtime(tmp_path)
    _register(rt)
    items = rt.queue.on_explicit_enqueue(client="codex", session_id="s1", content=_queue_dialogue(), structured_brief=BRIEF)

    assert [item.chunk_index for item in items] == [1, 2]  # above the 24000-character chunk limit
    texts = [rt.queue.load_queue_item_text(item) for item in items]
    brief_block = render_structured_brief(BRIEF)
    assert texts[0].endswith("\n\n" + brief_block)
    assert P02_APPENDED_BRIEF_HEADER not in texts[1]

    record = rt.db.get_session("codex:s1")
    first, second = (_sessions_of(_build_p02_prompt([(record, text)], [], None, "daily"))[0] for text in texts)

    assert first["structured_brief"] == f"{P02_BRIEF_MARKER}\n{brief_block}"
    # The dialogue alone is excerpted: the brief is neither cut nor counted against the budget.
    # (Excerpting the text as the queue stored it would have spent the tail on the brief.)
    dialogue = texts[0][: -len("\n\n" + brief_block)]
    assert len(dialogue) > P02_SESSION_TEXT_LIMIT
    assert first["text"] == excerpt_session_text(dialogue)
    assert P02_APPENDED_BRIEF_HEADER not in first["text"]
    # The later chunk carries dialogue only: the review reads the brief once, with the first part.
    assert "structured_brief" not in second
    assert second["text"] == texts[1]
    assert P02_APPENDED_BRIEF_HEADER not in second["text"]


def test_p02_prompt_states_the_structured_brief_and_asks_for_events():
    prompt = _build_p02_prompt(_sessions("s1"), [], None, "daily")

    for needle in (
        "STRUCTURED BRIEF:",
        '"structured_brief" ahead of its "text"',
        "It is evidence the client submitted, not dialogue",
        "quote the dialogue, never the brief, as something the session said",
        "EVENTS:",
        '"events" array',
        '"event_id" (ev-1, ev-2, ...)',
        '"kind" (delivery|incident|decision|domain_fact)',
        '"session_key" (copied verbatim)',
        "An incident is a collaboration accident",
        "a business delivery and the incident that happened on the way are two events",
        "Record every incident as its own event",
        "Leave the array empty ([]) when there is no such event.",
    ):
        assert needle in prompt, needle
    contract = prompt.split("Return STRICT JSON ONLY:\n", 1)[1].split("\n\n", 1)[0]
    assert (
        '"events":[{"event_id":"ev-1","kind":"delivery|incident|decision|domain_fact","title":"简洁中文标题",'
        '"summary":"...","session_key":"..."}]'
    ) in contract
    assert contract.index('"findings":[') < contract.index('"events":[') < contract.index('"mistakes":[')


def test_review_events_are_normalized_into_one_shape():
    raw = [
        _event(" Delivery "),  # the kind as a model may spell it
        _event("incident", title=" 事故：静默改错配置 ", note="not part of the contract"),
        _event("decision", title="决策：金额以分为单位", summary=""),
        _event("domain_fact", title="事实：ledger_entries 主键", summary="entry_id 是主键"),
        _event("delivery"),  # the first event again
        "not an object",
        42,
        None,
        _event("mistake", title="不认识的种类"),
        _event(key="   "),  # no session it belongs to
        _event("decision", title="  ", summary=""),  # nothing to say
    ]

    events = normalize_review_events(raw)

    said = "完成对账任务，金额以分为单位。"
    assert events == [
        {"event_id": "ev-1", "kind": "delivery", "title": "交付：对账任务", "summary": said, "session_key": "codex:s1"},
        {"event_id": "ev-2", "kind": "incident", "title": "事故：静默改错配置", "summary": said, "session_key": "codex:s1"},
        {"event_id": "ev-3", "kind": "decision", "title": "决策：金额以分为单位", "summary": "", "session_key": "codex:s1"},
        {"event_id": "ev-4", "kind": "domain_fact", "title": "事实：ledger_entries 主键", "summary": "entry_id 是主键", "session_key": "codex:s1"},
    ]
    assert list(events[0]) == ["event_id", "kind", "title", "summary", "session_key"]
    assert REVIEW_EVENT_KINDS == ("delivery", "incident", "decision", "domain_fact")
    # An id is a place in the list: the same reply always yields the same ids, and a second pass changes nothing.
    assert normalize_review_events(events) == events
    assert [event["event_id"] for event in normalize_review_events(list(reversed(events)))] == ["ev-1", "ev-2", "ev-3", "ev-4"]


@pytest.mark.parametrize("raw", [_OMITTED, None, {"kind": "delivery"}, "events", 7, [{"kind": "delivery"}]])
def test_anything_but_a_list_of_usable_events_reads_as_no_event(raw):
    review = {"findings": [_finding("s1")]}
    if raw is not _OMITTED:
        review["events"] = raw

    assert normalize_review_events(review.get("events")) == []


def test_review_events_are_bounded_masked_and_kept_in_order():
    many = normalize_review_events([_event("delivery", title=f"交付：任务{index}") for index in range(30)])

    assert [event["event_id"] for event in many] == [f"ev-{number}" for number in range(1, 25)]
    assert [event["title"] for event in many][:3] == ["交付：任务0", "交付：任务1", "交付：任务2"]

    (masked,) = normalize_review_events(
        [_event("incident", title="事故：" + "长" * 100, summary="数据库密码：Hk9-Qp31x。" + "经过" * 400, key="k" * 200)]
    )
    assert (len(masked["title"]), len(masked["summary"]), len(masked["session_key"])) == (80, 600, 120)
    assert "Hk9-Qp31x" not in masked["summary"]
    assert masked["summary"].startswith("数据库密码：[REDACTED]")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_OMITTED, id="omitted"),
        pytest.param(None, id="null"),
        pytest.param({"kind": "delivery"}, id="not-a-list"),
        pytest.param([{"kind": "delivery"}], id="no-usable-entry"),
        pytest.param(["x", 3], id="garbage-entries"),
    ],
)
def test_a_review_without_usable_events_reads_as_no_event(tmp_path: Path, raw: Any):
    review = json.loads(_review("s1"))
    if raw is not _OMITTED:
        review["events"] = raw
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _FakeModel(json.dumps(review, ensure_ascii=False))

    result = runner.review_sessions(_sessions("s1"), [])

    assert result["events"] == []
    assert result["review_diagnostics"]["reason_code"] is None
    assert [finding["session_key"] for finding in result["findings"]] == ["codex:s1"]


def test_review_events_pass_through_the_model_review(tmp_path: Path):
    runner = _api_runner(tmp_path)
    event = _event("incident", title="事故：静默改错配置", summary="改错了测试库配置，未告知。")
    runner._call_chat_completion = _FakeModel(_events_review(event))

    result = runner.review_sessions(_sessions("s1"), [])

    assert result["events"] == [{**event, "event_id": "ev-1"}]
    assert result["review_diagnostics"]["finding_count"] == 1
    # A review read from a cache written before the events existed states none, like a review without them.
    cached = _api_runner(tmp_path)
    second = cached.review_sessions(_sessions("s1"), [])
    assert second["review_diagnostics"]["cache_hit"] is True
    assert second["events"] == result["events"]


def test_every_reviewer_states_the_events(tmp_path: Path):
    # The local reviewer reads no transcript in detail: it records no event and says so.
    assert DryRunHeuristicRunner().review_sessions(_sessions(*SESSION_IDS), [])["events"] == []

    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "cli"
    cfg.runner.cli.command = "mock-cli"
    cli = CliSubprocessRunner(cfg, ImmutableFileStore(tmp_path / "cli-receipts"))
    cli._invoke_cli = lambda stage, prompt, **kwargs: json.loads(_events_review(_event("incident", title="事故：静默改错配置")))  # type: ignore[method-assign]

    (event,) = cli.review_sessions(_sessions("s1"), [])["events"]

    assert (event["kind"], event["title"], event["event_id"]) == ("incident", "事故：静默改错配置", "ev-1")


class _RecordingModel:
    """Stands in for the model gateway and keeps the whole prompt of every call."""

    P02_LINE = "You are the P02 Review Worker of AI Flywheel."
    P03_LINE = "You are the P03 Route & Write-Plan Worker of AI Flywheel."
    REPAIR_LINE = "The previous output was malformed JSON"

    def __init__(self, p02_reply: str, *, repair_reply: str = "", p03_reply: str = '{"planned_writes": []}') -> None:
        self.p02_reply = p02_reply
        self.repair_reply = repair_reply
        self.p03_reply = p03_reply
        self.prompts: list[str] = []

    def __call__(self, prompt: str, system_prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith(self.P02_LINE):
            return self.p02_reply
        if prompt.startswith(self.REPAIR_LINE):
            return self.repair_reply
        return self.p03_reply

    def prompts_of(self, first_line: str) -> list[str]:
        return [prompt for prompt in self.prompts if prompt.startswith(first_line)]


def test_the_events_of_a_review_reach_the_review_node_and_the_plan_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt, _ = _runtime(tmp_path)
    _register(rt)
    events = [
        _event("delivery", title="交付：对账任务"),
        _event("incident", title="事故：静默改错配置", summary="改错了测试库配置，未告知。"),
    ]
    model = _RecordingModel(_events_review(*events))
    monkeypatch.setattr(OpenAICompatibleRunner, "_call_chat_completion", model)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    expected = [{**event, "event_id": f"ev-{index}"} for index, event in enumerate(events, start=1)]
    p02 = _node(run, "P02_REVIEW")
    assert p02.status == NodeStatus.PASS
    assert p02.payload["events"] == expected
    assert next(check for check in p02.internal_checks if check["name"] == "重复流程与 Skill 线索扫描")["detail"]["events"] == 2
    (plan_prompt,) = model.prompts_of(_RecordingModel.P03_LINE)
    assert json.loads(plan_prompt.rsplit("\n\n", 1)[1])["events"] == expected
    assert "EVENTS:" in plan_prompt
    assert "事故：静默改错配置" in plan_prompt


def test_the_format_repair_is_shown_the_events_key(tmp_path: Path):
    runner = _api_runner(tmp_path)
    model = _RecordingModel('{"findings": [ BROKEN JSON', repair_reply=_events_review(_event("decision", title="决策：金额以分为单位")))
    runner._call_chat_completion = model

    result = runner.review_sessions(_sessions("s1"), [])

    (repair,) = model.prompts_of(_RecordingModel.REPAIR_LINE)
    assert repair.index('"findings":[...]') < repair.index('"events":[...]') < repair.index('"mistakes":[...]')
    assert [event["kind"] for event in result["events"]] == ["decision"]


# ---- per-stage model layering


class _Gateway:
    """Stands in for the model gateway: records every request body, answers with the next canned reply."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any = None, json: Any = None, timeout: Any = None) -> httpx.Response:
        self.bodies.append(json)
        content = self.replies[min(len(self.bodies) - 1, len(self.replies) - 1)]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"model": "served-model", "choices": [{"message": {"content": content}}]},
        )


def _staged_runner(tmp_path: Path, **stage: str) -> OpenAICompatibleRunner:
    """An API runner on `mock-model` at `medium` effort, with the per-stage keys given."""
    runner = _api_runner(tmp_path)
    runner.cfg.runner.api.reasoning_effort = "medium"
    for key, value in stage.items():
        setattr(runner.cfg.runner.api, key, value)
    return runner


def test_the_review_runs_on_its_own_model_and_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """规格场景：review_model / review_reasoning_effort 进 P02 请求体，缓存键也与默认模型不同。"""
    runner = _staged_runner(tmp_path, review_model="strong-review-model", review_reasoning_effort="high")
    gateway = _Gateway([_review("s1", "s2", "s3")])
    monkeypatch.setattr(httpx, "post", gateway)

    result = runner.review_sessions(_sessions("s1", "s2", "s3"), [])

    (request,) = gateway.bodies
    assert request["model"] == "strong-review-model"
    assert request["reasoning_effort"] == "high"
    assert result["runner"] == "api:strong-review-model"
    assert result["review_source"] == REVIEW_SOURCE_MODEL
    telemetry = result["review_diagnostics"]["call_telemetry"]
    assert (telemetry["model"], telemetry["reasoning_effort"], telemetry["served_model"]) == ("strong-review-model", "high", "served-model")

    # The review's cache is keyed by the pair it really ran on: another model's reply is never served to it.
    same_effort = _staged_runner(tmp_path)
    prompt = "同一个提示词"
    assert runner._cache_file(P02_STAGE, prompt).name != same_effort._cache_file(P02_STAGE, prompt).name
    # The review's keys say nothing about the plan stage.
    assert runner._cache_file(P03_STAGE, prompt).name == same_effort._cache_file(P03_STAGE, prompt).name

    plain_model = _FakeModel(_review("s1"))
    same_effort._call_chat_completion = plain_model
    same_effort.review_sessions(_sessions("s1"), [])  # the default model's reply lands in the cache
    staged_model = _FakeModel(_review("s1"))
    runner._call_chat_completion = staged_model

    asked = runner.review_sessions(_sessions("s1"), [])

    assert len(plain_model.prompts) == 1
    assert len(staged_model.prompts) == 1  # asked again: the other model's reply is not this one's
    assert asked["review_diagnostics"]["cache_hit"] is False
    assert runner.review_sessions(_sessions("s1"), [])["review_diagnostics"]["cache_hit"] is True


def test_without_a_stage_override_the_cache_key_is_what_it_always_was(tmp_path: Path):
    runner = _staged_runner(tmp_path)
    prompt = "同一个提示词"

    for stage in (P02_STAGE, P03_STAGE):
        expected = canonical_hash(
            {
                "stage": stage,
                "base_url": "http://127.0.0.1:9999/v1",
                "model": "mock-model",
                "reasoning_effort": "medium",
                "prompt": prompt,
            }
        )[:16]
        assert runner._cache_file(stage, prompt).name == f"{stage}-{expected}.json"
    # Only one of the pair overridden: the other half still comes from the configured pair.
    effort_only = _staged_runner(tmp_path, plan_reasoning_effort="high")
    assert effort_only._cache_file(P03_STAGE, prompt).name != runner._cache_file(P03_STAGE, prompt).name
    assert effort_only._cache_file(P02_STAGE, prompt).name == runner._cache_file(P02_STAGE, prompt).name


@pytest.mark.parametrize(
    ("stage", "keys", "expected"),
    [
        pytest.param(P02_STAGE, {}, ("mock-model", "medium"), id="review-unset"),
        pytest.param(P02_STAGE, {"review_model": "  "}, ("mock-model", "medium"), id="review-blank-model"),
        pytest.param(P02_STAGE, {"review_model": " r-model "}, ("r-model", "medium"), id="review-model-only"),
        pytest.param(P02_STAGE, {"review_reasoning_effort": " high "}, ("mock-model", "high"), id="review-effort-only"),
        pytest.param(P02_STAGE, {"plan_model": "p-model", "plan_reasoning_effort": "low"}, ("mock-model", "medium"), id="plan-keys-ignore"),
        pytest.param(P03_STAGE, {}, ("mock-model", "medium"), id="plan-unset"),
        pytest.param(P03_STAGE, {"plan_model": "p-model", "plan_reasoning_effort": "low"}, ("p-model", "low"), id="plan-both"),
        pytest.param(P03_STAGE, {"review_model": "r-model", "review_reasoning_effort": "high"}, ("mock-model", "medium"), id="review-keys-ignore"),
        pytest.param("p04_write", {"review_model": "r-model", "plan_model": "p-model"}, ("mock-model", "medium"), id="unknown-stage"),
    ],
)
def test_a_stage_falls_back_to_the_configured_pair_key_by_key(tmp_path: Path, stage: str, keys: dict[str, str], expected: tuple[str, str]):
    runner = _staged_runner(tmp_path, **keys)

    assert runner._resolve_stage_api_params(stage) == expected


def test_the_plan_runs_on_its_own_model_and_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = _staged_runner(tmp_path, plan_model="strong-plan-model", plan_reasoning_effort="low")
    gateway = _Gateway([json.dumps({"planned_writes": []}, ensure_ascii=False)])
    monkeypatch.setattr(httpx, "post", gateway)

    outcome = runner.plan_writes_detailed({"findings": []}, [], 8)

    (request,) = gateway.bodies
    assert (request["model"], request["reasoning_effort"]) == ("strong-plan-model", "low")
    assert outcome.diagnostics.model == "strong-plan-model"
    assert outcome.diagnostics.call_telemetry["model"] == "strong-plan-model"
    assert outcome.diagnostics.to_dict()["model"] == "strong-plan-model"


def test_an_effort_reaches_the_request_only_when_someone_set_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway = _Gateway([_review("s1")])
    monkeypatch.setattr(httpx, "post", gateway)
    bare = _api_runner(tmp_path)  # no effort configured anywhere
    bare._call_chat_completion("p", "s")
    assert "reasoning_effort" not in gateway.bodies[0]

    runner = _api_runner(tmp_path)
    runner.cfg.runner.api.review_reasoning_effort = "high"
    runner.review_sessions(_sessions("s1"), [])

    assert gateway.bodies[1]["reasoning_effort"] == "high"
    # Outside a stage the same runner asks with the configured pair.
    runner._call_chat_completion("p", "s")
    assert "reasoning_effort" not in gateway.bodies[2]
    assert gateway.bodies[2]["model"] == "mock-model"


def test_the_format_repair_asks_the_same_stage_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = _staged_runner(tmp_path, review_model="strong-review-model", review_reasoning_effort="high")
    gateway = _Gateway(['{"findings": [ BROKEN JSON', _review("s1")])
    monkeypatch.setattr(httpx, "post", gateway)

    result = runner.review_sessions(_sessions("s1"), [])

    assert [(request["model"], request["reasoning_effort"]) for request in gateway.bodies] == [("strong-review-model", "high")] * 2
    assert result["review_diagnostics"]["repair_attempted"] is True
    assert result["review_diagnostics"]["reason_code"] is None
    assert [finding["session_key"] for finding in result["findings"]] == ["codex:s1"]


def test_the_model_call_keeps_its_two_argument_signature_and_leaves_no_stage_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # A sandbox replay replaces this method with a stub that takes (prompt, system_prompt): the signature is a contract.
    assert list(inspect.signature(OpenAICompatibleRunner._call_chat_completion).parameters) == ["self", "prompt", "system_prompt"]

    runner = _staged_runner(tmp_path, review_model="strong-review-model", review_reasoning_effort="high")
    runner.cfg.runner.api.transport_retries = 0

    def refused(url: str, headers: Any = None, json: Any = None, timeout: Any = None) -> httpx.Response:
        raise httpx.ConnectError("refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", refused)
    with pytest.raises(ModelCallError) as refused_call:
        runner.review_sessions(_sessions("s1"), [])

    assert refused_call.value.telemetry["model"] == "strong-review-model"
    assert (runner._active_stage_model, runner._active_stage_effort) == ("", "")

    # No stage in flight: the call asks for the configured pair, as it always did.
    gateway = _Gateway(["ok"])
    monkeypatch.setattr(httpx, "post", gateway)
    runner._call_chat_completion("p", "s")
    assert (gateway.bodies[0]["model"], gateway.bodies[0]["reasoning_effort"]) == ("mock-model", "medium")
    # A stage in flight nests: the one before it is back afterwards.
    with runner._stage_call(P03_STAGE):
        runner._call_chat_completion("p", "s")
    assert gateway.bodies[1]["model"] == "mock-model"
    assert (runner._active_stage_model, runner._active_stage_effort) == ("", "")


def test_a_run_reviews_on_the_review_model_and_plans_on_the_plan_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt, _ = _runtime(tmp_path)
    rt.cfg.runner.api.reasoning_effort = "medium"
    rt.cfg.runner.api.review_model = "strong-review-model"
    rt.cfg.runner.api.review_reasoning_effort = "high"
    rt.cfg.runner.api.plan_model = "strong-plan-model"
    _register(rt)
    gateway = _Gateway([_review("s1"), '{"planned_writes": []}'])
    monkeypatch.setattr(httpx, "post", gateway)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    assert [(request["model"], request["reasoning_effort"]) for request in gateway.bodies] == [
        ("strong-review-model", "high"),
        ("strong-plan-model", "medium"),
    ]
    p02, p03 = _node(run, "P02_REVIEW"), _node(run, "P03_ROUTE_PLAN")
    assert p02.status == NodeStatus.PASS and p03.status == NodeStatus.PASS
    assert p02.payload["review_diagnostics"]["call_telemetry"]["model"] == "strong-review-model"
    assert p03.payload["plan_diagnostics"]["model"] == "strong-plan-model"
    assert p03.payload["plan_diagnostics"]["call_telemetry"]["model"] == "strong-plan-model"
    # The batch's runner mode names the configured runner; which model answered is on the nodes.
    assert run.runner_mode == "api:mock-model"
    assert rt.db.get_session("codex:s1").status == SessionStatus.PROCESSED


# ---------------------------------------------------------------- the cache and the privacy gate
# A review quotes the sessions it read. One that quotes a credential is still a usable review —
# and its reply stays off the disk, where the next identical batch would read it back verbatim.
# The synthetic credential is built here, so its shape never appears whole in this file.


SYNTHETIC_KEY = "sk-" + "9" * 32

REFUSED_TAIL = "): a credential shape is in the reply (OpenAI/Anthropic API Key). The next identical batch asks again."


def _review_quoting_a_credential() -> str:
    """A usable review: one finding, whose fact quotes a credential the session held."""
    finding = _finding("s1")
    finding["facts"] = [f"联调时把密钥贴进了对话：{SYNTHETIC_KEY}"]
    return json.dumps({"findings": [finding], "mistakes": [], "dissents": [], "repeated_workflows": []}, ensure_ascii=False)


def _files_under(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()) if root.is_dir() else []


def test_a_review_that_quotes_a_credential_is_usable_but_never_cached(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    runner = _api_runner(tmp_path)
    fake = _FakeModel(_review_quoting_a_credential())
    runner._call_chat_completion = fake

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        first = runner.review_sessions(_sessions("s1"), [])
        second = runner.review_sessions(_sessions("s1"), [])

    # The review itself is unaffected: the finding is there, with nothing wrong to report ...
    assert [finding["session_key"] for finding in first["findings"]] == ["codex:s1"]
    assert first["review_source"] == REVIEW_SOURCE_MODEL
    assert first["review_diagnostics"]["reason_code"] is None
    assert second["findings"] == first["findings"]
    # ... but its reply was never written, so the next identical batch asks the model again.
    assert len(fake.prompts) == 2
    for review in (first, second):
        assert (review["review_diagnostics"]["cache_hit"], review["review_diagnostics"]["cache_file"]) == (False, None)
    assert _files_under(tmp_path / "receipts") == []
    refusals = [record.getMessage() for record in caplog.records if record.getMessage().startswith("Reply not cached (p02_review-")]
    assert len(refusals) == 2
    assert all(message.endswith(REFUSED_TAIL) for message in refusals)
    # The log and the diagnostics name the kind of credential, never the credential.
    assert SYNTHETIC_KEY not in caplog.text
    assert SYNTHETIC_KEY not in json.dumps(first["review_diagnostics"], ensure_ascii=False)


def test_the_cli_channel_caches_no_such_review_either(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    base = tmp_path / "cli"
    base.mkdir()
    cfg = FlywheelConfig(base_dir=base)
    cfg.runner.mode = "cli"
    reply_file = base / "reply.json"
    reply_file.write_text(json.dumps(json.loads(_review_quoting_a_credential()), ensure_ascii=True), encoding="ascii")
    calls = base / "calls.log"
    script = (
        "import sys, pathlib; "
        "pathlib.Path(%r).open('a', encoding='ascii').write(sys.stdin.read()[:1]); "
        "sys.stdout.write(pathlib.Path(%r).read_text(encoding='ascii'))"
    ) % (str(calls), str(reply_file))
    cfg.runner.cli.command = sys.executable
    cfg.runner.cli.args = ["-c", script]
    cli = CliSubprocessRunner(cfg, ImmutableFileStore(base / "receipts"))

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        first = cli.review_sessions(_sessions("s1"), [])
        second = cli.review_sessions(_sessions("s1"), [])

    assert [finding["session_key"] for finding in first["findings"]] == ["codex:s1"]
    assert len(calls.read_text(encoding="ascii")) == 2  # the "CLI" really ran, once per batch
    for review in (first, second):
        assert (review["review_diagnostics"]["cache_hit"], review["review_diagnostics"]["cache_file"]) == (False, None)
    assert _files_under(base / "receipts") == []
    refusals = [record.getMessage() for record in caplog.records if record.getMessage().startswith("Reply not cached (p02_review-")]
    assert len(refusals) == 2
    assert all(message.endswith(REFUSED_TAIL) for message in refusals)
    assert SYNTHETIC_KEY not in caplog.text


def test_a_cached_review_that_carries_a_credential_is_discarded_not_served(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    runner = _api_runner(tmp_path)
    runner._call_chat_completion = _FakeModel(_review("s1"))
    clean = runner.review_sessions(_sessions("s1"), [])
    (cached,) = _api_cache_dir(tmp_path).glob("p02_review-*.json")
    clean_file = cached.read_text(encoding="utf-8")
    assert SYNTHETIC_KEY not in clean_file
    assert runner.review_sessions(_sessions("s1"), [])["review_diagnostics"]["cache_hit"] is True

    # A file from before the cache refused such replies.
    cached.write_text(_review_quoting_a_credential(), encoding="utf-8")
    asked = _FakeModel(_review("s1"))
    runner._call_chat_completion = asked

    with caplog.at_level(logging.WARNING, logger=RUN_LOGGER):
        result = runner.review_sessions(_sessions("s1"), [])

    assert len(asked.prompts) == 1  # asked again
    assert result["review_diagnostics"]["cache_hit"] is False
    assert result["findings"] == clean["findings"]
    assert cached.read_text(encoding="utf-8") == clean_file  # and the clean reply took its place
    assert [message for message in caplog.messages if message.startswith("Cached reply discarded (")] == [
        f"Cached reply discarded ({cached.name}): a credential shape is in it (OpenAI/Anthropic API Key). The run asks again."
    ]


# ---------------------------------------------------------------- a model for each stage, and no common one
# `model` may be left empty when each of the two stages names its own model. Such a
# configuration is an API configuration: it selects the API runner, and each stage asks its
# own model. It used to select the dry-run planner without a word.


def _channel(**keys: str) -> FlywheelConfig:
    """An API configuration with an endpoint and no model, plus the `runner.api` keys given."""
    cfg = FlywheelConfig()
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    for key, value in keys.items():
        setattr(cfg.runner.api, key, value)
    return cfg


def test_a_model_for_each_stage_and_no_common_one_selects_the_api_runner(tmp_path: Path):
    cfg = _channel(model="", review_model=" r-model ", plan_model="p-model")
    receipts = ImmutableFileStore(tmp_path / "receipts")

    assert clients_mod.api_model_label(cfg.runner.api) == "r-model/p-model"
    assert clients_mod.resolve_runner_mode(cfg) == "api:r-model/p-model"
    assert isinstance(clients_mod.build_runner(cfg, receipts), OpenAICompatibleRunner)
    runner = OpenAICompatibleRunner(cfg, receipts)
    assert [runner._resolve_stage_api_params(stage)[0] for stage in (P02_STAGE, P03_STAGE)] == ["r-model", "p-model"]


@pytest.mark.parametrize(
    ("model", "keys", "label", "runner"),
    [
        pytest.param("", {"review_model": "r-model"}, "", DryRunHeuristicRunner, id="review-model-alone"),
        pytest.param("", {"plan_model": "p-model"}, "", DryRunHeuristicRunner, id="plan-model-alone"),
        pytest.param("", {"review_model": "r-model", "plan_model": "  "}, "", DryRunHeuristicRunner, id="plan-model-blank"),
        pytest.param("", {}, "", DryRunHeuristicRunner, id="nothing-configured"),
        pytest.param("common-model", {"review_model": "r-model", "plan_model": "p-model"}, "common-model", OpenAICompatibleRunner, id="the-common-model-is-the-label"),
    ],
)
def test_the_api_channel_is_ready_only_when_every_stage_has_a_model(tmp_path: Path, model: str, keys: dict[str, str], label: str, runner: type):
    cfg = _channel(model=model, **keys)

    assert clients_mod.api_model_label(cfg.runner.api) == label
    assert clients_mod.resolve_runner_mode(cfg) == (f"api:{label}" if label else "dry_run")
    assert type(clients_mod.build_runner(cfg, ImmutableFileStore(tmp_path / "receipts"))) is runner


@pytest.mark.parametrize("blocked", ["the-valve", "the-endpoint"])
def test_two_stage_models_do_not_get_past_the_valve_or_a_missing_endpoint(tmp_path: Path, blocked: str):
    cfg = _channel(review_model="r-model", plan_model="p-model")
    if blocked == "the-valve":
        cfg.safety_valves.model_execution_enabled = False
    else:
        cfg.runner.api.base_url = ""

    assert clients_mod.resolve_runner_mode(cfg) == "dry_run"
    assert isinstance(clients_mod.build_runner(cfg, ImmutableFileStore(tmp_path / "receipts")), DryRunHeuristicRunner)


def test_a_stage_without_a_model_fails_instead_of_asking_with_a_blank_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Only the review names a model. The plan fails and names the cause: no request goes out with a blank model name, and no dry-run plan takes its place."""
    runner = _staged_runner(tmp_path, model="", review_model="strong-review-model")
    gateway = _Gateway([_review("s1")])
    monkeypatch.setattr(httpx, "post", gateway)

    with pytest.raises(PlanUnavailableError) as refused:
        runner.plan_writes_detailed({"findings": []}, [], 8)

    assert "缺少 planned_writes 列表" in str(refused.value)
    assert "本批不写入" in str(refused.value)
    assert refused.value.diagnostics.source == PLAN_SOURCE_MODEL
    assert gateway.bodies == []

    # The review has its model: it runs on it.
    result = runner.review_sessions(_sessions("s1"), [])
    assert [(request["model"], request["reasoning_effort"]) for request in gateway.bodies] == [("strong-review-model", "medium")]
    assert [finding["session_key"] for finding in result["findings"]] == ["codex:s1"]


def test_a_run_on_a_model_for_each_stage_and_no_common_one_is_an_api_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heuristic_reviewer_forbidden: None
):
    rt, _ = _runtime(tmp_path)
    rt.cfg.runner.api.model = ""
    rt.cfg.runner.api.reasoning_effort = "medium"
    rt.cfg.runner.api.review_model = "strong-review-model"
    rt.cfg.runner.api.plan_model = "strong-plan-model"
    _register(rt)
    gateway = _Gateway([_review("s1"), '{"planned_writes": []}'])
    monkeypatch.setattr(httpx, "post", gateway)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s1"], auto_approve_override=True)

    assert run.status == RunStatus.PASS, run.error
    assert run.runner_mode == "api:strong-review-model/strong-plan-model"
    assert [(request["model"], request["reasoning_effort"]) for request in gateway.bodies] == [
        ("strong-review-model", "medium"),
        ("strong-plan-model", "medium"),
    ]
    p02, p03 = _node(run, "P02_REVIEW"), _node(run, "P03_ROUTE_PLAN")
    assert p02.status == NodeStatus.PASS and p03.status == NodeStatus.PASS
    assert p02.payload["review_source"] == REVIEW_SOURCE_MODEL
    assert p02.payload["review_diagnostics"]["call_telemetry"]["model"] == "strong-review-model"
    assert p03.payload["plan_source"] == PLAN_SOURCE_MODEL
    assert p03.payload["plan_diagnostics"]["call_telemetry"]["model"] == "strong-plan-model"
    assert rt.db.get_session("codex:s1").status == SessionStatus.PROCESSED


# ---------------------------------------------------------------- one runner, two batches in flight
# One runner serves more than one batch. A second batch is reviewed while the first is being
# planned, on the same object from another thread: every call asks for the model and the effort
# of its own stage, every batch keeps the telemetry of its own call, and neither batch leaves
# its stage behind on the runner.


WAIT = 10.0


class _ScriptedGateway:
    """Stands in for the model gateway and answers by the prompt's first line.

    A review gets a review of the session it asks about; the plan gets malformed JSON first and
    an empty plan after the format repair. One call — `hold_call`, by ordinal — is held inside
    the gateway until the test releases it, so another call can complete while it waits.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []  # (the calling thread's name, the request body)
        self.hold_call: int | None = None
        self.held = threading.Event()
        self.release = threading.Event()

    def __call__(self, url: str, headers: Any = None, json: Any = None, timeout: Any = None) -> httpx.Response:
        prompt = json["messages"][1]["content"]
        ordinal = len(self.calls)
        self.calls.append((threading.current_thread().name, json))
        if ordinal == self.hold_call:
            self.held.set()
            assert self.release.wait(WAIT), "the test never released the held call"
        if prompt.startswith(_RecordingModel.P02_LINE):
            content = _review("s2" if "codex:s2" in prompt else "s1")
        elif prompt.startswith(_RecordingModel.REPAIR_LINE):
            content = '{"planned_writes": []}'
        else:
            content = '{"planned_writes": [ BROKEN JSON'
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"model": "served-model", "choices": [{"message": {"content": content}}]},
        )


def _batch(name: str, work: Any, failures: list[BaseException]) -> threading.Thread:
    """A thread that makes one batch's model calls; what it raises is kept for the test to report."""

    def run() -> None:
        try:
            work()
        except BaseException as exc:
            failures.append(exc)

    return threading.Thread(target=run, name=name, daemon=True)


def _reached(event: threading.Event, failures: list[BaseException]) -> bool:
    """Whether a step was reached in time; a batch that already died fails the wait at once."""
    return not failures and event.wait(WAIT)


def _model_and_effort(call: dict[str, Any]) -> tuple[str, str]:
    return call["model"], call["reasoning_effort"]


def test_two_batches_in_flight_on_one_runner_each_keep_their_own_stage_and_telemetry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = _staged_runner(
        tmp_path,
        review_model="strong-review-model",
        review_reasoning_effort="high",
        plan_model="strong-plan-model",
        plan_reasoning_effort="low",
    )
    runner.cfg.runner.api.transport_retries = 0
    gateway = _ScriptedGateway()
    monkeypatch.setattr(httpx, "post", gateway)

    # Every call stops before it is made until the test opens its gate. `seen` is the stage the
    # calling thread finds itself in on arrival: the other batch's stage must not be in it.
    arrived = [threading.Event() for _ in range(4)]
    gates = [threading.Event() for _ in range(4)]
    seen: list[tuple[str, str, str]] = []
    real_call = runner._call_chat_completion

    def gated(prompt: str, system_prompt: str) -> str:
        step = len(seen)
        seen.append((threading.current_thread().name, runner._active_stage_model, runner._active_stage_effort))
        arrived[step].set()
        assert gates[step].wait(WAIT), "the test never opened this call's gate"
        return real_call(prompt, system_prompt)

    runner._call_chat_completion = gated

    first_review_returned, repair_in_flight, second_review_returned = threading.Event(), threading.Event(), threading.Event()
    telemetry: dict[str, tuple[str, str]] = {}
    failures: list[BaseException] = []

    def review_two_batches() -> None:
        first = runner.review_sessions(_sessions("s1"), [])
        telemetry["review-1"] = _model_and_effort(first["review_diagnostics"]["call_telemetry"])
        first_review_returned.set()
        assert repair_in_flight.wait(WAIT), "the format repair never became in flight"
        second = runner.review_sessions(_sessions("s2"), [])
        telemetry["review-2"] = _model_and_effort(second["review_diagnostics"]["call_telemetry"])
        second_review_returned.set()

    def plan_a_batch() -> None:
        outcome = runner.plan_writes_detailed({"findings": []}, [], 8)
        telemetry["plan"] = _model_and_effort(outcome.diagnostics.call_telemetry)

    review_batch = _batch("review-batch", review_two_batches, failures)
    plan_batch = _batch("plan-batch", plan_a_batch, failures)

    try:
        review_batch.start()
        assert _reached(arrived[0], failures), failures  # a review is in flight
        plan_batch.start()
        assert _reached(arrived[1], failures), failures  # a plan is in flight too, on the same runner

        gateway.hold_call = 0
        gates[1].set()  # the plan's call reaches the gateway and is held there
        assert gateway.held.wait(WAIT)
        gates[0].set()  # the review's call goes through meanwhile
        assert _reached(first_review_returned, failures), failures
        gateway.release.set()
        assert _reached(arrived[2], failures), failures  # malformed: the plan asks for the format repair

        repair_in_flight.set()
        assert _reached(arrived[3], failures), failures  # a second batch is reviewed while the repair waits
        gates[3].set()
        assert _reached(second_review_returned, failures), failures
        review_batch.join(WAIT)
        gates[2].set()  # the repair goes through once the review batch has left both its stages
        plan_batch.join(WAIT)
    finally:
        # A failed step must not leave the two batches parked on their gates.
        for gate in gates:
            gate.set()
        gateway.release.set()
        review_batch.join(WAIT)
        plan_batch.join(WAIT)

    assert failures == [], failures
    assert not review_batch.is_alive() and not plan_batch.is_alive()
    assert [(thread, *_model_and_effort(request)) for thread, request in gateway.calls] == [
        ("plan-batch", "strong-plan-model", "low"),
        ("review-batch", "strong-review-model", "high"),
        ("review-batch", "strong-review-model", "high"),
        ("plan-batch", "strong-plan-model", "low"),
    ]
    assert seen == [
        ("review-batch", "strong-review-model", "high"),
        ("plan-batch", "strong-plan-model", "low"),
        ("plan-batch", "strong-plan-model", "low"),
        ("review-batch", "strong-review-model", "high"),
    ]
    # The review's call completed while the plan's was still held: each batch read its own call.
    assert telemetry == {
        "review-1": ("strong-review-model", "high"),
        "review-2": ("strong-review-model", "high"),
        "plan": ("strong-plan-model", "low"),
    }
    # Neither batch left its stage, or its last call, behind on the runner.
    assert (runner._active_stage_model, runner._active_stage_effort) == ("", "")
    assert runner._last_call_telemetry == {}
    gateway.hold_call = None
    real_call("p", "s")  # from this thread, with no stage in flight: the configured pair
    assert [(thread, *_model_and_effort(request)) for thread, request in gateway.calls][-1] == ("MainThread", "mock-model", "medium")
