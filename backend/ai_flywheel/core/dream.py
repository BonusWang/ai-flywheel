from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.time_utils import shanghai_iso_week_key, shanghai_today_ymd
from ai_flywheel.models import RuleRecord, RuleStatus
from ai_flywheel.storage.db import FlywheelDB


def _jaccard(a: list[str], b: list[str]) -> float:
    sa = {x.strip().lower() for x in a if x.strip()}
    sb = {x.strip().lower() for x in b if x.strip()}
    if not sa or not sb:
        return 0.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def _shanghai_iso_week_key(now_utc: datetime | None = None) -> str:
    """The ISO week of the Shanghai date; the weekly distillation key."""
    return shanghai_iso_week_key(now_utc)


def find_overlapping_rule_pairs(rules: list[RuleRecord], threshold: float = 0.7) -> list[dict[str, Any]]:
    active = [r for r in rules if r.status in {RuleStatus.VERIFIED_ACTIVE, RuleStatus.UNVERIFIED_ACTIVE}]
    pairs: list[dict[str, Any]] = []
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            sim = _jaccard(active[i].triggers, active[j].triggers)
            if sim >= threshold:
                pairs.append(
                    {
                        "rule_a": active[i].rule_id,
                        "title_a": active[i].title,
                        "rule_b": active[j].rule_id,
                        "title_b": active[j].title,
                        "trigger_overlap": round(sim, 2),
                    }
                )
    return pairs


def _collect_skill_alerts(
    db: FlywheelDB,
    now: datetime,
    stale_skill_days: int = 21,
) -> list[dict[str, Any]]:
    cutoff_skill = (now - timedelta(days=stale_skill_days)).isoformat()
    skill_alerts: list[dict[str, Any]] = []
    for sk in db.list_skill_assets():
        inv = int(sk.get("invoke_count") or 0)
        fails = int(sk.get("fail_count") or 0)
        origin = str(sk.get("origin") or "manual")
        created_at = str(sk.get("created_at") or "")
        if fails >= 3:
            skill_alerts.append(
                {
                    "skill_name": sk["skill_name"],
                    "reason": f"实测失败 {fails} 次（调用 {inv} 次），建议检修或下线",
                    "kind": "failing",
                }
            )
        elif origin.startswith("flywheel") and inv == 0 and created_at and created_at < cutoff_skill:
            skill_alerts.append(
                {
                    "skill_name": sk["skill_name"],
                    "reason": f"飞轮创建超过 {stale_skill_days} 天且调用 0 次，疑似闲置 Skill，建议精简归档",
                    "kind": "stale_unused",
                }
            )
    return skill_alerts


def _build_proposals(
    merge_candidates: list[dict[str, Any]],
    skill_alerts: list[dict[str, Any]],
    expired_cards: list[dict[str, Any]],
    open_dissents: list[dict[str, Any]],
) -> list[str]:
    proposals: list[str] = []
    for pair in merge_candidates[:5]:
        proposals.append(
            f"规则合并建议：`{pair['rule_a']}`（{pair['title_a']}）与 `{pair['rule_b']}`（{pair['title_b']}）触发词重合度 {int(pair['trigger_overlap'] * 100)}%"
        )
    for sk_alert in skill_alerts[:5]:
        proposals.append(f"Skill 清理/检修建议：`{sk_alert['skill_name']}` — {sk_alert['reason']}")
    if expired_cards:
        proposals.append(f"经验卡复核提醒：共 {len(expired_cards)} 篇经验卡已超过有效复核期，建议核实或归档")
    if open_dissents:
        proposals.append(f"分歧收敛提醒：当前有 {len(open_dissents)} 条未处理分歧/反例，建议吸收为带条件的规则")
    return proposals


def preview_dream_consolidation(
    cfg: FlywheelConfig,
    db: FlywheelDB,
    *,
    dormant_days: int = 14,
    stale_skill_days: int = 21,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Read-only preview of Friday Dream Consolidation with zero database writes."""
    del cfg  # reserved for future threshold overrides
    now = now_utc or db.now_datetime()
    today = shanghai_today_ymd(now)
    period_key = _shanghai_iso_week_key(now)
    cutoff_dormant = (now - timedelta(days=dormant_days)).isoformat()

    all_rules = db.list_rules()
    dormant_candidates = [
        {"rule_id": r.rule_id, "title": r.title, "created_at": r.created_at}
        for r in all_rules
        if r.status == RuleStatus.UNVERIFIED_ACTIVE
        and int(r.hit_count or 0) == 0
        and r.created_at
        and r.created_at < cutoff_dormant
    ]

    freshness_map = db.get_knowledge_freshness_map()
    expired_cards = [
        info
        for info in freshness_map.values()
        if bool(info.get("is_expired"))
        or (
            bool(info.get("valid_until"))
            and str(info.get("valid_until")) < today
            and info.get("freshness_state") not in {"archived", "superseded"}
        )
    ]
    expired_cards.sort(key=lambda item: str(item.get("valid_until") or ""))

    merge_candidates = find_overlapping_rule_pairs(all_rules, threshold=0.7)
    skill_alerts = _collect_skill_alerts(db, now, stale_skill_days=stale_skill_days)
    open_dissents = db.list_dissent_records(status="open", limit=30)
    proposals = _build_proposals(merge_candidates, skill_alerts, expired_cards, open_dissents)

    return {
        "dry_run": True,
        "period_key": period_key,
        "auto_dormant_rules": dormant_candidates,
        "expired_cards": expired_cards,
        "merge_candidates": merge_candidates,
        "skill_alerts": skill_alerts,
        "open_dissents_count": len(open_dissents),
        "proposals": proposals,
        "snapshot": None,
    }


def run_dream_consolidation(
    cfg: FlywheelConfig,
    db: FlywheelDB,
    *,
    run_id: str = "",
    dormant_days: int = 14,
    stale_skill_days: int = 21,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Friday Dream Consolidation: tiered subtraction, expiry sweep, dissent convergence hints, and weekly ledger.

    Tier 1 (auto-executed, reversible, audit-logged):
      - Auto-dormant `unverified_active` rules older than `dormant_days` with `hit_count == 0`.
      - Mark expired knowledge cards (`valid_until < today`) as `needs_recheck`.
      - Persist weekly `flywheel_metrics_snapshots` row.

    Tier 2 (proposed for maintainer confirmation via Feishu/portal decision card, never auto-deleted):
      - Duplicate/overlapping active rules (trigger Jaccard >= 0.7).
      - Stale flywheel-created Skills (`invoke_count == 0` for >= 21 days) or failing Skills (`fail_count >= 3`).
      - Open dissents waiting to converge into conditional rules.

    ``now_utc`` overrides the current time; without it the store's clock decides,
    so a test with an injected clock sees the same "today" as the store does.
    """
    now = now_utc or db.now_datetime()
    today = shanghai_today_ymd(now)
    period_key = _shanghai_iso_week_key(now)

    # Tier 1a: Auto-dormant zero-hit stale trial rules
    dormant_rules = db.mark_dormant_stale_rules(days=dormant_days)

    # Tier 1b: Sweep expired knowledge cards into needs_recheck
    expired_cards = db.mark_expired_knowledge_cards(today)

    # Tier 2a: Detect overlapping active rules
    all_rules = db.list_rules()
    merge_candidates = find_overlapping_rule_pairs(all_rules, threshold=0.7)

    # Tier 2b: Anti-Skill-Theater check (stale unused or repeatedly failing skills)
    skill_alerts = _collect_skill_alerts(db, now, stale_skill_days=stale_skill_days)

    # Tier 2c: Open dissents needing convergence
    open_dissents = db.list_dissent_records(status="open", limit=30)

    # Tier 1c: Persist weekly growth ledger snapshot
    live = db.compute_live_growth_metrics(today)
    snapshot = db.save_metrics_snapshot(
        {
            "period_key": period_key,
            "red_count": live["red_count"],
            "yellow_count": live["yellow_count"],
            "green_count": live["green_count"],
            "repeat_mistake_rate": live["repeat_mistake_rate"],
            "self_catch_rate": live["self_catch_rate"],
            "active_rule_count": live["active_rule_count"],
            "dormant_rule_count": live["dormant_rule_count"],
            "open_dissent_count": live["open_dissent_count"],
            "expired_card_count": live["expired_card_count"],
            "stale_skill_count": live["stale_skill_count"],
            "sample_sufficient": live["sample_sufficient"],
        }
    )

    proposals = _build_proposals(merge_candidates, skill_alerts, expired_cards, open_dissents)

    db.append_audit_event(
        entity_type="dream_consolidation",
        entity_id=run_id or period_key,
        event_type="dream_completed",
        actor="dream",
        summary=(
            f"每周蒸馏完成（{period_key}）：自动休眠 {len(dormant_rules)} 条零命中试运行规则，"
            f"标记 {len(expired_cards)} 篇过期经验卡，生成 {len(proposals)} 项精简建议"
        ),
        payload={
            "period_key": period_key,
            "dormant_rules": [d["rule_id"] for d in dormant_rules],
            "expired_cards": [c["doc_path"] for c in expired_cards[:10]],
            "merge_candidates": merge_candidates[:5],
            "skill_alerts": skill_alerts[:5],
            "open_dissent_count": len(open_dissents),
            "proposals": proposals,
        },
    )

    return {
        "dry_run": False,
        "period_key": period_key,
        "auto_dormant_rules": dormant_rules,
        "expired_cards": expired_cards,
        "merge_candidates": merge_candidates,
        "skill_alerts": skill_alerts,
        "open_dissents_count": len(open_dissents),
        "proposals": proposals,
        "snapshot": snapshot,
    }
