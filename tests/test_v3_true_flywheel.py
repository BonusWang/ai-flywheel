from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_flywheel.adapters.runners import OpenAICompatibleRunner, _clean_title
from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig, load_config
from ai_flywheel.core.core_card import MAX_BYTES, MAX_LINES, rebuild_core_card
from ai_flywheel.core.dream import run_dream_consolidation
from ai_flywheel.core.privacy import SecretLeakError
from ai_flywheel.models import (
    CheckResult,
    CreateRuleRequest,
    OutcomeCheck,
    OutcomeOrigin,
    OutcomeStatus,
    PreflightRequest,
    RecordOutcomeRequest,
    RegisterSessionRequest,
    RuleRecord,
    RuleStatus,
    RunStatus,
    VerificationKind,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.db import FlywheelDB


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def rt(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    cfg = load_config(base_dir=tmp_path)
    cfg.workflow.approval_mode = "auto"
    # `auto` only takes effect with the safety valve open: the valve has
    # the last word, so a closed valve would run the batch as `smart`.
    cfg.safety_valves.auto_approve_writes = True
    return FlywheelRuntime(cfg)


def test_v2_to_v3_migration_auto_backup_and_audit_columns(tmp_path: Path):
    db_path = tmp_path / "flywheel.db"
    # Seed a minimal v2-shaped database (version 2 recorded, no v3 tables/columns)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
        INSERT INTO schema_migrations VALUES (2, 'v2_normalized_audit_and_decisions', '2026-11-23T00:00:00Z');
        CREATE TABLE rules (
            rule_id TEXT PRIMARY KEY, title TEXT NOT NULL, domain TEXT NOT NULL DEFAULT 'general',
            projects_json TEXT NOT NULL DEFAULT '["generic"]', triggers_json TEXT NOT NULL DEFAULT '[]',
            checklist_json TEXT NOT NULL DEFAULT '[]', forbidden_routes_json TEXT NOT NULL DEFAULT '[]',
            applicable_skills_json TEXT NOT NULL DEFAULT '[]', source_pointers_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'unverified_active', version INTEGER NOT NULL DEFAULT 1,
            origin_session_key TEXT, hit_count INTEGER NOT NULL DEFAULT 0, natural_pass_count INTEGER NOT NULL DEFAULT 0,
            recurrence_count INTEGER NOT NULL DEFAULT 0, revalidation_history_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE skill_assets (
            skill_name TEXT PRIMARY KEY, source_dir TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT 'manual', sha256 TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
            last_run_id TEXT, backup_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE knowledge_card_metrics (
            doc_path TEXT PRIMARY KEY, surface_count INTEGER NOT NULL DEFAULT 0,
            cited_count INTEGER NOT NULL DEFAULT 0, helped_count INTEGER NOT NULL DEFAULT 0,
            last_surfaced_at TEXT, last_helped_at TEXT,
            created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE outcomes (
            outcome_id TEXT PRIMARY KEY, receipt_hash TEXT NOT NULL, session_key TEXT NOT NULL,
            client TEXT NOT NULL, preflight_receipt_id TEXT, origin TEXT NOT NULL, status TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '', checks_json TEXT NOT NULL, applied_rule_ids_json TEXT NOT NULL,
            artifacts_json TEXT NOT NULL, revalidation_effects_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.commit()
    conn.close()

    db = FlywheelDB(db_path)
    assert db_path.with_name("flywheel.db.bak-v2").exists()
    with db.connect() as c:
        vers = {r["version"] for r in c.execute("SELECT version FROM schema_migrations").fetchall()}
        assert 3 in vers
        for tbl in ("mistake_observations", "dissent_records", "flywheel_metrics_snapshots"):
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()}
            assert {"created_at", "updated_at"}.issubset(cols)
        rule_cols = {r["name"] for r in c.execute("PRAGMA table_info(rules)").fetchall()}
        assert {"last_hit_at", "dormant_at", "root_cause_key"}.issubset(rule_cols)
        km_cols = {r["name"] for r in c.execute("PRAGMA table_info(knowledge_card_metrics)").fetchall()}
        assert {"freshness_tier", "last_verified_at", "valid_until", "freshness_state"}.issubset(km_cols)


def test_write_time_privacy_redaction_and_secret_block(rt: FlywheelRuntime):
    # 1. <private> blocks are cleanly redacted before writing
    res = rt.knowledge.write_document_verified(
        "lessons/2026-11-23-privacy-test.md",
        "---\ntitle: 脱敏测试\nfreshness_tier: volatile\n---\n\n正文保留\n<private>机密内部IP 10.0.0.1</private>\n结尾保留\n",
    )
    saved = rt.knowledge.read_document(res["relative_path"])
    assert saved is not None
    assert "机密内部IP" not in saved["content"]
    assert "[REDACTED_PRIVATE]" in saved["content"]
    assert res["freshness_tier"] == "volatile"

    # 2. High-confidence secrets are hard-blocked (never written to disk)
    fake_secret = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4"
    with pytest.raises(SecretLeakError):
        rt.knowledge.write_document_verified(
            "lessons/2026-11-23-leak.md",
            f"# Leak\nAPI_KEY={fake_secret}\n",
        )
    assert rt.knowledge.read_document("lessons/2026-11-23-leak.md") is None

    with pytest.raises(SecretLeakError):
        rt.skill_writer.write_skill_verified(
            "bad-skill/SKILL.md",
            f"---\nname: bad-skill\ndescription: test\n---\n\nToken: ghp_{'A' * 36}\n",
        )


def test_two_track_mistake_promotion_rule_of_three_and_red_fast_track(rt: FlywheelRuntime):
    # First 2 occurrences of a yellow_caught mistake -> stay in observation pool, NO rule created
    for i in (1, 2):
        rt.register_session(
            RegisterSessionRequest(
                client="codex",
                session_id=f"yellow-{i}",
                title="参数校验报错修复",
                project="demo",
                summary=f"第 {i} 次自查发现报错并修复",
                content="报错后本地测试自查修复",
                metadata={"root_cause_key": "missing-null-guard"},
            )
        )
        run = rt.orchestrator.start_run(trigger="test", phase="daily", auto_approve_override=True)
        assert run.status == RunStatus.PASS

    obs_list = rt.db.list_mistake_observations()
    target_obs = next(o for o in obs_list if o["root_cause_key"] == "missing-null-guard")
    assert target_obs["occurrence_count"] == 2
    assert target_obs["status"] == "observing"
    assert len(rt.db.list_rules()) == 0

    # 3rd occurrence -> meets "事不过三" -> promotes to flywheel_rule!
    rt.register_session(
        RegisterSessionRequest(
            client="codex",
            session_id="yellow-3",
            title="参数校验报错修复",
            project="demo",
            summary="第 3 次同样根因报错",
            content="第三次遇到同样报错并修复",
            metadata={"root_cause_key": "missing-null-guard"},
        )
    )
    run3 = rt.orchestrator.start_run(trigger="test", phase="daily", auto_approve_override=True)
    assert run3.status == RunStatus.PASS
    assert len(rt.db.list_rules()) == 1
    promoted = rt.db.get_mistake_observation(target_obs["obs_id"])
    assert promoted["status"] == "promoted"
    assert promoted["promoted_rule_id"] is not None

    # Red silent mistake -> promotes immediately on occurrence #1!
    rt.register_session(
        RegisterSessionRequest(
            client="claude-code",
            session_id="red-1",
            title="线上静默失败漏检事故",
            project="demo",
            summary="用户发现静默失败导致数据丢失",
            content="线上静默失败，未校验返回值",
            metadata={"root_cause_key": "silent-swallow-error"},
        )
    )
    run4 = rt.orchestrator.start_run(trigger="test", phase="daily", auto_approve_override=True)
    assert run4.status == RunStatus.PASS
    assert len(rt.db.list_rules()) == 2


def test_dissent_preservation_preflight_surface_and_portal_api(rt: FlywheelRuntime):
    rule = rt.activation.register_rule(
        CreateRuleRequest(
            rule_id="rule-cache-check",
            title="缓存变更前必须校验失效策略",
            triggers=["缓存", "cache"],
            checklist=["确认 TTL 与失效键"],
            origin_session_key="codex:s0",
        )
    )
    # Fail verification -> auto-records traceable dissent record without deleting rule
    rt.receipts.record_outcome(
        RecordOutcomeRequest(
            session_key="codex:s1",
            client="codex",
            origin=OutcomeOrigin.NATURAL,
            status=OutcomeStatus.FAILED,
            summary="只读本地内存缓存不适用该失效键检查",
            checks=[
                OutcomeCheck(
                    name="cache-check",
                    result=CheckResult.FAIL,
                    kind=VerificationKind.REAL,
                    evidence=["local LRU has no remote key"],
                    rule_id=rule.rule_id,
                    note="只读本地内存缓存属于例外场景",
                )
            ],
            applied_rule_ids=[rule.rule_id],
        )
    )
    dissents = rt.db.list_dissent_records(target_id=rule.rule_id, status="open")
    assert len(dissents) == 1
    assert "只读本地内存缓存" in dissents[0]["counter_claim"]

    # Re-activate rule -> preflight surfaces the open dissent reminder!
    rt.activation.transition_rule_status(rule.rule_id, RuleStatus.UNVERIFIED_ACTIVE)
    pf = rt.activation.preflight_activate(
        PreflightRequest(task="修改缓存 TTL 配置", client="codex", session_id="s2")
    )
    assert "已知分歧/例外" in pf.compact_context
    assert "只读本地内存缓存" in pf.compact_context

    # Portal REST endpoints smoke check
    app = create_app(runtime=rt)
    with TestClient(app) as client:
        r_rules = client.get("/api/site/rules").json()
        assert rule.rule_id in r_rules["dissents_by_rule"]
        assert client.get("/api/site/growth-metrics").status_code == 200
        cc = client.get("/api/site/core-card").json()
        assert cc["lines"] <= MAX_LINES


def test_friday_dream_freshness_expiry_renewal_and_skill_theater(rt: FlywheelRuntime):
    # 1. Seed a stale unverified_active rule older than 14 days with hit_count == 0
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    stale_rule = RuleRecord(
        rule_id="rule-stale-01",
        title="旧试运行规则",
        triggers=["stale"],
        checklist=["check"],
        status=RuleStatus.UNVERIFIED_ACTIVE,
        hit_count=0,
        created_at=old_ts,
        updated_at=old_ts,
    )
    rt.db.upsert_rule(stale_rule)

    # 2. Seed an expired knowledge card (valid_until in the past)
    rt.knowledge.write_document_verified(
        "lessons/2026-01-01-old-api.md",
        "---\ntitle: 旧版网关超时排查指南\nfreshness_tier: volatile\nlast_verified: 2026-01-01\nvalid_until: 2026-01-31\n---\n\n# 旧版网关超时排查指南\n网关超时需要检查代理配置。\n",
    )

    # 3. Seed a stale unused flywheel skill (>21d, 0 invocations) and a failing skill
    rt.db.upsert_skill_asset(
        skill_name="unused-flywheel-skill",
        source_dir="skills",
        description="闲置测试技能",
        origin="flywheel_created",
    )
    with rt.db.connect() as conn:
        conn.execute(
            "UPDATE skill_assets SET created_at = ? WHERE skill_name = 'unused-flywheel-skill'",
            ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),),
        )
    for _ in range(3):
        rt.db.record_skill_usage(["flaky-skill"], passed=False)

    # 4. Run Friday Dream consolidation
    dream = run_dream_consolidation(rt.cfg, rt.db, run_id="run-friday-test")
    assert any(r["rule_id"] == "rule-stale-01" for r in dream["auto_dormant_rules"])
    assert rt.db.get_rule("rule-stale-01").status == RuleStatus.DORMANT
    assert any(c["doc_path"] == "lessons/2026-01-01-old-api.md" for c in dream["expired_cards"])
    alert_names = {a["skill_name"] for a in dream["skill_alerts"]}
    assert {"unused-flywheel-skill", "flaky-skill"}.issubset(alert_names)
    assert dream["snapshot"]["sample_sufficient"] == 0

    # 5. Preflight warns about expired card
    pf = rt.activation.preflight_activate(
        PreflightRequest(task="排查网关超时代理配置", client="codex", session_id="s-exp")
    )
    assert any(h["doc_path"] == "lessons/2026-01-01-old-api.md" and h.get("is_expired") for h in pf.knowledge_hits)
    assert "已过复核期" in pf.compact_context

    # 6. Real PASS outcome with used_knowledge_paths auto-renews card & tracks used_skills
    rt.receipts.record_outcome(
        RecordOutcomeRequest(
            session_key="codex:s-exp",
            client="codex",
            preflight_receipt_id=pf.receipt_id,
            status=OutcomeStatus.SUCCESS,
            summary="实测验证有效",
            checks=[
                OutcomeCheck(
                    name="gateway curl",
                    result=CheckResult.PASS,
                    kind=VerificationKind.REAL,
                    evidence=["curl 200 OK"],
                )
            ],
            used_knowledge_paths=["lessons/2026-01-01-old-api.md"],
            used_skills=["good-skill"],
        )
    )
    fmap = rt.db.get_knowledge_freshness_map(["lessons/2026-01-01-old-api.md"])
    assert fmap["lessons/2026-01-01-old-api.md"]["freshness_state"] == "fresh"
    assert fmap["lessons/2026-01-01-old-api.md"]["is_expired"] is False
    skills_by_name = {s["skill_name"]: s for s in rt.db.list_skill_assets()}
    assert skills_by_name["good-skill"]["invoke_count"] == 1
    assert skills_by_name["good-skill"]["verified_pass_count"] == 1


@pytest.mark.asyncio
async def test_derived_core_card_hard_cap_and_mcp_resource(rt: FlywheelRuntime):
    # Stress-test hard <=200 lines & <=24KB limit with 60 rules & 40 observations
    for i in range(60):
        rt.activation.register_rule(
            CreateRuleRequest(
                rule_id=f"rule-cap-{i:02d}",
                title=f"防线规则编号 {i:02d} 用于测试核心卡硬上限截断逻辑",
                triggers=[f"kw{i}"],
                checklist=["核对前置条件并执行真实验证"],
            )
        )
    for i in range(40):
        rt.db.upsert_mistake_observation(
            root_cause_key=f"cap-obs-{i:02d}",
            title=f"错题观察条目 {i:02d}",
            thinking_root_cause="未核对边界条件",
        )

    info = rebuild_core_card(rt.cfg, rt.db)
    card_file = Path(info["path"])
    assert card_file.parent.resolve() == rt.cfg.data_path.resolve()
    assert card_file.name == "FLYWHEEL-CORE.md"
    assert info["lines"] <= MAX_LINES
    assert info["bytes"] <= MAX_BYTES

    resources = await rt.mcp.list_resources()
    uris = {str(r.uri) for r in resources}
    assert "flywheel://core-card" in uris


def test_p03_json_one_retry_repair_and_clean_chinese_titles(tmp_path: Path):
    assert _clean_title("codex · 01a2072f-9a5d-7d", "修复部署脚本超时问题。附带验证。") == "修复部署脚本超时问题"
    assert _clean_title("正常中文标题", "其他内容") == "正常中文标题"

    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.runner.mode = "api"
    cfg.runner.api.base_url = "http://127.0.0.1:9999/v1"
    cfg.runner.api.model = "mock-model"
    rt = FlywheelRuntime(cfg, skip_fts_sync=True)
    runner = OpenAICompatibleRunner(cfg, rt.fs_store)

    calls: list[str] = []

    def fake_chat(prompt: str, system_prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return '{"planned_writes": [{"target_kind": "knowledge_doc", BROKEN JSON'
        return (
            '{"planned_writes": [{"target_kind": "knowledge_doc", "action": "create", '
            '"title": "修复网关重试逻辑", "summary": "总结超时重连经验", '
            '"content_preview": "---\\ntitle: 修复网关重试逻辑\\n---\\n\\n# 正文\\n"}]}'
        )

    runner._call_chat_completion = fake_chat  # type: ignore[method-assign]
    plans = runner.plan_writes(
        {"findings": [], "eligible_promotions": [], "repeated_workflows": []},
        existing_rules=[],
        max_write_set=5,
    )
    assert len(calls) == 2  # Original attempt + exactly 1 lightweight JSON repair retry
    assert len(plans) == 1
    assert plans[0].title == "修复网关重试逻辑"


def test_two_level_session_triage_filters_temp_chats_and_keeps_project_or_deep_knowledge(
    rt: FlywheelRuntime, tmp_path: Path
):
    from ai_flywheel.models import SessionRecord, SessionStatus

    # 1. Subagent fragment -> skipped & archived
    frag_path = tmp_path / "agent-a8a292968e.jsonl"
    frag_path.write_text('{"role":"assistant","content":"internal subagent log"}\n', encoding="utf-8")
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:agent-a8a292968e",
            client="claude-code",
            session_id="agent-a8a292968e",
            title="claude-code session agent-a8a292968e",
            transcript_path=str(frag_path),
            metadata={"discovered_by": "p00_reconcile_scan"},
        )
    )

    # 2. Short empty chat (< 600 chars) -> skipped & archived
    short_path = tmp_path / "short-01.jsonl"
    short_path.write_text('{"cwd":"/work/ClaudeChat","role":"user","content":"你好"}\n', encoding="utf-8")
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:short-01",
            client="claude-code",
            session_id="short-01",
            title="claude-code session short-01",
            workspace="/work/ClaudeChat",
            transcript_path=str(short_path),
            metadata={"discovered_by": "p00_reconcile_scan"},
        )
    )

    # 3. Temp *Chat workspace with casual chitchat (800 chars, low signal) -> skipped
    casual_path = tmp_path / "casual-01.jsonl"
    casual_text = "今天吃什么好呢？" * 120
    casual_path.write_text(
        f'{{"cwd":"/work/ClaudeChat","role":"user","content":"{casual_text}"}}\n'
        f'{{"role":"assistant","content":"可以吃面条或者米饭。"}}\n',
        encoding="utf-8",
    )
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:casual-01",
            client="claude-code",
            session_id="casual-01",
            title="claude-code session casual-01",
            workspace="/work/ClaudeChat",
            transcript_path=str(casual_path),
            metadata={"discovered_by": "p00_reconcile_scan"},
        )
    )

    # 4. Temp *Chat workspace with DEEP knowledge/architecture discussion (>=1500 chars + signals) -> ADMITTED
    deep_path = tmp_path / "deep-01.jsonl"
    deep_u = "我们在做知识飞轮架构调研和方案对比，排查数据库索引与经验卡时效治理设计。" * 35
    deep_a = "建议补充 SQLite schema 迁移规范、分歧反例记录与复盘工作流验收标准。" * 35
    deep_path.write_text(
        f'{{"cwd":"/work/ClaudeChat","role":"user","content":"{deep_u}"}}\n'
        f'{{"role":"assistant","content":"{deep_a}"}}\n',
        encoding="utf-8",
    )
    rt.db.upsert_session(
        SessionRecord(
            session_key="claude-code:deep-01",
            client="claude-code",
            session_id="deep-01",
            title="claude-code session deep-01",
            workspace="/work/ClaudeChat",
            transcript_path=str(deep_path),
            metadata={"discovered_by": "p00_reconcile_scan"},
        )
    )

    # 5. Formal project workspace (>= 600 chars) -> ADMITTED
    proj_path = tmp_path / "proj-01.jsonl"
    proj_body = "修复核心订单接口超时问题，排查发现连接池上限过小导致阻塞，调整参数并完成压测验证。" * 20
    proj_path.write_text(
        f'{{"cwd":"/work/github/order-service","role":"user","content":"{proj_body}"}}\n'
        f'{{"role":"assistant","content":"已完成排查与修复。"}}\n',
        encoding="utf-8",
    )
    rt.db.upsert_session(
        SessionRecord(
            session_key="codex:proj-01",
            client="codex",
            session_id="proj-01",
            title="codex session proj-01",
            transcript_path=str(proj_path),
            metadata={"discovered_by": "p00_reconcile_scan"},
        )
    )

    # 6. Explicit user submission of a one-off translation chat -> bypasses L1, rejected by L2 P02 worthiness gate
    rt.register_session(
        RegisterSessionRequest(
            client="claude",
            session_id="translate-01",
            title="帮我翻译一下这句话",
            project="generic",
            summary="帮我翻译一下这段英文",
            content="User: 帮我翻译一下这句话。Assistant: 好的，翻译结果如下。",
        )
    )

    # Run Level-1 triage sweep
    report = rt.sessions.sweep_pending_triage()
    assert report["skipped"] == 3
    assert report["admitted"] == 3
    assert rt.db.get_session("claude-code:agent-a8a292968e").status == SessionStatus.ARCHIVED
    assert rt.db.get_session("claude-code:short-01").status == SessionStatus.ARCHIVED
    assert rt.db.get_session("claude-code:casual-01").status == SessionStatus.ARCHIVED
    assert rt.db.get_session("claude-code:deep-01").status == SessionStatus.PENDING
    assert rt.db.get_session("codex:proj-01").status == SessionStatus.PENDING
    assert rt.db.get_session("codex:proj-01").workspace == "/work/github/order-service"

    # Run full workflow on the translation-only session -> L2 P02 marks worthy=False -> 0 docs written, session archived
    run = rt.orchestrator.start_run(
        trigger="test",
        phase="daily",
        session_keys=["claude:translate-01"],
        auto_approve_override=True,
    )
    assert run.status == RunStatus.PASS
    assert len(run.planned_writes) == 0
    assert rt.db.get_session("claude:translate-01").status == SessionStatus.ARCHIVED


def test_wiki_gate_normalizes_generic_p03_doc_into_valid_t5_practice_card(tmp_path: Path):
    wiki_root = tmp_path / "wiki"
    practice_dir = wiki_root / "wiki-知识库" / "projects-项目" / "demo-project" / "实战笔记"
    practice_dir.mkdir(parents=True)
    (practice_dir / "笔记索引.md").write_text(
        "---\ntitle: Demo 索引\ntype: project-practice-index\nproject_id: demo-proj\ncreated: 2026-11-23\nupdated: 2026-11-23\nstatus: active\n---\n\n# Demo 索引\n",
        encoding="utf-8",
    )
    (wiki_root / ".wiki-schema.md").write_text(
        """---
title: Wiki Schema
description: Test schema
type: protocol
created: 2026-11-01
updated: 2026-11-23
status: active
core_contract:
  version: "2.1"
  core_files: [.wiki-schema.md]
  required_fields: [title, description, type, created, updated]
partition_contract:
  version: "1.0"
  partitions:
    projects:
      root: wiki-知识库/projects-项目
content_contract:
  version: "1.4.1"
  page_types:
    project-practice-note:
      partition: projects
      required_fields: [title, description, type, project_id, record_id, event_date, load_policy, default_load, created, updated, status]
      path_pattern: "^[^/]+/实战笔记/[^/]+\\\\.md$"
      allowed_status: [active, deprecated, archived]
---
# Schema
""",
        encoding="utf-8",
    )
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.knowledge_sink.type = "local_markdown_wiki"
    cfg.knowledge_sink.root = str(wiki_root)
    cfg.knowledge_sink.wiki_gate_enabled = True
    cfg.knowledge_sink.wiki_gate_require_scripts = False

    from ai_flywheel.adapters.wiki_gate import WikiGate

    gate = WikiGate(cfg)
    raw_content = "---\ntitle: 网关超时排查\ntags: [gateway, timeout]\n---\n\n# 网关超时排查\n- 修复连接池超时。\n"
    norm_rel, norm_md = gate.normalize_planned_document(
        "wiki-知识库/projects-项目/2026-11-23-网关超时排查.md",
        "网关超时排查",
        "修复连接池超时导致请求失败",
        raw_content,
        source_sessions=["codex:s1"],
        project_hint="demo-project",
    )
    assert norm_rel.startswith("wiki-知识库/projects-项目/demo-project/实战笔记/")
    res = gate.validate_document(norm_rel, norm_md)
    assert res.ok is True, f"Unexpected WikiGate issues: {res.issues}"
    assert res.page_type == "project-practice-note"
    assert "适用前提与失效条件" in norm_md
    assert "分歧与反例" in norm_md
