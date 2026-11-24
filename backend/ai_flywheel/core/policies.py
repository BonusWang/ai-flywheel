from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ai_flywheel.config import FlywheelConfig, deep_merge
from ai_flywheel.storage.db import FlywheelDB


class PolicyEngine:
    """Hosts flywheel methodology policies independently of any external Wiki.

    Merge precedence (lowest -> highest):
      1. Built-in default profile (`./profile/policies/*.yaml`)
      2. Optional external override directory (e.g. private Home Repo `flywheel-profile/`)
      3. Runtime UI/API edits persisted in SQLite `policy_overrides`
    """

    def __init__(self, cfg: FlywheelConfig, db: FlywheelDB) -> None:
        self.cfg = cfg
        self.db = db

    def _load_yaml_if_exists(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def load_site_profile(self) -> dict[str, Any]:
        base = self._load_yaml_if_exists(self.cfg.default_profile_path / "site.yaml")
        if self.cfg.override_profile_path:
            ov = self._load_yaml_if_exists(self.cfg.override_profile_path / "site.yaml")
            base = deep_merge(base, ov)
        return base

    def list_policies(self) -> dict[str, dict[str, Any]]:
        policies: dict[str, dict[str, Any]] = {}
        default_dir = self.cfg.default_profile_path / "policies"
        if default_dir.exists():
            for fp in sorted(default_dir.glob("*.yaml")):
                doc = self._load_yaml_if_exists(fp)
                pid = str(doc.get("policy_id") or fp.stem)
                doc["policy_id"] = pid
                doc["_source"] = "default"
                policies[pid] = doc

        if self.cfg.override_profile_path:
            override_policies_dir = self.cfg.override_profile_path / "policies"
            # Support both `override_dir/policies/*.yaml` and flat `override_dir/*.yaml`
            candidates = []
            if override_policies_dir.exists():
                candidates.extend(sorted(override_policies_dir.glob("*.yaml")))
            for fp in sorted(self.cfg.override_profile_path.glob("*.yaml")):
                if fp.name not in {"site.yaml", "taxonomy.yaml"}:
                    candidates.append(fp)
            for fp in candidates:
                doc = self._load_yaml_if_exists(fp)
                pid = str(doc.get("policy_id") or fp.stem)
                if pid in policies:
                    policies[pid] = deep_merge(policies[pid], doc)
                    policies[pid]["_source"] = "profile_override"
                else:
                    doc["policy_id"] = pid
                    doc["_source"] = "profile_override"
                    policies[pid] = doc

        ui_overrides = self.db.get_policy_overrides()
        for pid, ov in ui_overrides.items():
            if pid in policies:
                policies[pid] = deep_merge(policies[pid], ov)
            else:
                policies[pid] = dict(ov)
            policies[pid]["policy_id"] = pid
            policies[pid]["_source"] = "ui_override"

        return policies

    def get_policy(self, policy_id: str) -> dict[str, Any] | None:
        return self.list_policies().get(policy_id)

    def update_policy(self, policy_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get_policy(policy_id) or {"policy_id": policy_id}
        cleaned = {k: v for k, v in patch.items() if not k.startswith("_")}
        merged = deep_merge({k: v for k, v in current.items() if not k.startswith("_")}, cleaned)
        merged["policy_id"] = policy_id
        self.db.save_policy_override(policy_id, merged)
        return self.get_policy(policy_id)  # type: ignore[return-value]

    def reset_policy(self, policy_id: str) -> dict[str, Any] | None:
        self.db.delete_policy_override(policy_id)
        return self.get_policy(policy_id)

    def determine_anti_laziness_mode(self, task: str, requested_mode: str | None = None) -> str:
        if requested_mode in {"baseline", "execution"}:
            return requested_mode
        policy = self.get_policy("anti_laziness") or {}
        exec_cfg = policy.get("execution", {})
        keywords = exec_cfg.get("trigger_keywords", [])
        lower_task = task.lower()
        for kw in keywords:
            if str(kw).lower() in lower_task:
                return "execution"
        return "execution" if len(task.strip()) >= 80 else "baseline"

    def render_anti_laziness_card(self, mode: str) -> str:
        policy = self.get_policy("anti_laziness") or {}
        if not policy.get("enabled", True):
            return ""
        lines: list[str] = []
        baseline = policy.get("baseline", {})
        if baseline.get("enabled", True):
            tag = baseline.get("tag", "[反偷懒·基线]")
            rules = "；".join(baseline.get("rules", []))
            if rules:
                lines.append(f"{tag} {rules}")
        if mode == "execution":
            execution = policy.get("execution", {})
            if execution.get("enabled", True):
                tag = execution.get("tag", "[反偷懒·执行]")
                rules = "；".join(execution.get("rules", []))
                if rules:
                    lines.append(f"{tag} {rules}")
        return "\n".join(lines)

    def render_user_preferences_card(self) -> str:
        prefs = self.get_policy("user_preferences") or {}
        if not prefs.get("enabled", True):
            return ""
        parts: list[str] = []
        address = prefs.get("address_as")
        if address:
            parts.append(f"[协作对象] 称呼：{address}")
        styles = prefs.get("expression_style", [])
        if styles:
            parts.append("[表达偏好] " + "；".join(styles))
        redlines = prefs.get("non_negotiables", [])
        if redlines:
            parts.append("[协作红线] " + "；".join(redlines))
        return "\n".join(parts)

    def get_hard_gates(self) -> dict[str, Any]:
        return self.get_policy("hard_gates") or {}
