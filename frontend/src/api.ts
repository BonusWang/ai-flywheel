import type {
  AboutPayload,
  AuditEventRecord,
  ConfigSaveResponse,
  ConnectPayload,
  DissentRecord,
  GrowthMetricsPayload,
  KnowledgeDocSummary,
  MistakeObservation,
  OverviewResponse,
  PreflightResponse,
  RetroItem,
  RuleRecord,
  SessionRecord,
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
    req<{ runs: WorkflowRunRecord[]; lock: WorkflowLock | null }>('/api/site/runs'),
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

  listObservations: () => req<{ observations: MistakeObservation[] }>('/api/site/observations'),
  promoteObservation: (obsId: string) =>
    req<{ observation: MistakeObservation; rule: RuleRecord }>(
      `/api/site/observations/${obsId}/promote`,
      { method: 'POST' }
    ),
  archiveObservation: (obsId: string) =>
    req<{ observation: MistakeObservation }>(`/api/site/observations/${obsId}/archive`, {
      method: 'POST',
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

  growthMetrics: () => req<GrowthMetricsPayload>('/api/site/growth-metrics'),
  coreCard: () =>
    req<{ path: string; lines: number; bytes: number; content: string; source?: string }>(
      '/api/site/core-card'
    ),

  listRetro: () => req<{ items: RetroItem[] }>('/api/site/retro'),
  updateRetroStatus: (retroId: string, status: RetroItem['status']) =>
    req<{ item: RetroItem }>(`/api/site/retro/${retroId}/status`, {
      method: 'POST',
      body: JSON.stringify({ status }),
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
    req<{ sink_type: string; root: string; documents: KnowledgeDocSummary[] }>('/api/site/knowledge'),
  feishuCheckStatus: () =>
    req<{ state: string; valid?: boolean; reason?: string; listener?: { state: string; reason: string } | null }>(
      '/api/site/feishu-check'
    ),
  readKnowledgeDoc: (path: string) =>
    req<{ doc_path: string; content: string; sha256: string }>(
      `/api/site/knowledge/doc?path=${encodeURIComponent(path)}`
    ),
  skillPreview: (skillName: string) =>
    req<SkillPreview>(`/api/site/skills/${encodeURIComponent(skillName)}/preview`),
}
