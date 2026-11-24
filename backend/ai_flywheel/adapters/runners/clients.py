"""The real runners: a local CLI subprocess and an OpenAI-compatible HTTP API.

Both turn an untrusted reply into a plan or a review through the same contract
(`outcomes.py`): an unusable reply fails the node, it never falls back to the local
heuristic planner. `resolve_runner_mode` is the single source of truth for which runner
a configuration selects, and `build_runner` follows it.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from ai_flywheel.adapters.runners.contracts import (
    P02_MAX_SESSIONS,
    P03_REPAIR_SCHEMA_HINT,
    P03_REPAIR_SCHEMA_HINT_WITH_ROUTE,
    PLAN_SOURCE_CLI,
    PLAN_SOURCE_MODEL,
    REVIEW_SOURCE_CLI,
    REVIEW_SOURCE_MODEL,
    BaseWorkflowRunner,
    ModelCallError,
    PlanOutcome,
    _as_int,
    _extract_json_block,
    _json_parse_error,
)
from ai_flywheel.adapters.runners.heuristic import DryRunHeuristicRunner
from ai_flywheel.adapters.runners.outcomes import _plan_from_runner_output, _review_from_runner_output
from ai_flywheel.adapters.runners.prompts import (
    _build_p02_prompt,
    _build_p03_prompt,
    _existing_skill_sections,
)
from ai_flywheel.adapters.wiki.router import RoutingCandidates
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import PlannedWrite, RetroItem, RuleRecord, SessionRecord
from ai_flywheel.storage.fs_store import ImmutableFileStore, canonical_hash


class CliSubprocessRunner(BaseWorkflowRunner):
    """Configurable local CLI subprocess runner (e.g. codex / claude CLI) with receipt caching."""

    def __init__(self, cfg: FlywheelConfig, fs_store: ImmutableFileStore) -> None:
        self.cfg = cfg
        self.fs_store = fs_store

    def _cache_file(self, stage: str, prompt: str) -> Path:
        prompt_hash = canonical_hash({"stage": stage, "cmd": self.cfg.runner.cli.command, "prompt": prompt})
        return self.fs_store.root / "cli_cache" / f"{stage}-{prompt_hash[:16]}.json"

    @staticmethod
    def _persist_cache(cache_file: Path, parsed: Any) -> None:
        """Write a validated reply to the cache. Only called once the reply passed validation."""
        if not isinstance(parsed, dict):
            return
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")

    def _invoke_cli(
        self, stage: str, prompt: str, reply_trace: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Run the local CLI on a prompt.

        `reply_trace`, when given, receives the reply's length and, when the output is not a JSON object, why.
        The reply is NOT cached here: caching happens after the reply passed validation, so an
        unusable output never poisons the next run.
        """
        cli_cfg = self.cfg.runner.cli
        if not self.cfg.safety_valves.effective_model_execution_enabled or not cli_cfg.command:
            return None

        cache_file = self._cache_file(stage, prompt)
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        cmd = [cli_cfg.command]
        used_placeholder = False
        for arg in cli_cfg.args:
            if "{prompt}" in arg:
                cmd.append(arg.replace("{prompt}", prompt))
                used_placeholder = True
            else:
                cmd.append(arg)

        proc = subprocess.run(
            cmd,
            input=None if used_placeholder else prompt,
            text=True,
            capture_output=True,
            timeout=cli_cfg.timeout_seconds,
            cwd=cli_cfg.working_dir or str(self.cfg.base_dir),
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Local CLI '{cli_cfg.command}' exited with code {proc.returncode}: {(proc.stderr or proc.stdout)[:400]}"
            )
        if reply_trace is not None:
            reply_trace["output_chars"] = len(proc.stdout)
        parsed = _extract_json_block(proc.stdout)
        if parsed is None and reply_trace is not None and proc.stdout.strip():
            # Why the output is not a JSON object. Never the output itself.
            reply_trace["parse_error"] = _json_parse_error(proc.stdout)
        return parsed

    def review_sessions(
        self,
        sessions: list[tuple[SessionRecord, str]],
        open_retros: list[RetroItem],
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> dict[str, Any]:
        """P02 from the CLI output only: an unusable reply fails the node, it never falls back."""
        prompt = _build_p02_prompt(sessions, open_retros, existing_skills, phase)
        cache_file = self._cache_file("p02_review", prompt)
        cache_hit = cache_file.exists()
        reply_trace: dict[str, Any] = {}
        parsed = self._invoke_cli("p02_review", prompt, reply_trace=reply_trace)
        review = _review_from_runner_output(
            parsed,
            source=REVIEW_SOURCE_CLI,
            phase=phase,
            sessions_sent=min(len(sessions), P02_MAX_SESSIONS),
            output_chars=reply_trace.get("output_chars"),
            repair_attempted=False,  # only the API runner repairs JSON
            parse_error=reply_trace.get("parse_error"),
            cache_file=cache_file.name,
            cache_hit=cache_hit,
            cache_file_present=cache_hit,
        )
        # The review is usable: now the reply may be cached for the next identical batch.
        self._persist_cache(cache_file, parsed)
        review["runner"] = f"cli:{self.cfg.runner.cli.command}"
        review["sent_session_keys"] = [s.session_key for s, _ in sessions[:P02_MAX_SESSIONS]]
        return review

    def plan_writes_detailed(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
        routing_candidates: RoutingCandidates | None = None,
    ) -> PlanOutcome:
        prompt = _build_p03_prompt(
            review_findings,
            existing_rules,
            existing_skills,
            max_write_set,
            default_doc_prefix,
            phase,
            routing_candidates=routing_candidates,
            existing_skill_sections=_existing_skill_sections(review_findings, self.cfg.primary_skills_dir),
        )
        cache_file = self._cache_file("p03_plan", prompt)
        cache_hit = cache_file.exists()
        reply_trace: dict[str, Any] = {}
        parsed = self._invoke_cli("p03_plan", prompt, reply_trace=reply_trace)
        outcome = _plan_from_runner_output(
            parsed,
            source=PLAN_SOURCE_CLI,
            max_write_set=max_write_set,
            default_doc_prefix=default_doc_prefix,
            skills_root=self.cfg.primary_skills_dir,
            cache_file=cache_file.name,
            cache_hit=cache_hit,
            cache_file_present=cache_hit,
            reply_trace=reply_trace,
        )
        # The plan is usable: now the reply may be cached for the next identical batch.
        self._persist_cache(cache_file, parsed)
        return outcome

    def plan_writes(self, *args: Any, **kwargs: Any) -> list[PlannedWrite]:
        """The plan alone; same contract and failures as `plan_writes_detailed`."""
        return self.plan_writes_detailed(*args, **kwargs).planned_writes


class OpenAICompatibleRunner(BaseWorkflowRunner):
    """OpenAI-compatible HTTP API runner (works with local routing gateways, OpenAI, DeepSeek, Ollama)."""

    def __init__(self, cfg: FlywheelConfig, fs_store: ImmutableFileStore) -> None:
        self.cfg = cfg
        self.fs_store = fs_store
        # What the last model call cost. Reset on every call; a stubbed call leaves it empty.
        self._last_call_telemetry: dict[str, Any] = {}

    def _call_chat_completion(self, prompt: str, system_prompt: str) -> str:
        """One chat completion call.

        A transport failure (timeout, connection error) is retried at once, without backoff: a
        timeout is not rate limiting. An HTTP error is never retried. The signature and the
        return type stay as they are, so a sandbox replay can replace this method with a stub.
        """
        api_cfg = self.cfg.runner.api
        endpoint = api_cfg.base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"

        headers = {"Content-Type": "application/json", **api_cfg.extra_headers}
        if api_cfg.api_key:
            headers.setdefault("Authorization", f"Bearer {api_cfg.api_key}")

        body: dict[str, Any] = {
            "model": api_cfg.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        # An empty reasoning effort is omitted: not every OpenAI-compatible endpoint knows it.
        effort = (api_cfg.reasoning_effort or "").strip()
        if effort:
            body["reasoning_effort"] = effort

        telemetry: dict[str, Any] = {
            "attempts": 0,
            "duration_ms": 0,
            "served_model": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "reasoning_tokens": None,
        }
        self._last_call_telemetry = telemetry
        started = time.perf_counter()
        max_attempts = 1 + max(0, int(api_cfg.transport_retries or 0))
        attempts = 0
        while True:
            attempts += 1
            telemetry["attempts"] = attempts
            try:
                resp = httpx.post(endpoint, headers=headers, json=body, timeout=api_cfg.timeout_seconds)
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                telemetry["duration_ms"] = int((time.perf_counter() - started) * 1000)
                # Sanitized on purpose: the status code is actionable, the request URL and
                # its query are not, and they can carry a deployment's private host names.
                raise RuntimeError(f"Model API returned HTTP {exc.response.status_code}") from None
            except (httpx.TimeoutException, httpx.ConnectError):
                telemetry["duration_ms"] = int((time.perf_counter() - started) * 1000)
                if attempts >= max_attempts:
                    raise
        telemetry["duration_ms"] = int((time.perf_counter() - started) * 1000)
        data = resp.json()
        if isinstance(data, dict):
            telemetry["served_model"] = data.get("model")
            usage = data.get("usage")
            if isinstance(usage, dict):
                telemetry["prompt_tokens"] = _as_int(usage.get("prompt_tokens"))
                telemetry["completion_tokens"] = _as_int(usage.get("completion_tokens"))
                details = usage.get("completion_tokens_details")
                reasoning = _as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else None
                if reasoning is None:
                    reasoning = _as_int(usage.get("reasoning_tokens"))
                telemetry["reasoning_tokens"] = reasoning
        content = ""
        if isinstance(data, dict):
            choices = data.get("choices") or []
            message = (choices[0] or {}).get("message") or {} if choices else {}
            content = message.get("content", "")
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    def _model_call_error(self, stage: str, exc: Exception) -> ModelCallError:
        """Describe a transport failure without naming the gateway."""
        api_cfg = self.cfg.runner.api
        telemetry = dict(self._last_call_telemetry or {})
        summary = {
            "stage": stage,
            "timeout_seconds": api_cfg.timeout_seconds,
            "attempts": int(telemetry.get("attempts") or 1),
            "error_type": type(exc).__name__,
        }
        message = (
            f"模型调用失败（阶段 {stage}，{summary['error_type']}，"
            f"超时阈值 {summary['timeout_seconds']} 秒，尝试 {summary['attempts']} 次）"
        )
        return ModelCallError(message, summary=summary, error_type=summary["error_type"], telemetry=telemetry)

    def _cache_file(self, stage: str, prompt: str) -> Path:
        api_cfg = self.cfg.runner.api
        prompt_hash = canonical_hash(
            {
                "stage": stage,
                "base_url": api_cfg.base_url,
                "model": api_cfg.model,
                # A different reasoning effort asks a different question: it must not hit an old cache.
                "reasoning_effort": (api_cfg.reasoning_effort or "").strip(),
                "prompt": prompt,
            }
        )
        return self.fs_store.root / "api_cache" / f"{stage}-{prompt_hash[:16]}.json"

    @staticmethod
    def _persist_cache(cache_file: Path, parsed: Any) -> None:
        """Write a validated reply to the cache. Only called once the reply passed validation."""
        if not isinstance(parsed, dict):
            return
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")

    def _invoke_api(
        self,
        stage: str,
        prompt: str,
        *,
        repair_schema_hint: str | None = None,
        reply_trace: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Call the model on a prompt, serving a cached reply when there is one.

        `reply_trace`, when given, receives the reply's length, why it is not a JSON object when it is not,
        and whether the format repair ran.
        The reply is NOT cached here: caching happens after the reply passed validation, so an
        unusable output never poisons the next run.
        """
        api_cfg = self.cfg.runner.api
        if (
            not self.cfg.safety_valves.effective_model_execution_enabled
            or not api_cfg.base_url
            or not api_cfg.model
        ):
            return None

        cache_file = self._cache_file(stage, prompt)
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass

        sys_prompt = "You are an AI Flywheel workflow worker. Return strictly valid JSON with no markdown prose outside JSON."
        try:
            raw_text = self._call_chat_completion(prompt, sys_prompt)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            # The transport failure is described without naming the gateway; the node keeps it.
            raise self._model_call_error(stage, exc) from None
        if reply_trace is not None:
            reply_trace["output_chars"] = len(raw_text)
            reply_trace["call_telemetry"] = dict(self._last_call_telemetry)
        parsed = _extract_json_block(raw_text)
        if parsed is None and reply_trace is not None and raw_text.strip():
            # Why the reply is not a JSON object, in the decoder's words. Never the reply itself.
            reply_trace["parse_error"] = _json_parse_error(raw_text)

        # Lightweight 1-retry JSON format repair (without re-running earlier stages)
        if parsed is None and repair_schema_hint and raw_text.strip():
            if reply_trace is not None:
                reply_trace["repair_attempted"] = True
            repair_prompt = (
                "The previous output was malformed JSON. Fix ONLY the JSON syntax/structure and return STRICT JSON ONLY.\n"
                f"Expected top-level schema: {repair_schema_hint}\n\n"
                f"Malformed output:\n{raw_text[:8000]}"
            )
            try:
                repaired_text = self._call_chat_completion(
                    repair_prompt,
                    "You are a strict JSON repair utility. Return ONLY one valid JSON object.",
                )
                parsed = _extract_json_block(repaired_text)
                if parsed is None and reply_trace is not None and repaired_text.strip():
                    reply_trace["repair_parse_error"] = _json_parse_error(repaired_text)
            except Exception as exc:
                parsed = None
                if reply_trace is not None:
                    # The repair call did not complete. Name the exception type; its message may carry a private URL.
                    reply_trace["repair_error"] = type(exc).__name__
                    reply_trace["repair_call_telemetry"] = dict(self._last_call_telemetry)

        return parsed

    def review_sessions(
        self,
        sessions: list[tuple[SessionRecord, str]],
        open_retros: list[RetroItem],
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
    ) -> dict[str, Any]:
        """P02 from the model reply only: an unusable reply fails the node, it never falls back."""
        prompt = _build_p02_prompt(sessions, open_retros, existing_skills, phase)
        cache_file = self._cache_file("p02_review", prompt)
        cache_hit = cache_file.exists()
        reply_trace: dict[str, Any] = {}
        parsed = self._invoke_api(
            "p02_review",
            prompt,
            repair_schema_hint='{"findings":[...],"mistakes":[...],"dissents":[...],"repeated_workflows":[...]}',
            reply_trace=reply_trace,
        )
        review = _review_from_runner_output(
            parsed,
            source=REVIEW_SOURCE_MODEL,
            phase=phase,
            sessions_sent=min(len(sessions), P02_MAX_SESSIONS),
            output_chars=reply_trace.get("output_chars"),
            repair_attempted=bool(reply_trace.get("repair_attempted")),
            parse_error=reply_trace.get("parse_error"),
            repair_parse_error=reply_trace.get("repair_parse_error"),
            repair_error=reply_trace.get("repair_error"),
            call_telemetry=reply_trace.get("call_telemetry"),
            cache_file=cache_file.name,
            cache_hit=cache_hit,
            cache_file_present=cache_hit,
        )
        # The review is usable: now the reply may be cached for the next identical batch.
        self._persist_cache(cache_file, parsed)
        review["runner"] = f"api:{self.cfg.runner.api.model}"
        review["sent_session_keys"] = [s.session_key for s, _ in sessions[:P02_MAX_SESSIONS]]
        return review

    def plan_writes_detailed(
        self,
        review_findings: dict[str, Any],
        existing_rules: list[RuleRecord],
        max_write_set: int,
        default_doc_prefix: str = "lessons/",
        *,
        existing_skills: list[dict[str, Any]] | None = None,
        phase: str = "daily",
        routing_candidates: RoutingCandidates | None = None,
    ) -> PlanOutcome:
        """P03 from the model output only: an unusable reply fails the node, it never falls back."""
        prompt = _build_p03_prompt(
            review_findings,
            existing_rules,
            existing_skills,
            max_write_set,
            default_doc_prefix,
            phase,
            routing_candidates=routing_candidates,
            existing_skill_sections=_existing_skill_sections(review_findings, self.cfg.primary_skills_dir),
        )
        cache_file = self._cache_file("p03_plan", prompt)
        cache_hit = cache_file.exists()
        repair_hint = (
            P03_REPAIR_SCHEMA_HINT_WITH_ROUTE
            if routing_candidates is not None and routing_candidates.routable
            else P03_REPAIR_SCHEMA_HINT
        )
        reply_trace: dict[str, Any] = {}
        parsed = self._invoke_api("p03_plan", prompt, repair_schema_hint=repair_hint, reply_trace=reply_trace)
        outcome = _plan_from_runner_output(
            parsed,
            source=PLAN_SOURCE_MODEL,
            max_write_set=max_write_set,
            default_doc_prefix=default_doc_prefix,
            skills_root=self.cfg.primary_skills_dir,
            cache_file=cache_file.name,
            cache_hit=cache_hit,
            cache_file_present=cache_hit,
            reply_trace=reply_trace,
        )
        # The plan is usable: now the reply may be cached for the next identical batch.
        self._persist_cache(cache_file, parsed)
        return outcome

    def plan_writes(self, *args: Any, **kwargs: Any) -> list[PlannedWrite]:
        """The plan alone; same contract and failures as `plan_writes_detailed`."""
        return self.plan_writes_detailed(*args, **kwargs).planned_writes


def resolve_runner_mode(cfg: FlywheelConfig) -> str:
    """Which runner a configuration selects: ``api:<model>``, ``cli`` or ``dry_run``.

    The single source of truth for the runner-mode string: `build_runner` follows it, and
    the orchestrator records it on the run. A valve that forbids execution turns any mode
    into the local dry-run planner.
    """
    if (
        cfg.runner.mode == "api"
        and cfg.runner.api.base_url
        and cfg.runner.api.model
        and cfg.safety_valves.effective_model_execution_enabled
    ):
        return f"api:{cfg.runner.api.model}"
    if (
        cfg.runner.mode == "cli"
        and cfg.runner.cli.command
        and cfg.safety_valves.effective_model_execution_enabled
    ):
        return "cli"
    return "dry_run"


def build_runner(cfg: FlywheelConfig, fs_store: ImmutableFileStore) -> BaseWorkflowRunner:
    """The runner this configuration selects; always the one `resolve_runner_mode` names."""
    mode = resolve_runner_mode(cfg)
    if mode.startswith("api:"):
        return OpenAICompatibleRunner(cfg, fs_store)
    if mode == "cli":
        return CliSubprocessRunner(cfg, fs_store)
    return DryRunHeuristicRunner()
