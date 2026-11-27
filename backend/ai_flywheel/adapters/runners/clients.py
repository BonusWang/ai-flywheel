"""The real runners: a local CLI subprocess and an OpenAI-compatible HTTP API.

Both turn an untrusted reply into a plan or a review through the same contract
(`outcomes.py`): an unusable reply fails the node, it never falls back to the local
heuristic planner. `resolve_runner_mode` is the single source of truth for which runner
a configuration selects, and `build_runner` follows it.

A reply is cached only once it proved usable — and never when any text of it carries a
credential: the item the privacy gate refused is still in the reply, and the cache is a
plain file on disk.
"""
from __future__ import annotations

import contextvars
import json
import logging
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
from ai_flywheel.adapters.runners.outcomes import (
    _plan_from_runner_output,
    _review_from_runner_output,
    deposit_page_types,
    plan_reply_refused_a_credential,
    reply_carries_secret,
)
from ai_flywheel.adapters.runners.prompts import (
    _build_p02_prompt,
    _build_p03_prompt,
    _existing_skill_sections,
)
from ai_flywheel.adapters.wiki.router import RoutingCandidates
from ai_flywheel.config import ApiRunnerConfig, FlywheelConfig
from ai_flywheel.models import PlannedWrite, RetroItem, RuleRecord, SessionRecord
from ai_flywheel.storage.fs_store import ImmutableFileStore, canonical_hash

logger = logging.getLogger(__name__)

# The two stages that call a model; their names also head their cache file names.
P02_STAGE = "p02_review"
P03_STAGE = "p03_plan"


def _write_reply_cache(cache_file: Path, parsed: Any) -> bool:
    """Write a validated reply to its cache file. Returns whether it was written.

    Only a caller that found the reply usable may come here, and even then one reply is
    refused: one that carries a credential anywhere in its text. The privacy gate reads
    items one by one, so a reply may hold a usable item next to the one it refused — and
    that refused item is still in the reply. The cache is a plain JSON file on disk, read
    back verbatim by the next identical batch, so such a reply is never written: the next
    run asks the model again instead. The log names the kind of credential, never the value.
    """
    if not isinstance(parsed, dict):
        return False
    shape = reply_carries_secret(parsed)
    if shape is not None:
        logger.warning(
            "Reply not cached (%s): a credential shape is in the reply (%s). The next identical batch asks again.",
            cache_file.name,
            shape,
        )
        return False
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _read_cached_reply(cache_file: Path) -> Any | None:
    """The cached reply for a prompt, or None when this run may not use one.

    A file that is not JSON is ignored, as it always was. So is one that carries a
    credential: the cache stopped writing such replies, and a file written before that is
    not served — the run asks again — nor left on disk. It is the product's own cache
    file, and a credential in it is plaintext at rest for as long as it exists.
    """
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    shape = reply_carries_secret(cached)
    if shape is None:
        return cached
    logger.warning(
        "Cached reply discarded (%s): a credential shape is in it (%s). The run asks again.",
        cache_file.name,
        shape,
    )
    try:
        cache_file.unlink()
    except OSError as exc:
        # Still not served; the name above tells the maintainer which file to remove by hand.
        logger.warning("Cached reply (%s) could not be removed: %s", cache_file.name, type(exc).__name__)
    return None


def _cached_reply(cache_file: Path, reply_trace: dict[str, Any] | None) -> Any | None:
    """The cached reply to serve for a prompt, or None when this run has to ask.

    Says in ``reply_trace["cache_hit"]`` what became of a cache file that exists: served,
    or discarded because it carries a credential. A run that asks the model after a
    discarded file is not reported as a cache hit.
    """
    if not cache_file.exists():
        return None
    cached = _read_cached_reply(cache_file)
    if reply_trace is not None:
        reply_trace["cache_hit"] = cached is not None
    return cached


def _plan_write_caps(cfg: FlywheelConfig, routing_candidates: RoutingCandidates | None) -> tuple[int | None, int | None]:
    """The caps a plan is counted under when it may hold project-domain pages.

    Returns `(max_domain_writes, total_write_set)`, or `(None, None)` for a plan that can
    hold no domain page and is counted by `max_write_set` alone. Domain pages are counted
    apart from every other item, under `workflow.max_domain_writes_per_run`; the plan as a
    whole is bounded by `knowledge_sink.max_write_set`. The domain cap is cut to that
    bound, so the three caps together never let a batch exceed it.
    """
    if not deposit_page_types(routing_candidates):
        return None, None
    total = max(1, int(cfg.knowledge_sink.max_write_set))
    return min(max(0, int(cfg.workflow.max_domain_writes_per_run)), total), total


class CliSubprocessRunner(BaseWorkflowRunner):
    """Configurable local CLI subprocess runner (e.g. codex / claude CLI) with receipt caching."""

    def __init__(self, cfg: FlywheelConfig, fs_store: ImmutableFileStore) -> None:
        self.cfg = cfg
        self.fs_store = fs_store

    def _cache_file(self, stage: str, prompt: str) -> Path:
        prompt_hash = canonical_hash({"stage": stage, "cmd": self.cfg.runner.cli.command, "prompt": prompt})
        return self.fs_store.root / "cli_cache" / f"{stage}-{prompt_hash[:16]}.json"

    @staticmethod
    def _persist_cache(cache_file: Path, parsed: Any) -> bool:
        """Write a validated reply to the cache — unless it carries a credential, see `_write_reply_cache`."""
        return _write_reply_cache(cache_file, parsed)

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
        cached = _cached_reply(cache_file, reply_trace)
        if cached is not None:
            return cached

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
        prompt = _build_p02_prompt(
            sessions, open_retros, existing_skills, phase,
            session_char_limit=self.cfg.runner.api.p02_session_char_limit,
        )
        cache_file = self._cache_file(P02_STAGE, prompt)
        reply_trace: dict[str, Any] = {}
        parsed = self._invoke_cli(P02_STAGE, prompt, reply_trace=reply_trace)
        cache_hit = reply_trace.get("cache_hit", cache_file.exists())
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
        _cache_review_reply(self._persist_cache, cache_file, parsed, cache_hit)
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
        domain_page_types = deposit_page_types(routing_candidates)
        max_domain_writes, total_write_set = _plan_write_caps(self.cfg, routing_candidates)
        prompt = _build_p03_prompt(
            review_findings,
            existing_rules,
            existing_skills,
            max_write_set,
            default_doc_prefix,
            phase,
            routing_candidates=routing_candidates,
            existing_skill_sections=_existing_skill_sections(review_findings, self.cfg.primary_skills_dir),
            max_domain_writes=max_domain_writes,
            total_write_set=total_write_set,
        )
        cache_file = self._cache_file(P03_STAGE, prompt)
        reply_trace: dict[str, Any] = {}
        parsed = self._invoke_cli(P03_STAGE, prompt, reply_trace=reply_trace)
        cache_hit = reply_trace.get("cache_hit", cache_file.exists())
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
            domain_page_types=domain_page_types,
            max_domain_writes=max_domain_writes,
            total_write_set=total_write_set,
        )
        # The plan is usable: now the reply may be cached for the next identical batch —
        # unless the privacy gate refused part of it, or a kept draft carries a credential.
        _cache_plan_reply(self._persist_cache, cache_file, parsed, outcome)
        return outcome

    def plan_writes(self, *args: Any, **kwargs: Any) -> list[PlannedWrite]:
        """The plan alone; same contract and failures as `plan_writes_detailed`."""
        return self.plan_writes_detailed(*args, **kwargs).planned_writes


def _cache_plan_reply(
    persist: Any, cache_file: Path, parsed: Any, outcome: PlanOutcome
) -> bool:
    """Cache the reply behind a usable plan, when nothing in it carries a credential.

    The item the privacy gate refused never reaches the plan, but it is still in the
    reply; and the title and summary of a kept item are not gated when the plan is
    normalised. Either is a reason to leave the whole reply out of the cache: the plan
    itself is unaffected. A plan that was not cached names no cache file, so no later
    message tells the maintainer to delete a file that does not exist.
    """
    if plan_reply_refused_a_credential(outcome, parsed) is not None:
        outcome.diagnostics.cache_file = None
    return persist(cache_file, parsed)


class OpenAICompatibleRunner(BaseWorkflowRunner):
    """OpenAI-compatible HTTP API runner (works with local routing gateways, OpenAI, DeepSeek, Ollama)."""

    def __init__(self, cfg: FlywheelConfig, fs_store: ImmutableFileStore) -> None:
        self.cfg = cfg
        self.fs_store = fs_store
        # What the last model call cost, for the thread that made it. Reset on every call; a
        # stubbed call leaves it empty.
        self._call_telemetry: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
            "flywheel_call_telemetry", default={}
        )
        # The model and the reasoning effort of the stage a call belongs to: `(model, effort)`
        # while one is in flight, None when none is. A context variable rather than an
        # attribute, because one runner is shared: two batches run it from two threads, and
        # each thread's context is its own. An attribute would let one batch's exit restore
        # the values it saw on entry over the stage the other batch entered meanwhile.
        # Empty values mean that no stage is in flight and the configured pair applies.
        self._stage_params: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
            "flywheel_stage_params", default=None
        )

    @property
    def _last_call_telemetry(self) -> dict[str, Any]:
        """The telemetry of the last call made from this thread; empty before the first one."""
        return self._call_telemetry.get()

    @property
    def _active_stage_model(self) -> str:
        """The model of the stage in flight in this thread; empty when none is."""
        params = self._stage_params.get()
        return params[0] if params else ""

    @property
    def _active_stage_effort(self) -> str:
        """The reasoning effort of the stage in flight in this thread; empty when none is."""
        params = self._stage_params.get()
        return params[1] if params else ""

    def _resolve_stage_api_params(self, stage: str) -> tuple[str, str]:
        """The model and the reasoning effort a stage really runs with.

        A stage's own key wins when it is set; `model` / `reasoning_effort` serve the stage
        otherwise, so a configuration written before the per-stage keys existed keeps its
        meaning. An unknown stage runs on the configured pair as well.
        """
        api_cfg = self.cfg.runner.api
        model = api_cfg.model
        effort = (api_cfg.reasoning_effort or "").strip()
        if stage == P02_STAGE:
            model = (api_cfg.review_model or "").strip() or model
            effort = (api_cfg.review_reasoning_effort or "").strip() or effort
        elif stage == P03_STAGE:
            model = (api_cfg.plan_model or "").strip() or model
            effort = (api_cfg.plan_reasoning_effort or "").strip() or effort
        return model, effort

    @contextmanager
    def _stage_call(self, stage: str) -> Iterator[None]:
        """Make the calls made inside run on `stage`'s model and effort.

        The two-argument `_call_chat_completion(prompt, system_prompt)` keeps its signature —
        a sandbox replay replaces it with a stub that takes no stage — and reads the stage
        off the context instead. Whatever happens inside, the stage that was in flight
        before is back afterwards, in the thread that entered.
        """
        token = self._stage_params.set(self._resolve_stage_api_params(stage))
        try:
            yield
        finally:
            self._stage_params.reset(token)

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

        # The model and the effort the call in flight asks for: the stage's own when one is
        # in flight (`_stage_call`), the configured pair otherwise.
        model, effort = api_cfg.model, (api_cfg.reasoning_effort or "").strip()
        stage_params = self._stage_params.get()
        if stage_params is not None and (stage_params[0] or "").strip():
            model, effort = stage_params[0], (stage_params[1] or "").strip()
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        # An empty reasoning effort is omitted: not every OpenAI-compatible endpoint knows it.
        if effort:
            body["reasoning_effort"] = effort

        telemetry: dict[str, Any] = {
            "attempts": 0,
            "duration_ms": 0,
            # What was asked for, as distinct from `served_model`, which is what answered.
            "model": model,
            "reasoning_effort": effort or None,
            "served_model": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "reasoning_tokens": None,
        }
        self._call_telemetry.set(telemetry)
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
        # The pair the stage really runs with. A stage that overrides the model, the effort or
        # both must not be served the reply the configured pair produced, whichever way the
        # configuration changes; without an override the pair is the configured one, so a
        # cache written before the per-stage keys existed is still found.
        model, effort = self._resolve_stage_api_params(stage)
        prompt_hash = canonical_hash(
            {
                "stage": stage,
                "base_url": api_cfg.base_url,
                "model": model,
                # A different reasoning effort asks a different question: it must not hit an old cache.
                "reasoning_effort": effort,
                "prompt": prompt,
            }
        )
        return self.fs_store.root / "api_cache" / f"{stage}-{prompt_hash[:16]}.json"

    @staticmethod
    def _persist_cache(cache_file: Path, parsed: Any) -> bool:
        """Write a validated reply to the cache — unless it carries a credential, see `_write_reply_cache`."""
        return _write_reply_cache(cache_file, parsed)

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
        # The model this stage really asks: its own when one is configured, the configured
        # `model` otherwise. A blank one (and no valve, no endpoint) means there is no call to make.
        effective_model = self._resolve_stage_api_params(stage)[0].strip()
        if (
            not self.cfg.safety_valves.effective_model_execution_enabled
            or not api_cfg.base_url
            or not effective_model
        ):
            return None

        cache_file = self._cache_file(stage, prompt)
        cached = _cached_reply(cache_file, reply_trace)
        if cached is not None:
            return cached

        sys_prompt = "You are an AI Flywheel workflow worker. Return strictly valid JSON with no markdown prose outside JSON."
        try:
            with self._stage_call(stage):
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
                # The repair answers for the same stage, on the same model and effort.
                with self._stage_call(stage):
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
        # The model the review really asks: the review's own when one is configured.
        model, _effort = self._resolve_stage_api_params(P02_STAGE)
        prompt = _build_p02_prompt(
            sessions, open_retros, existing_skills, phase,
            session_char_limit=self.cfg.runner.api.p02_session_char_limit,
        )
        cache_file = self._cache_file(P02_STAGE, prompt)
        reply_trace: dict[str, Any] = {}
        parsed = self._invoke_api(
            P02_STAGE,
            prompt,
            repair_schema_hint='{"findings":[...],"events":[...],"mistakes":[...],"dissents":[...],"repeated_workflows":[...]}',
            reply_trace=reply_trace,
        )
        cache_hit = reply_trace.get("cache_hit", cache_file.exists())
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
        _cache_review_reply(self._persist_cache, cache_file, parsed, cache_hit)
        review["runner"] = f"api:{model}"
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
        domain_page_types = deposit_page_types(routing_candidates)
        max_domain_writes, total_write_set = _plan_write_caps(self.cfg, routing_candidates)
        model, _effort = self._resolve_stage_api_params(P03_STAGE)
        prompt = _build_p03_prompt(
            review_findings,
            existing_rules,
            existing_skills,
            max_write_set,
            default_doc_prefix,
            phase,
            routing_candidates=routing_candidates,
            existing_skill_sections=_existing_skill_sections(review_findings, self.cfg.primary_skills_dir),
            max_domain_writes=max_domain_writes,
            total_write_set=total_write_set,
        )
        cache_file = self._cache_file(P03_STAGE, prompt)
        cache_hit = cache_file.exists()
        repair_hint = (
            P03_REPAIR_SCHEMA_HINT_WITH_ROUTE
            if routing_candidates is not None and routing_candidates.routable
            else P03_REPAIR_SCHEMA_HINT
        )
        reply_trace: dict[str, Any] = {}
        parsed = self._invoke_api(P03_STAGE, prompt, repair_schema_hint=repair_hint, reply_trace=reply_trace)
        cache_hit = reply_trace.get("cache_hit", cache_file.exists())
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
            domain_page_types=domain_page_types,
            max_domain_writes=max_domain_writes,
            total_write_set=total_write_set,
            model=model,
        )
        # The plan is usable: now the reply may be cached for the next identical batch —
        # unless the privacy gate refused part of it, or a kept draft carries a credential.
        _cache_plan_reply(self._persist_cache, cache_file, parsed, outcome)
        return outcome

    def plan_writes(self, *args: Any, **kwargs: Any) -> list[PlannedWrite]:
        """The plan alone; same contract and failures as `plan_writes_detailed`."""
        return self.plan_writes_detailed(*args, **kwargs).planned_writes


def _cache_review_reply(persist: Any, cache_file: Path, parsed: Any, cache_hit: bool) -> None:
    """Cache the reply behind a usable review, when nothing in it carries a credential.

    A review quotes the sessions it read, so a credential one of them held can be in its
    findings; such a reply stays out of the cache. The review itself is unaffected, and its
    diagnostics stop naming a cache file that was never written.
    """
    if not persist(cache_file, parsed) and not cache_hit:
        if isinstance(parsed, dict) and isinstance(parsed.get("review_diagnostics"), dict):
            parsed["review_diagnostics"]["cache_file"] = None


def api_model_label(api_cfg: ApiRunnerConfig) -> str:
    """The model the API channel runs on; empty when the configuration names none.

    The configured ``model`` serves every stage. A configuration may leave it empty and
    name a model for each of the two stages instead; both are needed, because one stage
    on its own would leave the other with nothing to ask. Such a channel is named
    ``<review_model>/<plan_model>``.
    """
    model = (api_cfg.model or "").strip()
    if model:
        return model
    review_model = (api_cfg.review_model or "").strip()
    plan_model = (api_cfg.plan_model or "").strip()
    if review_model and plan_model:
        return f"{review_model}/{plan_model}"
    return ""


def resolve_runner_mode(cfg: FlywheelConfig) -> str:
    """Which runner a configuration selects: ``api:<model>``, ``cli`` or ``dry_run``.

    The single source of truth for the runner-mode string: `build_runner` follows it, and
    the orchestrator records it on the run. A valve that forbids execution turns any mode
    into the local dry-run planner.

    The API channel is ready when it names a model (`api_model_label`): the configured
    one, or one for each stage. A configuration that names a model for each stage and no
    common one is an API configuration, not a dry-run one.
    """
    model = api_model_label(cfg.runner.api)
    if (
        cfg.runner.mode == "api"
        and cfg.runner.api.base_url
        and model
        and cfg.safety_valves.effective_model_execution_enabled
    ):
        return f"api:{model}"
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
