"""Shared leaf Markdown frontmatter and line-trimming helpers.

One BOM-safe, line-accurate implementation shared across core engines, skills,
runner normalizers, and the Wiki gate. Zero dependencies on other project
modules so every layer can import it cleanly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "parse_frontmatter",
    "parse_frontmatter_file",
    "split_frontmatter",
    "trim_blank_lines",
]


def split_frontmatter(content: str) -> tuple[str | None, str]:
    """Return ``(frontmatter_yaml_text, body)``. ``None`` means frontmatter is absent."""
    if content.startswith("\ufeff"):
        content = content[1:]
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, content
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None, content


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse leading YAML frontmatter into ``(mapping, body)``.

    Missing, malformed, or non-mapping frontmatter yields ``({}, body)`` without raising.
    """
    text = content or ""
    fm_text, body = split_frontmatter(text.lstrip())
    if fm_text is None:
        return {}, text
    try:
        loaded = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, body
    return (loaded if isinstance(loaded, dict) else {}), body


def parse_frontmatter_file(path: Path) -> dict[str, Any]:
    """Read a Markdown file and return its top-level YAML frontmatter mapping, or ``{}``."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    mapping, _ = parse_frontmatter(text)
    return mapping


def trim_blank_lines(lines: list[str]) -> list[str]:
    """Drop leading and trailing blank lines while preserving inner whitespace."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]
