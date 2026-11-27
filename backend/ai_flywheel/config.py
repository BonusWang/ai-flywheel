from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base without mutating inputs."""
    merged = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], val)
        else:
            merged[key] = copy.deepcopy(val)
    return merged


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: str = "./data"


class ProfileConfig(BaseModel):
    default_dir: str = "./profile"
    override_dir: str | None = None


class CapabilitiesConfig(BaseModel):
    skills_dirs: list[str] = Field(default_factory=list)
    agents_dirs: list[str] = Field(default_factory=list)


class ScanSourceConfig(BaseModel):
    client: str
    path: str
    glob: str = "**/*.jsonl"


class SessionTriageConfig(BaseModel):
    enabled: bool = True
    min_chars: int = 600
    gray_workspace_min_chars: int = 1500
    min_turns: int = 2
    exclude_filename_prefixes: list[str] = Field(
        default_factory=lambda: ["agent-", "subagent-", "warmup-"]
    )
    exclude_content_prefixes: list[str] = Field(default_factory=list)
    # Phrases that identify a controlled knowledge-base write dispatch in this
    # installation (see `is_controlled_write_dispatch`): a heading of its dispatch
    # template, the name of its writer skill. A session whose only instruction
    # carries one is the writing of a document, not a conversation to distill, and
    # is excluded. The built-in rule already recognises a dispatch that hands over
    # an approved write-set and names the write contract; a phrase only one
    # installation uses belongs here, in the private configuration, never in the code.
    controlled_write_markers: list[str] = Field(default_factory=list)
    project_workspace_patterns: list[str] = Field(
        default_factory=lambda: ["projects", "github", "repos", "workspace", "wiki", "src", "ideaprojects"]
    )
    temp_workspace_patterns: list[str] = Field(
        default_factory=lambda: ["chat", "temp", "tmp", "scratch", "desktop", "downloads"]
    )
    exclude_workspace_patterns: list[str] = Field(
        default_factory=lambda: ["node_modules", ".cache"]
    )


class SessionsConfig(BaseModel):
    scan_sources: list[ScanSourceConfig] = Field(default_factory=list)
    # Codex strict-archive sources: only sessions that land in one of these
    # archive directories (or are declared explicitly through MCP / the portal)
    # enter the distill queue. An active, unarchived session is never queued.
    codex_archive_sources: list[ScanSourceConfig] = Field(default_factory=list)
    triage: SessionTriageConfig = Field(default_factory=SessionTriageConfig)
    # Distill queue: per-item character cap (a larger session is compacted, then
    # split into ordered chunks 1/N..N/N), the retry budget before a poison item
    # is isolated into the dead-letter state, and the interval of the background
    # archive reconciliation loop that the serve role runs.
    queue_max_item_chars: int = 24000
    queue_max_retries: int = 3
    codex_archive_sync_interval_seconds: int = 60
    # How long an Antigravity transcript must have gone unchanged before a
    # passive scan reads it, in seconds (900 = 15 minutes). Antigravity keeps no
    # archive directory that marks a finished conversation, so a transcript that
    # still changes in place is an ongoing one: the scan leaves it out of the
    # pass and picks it up once it has gone quiet. 0 switches the window off.
    scan_quiet_window_seconds: int = Field(default=900, ge=0)


class KnowledgeSinkConfig(BaseModel):
    type: str = "builtin_kb"  # builtin_kb | local_markdown_wiki
    root: str = "./data/knowledge"
    allowed_write_prefixes: list[str] = Field(
        default_factory=lambda: ["lessons/", "topics/", "projects/"]
    )
    max_write_set: int = 8

    # D4 (2026-11-23): P04 write gate for a schema-governed Markdown Wiki.
    # The gate is contract-driven: it reads .wiki-schema.md at the sink root.
    # Script paths stay empty by default so the public repo carries no personal
    # absolute paths; a private flywheel.yaml (gitignored) or the env vars below
    # supply them.  When the sink root is a Wiki and scripts are required but
    # missing, writes fail closed instead of silently skipping index/validator.
    wiki_gate_enabled: bool = True
    wiki_gate_require_scripts: bool = True
    # Neutral name of the post-write verification hook (SchemaWriteGate). Any
    # schema-governed root can supply one; it need not be a Wiki-specific D2 script.
    postflight_hook_script: str = ""
    # Legacy name of the same hook. Kept 100% backward compatible: existing
    # configurations and env vars keep working, and `effective_postflight_script`
    # falls back to it whenever the neutral name is unset.
    wiki_d2_postflight_script: str = ""
    wiki_lint_script: str = ""
    wiki_python: str = ""
    wiki_bash: str = "bash"
    wiki_gate_timeout_seconds: int = 300
    wiki_gate_evidence_dir: str = ""
    # Name of the environment variable the D2 script reads as its work root. Empty means
    # the neutral default (FLYWHEEL_WORK_ROOT); a deployment whose script expects a
    # legacy name sets it here (or via the FLYWHEEL_D2_WORK_ROOT_ENV variable).
    wiki_d2_work_root_env: str = ""

    # Catch-all project for knowledge documents routed on EVIDENCE, i.e. plans of the zero-config
    # dry_run planner (source session's workspace path first, then project identifiers in the draft).
    # api / cli plans declare their route in the P03 plan and are never redirected here: a note that
    # is declared unattributed, or whose declaration is contradicted by the evidence, is skipped and
    # surfaced. Set this to a project directory name (a folder under the projects partition that
    # has 实战笔记/) to catch the evidence-routed cards instead.
    practice_fallback_project: str = ""

    # Opt-in P04 audit trail: after a batch's writes are verified, commit exactly those
    # written files to the local Git repository they live in (one commit per repository,
    # only the written paths staged, never a push). A path outside a repository, a missing
    # git binary or a failing commit is recorded as an audit event and never fails P04.
    auto_git_commit: bool = False

    @property
    def effective_postflight_script(self) -> str:
        """The post-write hook that actually runs.

        The neutral `postflight_hook_script` wins; the legacy `wiki_d2_postflight_script`
        stays a fully supported alias and is used whenever the neutral name is blank.
        """
        return self.postflight_hook_script.strip() or self.wiki_d2_postflight_script.strip()


class WorkflowConfig(BaseModel):
    schedule_enabled: bool = False
    daily_cron: str = "0 20 * * *"
    weekly_exception_cron: str = "0 20 * * 5"
    friday_weekly_enabled: bool = True
    approval_mode: str = "always"  # smart | always | auto
    high_write_count_threshold: int = 6
    max_writes_per_run: int = 8
    # Project-domain increment pages (the table / configuration / architecture /
    # algorithm pages the mounted Wiki's `deposit_routing` names) are counted
    # apart from every other write item, under this cap: an investigation that
    # touches several tables must not crowd the main experience card or a rule
    # out of the plan. 0 plans no domain page. Whatever the two caps allow, the
    # batch as a whole still ends at `knowledge_sink.max_write_set` physical
    # paths, and the high-write approval threshold counts the final items.
    max_domain_writes_per_run: int = Field(default=8, ge=0, le=32)
    auto_approve_writes: bool = False
    max_sessions_per_run: int = 10
    max_chars_per_run: int = 180000
    # How late the daily_flywheel job may still run after its fire time. A host
    # that slept or was busy past the cron time resumes the batch on wake-up
    # instead of losing it; 14400 is four hours. The same value bounds the
    # window in which a missed fire (the service was not running at all) is
    # caught up by the recovery loop.
    misfire_grace_seconds: int = Field(default=14400, ge=60)
    # Serve role only: when today's scheduled fire was missed and the grace
    # window is still open, the recovery loop starts one catch-up batch
    # (trigger="scheduled") instead of only recording the miss.
    catchup_missed_schedule: bool = True


class CliRunnerConfig(BaseModel):
    command: str = ""
    args: list[str] = Field(default_factory=list)
    timeout_seconds: int = 180
    working_dir: str | None = None


class ApiRunnerConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: int = 240
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # Reasoning effort sent to an OpenAI-compatible endpoint ("low", "medium", "high").
    # Empty means the parameter is omitted from the request, which keeps the runner portable
    # to endpoints that do not know it. A non-empty value also enters the cache key: a
    # different effort asks a different question and must not hit an older reply.
    reasoning_effort: str = ""
    # How many times a timed-out or refused connection is retried at once (no backoff).
    # 0 disables the retry; 1 means one extra attempt after the first one.
    transport_retries: int = 1
    # How much of one session's text the P02 review prompt may carry, in
    # characters (default 16000, range 2000..64000). A session longer than this
    # keeps its head and its tail around one explicit omission marker; a session
    # within the budget is carried whole, so a root cause in the middle of a long
    # investigation reaches the reviewer.
    p02_session_char_limit: int = Field(default=16000, ge=2000, le=64000)
    # Per-stage model layering. Each key falls back to `model` / `reasoning_effort`
    # when empty, so a configuration written before these keys existed keeps its
    # meaning. The effective pair of a stage enters the api_cache key: switching
    # a stage to another model never reuses the reply the old one produced.
    review_model: str = ""
    review_reasoning_effort: str = ""
    plan_model: str = ""
    plan_reasoning_effort: str = ""


class RunnerConfig(BaseModel):
    mode: str = "dry_run"  # dry_run | api | cli
    api: ApiRunnerConfig = Field(default_factory=ApiRunnerConfig)
    cli: CliRunnerConfig = Field(default_factory=CliRunnerConfig)


class GitClosureConfig(BaseModel):
    enabled: bool = False
    remote: str = "origin"
    branch: str = "main"


class WebhookDeliveryConfig(BaseModel):
    """A generic JSON webhook: the outbound channel that is not tied to one vendor.

    Every string the channel sends passes the privacy gate first, and the payload
    is signed when ``secret`` is set. The secret is never logged and is masked in
    every configuration dump.
    """

    enabled: bool = False
    url: str = ""
    # Optional HMAC-SHA256 signing key. When non-empty, every request carries
    # ``X-Flywheel-Signature: sha256=<hex>`` over the raw UTF-8 JSON body.
    secret: str = ""
    timeout_seconds: int = 10
    # Routine 'fyi' notices are opt-in; approval and failure notices are not.
    notify_on_fyi: bool = False


class FeishuCliConfig(BaseModel):
    enabled: bool = False
    command: str = "lark-cli"
    # Extra argv inserted between the resolved executable and the subcommand
    # (test doubles inject e.g. ["path/to/fake_lark_cli.py"] with command = python).
    command_args: list[str] = Field(default_factory=list)
    identity: str = "bot"
    user_id: str = ""
    chat_id: str = ""
    notify_on_fyi: bool = False
    # Interactive approval cards (feat-feishu-approval-card-loop). Changes to
    # command / command_args / identity only take effect after a restart.
    interactive: bool = False
    approver_open_ids: list[str] = Field(default_factory=list)

    @property
    def effective_approver_ids(self) -> list[str]:
        """Deduplicated, order-stable approver set: non-blank approver_open_ids plus a non-blank user_id."""
        seen: list[str] = []
        for raw in [*self.approver_open_ids, self.user_id]:
            val = raw.strip()
            if val and val not in seen:
                seen.append(val)
        return seen

    @property
    def interaction_ready(self) -> bool:
        """Interactive mode entry conditions: private-chat recipient, bot identity, non-empty approvers."""
        return bool(self.enabled and self.interactive and self.user_id and self.identity == "bot" and self.effective_approver_ids)


class DeliveryConfig(BaseModel):
    local_portal: bool = True
    git_closure: GitClosureConfig = Field(default_factory=GitClosureConfig)
    webhook: WebhookDeliveryConfig = Field(default_factory=WebhookDeliveryConfig)
    feishu_cli: FeishuCliConfig = Field(default_factory=FeishuCliConfig)


class SafetyValvesConfig(BaseModel):
    workflow_enabled: bool = True
    cli_execution_enabled: bool = True
    # The truthful name of the model-execution valve: it gates every runner that
    # calls a model or a local CLI. `None` means "follow cli_execution_enabled",
    # so a configuration written before this field existed keeps its meaning.
    model_execution_enabled: bool | None = None
    skill_write_enabled: bool = True
    auto_approve_writes: bool = False
    git_closure_enabled: bool = False
    public_export_strip_paths: bool = True

    @property
    def effective_model_execution_enabled(self) -> bool:
        """Whether a runner may call a model or a local CLI.

        `model_execution_enabled` is the name that says what it does; the older
        `cli_execution_enabled` is kept for backward compatibility and decides
        whenever the new field is unset.
        """
        if self.model_execution_enabled is not None:
            return bool(self.model_execution_enabled)
        return bool(self.cli_execution_enabled)


class FlywheelConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    knowledge_sink: KnowledgeSinkConfig = Field(default_factory=KnowledgeSinkConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    safety_valves: SafetyValvesConfig = Field(default_factory=SafetyValvesConfig)

    # Resolved runtime root (not serialized to YAML)
    base_dir: Path = Field(default_factory=lambda: Path.cwd(), exclude=True)
    config_path: Path | None = Field(default=None, exclude=True)

    def resolve_path(self, raw: str) -> Path:
        expanded = Path(os.path.expanduser(raw))
        if expanded.is_absolute():
            return expanded
        return (self.base_dir / expanded).resolve()

    @property
    def data_path(self) -> Path:
        return self.resolve_path(self.server.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "flywheel.db"

    @property
    def receipts_dir(self) -> Path:
        return self.data_path / "receipts"

    @property
    def default_profile_path(self) -> Path:
        candidate = self.resolve_path(self.profile.default_dir)
        if candidate.exists():
            return candidate
        # When launched with an external --config / FLYWHEEL_CONFIG (so base_dir
        # points outside the checkout), fall back to the bundled profile/.
        if self.profile.default_dir in {"./profile", "profile"}:
            bundled = (Path(__file__).resolve().parents[2] / "profile").resolve()
            if bundled.exists():
                return bundled
        return candidate

    @property
    def override_profile_path(self) -> Path | None:
        if not self.profile.override_dir:
            return None
        return self.resolve_path(self.profile.override_dir)

    @property
    def knowledge_root_path(self) -> Path:
        if self.knowledge_sink.type == "builtin_kb":
            return self.resolve_path(self.knowledge_sink.root or "./data/knowledge")
        return self.resolve_path(self.knowledge_sink.root)

    @property
    def wiki_gate_evidence_path(self) -> Path:
        raw = self.knowledge_sink.wiki_gate_evidence_dir or (self.data_path / "wiki-gate")
        return self.resolve_path(str(raw))

    @property
    def primary_skills_dir(self) -> Path:
        if self.capabilities.skills_dirs:
            return self.resolve_path(self.capabilities.skills_dirs[0])
        return self.data_path / "skills"

    @property
    def effective_auto_approve(self) -> bool:
        """Whether writes may be approved without a human.

        The safety valve has the last word: `safety_valves.auto_approve_writes =
        False` vetoes auto-approval even when the workflow asks for it
        (`workflow.auto_approve_writes` or `workflow.approval_mode = "auto"`).
        """
        return bool(
            self.safety_valves.auto_approve_writes
            and (self.workflow.auto_approve_writes or self.workflow.approval_mode == "auto")
        )

    @property
    def effective_approval_mode(self) -> str:
        """The approval mode this configuration really runs with.

        `auto` needs the safety valve open. With the valve closed an `auto`
        request is downgraded to `smart` — routine writes pass, sensitive ones
        still wait for a human — instead of bypassing the gate.
        """
        mode = (self.workflow.approval_mode or "always").strip().lower()
        if mode not in {"smart", "always", "auto"}:
            mode = "always"
        if mode == "auto":
            return "auto" if self.safety_valves.auto_approve_writes else "smart"
        if self.effective_auto_approve:
            return "auto"
        return mode


def _apply_env_valves(raw: dict[str, Any]) -> dict[str, Any]:
    valves = raw.setdefault("safety_valves", {})
    env_map = {
        "FLYWHEEL_WORKFLOW_ENABLED": "workflow_enabled",
        "FLYWHEEL_CLI_EXECUTION_ENABLED": "cli_execution_enabled",
        "FLYWHEEL_MODEL_EXECUTION_ENABLED": "model_execution_enabled",
        "FLYWHEEL_AUTO_APPROVE_WRITES": "auto_approve_writes",
        "FLYWHEEL_GIT_CLOSURE_ENABLED": "git_closure_enabled",
    }
    for env_key, field_name in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            valves[field_name] = val.strip().lower() in {"1", "true", "yes", "on"}
    return raw


def _apply_env_knowledge_sink(raw: dict[str, Any]) -> dict[str, Any]:
    sink = raw.setdefault("knowledge_sink", {})
    bool_map = {
        "FLYWHEEL_WIKI_GATE_ENABLED": "wiki_gate_enabled",
        "FLYWHEEL_WIKI_GATE_REQUIRE_SCRIPTS": "wiki_gate_require_scripts",
    }
    for env_key, field_name in bool_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            sink[field_name] = val.strip().lower() in {"1", "true", "yes", "on"}
    str_map = {
        "FLYWHEEL_POSTFLIGHT_HOOK_SCRIPT": "postflight_hook_script",
        "FLYWHEEL_WIKI_D2_POSTFLIGHT": "wiki_d2_postflight_script",
        "FLYWHEEL_WIKI_LINT_SCRIPT": "wiki_lint_script",
        "FLYWHEEL_WIKI_PYTHON": "wiki_python",
        "FLYWHEEL_WIKI_BASH": "wiki_bash",
        "FLYWHEEL_WIKI_GATE_EVIDENCE_DIR": "wiki_gate_evidence_dir",
    }
    for env_key, field_name in str_map.items():
        val = os.environ.get(env_key)
        if val:
            sink[field_name] = val
    timeout = os.environ.get("FLYWHEEL_WIKI_GATE_TIMEOUT")
    if timeout:
        try:
            sink["wiki_gate_timeout_seconds"] = int(timeout)
        except ValueError:
            pass
    return raw


def _apply_env_delivery(raw: dict[str, Any]) -> dict[str, Any]:
    """Environment overrides for the webhook endpoint and its signing secret.

    A deployment can keep the secret entirely out of ``flywheel.yaml`` by setting
    ``FLYWHEEL_WEBHOOK_SECRET``. An empty or absent variable never overwrites a
    configured value.
    """
    webhook = raw.setdefault("delivery", {}).setdefault("webhook", {})
    for env_key, field_name in (
        ("FLYWHEEL_WEBHOOK_URL", "url"),
        ("FLYWHEEL_WEBHOOK_SECRET", "secret"),
    ):
        val = os.environ.get(env_key)
        if val:
            webhook[field_name] = val
    return raw


def load_config(config_path: str | Path | None = None, base_dir: str | Path | None = None) -> FlywheelConfig:
    """Load flywheel.yaml (or defaults if absent), applying environment safety valves."""
    resolved_base = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
    target_path: Path | None = None

    if config_path:
        target_path = Path(config_path).resolve()
        if not base_dir:
            resolved_base = target_path.parent
    else:
        env_cfg = os.environ.get("FLYWHEEL_CONFIG")
        if env_cfg:
            target_path = Path(env_cfg).resolve()
            if not base_dir:
                resolved_base = target_path.parent
        else:
            candidate = resolved_base / "flywheel.yaml"
            if candidate.exists():
                target_path = candidate
            else:
                target_path = candidate  # default save location

    raw: dict[str, Any] = {}
    if target_path and target_path.exists():
        loaded = yaml.safe_load(target_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded

    raw = _apply_env_valves(raw)
    raw = _apply_env_knowledge_sink(raw)
    raw = _apply_env_delivery(raw)
    cfg = FlywheelConfig.model_validate(raw)
    cfg.base_dir = resolved_base
    cfg.config_path = target_path
    return cfg


def save_config(cfg: FlywheelConfig, path: Path | None = None) -> Path:
    """Persist runtime config to flywheel.yaml (atomic write)."""
    target = path or cfg.config_path or (cfg.base_dir / "flywheel.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json")
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(target)
    return target
