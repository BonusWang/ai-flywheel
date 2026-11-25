"""`flywheel init-client`: render or safely merge one MCP client configuration.

Two rules shape this module:

- **Dry-run by default.** Without ``--apply`` nothing on disk is touched — not the
  target file, not its parent directory. The command renders the exact entry and a
  merge summary (which existing servers and top-level keys would be preserved).
- **Merge, never overwrite.** Only the ``ai-flywheel`` MCP server entry is written.
  Every other key, server and section is preserved; the result is re-parsed before it
  replaces the file; an existing file that cannot be parsed fails closed, untouched.
  A backup of the pre-image is taken before a real modification, a temporary file is
  swapped in atomically with LF line endings, and a second ``--apply`` changes nothing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_flywheel.config import FlywheelConfig

SERVER_KEY = "ai-flywheel"

FORMAT_JSON = "json"
FORMAT_TOML = "toml"

TRANSPORT_HTTP = "http"
TRANSPORT_STDIO = "stdio"


class OnboardingError(RuntimeError):
    """A client configuration cannot be rendered or merged safely."""


@dataclass(frozen=True)
class ClientTarget:
    """Where a supported client keeps its MCP configuration, and how it is encoded."""

    client_id: str
    label: str
    format: str
    default_transport: str
    transports: tuple[str, ...]


CLIENT_TARGETS: dict[str, ClientTarget] = {
    "claude-code": ClientTarget(
        client_id="claude-code",
        label="Claude Code",
        format=FORMAT_JSON,
        default_transport=TRANSPORT_HTTP,
        transports=(TRANSPORT_HTTP, TRANSPORT_STDIO),
    ),
    "codex": ClientTarget(
        client_id="codex",
        label="Codex",
        format=FORMAT_TOML,
        default_transport=TRANSPORT_HTTP,
        transports=(TRANSPORT_HTTP, TRANSPORT_STDIO),
    ),
    "claude-desktop": ClientTarget(
        client_id="claude-desktop",
        label="Claude Desktop",
        format=FORMAT_JSON,
        default_transport=TRANSPORT_STDIO,
        # Claude Desktop launches stdio servers only.
        transports=(TRANSPORT_STDIO,),
    ),
    "antigravity": ClientTarget(
        client_id="antigravity",
        label="Antigravity",
        format=FORMAT_JSON,
        default_transport=TRANSPORT_HTTP,
        transports=(TRANSPORT_HTTP, TRANSPORT_STDIO),
    ),
}

SUPPORTED_CLIENTS: tuple[str, ...] = tuple(CLIENT_TARGETS)


def default_target_path(client: str) -> Path:
    """The conventional configuration file of `client` on this platform.

    These are conventions, not contracts: `--target` always wins, and the command
    never writes without `--apply`.
    """
    if client == "claude-code":
        return Path.home() / ".claude.json"
    if client == "codex":
        return Path.home() / ".codex" / "config.toml"
    if client == "claude-desktop":
        if os.name == "nt":
            appdata = os.environ.get("APPDATA", "").strip()
            base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
            return base / "Claude" / "claude_desktop_config.json"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
    if client == "antigravity":
        return Path.home() / ".gemini" / "antigravity" / "mcp_config.json"
    raise OnboardingError(f"unsupported client '{client}' (expected one of {', '.join(SUPPORTED_CLIENTS)})")


def build_mcp_entry(cfg: FlywheelConfig, client: str, transport: str) -> dict[str, Any]:
    """The `ai-flywheel` MCP entry for one client and transport."""
    target = CLIENT_TARGETS.get(client)
    if target is None:
        raise OnboardingError(f"unsupported client '{client}' (expected one of {', '.join(SUPPORTED_CLIENTS)})")
    if transport not in target.transports:
        raise OnboardingError(
            f"{client} does not support the {transport} transport "
            f"(supported: {', '.join(target.transports)})"
        )
    if transport == TRANSPORT_STDIO:
        return {"command": "flywheel", "args": ["mcp-stdio"]}
    url = f"http://{cfg.server.host}:{cfg.server.port}/mcp"
    if client == "antigravity":
        return {"serverUrl": url}
    if client == "codex":
        return {"url": url}
    return {"type": "http", "url": url}


# ---------------------------------------------------------------- JSON merge

def _merge_json(existing_text: str, entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Merge the entry into ``mcpServers[SERVER_KEY]`` of an existing JSON document."""
    if existing_text.strip():
        try:
            data = json.loads(existing_text)
        except json.JSONDecodeError as exc:
            raise OnboardingError(
                f"existing file is not valid JSON ({exc.msg} at line {exc.lineno}); nothing was written"
            ) from None
    else:
        data = {}
    if not isinstance(data, dict):
        raise OnboardingError("existing file is not a JSON object; nothing was written")
    servers = data.get("mcpServers", {})
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise OnboardingError('existing "mcpServers" is not a JSON object; nothing was written')
    merged_servers = dict(servers)
    merged_servers[SERVER_KEY] = entry
    merged = {**data, "mcpServers": merged_servers}
    text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"

    # Re-validate: the text must parse, our entry must be there, and every other
    # key and server must be exactly what it was.
    try:
        reparsed = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - json.dumps output always parses
        raise OnboardingError(f"merged JSON does not parse back ({exc.msg}); nothing was written") from None
    if reparsed.get("mcpServers", {}).get(SERVER_KEY) != entry:
        raise OnboardingError("merged JSON lost the ai-flywheel entry; nothing was written")
    before_rest = {key: value for key, value in data.items() if key != "mcpServers"}
    after_rest = {key: value for key, value in reparsed.items() if key != "mcpServers"}
    if before_rest != after_rest:
        raise OnboardingError("merge changed top-level keys it must not touch; nothing was written")
    if {k: v for k, v in servers.items() if k != SERVER_KEY} != {
        k: v for k, v in reparsed["mcpServers"].items() if k != SERVER_KEY
    }:
        raise OnboardingError("merge changed sibling MCP servers it must not touch; nothing was written")
    summary = {
        "target_exists": bool(existing_text.strip()),
        "existing_servers": sorted(servers),
        "preserved_top_level_keys": sorted(key for key in data if key != "mcpServers"),
        "replaces_existing_entry": SERVER_KEY in servers,
    }
    return text, summary


# ---------------------------------------------------------------- TOML merge

_TOML_HEADER_RE = re.compile(
    r"""^\[[ \t]*mcp_servers[ \t]*\.[ \t]*(?:"ai-flywheel"|'ai-flywheel'|ai-flywheel)[ \t]*\][ \t]*(?:#.*)?$"""
)


def _toml_value(value: Any) -> str:
    """A TOML basic string or a string array; our entries are flat by construction."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in value) + "]"
    raise OnboardingError(f"cannot render a TOML value for {value!r}")


def _render_toml_section(entry: dict[str, Any]) -> list[str]:
    lines = [f"[mcp_servers.{SERVER_KEY}]"]
    lines.extend(f"{key} = {_toml_value(value)}" for key, value in entry.items())
    return lines


def _merge_toml(existing_text: str, entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Merge the entry into ``[mcp_servers.ai-flywheel]`` of an existing TOML document.

    The section is replaced in place (quoted or bare spelling alike) or appended at
    the end; every other line is preserved. The result is parsed with ``tomllib`` and
    compared against the pre-merge document before it may replace the file.
    """
    normalized = existing_text.replace("\r\n", "\n")
    if normalized.strip():
        try:
            before = tomllib.loads(normalized)
        except tomllib.TOMLDecodeError as exc:
            raise OnboardingError(
                f"existing file is not valid TOML ({exc}); nothing was written"
            ) from None
    else:
        before = {}
    if not isinstance(before, dict):
        raise OnboardingError("existing file is not a TOML document; nothing was written")
    before_servers_table = before.get("mcp_servers", {})
    if not isinstance(before_servers_table, dict):
        raise OnboardingError('existing "mcp_servers" is not a table; nothing was written')

    section_lines = _render_toml_section(entry)
    lines = normalized.split("\n")
    start = next((index for index, line in enumerate(lines) if _TOML_HEADER_RE.match(line)), None)
    if start is None:
        body = normalized.rstrip("\n")
        text = (body + "\n\n" if body else "") + "\n".join(section_lines) + "\n"
    else:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if lines[index].strip().startswith("["):
                end = index
                break
        new_lines = lines[:start] + section_lines
        tail = lines[end:]
        if tail:
            new_lines += [""] + tail
        text = "\n".join(new_lines)
        if not text.endswith("\n"):
            text += "\n"

    try:
        after = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise OnboardingError(
            f"merged TOML does not parse back ({exc}); nothing was written. "
            "If the existing file declares mcp_servers.ai-flywheel inline, edit it by hand."
        ) from None
    after_entry = after.get("mcp_servers", {}).get(SERVER_KEY)
    if not isinstance(after_entry, dict) or any(after_entry.get(key) != value for key, value in entry.items()):
        raise OnboardingError("merged TOML lost the ai-flywheel entry; nothing was written")
    before_rest = {key: value for key, value in before.items() if key != "mcp_servers"}
    after_rest = {key: value for key, value in after.items() if key != "mcp_servers"}
    if before_rest != after_rest:
        raise OnboardingError("merge changed top-level sections it must not touch; nothing was written")
    before_servers = {k: v for k, v in before_servers_table.items() if k != SERVER_KEY}
    after_servers = {k: v for k, v in after.get("mcp_servers", {}).items() if k != SERVER_KEY}
    if before_servers != after_servers:
        raise OnboardingError("merge changed sibling MCP servers it must not touch; nothing was written")
    summary = {
        "target_exists": bool(normalized.strip()),
        "existing_servers": sorted(before_servers_table),
        "preserved_top_level_keys": sorted(key for key in before if key != "mcp_servers"),
        "replaces_existing_entry": SERVER_KEY in before_servers_table,
    }
    return text, summary


# ---------------------------------------------------------------- plan / apply

def _read_text(target: Path) -> str:
    """The existing file as text; a BOM is tolerated on read and never written back."""
    return target.read_bytes().decode("utf-8-sig")


def _merge(client_format: str, existing_text: str, entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if client_format == FORMAT_JSON:
        return _merge_json(existing_text, entry)
    if client_format == FORMAT_TOML:
        return _merge_toml(existing_text, entry)
    raise OnboardingError(f"unsupported config format '{client_format}'")


def _resolve(client: str, transport: str | None, target: str | Path | None) -> tuple[ClientTarget, str, Path]:
    spec = CLIENT_TARGETS.get(client)
    if spec is None:
        raise OnboardingError(f"unsupported client '{client}' (expected one of {', '.join(SUPPORTED_CLIENTS)})")
    chosen = (transport or spec.default_transport).strip().lower()
    if chosen not in spec.transports:
        raise OnboardingError(
            f"{client} does not support the {chosen} transport (supported: {', '.join(spec.transports)})"
        )
    path = Path(target).expanduser() if target else default_target_path(client)
    return spec, chosen, path


def render_entry_preview(client_format: str, entry: dict[str, Any]) -> str:
    """The `ai-flywheel` entry alone, in the client's own syntax."""
    if client_format == FORMAT_TOML:
        return "\n".join(_render_toml_section(entry)) + "\n"
    return json.dumps({SERVER_KEY: entry}, ensure_ascii=False, indent=2) + "\n"


def _base_result(
    spec: ClientTarget,
    transport: str,
    target: Path,
    entry: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    return {
        "ok": True,
        "client": spec.client_id,
        "label": spec.label,
        "transport": transport,
        "format": spec.format,
        "target": str(target),
        "server_key": SERVER_KEY,
        "mode": mode,
        "action": "",
        "changed": False,
        "entry": entry,
        "entry_preview": render_entry_preview(spec.format, entry),
        "merge": {
            "target_exists": False,
            "existing_servers": [],
            "preserved_top_level_keys": [],
            "replaces_existing_entry": False,
        },
        "backup": None,
        "error": "",
    }


def _error_result(
    client: str,
    transport: str | None,
    target: str | Path | None,
    error: str,
) -> dict[str, Any]:
    return {
        "ok": False,
        "client": client,
        "label": "",
        "transport": transport or "",
        "format": "",
        "target": str(target) if target else "",
        "server_key": SERVER_KEY,
        "mode": "dry-run",
        "action": "",
        "changed": False,
        "entry": {},
        "entry_preview": "",
        "merge": {
            "target_exists": False,
            "existing_servers": [],
            "preserved_top_level_keys": [],
            "replaces_existing_entry": False,
        },
        "backup": None,
        "error": error,
    }


def _prepare(
    cfg: FlywheelConfig,
    client: str,
    transport: str | None,
    target: str | Path | None,
) -> tuple[dict[str, Any], str]:
    """The public result plus the exact text an apply would write (kept out of the result)."""
    try:
        spec, chosen, path = _resolve(client, transport, target)
        entry = build_mcp_entry(cfg, client, chosen)
    except OnboardingError as exc:
        return _error_result(client, transport, target, str(exc)), ""
    result = _base_result(spec, chosen, path, entry, "dry-run")
    exists = path.is_file()
    try:
        existing = _read_text(path) if exists else ""
        text, summary = _merge(spec.format, existing, entry)
    except (OSError, UnicodeDecodeError) as exc:
        result.update(ok=False, error=f"cannot read {path}: {exc}")
        return result, ""
    except OnboardingError as exc:
        result.update(ok=False, error=str(exc))
        return result, ""
    result["merge"] = summary
    result["action"] = "unchanged" if exists and text == existing else ("update" if exists else "create")
    return result, text


def plan_init_client(
    cfg: FlywheelConfig,
    client: str,
    *,
    transport: str | None = None,
    target: str | Path | None = None,
) -> dict[str, Any]:
    """Render the entry and the merge summary, without touching the disk."""
    result, _text = _prepare(cfg, client, transport, target)
    return result


def _backup(target: Path, preimage: bytes) -> Path:
    """Copy the pre-image next to the target; never clobber an existing backup."""
    candidate = target.with_name(target.name + ".bak")
    if candidate.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = target.with_name(f"{target.name}.{stamp}.bak")
        counter = 2
        while candidate.exists():
            candidate = target.with_name(f"{target.name}.{stamp}-{counter}.bak")
            counter += 1
    candidate.write_bytes(preimage)
    return candidate


def _atomic_write(target: Path, text: str) -> None:
    """Write through a temporary file in the same directory, verify the read-back, swap in."""
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        with tmp.open("r", encoding="utf-8", newline="") as handle:
            readback = handle.read()
        if readback != text:
            raise OnboardingError(f"read-back mismatch on {tmp}; the target was not modified")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def apply_init_client(
    cfg: FlywheelConfig,
    client: str,
    *,
    transport: str | None = None,
    target: str | Path | None = None,
) -> dict[str, Any]:
    """Merge the entry into the target file: backup, atomic LF write, idempotent."""
    plan, text = _prepare(cfg, client, transport, target)
    if not plan["ok"]:
        return plan
    plan["mode"] = "apply"
    if plan["action"] == "unchanged":
        return plan
    path = Path(plan["target"])
    try:
        preimage = path.read_bytes() if path.is_file() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        if preimage is not None:
            plan["backup"] = str(_backup(path, preimage))
        _atomic_write(path, text)
    except (OSError, OnboardingError) as exc:
        plan.update(ok=False, error=str(exc))
        return plan
    plan["changed"] = True
    return plan
