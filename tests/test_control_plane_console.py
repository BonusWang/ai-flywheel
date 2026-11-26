"""Tests for Control Plane Console governance endpoints and read-only Dream preview."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import CreateRuleRequest, RuleStatus
from ai_flywheel.runtime import FlywheelRuntime


def _make_runtime(tmp_path: Path) -> FlywheelRuntime:
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.server.data_dir = str(tmp_path / "data")
    cfg.knowledge_sink.type = "builtin_kb"
    cfg.knowledge_sink.root = str(tmp_path / "data" / "knowledge")
    cfg.sessions.scan_sources = []
    cfg.workflow.schedule_enabled = False
    cfg.runner.mode = "dry_run"
    return FlywheelRuntime(cfg, skip_fts_sync=True, process_role="cli")


def test_site_skills_and_knowledge_freshness_enrichment(tmp_path: Path):
    rt = _make_runtime(tmp_path)

    # Seed one skill asset and record usage (1 pass + 3 fails -> failing health_state)
    rt.db.upsert_skill_asset(
        skill_name="demo-audit-skill",
        source_dir=str(tmp_path / "skills"),
        description="Demo skill for audit ledger",
        origin="flywheel_created",
    )
    rt.db.record_skill_usage(["demo-audit-skill"], passed=True)
    for _ in range(3):
        rt.db.record_skill_usage(["demo-audit-skill"], passed=False)

    # Write one knowledge doc and mark it expired in knowledge_card_metrics
    doc_path = "lessons/expired-demo-card.md"
    target = rt.knowledge.root / doc_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Expired Demo Card\n\nOld procedure that must be rechecked.\n",
        encoding="utf-8",
    )
    rt.db.upsert_knowledge_freshness(
        doc_path=doc_path,
        freshness_tier="volatile",
        last_verified_at="2025-01-01",
        valid_until="2025-01-31",
        freshness_state="fresh",
    )

    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        sk_res = client.get("/api/site/skills")
        assert sk_res.status_code == 200
        sk_data = sk_res.json()
        assert sk_data["probed_count"] == 0
        matched = [s for s in sk_data["skills"] if s["skill_name"] == "demo-audit-skill"]
        assert len(matched) == 1
        assert matched[0]["invoke_count"] == 4
        assert matched[0]["verified_pass_count"] == 1
        assert matched[0]["fail_count"] == 3
        assert matched[0]["health_state"] == "failing"

        kb_res = client.get("/api/site/knowledge")
        assert kb_res.status_code == 200
        docs = kb_res.json()["documents"]
        card = next(d for d in docs if d["doc_path"] == doc_path)
        assert card["title"] == "Expired Demo Card"
        assert card["freshness_tier"] == "volatile"
        assert card["valid_until"] == "2025-01-31"
        assert card["is_expired"] is True
        assert card["helped_count"] == 0


def test_dream_preview_is_strictly_read_only_and_explicit_run_persists(tmp_path: Path):
    rt = _make_runtime(tmp_path)

    # Create a stale trial rule (20 days old, 0 hits) and an expired card
    rule = rt.activation.register_rule(
        CreateRuleRequest(
            title="Stale trial rule for dream test",
            domain="engineering",
            triggers=["deploy", "release"],
            checklist=["Verify version"],
        )
    )
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    with rt.db.connect() as conn:
        conn.execute(
            "UPDATE rules SET created_at = ?, hit_count = 0 WHERE rule_id = ?",
            (old_ts, rule.rule_id),
        )

    recent_until = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    recent_verified = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d")
    rt.db.upsert_knowledge_freshness(
        doc_path="lessons/stale-card.md",
        freshness_tier="volatile",
        last_verified_at=recent_verified,
        valid_until=recent_until,
        freshness_state="fresh",
    )

    app = create_app(runtime=rt, process_role="cli")
    with TestClient(app) as client:
        # 1. GET /api/site/dream — read-only preview
        get_res = client.get("/api/site/dream")
        assert get_res.status_code == 200
        body = get_res.json()
        assert body["latest_event"] is None
        assert body["preview"]["dry_run"] is True
        assert any(r["rule_id"] == rule.rule_id for r in body["preview"]["auto_dormant_rules"])
        assert any(c["doc_path"] == "lessons/stale-card.md" for c in body["preview"]["expired_cards"])

        # 2. POST /api/site/dream/run with dry_run=True — still zero DB side effects
        dry_res = client.post("/api/site/dream/run", json={"dry_run": True})
        assert dry_res.status_code == 200
        assert dry_res.json()["result"]["dry_run"] is True
        assert dry_res.json()["latest_event"] is None
        still_active = rt.db.get_rule(rule.rule_id)
        assert still_active is not None
        assert still_active.status == RuleStatus.UNVERIFIED_ACTIVE
        assert rt.db.list_audit_events(entity_type="dream_consolidation", limit=5) == []

        # 3. POST /api/site/dream/run with dry_run=False — persists changes & audit & core card
        run_res = client.post("/api/site/dream/run", json={"dry_run": False})
        assert run_res.status_code == 200
        run_body = run_res.json()
        assert run_body["result"]["dry_run"] is False
        assert run_body["latest_event"] is not None
        assert run_body["latest_event"]["event_type"] == "dream_completed"
        demoted = rt.db.get_rule(rule.rule_id)
        assert demoted is not None
        assert demoted.status == RuleStatus.DORMANT
        freshness = rt.db.get_knowledge_freshness_map(["lessons/stale-card.md"])
        assert freshness["lessons/stale-card.md"]["freshness_state"] == "needs_recheck"
        assert (rt.cfg.data_path / "FLYWHEEL-CORE.md").exists()
