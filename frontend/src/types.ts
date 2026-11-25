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
  created_at: string
  updated_at: string
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
  payload: Record<string, unknown>
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
