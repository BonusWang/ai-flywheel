"""模型调用可靠性（fix-model-call-timeout-margin）。

超时与连接错误重试（不退避）、HTTP 错误不重试、推理强度可配置且进缓存键、每次调用的用量与
耗时进节点载荷；传输失败时留下的只有阶段、阈值、尝试次数与异常类型，没有网关地址。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_flywheel.adapters import runners
from ai_flywheel.adapters.runners import OpenAICompatibleRunner
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import RegisterSessionRequest, RunStatus
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.fs_store import ImmutableFileStore

REPO_ROOT = Path(__file__).resolve().parents[1]

GATEWAY_URL = "http://private-gateway.invalid/v1/chat/completions"


def _reply(text: str, *, usage: dict[str, Any] | None = None, served_model: str = "served-model") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": served_model,
        "choices": [{"message": {"content": text}}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", GATEWAY_URL)
            raise httpx.HTTPStatusError(
                "server error",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _Gateway:
    """Stands in for the model gateway: every call records its request and returns the next outcome."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any = None, json: Any = None, timeout: Any = None) -> _FakeResponse:
        self.calls.append({"url": url, "headers": headers, "body": json, "timeout": timeout})
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, _FakeResponse):
            return outcome
        return _FakeResponse(outcome)


def _timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("timed out", request=httpx.Request("POST", GATEWAY_URL))


def _runner(tmp_path: Path, **api_cfg: Any) -> OpenAICompatibleRunner:
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    for key, value in api_cfg.items():
        setattr(cfg.runner.api, key, value)
    return OpenAICompatibleRunner(cfg, ImmutableFileStore(tmp_path / "receipts"))


# ---------------------------------------------------------------- request body


def test_reasoning_effort_is_omitted_when_empty_and_sent_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway = _Gateway([_reply("ok")])
    monkeypatch.setattr(runners.httpx, "post", gateway)

    runner = _runner(tmp_path)
    runner._call_chat_completion("p", "s")
    assert "reasoning_effort" not in gateway.calls[0]["body"]

    gateway = _Gateway([_reply("ok")])
    monkeypatch.setattr(runners.httpx, "post", gateway)
    runner = _runner(tmp_path, reasoning_effort="low")
    runner._call_chat_completion("p", "s")
    assert gateway.calls[0]["body"]["reasoning_effort"] == "low"


def test_reasoning_effort_changes_the_cache_key(tmp_path: Path):
    plain = _runner(tmp_path)
    low = _runner(tmp_path, reasoning_effort="low")

    assert plain._cache_file("p03_plan", "同一个提示词").name != low._cache_file("p03_plan", "同一个提示词").name
    assert plain._cache_file("p03_plan", "同一个提示词").name == plain._cache_file("p03_plan", "同一个提示词").name


# ---------------------------------------------------------------- retries


def test_a_timeout_is_retried_once_and_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway = _Gateway([_timeout(), _reply("第二次成功")])
    monkeypatch.setattr(runners.httpx, "post", gateway)
    runner = _runner(tmp_path, transport_retries=1)

    assert runner._call_chat_completion("p", "s") == "第二次成功"
    assert len(gateway.calls) == 2


def test_the_number_of_retries_follows_the_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway = _Gateway([_timeout()])
    monkeypatch.setattr(runners.httpx, "post", gateway)
    runner = _runner(tmp_path, transport_retries=0)

    with pytest.raises(httpx.ReadTimeout):
        runner._call_chat_completion("p", "s")
    assert len(gateway.calls) == 1

    gateway = _Gateway([_timeout()])
    monkeypatch.setattr(runners.httpx, "post", gateway)
    runner = _runner(tmp_path, transport_retries=2)

    with pytest.raises(httpx.ReadTimeout):
        runner._call_chat_completion("p", "s")
    assert len(gateway.calls) == 3


def test_a_connection_error_is_retried_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway = _Gateway([httpx.ConnectError("connection refused"), _reply("连上了")])
    monkeypatch.setattr(runners.httpx, "post", gateway)
    runner = _runner(tmp_path, transport_retries=1)

    assert runner._call_chat_completion("p", "s") == "连上了"
    assert len(gateway.calls) == 2


def test_an_http_error_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway = _Gateway([_FakeResponse({}, status_code=502)])
    monkeypatch.setattr(runners.httpx, "post", gateway)
    runner = _runner(tmp_path, transport_retries=3)

    with pytest.raises(RuntimeError) as excinfo:
        runner._call_chat_completion("p", "s")
    assert str(excinfo.value) == "Model API returned HTTP 502"
    assert len(gateway.calls) == 1


# ---------------------------------------------------------------- telemetry


def test_the_call_telemetry_records_attempts_duration_model_and_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway = _Gateway(
        [
            _reply(
                "ok",
                usage={
                    "prompt_tokens": 120,
                    "completion_tokens": 45,
                    "completion_tokens_details": {"reasoning_tokens": 30},
                },
            )
        ]
    )
    monkeypatch.setattr(runners.httpx, "post", gateway)
    runner = _runner(tmp_path)

    runner._call_chat_completion("p", "s")

    telemetry = runner._last_call_telemetry
    assert telemetry["attempts"] == 1
    assert telemetry["duration_ms"] >= 0
    assert telemetry["served_model"] == "served-model"
    assert telemetry["prompt_tokens"] == 120
    assert telemetry["completion_tokens"] == 45
    assert telemetry["reasoning_tokens"] == 30


def test_reasoning_tokens_are_read_from_the_flat_field_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway = _Gateway([_reply("ok", usage={"prompt_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 7})])
    monkeypatch.setattr(runners.httpx, "post", gateway)
    runner = _runner(tmp_path)

    runner._call_chat_completion("p", "s")
    assert runner._last_call_telemetry["reasoning_tokens"] == 7


def test_a_reply_without_usage_is_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway = _Gateway([_reply("ok")])
    monkeypatch.setattr(runners.httpx, "post", gateway)
    runner = _runner(tmp_path)

    assert runner._call_chat_completion("p", "s") == "ok"
    telemetry = runner._last_call_telemetry
    assert telemetry["prompt_tokens"] is None
    assert telemetry["completion_tokens"] is None
    assert telemetry["reasoning_tokens"] is None
    assert telemetry["attempts"] == 1


def test_a_stubbed_call_leaves_no_telemetry_and_does_not_break_the_runner(tmp_path: Path):
    runner = _runner(tmp_path)
    runner._call_chat_completion = lambda prompt, system_prompt: '{"planned_writes": []}'  # type: ignore[method-assign]

    outcome = runner.plan_writes_detailed({"findings": [], "repeated_workflows": []}, [], 8)

    assert outcome.planned_writes == []
    assert outcome.diagnostics.call_telemetry is None


# ---------------------------------------------------------------- the node payloads


def _runtime(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills_dir)]
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    cfg.workflow.approval_mode = "auto"
    # `auto` only takes effect with the safety valve open: the valve has
    # the last word, so a closed valve would run the batch as `smart`.
    cfg.safety_valves.auto_approve_writes = True
    return FlywheelRuntime(cfg)


_P02_REVIEW = json.dumps(
    {
        "findings": [
            {
                "session_key": "codex:s-1",
                "client": "codex",
                "project": "demo",
                "title": "接口超时排查",
                "worthy": True,
                "category": "project",
                "skip_reason": "",
                "facts": ["连接池上限过小"],
                "anomalies": [],
                "tags": ["demo"],
                "suggest_rule": False,
                "skill_route": {"hit": False, "action": "none", "target_skill": "", "reason": "单次任务"},
            }
        ],
        "mistakes": [],
        "dissents": [],
        "repeated_workflows": [],
    },
    ensure_ascii=False,
)

_P03_PLAN = json.dumps({"planned_writes": []}, ensure_ascii=False)


class _RoutingGateway:
    """Answers the P02 review and the P03 plan differently, like the real stages do."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: Any = None, json: Any = None, timeout: Any = None) -> _FakeResponse:
        self.calls.append({"url": url, "body": json})
        prompt = (json or {}).get("messages", [{}])[-1].get("content", "")
        text = _P02_REVIEW if prompt.startswith("You are the P02 Review Worker") else _P03_PLAN
        return _FakeResponse(
            _reply(
                text,
                usage={"prompt_tokens": 100, "completion_tokens": 50, "completion_tokens_details": {"reasoning_tokens": 20}},
            )
        )


def _register(rt: FlywheelRuntime) -> None:
    rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id="s-1",
            title="接口超时排查",
            project="demo",
            summary="记录接口超时的根因",
            content="用户：接口超时怎么排查。助手：先看连接池上限。",
        )
    )


def test_the_p02_and_p03_payloads_carry_the_call_telemetry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _runtime(tmp_path)
    _register(rt)
    gateway = _RoutingGateway()
    monkeypatch.setattr(runners.httpx, "post", gateway)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s-1"])

    assert run.status == RunStatus.PASS, run.error
    p02 = next(n for n in run.nodes if n.node_id == "P02_REVIEW")
    p03 = next(n for n in run.nodes if n.node_id == "P03_ROUTE_PLAN")
    assert p02.payload["call_telemetry"]["served_model"] == "served-model"
    assert p02.payload["call_telemetry"]["prompt_tokens"] == 100
    assert p02.payload["call_telemetry"]["reasoning_tokens"] == 20
    assert p02.payload["review_diagnostics"]["call_telemetry"]["completion_tokens"] == 50
    assert p03.payload["call_telemetry"]["attempts"] == 1
    assert p03.payload["plan_diagnostics"]["call_telemetry"]["served_model"] == "served-model"


def test_a_transport_failure_leaves_a_sanitized_summary_and_no_gateway_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rt = _runtime(tmp_path)
    _register(rt)
    gateway = _Gateway([_timeout()])
    monkeypatch.setattr(runners.httpx, "post", gateway)

    run = rt.orchestrator.start_run(trigger="test", phase="daily", session_keys=["codex:s-1"])

    assert run.status == RunStatus.FAILED
    p02 = next(n for n in run.nodes if n.node_id == "P02_REVIEW")
    assert p02.status.value == "FAILED"
    summary = p02.payload["call_error"]
    assert summary["stage"] == "p02_review"
    assert summary["timeout_seconds"] == 240
    assert summary["attempts"] == 2  # 一次调用 + 一次重试
    assert summary["error_type"] == "ReadTimeout"
    assert "private-gateway" not in json.dumps(p02.payload, ensure_ascii=False)
    assert "private-gateway" not in (p02.error or "")
    assert "private-gateway" not in (run.error or "")
    assert "ReadTimeout" in (p02.error or "")
