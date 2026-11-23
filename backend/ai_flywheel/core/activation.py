from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.core.privacy import sanitize_and_check_secrets
from ai_flywheel.models import (
    CreateRuleRequest,
    OutcomeOrigin,
    OutcomeRecord,
    PreflightRequest,
    PreflightResponse,
    RuleRecord,
    RuleStatus,
    utc_now_iso,
)
from ai_flywheel.storage.db import FlywheelDB
from ai_flywheel.storage.fs_store import ImmutableFileStore, canonical_hash


def _parse_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1])
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


class ActivationEngine:
    """Context activation, skill probing, and two-tier rule revalidation lifecycle."""

    def __init__(
        self,
        cfg: FlywheelConfig,
        db: FlywheelDB,
        fs_store: ImmutableFileStore,
        policies: PolicyEngine,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.fs_store = fs_store
        self.policies = policies

    def probe_local_skills(self) -> list[dict[str, Any]]:
        """Read-only filesystem probe for user-configured Skill directories (no registry state)."""
        discovered: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_dir in self.cfg.capabilities.skills_dirs:
            root = self.cfg.resolve_path(raw_dir)
            if not root.exists() or not root.is_dir():
                continue
            for skill_md in sorted(root.glob("*/SKILL.md")):
                fm = _parse_frontmatter(skill_md)
                name = str(fm.get("name") or skill_md.parent.name)
                if name in seen:
                    continue
                seen.add(name)
                discovered.append(
                    {
                        "name": name,
                        "description": str(fm.get("description") or "").strip(),
                        "path": str(skill_md),
                    }
                )
        return discovered

    def match_rules(self, task: str, project: str = "generic") -> list[RuleRecord]:
        """Match active rules (`unverified_active` and `verified_active`) against task text."""
        lower_task = task.lower()
        lower_proj = (project or "generic").lower()
        active_statuses = {RuleStatus.UNVERIFIED_ACTIVE, RuleStatus.VERIFIED_ACTIVE}
        scored: list[tuple[int, RuleRecord]] = []

        for rule in self.db.list_rules():
            if rule.status not in active_statuses:
                continue
            rule_projects = [p.lower() for p in rule.projects]
            if lower_proj != "generic" and "generic" not in rule_projects and lower_proj not in rule_projects:
                continue
            score = 0
            for trig in rule.triggers:
                t = trig.strip().lower()
                if t and t in lower_task:
                    score += 10
            # Fallback token overlap with title
            if score == 0:
                title_tokens = [w for w in re.split(r"[\s/\-_,，。:：]+", rule.title.lower()) if len(w) >= 2]
                hits = sum(1 for tok in title_tokens if tok in lower_task)
                if hits >= 2:
                    score += hits * 3
            if score > 0:
                if rule.status == RuleStatus.VERIFIED_ACTIVE:
                    score += 2
                scored.append((score, rule))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:5]]

    def suggest_skills(self, task: str, matched_rules: list[RuleRecord]) -> list[dict[str, Any]]:
        skills = self.probe_local_skills()
        if not skills:
            return []
        rule_skill_names = {s for r in matched_rules for s in r.applicable_skills}
        lower_task = task.lower()
        out: list[dict[str, Any]] = []
        for sk in skills:
            score = 0
            if sk["name"] in rule_skill_names:
                score += 20
            if sk["name"].lower() in lower_task:
                score += 15
            desc_tokens = [w for w in re.split(r"[\s,，。;；]+", sk["description"].lower()) if len(w) >= 3]
            score += sum(1 for t in desc_tokens[:12] if t in lower_task)
            if score > 0:
                out.append({**sk, "score": score})
        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:3]

    def preflight_activate(self, req: PreflightRequest) -> PreflightResponse:
        mode = self.policies.determine_anti_laziness_mode(req.task, req.mode)
        policy_card = self.policies.render_anti_laziness_card(mode)
        prefs_card = self.policies.render_user_preferences_card()
        matched_rules = self.match_rules(req.task, req.project)
        for rule in matched_rules:
            self.db.increment_rule_hit(rule.rule_id)
        suggested_skills = self.suggest_skills(req.task, matched_rules)
        knowledge_hits = self.db.search_knowledge_fts(req.task, limit=4)
        open_dissents = self.db.get_open_dissents_by_targets([r.rule_id for r in matched_rules])
        freshness_map = self.db.get_knowledge_freshness_map([str(h.get("doc_path", "")) for h in knowledge_hits])

        # Enrich knowledge hits with freshness status & demote expired cards behind fresh ones
        for hit in knowledge_hits:
            finfo = freshness_map.get(str(hit.get("doc_path", ""))) or {}
            hit["freshness_tier"] = finfo.get("freshness_tier", "evolving")
            hit["valid_until"] = finfo.get("valid_until")
            hit["is_expired"] = bool(finfo.get("is_expired", False))
            if hit["is_expired"]:
                hit["freshness_warning"] = "⚠️ [已过复核期，执行前请先核实]"
        knowledge_hits.sort(key=lambda h: 1 if h.get("is_expired") else 0)

        compact_lines: list[str] = []
        if policy_card:
            compact_lines.append(policy_card)
        if prefs_card:
            compact_lines.append(prefs_card)
        if matched_rules:
            compact_lines.append("[命中先例规则]")
            for r in matched_rules:
                badge = "已验证" if r.status == RuleStatus.VERIFIED_ACTIVE else "待自然验证"
                compact_lines.append(f"- ({r.rule_id} · {badge}) {r.title}")
                if r.checklist:
                    compact_lines.append("  必查: " + " | ".join(r.checklist[:3]))
                if r.forbidden_routes:
                    compact_lines.append("  禁用路线: " + " | ".join(r.forbidden_routes[:2]))
                for dis in (open_dissents.get(r.rule_id) or [])[:2]:
                    scope = f"（条件: {dis['condition_scope']}）" if dis.get("condition_scope") else ""
                    compact_lines.append(f"  ⚠️ 已知分歧/例外: {dis['counter_claim'][:120]}{scope}")
        if suggested_skills:
            compact_lines.append(
                "[推荐 Skill] " + ", ".join(s["name"] for s in suggested_skills)
            )
        if knowledge_hits:
            formatted_hits = [
                f"{h['doc_path']} ⚠️[已过复核期，执行前请先核实]" if h.get("is_expired") else h["doc_path"]
                for h in knowledge_hits
            ]
            compact_lines.append("[关联知识指针] " + ", ".join(formatted_hits))

        receipt_id = f"pf-{uuid.uuid4().hex[:12]}"
        task_fp = canonical_hash({"task": req.task, "project": req.project})
        receipt_payload = {
            "receipt_id": receipt_id,
            "client": req.client,
            "session_id": req.session_id,
            "turn_id": req.turn_id,
            "project": req.project,
            "task_fingerprint": task_fp,
            "anti_laziness_mode": mode,
            "matched_rule_ids": [r.rule_id for r in matched_rules],
            "suggested_skills": [s["name"] for s in suggested_skills],
            "knowledge_pointers": [h["doc_path"] for h in knowledge_hits],
            "created_at": utc_now_iso(),
        }
        _, receipt_hash = self.fs_store.write_receipt("preflight", receipt_id, receipt_payload)
        self.db.save_preflight_receipt(
            receipt_id=receipt_id,
            receipt_hash=receipt_hash,
            client=req.client,
            session_id=req.session_id,
            turn_id=req.turn_id,
            project=req.project,
            task_fingerprint=task_fp,
            anti_laziness_mode=mode,
            matched_rule_ids=[r.rule_id for r in matched_rules],
            payload=receipt_payload,
        )
        # D6 (2026-11-23): every card surfaced by a persisted preflight gets surface_count +1.
        self.db.record_knowledge_surfaces(
            [str(hit.get("doc_path", "")) for hit in knowledge_hits],
            receipt_payload["created_at"],
        )

        # Refresh rule records with incremented hit counts
        refreshed_rules = [self.db.get_rule(r.rule_id) or r for r in matched_rules]
        return PreflightResponse(
            receipt_id=receipt_id,
            receipt_hash=receipt_hash,
            anti_laziness_mode=mode,
            policy_card=policy_card,
            user_preferences_card=prefs_card,
            matched_rules=refreshed_rules,
            suggested_skills=suggested_skills,
            knowledge_hits=knowledge_hits,
            compact_context="\n".join(compact_lines),
        )

    def register_rule(self, req: CreateRuleRequest, *, root_cause_key: str | None = None) -> RuleRecord:
        safe_title = sanitize_and_check_secrets(req.title, context="rule:title")
        safe_checklist = [sanitize_and_check_secrets(c, context="rule:checklist") for c in req.checklist]
        safe_forbidden = [sanitize_and_check_secrets(f, context="rule:forbidden") for f in req.forbidden_routes]

        rid = req.rule_id or f"rule-{uuid.uuid4().hex[:8]}"
        existing = self.db.get_rule(rid)
        if existing:
            existing.title = safe_title
            existing.domain = req.domain
            existing.projects = req.projects or existing.projects
            existing.triggers = req.triggers or existing.triggers
            existing.checklist = safe_checklist or existing.checklist
            existing.forbidden_routes = safe_forbidden or existing.forbidden_routes
            existing.applicable_skills = req.applicable_skills or existing.applicable_skills
            existing.source_pointers = req.source_pointers or existing.source_pointers
            if root_cause_key:
                existing.root_cause_key = root_cause_key
            existing.version += 1
            if existing.status in {RuleStatus.NEEDS_REVISION, RuleStatus.DORMANT}:
                existing.status = RuleStatus.UNVERIFIED_ACTIVE
                existing.dormant_at = None
                existing.revalidation_history.append(
                    {
                        "at": utc_now_iso(),
                        "event": "revised_and_reactivated",
                        "version": existing.version,
                    }
                )
            return self.db.upsert_rule(existing)

        rule = RuleRecord(
            rule_id=rid,
            title=safe_title,
            domain=req.domain,
            projects=req.projects or ["generic"],
            triggers=req.triggers,
            checklist=safe_checklist,
            forbidden_routes=safe_forbidden,
            applicable_skills=req.applicable_skills,
            source_pointers=req.source_pointers,
            status=RuleStatus.UNVERIFIED_ACTIVE,
            version=1,
            origin_session_key=req.origin_session_key,
            root_cause_key=root_cause_key,
            revalidation_history=[
                {
                    "at": utc_now_iso(),
                    "event": "registered_unverified_active",
                    "version": 1,
                }
            ],
        )
        return self.db.upsert_rule(rule)

    def revalidate_from_outcome(self, outcome: OutcomeRecord) -> list[dict[str, Any]]:
        """Two-tier activation lifecycle transition driven by real natural outcomes."""
        if outcome.origin != OutcomeOrigin.NATURAL:
            return []
        effects: list[dict[str, Any]] = []
        checks_by_rule = {c.rule_id: c for c in outcome.checks if c.rule_id}

        for rule_id in outcome.applied_rule_ids:
            rule = self.db.get_rule(rule_id)
            if not rule:
                continue
            # Origin session cannot self-promote its own newly created rule
            if rule.origin_session_key and outcome.session_key == rule.origin_session_key:
                effects.append(
                    {
                        "rule_id": rule_id,
                        "action": "skipped_origin_session",
                        "status": rule.status.value,
                    }
                )
                continue

            check = checks_by_rule.get(rule_id)
            if not check:
                continue

            prev_status = rule.status
            if check.result.value == "pass" and check.kind.value in {"real", "test"}:
                rule.natural_pass_count += 1
                if rule.status == RuleStatus.UNVERIFIED_ACTIVE:
                    rule.status = RuleStatus.VERIFIED_ACTIVE
                rule.revalidation_history.append(
                    {
                        "at": utc_now_iso(),
                        "event": "natural_pass",
                        "outcome_id": outcome.outcome_id,
                        "kind": check.kind.value,
                        "from": prev_status.value,
                        "to": rule.status.value,
                    }
                )
                self.db.upsert_rule(rule)
                effects.append(
                    {
                        "rule_id": rule_id,
                        "action": "promoted" if prev_status != rule.status else "confirmed",
                        "from": prev_status.value,
                        "to": rule.status.value,
                    }
                )
            elif check.result.value == "fail":
                rule.recurrence_count += 1
                rule.status = RuleStatus.NEEDS_REVISION
                rule.revalidation_history.append(
                    {
                        "at": utc_now_iso(),
                        "event": "recurrence_failed",
                        "outcome_id": outcome.outcome_id,
                        "from": prev_status.value,
                        "to": rule.status.value,
                    }
                )
                self.db.upsert_rule(rule)
                effects.append(
                    {
                        "rule_id": rule_id,
                        "action": "downgraded_on_recurrence",
                        "from": prev_status.value,
                        "to": rule.status.value,
                    }
                )
        return effects

    def mark_rule_recurrence(self, rule_id: str, reason: str, session_key: str = "") -> RuleRecord | None:
        rule = self.db.get_rule(rule_id)
        if not rule:
            return None
        prev = rule.status.value
        rule.recurrence_count += 1
        rule.status = RuleStatus.NEEDS_REVISION
        rule.revalidation_history.append(
            {
                "at": utc_now_iso(),
                "event": "signal_recurrence",
                "session_key": session_key,
                "reason": reason,
                "from": prev,
                "to": rule.status.value,
            }
        )
        return self.db.upsert_rule(rule)

    def transition_rule_status(self, rule_id: str, new_status: RuleStatus, note: str = "") -> RuleRecord | None:
        rule = self.db.get_rule(rule_id)
        if not rule:
            return None
        prev = rule.status.value
        rule.status = new_status
        if new_status in {RuleStatus.UNVERIFIED_ACTIVE, RuleStatus.VERIFIED_ACTIVE}:
            rule.dormant_at = None
        elif new_status == RuleStatus.DORMANT:
            rule.dormant_at = utc_now_iso()
        if new_status == RuleStatus.UNVERIFIED_ACTIVE and prev == RuleStatus.NEEDS_REVISION.value:
            rule.version += 1
        rule.revalidation_history.append(
            {
                "at": utc_now_iso(),
                "event": "manual_transition",
                "from": prev,
                "to": new_status.value,
                "note": note,
            }
        )
        return self.db.upsert_rule(rule)
