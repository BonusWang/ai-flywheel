import type {
  AboutPayload,
  AuditEventRecord,
  AutomationsSnapshot,
  ConfigSaveResponse,
  ConnectPayload,
  CoreCardPayload,
  DissentRecord,
  DreamResult,
  DreamStatusPayload,
  FeishuCheckStatus,
  GrowthMetricsPayload,
  KnowledgeDocSummary,
  MistakeObservation,
  OutcomeRecord,
  OverviewResponse,
  PreflightResponse,
  RetroItem,
  RuleRecord,
  SessionRecord,
  SkillAssetRecord,
  SkillPreview,
  WorkflowLock,
  WorkflowRunRecord,
} from './types'

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  })
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const data = await res.json()
      msg = data.detail || data.error || msg
    } catch {
      // ignore
    }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

export const api = {
  overview: () => req<OverviewResponse>('/api/site/overview'),
  about: () => req<AboutPayload>('/api/site/about'),
  seed: () => req<Record<string, number>>('/api/site/seed', { method: 'POST' }),
  preflight: (body: { task: string; project?: string; client?: string }) =>
    req<PreflightResponse>('/api/site/preflight', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  listRuns: () =>
    req<{ runs: WorkflowRunRecord[]; total?: number; lock: WorkflowLock | null }>('/api/site/runs'),
  getRunEvents: (runId: string) =>
    req<{ events: AuditEventRecord[] }>(`/api/site/runs/${runId}/events`),
  startRun: (
    body: { phase?: string; auto_approve?: boolean | null },
    options?: { background?: boolean }
  ) =>
    req<{ run: WorkflowRunRecord | null; run_id?: string; background?: boolean }>(
      `/api/site/runs${options?.background ? '?background=true' : ''}`,
      {
        method: 'POST',
        body: JSON.stringify(body),
      }
    ),
  approveRun: (
    runId: string,
    approvedIndices?: number[],
    options?: { background?: boolean }
  ) =>
    req<{ run: WorkflowRunRecord | null; background?: boolean }>(
      `/api/site/runs/${runId}/approve${options?.background ? '?background=true' : ''}`,
      {
        method: 'POST',
        body: JSON.stringify({ approved_indices: approvedIndices ?? null }),
      }
    ),
  rejectRun: (runId: string, reason?: string) =>
    req<{ run: WorkflowRunRecord }>(`/api/site/runs/${runId}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason: reason || '用户在画布中取消放行' }),
    }),

  listSessions: () => req<{ sessions: SessionRecord[] }>('/api/site/sessions'),
  registerSession: (body: {
    client: string
    session_id: string
    title: string
    project: string
    summary: string
    content?: string
    tags?: string[]
  }) =>
    req<{ session: SessionRecord }>('/api/site/sessions', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  listRules: () =>
    req<{ rules: RuleRecord[]; dissents_by_rule?: Record<string, DissentRecord[]> }>(
      '/api/site/rules'
    ),
  createRule: (body: {
    title: string
    domain: string
    triggers: string[]
    checklist: string[]
    forbidden_routes: string[]
  }) =>
    req<{ rule: RuleRecord }>('/api/site/rules', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  transitionRule: (ruleId: string, status: RuleRecord['status'], note = '') =>
    req<{ rule: RuleRecord }>(`/api/site/rules/${ruleId}/transition`, {
      method: 'POST',
      body: JSON.stringify({ status, note }),
    }),

  listObservations: () =>
    req<{ observations: MistakeObservation[]; total?: number }>('/api/site/observations'),
  promoteObservation: (obsId: string) =>
    req<{ observation: MistakeObservation; rule: RuleRecord }>(
      `/api/site/observations/${obsId}/promote`,
      { method: 'POST' }
    ),
  archiveObservation: (obsId: string) =>
    req<{ observation: MistakeObservation }>(`/api/site/observations/${obsId}/archive`, {
      method: 'POST',
    }),
  bulkArchiveObservations: (obsIds: string[]) =>
    req<{ updated: number }>('/api/site/observations/bulk-archive', {
      method: 'POST',
      body: JSON.stringify({ obs_ids: obsIds }),
    }),

  createDissent: (body: {
    target_type?: 'rule' | 'knowledge_doc' | 'skill'
    target_id: string
    counter_claim: string
    condition_scope?: string
  }) =>
    req<{ dissent: DissentRecord }>('/api/site/dissents', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  updateDissentStatus: (dissentId: string, status: 'open' | 'merged' | 'dismissed') =>
    req<{ dissent: DissentRecord }>(`/api/site/dissents/${dissentId}/status`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    }),
  bulkUpdateDissents: (dissentIds: string[], status: 'open' | 'merged' | 'dismissed') =>
    req<{ updated: number }>('/api/site/dissents/bulk-status', {
      method: 'POST',
      body: JSON.stringify({ dissent_ids: dissentIds, status }),
    }),

  growthMetrics: () => req<GrowthMetricsPayload>('/api/site/growth-metrics'),
  coreCard: () => req<CoreCardPayload>('/api/site/core-card'),
  refreshCoreCard: () =>
    req<CoreCardPayload>('/api/site/core-card/refresh', { method: 'POST' }),
  listOutcomes: () => req<{ outcomes: OutcomeRecord[]; total?: number }>('/api/site/outcomes'),
  listSkills: () =>
    req<{ skills: SkillAssetRecord[]; probed_count: number; tracked_count?: number }>(
      '/api/site/skills'
    ),
  listDissents: (status?: string) =>
    req<{ dissents: DissentRecord[]; total?: number }>(
      status ? `/api/site/dissents?status=${encodeURIComponent(status)}` : '/api/site/dissents'
    ),
  getDream: () => req<DreamStatusPayload>('/api/site/dream'),
  runDream: (dryRun = false) =>
    req<{ result: DreamResult; latest_event: AuditEventRecord | null }>('/api/site/dream/run', {
      method: 'POST',
      body: JSON.stringify({ dry_run: dryRun }),
    }),

  listRetro: () => req<{ items: RetroItem[]; total?: number }>('/api/site/retro'),
  updateRetroStatus: (retroId: string, status: RetroItem['status']) =>
    req<{ item: RetroItem }>(`/api/site/retro/${retroId}/status`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    }),
  bulkUpdateRetro: (retroIds: string[], status: RetroItem['status']) =>
    req<{ updated: number }>('/api/site/retro/bulk-status', {
      method: 'POST',
      body: JSON.stringify({ retro_ids: retroIds, status }),
    }),

  listPolicies: () => req<{ policies: Record<string, any> }>('/api/site/policies'),
  updatePolicy: (policyId: string, patch: Record<string, any>) =>
    req<{ policy: any }>(`/api/site/policies/${policyId}`, {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),
  resetPolicy: (policyId: string) =>
    req<{ policy: any }>(`/api/site/policies/${policyId}/reset`, { method: 'POST' }),

  connectInfo: () => req<ConnectPayload>('/api/site/connect'),
  updateConfig: (patch: Record<string, any>) =>
    req<ConfigSaveResponse>('/api/site/config', {
      method: 'PUT',
      body: JSON.stringify(patch),
    }),

  listKnowledge: () =>
    req<{
      sink_type: string
      root: string
      documents: KnowledgeDocSummary[]
      total?: number
      expired_total?: number
    }>('/api/site/knowledge'),
  feishuCheckStatus: () => req<FeishuCheckStatus>('/api/site/feishu-check'),
  startFeishuCheck: (waitSeconds = 120) =>
    req<{ started: boolean; wait_seconds: number }>('/api/site/feishu-check', {
      method: 'POST',
      body: JSON.stringify({ wait_seconds: waitSeconds }),
    }),
  readKnowledgeDoc: (path: string) =>
    req<{ doc_path: string; content: string; sha256: string }>(
      `/api/site/knowledge/doc?path=${encodeURIComponent(path)}`
    ),
  skillPreview: (skillName: string) =>
    req<SkillPreview>(`/api/site/skills/${encodeURIComponent(skillName)}/preview`),

  automations: () => req<AutomationsSnapshot>('/api/site/automations'),
  updateAutomationSchedule: (body: {
    schedule_enabled?: boolean
    daily_cron?: string
    friday_weekly_enabled?: boolean
    approval_mode?: string
  }) =>
    req<{
      automations: AutomationsSnapshot
      restart_required: boolean
      restart_reasons: string[]
    }>('/api/site/automations/schedule', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  triggerAutomationRecovery: () =>
    req<{
      reaped_count: number
      reaped_run_ids: string[]
      checked_at: string | null
      automations: AutomationsSnapshot
    }>('/api/site/automations/recover', { method: 'POST' }),
}
