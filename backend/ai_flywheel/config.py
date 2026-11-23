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
    triage: SessionTriageConfig = Field(default_factory=SessionTriageConfig)


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
    wiki_d2_postflight_script: str = ""
    wiki_lint_script: str = ""
    wiki_python: str = ""
    wiki_bash: str = "bash"
    wiki_gate_timeout_seconds: int = 300
    wiki_gate_evidence_dir: str = ""


class WorkflowConfig(BaseModel):
    schedule_enabled: bool = False
    daily_cron: str = "0 20 * * *"
    weekly_exception_cron: str = "0 20 * * 5"
    friday_weekly_enabled: bool = True
    approval_mode: str = "always"  # smart | always | auto
    high_write_count_threshold: int = 6
    max_writes_per_run: int = 8
    auto_approve_writes: bool = False
    max_sessions_per_run: int = 10
    max_chars_per_run: int = 180000


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


class RunnerConfig(BaseModel):
    mode: str = "dry_run"  # dry_run | api | cli
    api: ApiRunnerConfig = Field(default_factory=ApiRunnerConfig)
    cli: CliRunnerConfig = Field(default_factory=CliRunnerConfig)


class GitClosureConfig(BaseModel):
    enabled: bool = False
    remote: str = "origin"
    branch: str = "main"


class WebhookConfig(BaseModel):
    enabled: bool = False
    url: str = ""


class FeishuCliConfig(BaseModel):
    enabled: bool = False
    command: str = "lark-cli"
    identity: str = "bot"
    user_id: str = ""
    chat_id: str = ""
    notify_on_fyi: bool = False


class DeliveryConfig(BaseModel):
    local_portal: bool = True
    git_closure: GitClosureConfig = Field(default_factory=GitClosureConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    feishu_cli: FeishuCliConfig = Field(default_factory=FeishuCliConfig)


class SafetyValvesConfig(BaseModel):
    workflow_enabled: bool = True
    cli_execution_enabled: bool = True
    skill_write_enabled: bool = True
    auto_approve_writes: bool = False
    git_closure_enabled: bool = False
    public_export_strip_paths: bool = True


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
        return self.resolve_path(self.profile.default_dir)

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
        return bool(
            (self.workflow.auto_approve_writes and self.safety_valves.auto_approve_writes)
            or self.workflow.approval_mode == "auto"
        )

    @property
    def effective_approval_mode(self) -> str:
        if self.effective_auto_approve:
            return "auto"
        mode = (self.workflow.approval_mode or "always").strip().lower()
        return mode if mode in {"smart", "always", "auto"} else "always"


def _apply_env_valves(raw: dict[str, Any]) -> dict[str, Any]:
    valves = raw.setdefault("safety_valves", {})
    env_map = {
        "FLYWHEEL_WORKFLOW_ENABLED": "workflow_enabled",
        "FLYWHEEL_CLI_EXECUTION_ENABLED": "cli_execution_enabled",
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


def load_config(config_path: str | Path | None = None, base_dir: str | Path | None = None) -> FlywheelConfig:
    """Load flywheel.yaml (or defaults if absent), applying environment safety valves."""
    resolved_base = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
    target_path: Path | None = None

    if config_path:
        target_path = Path(config_path).resolve()
        resolved_base = target_path.parent
    else:
        env_cfg = os.environ.get("FLYWHEEL_CONFIG")
        if env_cfg:
            target_path = Path(env_cfg).resolve()
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
