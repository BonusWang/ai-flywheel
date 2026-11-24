"""Rules, the mistake observation pool, dissents, skills and system settings."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from ai_flywheel.models import RuleRecord, RuleStatus
from ai_flywheel.storage.db.schema import SEVERITY_RANK, normalize_root_cause_key


class RuleRepoMixin:
    """Rules, the mistake observation pool, dissents, skills and settings."""


    # ---------------- Rules ----------------
    def upsert_rule(self, rule: RuleRecord) -> RuleRecord:
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO rules (
                    rule_id, title, domain, projects_json, triggers_json, checklist_json,
                    forbidden_routes_json, applicable_skills_json, source_pointers_json,
                    status, version, origin_session_key, hit_count, natural_pass_count,
                    recurrence_count, revalidation_history_json, last_hit_at, dormant_at,
                    root_cause_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    title = excluded.title,
                    domain = excluded.domain,
                    projects_json = excluded.projects_json,
                    triggers_json = excluded.triggers_json,
                    checklist_json = excluded.checklist_json,
                    forbidden_routes_json = excluded.forbidden_routes_json,
                    applicable_skills_json = excluded.applicable_skills_json,
                    source_pointers_json = excluded.source_pointers_json,
                    status = excluded.status,
                    version = excluded.version,
                    hit_count = excluded.hit_count,
                    natural_pass_count = excluded.natural_pass_count,
                    recurrence_count = excluded.recurrence_count,
                    revalidation_history_json = excluded.revalidation_history_json,
                    last_hit_at = COALESCE(excluded.last_hit_at, rules.last_hit_at),
                    dormant_at = excluded.dormant_at,
                    root_cause_key = COALESCE(excluded.root_cause_key, rules.root_cause_key),
                    updated_at = excluded.updated_at
                """,
                (
                    rule.rule_id,
                    rule.title,
                    rule.domain,
                    json.dumps(rule.projects, ensure_ascii=False),
                    json.dumps(rule.triggers, ensure_ascii=False),
                    json.dumps(rule.checklist, ensure_ascii=False),
                    json.dumps(rule.forbidden_routes, ensure_ascii=False),
                    json.dumps(rule.applicable_skills, ensure_ascii=False),
                    json.dumps(rule.source_pointers, ensure_ascii=False),
                    rule.status.value,
                    rule.version,
                    rule.origin_session_key,
                    rule.hit_count,
                    rule.natural_pass_count,
                    rule.recurrence_count,
                    json.dumps(rule.revalidation_history, ensure_ascii=False),
                    rule.last_hit_at,
                    rule.dormant_at,
                    rule.root_cause_key,
                    rule.created_at,
                    now,
                ),
            )
        return self.get_rule(rule.rule_id)  # type: ignore[return-value]

    def _row_to_rule(self, row: sqlite3.Row) -> RuleRecord:
        keys = set(row.keys())
        return RuleRecord(
            rule_id=row["rule_id"],
            title=row["title"],
            domain=row["domain"],
            projects=json.loads(row["projects_json"]),
            triggers=json.loads(row["triggers_json"]),
            checklist=json.loads(row["checklist_json"]),
            forbidden_routes=json.loads(row["forbidden_routes_json"]),
            applicable_skills=json.loads(row["applicable_skills_json"]),
            source_pointers=json.loads(row["source_pointers_json"]),
            status=RuleStatus(row["status"]),
            version=row["version"],
            origin_session_key=row["origin_session_key"],
            hit_count=row["hit_count"],
            natural_pass_count=row["natural_pass_count"],
            recurrence_count=row["recurrence_count"],
            revalidation_history=json.loads(row["revalidation_history_json"]),
            last_hit_at=row["last_hit_at"] if "last_hit_at" in keys else None,
            dormant_at=row["dormant_at"] if "dormant_at" in keys else None,
            root_cause_key=row["root_cause_key"] if "root_cause_key" in keys else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_rule(self, rule_id: str) -> RuleRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
            return self._row_to_rule(row) if row else None

    def list_rules(self, status: str | None = None) -> list[RuleRecord]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM rules WHERE status = ? ORDER BY updated_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM rules ORDER BY updated_at DESC").fetchall()
            return [self._row_to_rule(r) for r in rows]

    def increment_rule_hit(self, rule_id: str) -> None:
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                "UPDATE rules SET hit_count = hit_count + 1, last_hit_at = ?, updated_at = ? WHERE rule_id = ?",
                (now, now, rule_id),
            )

    # ---------------- v3 Mistake Observation Pool ----------------
    def upsert_mistake_observation(
        self,
        *,
        root_cause_key: str,
        title: str,
        severity_grade: str = "yellow_caught",
        phenomenon: str = "",
        truth: str = "",
        thinking_root_cause: str = "",
        caught_by: str = "self",
        session_key: str = "",
    ) -> tuple[dict[str, Any], bool]:
        key = normalize_root_cause_key(root_cause_key) or f"obs-{uuid.uuid4().hex[:8]}"
        grade = severity_grade if severity_grade in {"red_silent", "yellow_caught", "green_minor"} else "yellow_caught"
        catcher = caught_by if caught_by in {"user", "tool", "self"} else "self"
        now = self._now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM mistake_observations WHERE root_cause_key = ?",
                (key,),
            ).fetchone()
            if existing:
                sessions = json.loads(existing["session_keys_json"] or "[]")
                new_count = int(existing["occurrence_count"])
                if session_key and session_key not in sessions:
                    sessions.append(session_key)
                    new_count += 1
                elif not session_key:
                    new_count += 1
                # Upgrade severity if a red_silent occurrence arrives
                merged_grade = existing["severity_grade"]
                rank = SEVERITY_RANK
                if rank.get(grade, 1) > rank.get(merged_grade, 1):
                    merged_grade = grade
                conn.execute(
                    """
                    UPDATE mistake_observations
                    SET severity_grade = ?,
                        title = CASE WHEN ? != '' THEN ? ELSE title END,
                        phenomenon = CASE WHEN ? != '' THEN ? ELSE phenomenon END,
                        truth = CASE WHEN ? != '' THEN ? ELSE truth END,
                        thinking_root_cause = CASE WHEN ? != '' THEN ? ELSE thinking_root_cause END,
                        caught_by = ?,
                        occurrence_count = ?,
                        session_keys_json = ?,
                        version = version + 1,
                        updated_at = ?
                    WHERE obs_id = ?
                    """,
                    (
                        merged_grade,
                        title.strip(),
                        title.strip(),
                        phenomenon.strip(),
                        phenomenon.strip(),
                        truth.strip(),
                        truth.strip(),
                        thinking_root_cause.strip(),
                        thinking_root_cause.strip(),
                        catcher,
                        new_count,
                        json.dumps(sessions, ensure_ascii=False),
                        now,
                        existing["obs_id"],
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM mistake_observations WHERE obs_id = ?",
                    (existing["obs_id"],),
                ).fetchone()
                return self._row_to_observation(row), False

            obs_id = f"obs-{uuid.uuid4().hex[:10]}"
            sessions = [session_key] if session_key else []
            conn.execute(
                """
                INSERT INTO mistake_observations (
                    obs_id, root_cause_key, severity_grade, title, phenomenon, truth,
                    thinking_root_cause, caught_by, occurrence_count, session_keys_json,
                    status, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'observing', 1, ?, ?)
                """,
                (
                    obs_id,
                    key,
                    grade,
                    title.strip() or key,
                    phenomenon.strip(),
                    truth.strip(),
                    thinking_root_cause.strip(),
                    catcher,
                    json.dumps(sessions, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM mistake_observations WHERE obs_id = ?",
                (obs_id,),
            ).fetchone()
            return self._row_to_observation(row), True

    @staticmethod
    def _row_to_observation(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["session_keys"] = json.loads(d.pop("session_keys_json", "[]") or "[]")
        return d

    def get_mistake_observation(self, obs_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM mistake_observations WHERE obs_id = ?", (obs_id,)).fetchone()
            return self._row_to_observation(row) if row else None

    def get_mistake_observation_by_root_cause_key(
        self, root_cause_key: str
    ) -> dict[str, Any] | None:
        """The observation of one root cause, looked up by its normalized key."""
        key = normalize_root_cause_key(root_cause_key)
        if not key:
            return None
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM mistake_observations WHERE root_cause_key = ?", (key,)
            ).fetchone()
            return self._row_to_observation(row) if row else None

    def list_mistake_observations(
        self,
        *,
        status: str | None = None,
        severity_grade: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if severity_grade:
            clauses.append("severity_grade = ?")
            params.append(severity_grade)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM mistake_observations
                {where}
                ORDER BY
                    CASE severity_grade WHEN 'red_silent' THEN 0 WHEN 'yellow_caught' THEN 1 ELSE 2 END ASC,
                    occurrence_count DESC,
                    updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [self._row_to_observation(r) for r in rows]

    def update_mistake_observation_status(
        self,
        obs_id: str,
        status: str,
        *,
        promoted_rule_id: str | None = None,
    ) -> dict[str, Any] | None:
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE mistake_observations
                SET status = ?,
                    promoted_rule_id = COALESCE(?, promoted_rule_id),
                    version = version + 1,
                    updated_at = ?
                WHERE obs_id = ?
                """,
                (status, promoted_rule_id, now, obs_id),
            )
            row = conn.execute("SELECT * FROM mistake_observations WHERE obs_id = ?", (obs_id,)).fetchone()
            return self._row_to_observation(row) if row else None

    # ---------------- v3 Dissent & Counter-Evidence Records ----------------
    def add_dissent_record(
        self,
        *,
        target_type: str = "rule",
        target_id: str,
        counter_claim: str,
        condition_scope: str = "",
        evidence_pointer: str = "",
        session_key: str = "",
    ) -> dict[str, Any]:
        ttype = target_type if target_type in {"rule", "knowledge_doc", "skill"} else "rule"
        claim = counter_claim.strip()
        now = self._now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM dissent_records
                WHERE target_type = ? AND target_id = ? AND counter_claim = ? AND status = 'open'
                """,
                (ttype, target_id, claim),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE dissent_records
                    SET condition_scope = CASE WHEN ? != '' THEN ? ELSE condition_scope END,
                        evidence_pointer = CASE WHEN ? != '' THEN ? ELSE evidence_pointer END,
                        updated_at = ?
                    WHERE dissent_id = ?
                    """,
                    (
                        condition_scope.strip(),
                        condition_scope.strip(),
                        evidence_pointer.strip(),
                        evidence_pointer.strip(),
                        now,
                        existing["dissent_id"],
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM dissent_records WHERE dissent_id = ?",
                    (existing["dissent_id"],),
                ).fetchone()
                return dict(row)

            dissent_id = f"dis-{uuid.uuid4().hex[:10]}"
            conn.execute(
                """
                INSERT INTO dissent_records (
                    dissent_id, target_type, target_id, counter_claim, condition_scope,
                    evidence_pointer, session_key, status, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)
                """,
                (
                    dissent_id,
                    ttype,
                    target_id,
                    claim,
                    condition_scope.strip(),
                    evidence_pointer.strip(),
                    session_key.strip(),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM dissent_records WHERE dissent_id = ?", (dissent_id,)).fetchone()
            return dict(row)

    def list_dissent_records(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if target_id:
            clauses.append("target_id = ?")
            params.append(target_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM dissent_records {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_open_dissents_by_targets(self, target_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        ids = [t for t in dict.fromkeys(target_ids) if t]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM dissent_records
                WHERE status = 'open' AND target_id IN ({placeholders})
                ORDER BY created_at DESC
                """,
                tuple(ids),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            d = dict(r)
            grouped.setdefault(d["target_id"], []).append(d)
        return grouped

    def update_dissent_status(self, dissent_id: str, status: str) -> dict[str, Any] | None:
        if status not in {"open", "merged", "dismissed"}:
            return None
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE dissent_records
                SET status = ?, version = version + 1, updated_at = ?
                WHERE dissent_id = ?
                """,
                (status, now, dissent_id),
            )
            row = conn.execute("SELECT * FROM dissent_records WHERE dissent_id = ?", (dissent_id,)).fetchone()
            return dict(row) if row else None

    # ---------------- v3 Rule Auto-Dormancy & Growth Metrics ----------------
    def mark_dormant_stale_rules(self, *, days: int = 14) -> list[dict[str, Any]]:
        """Auto-demote unverified_active rules older than `days` with hit_count == 0 to dormant."""
        cutoff = (self.now_datetime() - timedelta(days=days)).isoformat()
        now = self._now_iso()
        demoted: list[dict[str, Any]] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT rule_id, title, created_at FROM rules
                WHERE status = ? AND hit_count = 0 AND created_at < ?
                """,
                (RuleStatus.UNVERIFIED_ACTIVE.value, cutoff),
            ).fetchall()
            for r in rows:
                conn.execute(
                    """
                    UPDATE rules
                    SET status = ?, dormant_at = ?, updated_at = ?
                    WHERE rule_id = ?
                    """,
                    (RuleStatus.DORMANT.value, now, now, r["rule_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO audit_events (entity_type, entity_id, event_type, actor, summary, payload_json, created_at)
                    VALUES ('rule', ?, 'auto_dormant', 'dream', ?, ?, ?)
                    """,
                    (
                        r["rule_id"],
                        f"试运行规则超过 {days} 天零命中，自动转入休眠：{r['title']}",
                        json.dumps({"days_threshold": days, "created_at": r["created_at"]}, ensure_ascii=False),
                        now,
                    ),
                )
                demoted.append({"rule_id": r["rule_id"], "title": r["title"], "created_at": r["created_at"]})
        return demoted
