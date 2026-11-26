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


def test_dream_auto_archives_stale_zero_help_cards_only(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    now_utc = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    # Card A: valid_until is 40 days ago (< cutoff 30 days), helped_count = 0 -> must auto-archive
    rt.db.upsert_knowledge_freshness(
        doc_path="projects/card-a.md",
        freshness_tier="evolving",
        last_verified_at="2026-01-01",
        valid_until="2026-04-20",
        freshness_state="needs_recheck",
    )
    # Card B: valid_until is 40 days ago, but helped_count = 2 -> protected from auto-archive
    rt.db.upsert_knowledge_freshness(
        doc_path="projects/card-b.md",
        freshness_tier="evolving",
        last_verified_at="2026-01-01",
        valid_until="2026-04-20",
        freshness_state="needs_recheck",
    )
    rt.db.record_knowledge_usage(["projects/card-b.md"], "2026-04-01")
    rt.db.record_knowledge_usage(["projects/card-b.md"], "2026-04-15")

    # Card C: valid_until is only 10 days ago (> cutoff 30 days), helped_count = 0 -> within grace, not auto-archived
    rt.db.upsert_knowledge_freshness(
        doc_path="projects/card-c.md",
        freshness_tier="evolving",
        last_verified_at="2026-03-01",
        valid_until="2026-05-22",
        freshness_state="needs_recheck",
    )

    # Create dummy files on disk to verify zero byte modification
    kb_dir = Path(rt.cfg.knowledge_sink.root)
    (kb_dir / "projects").mkdir(parents=True, exist_ok=True)
    card_a_file = kb_dir / "projects" / "card-a.md"
    card_b_file = kb_dir / "projects" / "card-b.md"
    card_a_bytes = b"# Card A\nImmutable content\n"
    card_b_bytes = b"# Card B\nImmutable content\n"
    card_a_file.write_bytes(card_a_bytes)
    card_b_file.write_bytes(card_b_bytes)

    # 1. Preview: strictly read-only
    preview = preview_dream_consolidation(rt.cfg, rt.db, now_utc=now_utc)
    preview_archived_paths = [c["doc_path"] for c in preview["auto_archived_cards"]]
    assert "projects/card-a.md" in preview_archived_paths
    assert "projects/card-b.md" not in preview_archived_paths
    assert "projects/card-c.md" not in preview_archived_paths

    preview_expired_paths = [c["doc_path"] for c in preview["expired_cards"]]
    assert "projects/card-a.md" not in preview_expired_paths
    assert "projects/card-b.md" in preview_expired_paths
    assert "projects/card-c.md" in preview_expired_paths

    # Confirm zero database modification during preview
    fmap_pre = rt.db.get_knowledge_freshness_map()
    assert fmap_pre["projects/card-a.md"]["freshness_state"] == "needs_recheck"

    # 2. Run Dream
    res = run_dream_consolidation(rt.cfg, rt.db, now_utc=now_utc)
    archived_paths = [c["doc_path"] for c in res["auto_archived_cards"]]
    assert archived_paths == ["projects/card-a.md"]

    expired_paths = [c["doc_path"] for c in res["expired_cards"]]
    assert "projects/card-a.md" not in expired_paths
    assert "projects/card-b.md" in expired_paths
    assert "projects/card-c.md" in expired_paths

    fmap_post = rt.db.get_knowledge_freshness_map()
    assert fmap_post["projects/card-a.md"]["freshness_state"] == "archived"
    assert fmap_post["projects/card-b.md"]["freshness_state"] == "needs_recheck"
    assert fmap_post["projects/card-c.md"]["freshness_state"] == "needs_recheck"

    # Confirm audit event was recorded
    audit_events = rt.db.list_audit_events(entity_type="knowledge_card", limit=10)
    assert any(e["event_type"] == "knowledge_freshness_auto_archived" for e in audit_events)

    # Confirm zero disk file changes
    assert card_a_file.read_bytes() == card_a_bytes
    assert card_b_file.read_bytes() == card_b_bytes


def test_knowledge_freshness_actions_api_and_file_preservation(tmp_path: Path) -> None:
    rt = _make_runtime(tmp_path)
    kb_dir = Path(rt.cfg.knowledge_sink.root)
    doc_path = "fresh-doc.md"
    doc_file = kb_dir / doc_path
    original_bytes = doc_file.read_bytes()

    app = create_app(runtime=rt)
    client = TestClient(app)

    # Initial state: doc is in kb, freshness entry created with volatile (30d) in past
    rt.db.upsert_knowledge_freshness(
        doc_path=doc_path,
        freshness_tier="volatile",
        last_verified_at="2025-01-01",
        valid_until="2025-01-31",
        freshness_state="needs_recheck",
    )

    # 1. Action: verify -> renews validity and state to fresh
    res = client.post(
        "/api/site/knowledge/freshness",
        json={"doc_path": doc_path, "action": "verify"},
    )
    assert res.status_code == 200
    data = res.json()
    card = data["card"]
    assert card["freshness_state"] == "fresh"
    assert card["is_expired"] is False
    assert card["last_verified_at"] is not None
    assert doc_file.read_bytes() == original_bytes

    # 2. Action: set_tier -> change tier to evergreen (180d)
    res = client.post(
        "/api/site/knowledge/freshness",
        json={"doc_path": doc_path, "action": "set_tier", "freshness_tier": "evergreen"},
    )
    assert res.status_code == 200
    card = res.json()["card"]
    assert card["freshness_tier"] == "evergreen"
    assert doc_file.read_bytes() == original_bytes

    # 3. Action: archive -> moves out of needs_recheck
    res = client.post(
        "/api/site/knowledge/freshness",
        json={"doc_path": doc_path, "action": "archive"},
    )
    assert res.status_code == 200
    card = res.json()["card"]
    assert card["freshness_state"] == "archived"
    assert card["is_expired"] is False
    assert doc_file.read_bytes() == original_bytes

    # GET /api/site/knowledge returns the archived card
    list_res = client.get("/api/site/knowledge")
    assert list_res.status_code == 200
    docs = list_res.json()["documents"]
    archived_doc = next(d for d in docs if d["doc_path"] == doc_path)
    assert archived_doc["freshness_state"] == "archived"

    # 4. Action: restore -> brings back to active tracking
    res = client.post(
        "/api/site/knowledge/freshness",
        json={"doc_path": doc_path, "action": "restore"},
    )
    assert res.status_code == 200
    card = res.json()["card"]
    assert card["freshness_state"] == "fresh"
    assert card["is_expired"] is False
    assert doc_file.read_bytes() == original_bytes

    # 5. Validation errors
    assert client.post("/api/site/knowledge/freshness", json={"doc_path": "", "action": "verify"}).status_code == 422
    assert client.post("/api/site/knowledge/freshness", json={"doc_path": doc_path, "action": "invalid_act"}).status_code == 422
    assert client.post("/api/site/knowledge/freshness", json={"doc_path": doc_path, "action": "set_tier", "freshness_tier": "invalid_tier"}).status_code == 422

