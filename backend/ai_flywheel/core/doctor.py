"""Read-only environment diagnostics behind `flywheel doctor`.

Every check answers one first-run question — "is this installation able to do what
it is configured to do?" — with **PASS** / **WARN** / **FAIL** plus a summary and a
detail line. Nothing here writes: no directory is created, no database is opened,
no configuration is saved. A missing data directory or database is a warning (the
next `serve` creates them), while a configuration that cannot work — an `api`
runner without a URL, a required post-write hook that is not configured — is a
failure.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from ai_flywheel import __version__
from ai_flywheel.adapters.lark_cli import LarkCliRunner, LarkCliUnavailableError
from ai_flywheel.adapters.runners.clients import resolve_runner_mode
from ai_flywheel.adapters.wiki.schema import SCHEMA_FILE_NAME
from ai_flywheel.config import FlywheelConfig

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_STATUS_ORDER = {PASS: 0, WARN: 1, FAIL: 2}

_SQLITE_MAGIC = b"SQLite format 3\x00"


def _check(name: str, status: str, summary: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "summary": summary, "detail": detail}


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=_STATUS_ORDER.__getitem__) if statuses else PASS


def _resolvable_command(command: str) -> bool:
    """Whether a bare command is on PATH, or an explicit path names an existing file."""
    raw = Path(os.path.expanduser(command))
    if raw.is_absolute() or os.path.sep in command or (os.path.altsep and os.path.altsep in command):
        return raw.is_file()
    return shutil.which(command) is not None


# ---------------------------------------------------------------- individual checks


def _check_config(cfg: FlywheelConfig) -> dict[str, str]:
    path = cfg.config_path
    if path is not None and path.is_file():
        return _check(
            "config",
            PASS,
            "configuration loaded",
            f"config={path} base_dir={cfg.base_dir}",
        )
    return _check(
        "config",
        PASS,
        "built-in defaults (no flywheel.yaml yet)",
        f"expected={path if path is not None else cfg.base_dir / 'flywheel.yaml'} base_dir={cfg.base_dir}",
    )


def _check_storage(cfg: FlywheelConfig) -> dict[str, str]:
    data_dir = cfg.data_path
    lines: list[str] = [f"data_dir={data_dir}"]
    statuses: list[str] = []

    if data_dir.is_dir():
        writable = os.access(data_dir, os.W_OK)
        statuses.append(PASS if writable else FAIL)
        lines.append("data_dir exists" + ("" if writable else " but is not writable"))
    elif data_dir.exists():
        statuses.append(FAIL)
        lines.append("data_dir exists but is not a directory")
    else:
        parent = data_dir.parent
        parent_ready = parent.is_dir() and os.access(parent, os.W_OK)
        statuses.append(WARN if parent_ready else FAIL)
        lines.append(
            "data_dir does not exist yet (created on first run)"
            if parent_ready
            else "data_dir does not exist and its parent is not a writable directory"
        )

    db_path = cfg.db_path
    if db_path.is_file():
        try:
            size = db_path.stat().st_size
            head = db_path.open("rb").read(len(_SQLITE_MAGIC))
        except OSError as exc:
            statuses.append(FAIL)
            lines.append(f"database cannot be read: {exc}")
        else:
            if head == _SQLITE_MAGIC:
                statuses.append(PASS)
                lines.append(f"database={db_path} sqlite ok ({size} bytes)")
            elif size == 0:
                statuses.append(WARN)
                lines.append(f"database={db_path} is empty (SQLite initialises it on first write)")
            else:
                statuses.append(FAIL)
                lines.append(f"database={db_path} is not a SQLite database")
    else:
        statuses.append(WARN)
        lines.append(f"database not created yet: {db_path}")

    core_card = data_dir / "FLYWHEEL-CORE.md"
    if core_card.is_file():
        lines.append(f"core_card={core_card} ({core_card.stat().st_size} bytes)")
    else:
        lines.append("core_card not generated yet (written at serve startup)")
        if cfg.db_path.is_file():
            statuses.append(WARN)

    status = _worst(statuses)
    summary = {
        PASS: "data directory and database are usable",
        WARN: "usable, with paths the first run will create",
        FAIL: "storage cannot be used as configured",
    }[status]
    return _check("storage", status, summary, " | ".join(lines))


def _check_knowledge_sink(cfg: FlywheelConfig) -> dict[str, str]:
    sink = cfg.knowledge_sink
    root = cfg.knowledge_root_path
    statuses: list[str] = []
    lines: list[str] = [f"type={sink.type} root={root}"]

    if sink.type not in {"builtin_kb", "local_markdown_wiki"}:
        statuses.append(WARN)
        lines.append(f"unknown sink type '{sink.type}' (writes still use the configured root)")

    if root.is_dir():
        statuses.append(PASS)
        lines.append("root exists")
    elif root.exists():
        statuses.append(FAIL)
        lines.append("root exists but is not a directory")
    elif sink.type == "local_markdown_wiki":
        statuses.append(FAIL)
        lines.append("mounted root does not exist")
    else:
        statuses.append(WARN)
        lines.append("root does not exist yet (created on first write)")

    schema_present = root.is_dir() and (root / SCHEMA_FILE_NAME).is_file()
    if not schema_present:
        lines.append(f"no {SCHEMA_FILE_NAME}; the schema write gate is not applicable")
    elif not sink.wiki_gate_enabled:
        lines.append(f"{SCHEMA_FILE_NAME} found; the schema write gate is disabled by configuration")
    else:
        lines.append(f"{SCHEMA_FILE_NAME} found; the schema write gate applies")
        script = sink.effective_postflight_script
        if script:
            script_path = cfg.resolve_path(script)
            if script_path.is_file():
                statuses.append(PASS)
                lines.append(f"postflight hook={script_path}")
            else:
                statuses.append(FAIL)
                lines.append(f"postflight hook does not exist: {script_path}")
        elif sink.wiki_gate_require_scripts:
            statuses.append(FAIL)
            lines.append(
                "wiki_gate_require_scripts is on but no postflight hook is configured "
                "(set postflight_hook_script, or the legacy wiki_d2_postflight_script); "
                "writes to this root fail closed"
            )
        else:
            statuses.append(WARN)
            lines.append("no postflight hook configured; index/validator sync is skipped")

    status = _worst(statuses)
    summary = {
        PASS: "the knowledge sink is usable",
        WARN: "the knowledge sink is usable, with degraded or not-yet-created paths",
        FAIL: "the knowledge sink is not usable as configured",
    }[status]
    return _check("knowledge_sink", status, summary, " | ".join(lines))


def _check_skills(cfg: FlywheelConfig) -> dict[str, str]:
    configured = cfg.capabilities.skills_dirs
    if not configured:
        return _check(
            "skills",
            PASS,
            "no Skill directories configured",
            f"Skill writes default to {cfg.primary_skills_dir}",
        )
    statuses: list[str] = []
    lines: list[str] = []
    total = 0
    for raw_dir in configured:
        root = cfg.resolve_path(raw_dir)
        if root.is_dir():
            count = len(list(root.glob("*/SKILL.md")))
            total += count
            statuses.append(PASS)
            lines.append(f"{root}: {count} SKILL.md")
        elif root.exists():
            statuses.append(WARN)
            lines.append(f"{root}: exists but is not a directory")
        else:
            statuses.append(WARN)
            lines.append(f"{root}: directory does not exist")
    status = _worst(statuses)
    summary = f"{total} SKILL.md across {len(configured)} configured director{'y' if len(configured) == 1 else 'ies'}"
    return _check("skills", status, summary, " | ".join(lines))


def _check_runner(cfg: FlywheelConfig) -> dict[str, str]:
    requested = (cfg.runner.mode or "dry_run").strip()
    resolved = resolve_runner_mode(cfg)
    valve = cfg.safety_valves.effective_model_execution_enabled

    if requested == "api":
        missing = [
            name
            for name in ("base_url", "model")
            if not getattr(cfg.runner.api, name).strip()
        ]
        if missing:
            return _check(
                "runner",
                FAIL,
                f"runner.mode=api but api.{' and api.'.join(missing)} is empty",
                f"resolved={resolved} model_execution_enabled={valve}",
            )
        if not valve:
            return _check(
                "runner",
                WARN,
                "runner.mode=api but model execution is disabled; runs use dry_run",
                f"resolved={resolved} model_execution_enabled={valve}",
            )
        return _check(
            "runner",
            PASS,
            f"api runner ready: {resolved}",
            f"base_url={cfg.runner.api.base_url}",
        )

    if requested == "cli":
        command = cfg.runner.cli.command.strip()
        if not command:
            return _check(
                "runner",
                FAIL,
                "runner.mode=cli but runner.cli.command is empty",
                f"resolved={resolved}",
            )
        if not _resolvable_command(command):
            return _check(
                "runner",
                FAIL,
                f"cli command cannot be resolved: {command}",
                f"resolved={resolved}",
            )
        if not valve:
            return _check(
                "runner",
                WARN,
                "runner.mode=cli but model execution is disabled; runs use dry_run",
                f"resolved={resolved} model_execution_enabled={valve}",
            )
        return _check("runner", PASS, f"cli runner ready: {command}", f"resolved={resolved}")

    if requested == "dry_run":
        return _check(
            "runner",
            PASS,
            "dry_run (no model calls)",
            "the zero-config planner runs; set runner.mode to api or cli for real model calls",
        )

    return _check(
        "runner",
        FAIL,
        f"unknown runner.mode '{requested}'",
        "expected dry_run, api or cli",
    )


def _check_safety_valves(cfg: FlywheelConfig) -> dict[str, str]:
    valves = cfg.safety_valves
    requested = (cfg.workflow.approval_mode or "always").strip().lower()
    effective = cfg.effective_approval_mode
    detail = " | ".join(
        [
            f"approval_mode requested={requested} effective={effective}",
            f"auto_approve_writes={valves.auto_approve_writes}",
            f"workflow_enabled={valves.workflow_enabled}",
            f"model_execution_enabled={valves.effective_model_execution_enabled}",
        ]
    )
    if requested == "auto" and not valves.auto_approve_writes:
        return _check(
            "safety_valves",
            WARN,
            "approval_mode=auto is downgraded to smart: safety_valves.auto_approve_writes is false",
            detail,
        )
    return _check("safety_valves", PASS, f"approval_mode={effective}", detail)


def _check_channels(cfg: FlywheelConfig) -> dict[str, str]:
    """Outbound notification channels: Feishu and the generic webhook.

    The webhook's signing secret is reported as on/off only — doctor output is
    printed and pasted into issues, so the value itself never appears here.
    """
    feishu = cfg.delivery.feishu_cli
    webhook = cfg.delivery.webhook
    statuses: list[str] = []
    lines: list[str] = []

    if not feishu.enabled:
        lines.append("Feishu disabled")
        feishu_summary = "Feishu channel disabled"
    elif not feishu.interactive:
        statuses.append(PASS)
        lines.append(f"Feishu text-only (command={feishu.command} identity={feishu.identity})")
        feishu_summary = "Feishu text-only (interactive cards off)"
    elif not feishu.interaction_ready:
        statuses.append(FAIL)
        lines.append("interactive cards need a bot identity, a private-chat user_id and at least one approver")
        feishu_summary = "interactive cards are on but incomplete"
    else:
        try:
            argv, kind = LarkCliRunner.resolve(feishu.command)
        except LarkCliUnavailableError as exc:
            statuses.append(FAIL)
            lines.append(f"lark-cli cannot be resolved: {exc}")
            feishu_summary = "interactive cards are on but lark-cli cannot be resolved"
        else:
            statuses.append(PASS)
            lines.append(f"lark-cli={argv[0]} kind={kind} approvers={len(feishu.effective_approver_ids)}")
            feishu_summary = "Feishu interactive cards configured"

    if webhook.enabled and webhook.url.strip():
        statuses.append(PASS)
        lines.append(f"Webhook configured (signature={'on' if webhook.secret.strip() else 'off'})")
    elif webhook.enabled:
        statuses.append(FAIL)
        lines.append("Webhook enabled but delivery.webhook.url is empty")
    else:
        lines.append("Webhook disabled")

    status = _worst(statuses)
    if webhook.enabled and not webhook.url.strip():
        summary = "webhook is enabled but delivery.webhook.url is empty"
    elif webhook.enabled and status == FAIL:
        summary = "an enabled channel is not usable as configured"
    elif webhook.enabled and not feishu.enabled:
        summary = "Webhook channel enabled"
    else:
        summary = feishu_summary
    return _check("channels", status, summary, " | ".join(lines))


# ---------------------------------------------------------------- entry point


def run_doctor(cfg: FlywheelConfig) -> dict[str, Any]:
    """Diagnose the configuration without modifying any file, database or setting.

    Returns ``{"ok", "version", "counts", "checks"}``: ``ok`` is true exactly when no
    check reports ``FAIL``. Each check carries ``name``, ``status``, ``summary`` and
    ``detail``.
    """
    checks = [
        _check_config(cfg),
        _check_storage(cfg),
        _check_knowledge_sink(cfg),
        _check_skills(cfg),
        _check_runner(cfg),
        _check_safety_valves(cfg),
        _check_channels(cfg),
    ]
    counts = {status: sum(1 for check in checks if check["status"] == status) for status in (PASS, WARN, FAIL)}
    return {
        "ok": counts[FAIL] == 0,
        "version": __version__,
        "counts": counts,
        "checks": checks,
    }
