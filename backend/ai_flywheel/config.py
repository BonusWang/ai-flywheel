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


class SessionsConfig(BaseModel):
    scan_sources: list[ScanSourceConfig] = Field(default_factory=list)


class KnowledgeSinkConfig(BaseModel):
    type: str = "builtin_kb"  # builtin_kb | local_markdown_wiki
    root: str = "./data/knowledge"
    allowed_write_prefixes: list[str] = Field(
        default_factory=lambda: ["lessons/", "topics/", "projects/"]
    )
    max_write_set: int = 8


class WorkflowConfig(BaseModel):
    schedule_enabled: bool = False
    daily_cron: str = "0 20 * * *"
    weekly_exception_cron: str = "0 20 * * 5"
    auto_approve_writes: bool = False
    max_sessions_per_run: int = 10
    max_chars_per_run: int = 180000


class CliRunnerConfig(BaseModel):
    command: str = ""
    args: list[str] = Field(default_factory=list)
    timeout_seconds: int = 180
    working_dir: str | None = None


class RunnerConfig(BaseModel):
    mode: str = "dry_run"  # dry_run | cli
    cli: CliRunnerConfig = Field(default_factory=CliRunnerConfig)


class GitClosureConfig(BaseModel):
    enabled: bool = False
    remote: str = "origin"
    branch: str = "main"


class WebhookConfig(BaseModel):
    enabled: bool = False
    url: str = ""


class DeliveryConfig(BaseModel):
    local_portal: bool = True
    git_closure: GitClosureConfig = Field(default_factory=GitClosureConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)


class SafetyValvesConfig(BaseModel):
    workflow_enabled: bool = True
    cli_execution_enabled: bool = True
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
    def effective_auto_approve(self) -> bool:
        return bool(self.workflow.auto_approve_writes and self.safety_valves.auto_approve_writes)


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
