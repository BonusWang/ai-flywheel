import React, { useMemo, useState } from 'react'
import {
  AlertTriangle,
  Archive,
  ArrowUpRight,
  CheckCheck,
  ChevronDown,
  ChevronRight,
  EyeOff,
  GitBranch,
  Radar,
  ShieldAlert,
} from 'lucide-react'
import { api } from '../../api'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { PillTabs } from '../PageLayout'
import { StatusBadge } from '../StatusBadge'
import { CLIENT_LABELS, formatDateYMD, labelOf, RETRO_KIND_LABELS } from '../../lib/labels'
import type { DissentRecord, MistakeObservation, RetroItem } from '../../types'

type SubViewKey = 'action' | 'observations' | 'dissents' | 'retros'

type ActionItem =
  | { kind: 'mistake'; id: string; createdAt: string; obs: MistakeObservation }
  | { kind: 'dissent'; id: string; createdAt: string; dissent: DissentRecord }

interface RetroGroup {
  key: string
  ids: string[]
  latest: RetroItem
  count: number
  openCount: number
  clients: string[]
  sessions: string[]
}

const DISSENT_STATUS_LABELS: Record<string, string> = {
  open: '待收敛',
  merged: '已吸收',
  dismissed: '已忽略',
}

const DISSENT_TARGET_LABELS: Record<string, string> = {
  rule: '规则',
  skill: 'Skill',
  knowledge_doc: '知识文档',
}

function byCreatedDesc(a?: string | null, b?: string | null): number {
  return String(b || '').localeCompare(String(a || ''))
}

export const CluesObservationsTab: React.FC<{
  observations: MistakeObservation[]
  dissents: DissentRecord[]
  retros: RetroItem[]
  onChanged: () => Promise<void>
  onToast: (msg: string) => void
}> = ({ observations, dissents, retros, onChanged, onToast }) => {
  const [subView, setSubView] = useState<SubViewKey>('action')
  const [expandedIds, setExpandedIds] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState(false)

  // Filters per sub-view
  const [query, setQuery] = useState('')
  const [obsStatus, setObsStatus] = useState<string>('observing')
  const [obsSeverity, setObsSeverity] = useState<string>('all')
  const [dissentStatus, setDissentStatus] = useState<string>('open')
  const [retroStatus, setRetroStatus] = useState<string>('open')

  // Multi-select state per sub-view
  const [selectedObs, setSelectedObs] = useState<string[]>([])
  const [selectedDissents, setSelectedDissents] = useState<string[]>([])
  const [selectedRetroKeys, setSelectedRetroKeys] = useState<string[]>([])

  const toggleExpand = (id: string) =>
    setExpandedIds((prev) => ({ ...prev, [id]: !prev[id] }))

  const switchSubView = (next: SubViewKey) => {
    setSubView(next)
    setQuery('')
    setSelectedObs([])
    setSelectedDissents([])
    setSelectedRetroKeys([])
  }

  // ---- Derived datasets (all default created_at DESC) ----
  const highPriorityObs = useMemo(
    () =>
      observations
        .filter(
          (o) =>
            o.status === 'observing' &&
            (o.severity_grade === 'red_silent' || o.occurrence_count >= 3)
        )
        .sort((a, b) => byCreatedDesc(a.created_at, b.created_at)),
    [observations]
  )

  const openDissents = useMemo(
    () =>
      dissents
        .filter((d) => d.status === 'open')
        .sort((a, b) => byCreatedDesc(a.created_at, b.created_at)),
    [dissents]
  )

  const observingCount = useMemo(
    () => observations.filter((o) => o.status === 'observing').length,
    [observations]
  )

  const actionItems = useMemo<ActionItem[]>(() => {
    const q = query.trim().toLowerCase()
    const list: ActionItem[] = [
      ...highPriorityObs.map((obs) => ({
        kind: 'mistake' as const,
        id: `m:${obs.obs_id}`,
        createdAt: obs.created_at || obs.updated_at,
        obs,
      })),
      ...openDissents.map((dissent) => ({
        kind: 'dissent' as const,
        id: `d:${dissent.dissent_id}`,
        createdAt: dissent.created_at || dissent.updated_at,
        dissent,
      })),
    ].sort((a, b) => byCreatedDesc(a.createdAt, b.createdAt))

    if (!q) return list
    return list.filter((item) => {
      if (item.kind === 'mistake') {
        const o = item.obs
        return `${o.title} ${o.root_cause_key} ${o.thinking_root_cause} ${o.truth}`
          .toLowerCase()
          .includes(q)
      }
      const d = item.dissent
      return `${d.counter_claim} ${d.condition_scope} ${d.target_title || ''} ${d.target_id}`
        .toLowerCase()
        .includes(q)
    })
  }, [highPriorityObs, openDissents, query])

  const filteredObs = useMemo(() => {
    const q = query.trim().toLowerCase()
    return observations
      .filter((o) => (obsStatus === 'all' ? true : o.status === obsStatus))
      .filter((o) => (obsSeverity === 'all' ? true : o.severity_grade === obsSeverity))
      .filter((o) =>
        !q
          ? true
          : `${o.title} ${o.root_cause_key} ${o.thinking_root_cause} ${o.truth}`
              .toLowerCase()
              .includes(q)
      )
      .sort((a, b) => byCreatedDesc(a.created_at, b.created_at))
  }, [observations, obsStatus, obsSeverity, query])

  const filteredDissents = useMemo(() => {
    const q = query.trim().toLowerCase()
    return dissents
      .filter((d) => (dissentStatus === 'all' ? true : d.status === dissentStatus))
      .filter((d) =>
        !q
          ? true
          : `${d.counter_claim} ${d.condition_scope} ${d.target_title || ''} ${d.target_id} ${d.evidence_pointer}`
              .toLowerCase()
              .includes(q)
      )
      .sort((a, b) => byCreatedDesc(a.created_at, b.created_at))
  }, [dissents, dissentStatus, query])

  const groupedRetros = useMemo<RetroGroup[]>(() => {
    const q = query.trim().toLowerCase()
    const filtered = retros
      .filter((r) => {
        if (retroStatus === 'all') return true
        if (retroStatus === 'open') return r.status === 'open' || r.status === 'in_review'
        return r.status === retroStatus
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
      const key = `${stateBucket}::${item.kind}::${item.client}::${item.summary.trim()}`
      const existing = map.get(key)
      if (!existing) {
        map.set(key, {
          key,
          ids: [item.retro_id],
          latest: item,
          count: 1,
          openCount: stateBucket === 'open' ? 1 : 0,
          clients: [item.client],
          sessions: item.session_key ? [item.session_key] : [],
        })
      } else {
        existing.ids.push(item.retro_id)
        existing.count += 1
        if (stateBucket === 'open') existing.openCount += 1
        if (item.session_key && !existing.sessions.includes(item.session_key)) {
          existing.sessions.push(item.session_key)
        }
      }
    }
    return Array.from(map.values()).sort((a, b) =>
      byCreatedDesc(a.latest.created_at, b.latest.created_at)
    )
  }, [retros, retroStatus, query])

  const openRetroGroupCount = useMemo(() => {
    const keys = new Set<string>()
    for (const r of retros) {
      if (r.status === 'open' || r.status === 'in_review') {
        keys.add(`${r.kind}::${r.client}::${r.summary.trim()}`)
      }
    }
    return keys.size
  }, [retros])

  // Paginators (all default 10 per page)
  const actionPager = usePagination(actionItems, 10)
  const obsPager = usePagination(filteredObs, 10)
  const dissentPager = usePagination(filteredDissents, 10)
  const retroPager = usePagination(groupedRetros, 10)

  // ---- Actions ----
  const runAction = async (fn: () => Promise<void>) => {
    setBusy(true)
    try {
      await fn()
    } finally {
      setBusy(false)
    }
  }

  const handlePromoteObservation = (obsId: string) =>
    runAction(async () => {
      await api.promoteObservation(obsId)
      await onChanged()
      onToast('已将该错题升级为试运行避坑规则')
    })

  const handleArchiveObservation = (obsId: string) =>
    runAction(async () => {
      await api.archiveObservation(obsId)
      await onChanged()
      onToast('错题已归档')
    })

  const handleBulkArchiveObs = (ids: string[]) =>
    runAction(async () => {
      if (ids.length === 0) return
      const res = await api.bulkArchiveObservations(ids)
      setSelectedObs([])
      await onChanged()
      onToast(`已批量归档 ${res.updated} 条观察错题`)
    })

  const handleDissentStatus = (dissentId: string, status: 'merged' | 'dismissed') =>
    runAction(async () => {
      await api.updateDissentStatus(dissentId, status)
      await onChanged()
      onToast(status === 'merged' ? '分歧已标记为已吸收' : '分歧已忽略')
    })

  const handleBulkDissents = (ids: string[], status: 'merged' | 'dismissed') =>
    runAction(async () => {
      if (ids.length === 0) return
      const res = await api.bulkUpdateDissents(ids, status)
      setSelectedDissents([])
      await onChanged()
      onToast(`已批量${status === 'merged' ? '标记吸收' : '忽略'} ${res.updated} 条规则分歧`)
    })

  const handleRetroGroupStatus = (ids: string[], status: RetroItem['status']) =>
    runAction(async () => {
      if (ids.length === 0) return
      const res = await api.bulkUpdateRetro(ids, status)
      setSelectedRetroKeys([])
      await onChanged()
      onToast(`已批量标记 ${res.updated} 条线索为${status === 'resolved' ? '已处理' : '已忽略'}`)
    })

  const totalActionCount = highPriorityObs.length + openDissents.length

  const toggleSelect = (
    id: string,
    list: string[],
    setter: React.Dispatch<React.SetStateAction<string[]>>
  ) => {
    setter(list.includes(id) ? list.filter((x) => x !== id) : [...list, id])
  }

  return (
    <div className="space-y-4">
      {/* Sub-view Switcher */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PillTabs
          items={[
            { key: 'action', label: '待我处理', count: totalActionCount },
            { key: 'observations', label: '错题观察池', count: observingCount },
            { key: 'dissents', label: '规则分歧', count: openDissents.length },
            { key: 'retros', label: '实时异常线索', count: openRetroGroupCount },
          ]}
          active={subView}
          onChange={(k) => switchSubView(k as SubViewKey)}
        />
      </div>

      {/* ==================== 1. 待我处理 ==================== */}
      {subView === 'action' && (
        <section className="card p-5 space-y-3.5">
          <div className="rounded-[var(--radius-tile)] bg-accent-softer/70 border border-accent/20 px-4 py-2.5 flex flex-wrap items-center justify-between gap-2 text-[12.5px]">
            <div className="flex items-center gap-2 text-ink-2">
              <ShieldAlert className="w-4 h-4 text-accent shrink-0" />
              <span>
                仅汇总真正需要你拍板的高优项：
                <strong className="text-ink font-semibold"> 🔴 沉默错误 ({highPriorityObs.length})</strong>{' '}
                与 <strong className="text-ink font-semibold">未收敛规则分歧 ({openDissents.length})</strong>
                ；普通观察错题与运行线索已由系统自动流转，无需逐条确认。
              </span>
            </div>
          </div>

          <ListToolbar
            query={query}
            onQueryChange={(q) => {
              setQuery(q)
              actionPager.resetPage()
            }}
            placeholder="搜索高优错题或规则分歧…"
          />

          {actionItems.length === 0 ? (
            <div className="py-10 text-center text-[13px] text-ink-4">
              当前没有需要人工拍板的高优错题或分歧。系统仍在后台自动观察普通失误并按批次结算异常线索。
            </div>
          ) : (
            <div className="divide-y divide-line-soft">
              {actionPager.pageItems.map((item) => {
                const expanded = Boolean(expandedIds[item.id])
                if (item.kind === 'mistake') {
                  const obs = item.obs
                  return (
                    <div key={item.id} className="py-3 space-y-2">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <button
                          type="button"
                          onClick={() => toggleExpand(item.id)}
                          className="flex items-start gap-2 min-w-0 flex-1 text-left group"
                        >
                          {expanded ? (
                            <ChevronDown className="w-4 h-4 text-accent shrink-0 mt-0.5" />
                          ) : (
                            <ChevronRight className="w-4 h-4 text-ink-4 shrink-0 mt-0.5 group-hover:text-ink" />
                          )}
                          <div className="min-w-0 flex-1 space-y-1">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className="px-1.5 py-0.5 rounded bg-hot-soft text-hot text-[11px] font-semibold">
                                错题 · 需拍板
                              </span>
                              <StatusBadge status={obs.severity_grade} />
                              <span className="chip num">复现 {obs.occurrence_count}/3</span>
                              <span className="num text-[11.5px] text-ink-4">
                                {formatDateYMD(obs.created_at)}
                              </span>
                            </div>
                            <div className="text-[13.5px] font-semibold text-ink line-clamp-1">
                              {obs.title}
                            </div>
                          </div>
                        </button>
                        <div className="flex items-center gap-1.5 shrink-0">
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handlePromoteObservation(obs.obs_id)}
                            title="将其升级为试运行避坑规则，下次同类任务自动提醒"
                            className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[12px] font-medium hover:bg-accent/20 disabled:opacity-50"
                          >
                            <ArrowUpRight className="w-3.5 h-3.5" />
                            升为规则
                          </button>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handleArchiveObservation(obs.obs_id)}
                            title="归档为一次性噪音，不再提示"
                            className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px] hover:text-ink disabled:opacity-50"
                          >
                            归档
                          </button>
                        </div>
                      </div>
                      {expanded && (
                        <div className="ml-6 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12.5px] space-y-1.5">
                          <div className="mono text-[11.5px] text-ink-4">
                            根因标识：{obs.root_cause_key}
                          </div>
                          {obs.thinking_root_cause && (
                            <p className="text-ink-2">
                              <span className="text-ink-4">思维根因：</span>
                              {obs.thinking_root_cause}
                            </p>
                          )}
                          {obs.truth && (
                            <p className="text-ink-3">
                              <span className="text-ink-4">正确做法：</span>
                              {obs.truth}
                            </p>
                          )}
                        </div>
                      )}
                    </div>
                  )
                }

                const d = item.dissent
                return (
                  <div key={item.id} className="py-3 space-y-2">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <button
                        type="button"
                        onClick={() => toggleExpand(item.id)}
                        className="flex items-start gap-2 min-w-0 flex-1 text-left group"
                      >
                        {expanded ? (
                          <ChevronDown className="w-4 h-4 text-accent shrink-0 mt-0.5" />
                        ) : (
                          <ChevronRight className="w-4 h-4 text-ink-4 shrink-0 mt-0.5 group-hover:text-ink" />
                        )}
                        <div className="min-w-0 flex-1 space-y-1">
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[11px] font-semibold">
                              分歧 · 待收敛
                            </span>
                            <span className="chip truncate max-w-[280px]">
                              {DISSENT_TARGET_LABELS[d.target_type] || d.target_type}：{d.target_title || d.target_id}
                            </span>
                            <span className="num text-[11.5px] text-ink-4">
                              {formatDateYMD(d.created_at)}
                            </span>
                          </div>
                          <div className="text-[13.5px] font-medium text-ink line-clamp-1">
                            {d.counter_claim}
                          </div>
                        </div>
                      </button>
                      <div className="flex items-center gap-1.5 shrink-0">
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => handleDissentStatus(d.dissent_id, 'merged')}
                          title="确认该反例已吸收进规则条件，关闭待处理"
                          className="h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[12px] font-medium disabled:opacity-50"
                        >
                          已吸收
                        </button>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => handleDissentStatus(d.dissent_id, 'dismissed')}
                          title="判定为无效反例，忽略关闭"
                          className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px] hover:text-ink disabled:opacity-50"
                        >
                          忽略
                        </button>
                      </div>
                    </div>
                    {expanded && (
                      <div className="ml-6 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12.5px] space-y-1.5">
                        <p className="text-ink-2">
                          <span className="text-ink-4">反例说明：</span>
                          {d.counter_claim}
                        </p>
                        {d.condition_scope && (
                          <p className="text-ink-3">
                            <span className="text-ink-4">适用边界：</span>
                            {d.condition_scope}
                          </p>
                        )}
                        <div className="flex flex-wrap gap-3 text-[11.5px] text-ink-4 mono">
                          <span>目标：{d.target_id}</span>
                          {d.evidence_pointer && <span>来源：{d.evidence_pointer}</span>}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          <PaginationBar
            total={actionPager.total}
            page={actionPager.page}
            pageSize={actionPager.pageSize}
            totalPages={actionPager.totalPages}
            onPageChange={actionPager.setPage}
            onPageSizeChange={actionPager.setPageSize}
          />
        </section>
      )}

      {/* ==================== 2. 错题观察池 ==================== */}
      {subView === 'observations' && (
        <section className="card p-5 space-y-3.5">
          <div className="rounded-[var(--radius-tile)] bg-bg-sunk/65 border border-line-soft px-4 py-2.5 text-[12px] text-ink-3 flex flex-wrap items-center justify-between gap-2">
            <span>
              <strong className="text-ink">系统自动流转（事不过三）</strong>：普通失误（🟡/🟢）自动按根因计数，满 3 次自动晋升规则，超 30 天未复发单次低危错题由周五蒸馏自动归档；平时无需逐条操作。
            </span>
          </div>

          <ListToolbar
            query={query}
            onQueryChange={(q) => {
              setQuery(q)
              obsPager.resetPage()
            }}
            placeholder="搜索错题标题、根因或做法…"
            actions={
              selectedObs.length > 0 ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => handleBulkArchiveObs(selectedObs)}
                  className="inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-[12px] font-medium text-ink hover:border-accent"
                >
                  <Archive className="w-3.5 h-3.5" />
                  批量归档 ({selectedObs.length})
                </button>
              ) : null
            }
          >
            <select
              aria-label="错题状态筛选"
              value={obsStatus}
              onChange={(e) => {
                setObsStatus(e.target.value)
                setSelectedObs([])
                obsPager.resetPage()
              }}
              className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
            >
              <option value="observing">仅看观察中</option>
              <option value="all">全部状态</option>
              <option value="promoted">已升级规则</option>
              <option value="archived">已归档</option>
            </select>
            <select
              aria-label="错题级别筛选"
              value={obsSeverity}
              onChange={(e) => {
                setObsSeverity(e.target.value)
                setSelectedObs([])
                obsPager.resetPage()
              }}
              className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
            >
              <option value="all">全部级别</option>
              <option value="red_silent">🔴 沉默错误</option>
              <option value="yellow_caught">🟡 自查拦截</option>
              <option value="green_minor">🟢 轻微</option>
            </select>
          </ListToolbar>

          {filteredObs.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-8 text-center">没有符合条件的错题记录。</p>
          ) : (
            <div className="divide-y divide-line-soft">
              {obsPager.pageItems.map((obs) => {
                const key = `obs:${obs.obs_id}`
                const expanded = Boolean(expandedIds[key])
                const checked = selectedObs.includes(obs.obs_id)
                return (
                  <div key={obs.obs_id} className="py-3 space-y-2">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div className="flex items-start gap-2 min-w-0 flex-1">
                        {obs.status === 'observing' && (
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggleSelect(obs.obs_id, selectedObs, setSelectedObs)}
                            className="mt-1 size-3.5 accent-[var(--accent)]"
                          />
                        )}
                        <button
                          type="button"
                          onClick={() => toggleExpand(key)}
                          className="flex items-start gap-1.5 min-w-0 flex-1 text-left group"
                        >
                          {expanded ? (
                            <ChevronDown className="w-4 h-4 text-accent shrink-0 mt-0.5" />
                          ) : (
                            <ChevronRight className="w-4 h-4 text-ink-4 shrink-0 mt-0.5 group-hover:text-ink" />
                          )}
                          <div className="min-w-0 flex-1 space-y-1">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <StatusBadge status={obs.severity_grade} />
                              <StatusBadge status={obs.status} />
                              <span className="chip num">复现 {obs.occurrence_count}/3</span>
                              <span className="num text-[11.5px] text-ink-4">
                                {formatDateYMD(obs.created_at)}
                              </span>
                            </div>
                            <div className="text-[13.5px] font-semibold text-ink line-clamp-1">
                              {obs.title}
                            </div>
                          </div>
                        </button>
                      </div>
                      {obs.status === 'observing' && (
                        <div className="flex items-center gap-1.5 shrink-0">
                          {obs.severity_grade === 'red_silent' || obs.occurrence_count >= 3 ? (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => handlePromoteObservation(obs.obs_id)}
                              className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[12px] font-medium"
                            >
                              <ArrowUpRight className="w-3.5 h-3.5" />
                              升为规则
                            </button>
                          ) : (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => handlePromoteObservation(obs.obs_id)}
                              className="h-7 px-2 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[11.5px] hover:text-ink"
                            >
                              提前升规则
                            </button>
                          )}
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handleArchiveObservation(obs.obs_id)}
                            className="h-7 px-2 rounded-[var(--radius-mark)] border border-line text-ink-4 text-[11.5px] hover:text-ink"
                          >
                            归档
                          </button>
                        </div>
                      )}
                    </div>
                    {expanded && (
                      <div className="ml-8 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12.5px] space-y-1.5">
                        <div className="mono text-[11.5px] text-ink-4">
                          根因标识：{obs.root_cause_key}
                        </div>
                        {obs.thinking_root_cause && (
                          <p className="text-ink-2">
                            <span className="text-ink-4">思维根因：</span>
                            {obs.thinking_root_cause}
                          </p>
                        )}
                        {obs.truth && (
                          <p className="text-ink-3">
                            <span className="text-ink-4">正确做法：</span>
                            {obs.truth}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          <PaginationBar
            total={obsPager.total}
            page={obsPager.page}
            pageSize={obsPager.pageSize}
            totalPages={obsPager.totalPages}
            onPageChange={obsPager.setPage}
            onPageSizeChange={obsPager.setPageSize}
          />
        </section>
      )}

      {/* ==================== 3. 规则分歧 ==================== */}
      {subView === 'dissents' && (
        <section className="card p-5 space-y-3.5">
          <div className="rounded-[var(--radius-tile)] bg-amber-soft/50 border border-amber/25 px-4 py-2.5 text-[12px] text-ink-2 flex items-center gap-2">
            <GitBranch className="w-4 h-4 text-amber-ink shrink-0" />
            <span>
              <strong className="text-ink">规则分歧与反例</strong>：当既有规则在特定场景不适用时保留反例不覆盖原规则。确认已吸收进规则条件请点「已吸收」，判定无效反例请点「忽略」。
            </span>
          </div>

          <ListToolbar
            query={query}
            onQueryChange={(q) => {
              setQuery(q)
              dissentPager.resetPage()
            }}
            placeholder="搜索反例内容、适用边界或关联规则…"
            actions={
              selectedDissents.length > 0 ? (
                <div className="flex items-center gap-1.5">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => handleBulkDissents(selectedDissents, 'merged')}
                    className="h-8 px-2.5 rounded-[var(--radius-control)] bg-ok-soft text-ok-ink text-[12px] font-medium"
                  >
                    批量已吸收 ({selectedDissents.length})
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => handleBulkDissents(selectedDissents, 'dismissed')}
                    className="h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-ink-3 text-[12px]"
                  >
                    批量忽略
                  </button>
                </div>
              ) : null
            }
          >
            <select
              aria-label="分歧状态筛选"
              value={dissentStatus}
              onChange={(e) => {
                setDissentStatus(e.target.value)
                setSelectedDissents([])
                dissentPager.resetPage()
              }}
              className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
            >
              <option value="open">仅看待收敛</option>
              <option value="all">全部状态</option>
              <option value="merged">已吸收</option>
              <option value="dismissed">已忽略</option>
            </select>
          </ListToolbar>

          {filteredDissents.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-8 text-center">当前没有符合条件的规则分歧记录。</p>
          ) : (
            <div className="divide-y divide-line-soft">
              {dissentPager.pageItems.map((d) => {
                const key = `dis:${d.dissent_id}`
                const expanded = Boolean(expandedIds[key])
                const checked = selectedDissents.includes(d.dissent_id)
                return (
                  <div key={d.dissent_id} className="py-3 space-y-2">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div className="flex items-start gap-2 min-w-0 flex-1">
                        {d.status === 'open' && (
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() =>
                              toggleSelect(d.dissent_id, selectedDissents, setSelectedDissents)
                            }
                            className="mt-1 size-3.5 accent-[var(--accent)]"
                          />
                        )}
                        <button
                          type="button"
                          onClick={() => toggleExpand(key)}
                          className="flex items-start gap-1.5 min-w-0 flex-1 text-left group"
                        >
                          {expanded ? (
                            <ChevronDown className="w-4 h-4 text-accent shrink-0 mt-0.5" />
                          ) : (
                            <ChevronRight className="w-4 h-4 text-ink-4 shrink-0 mt-0.5 group-hover:text-ink" />
                          )}
                          <div className="min-w-0 flex-1 space-y-1">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span
                                className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${
                                  d.status === 'open'
                                    ? 'bg-amber-soft text-amber-ink'
                                    : d.status === 'merged'
                                      ? 'bg-ok-soft text-ok-ink'
                                      : 'bg-bg-sunk text-ink-4'
                                }`}
                              >
                                {DISSENT_STATUS_LABELS[d.status] || d.status}
                              </span>
                              <span className="chip truncate max-w-[300px]">
                                {DISSENT_TARGET_LABELS[d.target_type] || d.target_type}：{d.target_title || d.target_id}
                              </span>
                              <span className="num text-[11.5px] text-ink-4">
                                {formatDateYMD(d.created_at)}
                              </span>
                            </div>
                            <div className="text-[13.5px] font-medium text-ink line-clamp-1">
                              {d.counter_claim}
                            </div>
                          </div>
                        </button>
                      </div>
                      {d.status === 'open' && (
                        <div className="flex items-center gap-1.5 shrink-0">
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handleDissentStatus(d.dissent_id, 'merged')}
                            className="h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[12px] font-medium"
                          >
                            已吸收
                          </button>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handleDissentStatus(d.dissent_id, 'dismissed')}
                            className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px]"
                          >
                            忽略
                          </button>
                        </div>
                      )}
                    </div>
                    {expanded && (
                      <div className="ml-8 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12.5px] space-y-1.5">
                        <p className="text-ink-2">
                          <span className="text-ink-4">完整反例：</span>
                          {d.counter_claim}
                        </p>
                        {d.condition_scope && (
                          <p className="text-ink-3">
                            <span className="text-ink-4">适用条件：</span>
                            {d.condition_scope}
                          </p>
                        )}
                        <div className="flex flex-wrap gap-3 text-[11.5px] text-ink-4 mono">
                          <span>目标 ID：{d.target_id}</span>
                          {d.evidence_pointer && <span>证据：{d.evidence_pointer}</span>}
                          {d.session_key && <span>会话：{d.session_key}</span>}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          <PaginationBar
            total={dissentPager.total}
            page={dissentPager.page}
            pageSize={dissentPager.pageSize}
            totalPages={dissentPager.totalPages}
            onPageChange={dissentPager.setPage}
            onPageSizeChange={dissentPager.setPageSize}
          />
        </section>
      )}

      {/* ==================== 4. 实时异常线索 ==================== */}
      {subView === 'retros' && (
        <section className="card p-5 space-y-3.5">
          <div className="rounded-[var(--radius-tile)] bg-bg-sunk/65 border border-line-soft px-4 py-2.5 text-[12px] text-ink-3 flex items-center gap-2">
            <Radar className="w-4 h-4 text-accent shrink-0" />
            <span>
              <strong className="text-ink">系统自动复盘原料</strong>：由 Hook / MCP 实时捕获，每日 20:00 批次结算时自动标记已处理；同类重复信号已自动聚合显示，无需逐条手工点完，也可按组批量忽略清理噪音。
            </span>
          </div>

          <ListToolbar
            query={query}
            onQueryChange={(q) => {
              setQuery(q)
              retroPager.resetPage()
            }}
            placeholder="搜索异常摘要、工具名或客户端…"
            actions={
              <div className="flex items-center gap-1.5">
                {selectedRetroKeys.length > 0 && (
                  <>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        const ids = groupedRetros
                          .filter((g) => selectedRetroKeys.includes(g.key))
                          .flatMap((g) => g.ids)
                        handleRetroGroupStatus(ids, 'resolved')
                      }}
                      className="inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] bg-ok-soft text-ok-ink text-[12px] font-medium"
                    >
                      <CheckCheck className="w-3.5 h-3.5" />
                      批量已处理
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        const ids = groupedRetros
                          .filter((g) => selectedRetroKeys.includes(g.key))
                          .flatMap((g) => g.ids)
                        handleRetroGroupStatus(ids, 'dismissed')
                      }}
                      className="inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-ink-3 text-[12px]"
                    >
                      <EyeOff className="w-3.5 h-3.5" />
                      批量忽略
                    </button>
                  </>
                )}
                {retroStatus === 'open' && groupedRetros.length > 0 && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      handleRetroGroupStatus(
                        retroPager.pageItems.flatMap((g) => g.ids),
                        'dismissed'
                      )
                    }
                    className="h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-ink-3 text-[12px] hover:text-ink"
                  >
                    忽略本页全部
                  </button>
                )}
              </div>
            }
          >
            <select
              aria-label="线索状态筛选"
              value={retroStatus}
              onChange={(e) => {
                setRetroStatus(e.target.value)
                setSelectedRetroKeys([])
                retroPager.resetPage()
              }}
              className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
            >
              <option value="open">仅看待查看</option>
              <option value="all">全部（含历史已处理）</option>
              <option value="resolved">已处理</option>
              <option value="dismissed">已忽略</option>
            </select>
          </ListToolbar>

          {groupedRetros.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-8 text-center">暂无符合条件的实时异常线索。</p>
          ) : (
            <div className="divide-y divide-line-soft">
              {retroPager.pageItems.map((group) => {
                const item = group.latest
                const expanded = Boolean(expandedIds[group.key])
                const isOpen = item.status === 'open' || item.status === 'in_review'
                const checked = selectedRetroKeys.includes(group.key)
                return (
                  <div key={group.key} className="py-3 space-y-2">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div className="flex items-start gap-2 min-w-0 flex-1">
                        {isOpen && (
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() =>
                              toggleSelect(group.key, selectedRetroKeys, setSelectedRetroKeys)
                            }
                            className="mt-1 size-3.5 accent-[var(--accent)]"
                          />
                        )}
                        <button
                          type="button"
                          onClick={() => toggleExpand(group.key)}
                          className="flex items-start gap-1.5 min-w-0 flex-1 text-left group"
                        >
                          {expanded ? (
                            <ChevronDown className="w-4 h-4 text-accent shrink-0 mt-0.5" />
                          ) : (
                            <ChevronRight className="w-4 h-4 text-ink-4 shrink-0 mt-0.5 group-hover:text-ink" />
                          )}
                          <div className="min-w-0 flex-1 space-y-1">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <StatusBadge status={item.status} />
                              <span className="chip">{labelOf(RETRO_KIND_LABELS, item.kind)}</span>
                              <span className="chip">{labelOf(CLIENT_LABELS, item.client)}</span>
                              {group.count > 1 && (
                                <span className="px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[11px] font-semibold num">
                                  同类 ×{group.count}
                                </span>
                              )}
                              <span className="num text-[11.5px] text-ink-4">
                                最近 {formatDateYMD(item.created_at)}
                              </span>
                            </div>
                            <p className="text-[13px] text-ink line-clamp-1">{item.summary}</p>
                          </div>
                        </button>
                      </div>
                      {isOpen && (
                        <div className="flex items-center gap-1.5 shrink-0">
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handleRetroGroupStatus(group.ids, 'resolved')}
                            className="h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[12px] font-medium"
                          >
                            {group.count > 1 ? `整组已处理 (${group.count})` : '已处理'}
                          </button>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => handleRetroGroupStatus(group.ids, 'dismissed')}
                            className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px]"
                          >
                            忽略
                          </button>
                        </div>
                      )}
                    </div>
                    {expanded && (
                      <div className="ml-8 rounded-[var(--radius-tile)] bg-bg-sunk/60 border border-line-soft p-3 text-[12px] text-ink-3 space-y-1">
                        <p className="text-ink-2">{item.summary}</p>
                        <div className="flex flex-wrap gap-3 text-[11.5px] text-ink-4 mono">
                          {item.tool_name && <span>工具：{item.tool_name}</span>}
                          <span>合并条数：{group.count}</span>
                          {group.sessions.length > 0 && (
                            <span>涉及会话：{group.sessions.slice(0, 3).join(', ')}</span>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          <PaginationBar
            total={retroPager.total}
            page={retroPager.page}
            pageSize={retroPager.pageSize}
            totalPages={retroPager.totalPages}
            onPageChange={retroPager.setPage}
            onPageSizeChange={retroPager.setPageSize}
          />
        </section>
      )}
    </div>
  )
}
