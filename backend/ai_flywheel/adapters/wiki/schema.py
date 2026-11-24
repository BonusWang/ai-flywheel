"""The mounted Wiki's schema contract.

A schema-governed Wiki declares itself in `.wiki-schema.md` at its root. This module
reads that contract (once per file revision), splits a document's frontmatter from its
body, and knows which path segments no P04 write may touch.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# The schema contract of a mounted Wiki: never read back through the document surface.
SCHEMA_FILE_NAME = ".wiki-schema.md"

# Path segments no P04 write may touch (raw material, archives, process memory).
FORBIDDEN_PATH_SEGMENTS = {"raw", "raw-原始素材", ".wiki-tmp", ".wiki-agent-memory"}

# The machine blocks a contract must declare to be usable by the gate.
REQUIRED_SCHEMA_BLOCKS = ("core_contract", "partition_contract", "content_contract")

# The partition that holds the Wiki's projects, and the root it is declared under.
PROJECTS_PARTITION_ID = "projects"
DEFAULT_PROJECTS_ROOT = "wiki-知识库/projects-项目"


class WikiGateError(ValueError):
    """Raised when a P04 write is blocked by the schema-aware Wiki gate."""


def normalize_relative_path(relative_path: str) -> str:
    """A relative path in the Wiki's own notation: forward slashes, no leading ``./`` or ``/``."""
    rel = (relative_path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.lstrip("/")


def split_frontmatter(content: str) -> tuple[str | None, str]:
    """Return (frontmatter_yaml_text, body).  None means frontmatter is absent."""
    if content.startswith("\ufeff"):
        content = content[1:]
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, content
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None, content


class WikiSchemaLoader:
    """Reads the mounted Wiki's `.wiki-schema.md`, cached by the file's revision.

    The contract is read once per file revision: a batch that validates ten documents
    parses the schema once, and a contract edited between two runs is picked up.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: tuple[float, dict[str, Any]] | None = None

    @property
    def schema_path(self) -> Path:
        return self.root / SCHEMA_FILE_NAME

    def load(self, required_blocks: tuple[str, ...] = REQUIRED_SCHEMA_BLOCKS) -> dict[str, Any]:
        """The parsed contract. Raises `WikiGateError` when it cannot be used.

        A broken contract must fail loudly: treating it as "nothing routable" would
        quietly plan zero knowledge documents.
        """
        stat = self.schema_path.stat()
        if self._cache and self._cache[0] == stat.st_mtime:
            return self._cache[1]
        text = self.schema_path.read_text(encoding="utf-8", errors="replace")
        fm_text, _ = split_frontmatter(text)
        if fm_text is None:
            raise WikiGateError(f"{self.schema_path} has no parseable frontmatter")
        data = yaml.safe_load(fm_text)
        if not isinstance(data, dict):
            raise WikiGateError(f"{self.schema_path} frontmatter is not a mapping")
        for key in required_blocks:
            if not isinstance(data.get(key), dict):
                raise WikiGateError(f"{self.schema_path} is missing machine block '{key}'")
        self._cache = (stat.st_mtime, data)
        return data

    def projects_root_rel(self, schema: dict[str, Any]) -> str:
        """The path (relative to the Wiki root) of the partition that holds the projects.

        Read off the contract, never hardcoded: a Wiki may declare another root.
        """
        partitions = (schema.get("partition_contract") or {}).get("partitions") or {}
        root_rel = str((partitions.get(PROJECTS_PARTITION_ID) or {}).get("root") or DEFAULT_PROJECTS_ROOT)
        return root_rel.strip("/")
