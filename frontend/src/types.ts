export interface LoopStep {
  step: number
  key: string
  title: string
  short: string
  phase: 'frontstage' | 'backstage'
}

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
  loop_steps: LoopStep[]
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
  status: 'unverified_active' | 'verified_active' | 'needs_revision' | 'retired'
  version: number
  origin_session_key: string | null
  hit_count: number
  natural_pass_count: number
  recurrence_count: number
  revalidation_history: Array<Record<string, unknown>>
  created_at: string
  updated_at: string
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
  source_session_keys: string[]
  approved: boolean
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
  trigger: string
  phase: string
  runner_mode: string
  status: 'PENDING' | 'RUNNING' | 'AWAITING_APPROVAL' | 'PASS' | 'PARTIAL' | 'FAILED'
  admitted_sessions: string[]
  nodes: NodeExecution[]
  planned_writes: PlannedWrite[]
  digest_markdown: string
  error: string | null
  lock_released: boolean
  started_at: string
  finished_at: string | null
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
  workflow_lock: { run_id: string; acquired_at: string } | null
}

export interface PreflightResponse {
  receipt_id: string
  receipt_hash: string
  anti_laziness_mode: string
  policy_card: string
  user_preferences_card: string
  matched_rules: RuleRecord[]
  suggested_skills: Array<{ name: string; description: string; path: string }>
  knowledge_hits: Array<{ doc_path: string; title: string; snippet: string }>
  compact_context: string
  created_at: string
}

export interface KnowledgeDocSummary {
  doc_path: string
  title: string
  size_bytes: number
  excerpt: string
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
}
