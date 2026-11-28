from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.markdown_utils import parse_frontmatter, split_frontmatter
from ai_flywheel.core.privacy import sanitize_and_check_secrets
from ai_flywheel.core.time_utils import shanghai_today_ymd

_SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# A level-1 heading (a level-2 heading does not match: `##` is not followed by a space).
_H1_RE = re.compile(r"^#[ \t]+(?P<title>.+?)[ \t]*#*[ \t]*$")
# A level-2 heading.
_H2_RE = re.compile(r"^##[ \t]+(?P<title>.+?)[ \t]*$")
# The date suffix an appended section carries, e.g. "（2026-11-25 补充）".
_DATE_SUFFIX_RE = re.compile(r"（\d{4}-\d{2}-\d{2}[ \t]*补充）[ \t]*$")


class SkillWriteError(RuntimeError):
    """Raised when a proposed Skill write violates naming, path, or SKILL.md format gates."""


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block. Text without one is returned unchanged."""
    fm_text, body = split_frontmatter((text or "").lstrip())
    return body if fm_text is not None else text


def _strip_leading_h1(text: str) -> str:
    """Drop the document title (a level-1 heading) the model wrote when it produced a whole file."""
    lines = (text or "").splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or not _H1_RE.match(lines[index].strip()):
        return text
    rest = lines[index + 1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    return "\n".join(rest)


def extract_h2_headings(text: str) -> list[str]:
    """The level-2 headings of a markdown body, without the `##` marker.

    Headings inside fenced code blocks are not headings and are skipped.
    """
    headings: list[str] = []
    fenced = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("```") or line.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _H2_RE.match(line)
        if match:
            headings.append(match.group("title").strip())
    return headings


def normalize_update_addition(raw_addition: str, *, fallback_heading: str, today_ymd: str) -> str:
    """Turn whatever the model wrote for an update into the one Markdown section to append.

    A model that still writes a whole file gets its YAML frontmatter and its level-1 title
    removed; the body is then guaranteed to start with a level-2 heading, dated once. The result
    is idempotent: normalizing an already normalized addition returns the very same text.
    """
    text = (raw_addition or "").strip()
    if not text:
        return ""
    text = _strip_frontmatter(text).strip()
    text = _strip_leading_h1(text).strip()
    if not text:
        return ""
    heading = (fallback_heading or "").strip() or "补充说明"
    date_suffix = f"（{today_ymd} 补充）"
    lines = text.splitlines()
    first = next((line for line in lines if line.strip()), "")
    match = _H2_RE.match(first.strip())
    if not match:
        # No level-2 heading: the whole body becomes one appended section.
        return f"## {heading}{date_suffix}\n\n{text}"
    title = match.group("title").strip()
    if not _DATE_SUFFIX_RE.search(title):
        title = f"{title}{date_suffix}"
    lines[lines.index(first)] = f"## {title}"
    return "\n".join(lines).strip()


def _resolve_skill_file(skills_root: Path, skill_name: str) -> Path | None:
    """The `SKILL.md` of `skill_name` inside `skills_root`, or None when there is none.

    The path never leaves `skills_root`. A Skill whose frontmatter name differs from its
    folder name is found by reading the frontmatter of the folders in the root.
    """
    direct = (skills_root / skill_name).resolve()
    if direct.parent == skills_root:
        candidate = direct / "SKILL.md"
        if candidate.is_file():
            return candidate
    try:
        folders = sorted(path for path in skills_root.iterdir() if path.is_dir())
    except OSError:
        return None
    for folder in folders:
        skill_md = folder / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if str(parse_frontmatter(text)[0].get("name") or "").strip() == skill_name:
            return skill_md
    return None


def validate_skill_markdown(skill_name: str, content: str) -> dict[str, str]:
    """Check that a `SKILL.md` has a usable frontmatter and an actionable body.

    Raises `SkillWriteError` naming what is wrong; returns the frontmatter's name and description.
    """
    if not _SKILL_SLUG_RE.match(skill_name):
        raise SkillWriteError(
            f"Invalid skill folder name '{skill_name}': must be lowercase alphanumeric + hyphens (2-63 chars)."
        )
    text = (content or "").removeprefix("\ufeff").strip()
    first_line = text.splitlines()[0].strip() if text else ""
    if first_line != "---":
        raise SkillWriteError(f"Skill '{skill_name}' SKILL.md must start with YAML frontmatter (---).")
    fm_text, raw_body = split_frontmatter(text)
    if fm_text is None:
        raise SkillWriteError(f"Skill '{skill_name}' SKILL.md has unclosed YAML frontmatter.")
    try:
        meta = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise SkillWriteError(f"Skill '{skill_name}' frontmatter YAML parse error: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillWriteError(f"Skill '{skill_name}' frontmatter must be a YAML mapping.")
    name = str(meta.get("name") or "").strip()
    desc = str(meta.get("description") or "").strip()
    body = raw_body.strip()
    if not name:
        raise SkillWriteError(f"Skill '{skill_name}' frontmatter missing required 'name'.")
    if not desc:
        raise SkillWriteError(f"Skill '{skill_name}' frontmatter missing required 'description'.")
    if len(body) < 20:
        raise SkillWriteError(f"Skill '{skill_name}' SKILL.md body is too short to be actionable.")
    return {"name": name, "description": desc}


def inspect_skill_at(root: Path, skill_name: str) -> dict[str, Any]:
    """Read-only inspection of an existing Skill: what is there, and is it a valid SKILL.md?

    Never writes and never raises: an unusable target is reported through `valid` and `error`.
    """
    skills_root = Path(root).resolve()
    name = (skill_name or "").strip()
    info: dict[str, Any] = {
        "exists": False,
        "skill_name": name,
        "valid": False,
        "error": None,
        "line_count": 0,
        "headings": [],
        "eol": "\n",
        "content": "",
    }
    if not _SKILL_SLUG_RE.match(name):
        info["error"] = (
            f"Invalid skill folder name '{name}': must be lowercase alphanumeric + hyphens (2-63 chars)."
        )
        return info
    target = _resolve_skill_file(skills_root, name)
    if target is None:
        info["error"] = f"Skill '{name}' has no SKILL.md"
        return info
    raw = target.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    info["exists"] = True
    info["skill_name"] = target.parent.name
    info["eol"] = "\r\n" if b"\r\n" in raw else "\n"
    info["content"] = text
    info["line_count"] = len(text.splitlines())
    info["headings"] = extract_h2_headings(text)
    try:
        validate_skill_markdown(info["skill_name"], text)
    except SkillWriteError as exc:
        info["error"] = str(exc)
        return info
    info["valid"] = True
    return info


class SkillWriterAdapter:
    """Controlled Skill creation & improvement writer with frontmatter validation and backup."""

    def __init__(self, cfg: FlywheelConfig) -> None:
        self.cfg = cfg

    @property
    def root(self) -> Path:
        return self.cfg.primary_skills_dir

    @staticmethod
    def extract_skill_name(relative_path: str | None, fallback_title: str = "") -> str:
        raw = (relative_path or "").replace("\\", "/").strip("/")
        parts = [p for p in raw.split("/") if p]
        candidate = ""
        if parts:
            if parts[-1].lower() == "skill.md" and len(parts) >= 2:
                candidate = parts[-2]
            else:
                candidate = parts[0]
        if not candidate and fallback_title:
            candidate = re.sub(r"[^a-z0-9]+", "-", fallback_title.lower()).strip("-")
        candidate = re.sub(r"[^a-z0-9-]+", "-", candidate.lower()).strip("-")
        return candidate[:62]

    @staticmethod
    def validate_skill_markdown(skill_name: str, content: str) -> dict[str, str]:
        return validate_skill_markdown(skill_name, content)

    def inspect_existing_skill(self, skill_name: str) -> dict[str, Any]:
        """Read-only view of an existing Skill: size, section outline, line ending and validity."""
        return inspect_skill_at(self.root, skill_name)

    def _appended_bytes(
        self,
        skill_name: str,
        existing_bytes: bytes,
        content: str,
        *,
        fallback_title: str,
    ) -> tuple[bytes, int, int, int]:
        """The existing file's bytes plus the appended section.

        The existing bytes stay a verbatim prefix of the result: an update never rewrites, never
        drops and never reorders what is already in the file.
        """
        existing_text = existing_bytes.decode("utf-8", errors="replace")
        eol = "\r\n" if b"\r\n" in existing_bytes else "\n"
        addition = normalize_update_addition(
            content,
            fallback_heading=fallback_title or skill_name,
            today_ymd=shanghai_today_ymd(),
        )
        if not addition:
            raise SkillWriteError(f"Cannot update skill '{skill_name}': the appended section is empty.")
        addition = sanitize_and_check_secrets(addition, context=f"skill:{skill_name}")
        addition_bytes = addition.replace("\r\n", "\n").replace("\n", eol).encode("utf-8")
        prefix = existing_bytes
        if not prefix.endswith(b"\n"):
            prefix += eol.encode("utf-8")
        # Exactly one blank line separates the old content from the appended section.
        new_bytes = prefix + eol.encode("utf-8") + addition_bytes + eol.encode("utf-8")
        if not new_bytes.startswith(existing_bytes):
            raise SkillWriteError(f"Lossless update invariant violated for skill '{skill_name}'.")
        assert new_bytes.startswith(existing_bytes)
        existing_lines = len(existing_text.splitlines())
        total_lines = len(new_bytes.decode("utf-8").splitlines())
        return new_bytes, existing_lines, total_lines - existing_lines, total_lines

    def write_skill_verified(
        self,
        relative_path: str | None,
        content: str,
        *,
        action: str = "create",
        run_id: str = "",
        fallback_title: str = "",
    ) -> dict[str, Any]:
        """Write a Skill and verify it byte for byte.

        `action="create"` writes a whole `SKILL.md` (LF normalized). `action="update"` appends
        one section to the existing `SKILL.md`: the existing bytes are kept verbatim as the
        prefix of the new file, and the target must already exist.
        """
        skill_name = self.extract_skill_name(relative_path, fallback_title=fallback_title)
        if not _SKILL_SLUG_RE.match(skill_name):
            raise SkillWriteError(
                f"Invalid skill folder name '{skill_name}': must be lowercase alphanumeric + hyphens (2-63 chars)."
            )

        skills_root = self.root.resolve()
        skills_root.mkdir(parents=True, exist_ok=True)
        target_dir = (skills_root / skill_name).resolve()
        if target_dir.parent != skills_root:
            raise SkillWriteError(f"Path traversal blocked for skill '{skill_name}'.")
        target_file = target_dir / "SKILL.md"

        existing_bytes = target_file.read_bytes() if target_file.is_file() else None

        if action == "update":
            if existing_bytes is None:
                # An update appends to an existing Skill: it never becomes a new file.
                raise SkillWriteError(f"Cannot update missing skill '{skill_name}'")
            new_bytes, existing_lines, added_lines, total_lines = self._appended_bytes(
                skill_name,
                existing_bytes,
                content,
                fallback_title=fallback_title,
            )
            existing_text = existing_bytes.decode("utf-8", errors="replace")
            try:
                meta = self.validate_skill_markdown(skill_name, existing_text)
            except SkillWriteError as exc:
                raise SkillWriteError(f"Cannot update skill '{skill_name}': {exc}") from None
            effective_action = "update"
            update_mode = "append_section"
        else:
            body = sanitize_and_check_secrets(content, context=f"skill:{skill_name}")
            meta = self.validate_skill_markdown(skill_name, body)
            normalized_bytes = (body.replace("\r\n", "\n").rstrip() + "\n").encode("utf-8")
            new_bytes = normalized_bytes
            existing_lines = 0
            total_lines = len(normalized_bytes.decode("utf-8").splitlines())
            added_lines = total_lines
            # A create that lands on an existing file replaces it: that is an update in fact.
            effective_action = "update" if existing_bytes is not None else "create"
            update_mode = "create"

        backup_path: str | None = None
        if target_file.exists():
            backup_dir = (self.cfg.data_path / "skill_backups" / skill_name).resolve()
            backup_dir.mkdir(parents=True, exist_ok=True)
            tag = run_id or "manual"
            bfile = backup_dir / f"{tag}-SKILL.md.bak"
            bfile.write_bytes(target_file.read_bytes())
            backup_path = str(bfile)

        target_dir.mkdir(parents=True, exist_ok=True)
        tmp_file = target_dir / "SKILL.md.tmp"
        tmp_file.write_bytes(new_bytes)
        tmp_file.replace(target_file)

        # Read-back SHA-256 verification
        readback = target_file.read_bytes()
        expected_sha = hashlib.sha256(new_bytes).hexdigest()
        actual_sha = hashlib.sha256(readback).hexdigest()
        if actual_sha != expected_sha:
            raise SkillWriteError(f"Read-back SHA-256 mismatch for skill '{skill_name}'.")

        return {
            "kind": "skill_improvement",
            "skill_name": skill_name,
            "frontmatter_name": meta["name"],
            "relative_path": f"{skill_name}/SKILL.md",
            "abs_path": str(target_file),
            "action": effective_action,
            "sha256": actual_sha,
            "backup_path": backup_path,
            "verified": True,
            "existing_lines": existing_lines,
            "added_lines": added_lines,
            "total_lines": total_lines,
            "update_mode": update_mode,
        }
