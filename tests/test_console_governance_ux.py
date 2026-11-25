"""Tests for console governance UX, bulk actions, skills/freshness alignment, and dream auto-archive."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.core.dream import preview_dream_consolidation, run_dream_consolidation
from ai_flywheel.models import CreateRuleRequest, ReportSignalRequest, SignalKind
from ai_flywheel.runtime import FlywheelRuntime


def _make_runtime(tmp_path: Path) -> FlywheelRuntime:
    skills_dir = tmp_path / "local-skills"
    (skills_dir / "alpha-skill").mkdir(parents=True)
    (skills_dir / "alpha-skill" / "SKILL.md").write_text(
        "---\nname: alpha-skill\ndescription: Alpha local helper\n---\n# Alpha\n",
        encoding="utf-8",
    )
    (skills_dir / "beta-skill").mkdir(parents=True)
    (skills_dir / "beta-skill" / "SKILL.md").write_text(
        "---\nname: beta-skill\ndescription: Beta local helper\n---\n# Beta\n",
        encoding="utf-8",
    )

    kb_dir = tmp_path / "kb"
    kb_dir.mkdir(parents=True)
    (kb_dir / "fresh-doc.md").write_text("# Fresh Doc\ncontent\n", encoding="utf-8")

    cfg = FlywheelConfig()
    cfg.base_dir = tmp_path
    cfg.server.data_dir = str(tmp_path / "data")
    cfg.capabilities.skills_dirs = [str(skills_dir)]
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(kb_dir)
    cfg.knowledge_sink.wiki_gate_enabled = False
    cfg.workflow.schedule_enabled = False
    return FlywheelRuntime(cfg)


def test_skills_api_merges_locally_probed_skills(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    # Add a third skill after runtime init (not yet in skill_assets table)
    gamma_dir = tmp_path / "local-skills" / "gamma-skill"
    gamma_dir.mkdir(parents=True)
    (gamma_dir / "SKILL.md").write_text(
        "---\nname: gamma-skill\ndescription: Gamma live probed skill\n---\n# Gamma\n",
        encoding="utf-8",
    )
    app = create_app(runtime=rt)
    client = TestClient(app)

    res = client.get("/api/site/skills")
    assert res.status_code == 200
    body = res.json()
    assert body["probed_count"] == 3
    names = [s["skill_name"] for s in body["skills"]]
    assert names == ["alpha-skill", "beta-skill", "gamma-skill"]
    gamma = next(s for s in body["skills"] if s["skill_name"] == "gamma-skill")
    assert gamma["origin"] == "local_probed"
    assert gamma["description"] == "Gamma live probed skill"


def test_knowledge_api_includes_expired_cards_beyond_limit(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    # Track an expired card in SQLite that is not among the top `limit=1` filesystem files
    rt.db.upsert_knowledge_freshness(
        doc_path="projects/old-card.md",
        freshness_tier="volatile",
        last_verified_at="2025-01-01",
        valid_until="2025-02-01",
        freshness_state="needs_recheck",
    )
    app = create_app(runtime=rt)
    client = TestClient(app)

    res = client.get("/api/site/knowledge?limit=1")
    assert res.status_code == 200
    body = res.json()
    assert body["expired_total"] >= 1
    paths = [d["doc_path"] for d in body["documents"]]
    assert "projects/old-card.md" in paths
    old_card = next(d for d in body["documents"] if d["doc_path"] == "projects/old-card.md")
    assert old_card["is_expired"] is True


def test_dissents_enriched_and_bulk_governance_endpoints(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    rule = rt.activation.register_rule(
        CreateRuleRequest(
            title="Never skip tests",
            domain="engineering",
            triggers=["pytest"],
            checklist=["Run pytest"],
        )
    )
    d1 = rt.db.add_dissent_record(
        target_type="rule",
        target_id=rule.rule_id,
        counter_claim="Docs-only edits do not need full pytest",
        condition_scope="docs-only",
    )
    d2 = rt.db.add_dissent_record(
        target_type="rule",
        target_id=rule.rule_id,
        counter_claim="Draft scratch files skip pytest",
    )
    obs1, _ = rt.db.upsert_mistake_observation(
        root_cause_key="minor-typo",
        severity_grade="green_minor",
        title="Minor typo",
    )
    obs2, _ = rt.db.upsert_mistake_observation(
        root_cause_key="second-minor",
        severity_grade="yellow_caught",
        title="Second minor",
    )
    r1, _ = rt.receipts.report_signal(
        ReportSignalRequest(client="codex", kind=SignalKind.RETRY_LOOP, summary="Loop A")
    )
    r2, _ = rt.receipts.report_signal(
        ReportSignalRequest(client="codex", kind=SignalKind.RETRY_LOOP, summary="Loop B")
    )

    app = create_app(runtime=rt)
    client = TestClient(app)

    # Dissents include target_title & target_status
    res = client.get("/api/site/dissents")
    assert res.status_code == 200
    dissents = res.json()["dissents"]
    assert len(dissents) == 2
    assert dissents[0]["target_title"] == "Never skip tests"
    assert dissents[0]["target_status"] == rule.status.value

    # Bulk update dissents
    res = client.post(
        "/api/site/dissents/bulk-status",
        json={"dissent_ids": [d1["dissent_id"], d2["dissent_id"]], "status": "merged"},
    )
    assert res.status_code == 200
    assert res.json()["updated"] == 2
    assert all(d["status"] == "merged" for d in client.get("/api/site/dissents").json()["dissents"])

    # Bulk archive observations
    res = client.post(
        "/api/site/observations/bulk-archive",
        json={"obs_ids": [obs1["obs_id"], obs2["obs_id"]]},
    )
    assert res.status_code == 200
    assert res.json()["updated"] == 2
    assert len(client.get("/api/site/observations?status=observing").json()["observations"]) == 0

    # Bulk restore observations (Undo flow)
    res = client.post(
        "/api/site/observations/bulk-restore",
        json={"obs_ids": [obs1["obs_id"], obs2["obs_id"]]},
    )
    assert res.status_code == 200
    assert res.json()["updated"] == 2
    assert len(client.get("/api/site/observations?status=observing").json()["observations"]) == 2

    # Single archive then restore observation
    res = client.post(f"/api/site/observations/{obs1['obs_id']}/archive")
    assert res.status_code == 200
    assert res.json()["observation"]["status"] == "archived"
    # Archiving an already-archived observation returns 409
    assert client.post(f"/api/site/observations/{obs1['obs_id']}/archive").status_code == 409
    res = client.post(f"/api/site/observations/{obs1['obs_id']}/restore")
    assert res.status_code == 200
    assert res.json()["observation"]["status"] == "observing"
    # Restoring a non-archived (observing or promoted) observation returns 409
    assert client.post(f"/api/site/observations/{obs1['obs_id']}/restore").status_code == 409
    assert client.post(f"/api/site/observations/{obs1['obs_id']}/promote").status_code == 200
    assert client.post(f"/api/site/observations/{obs1['obs_id']}/restore").status_code == 409
    assert client.post("/api/site/observations/missing-obs-id/restore").status_code == 404

    # Bulk update retro items
    res = client.post(
        "/api/site/retro/bulk-status",
        json={"retro_ids": [r1.retro_id, r2.retro_id], "status": "dismissed"},
    )
    assert res.status_code == 200
    assert res.json()["updated"] == 2
    assert len(client.get("/api/site/retro?status=open").json()["items"]) == 0


def test_dream_auto_archives_stale_low_severity_only(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()

    low_obs, _ = rt.db.upsert_mistake_observation(
        root_cause_key="stale-low-one",
        severity_grade="yellow_caught",
        title="Stale low severity single hit",
    )
    red_obs, _ = rt.db.upsert_mistake_observation(
        root_cause_key="stale-red-silent",
        severity_grade="red_silent",
        title="Stale red silent must stay observing",
    )
    with rt.db.connect() as conn:
        conn.execute(
            "UPDATE mistake_observations SET updated_at = ?, created_at = ? WHERE obs_id IN (?, ?)",
            (old_ts, old_ts, low_obs["obs_id"], red_obs["obs_id"]),
        )

    preview = preview_dream_consolidation(rt.cfg, rt.db)
    preview_ids = [o["obs_id"] for o in preview["auto_archived_observations"]]
    assert low_obs["obs_id"] in preview_ids
    assert red_obs["obs_id"] not in preview_ids

    result = run_dream_consolidation(rt.cfg, rt.db)
    archived_ids = [o["obs_id"] for o in result["auto_archived_observations"]]
    assert archived_ids == [low_obs["obs_id"]]
    assert rt.db.get_mistake_observation(low_obs["obs_id"])["status"] == "archived"
    assert rt.db.get_mistake_observation(red_obs["obs_id"])["status"] == "observing"
