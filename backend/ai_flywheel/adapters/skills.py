from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from ai_flywheel.config import FlywheelConfig

_SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class SkillWriteError(RuntimeError):
    """Raised when a proposed Skill write violates naming, path, or SKILL.md format gates."""


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
        if not _SKILL_SLUG_RE.match(skill_name):
            raise SkillWriteError(
                f"Invalid skill folder name '{skill_name}': must be lowercase alphanumeric + hyphens (2-63 chars)."
            )
        text = content.strip()
        if not text.startswith("---"):
            raise SkillWriteError(f"Skill '{skill_name}' SKILL.md must start with YAML frontmatter (---).")
        parts = text.split("---", 2)
        if len(parts) < 3:
            raise SkillWriteError(f"Skill '{skill_name}' SKILL.md has unclosed YAML frontmatter.")
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as exc:
            raise SkillWriteError(f"Skill '{skill_name}' frontmatter YAML parse error: {exc}") from exc
        if not isinstance(meta, dict):
            raise SkillWriteError(f"Skill '{skill_name}' frontmatter must be a YAML mapping.")
        name = str(meta.get("name") or "").strip()
        desc = str(meta.get("description") or "").strip()
        body = parts[2].strip()
        if not name:
            raise SkillWriteError(f"Skill '{skill_name}' frontmatter missing required 'name'.")
        if not desc:
            raise SkillWriteError(f"Skill '{skill_name}' frontmatter missing required 'description'.")
        if len(body) < 20:
            raise SkillWriteError(f"Skill '{skill_name}' SKILL.md body is too short to be actionable.")
        return {"name": name, "description": desc}

    def write_skill_verified(
        self,
        relative_path: str | None,
        content: str,
        *,
        action: str = "create",
        run_id: str = "",
        fallback_title: str = "",
    ) -> dict[str, Any]:
        skill_name = self.extract_skill_name(relative_path, fallback_title=fallback_title)
        meta = self.validate_skill_markdown(skill_name, content)

        skills_root = self.root.resolve()
        skills_root.mkdir(parents=True, exist_ok=True)
        target_dir = (skills_root / skill_name).resolve()
        if target_dir.parent != skills_root:
            raise SkillWriteError(f"Path traversal blocked for skill '{skill_name}'.")
        target_file = target_dir / "SKILL.md"

        backup_path: str | None = None
        if target_file.exists():
            backup_dir = (self.cfg.data_path / "skill_backups" / skill_name).resolve()
            backup_dir.mkdir(parents=True, exist_ok=True)
            tag = run_id or "manual"
            bfile = backup_dir / f"{tag}-SKILL.md.bak"
            bfile.write_bytes(target_file.read_bytes())
            backup_path = str(bfile)
            effective_action = "update"
        else:
            effective_action = "create" if action != "update" else "create"

        target_dir.mkdir(parents=True, exist_ok=True)
        normalized_bytes = (content.replace("\r\n", "\n").rstrip() + "\n").encode("utf-8")
        tmp_file = target_dir / "SKILL.md.tmp"
        tmp_file.write_bytes(normalized_bytes)
        tmp_file.replace(target_file)

        # Read-back SHA-256 verification
        readback = target_file.read_bytes()
        expected_sha = hashlib.sha256(normalized_bytes).hexdigest()
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
        }
