"""lark-cli invocation layer (feat-feishu-approval-card-loop, design D9).

Resolves the native executable (never falls back to a .cmd shim that would let
cmd.exe re-parse our argv), runs send / patch / event-consume with UTF-8 bodies,
and classifies results as sent / failed / unknown. Frozen once per process.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_LARK_CLI_VERSION = (1, 0, 64)

_LARK_ENV = {
    "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
    "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
}


class LarkCliUnavailableError(RuntimeError):
    """The configured lark-cli command cannot be resolved to a safe invocation."""


@dataclass
class LarkResult:
    status: str  # sent | failed | unknown
    message_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


def _path_entries() -> list[Path]:
    out: list[Path] = []
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        raw = entry.strip().strip('"')
        if raw:
            out.append(Path(raw))
    return out


def _resolve_on_path(name: str, exts: list[str]) -> Path | None:
    if Path(name).suffix and any(name.lower().endswith(e) for e in exts):
        exts = [""]
    for folder in _path_entries():
        for ext in exts:
            cand = folder / (name + ext)
            if cand.is_file():
                return cand
    return None


def _windows_ext_order() -> list[str]:
    listed = [e.lower() for e in (os.environ.get("PATHEXT") or "").split(";") if e]
    base = [".exe", ".com"]
    return [e for e in listed if e in base] + [e for e in base if e not in listed]


class LarkCliRunner:
    """Thin, testable wrapper around one resolved lark-cli invocation target."""

    def __init__(
        self,
        command: str = "lark-cli",
        command_args: list[str] | None = None,
        identity: str = "bot",
    ) -> None:
        self._prefix, self._kind = self.resolve(command)
        self._extra_args = list(command_args or [])
        # The identity the command runs as ("bot" or "user"); it is an argument
        # of every call, so it belongs to the runner and not to each call site.
        self._identity = identity or "bot"

    # ---------------- resolution ----------------

    @staticmethod
    def resolve(command: str) -> tuple[list[str], str]:
        """Resolve to (argv prefix, kind). Kinds: exe | node. Never returns .cmd.

        Steps (design D9): (1) the command itself is an executable file;
        (2) a .cmd shim points at a package entry — use the native exe next to it;
        (3) else node + the entry script. No fourth step: no shim fallback.
        """
        raw = Path(os.path.expanduser(command))
        if raw.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            return LarkCliRunner._from_shim(raw)
        if os.path.sep in command or raw.is_absolute():
            if raw.is_file():
                return ([str(raw)], "exe")
            raise LarkCliUnavailableError(f"configured lark-cli command does not exist: {command}")
        exts = _windows_ext_order() if os.name == "nt" else [""]
        direct = _resolve_on_path(command, exts)
        if direct is not None:
            if direct.suffix.lower() in {".cmd", ".bat", ".ps1"}:
                return LarkCliRunner._from_shim(direct)
            return ([str(direct)], "exe")
        shim = _resolve_on_path(command, [".cmd", ".bat", ".ps1"] if os.name == "nt" else [""])
        if shim is not None:
            if shim.suffix.lower() in {".cmd", ".bat", ".ps1"}:
                return LarkCliRunner._from_shim(shim)
            return ([str(shim)], "exe")
        raise LarkCliUnavailableError(
            f"cannot resolve lark-cli to a native executable or node entry (PATH searched without the current directory): {command}"
        )

    @staticmethod
    def _from_shim(shim: Path) -> tuple[list[str], str]:
        try:
            text = shim.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LarkCliUnavailableError(f"cannot read shim {shim}: {exc}") from exc
        # The npm shim's final invocation references its package entry under
        # node_modules; other quoted %dp0% paths (e.g. an `IF EXIST "%dp0%\node.exe"`
        # probe line) must not be mistaken for the entry.
        m = re.search(r'%~?dp0%?\\?((?:[^"\r\n]*node_modules[^"\r\n]+?)\.[cm]?js)"', text, re.I)
        if not m:
            raise LarkCliUnavailableError(f"cannot extract the JS entry from shim {shim}; refusing to run the shim itself")
        entry_rel = m.group(1).replace("/", "\\").lstrip("%")
        entry = (shim.parent / entry_rel).resolve()
        shim_dir = shim.parent.resolve()
        try:
            entry.relative_to(shim_dir)
        except ValueError:
            raise LarkCliUnavailableError(f"shim entry escapes the shim directory: {entry}") from None
        if entry.suffix.lower() == ".exe":
            return ([str(entry)], "exe")
        native = entry.parent.parent / "bin" / "lark-cli.exe"
        if native.is_file():
            return ([str(native)], "exe")
        node = _resolve_on_path("node", [".exe", ""] if os.name == "nt" else [""])
        if node is not None:
            return ([str(node), str(entry)], "node")
        raise LarkCliUnavailableError(
            f"no native lark-cli.exe next to the shim entry and no node on PATH (shim {shim})"
        )

    # ---------------- properties / subprocess plumbing ----------------

    @property
    def argv_prefix(self) -> list[str]:
        return list(self._prefix)

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def target_path(self) -> str:
        return self._prefix[0]

    def _full_argv(self, args: list[str]) -> list[str]:
        return [*self._prefix, *self._extra_args, *args]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(_LARK_ENV)
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        timeout: float = 20.0,
    ) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                self._full_argv(args),
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                env=self._env(),
            )
        except subprocess.TimeoutExpired:
            return -1, "", "timeout"
        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
        return proc.returncode, out, err

    def popen(self, args: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            self._full_argv(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    # ---------------- high level operations ----------------

    def version(self) -> str:
        code, out, err = self.run(["--version"], timeout=15)
        if code != 0:
            raise LarkCliUnavailableError(f"lark-cli --version failed: {(err or out).strip()[:200]}")
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
        if not m:
            raise LarkCliUnavailableError(f"cannot parse lark-cli version from: {out.strip()[:120]}")
        return m.group(0)

    def check_version(self) -> None:
        ver = self.version()
        parts = tuple(int(x) for x in ver.split("."))
        if parts < MIN_LARK_CLI_VERSION:
            raise LarkCliUnavailableError(
                f"lark-cli {ver} is below the required {MIN_LARK_CLI_VERSION[0]}.{MIN_LARK_CLI_VERSION[1]}.{MIN_LARK_CLI_VERSION[2]}"
            )

    def auth_bot_ready(self) -> bool:
        code, out, _ = self.run(["auth", "status", "--json"], timeout=15)
        if code != 0:
            return False
        try:
            data = json.loads(out)
        except ValueError:
            return False
        identities = data.get("identities") or {}
        bot = identities.get("bot") or {}
        return bool(bot.get("status") == "ready")

    def _parse_envelope(self, code: int, out: str, err: str) -> LarkResult:
        if code == -1:
            return LarkResult("unknown", error="timeout")
        if code != 0:
            return LarkResult("failed", error=(err or out).strip()[:300] or f"exit {code}")
        try:
            data = json.loads(out)
        except ValueError:
            return LarkResult("failed", error=f"unparseable stdout: {out[:200]}")
        if data.get("ok") is False or data.get("error"):
            err_obj = data.get("error")
            return LarkResult("failed", error=json.dumps(err_obj, ensure_ascii=False)[:300] if err_obj else "error envelope")
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        message_id = payload.get("message_id") if isinstance(payload, dict) else None
        return LarkResult("sent", message_id=message_id, raw=data)

    def send_text(self, *, user_id: str = "", chat_id: str = "", text: str, idempotency_key: str = "") -> LarkResult:
        if not user_id and not chat_id:
            return LarkResult("failed", error="no user_id or chat_id configured")
        args = ["im", "+messages-send", "--as", self._identity]
        args += ["--user-id", user_id] if user_id else ["--chat-id", chat_id]
        args += ["--msg-type", "text", "--content", json.dumps({"text": text}, ensure_ascii=False), "--format", "json"]
        if idempotency_key:
            args += ["--idempotency-key", idempotency_key[:50]]
        code, out, err = self.run(args, timeout=20)
        return self._parse_envelope(code, out, err)

    def send_interactive(self, *, user_id: str, card_json: str, idempotency_key: str) -> LarkResult:
        args = [
            "im", "+messages-send", "--as", self._identity, "--user-id", user_id,
            "--msg-type", "interactive", "--content", card_json,
            "--format", "json",
            "--idempotency-key", idempotency_key[:50],
        ]
        code, out, err = self.run(args, timeout=20)
        return self._parse_envelope(code, out, err)

    def patch_card(self, *, message_id: str, card_json: str) -> LarkResult:
        body = json.dumps({"content": card_json}, ensure_ascii=False).encode("utf-8")
        code, out, err = self.run(
            ["api", "PATCH", f"/open-apis/im/v1/messages/{message_id}", "--as", self._identity, "--data", "-", "--format", "json"],
            input_bytes=body,
            timeout=20,
        )
        return self._parse_envelope(code, out, err)

    def consume_args(self) -> list[str]:
        return ["event", "consume", "card.action.trigger", "--as", self._identity]
