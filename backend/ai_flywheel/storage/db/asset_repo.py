"""Skill assets, policy overrides and system settings.

These are the maintainer's own registrations and overrides: which local Skills
the flywheel knows about, which policies are overridden and which settings are
in force. They are not part of the rule lifecycle, so they live apart from it.
"""
from __future__ import annotations

import json
from typing import Any


class AssetRepoMixin:
    """Skill assets, policy overrides and system settings."""

    def upsert_skill_asset(
        self,
        *,
        skill_name: str,
        source_dir: str,
        description: str,
        origin: str = "manual",
        sha256: str = "",
        last_run_id: str | None = None,
        backup_path: str | None = None,
    ) -> None:
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_assets (
                    skill_name, source_dir, description, origin, sha256, version,
                    last_run_id, backup_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(skill_name) DO UPDATE SET
                    source_dir = excluded.source_dir,
                    description = excluded.description,
                    origin = CASE WHEN excluded.origin != 'manual' THEN excluded.origin ELSE skill_assets.origin END,
                    sha256 = CASE WHEN excluded.sha256 != '' THEN excluded.sha256 ELSE skill_assets.sha256 END,
                    version = CASE WHEN excluded.sha256 != '' AND excluded.sha256 != skill_assets.sha256 THEN skill_assets.version + 1 ELSE skill_assets.version END,
                    last_run_id = COALESCE(excluded.last_run_id, skill_assets.last_run_id),
                    backup_path = COALESCE(excluded.backup_path, skill_assets.backup_path),
                    updated_at = excluded.updated_at
                """,
                (skill_name, source_dir, description, origin, sha256, last_run_id, backup_path, now, now),
            )

    def list_skill_assets(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM skill_assets ORDER BY updated_at DESC, skill_name ASC").fetchall()
            return [dict(r) for r in rows]

    # ---------------- v3 Skill Usage & Health ----------------
    def record_skill_usage(self, skill_names: list[str], *, passed: bool, at: str | None = None) -> None:
        names = [s.strip() for s in dict.fromkeys(skill_names) if s and s.strip()]
        if not names:
            return
        now = at or self._now_iso()
        with self.connect() as conn:
            for name in names:
                existing = conn.execute(
                    "SELECT skill_name, fail_count FROM skill_assets WHERE skill_name = ?",
                    (name,),
                ).fetchone()
                if existing:
                    new_fails = int(existing["fail_count"] or 0) + (0 if passed else 1)
                    health = "failing" if new_fails >= 3 else "active"
                    conn.execute(
                        """
                        UPDATE skill_assets
                        SET invoke_count = invoke_count + 1,
                            verified_pass_count = verified_pass_count + ?,
                            fail_count = ?,
                            last_used_at = ?,
                            health_state = ?,
                            updated_at = ?
                        WHERE skill_name = ?
                        """,
                        (1 if passed else 0, new_fails, now, health, now, name),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO skill_assets (
                            skill_name, source_dir, description, origin, sha256, version,
                            invoke_count, verified_pass_count, fail_count, last_used_at,
                            health_state, created_at, updated_at
                        ) VALUES (?, '', '', 'observed', '', 1, 1, ?, ?, ?, 'active', ?, ?)
                        """,
                        (name, 1 if passed else 0, 0 if passed else 1, now, now, now),
                    )

    # ---------------- Policy Overrides ----------------
    def get_policy_overrides(self) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT policy_id, content_json FROM policy_overrides").fetchall()
            return {r["policy_id"]: json.loads(r["content_json"]) for r in rows}

    def save_policy_override(self, policy_id: str, content: dict[str, Any]) -> None:
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO policy_overrides (policy_id, content_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    content_json = excluded.content_json,
                    updated_at = excluded.updated_at
                """,
                (policy_id, json.dumps(content, ensure_ascii=False), now, now),
            )

    def set_system_setting(
        self,
        key: str,
        category: str,
        value: Any,
        *,
        updated_by: str = "user",
    ) -> None:
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO system_settings (key, category, value_json, updated_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    category = excluded.category,
                    value_json = excluded.value_json,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (key, category, json.dumps(value, ensure_ascii=False), updated_by, now, now),
            )

    def list_system_settings(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM system_settings ORDER BY category, key").fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["value"] = json.loads(d.pop("value_json", "null"))
                out.append(d)
            return out

    def delete_policy_override(self, policy_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM policy_overrides WHERE policy_id = ?", (policy_id,))
