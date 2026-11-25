#!/usr/bin/env python
"""One-shot verification gate for the public AI Flywheel repository.

Four checks, one command, one exit code:

1. **hygiene** — the public-repo rules: no personal identifiers or local paths, no
   UTF-8 BOM in Python sources, no dead-code markers, no production hold/park
   hooks, and no duplicated CHANGELOG heading inside a release section.
2. **openspec** — `openspec validate --all --strict`.
3. **pytest** — the whole test suite, with the interpreter that runs this script.
4. **frontend** — `npm run build` (only with `--frontend`).

Usage::

    python scripts/verify_repo.py            # hygiene + openspec + pytest
    python scripts/verify_repo.py --frontend # ... and the frontend build

The script exits non-zero as soon as any check it ran failed. It never fixes
anything: a failing check is a report, not a repair.

The personal tokens this file forbids are assembled from fragments on purpose:
a guard that spelled them out would trip over itself.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# A source checkout must work without an install: the credential patterns live in the package.
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

# ---------------------------------------------------------------- file surface

#: The file kinds the public-repo rules apply to. SVG is text: a path or a name in a
#: diagram is just as public as one in a Markdown file. SQL is the authoritative DDL.
SCAN_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".md", ".yaml", ".yml", ".example", ".svg", ".sql"})

#: Directories that never hold shipped source.
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".codegraph",
        "data",
        "__pycache__",
        ".pytest_cache",
        "dist",
        ".vite",
        ".mypy_cache",
        ".ruff_cache",
    }
)

#: Files that hold local configuration and are therefore never part of the public surface.
EXCLUDED_FILES = frozenset({"flywheel.yaml"})

#: User names that are obviously placeholders: a test fixture, not a person.
PLACEHOLDER_USERS = (
    "example",
    "examples",
    "someone",
    "someones",
    "test",
    "tests",
    "tmp",
    "temp",
    "user",
    "username",
    "yourname",
    "your-name",
    "your_name",
    "your-username",
    "dev",
    "developer",
    "sample",
    "demo",
    "placeholder",
    "redacted",
    "xxx",
    "name",
    "someoneelse",
)

# The private home repository, the operator nickname and the public owner handle.
# Assembled from fragments so this file does not contain them verbatim.
_PRIVATE_HOME_REPO = "ai" + "-" + "home"
_OPERATOR_NICKNAME = "J" + "T"
_OWNER_HANDLE = "Bonus" + "Wang"

#: The files allowed to name the owner handle: the licence and the clone URL.
OWNER_HANDLE_ALLOWED_FILES = frozenset({"LICENSE", "README.md"})


def _user_pattern(root: str) -> re.Pattern[str]:
    """A home-directory path that names an account which is not a placeholder."""
    allowed = "|".join(re.escape(name) for name in PLACEHOLDER_USERS)
    return re.compile(
        rf"(?<![A-Za-z0-9]){root}[\\/]+(?!{allowed}(?![A-Za-z0-9_-]))[A-Za-z0-9_.-]+",
        re.IGNORECASE,
    )


#: Windows (`C:\Users\name`) and macOS (`/Users/name`) home directories.
PERSONAL_PATH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_user_pattern(r"[A-Za-z]:[\\/]{1}Users"), "a Windows home directory"),
    (re.compile(rf"/Users/(?!{'|'.join(PLACEHOLDER_USERS)}(?![A-Za-z0-9_-]))[A-Za-z0-9_.-]+", re.IGNORECASE), "a macOS home directory"),
    # A local checkout of the private home repository, wherever it sits.
    (re.compile(rf"(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1}[^\s\"'<>]*{re.escape(_PRIVATE_HOME_REPO)}", re.IGNORECASE), "the private home repository"),
    # A local checkout of the private knowledge base.
    (re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1}wiki(?:[\\/]|$)", re.IGNORECASE), "the private knowledge base"),
    # The local `github` checkout folder: the public repo never names a local path.
    (re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1}github[\\/]", re.IGNORECASE), "a local checkout path"),
    # The operator's nickname.
    (re.compile(rf"(?<![A-Za-z0-9]){re.escape(_OPERATOR_NICKNAME)}(?![A-Za-z0-9])"), "the operator nickname"),
)

#: The owner's handle: allowed only where the licence and the clone URL live.
OWNER_HANDLE_RE = re.compile(re.escape(_OWNER_HANDLE))

#: A credential-shaped string that reads as a fixture is not a leak: the tests have to
#: feed the privacy gate something that looks like a key.
CREDENTIAL_PLACEHOLDERS = (
    "placeholder",
    "example",
    "dummy",
    "redacted",
    "notreal",
    "not-a-real",
    "your-",
    "xxxx",
)

#: Dead code markers the public repo must not carry.
UNUSED_METHOD_RE = re.compile(r"^\s*def\s+_unused_\w*", re.MULTILINE)
DEAD_BRANCH_RE = re.compile(r"^[ \t]*if\s+False\s*:", re.MULTILINE)

#: Production hooks that would freeze a real run; they must never ship.
HOLD_HOOK_TOKENS = ("FLYWHEEL_HOLD_AT_NODE", "FLYWHEEL_PARK_AFTER_SEND")

#: The CHANGELOG headings a release section may use.
CHANGELOG_SECTION_HEADINGS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")


# ---------------------------------------------------------------- data model


@dataclass
class Violation:
    """One broken rule: where it is and what it says."""

    file: str
    line: int
    kind: str
    detail: str

    def render(self) -> str:
        return f"{self.file}:{self.line}: [{self.kind}] {self.detail}"


@dataclass
class CheckResult:
    """One verification step and what it had to say."""

    name: str
    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)
    skipped: bool = False

    def render(self) -> str:
        mark = "SKIP" if self.skipped else ("PASS" if self.ok else "FAIL")
        lines = [f"[{mark}] {self.name}: {self.summary}"]
        lines.extend(f"       {detail}" for detail in self.details)
        return "\n".join(lines)


# ---------------------------------------------------------------- file discovery


def _git_files() -> list[Path] | None:
    """Every file git does not ignore, or None when git cannot answer."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run(
            [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    names = [name for name in proc.stdout.split("\0") if name.strip()]
    return [REPO_ROOT / name for name in names]


def _walk_files() -> list[Path]:
    """Every file outside the excluded directories, used when git cannot answer."""
    found: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(REPO_ROOT)
        if any(part in EXCLUDED_DIRS for part in relative.parts[:-1]):
            continue
        if relative.name in EXCLUDED_FILES:
            continue
        found.append(path)
    return found


def repo_files() -> list[Path]:
    """The files the public-repo rules apply to."""
    candidates = _git_files()
    if candidates is None:
        candidates = _walk_files()
    files: list[Path] = []
    for path in candidates:
        if path.name in EXCLUDED_FILES:
            continue
        if path.suffix not in SCAN_SUFFIXES:
            continue
        if not path.is_file():
            continue
        files.append(path)
    return sorted(files)


def _relative(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- the hygiene check


def _credential_patterns() -> list[tuple[re.Pattern[str], str]]:
    """The credential shapes the product itself knows, straight from the privacy gate."""
    try:
        from ai_flywheel.core.privacy import SECRET_PATTERNS
    except Exception:  # pragma: no cover - the package is optional for this script
        return []
    return [(secret.pattern, secret.label) for secret in SECRET_PATTERNS if secret.credential]


def hygiene_violations() -> list[Violation]:
    """Every public-repo rule the current tree breaks."""
    violations: list[Violation] = []
    credentials = _credential_patterns()

    for path in repo_files():
        relative = _relative(path)
        text = _read(path)

        for pattern, label in PERSONAL_PATH_PATTERNS:
            for match in pattern.finditer(text):
                violations.append(
                    Violation(relative, text[: match.start()].count("\n") + 1, "personal-path", f"{label}: {match.group(0)!r}")
                )

        if relative not in OWNER_HANDLE_ALLOWED_FILES:
            for match in OWNER_HANDLE_RE.finditer(text):
                violations.append(
                    Violation(relative, text[: match.start()].count("\n") + 1, "personal-identity", "the owner handle")
                )

        for pattern, label in credentials:
            for match in pattern.finditer(text):
                matched = match.group(0).lower()
                if any(marker in matched for marker in CREDENTIAL_PLACEHOLDERS):
                    continue
                violations.append(
                    Violation(relative, text[: match.start()].count("\n") + 1, "credential", f"{label}")
                )

        if path.suffix == ".py":
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                violations.append(Violation(relative, 1, "encoding", "a UTF-8 BOM"))

        for match in UNUSED_METHOD_RE.finditer(text):
            violations.append(
                Violation(relative, text[: match.start()].count("\n") + 1, "dead-code", "an unused method")
            )

        for match in DEAD_BRANCH_RE.finditer(text):
            violations.append(
                Violation(relative, text[: match.start()].count("\n") + 1, "dead-code", "a dead branch")
            )

        if relative.startswith("backend/"):
            for token in HOLD_HOOK_TOKENS:
                for match in re.finditer(re.escape(token), text):
                    violations.append(
                        Violation(relative, text[: match.start()].count("\n") + 1, "production-hook", token)
                    )

    return violations


def changelog_violations() -> list[Violation]:
    """Every CHANGELOG heading rule the current file breaks."""
    path = REPO_ROOT / "CHANGELOG.md"
    if not path.is_file():
        return [Violation("CHANGELOG.md", 1, "changelog", "the changelog is missing")]
    violations: list[Violation] = []
    text = _read(path)
    section = ""
    seen: dict[str, list[int]] = {}
    for index, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            section = line[3:].strip()
            seen = {}
            continue
        if not line.startswith("### "):
            continue
        heading = line[4:].strip()
        if heading in seen:
            violations.append(
                Violation(
                    "CHANGELOG.md",
                    index,
                    "changelog",
                    f"the heading {heading!r} already appears at line {seen[heading]} in {section or 'the preamble'}",
                )
            )
        else:
            seen[heading] = index
        if heading not in CHANGELOG_SECTION_HEADINGS:
            violations.append(
                Violation("CHANGELOG.md", index, "changelog", f"the heading {heading!r} is not a Keep-a-Changelog heading")
            )
    return violations


def check_hygiene() -> CheckResult:
    """The public-repo rules and the CHANGELOG heading rules."""
    violations = hygiene_violations() + changelog_violations()
    details = [violation.render() for violation in violations]
    if violations:
        return CheckResult("hygiene", False, f"{len(violations)} violation(s)", details)
    return CheckResult("hygiene", True, f"{len(repo_files())} file(s) clean")


# ---------------------------------------------------------------- the external checks


def _run(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        shell=True,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def check_openspec() -> CheckResult:
    """`openspec validate --all --strict`."""
    if not shutil.which("openspec"):
        return CheckResult("openspec", False, "the openspec CLI is not on PATH", ["install it with `npm i -g openspec`"])
    proc = _run("openspec validate --all --strict", REPO_ROOT)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return CheckResult("openspec", True, output.strip().splitlines()[-1] if output.strip() else "the specs are valid")
    return CheckResult("openspec", False, f"exit code {proc.returncode}", output.strip().splitlines()[-25:])


def check_pytest() -> CheckResult:
    """The whole test suite, with the interpreter that runs this script."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = [line for line in output.strip().splitlines() if line.strip()][-25:]
    if proc.returncode == 0:
        summary = next((line for line in reversed(output.splitlines()) if "passed" in line), "all tests passed")
        return CheckResult("pytest", True, summary.strip())
    return CheckResult("pytest", False, f"exit code {proc.returncode}", tail)


def check_frontend() -> CheckResult:
    """`npm run build` in the frontend."""
    if not shutil.which("npm"):
        return CheckResult("frontend", False, "npm is not on PATH", [])
    proc = _run("npm run build", REPO_ROOT / "frontend")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode == 0:
        tail = [line for line in output.splitlines() if line.strip()][-3:]
        return CheckResult("frontend", True, "the frontend built", tail)
    return CheckResult("frontend", False, f"exit code {proc.returncode}", output.splitlines()[-25:])


# ---------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the AI Flywheel repository in one shot.")
    parser.add_argument("--frontend", action="store_true", help="also run the frontend build")
    args = parser.parse_args(argv)

    results = [check_hygiene(), check_openspec(), check_pytest()]
    if args.frontend:
        results.append(check_frontend())

    print("AI Flywheel · repository verification")
    print("=" * 60)
    for result in results:
        print(result.render())
    print("=" * 60)

    failed = [result for result in results if not result.ok and not result.skipped]
    if failed:
        print(f"FAILED: {len(failed)} check(s) did not pass: {', '.join(result.name for result in failed)}")
        return 1
    print(f"OK: {len(results)} check(s) passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
