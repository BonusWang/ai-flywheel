import type { DissentRecord, MistakeObservation, RetroAdvisory, RetroItem } from '../../types'

export type SubViewKey = 'action' | 'observations' | 'dissents' | 'retros'

export type ActionItem =
  | { kind: 'mistake'; id: string; createdAt: string; obs: MistakeObservation }
  | { kind: 'dissent'; id: string; createdAt: string; dissent: DissentRecord }

export interface RetroGroup {
  key: string
  ids: string[]
  latest: RetroItem
  count: number
  openCount: number
  maxAttempt: number
  clients: string[]
  sessions: string[]
  advisory?: RetroAdvisory
  hasQueuedSession: boolean
}

export const DISSENT_TARGET_LABELS: Record<string, string> = {
  rule: '规则',
  skill: 'Skill',
  knowledge_doc: '知识笔记',
}

export function canonicalRetroKey(item: RetroItem): string {
  if (item.advisory?.group_key) return item.advisory.group_key
  const tool = (item.tool_name || '').trim().toLowerCase()
  const norm = item.summary.trim().replace(/已连续尝试\s*\d+\s*次/g, '已连续尝试 N 次')
  if (item.kind === 'retry_loop' && tool) return `retry_loop::${tool}`
  return `${item.kind}::${norm}`
}

export function extractAttempt(item: RetroItem): number {
  if (item.advisory?.attempt && item.advisory.attempt > 0) return item.advisory.attempt
  const raw = item.detail?.attempt
  if (typeof raw === 'number' && raw > 0) return raw
  const m = item.summary.match(/已连续尝试\s*(\d+)\s*次/)
  return m ? Number(m[1]) : 0
}

export function byCreatedDesc(a?: string | null, b?: string | null): number {
  return String(b || '').localeCompare(String(a || ''))
}

export function groupRetroItems(
  retros: RetroItem[],
  statusFilter: string,
  query: string
): RetroGroup[] {
  const q = query.trim().toLowerCase()
  const filtered = retros
    .filter((r) => {
      if (statusFilter === 'all') return true
      if (statusFilter === 'open') return r.status === 'open' || r.status === 'in_review'
      return r.status === statusFilter
    })
    .filter((r) =>
      !q
        ? true
        : `${r.summary} ${r.kind} ${r.client} ${r.tool_name || ''}`.toLowerCase().includes(q)
    )
    .sort((a, b) => byCreatedDesc(a.created_at, b.created_at))

  const map = new Map<string, RetroGroup>()
  for (const item of filtered) {
    const stateBucket =
      item.status === 'open' || item.status === 'in_review' ? 'open' : item.status
    const key = `${stateBucket}::${canonicalRetroKey(item)}`
    const attempt = extractAttempt(item)
    const existing = map.get(key)
    if (!existing) {
      map.set(key, {
        key,
        ids: [item.retro_id],
        latest: item,
        count: 1,
        openCount: stateBucket === 'open' ? 1 : 0,
        maxAttempt: attempt,
        clients: [item.client],
        sessions: item.session_key ? [item.session_key] : [],
        advisory: item.advisory,
        hasQueuedSession: Boolean(item.has_queued_session),
      })
    } else {
      existing.ids.push(item.retro_id)
      existing.count += 1
      if (stateBucket === 'open') existing.openCount += 1
      if (attempt > existing.maxAttempt) existing.maxAttempt = attempt
      if (item.client && !existing.clients.includes(item.client)) {
        existing.clients.push(item.client)
      }
      if (item.session_key && !existing.sessions.includes(item.session_key)) {
        existing.sessions.push(item.session_key)
      }
      if (!existing.advisory && item.advisory) {
        existing.advisory = item.advisory
      }
      if (item.has_queued_session) {
        existing.hasQueuedSession = true
      }
    }
  }
  return Array.from(map.values()).sort((a, b) =>
    byCreatedDesc(a.latest.created_at, b.latest.created_at)
  )
}
