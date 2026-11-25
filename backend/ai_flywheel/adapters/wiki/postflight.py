"""What happens after the document was written.

Two hooks of the mounted contract may judge the result: the topic lint (a topic card only)
and the post-write hook (the contract's own index/validator sync, configured through the
neutral ``postflight_hook_script`` name or the legacy ``wiki_d2_postflight_script`` alias).
Both are optional; when the sink requires them, a missing script is an error rather than a
silent skip.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_flywheel.adapters.wiki.validator import WikiGateResult
from ai_flywheel.config import KnowledgeSinkConfig
from ai_flywheel.core.time_utils import shanghai_today_ymd

# Git Bash is the bash this adapter's D2 scripts are written for. On Windows a bare
# "bash" often resolves to the WSL launcher instead, which cannot run them.
_GIT_BASH_FALLBACKS = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
)
_WSL_BASH_MARKERS = ("windowsapps\\bash.exe", "\\system32\\bash.exe")


def _git_bash_candidates() -> list[str]:
    """Git Bash executables to try, most specific first: next to git.exe, then the
    standard install locations."""
    candidates: list[str] = []
    git = shutil.which("git")
    if git:
        git_root = Path(git).parent.parent
        candidates.append(str(git_root / "usr" / "bin" / "bash.exe"))
        candidates.append(str(git_root / "bin" / "bash.exe"))
    candidates.extend(_GIT_BASH_FALLBACKS)
    return candidates


def resolve_bash_executable(configured: str) -> str:
    """Resolve the bash that runs the D2 postflight script.

    On Windows a configured "bash" can resolve to the WSL launcher, which cannot run
    these Git-flavoured scripts; Git Bash is preferred instead. Any other configured
    command is used verbatim.
    """
    command = (configured or "").strip() or "bash"
    if os.name != "nt":
        return command
    if Path(command).name.lower() not in {"bash", "bash.exe"}:
        return command
    found = shutil.which(command)
    if not found:
        return command
    lowered = found.replace("/", "\\").lower()
    if not any(marker in lowered for marker in _WSL_BASH_MARKERS):
        return found
    for candidate in _git_bash_candidates():
        if candidate and Path(candidate).is_file():
            return candidate
    return found


def work_root_env(sink: KnowledgeSinkConfig, work_root: Path) -> dict[str, str]:
    """The work root the D2 script runs in.

    A neutral variable name is always exported; a deployment whose script expects a
    different name sets ``knowledge_sink.wiki_d2_work_root_env`` (or the
    ``FLYWHEEL_D2_WORK_ROOT_ENV`` environment variable) and gets it exported too.
    """
    root = str(work_root)
    env = {"FLYWHEEL_WORK_ROOT": root}
    name = (
        sink.wiki_d2_work_root_env
        or os.environ.get("FLYWHEEL_D2_WORK_ROOT_ENV", "")
        or ""
    ).strip()
    if name:
        env[name] = root
    return env


def run_topic_lint(result: WikiGateResult, relative_path: str, *, sink: KnowledgeSinkConfig, root: Path) -> None:
    """Judge a written topic card with the Wiki's own lint script."""
    if result.page_type != "topic":
        result.topic_lint = {"status": "skipped", "reason": "not_a_topic_card"}
        return
    script = sink.wiki_lint_script.strip()
    if not script:
        if sink.wiki_gate_require_scripts:
            result.error("LINT_SCRIPT_MISSING", "wiki_lint_script is not configured for topic cards.")
        else:
            result.topic_lint = {"status": "skipped", "reason": "lint_script_not_configured"}
        return
    target = root / relative_path
    python = sink.wiki_python.strip() or sys.executable
    command = [python, script, str(root), "--topic-card", str(target), "--change-state", "touched"]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=sink.wiki_gate_timeout_seconds,
            cwd=str(root),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.error("LINT_FAILED", f"wiki-lint could not run: {exc}")
        result.topic_lint = {"status": "error", "error": str(exc)}
        return
    payload = _last_json_line(proc.stdout) or {}
    result.topic_lint = {
        "status": "pass" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "report": payload,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-1000:],
    }
    if proc.returncode != 0:
        result.error(
            "LINT_FAILED",
            f"wiki-lint rejected the topic card (rc={proc.returncode}).",
        )


def run_postflight(
    result: WikiGateResult,
    relative_path: str,
    *,
    sink: KnowledgeSinkConfig,
    root: Path,
    evidence_dir: Path,
) -> None:
    """Run the contract's D2 postflight script over the written document."""
    script = sink.effective_postflight_script
    if not script:
        if sink.wiki_gate_require_scripts:
            result.error(
                "D2_SCRIPT_MISSING",
                "postflight_hook_script (or the legacy wiki_d2_postflight_script) is not configured.",
            )
        else:
            result.postflight = {"status": "skipped", "reason": "d2_script_not_configured"}
        return
    evidence_dir.mkdir(parents=True, exist_ok=True)
    json_out = evidence_dir / (
        "d2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
    )
    python = sink.wiki_python.strip() or sys.executable
    command = [
        resolve_bash_executable(sink.wiki_bash),
        script,
        "--wiki-root",
        str(root),
        "--write-set",
        relative_path,
        "--today",
        shanghai_today_ymd(),
        "--json-out",
        str(json_out),
    ]
    env = os.environ.copy()
    env["PYTHON_BIN"] = python
    env.update(work_root_env(sink, evidence_dir / "d2-work"))
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=sink.wiki_gate_timeout_seconds,
            cwd=str(root),
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.error("D2_FAILED", f"wiki_d2_postflight.sh could not run: {exc}")
        result.postflight = {"status": "error", "error": str(exc)}
        return
    diagnostic = _last_json_line(proc.stdout) or {}
    if not diagnostic and json_out.is_file():
        try:
            diagnostic = json.loads(json_out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            diagnostic = {}
    status = "pass" if proc.returncode == 0 else ("blocked" if proc.returncode == 3 else "fail")
    result.postflight = {
        "status": status,
        "returncode": proc.returncode,
        "diagnostic": diagnostic,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-2000:],
        "evidence": str(json_out),
    }
    if proc.returncode != 0:
        detail = ""
        validator = diagnostic.get("validator") or {}
        if validator:
            detail = f" validator errors={validator.get('errors')} codes={validator.get('issue_codes')}"
        result.error(
            "D2_BLOCKED" if proc.returncode == 3 else "D2_FAILED",
            f"D2 postflight did not pass (rc={proc.returncode}).{detail}",
        )


def resync_after_rollback(
    relative_path: str,
    *,
    sink: KnowledgeSinkConfig,
    root: Path,
    evidence_dir: Path,
) -> dict[str, Any]:
    """Re-run index sync only after a failed write has been rolled back."""
    script = sink.effective_postflight_script
    if not script:
        return {"status": "skipped", "reason": "d2_script_not_configured"}
    python = sink.wiki_python.strip() or sys.executable
    command = [
        resolve_bash_executable(sink.wiki_bash),
        script,
        "--wiki-root",
        str(root),
        "--write-set",
        relative_path,
        "--today",
        shanghai_today_ymd(),
        "--skip-validator",
    ]
    env = os.environ.copy()
    env["PYTHON_BIN"] = python
    env.update(work_root_env(sink, evidence_dir / "d2-work"))
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=sink.wiki_gate_timeout_seconds,
            cwd=str(root),
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "error": str(exc)}
    return {
        "status": "pass" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-1000:],
    }


def _last_json_line(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}
