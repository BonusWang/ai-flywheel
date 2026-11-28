"""Governance domain service: observation lifecycle, retro promotion, knowledge freshness, and skills ledger.

Encapsulates the domain orchestration behind the governance portal routes so HTTP
handlers remain thin transport adapters and every governance operation is directly
callable and testable without FastAPI.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from ai_flywheel.adapters.knowledge import KnowledgeSinkAdapter
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.activation import ActivationEngine
from ai_flywheel.core.policies import PolicyEngine
from ai_flywheel.core.retro_advisory import (
    advise_retro_item,
    build_retro_group_key,
    normalize_retro_summary,
)
from ai_flywheel.core.time_utils import shanghai_today_ymd
from ai_flywheel.models import RetroStatus, SignalKind
from ai_flywheel.storage.db import FlywheelDB


class GovernanceError(Exception):
    """Base error for domain-level governance rejections."""


class GovernanceNotFoundError(GovernanceError):
    """Raised when a targeted observation, retro item, or asset does not exist."""


class GovernanceConflictError(GovernanceError):
    """Raised when an entity's current state disallows the requested transition."""


class GovernanceValidationError(GovernanceError):
    """Raised when caller-supplied parameters fail domain validation."""


_FRESHNESS_ACTIONS = frozenset({"verify", "archive", "restore", "set_tier"})
_FRESHNESS_TIERS = frozenset({"volatile", "evolving", "evergreen"})


class GovernanceService:
    """Cohesive domain engine for mistake observations, retro clues, knowledge freshness, and skills."""

    def __init__(
        self,
        cfg: FlywheelConfig,
        db: FlywheelDB,
        policies: PolicyEngine,
        activation: ActivationEngine,
        knowledge: KnowledgeSinkAdapter,
        *,
        on_state_change: Callable[[], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.policies = policies
        self.activation = activation
        self.knowledge = knowledge
        self._on_state_change = on_state_change

    def _notify_state_change(self) -> None:
        if self._on_state_change is not None:
            self._on_state_change()

    # ---------------- Mistake Observations ----------------

    def archive_observation(self, obs_id: str, *, actor: str = "portal") -> dict[str, Any] | None:
        existing = self.db.get_mistake_observation(obs_id)
        if not existing:
            raise GovernanceNotFoundError("Observation not found")
        if existing.get("status") != "observing":
            raise GovernanceConflictError(
                f"Cannot archive observation in status '{existing.get('status')}'"
            )
        updated = self.db.update_mistake_observation_status(obs_id, "archived")
        self.db.append_audit_event(
            entity_type="mistake_observation",
            entity_id=obs_id,
            event_type="observation_archived",
            actor=actor,
            summary="归档观察错题",
        )
        self._notify_state_change()
        return updated

    def restore_observation(self, obs_id: str, *, actor: str = "portal") -> dict[str, Any] | None:
        existing = self.db.get_mistake_observation(obs_id)
        if not existing:
            raise GovernanceNotFoundError("Observation not found")
        if existing.get("status") != "archived":
            raise GovernanceConflictError(
                f"Only archived observations can be restored (current: '{existing.get('status')}')"
            )
        updated = self.db.update_mistake_observation_status(obs_id, "observing")
        self.db.append_audit_event(
            entity_type="mistake_observation",
            entity_id=obs_id,
            event_type="observation_restored",
            actor=actor,
            summary="撤销归档，恢复错题至观察池",
        )
        self._notify_state_change()
        return updated

    def bulk_archive_observations(self, obs_ids: list[str], *, actor: str = "portal") -> int:
        eligible_ids = [
            obs_id
            for obs_id in dict.fromkeys(obs_ids)
            if (existing := self.db.get_mistake_observation(obs_id))
            and existing.get("status") == "observing"
        ]
        updated = self.db.bulk_update_mistake_observation_status(eligible_ids, "archived")
        if updated > 0:
            self.db.append_audit_event(
                entity_type="mistake_observation",
                entity_id="bulk",
                event_type="observation_bulk_archived",
                actor=actor,
                summary=f"批量归档 {updated} 条观察错题",
                payload={"obs_ids": eligible_ids},
            )
            self._notify_state_change()
        return updated

    def bulk_restore_observations(self, obs_ids: list[str], *, actor: str = "portal") -> int:
        eligible_ids = [
            obs_id
            for obs_id in dict.fromkeys(obs_ids)
            if (existing := self.db.get_mistake_observation(obs_id))
            and existing.get("status") == "archived"
        ]
        updated = self.db.bulk_update_mistake_observation_status(eligible_ids, "observing")
        if updated > 0:
            self.db.append_audit_event(
                entity_type="mistake_observation",
                entity_id="bulk",
                event_type="observation_bulk_restored",
                actor=actor,
                summary=f"批量恢复 {updated} 条已归档错题至观察池",
                payload={"obs_ids": eligible_ids},
            )
            self._notify_state_change()
        return updated

    # ---------------- Retro Queue ----------------

    def list_enriched_retro(self, *, status: str | None = None, limit: int = 300) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        items = self.db.list_retro_items(status=status, limit=safe_limit)
        active_queue_keys = self.db.list_active_queue_session_keys()
        user_prefs = self.policies.get_policy("user_preferences") or {}
        hard_gates = self.policies.get_hard_gates()
        enriched: list[dict[str, Any]] = []
        for item in items:
            has_queued_session = bool(item.session_key and item.session_key in active_queue_keys)
            settlement_mode = "auto_next_batch" if has_queued_session else "manual_only"
            dumped = item.model_dump(mode="json")
            dumped["has_queued_session"] = has_queued_session
            dumped["settlement_mode"] = settlement_mode
            dumped["advisory"] = advise_retro_item(item, user_prefs=user_prefs, hard_gates=hard_gates)
            enriched.append(dumped)
        return {
            "items": enriched,
            "total": self.db.count_retro_items(status=status),
        }

    def promote_retro_items(
        self,
        retro_ids: list[str],
        *,
        severity_grade: str | None = None,
        actor: str = "portal",
    ) -> dict[str, Any]:
        if not retro_ids:
            raise GovernanceValidationError("retro_ids cannot be empty")
        items = self.db.get_retro_items_by_ids(retro_ids)
        if not items:
            raise GovernanceNotFoundError("No matching retro items found")

        items.sort(key=lambda it: it.created_at, reverse=True)
        primary = items[0]
        found_ids = [it.retro_id for it in items]

        kind_val = primary.kind.value if isinstance(primary.kind, SignalKind) else str(primary.kind)
        if primary.rule_id:
            root_cause_key = f"retro-rule-{primary.rule_id}"
        else:
            group_key = build_retro_group_key(primary)
            sha = hashlib.sha256(group_key.encode("utf-8")).hexdigest()
            kind_kebab = kind_val.replace("_", "-")
            root_cause_key = f"retro-{kind_kebab}-{sha[:10]}"

        title = normalize_retro_summary(primary.summary)[:80]
        if severity_grade in {"red_silent", "yellow_caught", "green_minor"}:
            grade = severity_grade
        else:
            is_high = (
                primary.kind in {SignalKind.RULE_RECURRENCE, SignalKind.MISSING_EVIDENCE}
                or kind_val in {"rule_recurrence", "missing_evidence"}
                or primary.severity == "high"
            )
            grade = "red_silent" if is_high else "yellow_caught"

        phenomenon = primary.summary
        truth = f"来源实时异常线索（{kind_val}），需核查根因并固化验证/边界规则"

        adv = advise_retro_item(
            primary,
            user_prefs=self.policies.get_policy("user_preferences") or {},
            hard_gates=self.policies.get_hard_gates(),
        )
        thinking_root_cause = adv.get("reason", "")
        caught_by = "tool"
        session_key = primary.session_key or ""

        obs, created = self.db.upsert_mistake_observation(
            root_cause_key=root_cause_key,
            title=title,
            severity_grade=grade,
            phenomenon=phenomenon,
            truth=truth,
            thinking_root_cause=thinking_root_cause,
            caught_by=caught_by,
            session_key=session_key,
        )

        for other in items[1:]:
            if other.session_key and other.session_key != primary.session_key:
                obs, _ = self.db.upsert_mistake_observation(
                    root_cause_key=root_cause_key,
                    title="",
                    severity_grade=grade,
                    phenomenon="",
                    truth="",
                    thinking_root_cause="",
                    caught_by=caught_by,
                    session_key=other.session_key,
                )

        updated = self.db.bulk_update_retro_status(found_ids, RetroStatus.RESOLVED)

        self.db.append_audit_event(
            entity_type="retro_queue",
            entity_id=primary.retro_id,
            event_type="retro_promoted_to_observation",
            actor=actor,
            summary=f"实时异常线索转入错题观察池：{obs.get('title', '')}",
            payload={
                "obs_id": obs.get("obs_id"),
                "root_cause_key": root_cause_key,
                "retro_ids": found_ids,
                "resolved_count": updated,
            },
        )
        self._notify_state_change()

        return {
            "observation": obs,
            "created": created,
            "resolved_count": updated,
            "retro_ids": found_ids,
        }

    # ---------------- Knowledge Freshness Ledger ----------------

    def list_enriched_knowledge(self, *, limit: int = 200) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        today = shanghai_today_ymd()
        docs = self.knowledge.list_documents(limit=safe_limit)
        freshness_map = self.db.get_knowledge_freshness_map()

        def _is_card_expired(finfo: dict[str, Any]) -> bool:
            if not finfo:
                return False
            if finfo.get("freshness_state") in {"archived", "superseded"}:
                return False
            if bool(finfo.get("is_expired")):
                return True
            valid_until = str(finfo.get("valid_until") or "")
            return bool(valid_until and valid_until < today)

        by_path: dict[str, dict[str, Any]] = {}
        for doc in docs:
            doc_path = doc["doc_path"]
            finfo = freshness_map.get(doc_path) or {}
            by_path[doc_path] = {
                **doc,
                "freshness_tier": finfo.get("freshness_tier", "evolving"),
                "last_verified_at": finfo.get("last_verified_at"),
                "valid_until": finfo.get("valid_until"),
                "freshness_state": finfo.get("freshness_state", "fresh"),
                "is_expired": _is_card_expired(finfo),
                "surface_count": int(finfo.get("surface_count") or 0),
                "helped_count": int(finfo.get("helped_count") or 0),
            }

        for doc_path, finfo in freshness_map.items():
            if doc_path in by_path:
                continue
            expired = _is_card_expired(finfo)
            state = str(finfo.get("freshness_state") or "")
            is_archived = state in {"archived", "superseded"}
            if (
                not expired
                and not is_archived
                and int(finfo.get("surface_count") or 0) == 0
                and int(finfo.get("helped_count") or 0) == 0
            ):
                continue
            by_path[doc_path] = {
                "doc_path": doc_path,
                "title": Path(doc_path).stem,
                "size_bytes": 0,
                "excerpt": "",
                "freshness_tier": finfo.get("freshness_tier", "evolving"),
                "last_verified_at": finfo.get("last_verified_at"),
                "valid_until": finfo.get("valid_until"),
                "freshness_state": finfo.get("freshness_state", "fresh"),
                "is_expired": expired,
                "surface_count": int(finfo.get("surface_count") or 0),
                "helped_count": int(finfo.get("helped_count") or 0),
            }

        enriched = sorted(
            by_path.values(),
            key=lambda d: (
                0 if d["is_expired"] else 1,
                str(d.get("valid_until") or "9999-99-99"),
                str(d.get("doc_path") or ""),
            ),
        )
        expired_total = sum(1 for d in enriched if d["is_expired"])
        return {
            "sink_type": self.cfg.knowledge_sink.type,
            "root": str(self.knowledge.root),
            "documents": enriched,
            "total": len(enriched),
            "expired_total": expired_total,
        }

    def update_knowledge_freshness(
        self,
        *,
        doc_path: str,
        action: str,
        freshness_tier: str | None = None,
        actor: str = "portal",
    ) -> dict[str, Any]:
        clean_path = doc_path.strip()
        if not clean_path:
            raise GovernanceValidationError("doc_path is required")
        clean_action = action.strip().lower()
        if clean_action not in _FRESHNESS_ACTIONS:
            raise GovernanceValidationError(
                f"Invalid action '{action}'. Must be one of verify, archive, restore, set_tier"
            )
        tier = (freshness_tier or "").strip().lower() or None
        if clean_action == "set_tier":
            if not tier or tier not in _FRESHNESS_TIERS:
                raise GovernanceValidationError(
                    f"Invalid freshness_tier '{freshness_tier}'. Must be volatile, evolving, or evergreen"
                )
        elif tier and tier not in _FRESHNESS_TIERS:
            raise GovernanceValidationError(
                f"Invalid freshness_tier '{freshness_tier}'. Must be volatile, evolving, or evergreen"
            )

        today = shanghai_today_ymd()
        try:
            card = self.db.update_knowledge_freshness_action(
                clean_path,
                clean_action,
                today_ymd=today,
                freshness_tier=tier,
            )
        except ValueError as exc:
            raise GovernanceValidationError(str(exc)) from exc

        action_labels = {
            "verify": "确认笔记有效并顺延保鲜期",
            "archive": "移出待复核（台账归档）",
            "restore": "恢复保鲜跟踪",
            "set_tier": f"调整保鲜档位为 {tier}",
        }
        self.db.append_audit_event(
            entity_type="knowledge_card",
            entity_id=clean_path,
            event_type=f"knowledge_freshness_{clean_action}",
            actor=actor,
            summary=f"人工台账复核：{action_labels.get(clean_action, clean_action)}（{clean_path}）",
            payload={
                "doc_path": clean_path,
                "action": clean_action,
                "freshness_tier": tier,
                "card": card,
            },
        )
        self._notify_state_change()

        freshness_map = self.db.get_knowledge_freshness_map()
        expired_total = sum(1 for finfo in freshness_map.values() if finfo.get("is_expired"))
        return {
            "card": card,
            "expired_total": expired_total,
        }

    # ---------------- Skills Ledger ----------------

    def list_merged_skills(self) -> dict[str, Any]:
        db_skills = self.db.list_skill_assets()
        probed = self.activation.probe_local_skills()
        probed_by_name = {str(p.get("name") or ""): p for p in probed if p.get("name")}
        merged_by_name: dict[str, dict[str, Any]] = {}

        for row in db_skills:
            name = str(row.get("skill_name") or "")
            if not name:
                continue
            pinfo = probed_by_name.get(name) or {}
            merged_by_name[name] = {
                **row,
                "description": str(row.get("description") or pinfo.get("description") or "").strip(),
                "path": str(row.get("path") or pinfo.get("path") or ""),
                "probed": name in probed_by_name,
            }

        for name, pinfo in probed_by_name.items():
            if name in merged_by_name:
                continue
            merged_by_name[name] = {
                "skill_name": name,
                "path": str(pinfo.get("path") or ""),
                "description": str(pinfo.get("description") or "").strip(),
                "origin": "local_probed",
                "invoke_count": 0,
                "verified_pass_count": 0,
                "fail_count": 0,
                "health_state": "untested",
                "version": 1,
                "last_used_at": None,
                "created_at": "",
                "updated_at": "",
                "probed": True,
            }

        merged = sorted(
            merged_by_name.values(),
            key=lambda s: (
                0 if (int(s.get("fail_count") or 0) >= 3 or s.get("health_state") == "failing") else 1,
                -int(s.get("invoke_count") or 0),
                str(s.get("skill_name") or "").lower(),
            ),
        )
        return {
            "skills": merged,
            "probed_count": len(probed),
            "tracked_count": len(db_skills),
        }
