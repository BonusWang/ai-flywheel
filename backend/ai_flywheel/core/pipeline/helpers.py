"""Pure and database-backed helpers the P00–P07 stages share.

Everything here is deterministic: normalisation of a review's mistake and dissent
candidates, the two-track promotion rule, routing validation for a knowledge
document, and the payload a half-done P04 batch leaves behind. Nothing in this
module writes knowledge documents, observations, dissents or Skills — the stage
that calls a helper owns the side effect.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from ai_flywheel.adapters.knowledge import KnowledgeSinkAdapter
from ai_flywheel.adapters.wiki_gate import UNROUTED_REASON_LABEL, RouteDecision, RoutingCandidates
from ai_flywheel.models import PlannedWrite, SessionRecord, WorkflowRunRecord
from ai_flywheel.storage.db import FlywheelDB, SEVERITY_RANK, normalize_root_cause_key


def unrouted_reason_breakdown(reason_counts: Counter) -> str:
    """``<label> <count>`` per reason code, in order of first appearance."""
    return "、".join(f"{UNROUTED_REASON_LABEL.get(code, code)} {count}" for code, count in reason_counts.items())


# ---------------- P02: read-only mistake & dissent candidates ----------------


def normalize_candidate_mistake(raw: Any) -> dict[str, Any] | None:
    """One mistake a review found, normalized into the shape the observation pool uses."""
    if not isinstance(raw, dict) or not raw.get("root_cause_key"):
        return None
    key = normalize_root_cause_key(str(raw["root_cause_key"]))
    if not key:
        return None
    grade = str(raw.get("severity_grade") or "yellow_caught")
    if grade not in SEVERITY_RANK:
        grade = "yellow_caught"
    caught_by = str(raw.get("caught_by") or "self")
    if caught_by not in {"user", "tool", "self"}:
        caught_by = "self"
    return {
        "root_cause_key": key,
        "severity_grade": grade,
        "title": str(raw.get("title") or key),
        "phenomenon": str(raw.get("phenomenon") or ""),
        "truth": str(raw.get("truth") or ""),
        "thinking_root_cause": str(raw.get("thinking_root_cause") or ""),
        "caught_by": caught_by,
        "session_key": str(raw.get("session_key") or ""),
    }


def candidate_mistakes(raw_mistakes: list[Any]) -> list[dict[str, Any]]:
    """The mistakes this review found, normalized. Nothing is written to the pool here."""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_mistakes:
        candidate = normalize_candidate_mistake(raw)
        if candidate is None:
            continue
        key = candidate["root_cause_key"]
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def candidate_dissents(raw_dissents: list[Any]) -> list[dict[str, Any]]:
    """The counter-evidence this review found, normalized. Nothing is written here."""
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_dissents:
        if not isinstance(raw, dict) or not raw.get("target_id") or not raw.get("counter_claim"):
            continue
        target_type = str(raw.get("target_type") or "rule")
        if target_type not in {"rule", "knowledge_doc", "skill"}:
            target_type = "rule"
        entry = {
            "target_type": target_type,
            "target_id": str(raw["target_id"]),
            "counter_claim": str(raw["counter_claim"]),
            "condition_scope": str(raw.get("condition_scope") or ""),
            "evidence_pointer": str(raw.get("evidence_pointer") or ""),
            "session_key": str(raw.get("session_key") or ""),
        }
        fingerprint = (entry["target_type"], entry["target_id"], entry["counter_claim"])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append(entry)
    return candidates


def qualifies_for_promotion(severity_grade: str, occurrence_count: int) -> bool:
    """The two-track rule: a red silent mistake promotes at once, a self-caught one after three."""
    if severity_grade == "red_silent":
        return True
    return severity_grade == "yellow_caught" and occurrence_count >= 3


def eligible_promotions(db: FlywheelDB, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The mistakes that may become rules this batch — computed without writing anything.

    Two sources: the observations already in the pool that qualify, and the mistakes this
    review found whose *projected* occurrence count qualifies them (the count they would
    reach once P04 persists them).
    """
    eligible: list[dict[str, Any]] = []
    seen: set[str] = set()

    for obs in db.list_mistake_observations(status="observing", limit=200):
        key = str(obs.get("root_cause_key") or "")
        if not key or key in seen:
            continue
        if not qualifies_for_promotion(
            str(obs.get("severity_grade") or ""), int(obs.get("occurrence_count") or 1)
        ):
            continue
        seen.add(key)
        eligible.append(dict(obs))

    for candidate in candidates:
        key = candidate["root_cause_key"]
        if key in seen:
            continue
        existing = db.get_mistake_observation_by_root_cause_key(key)
        if existing and str(existing.get("status")) != "observing":
            # Already promoted or archived: it never re-enters the promotion list.
            continue
        grade = candidate["severity_grade"]
        count = 1
        session_keys: list[str] = []
        obs_id: str | None = None
        if existing:
            obs_id = str(existing.get("obs_id") or "") or None
            existing_grade = str(existing.get("severity_grade") or "")
            if SEVERITY_RANK.get(existing_grade, 1) > SEVERITY_RANK.get(grade, 1):
                # The pool only ever upgrades a severity, never downgrades it.
                grade = existing_grade
            session_keys = [str(k) for k in (existing.get("session_keys") or [])]
            count = int(existing.get("occurrence_count") or 1)
            # The same session seen again does not add an occurrence.
            if candidate["session_key"] and candidate["session_key"] in session_keys:
                count += 0
            else:
                count += 1
            if candidate["session_key"]:
                session_keys = list(dict.fromkeys([*session_keys, candidate["session_key"]]))
        if not qualifies_for_promotion(grade, count):
            continue
        seen.add(key)
        eligible.append(
            {
                "obs_id": obs_id,
                "root_cause_key": key,
                "severity_grade": grade,
                "title": candidate["title"] or (existing or {}).get("title") or key,
                "phenomenon": candidate["phenomenon"] or (existing or {}).get("phenomenon") or "",
                "truth": candidate["truth"] or (existing or {}).get("truth") or "",
                "thinking_root_cause": candidate["thinking_root_cause"]
                or (existing or {}).get("thinking_root_cause")
                or "",
                "caught_by": candidate["caught_by"],
                "occurrence_count": count,
                "session_keys": session_keys,
                "status": "observing",
                "is_candidate": True,
            }
        )
    return eligible


def promote_observation(db: FlywheelDB, rule_id: str, obs_id: Any, root_cause_key: Any) -> str | None:
    """Link a freshly registered rule to the mistake observation it came from.

    The rule carries either the observation id or the root cause key; both are
    accepted, because a model plan may know the key but not the generated id.
    Returns the observation id that was promoted, if any.
    """
    target_obs_id = str(obs_id).strip() if obs_id else ""
    if not target_obs_id:
        key = normalize_root_cause_key(str(root_cause_key or ""))
        if key:
            existing = db.get_mistake_observation_by_root_cause_key(key)
            if existing:
                target_obs_id = str(existing.get("obs_id") or "")
    if not target_obs_id:
        return None
    updated = db.update_mistake_observation_status(target_obs_id, "promoted", promoted_rule_id=rule_id)
    return target_obs_id if updated else None


# ---------------- routing & partial writes ----------------


def route_knowledge_doc(
    knowledge: KnowledgeSinkAdapter,
    run: WorkflowRunRecord,
    item: PlannedWrite,
    sessions_by_key: dict[str, SessionRecord],
    candidates: RoutingCandidates | None,
) -> RouteDecision:
    """Validate a knowledge document's routing declaration against the mounted Wiki contract.

    Only the dry-run planner's plans fall back to evidence-based routing; a model plan must
    declare where every knowledge document lives.
    """
    project_hint: list[str] = []
    workspaces: list[str] = []
    for sk in item.source_session_keys:
        record = sessions_by_key.get(sk)
        if record:
            project_hint.extend([record.project, record.workspace])
            if record.workspace:
                workspaces.append(record.workspace)
    return knowledge.wiki_gate.evaluate_route(
        relative_path=item.relative_path,
        title=item.title,
        summary=item.summary,
        content=item.content_preview,
        wiki_route=item.wiki_route,
        source_workspaces=workspaces,
        project_hint=" ".join(project_hint),
        allow_evidence_fallback=run.runner_mode == "dry_run",
        candidates=candidates,
    )


def partial_write_payload(
    existing: dict[str, Any] | None,
    write_receipts: list[dict[str, Any]],
    written_paths: list[str],
    error: BaseException,
) -> dict[str, Any]:
    """The receipts of a half-done batch, left on the P04 node before the run fails."""
    return {
        **(existing or {}),
        "partial_write_receipts": list(write_receipts),
        "partial_written_paths": list(written_paths),
        "partial_write_error": str(error),
    }
