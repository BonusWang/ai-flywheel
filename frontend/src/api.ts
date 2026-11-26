import type {
  AboutPayload,
  AuditEventRecord,
  AutomationsSnapshot,
  ConfigSaveResponse,
  ConnectPayload,
  CoreCardPayload,
  DictItem,
  DissentRecord,
  DistillQueueItem,
  DreamResult,
  DreamStatusPayload,
  FeishuCheckStatus,
  GrowthMetricsPayload,
  KnowledgeDocSummary,
  KnowledgeFreshnessAction,
  KnowledgeFreshnessUpdateRequest,
  KnowledgeFreshnessUpdateResponse,
  MistakeObservation,
  OutcomeRecord,
  OverviewResponse,
  PreflightResponse,
  QueueAction,
  QueueReTriageChange,
  QueueReTriageResult,
  QueueSummary,
  QueueSyncResult,
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
    body: {
      phase?: string
      auto_approve?: boolean | null
      queue_ids?: string[]
      session_keys?: string[]
    },
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

  // ---- Distill queue inventory & control plane ----
  listQueue: (params?: {
    status?: string
    client?: string
    ingress_channel?: string
    project?: string
    q?: string
    limit?: number
    offset?: number
  }) => {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value === undefined || value === null || value === '') continue
      search.set(key, String(value))
    }
    const qs = search.toString()
    return req<{ summary: QueueSummary; items: DistillQueueItem[]; total: number }>(
      `/api/site/queue${qs ? `?${qs}` : ''}`
    )
  },
  getQueueEvents: (queueId: string) =>
    req<{ events: AuditEventRecord[] }>(
      `/api/site/queue/${encodeURIComponent(queueId)}/events`
    ),
  syncQueue: () =>
    req<QueueSyncResult>('/api/site/queue/sync', { method: 'POST' }),
  queueItemAction: (queueId: string, action: QueueAction, reason = '') =>
    req<{ item: DistillQueueItem; summary: QueueSummary }>(
      `/api/site/queue/${encodeURIComponent(queueId)}/action`,
      {
        method: 'POST',
        body: JSON.stringify({ action, reason }),
      }
    ),
  queueBulkAction: (queueIds: string[], action: QueueAction, reason = '') =>
    req<{ updated: number; summary: QueueSummary }>('/api/site/queue/bulk-action', {
      method: 'POST',
      body: JSON.stringify({ queue_ids: queueIds, action, reason }),
    }),
  /**
   * Re-run the triage rules over the inventory.
   *
   * The backend reports the change list as `changes` (with `evaluated` /
   * `change_count`); it is normalized here so the panel always reads the same
   * `would_exclude` / `would_admit` split regardless of which keys the route
   * grows.
   */
  reTriageQueue: async (dryRun = true): Promise<QueueReTriageResult> => {
    const raw = await req<{
      dry_run?: boolean
      evaluated?: number
      checked?: number
      change_count?: number
      changed_count?: number
      metadata_refresh_count?: number
      changes?: QueueReTriageChange[]
      would_exclude?: QueueReTriageChange[]
      would_admit?: QueueReTriageChange[]
      summary?: QueueSummary
    }>('/api/site/queue/re-triage', {
      method: 'POST',
      body: JSON.stringify({ dry_run: dryRun }),
    })
    const changes = raw.changes ?? []
    return {
      dry_run: raw.dry_run ?? dryRun,
      checked: raw.checked ?? raw.evaluated ?? 0,
      changed_count: raw.changed_count ?? raw.change_count ?? changes.length,
      metadata_refresh_count: raw.metadata_refresh_count ?? 0,
      would_exclude:
        raw.would_exclude ?? changes.filter((c) => c.to_status === 'excluded'),
      would_admit: raw.would_admit ?? changes.filter((c) => c.to_status === 'queued'),
      summary: raw.summary,
    }
  },

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
  restoreObservation: (obsId: string) =>
    req<{ observation: MistakeObservation }>(`/api/site/observations/${obsId}/restore`, {
      method: 'POST',
    }),
  bulkArchiveObservations: (obsIds: string[]) =>
    req<{ updated: number }>('/api/site/observations/bulk-archive', {
      method: 'POST',
      body: JSON.stringify({ obs_ids: obsIds }),
    }),
  bulkRestoreObservations: (obsIds: string[]) =>
    req<{ updated: number }>('/api/site/observations/bulk-restore', {
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
  listDicts: (dictType?: string) =>
    req<{ dicts: Record<string, DictItem[]>; items: DictItem[]; total: number }>(
      dictType
        ? `/api/site/dicts?dict_type=${encodeURIComponent(dictType)}`
        : '/api/site/dicts'
    ),
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
  updateKnowledgeFreshness: (body: KnowledgeFreshnessUpdateRequest) =>
    req<KnowledgeFreshnessUpdateResponse>('/api/site/knowledge/freshness', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
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
