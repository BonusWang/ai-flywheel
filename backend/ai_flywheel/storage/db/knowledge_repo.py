"""Full-text search, knowledge-card metrics & freshness, and the growth ledger."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from ai_flywheel.core.time_utils import compute_valid_until, shanghai_today_ymd
from ai_flywheel.storage.db.schema import _knowledge_query_terms, _knowledge_snippet


class KnowledgeRepoMixin:
    """Full-text search, knowledge-card metrics & freshness, and the growth ledger."""


    # ---------------- Knowledge FTS5 ----------------
    def rebuild_knowledge_fts(self, docs: list[dict[str, str]]) -> int:
        with self.connect() as conn:
            conn.execute("DELETE FROM knowledge_fts")
            conn.executemany(
                """
                INSERT INTO knowledge_fts (
                    doc_path, title_index, description_index, body, body_index,
                    tags_index, project_id, page_type, status,
                    title_raw, description_raw, tags_raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        d["doc_path"],
                        d.get("title_index", d.get("title", "")),
                        d.get("description_index", d.get("description", "")),
                        d.get("body", ""),
                        d.get("body_index", d.get("search_text", "")),
                        d.get("tags_index", d.get("tags", "")),
                        d.get("project_id", ""),
                        d.get("page_type", ""),
                        d.get("status", ""),
                        d.get("title_raw", d.get("title", "")),
                        d.get("description_raw", d.get("description", "")),
                        d.get("tags_raw", d.get("tags", "")),
                    )
                    for d in docs
                ],
            )
        return len(docs)

    def search_knowledge_fts(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Chinese-capable search over formal Wiki cards.

        FTS5's unicode61 tokenizer treats a whole CJK run as one token, so the
        title/description/tags/body index columns store space-separated CJK
        bigrams while the ``*_raw`` columns keep the original text for display.
        Metadata carries a higher BM25 weight than body text.
        """
        term_plan = _knowledge_query_terms(query)
        if not term_plan.match_terms and not term_plan.like_terms:
            return []

        match_terms = term_plan.match_terms[:16]
        fts_query = " OR ".join('"' + term.replace('"', "") + '"' for term in match_terms)
        rows: list[sqlite3.Row] = []
        with self.connect() as conn:
            if match_terms:
                try:
                    rows = conn.execute(
                        """
                        SELECT doc_path, title_raw AS title, description_raw AS description,
                               body, tags_raw AS tags, project_id, page_type, status,
                               bm25(knowledge_fts,
                                    0.0, 8.0, 5.0, 0.0, 1.0, 4.0,
                                    0.0, 0.0, 0.0, 0.0, 0.0, 0.0) AS score
                        FROM knowledge_fts
                        WHERE knowledge_fts MATCH ?
                        ORDER BY score
                        LIMIT ?
                        """,
                        (fts_query, max(limit * 6, 24)),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if len(rows) < limit and term_plan.like_terms:
                rows = list(rows) + self._search_knowledge_like(conn, term_plan.like_terms, limit * 6)

        required_overlap = 2 if len(term_plan.overlap_terms) >= 4 else 1
        scored: list[tuple[int, float, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            haystack = "\n".join(
                str(item.get(key) or "") for key in ("title", "description", "body")
            ).casefold()
            overlap = sum(1 for term in term_plan.overlap_terms if term in haystack)
            if overlap < required_overlap:
                continue
            scored.append((overlap, float(item.pop("score", 0.0) or 0.0), item))
        scored.sort(key=lambda entry: (entry[0], -entry[1]), reverse=True)

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _, __, item in scored:
            path = str(item.get("doc_path", ""))
            if not path or path in seen:
                continue
            seen.add(path)
            body = str(item.pop("body", "") or "")
            item["snippet"] = _knowledge_snippet(body, term_plan.overlap_terms)
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def _search_knowledge_like(
        self, conn: sqlite3.Connection, terms: list[str], limit: int
    ) -> list[sqlite3.Row]:
        clauses = []
        params: list[Any] = []
        for term in terms[:4]:
            clauses.append("(title_raw LIKE ? OR description_raw LIKE ? OR body LIKE ?)")
            pattern = f"%{term}%"
            params.extend([pattern, pattern, pattern])
        if not clauses:
            return []
        try:
            return conn.execute(
                f"""
                SELECT doc_path, title_raw AS title, description_raw AS description,
                       body, tags_raw AS tags, project_id, page_type, status, 0.0 AS score
                FROM knowledge_fts
                WHERE {' OR '.join(clauses)}
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    # ---------------- Knowledge card metrics (D6 + v3 freshness) ----------------
    def record_knowledge_surfaces(self, doc_paths: list[str], at: str) -> None:
        paths = [p for p in dict.fromkeys(doc_paths) if p]
        if not paths:
            return
        with self.connect() as conn:
            for path in paths:
                conn.execute(
                    """
                    INSERT INTO knowledge_card_metrics (doc_path, surface_count, last_surfaced_at, created_at, updated_at)
                    VALUES (?, 1, ?, ?, ?)
                    ON CONFLICT(doc_path) DO UPDATE SET
                        surface_count = knowledge_card_metrics.surface_count + 1,
                        last_surfaced_at = excluded.last_surfaced_at,
                        updated_at = excluded.updated_at
                    """,
                    (path, at, at, at),
                )

    def record_knowledge_usage(
        self, doc_paths: list[str], at: str, *, cited_paths: list[str] | None = None
    ) -> None:
        paths = [p for p in dict.fromkeys(doc_paths) if p]
        if not paths:
            return
        cited = {p for p in (cited_paths or []) if p}
        with self.connect() as conn:
            for path in paths:
                conn.execute(
                    """
                    INSERT INTO knowledge_card_metrics (doc_path, helped_count, cited_count, last_helped_at, created_at, updated_at)
                    VALUES (?, 1, ?, ?, ?, ?)
                    ON CONFLICT(doc_path) DO UPDATE SET
                        helped_count = knowledge_card_metrics.helped_count + 1,
                        cited_count = knowledge_card_metrics.cited_count + excluded.cited_count,
                        last_helped_at = excluded.last_helped_at,
                        updated_at = excluded.updated_at
                    """,
                    (path, 1 if path in cited else 0, at, at, at),
                )

    def upsert_knowledge_freshness(
        self,
        doc_path: str,
        *,
        freshness_tier: str = "evolving",
        last_verified_at: str | None = None,
        valid_until: str | None = None,
        freshness_state: str = "fresh",
    ) -> None:
        if not doc_path:
            return
        now = self._now_iso()
        tier = freshness_tier if freshness_tier in {"volatile", "evolving", "evergreen"} else "evolving"
        verified = last_verified_at or shanghai_today_ymd()
        until = valid_until or compute_valid_until(tier, verified)
        state = freshness_state if freshness_state in {"fresh", "needs_recheck", "superseded", "archived"} else "fresh"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO knowledge_card_metrics (
                    doc_path, freshness_tier, last_verified_at, valid_until, freshness_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_path) DO UPDATE SET
                    freshness_tier = excluded.freshness_tier,
                    last_verified_at = COALESCE(excluded.last_verified_at, knowledge_card_metrics.last_verified_at),
                    valid_until = COALESCE(excluded.valid_until, knowledge_card_metrics.valid_until),
                    freshness_state = excluded.freshness_state,
                    updated_at = excluded.updated_at
                """,
                (doc_path, tier, verified, until, state, now, now),
            )

    def refresh_knowledge_freshness(self, doc_paths: list[str], verified_date_ymd: str) -> list[str]:
        """Auto-renew last_verified_at & valid_until when real verification passes."""
        paths = [p for p in dict.fromkeys(doc_paths) if p]
        if not paths:
            return []
        now = self._now_iso()
        refreshed: list[str] = []
        with self.connect() as conn:
            for path in paths:
                row = conn.execute(
                    "SELECT freshness_tier FROM knowledge_card_metrics WHERE doc_path = ?",
                    (path,),
                ).fetchone()
                tier = (row["freshness_tier"] if row and row["freshness_tier"] else "evolving")
                new_until = compute_valid_until(tier, verified_date_ymd)
                conn.execute(
                    """
                    INSERT INTO knowledge_card_metrics (
                        doc_path, freshness_tier, last_verified_at, valid_until, freshness_state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'fresh', ?, ?)
                    ON CONFLICT(doc_path) DO UPDATE SET
                        last_verified_at = excluded.last_verified_at,
                        valid_until = excluded.valid_until,
                        freshness_state = 'fresh',
                        updated_at = excluded.updated_at
                    """,
                    (path, tier, verified_date_ymd, new_until, now, now),
                )
                refreshed.append(path)
        return refreshed

    def mark_expired_knowledge_cards(self, today_ymd: str) -> list[dict[str, Any]]:
        """Mark fresh cards past valid_until as needs_recheck and return all cards currently needing recheck."""
        now = self._now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE knowledge_card_metrics
                SET freshness_state = 'needs_recheck', updated_at = ?
                WHERE valid_until IS NOT NULL AND valid_until != ''
                  AND valid_until < ?
                  AND freshness_state = 'fresh'
                """,
                (now, today_ymd),
            )
            rows = conn.execute(
                """
                SELECT doc_path, freshness_tier, last_verified_at, valid_until, freshness_state,
                       surface_count, helped_count
                FROM knowledge_card_metrics
                WHERE freshness_state = 'needs_recheck'
                   OR (valid_until IS NOT NULL AND valid_until != '' AND valid_until < ? AND freshness_state NOT IN ('archived', 'superseded'))
                ORDER BY valid_until ASC
                """,
                (today_ymd,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_stale_zero_help_cards(
        self, today_ymd: str, *, grace_days: int = 30
    ) -> list[dict[str, Any]]:
        """Read-only query for cards past valid_until by at least grace_days with helped_count == 0."""
        cutoff_ymd = (date.fromisoformat(today_ymd) - timedelta(days=grace_days)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT doc_path, freshness_tier, last_verified_at, valid_until, freshness_state,
                       surface_count, helped_count
                FROM knowledge_card_metrics
                WHERE valid_until IS NOT NULL AND valid_until != ''
                  AND valid_until < ?
                  AND COALESCE(helped_count, 0) = 0
                  AND freshness_state NOT IN ('archived', 'superseded')
                ORDER BY valid_until ASC
                """,
                (cutoff_ymd,),
            ).fetchall()
        return [dict(r) for r in rows]

    def auto_archive_stale_zero_help_cards(
        self, today_ymd: str, *, grace_days: int = 30
    ) -> list[dict[str, Any]]:
        """Auto-archive stale zero-help cards into freshness_state='archived' and return the list."""
        cutoff_ymd = (date.fromisoformat(today_ymd) - timedelta(days=grace_days)).isoformat()
        now = self._now_iso()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT doc_path, freshness_tier, last_verified_at, valid_until, freshness_state,
                       surface_count, helped_count
                FROM knowledge_card_metrics
                WHERE valid_until IS NOT NULL AND valid_until != ''
                  AND valid_until < ?
                  AND COALESCE(helped_count, 0) = 0
                  AND freshness_state NOT IN ('archived', 'superseded')
                ORDER BY valid_until ASC
                """,
                (cutoff_ymd,),
            ).fetchall()
            cards = [dict(r) for r in rows]
            if cards:
                doc_paths = [c["doc_path"] for c in cards]
                placeholders = ",".join("?" for _ in doc_paths)
                conn.execute(
                    f"""
                    UPDATE knowledge_card_metrics
                    SET freshness_state = 'archived', updated_at = ?
                    WHERE doc_path IN ({placeholders})
                    """,
                    (now, *doc_paths),
                )
                for c in cards:
                    c["freshness_state"] = "archived"
            return cards

    def update_knowledge_freshness_action(
        self,
        doc_path: str,
        action: str,
        *,
        today_ymd: str,
        freshness_tier: str | None = None,
    ) -> dict[str, Any]:
        """Update knowledge freshness ledger atomically for portal review.
        Actions: verify, archive, restore, set_tier.
        Strictly modifies SQLite ledger only; never touches markdown files on disk.
        """
        if not doc_path:
            raise ValueError("doc_path is required")
        act = action.strip().lower()
        if act not in {"verify", "archive", "restore", "set_tier"}:
            raise ValueError(f"Invalid action '{action}'. Must be one of verify, archive, restore, set_tier")

        now = self._now_iso()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT doc_path, freshness_tier, last_verified_at, valid_until, freshness_state,
                       surface_count, helped_count
                FROM knowledge_card_metrics
                WHERE doc_path = ?
                """,
                (doc_path,),
            ).fetchone()
            curr = dict(row) if row else {
                "doc_path": doc_path,
                "freshness_tier": "evolving",
                "last_verified_at": today_ymd,
                "valid_until": compute_valid_until("evolving", today_ymd),
                "freshness_state": "fresh",
                "surface_count": 0,
                "helped_count": 0,
            }

            tier = curr.get("freshness_tier") or "evolving"
            if freshness_tier and freshness_tier in {"volatile", "evolving", "evergreen"}:
                tier = freshness_tier

            last_v = curr.get("last_verified_at") or today_ymd
            valid_u = curr.get("valid_until") or compute_valid_until(tier, last_v)
            state = curr.get("freshness_state") or "fresh"

            if act == "verify":
                last_v = today_ymd
                valid_u = compute_valid_until(tier, today_ymd)
                state = "fresh"
            elif act == "archive":
                state = "archived"
            elif act == "restore":
                state = "fresh"
                last_v = today_ymd
                valid_u = compute_valid_until(tier, today_ymd)
            elif act == "set_tier":
                if not freshness_tier or freshness_tier not in {"volatile", "evolving", "evergreen"}:
                    raise ValueError(f"Invalid freshness_tier '{freshness_tier}' for set_tier")
                tier = freshness_tier
                valid_u = compute_valid_until(tier, last_v)
                if state == "needs_recheck" and valid_u >= today_ymd:
                    state = "fresh"

            conn.execute(
                """
                INSERT INTO knowledge_card_metrics (
                    doc_path, freshness_tier, last_verified_at, valid_until, freshness_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_path) DO UPDATE SET
                    freshness_tier = excluded.freshness_tier,
                    last_verified_at = excluded.last_verified_at,
                    valid_until = excluded.valid_until,
                    freshness_state = excluded.freshness_state,
                    updated_at = excluded.updated_at
                """,
                (doc_path, tier, last_v, valid_u, state, now, now),
            )

            is_expired = bool(
                state == "needs_recheck"
                or (valid_u and valid_u < today_ymd and state not in {"archived", "superseded"})
            )
            return {
                "doc_path": doc_path,
                "freshness_tier": tier,
                "last_verified_at": last_v,
                "valid_until": valid_u,
                "freshness_state": state,
                "is_expired": is_expired,
                "surface_count": int(curr.get("surface_count") or 0),
                "helped_count": int(curr.get("helped_count") or 0),
            }

    def get_knowledge_freshness_map(self, doc_paths: list[str] | None = None) -> dict[str, dict[str, Any]]:
        today_ymd = shanghai_today_ymd()
        with self.connect() as conn:
            if doc_paths:
                placeholders = ",".join("?" for _ in doc_paths)
                rows = conn.execute(
                    f"""
                    SELECT doc_path, freshness_tier, last_verified_at, valid_until, freshness_state,
                           surface_count, helped_count
                    FROM knowledge_card_metrics
                    WHERE doc_path IN ({placeholders})
                    """,
                    tuple(doc_paths),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT doc_path, freshness_tier, last_verified_at, valid_until, freshness_state,
                           surface_count, helped_count
                    FROM knowledge_card_metrics
                    """
                ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            item = dict(r)
            valid_until = item.get("valid_until") or ""
            is_expired = bool(
                item.get("freshness_state") == "needs_recheck"
                or (valid_until and valid_until < today_ymd and item.get("freshness_state") not in {"archived", "superseded"})
            )
            item["is_expired"] = is_expired
            out[item["doc_path"]] = item
        return out

    def get_knowledge_card_metrics(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT doc_path, surface_count, cited_count, helped_count,
                       last_surfaced_at, last_helped_at,
                       freshness_tier, last_verified_at, valid_until, freshness_state
                FROM knowledge_card_metrics
                ORDER BY helped_count DESC, surface_count DESC, doc_path
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_metrics_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        now = self._now_iso()
        period_key = str(snapshot["period_key"])
        snap_id = snapshot.get("snapshot_id") or f"snap-{period_key}"
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO flywheel_metrics_snapshots (
                    snapshot_id, period_key, red_count, yellow_count, green_count,
                    repeat_mistake_rate, self_catch_rate, active_rule_count, dormant_rule_count,
                    open_dissent_count, expired_card_count, stale_skill_count, sample_sufficient,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(period_key) DO UPDATE SET
                    red_count = excluded.red_count,
                    yellow_count = excluded.yellow_count,
                    green_count = excluded.green_count,
                    repeat_mistake_rate = excluded.repeat_mistake_rate,
                    self_catch_rate = excluded.self_catch_rate,
                    active_rule_count = excluded.active_rule_count,
                    dormant_rule_count = excluded.dormant_rule_count,
                    open_dissent_count = excluded.open_dissent_count,
                    expired_card_count = excluded.expired_card_count,
                    stale_skill_count = excluded.stale_skill_count,
                    sample_sufficient = excluded.sample_sufficient,
                    updated_at = excluded.updated_at
                """,
                (
                    snap_id,
                    period_key,
                    int(snapshot.get("red_count", 0)),
                    int(snapshot.get("yellow_count", 0)),
                    int(snapshot.get("green_count", 0)),
                    float(snapshot.get("repeat_mistake_rate", 0.0)),
                    float(snapshot.get("self_catch_rate", 0.0)),
                    int(snapshot.get("active_rule_count", 0)),
                    int(snapshot.get("dormant_rule_count", 0)),
                    int(snapshot.get("open_dissent_count", 0)),
                    int(snapshot.get("expired_card_count", 0)),
                    int(snapshot.get("stale_skill_count", 0)),
                    1 if snapshot.get("sample_sufficient") else 0,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM flywheel_metrics_snapshots WHERE period_key = ?",
                (period_key,),
            ).fetchone()
            return dict(row) if row else {}

    def get_latest_metrics_snapshot(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM flywheel_metrics_snapshots ORDER BY period_key DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def list_metrics_snapshots(self, limit: int = 12) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM flywheel_metrics_snapshots ORDER BY period_key DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def compute_live_growth_metrics(self, today_ymd: str | None = None) -> dict[str, Any]:
        today = today_ymd or shanghai_today_ymd()
        stale_cutoff = (self.now_datetime() - timedelta(days=21)).isoformat()
        with self.connect() as conn:
            obs_rows = conn.execute(
                "SELECT severity_grade, occurrence_count, caught_by, status FROM mistake_observations WHERE status != 'archived'"
            ).fetchall()
            red_count = sum(1 for r in obs_rows if r["severity_grade"] == "red_silent")
            yellow_count = sum(1 for r in obs_rows if r["severity_grade"] == "yellow_caught")
            green_count = sum(1 for r in obs_rows if r["severity_grade"] == "green_minor")
            total_obs = len(obs_rows)
            repeat_obs = sum(1 for r in obs_rows if int(r["occurrence_count"] or 1) >= 2)
            self_caught = sum(1 for r in obs_rows if r["caught_by"] in {"self", "tool"})

            active_rule_count = int(
                conn.execute(
                    "SELECT COUNT(1) AS c FROM rules WHERE status IN ('verified_active', 'unverified_active')"
                ).fetchone()["c"]
            )
            dormant_rule_count = int(
                conn.execute(
                    "SELECT COUNT(1) AS c FROM rules WHERE status = 'dormant'"
                ).fetchone()["c"]
            )
            open_dissent_count = int(
                conn.execute(
                    "SELECT COUNT(1) AS c FROM dissent_records WHERE status = 'open'"
                ).fetchone()["c"]
            )
            expired_card_count = int(
                conn.execute(
                    """
                    SELECT COUNT(1) AS c FROM knowledge_card_metrics
                    WHERE freshness_state = 'needs_recheck'
                       OR (valid_until IS NOT NULL AND valid_until != '' AND valid_until < ? AND freshness_state != 'archived')
                    """,
                    (today,),
                ).fetchone()["c"]
            )
            stale_skill_count = int(
                conn.execute(
                    """
                    SELECT COUNT(1) AS c FROM skill_assets
                    WHERE (origin = 'flywheel' AND invoke_count = 0 AND created_at < ?)
                       OR fail_count >= 3
                    """,
                    (stale_cutoff,),
                ).fetchone()["c"]
            )
            observing_pool_count = sum(1 for r in obs_rows if r["status"] == "observing")

        return {
            "red_count": red_count,
            "yellow_count": yellow_count,
            "green_count": green_count,
            "total_mistakes": total_obs,
            "observing_pool_count": observing_pool_count,
            "repeat_mistake_rate": round(repeat_obs / total_obs, 4) if total_obs > 0 else 0.0,
            "self_catch_rate": round(self_caught / total_obs, 4) if total_obs > 0 else 0.0,
            "active_rule_count": active_rule_count,
            "dormant_rule_count": dormant_rule_count,
            "open_dissent_count": open_dissent_count,
            "expired_card_count": expired_card_count,
            "stale_skill_count": stale_skill_count,
            "sample_sufficient": total_obs >= 5,
        }
