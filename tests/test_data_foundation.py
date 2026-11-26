"""The v4 data foundation: dictionary, distill queue, trail columns and the dicts API.

Every fixture is synthetic; the v3-shaped database is built by hand so the
upgrade path is exercised exactly as an existing installation would hit it.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_flywheel.app import create_app
from ai_flywheel.config import FlywheelConfig
from ai_flywheel.models import (
    DictItem,
    DistillQueueItem,
    IngressChannel,
    QueueStatus,
    RuleStatus,
    RunStatus,
    SessionRecord,
    SignalKind,
)
from ai_flywheel.runtime import FlywheelRuntime
from ai_flywheel.storage.db import BUILTIN_DICT_SEEDS, FlywheelDB

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_DICT_TYPES = {
    "queue_status",
    "ingress_channel",
    "exclude_reason",
    "client_type",
    "priority_level",
    "run_status",
    "rule_status",
    "signal_kind",
    "entity_type",
}

SESSION_PROFILE_COLUMNS = {
    "opt_out",
    "opt_out_reason",
    "latest_content_hash",
    "latest_turn_count",
    "last_consumed_turn",
    "last_consumed_hash",
}

AUDIT_TRANSITION_COLUMNS = {"from_status", "to_status", "reason_code"}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _make_runtime(tmp_path: Path) -> FlywheelRuntime:
    shutil.copytree(REPO_ROOT / "profile", tmp_path / "profile")
    skills = tmp_path / "skills"
    skills.mkdir()
    cfg = FlywheelConfig(base_dir=tmp_path)
    cfg.capabilities.skills_dirs = [str(skills)]
    return FlywheelRuntime(cfg)


def _seed_session(db: FlywheelDB, session_key: str = "codex:session-1") -> None:
    db.upsert_session(
        SessionRecord(
            session_key=session_key,
            client="codex",
            session_id="session-1",
            title="接口超时排查",
            project="demo",
            summary="连接池上限过小导致超时",
            content="用户：接口联调超时。助手：先看日志，再核对连接池上限。",
        )
    )


def _build_v3_shaped_database(db_path: Path) -> None:
    """A v3 database: versions 2 and 3 recorded, pre-v4 sessions/audit_events shape."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations VALUES (2, 'v2_normalized_audit_and_decisions', '2026-11-23T00:00:00Z');
        INSERT INTO schema_migrations VALUES (3, 'v3_true_flywheel_evolution', '2026-11-23T00:00:00Z');

        CREATE TABLE sessions (
            session_key TEXT PRIMARY KEY,
            client TEXT NOT NULL,
            session_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT 'generic',
            workspace TEXT NOT NULL DEFAULT '',
            transcript_path TEXT,
            summary TEXT,
            content TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            priority TEXT NOT NULL DEFAULT 'P1_GENERAL',
            status TEXT NOT NULL DEFAULT 'pending',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            summary TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO sessions (
            session_key, client, session_id, title, project, tags_json, priority,
            status, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, '[]', 'P1_GENERAL', 'pending', '{}', ?, ?)
        """,
        ("codex:legacy-1", "codex", "legacy-1", "历史会话", "demo", "2026-11-20T00:00:00Z", "2026-11-20T00:00:00Z"),
    )
    conn.execute(
        """
        INSERT INTO audit_events (entity_type, entity_id, event_type, actor, summary, payload_json, created_at)
        VALUES ('workflow_run', 'run-legacy', 'run_started', 'system', '历史批次', '{}', '2026-11-20T00:00:00Z')
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- fresh schema


def test_a_fresh_database_has_the_v4_tables_columns_and_builtin_dicts(tmp_path: Path):
    db = FlywheelDB(tmp_path / "fresh.db")
    with db.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"sys_dict_items", "distill_queue"}.issubset(tables)
        assert SESSION_PROFILE_COLUMNS.issubset(_columns(conn, "sessions"))
        assert AUDIT_TRANSITION_COLUMNS.issubset(_columns(conn, "audit_events"))
        assert {"parent_session_key", "is_subagent"}.issubset(_columns(conn, "distill_queue"))
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "idx_distill_queue_parent" in indexes
        migration = conn.execute(
            "SELECT name FROM schema_migrations WHERE version = 4"
        ).fetchone()
        assert migration is not None
        assert migration["name"] == "v4_data_foundation_dicts_and_queue"

    items = db.list_dicts()
    assert {item.dict_type for item in items} == EXPECTED_DICT_TYPES
    assert {seed["dict_type"] for seed in BUILTIN_DICT_SEEDS} == EXPECTED_DICT_TYPES

    enum_coverage = {
        "queue_status": {status.value for status in QueueStatus},
        "ingress_channel": {channel.value for channel in IngressChannel},
        "run_status": {status.value for status in RunStatus},
        "rule_status": {status.value for status in RuleStatus},
        "signal_kind": {kind.value for kind in SignalKind},
    }
    for dict_type, expected in enum_coverage.items():
        enabled = {
            item.code for item in db.list_dicts(dict_type=dict_type) if item.enabled
        }
        assert expected <= enabled, f"{dict_type} is missing {expected - enabled}"
    exclude_codes = {item.code for item in db.list_dicts(dict_type="exclude_reason")}
    assert {
        "USER_OPT_OUT",
        "SUBAGENT_FRAGMENT",
        "EXCLUDED_WORKSPACE",
        "TOO_SHORT",
        "TEMP_CHAT_LOW_SIGNAL",
        "SINGLE_TURN_LOOKUP",
        "MANUAL_EXCLUDE",
        "EMPTY_DELTA",
        "SUPERSEDED",
    } <= exclude_codes


# ------------------------------------------------------ dictionary idempotency


def test_a_custom_dict_item_survives_reinitialization_untouched(tmp_path: Path):
    db_path = tmp_path / "dicts.db"
    db = FlywheelDB(db_path)
    db.upsert_dict_item(
        DictItem(
            dict_type="client_type",
            code="local-agent",
            label_zh="自定义助手",
            label_en="Local agent",
            group_key="custom",
            description="维护者自定义的接入端",
            sort_order=900,
            is_builtin=False,
            meta={"color": "teal"},
        )
    )
    # Tamper with a builtin label: the seed must restore it, without touching the custom row.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE sys_dict_items SET label_zh = '被改坏的标签' WHERE dict_type = 'queue_status' AND code = 'queued'"
        )

    second = FlywheelDB(db_path)
    items = {item.code: item for item in second.list_dicts("client_type")}
    custom = items["local-agent"]
    assert custom.label_zh == "自定义助手"
    assert custom.label_en == "Local agent"
    assert custom.enabled is True
    assert custom.is_builtin is False
    assert custom.meta == {"color": "teal"}

    queue_labels = {item.code: item.label_zh for item in second.list_dicts("queue_status")}
    assert queue_labels["queued"] == "排队中"


# ------------------------------------------------------- v3 to v4 upgrade


def test_a_v3_database_upgrades_in_place_with_a_backup_and_no_data_loss(tmp_path: Path):
    db_path = tmp_path / "flywheel.db"
    _build_v3_shaped_database(db_path)

    db = FlywheelDB(db_path)

    backup = db_path.with_name("flywheel.db.bak-v3")
    assert backup.exists()
    with sqlite3.connect(str(backup)) as backup_conn:
        # The backup is the pre-v4 snapshot: it still lacks the transition columns.
        assert "from_status" not in _columns(backup_conn, "audit_events")

    with db.connect() as conn:
        versions = {
            row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        assert {2, 3, 4} <= versions
        assert SESSION_PROFILE_COLUMNS.issubset(_columns(conn, "sessions"))
        assert AUDIT_TRANSITION_COLUMNS.issubset(_columns(conn, "audit_events"))
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_key = 'codex:legacy-1'"
        ).fetchone()
        assert session["title"] == "历史会话"
        assert session["opt_out"] == 0
        assert session["last_consumed_turn"] == 0
        event = conn.execute(
            "SELECT * FROM audit_events WHERE entity_id = 'run-legacy'"
        ).fetchone()
        assert event["event_type"] == "run_started"
        assert event["from_status"] == ""
        assert event["to_status"] == ""
        assert event["reason_code"] == ""

    assert db.list_dicts("queue_status")
    assert db.count_queue_items() == 0


# --------------------------------------------------------- distill queue


def test_distill_queue_supports_revisions_chunks_and_accurate_summaries(tmp_path: Path):
    db = FlywheelDB(tmp_path / "queue.db")
    _seed_session(db)

    db.upsert_queue_item(
        DistillQueueItem(
            queue_id="q-1",
            session_key="codex:session-1",
            client="codex",
            revision=1,
            chunk_index=1,
            chunk_total=1,
            status=QueueStatus.PROCESSED,
            total_chars=1200,
        )
    )
    db.upsert_queue_item(
        DistillQueueItem(
            queue_id="q-2",
            session_key="codex:session-1",
            client="codex",
            revision=2,
            chunk_index=1,
            chunk_total=2,
            status=QueueStatus.QUEUED,
            total_chars=9000,
            is_oversized=True,
            ingress_channel=IngressChannel.CODEX_ARCHIVE,
        )
    )
    db.upsert_queue_item(
        DistillQueueItem(
            queue_id="q-3",
            session_key="codex:session-1",
            client="codex",
            revision=2,
            chunk_index=2,
            chunk_total=2,
            status=QueueStatus.QUEUED,
            total_chars=8000,
            is_oversized=True,
            ingress_channel=IngressChannel.MCP_EXPLICIT,
        )
    )

    items = db.list_session_queue_items("codex:session-1")
    assert [item.queue_id for item in items] == ["q-2", "q-3", "q-1"]
    assert db.get_latest_queue_revision("codex:session-1") == 2
    assert db.get_latest_queue_revision("codex:missing") == 0

    # The same (session_key, revision, chunk_index) may not be duplicated under another id.
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_queue_item(
            DistillQueueItem(
                queue_id="q-duplicate",
                session_key="codex:session-1",
                client="codex",
                revision=1,
                chunk_index=1,
            )
        )

    summary = db.summarize_queue(max_sessions_per_run=1)
    assert summary["total"] == 3
    assert summary["by_status"] == {
        "queued": 2,
        "processing": 0,
        "processed": 1,
        "excluded": 0,
        "withdrawn": 0,
        "dead_letter": 0,
    }
    assert summary["by_client"]["codex"]["queued"] == 2
    assert summary["by_client"]["codex"]["processed"] == 1
    assert summary["by_client"]["codex"]["total"] == 3
    assert summary["by_channel"]["codex_archive"] == 2
    assert summary["by_channel"]["mcp_explicit"] == 1
    assert summary["estimated_batches"] == 2
    assert db.summarize_queue()["estimated_batches"] == 1

    assert [item.queue_id for item in db.list_queued_items_prioritized()]
    assert db.count_queue_items() == 3
    assert db.count_queue_items(status=QueueStatus.QUEUED) == 2


def test_update_queue_item_status_writes_the_structured_transition_trail(tmp_path: Path):
    db = FlywheelDB(tmp_path / "queue-trail.db")
    _seed_session(db)
    db.upsert_queue_item(
        DistillQueueItem(queue_id="q-1", session_key="codex:session-1", client="codex")
    )

    updated = db.update_queue_item_status(
        "q-1",
        QueueStatus.WITHDRAWN,
        actor="mcp",
        reason_code="USER_WITHDRAW",
        summary="用户撤回该沉淀任务",
    )
    assert updated is not None
    assert updated.status is QueueStatus.WITHDRAWN
    assert updated.completed_at

    events = db.list_audit_events(entity_type="queue_item", entity_id="q-1")
    assert events
    event = events[0]
    assert event["event_type"] == "status_changed"
    assert event["actor"] == "mcp"
    assert event["from_status"] == "queued"
    assert event["to_status"] == "withdrawn"
    assert event["reason_code"] == "USER_WITHDRAW"

    processing = db.update_queue_item_status(
        "q-1", QueueStatus.PROCESSING, increment_retry=True
    )
    assert processing is not None
    assert processing.retry_count == 1
    assert processing.claimed_at

    reset = db.update_queue_item_status(
        "q-1", QueueStatus.PROCESSING, reset_retry=True, record_audit=False
    )
    assert reset is not None
    assert reset.retry_count == 0
    events_after = db.list_audit_events(entity_type="queue_item", entity_id="q-1")
    assert len(events_after) == 2  # the reset wrote no third trail row

    assert db.update_queue_item_status("q-missing", QueueStatus.PROCESSED) is None


def test_append_audit_event_keeps_the_old_signature_backward_compatible(tmp_path: Path):
    db = FlywheelDB(tmp_path / "audit.db")
    seq = db.append_audit_event(
        entity_type="workflow_run",
        entity_id="run-1",
        event_type="run_started",
        summary="旧签名调用",
    )
    assert seq > 0
    event = db.list_audit_events(entity_type="workflow_run", entity_id="run-1")[0]
    assert event["summary"] == "旧签名调用"
    assert event["from_status"] == ""
    assert event["to_status"] == ""
    assert event["reason_code"] == ""

    db.append_audit_event(
        entity_type="queue_item",
        entity_id="q-1",
        event_type="status_changed",
        from_status="queued",
        to_status="withdrawn",
        reason_code="USER_WITHDRAW",
        actor="mcp",
    )
    transition = db.list_audit_events(entity_type="queue_item", entity_id="q-1")[0]
    assert transition["from_status"] == "queued"
    assert transition["to_status"] == "withdrawn"
    assert transition["reason_code"] == "USER_WITHDRAW"


# -------------------------------------------------------------- session profile


def test_the_session_profile_persists_opt_out_and_watermarks(tmp_path: Path):
    db = FlywheelDB(tmp_path / "sessions.db")
    _seed_session(db)

    opted_out = db.set_session_opt_out("codex:session-1", True, "用户要求不再沉淀")
    assert opted_out is not None
    assert opted_out.opt_out is True
    assert opted_out.opt_out_reason == "用户要求不再沉淀"

    # A late lifecycle hook must not undo the opt-out.
    db.upsert_session(
        SessionRecord(
            session_key="codex:session-1",
            client="codex",
            session_id="session-1",
            content="补充一段结尾",
        ),
        preserve_existing_lifecycle=True,
    )
    preserved = db.get_session("codex:session-1")
    assert preserved is not None
    assert preserved.opt_out is True
    assert preserved.opt_out_reason == "用户要求不再沉淀"

    db.update_session_watermark(
        "codex:session-1",
        latest_content_hash="hash-new",
        latest_turn_count=8,
        last_consumed_turn=5,
        last_consumed_hash="hash-old",
    )
    watermark = db.get_session("codex:session-1")
    assert watermark is not None
    assert watermark.latest_content_hash == "hash-new"
    assert watermark.latest_turn_count == 8
    assert watermark.last_consumed_turn == 5
    assert watermark.last_consumed_hash == "hash-old"


# ------------------------------------------------------------------ dicts API


def test_the_dicts_api_returns_grouped_and_flat_payloads(tmp_path: Path):
    rt = _make_runtime(tmp_path)
    client = TestClient(create_app(runtime=rt))

    response = client.get("/api/site/dicts")
    assert response.status_code == 200
    body = response.json()
    assert set(body["dicts"]) == EXPECTED_DICT_TYPES
    assert body["total"] == len(body["items"])
    assert all(item["enabled"] for item in body["items"])
    assert [item["code"] for item in body["dicts"]["queue_status"]] == [
        status.value for status in QueueStatus
    ]

    filtered = client.get("/api/site/dicts?dict_type=queue_status")
    assert filtered.status_code == 200
    payload = filtered.json()
    assert set(payload["dicts"]) == {"queue_status"}
    assert payload["total"] == len(QueueStatus)
    assert {item["dict_type"] for item in payload["items"]} == {"queue_status"}
    assert {item["code"] for item in payload["items"]} == {
        status.value for status in QueueStatus
    }

    # The payload is JSON-serializable and carries the dictionary metadata.
    first = payload["items"][0]
    assert json.loads(json.dumps(first))["meta"] == first["meta"]


def test_existing_v4_database_gains_queue_hierarchy_columns_on_startup(tmp_path: Path):
    """An existing v4 DB (without parent_session_key/is_subagent) upgrades cleanly on open."""
    db_path = tmp_path / "legacy-v4.db"
    FlywheelDB(db_path)
    # Simulate a pre-hierarchy v4 database by recreating distill_queue without the new columns
    # while leaving schema_migrations version=4 intact.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP TABLE distill_queue")
        conn.execute(
            """
            CREATE TABLE distill_queue (
                queue_id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                client TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT 'generic',
                workspace TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1,
                chunk_index INTEGER NOT NULL DEFAULT 1,
                chunk_total INTEGER NOT NULL DEFAULT 1,
                ingress_channel TEXT NOT NULL DEFAULT 'codex_archive',
                status TEXT NOT NULL DEFAULT 'queued',
                priority TEXT NOT NULL DEFAULT 'P1_GENERAL',
                exclude_reason_code TEXT NOT NULL DEFAULT '',
                exclude_reason_detail TEXT NOT NULL DEFAULT '',
                focus_note TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                total_chars INTEGER NOT NULL DEFAULT 0,
                compacted_chars INTEGER NOT NULL DEFAULT 0,
                is_oversized INTEGER NOT NULL DEFAULT 0,
                consumed_from_turn INTEGER NOT NULL DEFAULT 0,
                consumed_to_turn INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                last_error TEXT NOT NULL DEFAULT '',
                run_id TEXT,
                source_path TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                enqueued_at TEXT NOT NULL,
                claimed_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    reopened = FlywheelDB(db_path)
    cols = {
        row[1]
        for row in sqlite3.connect(str(db_path)).execute("PRAGMA table_info(distill_queue)").fetchall()
    }
    assert {"parent_session_key", "is_subagent"}.issubset(cols)
    assert reopened.list_queued_items_prioritized() == []


