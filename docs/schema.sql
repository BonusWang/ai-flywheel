-- AI Flywheel · authoritative SQLite schema (version 4)
--
-- This file is the complete, executable final-state DDL: it reproduces every
-- table, virtual table, column and index that FlywheelDB builds on a fresh
-- database, without the historical ALTER patches. `tests/test_arch_invariants.py`
-- executes it and compares the result with a freshly initialized runtime
-- database, so a schema change that is not mirrored here fails the build.
--
-- The `sessions` opt-out / watermark columns, the structured `audit_events`
-- transition columns, `sys_dict_items` and `distill_queue` are the v4 data
-- foundation. Everything else is the v1-v3 contract the migrations lead to.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------ sessions
CREATE TABLE IF NOT EXISTS sessions (
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
    updated_at TEXT NOT NULL,
    opt_out INTEGER NOT NULL DEFAULT 0,
    opt_out_reason TEXT NOT NULL DEFAULT '',
    latest_content_hash TEXT NOT NULL DEFAULT '',
    latest_turn_count INTEGER NOT NULL DEFAULT 0,
    last_consumed_turn INTEGER NOT NULL DEFAULT 0,
    last_consumed_hash TEXT NOT NULL DEFAULT ''
);

-- --------------------------------------------------------------------- rules
CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT 'general',
    projects_json TEXT NOT NULL DEFAULT '["generic"]',
    triggers_json TEXT NOT NULL DEFAULT '[]',
    checklist_json TEXT NOT NULL DEFAULT '[]',
    forbidden_routes_json TEXT NOT NULL DEFAULT '[]',
    applicable_skills_json TEXT NOT NULL DEFAULT '[]',
    source_pointers_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'unverified_active',
    version INTEGER NOT NULL DEFAULT 1,
    origin_session_key TEXT,
    hit_count INTEGER NOT NULL DEFAULT 0,
    natural_pass_count INTEGER NOT NULL DEFAULT 0,
    recurrence_count INTEGER NOT NULL DEFAULT 0,
    revalidation_history_json TEXT NOT NULL DEFAULT '[]',
    supersedes_rule_id TEXT,
    effective_from TEXT,
    effective_to TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_hit_at TEXT,
    dormant_at TEXT,
    root_cause_key TEXT
);

-- -------------------------------------------------------- preflight receipts
CREATE TABLE IF NOT EXISTS preflight_receipts (
    receipt_id TEXT PRIMARY KEY,
    receipt_hash TEXT NOT NULL,
    client TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT 'generic',
    task_fingerprint TEXT NOT NULL,
    anti_laziness_mode TEXT NOT NULL,
    matched_rule_ids_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);

-- ------------------------------------------------------------------ outcomes
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    receipt_hash TEXT NOT NULL,
    session_key TEXT NOT NULL,
    client TEXT NOT NULL,
    preflight_receipt_id TEXT,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    checks_json TEXT NOT NULL,
    applied_rule_ids_json TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    revalidation_effects_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    used_skills_json TEXT NOT NULL DEFAULT '[]'
);

-- --------------------------------------------------------------- retro queue
CREATE TABLE IF NOT EXISTS retro_queue (
    retro_id TEXT PRIMARY KEY,
    dedupe_key TEXT UNIQUE,
    session_key TEXT NOT NULL,
    client TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    summary TEXT NOT NULL,
    tool_name TEXT,
    rule_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    linked_run_id TEXT
);

-- ------------------------------------------------------------ workflow runs
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    trigger_source TEXT NOT NULL,
    phase TEXT NOT NULL,
    runner_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    admitted_sessions_json TEXT NOT NULL DEFAULT '[]',
    nodes_json TEXT NOT NULL DEFAULT '[]',
    planned_writes_json TEXT NOT NULL DEFAULT '[]',
    digest_markdown TEXT NOT NULL DEFAULT '',
    error TEXT,
    lock_released INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    batch_date TEXT NOT NULL DEFAULT '',
    batch_seq INTEGER NOT NULL DEFAULT 1,
    approval_mode TEXT NOT NULL DEFAULT 'always',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS workflow_lock (
    lock_id INTEGER PRIMARY KEY CHECK (lock_id = 1),
    run_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL DEFAULT 0,
    node_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    duration_ms INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    checks_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    receipt_hash TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, node_id)
);

CREATE TABLE IF NOT EXISTS run_sessions (
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    session_key TEXT NOT NULL,
    admitted_chars INTEGER NOT NULL DEFAULT 0,
    disposition TEXT NOT NULL DEFAULT 'admitted',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, session_key)
);

CREATE TABLE IF NOT EXISTS planned_writes (
    write_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    seq_no INTEGER NOT NULL DEFAULT 0,
    target_kind TEXT NOT NULL,
    action TEXT NOT NULL,
    relative_path TEXT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    content_preview TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    skill_route_json TEXT NOT NULL DEFAULT '{}',
    requires_approval INTEGER NOT NULL DEFAULT 0,
    approval_reason TEXT NOT NULL DEFAULT '',
    decision_status TEXT NOT NULL DEFAULT 'pending',
    approved INTEGER NOT NULL DEFAULT 1,
    result_sha256 TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_cards (
    card_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'ask',
    state TEXT NOT NULL DEFAULT 'open',
    title TEXT NOT NULL,
    body_markdown TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '[]',
    selected_option TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT,
    closed_at TEXT,
    result_summary TEXT,
    failed_node TEXT
);

CREATE TABLE IF NOT EXISTS card_deliveries (
    delivery_id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL REFERENCES decision_cards(card_id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    external_ref TEXT,
    tries INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    synced_version INTEGER NOT NULL DEFAULT 0,
    synced_avail TEXT NOT NULL DEFAULT '',
    first_try_at TEXT,
    next_attempt_at TEXT,
    given_up INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    deferred_at TEXT,
    ask_card_json TEXT,
    interactive_fails INTEGER NOT NULL DEFAULT 0,
    unknown_sends INTEGER NOT NULL DEFAULT 0,
    UNIQUE(card_id, channel)
);

-- ------------------------------------------------------------- settings/state
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'general',
    value_json TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_overrides (
    policy_id TEXT PRIMARY KEY,
    content_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS retry_counters (
    counter_key TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ''
);

-- ------------------------------------------------------------- audit trail
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    summary TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL DEFAULT ''
);

-- ------------------------------------------------------------ knowledge base
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    doc_path UNINDEXED,
    title_index,
    description_index,
    body UNINDEXED,
    body_index,
    tags_index,
    project_id UNINDEXED,
    page_type UNINDEXED,
    status UNINDEXED,
    title_raw UNINDEXED,
    description_raw UNINDEXED,
    tags_raw UNINDEXED,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS knowledge_card_metrics (
    doc_path TEXT PRIMARY KEY,
    surface_count INTEGER NOT NULL DEFAULT 0,
    cited_count INTEGER NOT NULL DEFAULT 0,
    helped_count INTEGER NOT NULL DEFAULT 0,
    last_surfaced_at TEXT,
    last_helped_at TEXT,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    freshness_tier TEXT NOT NULL DEFAULT 'evolving',
    last_verified_at TEXT,
    valid_until TEXT,
    freshness_state TEXT NOT NULL DEFAULT 'fresh'
);

-- ------------------------------------------------------------------- assets
CREATE TABLE IF NOT EXISTS skill_assets (
    skill_name TEXT PRIMARY KEY,
    source_dir TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'manual',
    sha256 TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    last_run_id TEXT,
    backup_path TEXT,
    invoke_count INTEGER NOT NULL DEFAULT 0,
    verified_pass_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    health_state TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ------------------------------------------------- v3 growth ledger (flywheel)
CREATE TABLE IF NOT EXISTS mistake_observations (
    obs_id TEXT PRIMARY KEY,
    root_cause_key TEXT NOT NULL UNIQUE,
    severity_grade TEXT NOT NULL DEFAULT 'yellow_caught',
    title TEXT NOT NULL,
    phenomenon TEXT NOT NULL DEFAULT '',
    truth TEXT NOT NULL DEFAULT '',
    thinking_root_cause TEXT NOT NULL DEFAULT '',
    caught_by TEXT NOT NULL DEFAULT 'self',
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    session_keys_json TEXT NOT NULL DEFAULT '[]',
    promoted_rule_id TEXT,
    status TEXT NOT NULL DEFAULT 'observing',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dissent_records (
    dissent_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL DEFAULT 'rule',
    target_id TEXT NOT NULL,
    counter_claim TEXT NOT NULL,
    condition_scope TEXT NOT NULL DEFAULT '',
    evidence_pointer TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flywheel_metrics_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    period_key TEXT NOT NULL UNIQUE,
    red_count INTEGER NOT NULL DEFAULT 0,
    yellow_count INTEGER NOT NULL DEFAULT 0,
    green_count INTEGER NOT NULL DEFAULT 0,
    repeat_mistake_rate REAL NOT NULL DEFAULT 0.0,
    self_catch_rate REAL NOT NULL DEFAULT 0.0,
    active_rule_count INTEGER NOT NULL DEFAULT 0,
    dormant_rule_count INTEGER NOT NULL DEFAULT 0,
    open_dissent_count INTEGER NOT NULL DEFAULT 0,
    expired_card_count INTEGER NOT NULL DEFAULT 0,
    stale_skill_count INTEGER NOT NULL DEFAULT 0,
    sample_sufficient INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ------------------------------------------- v4 unified code dictionary
CREATE TABLE IF NOT EXISTS sys_dict_items (
    dict_type TEXT NOT NULL,
    code TEXT NOT NULL,
    label_zh TEXT NOT NULL,
    label_en TEXT NOT NULL DEFAULT '',
    group_key TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 100,
    is_builtin INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dict_type, code)
);

-- ------------------------------------------------ v4 distill work inventory
CREATE TABLE IF NOT EXISTS distill_queue (
    queue_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL REFERENCES sessions(session_key) ON DELETE CASCADE,
    parent_session_key TEXT NOT NULL DEFAULT '',
    is_subagent INTEGER NOT NULL DEFAULT 0,
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
    updated_at TEXT NOT NULL,
    UNIQUE(session_key, revision, chunk_index)
);

-- ------------------------------------------------------------------ indexes
CREATE INDEX IF NOT EXISTS idx_sessions_status_updated ON sessions(status, priority, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_client_project ON sessions(client, project);
CREATE INDEX IF NOT EXISTS idx_rules_status_hits ON rules(status, hit_count DESC);
CREATE INDEX IF NOT EXISTS idx_rules_root_cause ON rules(root_cause_key);
CREATE INDEX IF NOT EXISTS idx_rules_supersedes ON rules(supersedes_rule_id);
CREATE INDEX IF NOT EXISTS idx_preflight_client_created ON preflight_receipts(client, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_outcomes_session_created ON outcomes(session_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_retro_status_session ON retro_queue(status, session_key);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_started ON workflow_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_batch ON workflow_runs(batch_date DESC, batch_seq DESC);
CREATE INDEX IF NOT EXISTS idx_workflow_nodes_run ON workflow_nodes(run_id, step_order);
CREATE INDEX IF NOT EXISTS idx_planned_writes_run ON planned_writes(run_id, seq_no);
CREATE INDEX IF NOT EXISTS idx_planned_writes_status ON planned_writes(decision_status, target_kind);
CREATE INDEX IF NOT EXISTS idx_decision_cards_state ON decision_cards(state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_card_deliveries_status ON card_deliveries(status, channel);
CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON audit_events(entity_type, entity_id, seq DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_created ON audit_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_event_type ON audit_events(event_type, seq DESC);
CREATE INDEX IF NOT EXISTS idx_system_settings_cat ON system_settings(category);
CREATE INDEX IF NOT EXISTS idx_knowledge_freshness ON knowledge_card_metrics(freshness_state, valid_until);
CREATE INDEX IF NOT EXISTS idx_mistake_obs_status_count ON mistake_observations(status, occurrence_count DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_dissent_target_status ON dissent_records(target_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_snapshots_period ON flywheel_metrics_snapshots(period_key DESC);
CREATE INDEX IF NOT EXISTS idx_sys_dict_type_enabled ON sys_dict_items(dict_type, enabled, sort_order ASC);
CREATE INDEX IF NOT EXISTS idx_distill_queue_status_priority ON distill_queue(status, priority, enqueued_at ASC);
CREATE INDEX IF NOT EXISTS idx_distill_queue_client_status ON distill_queue(client, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_distill_queue_session_rev ON distill_queue(session_key, revision DESC, chunk_index ASC);
CREATE INDEX IF NOT EXISTS idx_distill_queue_run ON distill_queue(run_id);
CREATE INDEX IF NOT EXISTS idx_distill_queue_parent ON distill_queue(parent_session_key, status);

