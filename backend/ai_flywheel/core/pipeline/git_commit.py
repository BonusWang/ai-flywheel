"""Optional P04 post-write local Git commit — an audit trail for written knowledge.

Inspired by MemFS-style change tracking: when ``knowledge_sink.auto_git_commit`` is
on, the files P04 just wrote and verified are committed locally so a later reader can
see exactly what the flywheel changed, in which batch, and on whose approval.

The contract is deliberately narrow:

- only the exact written paths are staged (``git add -- <paths>``); an unrelated
  modified or untracked file in the same working tree is never touched, and
  ``git add -A`` / ``git add .`` are never used;
- one commit per Git repository, created with ``git commit --only`` so staged
  changes to other paths do not ride along;
- the repository's configured ``user.name`` / ``user.email`` are preserved; a
  neutral fallback identity is used only when Git has no identity at all;
- ``git push`` is never run — this is a local trail, not a publication;
- a missing Git, a non-repository path or a failing commit never fails or rolls
  back a verified P04 write: the caller gets a structured result instead.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

#: Wall-clock limit of one Git invocation: a hung hook must not stall the pipeline.
GIT_TIMEOUT_SECONDS = 15

#: Used only when the repository has no configured identity of its own.
FALLBACK_IDENTITY = ("user.name=AI Flywheel", "user.email=flywheel@local")

#: Skipped-path reasons that mean "this path simply is not in a repository".
_NOT_A_REPO = "not_a_git_repo"
_MISSING_PATH = "missing_path"
_NOTHING_TO_COMMIT = "nothing_to_commit"


def _run_git(git: str, args: list[str]) -> tuple[int, str, str]:
    """Run one Git command and never raise. Returns ``(returncode, stdout, stderr)``.

    ``124`` is this module's timeout code and ``127`` its could-not-start code,
    mirroring the shell conventions.
    """
    try:
        proc = subprocess.run(
            [git, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"git timed out after {GIT_TIMEOUT_SECONDS}s: git {' '.join(args)}"
    except OSError as exc:
        return 127, "", f"git could not be started: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _repo_root(git: str, path: Path) -> Path | None:
    """The working-tree root that contains ``path``, or None when it is outside any repository."""
    code, out, _err = _run_git(git, ["-C", str(path.parent), "rev-parse", "--show-toplevel"])
    if code != 0:
        return None
    text = out.strip()
    if not text:
        return None
    try:
        return Path(text).resolve()
    except OSError:
        return None


def _identity_args(git: str, root: Path) -> list[str]:
    """``-c`` overrides for the identity fields Git has no value for.

    A repository (or the user's global config) that already names an author keeps
    it: the trail should read as the operator's own commit, not a bot's.
    """
    overrides: list[str] = []
    for key, fallback in (("user.name", FALLBACK_IDENTITY[0]), ("user.email", FALLBACK_IDENTITY[1])):
        code, out, _err = _run_git(git, ["-C", str(root), "config", key])
        if code != 0 or not out.strip():
            overrides.extend(["-c", fallback])
    return overrides


def _truncate(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def commit_p04_written_files(
    written_paths: Iterable[Path | str],
    *,
    run_id: str,
    approval_source: str = "portal",
    summary: str = "",
) -> dict[str, Any]:
    """Commit exactly ``written_paths`` locally, one commit per Git repository.

    Returns a JSON-safe result::

        {
          "status": "committed" | "skipped" | "failed",
          "reason": "" | "not_a_git_repo" | "nothing_to_commit" | "commit_failed" | ...,
          "commits": [{"root": ..., "commit": ..., "files": [...]}],
          "skipped_paths": [{"path": ..., "reason": ...}],
          "skipped_repos": [{"root": ..., "reason": "nothing_to_commit"}],
          "error": None | "...",
        }

    Never raises for a Git-side problem: P04's write already passed its gates and
    a failed audit commit must not turn a verified batch into a failed one.
    """
    result: dict[str, Any] = {
        "status": "skipped",
        "reason": "",
        "commits": [],
        "skipped_paths": [],
        "skipped_repos": [],
        "error": None,
    }

    ordered: list[Path] = []
    seen: set[str] = set()
    for raw in written_paths:
        path = Path(raw)
        fingerprint = str(path)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        ordered.append(path)
    if not ordered:
        result["reason"] = _NOTHING_TO_COMMIT
        return result

    git = shutil.which("git")
    if not git:
        result["status"] = "failed"
        result["reason"] = "git_unavailable"
        result["error"] = "the git executable is not on PATH"
        return result

    groups: dict[Path, list[tuple[Path, str]]] = {}
    for path in ordered:
        if not path.exists():
            result["skipped_paths"].append({"path": str(path), "reason": _MISSING_PATH})
            continue
        root = _repo_root(git, path)
        if root is None:
            result["skipped_paths"].append({"path": str(path), "reason": _NOT_A_REPO})
            continue
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except (OSError, ValueError):
            result["skipped_paths"].append({"path": str(path), "reason": _NOT_A_REPO})
            continue
        groups.setdefault(root, []).append((path, relative))

    if not groups:
        reasons = {entry["reason"] for entry in result["skipped_paths"]}
        result["reason"] = _NOT_A_REPO if _NOT_A_REPO in reasons else _MISSING_PATH
        return result

    failures: list[str] = []
    for root, entries in groups.items():
        relative_paths = [relative for _path, relative in entries]
        label = f"{root} ({len(relative_paths)} path(s))"

        code, _out, err = _run_git(git, ["-C", str(root), "add", "--", *relative_paths])
        if code != 0:
            failures.append(f"git add failed for {label}: {_truncate(err)}")
            continue

        # Only the written paths matter: other staged changes in this repository
        # must neither block this commit nor ride along with it.
        code, staged, err = _run_git(
            git, ["-C", str(root), "diff", "--cached", "--name-only", "--", *relative_paths]
        )
        if code != 0:
            failures.append(f"git diff --cached failed for {label}: {_truncate(err)}")
            continue
        if not staged.strip():
            result["skipped_repos"].append({"root": str(root), "reason": _NOTHING_TO_COMMIT})
            continue

        subject = f"flywheel(p04): {run_id} ({len(relative_paths)} file(s))"
        body_lines = [f"run_id: {run_id}", f"approval_source: {approval_source}"]
        if summary:
            body_lines.append(f"summary: {summary}")
        body_lines.append("files:")
        body_lines.extend(f"- {relative}" for relative in relative_paths)
        commit_args = [
            "-C",
            str(root),
            *_identity_args(git, root),
            "commit",
            "--only",
            "-m",
            subject,
            "-m",
            "\n".join(body_lines),
            "--",
            *relative_paths,
        ]
        code, _out, err = _run_git(git, commit_args)
        if code != 0:
            failures.append(f"git commit failed for {label}: {_truncate(err)}")
            continue

        code, head, _err = _run_git(git, ["-C", str(root), "rev-parse", "HEAD"])
        result["commits"].append(
            {
                "root": str(root),
                "commit": head.strip() if code == 0 else "",
                "subject": subject,
                "files": relative_paths,
            }
        )

    if failures:
        result["status"] = "failed"
        result["reason"] = "commit_failed"
        result["error"] = "; ".join(failures)
    elif result["commits"]:
        result["status"] = "committed"
        result["reason"] = ""
    else:
        result["status"] = "skipped"
        result["reason"] = _NOTHING_TO_COMMIT
    return result
