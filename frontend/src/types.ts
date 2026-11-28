export interface SiteProfile {
  brand: {
    name: string
    tagline: string
    motto: string
    subtitle: string
    owner_display: string
    mcp_prefix: string
    version: string
  }
}

export interface RuleRecord {
  rule_id: string
  title: string
  domain: string
  projects: string[]
  triggers: string[]
  checklist: string[]
  forbidden_routes: string[]
  applicable_skills: string[]
  source_pointers: string[]
  status: 'unverified_active' | 'verified_active' | 'needs_revision' | 'dormant' | 'retired'
  version: number
  origin_session_key: string | null
  hit_count: number
  natural_pass_count: number
  recurrence_count: number
  revalidation_history: Array<Record<string, unknown>>
  last_hit_at?: string | null
  dormant_at?: string | null
  root_cause_key?: string | null
  supersedes_rule_id?: string | null
  effective_from?: string | null
  effective_to?: string | null
  created_at: string
  updated_at: string
}

export interface MistakeObservation {
  obs_id: string
  root_cause_key: string
  severity_grade: 'red_silent' | 'yellow_caught' | 'green_minor'
  title: string
  phenomenon: string
  truth: string
  thinking_root_cause: string
  caught_by: 'user' | 'tool' | 'self'
  occurrence_count: number
  session_keys: string[]
  promoted_rule_id: string | null
  status: 'observing' | 'promoted' | 'archived'
  created_at: string
  updated_at: string
}

export interface DissentRecord {
  dissent_id: string
  target_type: 'rule' | 'knowledge_doc' | 'skill'
  target_id: string
  target_title?: string
  target_status?: string
  counter_claim: string
  condition_scope: string
  evidence_pointer: string
  session_key: string
  status: 'open' | 'merged' | 'dismissed'
  created_at: string
  updated_at: string
}

export interface GrowthMetricsLive {
  red_count: number
  yellow_count: number
  green_count: number
  total_mistakes: number
  observing_pool_count: number
  repeat_mistake_rate: number
  self_catch_rate: number
  active_rule_count: number
  dormant_rule_count: number
  open_dissent_count: number
  expired_card_count: number
  stale_skill_count: number
  sample_sufficient: boolean
}

export interface GrowthMetricsPayload {
  live: GrowthMetricsLive
  latest_snapshot: Record<string, any> | null
  history: Array<Record<string, any>>
}

export interface RetroAdvisory {
  group_key: string
  normalized_summary: string
  attempt: number
  verdict: 'ignore' | 'review' | 'keep'
  verdict_label: string
  recommended_action: 'dismissed' | 'resolved' | 'promote'
  reason: string
  matched_preference?: string
}

export interface RetroItem {
  retro_id: string
  session_key: string
  client: string
  kind: string
  severity: string
  summary: string
  tool_name: string | null
  rule_id: string | null
  detail: Record<string, unknown>
  status: 'open' | 'in_review' | 'resolved' | 'dismissed'
  advisory?: RetroAdvisory
  has_queued_session?: boolean
  settlement_mode?: 'auto_next_batch' | 'manual_only'
  created_at: string
  updated_at: string
}

export interface SessionRecord {
  session_key: string
  client: string
  session_id: string
  title: string
  project: string
  workspace: string
  transcript_path: string | null
  summary: string | null
  content: string | null
  tags: string[]
  priority: string
  status: 'pending' | 'in_progress' | 'processed' | 'archived'
  metadata?: Record<string, unknown>
  /** The user's opt-out from distillation, recorded on the session profile. */
  opt_out?: boolean
  opt_out_reason?: string
  /** Content fingerprint of the newest revision, and how far the pipeline consumed it. */
  latest_content_hash?: string
  latest_turn_count?: number
  last_consumed_turn?: number
  last_consumed_hash?: string
  created_at: string
  updated_at: string
}

/** The lifecycle of one distill-queue item (one revision / chunk of a session). */
export type QueueStatus = 'queued' | 'processing' | 'processed' | 'excluded' | 'withdrawn' | 'dead_letter'

/** How a distill-queue item entered the queue. */
export type IngressChannel = 'mcp_explicit' | 'manual_portal' | 'codex_archive' | 'claude_exit' | 'legacy_scan'

/** One row of the unified code dictionary (`sys_dict_items`). */
export interface DictItem {
  dict_type: string
  code: string
  label_zh: string
  label_en: string
  group_key: string
  description: string
  sort_order: number
  is_builtin: boolean
  enabled: boolean
  meta: Record<string, unknown>
  created_at: string
  updated_at: string
}

/** One unit of distill work: a session revision, optionally one chunk of it. */
export interface DistillQueueItem {
  queue_id: string
  session_key: string
  parent_session_key?: string
  is_subagent?: boolean
  client: string
  project: string
  workspace: string
  title: string
  revision: number
  chunk_index: number
  chunk_total: number
  ingress_channel: IngressChannel
  status: QueueStatus
  priority: string
  exclude_reason_code: string
  exclude_reason_detail: string
  focus_note: string
  content_hash: string
  total_chars: number
  compacted_chars: number
  is_oversized: boolean
  consumed_from_turn: number
  consumed_to_turn: number
  retry_count: number
  max_retries: number
  last_error: string
  run_id: string | null
  source_path: string | null
  payload: Record<string, unknown>
  enqueued_at: string
  claimed_at: string | null
  completed_at: string | null
  created_at: string
  updated_at: string
}

/** The inventory aggregation of `distill_queue`. */
export interface QueueSummary {
  total: number
  by_status: Record<QueueStatus, number>
  by_client: Record<string, Record<string, number>>
  by_channel: Record<IngressChannel, number>
  estimated_batches: number
}

/** The maintainer interventions the queue control plane accepts. */
export type QueueAction = 'requeue' | 'exclude' | 'withdraw' | 'opt_out' | 'clear_opt_out'

/** One item's pending transition in a rule re-triage pass. */
export interface QueueReTriageChange {
  queue_id: string
  session_key: string
  from_status: QueueStatus
  to_status: QueueStatus
  reason_code: string
  detail: string
}

/** The answer of `POST /api/site/queue/re-triage`. */
export interface QueueReTriageResult {
  dry_run: boolean
  /** How many queued / rule-excluded items the pass looked at. */
  checked: number
  /** How many items the pass would change (or did change, when applied). */
  changed_count: number
  /** How many items had display title / workspace / parentage metadata refreshed. */
  metadata_refresh_count?: number
  /** Items a real apply would move to `excluded`. */
  would_exclude: QueueReTriageChange[]
  /** Items a real apply would move back to `queued`. */
  would_admit: QueueReTriageChange[]
  summary?: QueueSummary
}

/** The answer of `POST /api/site/queue/sync`. */
export interface QueueSyncResult {
  /** The per-source reconcile report (`scanned / enqueued / excluded / withdrawn`). */
  sync: Record<string, any>
  /** How many legacy pending sessions were bridged into the queue. */
  bridged: number
  summary: QueueSummary
}

export interface PlannedWrite {
  target_kind: 'knowledge_doc' | 'flywheel_rule' | 'skill_improvement'
  relative_path: string | null
  action: string
  title: string
  summary: string
  content_preview: string
  rule_payload: Record<string, unknown> | null
  skill_route?: Record<string, unknown> | null
  /** The planner's routing declaration for a knowledge doc (page_type, project, reason). */
  wiki_route?: Record<string, unknown> | null
  /** Additional paths committed under the same approval item, such as a project practice index. */
  side_effects?: Array<{
    kind: string
    target_path: string
    action: string
    status: string
    entry_preview: string
  }>
  requires_approval?: boolean
  approval_reason?: string
  source_session_keys: string[]
  approved: boolean
}

/** What an existing Skill looks like, read-only, before it is updated. */
export interface SkillPreview {
  exists: boolean
  skill_name: string
  valid: boolean
  error: string | null
  line_count: number
  headings: string[]
  eol: string
  content: string
}

export interface CallTelemetry {
  model?: string
  duration_ms?: number | null
  attempts?: number | null
  prompt_tokens?: number | null
  completion_tokens?: number | null
  reasoning_tokens?: number | null
  cache_hit?: boolean
}

export interface DreamNodeSummary {
  period_key?: string
  auto_dormant_rules?: unknown[]
  expired_cards?: unknown[]
  open_dissents_count?: number
  proposals?: string[]
}

export interface NodeExecutionPayload extends Record<string, unknown> {
  call_telemetry?: CallTelemetry | null
  dream?: DreamNodeSummary | null
}

export interface NodeExecution {
  node_id: string
  name: string
  actor: string
  status: 'PENDING' | 'RUNNING' | 'AWAITING_APPROVAL' | 'PASS' | 'PARTIAL' | 'FAILED' | 'SKIPPED'
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  internal_checks: Array<{ name: string; status: string; detail?: unknown }>
  artifacts: string[]
  summary: string
  receipt_hash: string | null
  error: string | null
  payload: NodeExecutionPayload
}

export interface WorkflowRunRecord {
  run_id: string
  batch_date?: string
  batch_seq?: number
  trigger: string
  phase: string
  runner_mode: string
  approval_mode?: string
  status: 'PENDING' | 'RUNNING' | 'AWAITING_APPROVAL' | 'PASS' | 'PARTIAL' | 'CANCELLED' | 'FAILED'
  admitted_sessions: string[]
  nodes: NodeExecution[]
  planned_writes: PlannedWrite[]
  digest_markdown: string
  error: string | null
  lock_released: boolean
  /** The revision of this run record; it grows with every saved change. */
  version?: number
  started_at: string
  finished_at: string | null
  created_at?: string
  updated_at?: string
}

/** The single exclusive workflow lock. */
export interface WorkflowLock {
  run_id: string
  acquired_at: string
  expires_at: string | null
  heartbeat_at: string | null
}

export interface AuditEventRecord {
  seq: number
  entity_type: string
  entity_id: string
  event_type: string
  actor: string
  summary: string
  payload: Record<string, unknown>
  /** Structured state transition; empty strings on events that are not transitions. */
  from_status?: string
  to_status?: string
  reason_code?: string
  created_at: string
}

export interface OverviewResponse {
  site: SiteProfile
  metrics: {
    preflight_activations: number
    sessions_total: number
    sessions_pending: number
    rules_total: number
    rules_by_status: Record<string, number>
    verified_rule_ratio: number | null
    natural_outcomes_total: number
    real_verification_rate: number | null
    open_retro_count: number
    workflow_runs_total: number
    probed_skills_count: number
  }
  latest_run: WorkflowRunRecord | null
  recent_rules: RuleRecord[]
  recent_retro: RetroItem[]
  safety_valves: Record<string, boolean>
  workflow_lock: WorkflowLock | null
}

/** A knowledge card the preflight matched, with its freshness state. */
export interface KnowledgeHit {
  doc_path: string
  title: string
  description?: string
  snippet?: string
  project_id?: string
  page_type?: string
  status?: string
  score?: number
  freshness_tier?: string
  valid_until?: string | null
  is_expired?: boolean
  freshness_warning?: string
}

/** A local Skill the preflight suggests. */
export interface SuggestedSkill {
  name: string
  description: string
  path: string
  score?: number
}

/** What `PUT /api/site/config` answers with. */
export interface ConfigSaveResponse {
  status: string
  config: Record<string, any>
  /** True when some of the saved settings only take effect after a restart. */
  restart_required: boolean
  /** Which settings need the restart. */
  restart_reasons: string[]
}

export interface PreflightResponse {
  receipt_id: string
  receipt_hash: string
  anti_laziness_mode: string
  policy_card: string
  user_preferences_card: string
  matched_rules: RuleRecord[]
  suggested_skills: SuggestedSkill[]
  knowledge_hits: KnowledgeHit[]
  compact_context: string
  created_at: string
}

export interface KnowledgeDocSummary {
  doc_path: string
  title: string
  size_bytes: number
  excerpt: string
  freshness_tier?: 'volatile' | 'evolving' | 'evergreen' | string
  last_verified_at?: string | null
  valid_until?: string | null
  freshness_state?: string
  is_expired?: boolean
  surface_count?: number
  helped_count?: number
}

export type KnowledgeFreshnessAction = 'verify' | 'archive' | 'restore' | 'set_tier'

export interface KnowledgeFreshnessUpdateRequest {
  doc_path: string
  action: KnowledgeFreshnessAction
  freshness_tier?: 'volatile' | 'evolving' | 'evergreen'
}

export interface KnowledgeFreshnessUpdateResponse {
  card: KnowledgeDocSummary
  expired_total: number
}

export interface SkillAssetRecord {
  skill_name: string
  source_dir: string
  description: string
  origin: string
  sha256: string
  version: number
  invoke_count: number
  verified_pass_count: number
  fail_count: number
  last_used_at?: string | null
  health_state: string
  last_run_id?: string | null
  created_at: string
  updated_at: string
}

export interface OutcomeCheckItem {
  name: string
  result: 'pass' | 'fail' | 'unknown' | 'skipped'
  kind: 'real' | 'test' | 'static' | 'unknown'
  required?: boolean
  rule_id?: string | null
  evidence?: string[]
  notes?: string
}

export interface OutcomeRecord {
  outcome_id: string
  session_key: string
  client: string
  session_id: string
  turn_id: string
  preflight_receipt_id?: string | null
  origin: string
  status: 'success' | 'partial' | 'failed' | 'unverified'
  summary: string
  checks: OutcomeCheckItem[]
  applied_rule_ids: string[]
  used_skills: string[]
  artifacts: string[]
  used_knowledge_paths: string[]
  receipt_hash: string
  revalidation_effects: Array<Record<string, any>>
  created_at: string
}

export interface CoreCardPayload {
  path: string
  lines: number
  bytes: number
  content: string
  source?: string
}

export interface DreamResult {
  dry_run?: boolean
  period_key: string
  auto_dormant_rules: Array<{ rule_id: string; title: string; created_at?: string }>
  auto_archived_observations?: Array<{
    obs_id: string
    root_cause_key?: string
    title?: string
    severity_grade?: string
    updated_at?: string
  }>
  auto_archived_cards?: Array<{
    doc_path: string
    freshness_tier?: string
    valid_until?: string | null
    freshness_state?: string
    helped_count?: number
  }>
  expired_cards: Array<{
    doc_path: string
    freshness_tier?: string
    valid_until?: string | null
    freshness_state?: string
  }>
  merge_candidates: Array<{
    rule_a: string
    title_a: string
    rule_b: string
    title_b: string
    trigger_overlap: number
  }>
  skill_alerts: Array<{
    skill_name: string
    reason: string
    kind: string
  }>
  open_dissents_count: number
  proposals: string[]
  snapshot: Record<string, any> | null
}

export interface DreamStatusPayload {
  latest_event: AuditEventRecord | null
  recent_events: AuditEventRecord[]
  preview: DreamResult
}

export interface McpToolSummary {
  name: string
  title_zh: string
  phase: string
  description_zh?: string
  description: string
}

export interface ConnectPayload {
  guides: {
    endpoints: {
      portal_url: string
      mcp_http_url: string
      mcp_stdio_cmd: string
      llms_txt_url: string
    }
    clients: Array<{
      id: string
      name: string
      loop_guarantee: string
      mcp_label: string
      mcp_config: string
      hook_label: string
      hook_config: string
      notes: string
    }>
    universal_skill_template: string
  }
  config: Record<string, any>
  probed_skills: Array<{ name: string; description: string; path: string }>
  mcp_tools?: McpToolSummary[]
}

export interface RiverSource {
  name: string
  kind: string
  signal_only: boolean
}

export interface AboutPayload {
  site: SiteProfile
  metrics: OverviewResponse['metrics']
  endpoints: {
    portal_url: string
    mcp_http_url: string
    mcp_stdio_cmd: string
    llms_txt_url: string
    overview_api_url: string
  }
  mcp_tools: McpToolSummary[]
  llms_txt: string
  river_sources: RiverSource[]
  recent_rules: RuleRecord[]
}

export interface AutomationRunSummary {
  run_id: string
  batch_date: string
  batch_seq: number
  trigger: string
  phase: string
  status: WorkflowRunRecord['status']
  runner_mode: string
  approval_mode: string
  admitted_count: number
  planned_writes_count: number
  duration_ms: number
  started_at?: string | null
  finished_at?: string | null
  created_at: string
  error?: string | null
}

export interface AutomationsSnapshot {
  scheduler: {
    enabled: boolean
    running: boolean
    role_active: boolean
    timezone: string
    cron: string
    human_schedule: string
    friday_weekly_enabled: boolean
    next_run_at: string | null
    preview_next_run_at: string | null
    last_scheduled_run_at: string | null
    last_run_at: string | null
    last_run_id: string | null
    last_run_status: WorkflowRunRecord['status'] | null
    last_run_trigger: string | null
    cron_error: string | null
    // When this process came up, and how a scheduled fire it was not awake for is
    // handled: how late a batch may still run, whether the recovery loop catches a
    // lost one up, and whether the latest fire is still outstanding. `scheduled_today`
    // is the fire `missed_today` is about — today's latest, or yesterday's while its
    // grace window is still open — and `catchup_window_until` the moment the recovery
    // loop stops catching it up. `catchup_run_id` is the batch the recovery loop
    // started for a lost fire, when it started one. Optional because an older backend
    // does not send them.
    started_at?: string | null
    misfire_grace_seconds?: number
    catchup_missed_schedule?: boolean
    missed_today?: boolean
    scheduled_today?: string | null
    catchup_window_until?: string | null
    catchup_run_id?: string | null
  }
  metrics: {
    today_date: string
    today_runs_count: number
    today_passed_count: number
    today_awaiting_count: number
    today_failed_count: number
    today_scheduled_ran: boolean
    total_scheduled_runs: number
    total_scheduled_passed: number
    total_runs: number
    pending_sessions: number
    awaiting_approval_runs: number
  }
  policy: {
    workflow_enabled: boolean
    approval_mode: string
    effective_approval_mode: string
    runner_mode: string
    max_sessions_per_run: number
    max_writes_per_run: number
  }
  daemons: {
    recovery: {
      role_active: boolean
      interval_seconds: number
      stale_after_seconds: number
      last_pass_at: string | null
      last_reaped_count: number
      total_reaped_count: number
      last_error: string | null
    }
    feishu: {
      enabled: boolean
      interactive: boolean
      availability: string
      listener_state: string
      check_valid: boolean
    }
  }
  lock: WorkflowLock | null
  recent_runs: AutomationRunSummary[]
}

export interface FeishuCheckStatus {
  enabled?: boolean
  interactive?: boolean
  interaction_ready?: boolean
  availability?: string
  state?: string
  running?: boolean
  valid?: boolean
  reason?: string
  at?: string
  listener?: {
    state: string
    reason: string
    ready?: boolean
  } | null
}
