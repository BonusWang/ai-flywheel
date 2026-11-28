import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Archive, GitBranch, Inbox, Loader2, Play, Plus } from 'lucide-react'
import type { RouteIntent } from '../App'
import { api } from '../api'
import { ListToolbar, PaginationBar, usePagination } from '../components/ListControls'
import { AnimatedNumber } from '../components/ui/AnimatedNumber'
import { Collapse } from '../components/ui/Collapse'
import { useToast } from '../components/ui/Toast'
import {
  ManualSessionModal,
  type ManualSessionInput,
} from '../components/workflows/ManualSessionModal'
import { QueueInventoryPanel } from '../components/workflows/QueueInventoryPanel'
import { RunPipelineRow } from '../components/workflows/RunPipelineRow'
import type { WorkflowDetailTab } from '../components/workflows/RunDetailPanel'
import { errorMessage } from '../lib/errors'
import {
  CLIENT_LABELS,
  FEISHU_LISTENER_STATE_LABELS,
  PHASE_LABELS,
  TRIGGER_LABELS,
  dictLabels,
  labelOf,
  preferredScrollBehavior,
} from '../lib/labels'
import type {
  AuditEventRecord,
  DictItem,
  DistillQueueItem,
  QueueSummary,
  SessionRecord,
  WorkflowRunRecord,
} from '../types'

interface Props {
  refreshToken: number
  onGlobalRefresh: () => void
  intent?: RouteIntent
}

interface ApprovalSelection {
  selected: number[]
  initCount: number
}

const LAST_CLIENT_STORAGE_KEY = 'flywheel-manual-client'

const KPI_STATUSES: Array<{
  status: keyof QueueSummary['by_status']
  label: string
  dot: string
}> = [
  { status: 'queued', label: '待整理', dot: 'bg-accent' },
  { status: 'processing', label: '处理中', dot: 'bg-amber' },
  { status: 'processed', label: '已沉淀', dot: 'bg-ok' },
  { status: 'excluded', label: '已移出', dot: 'bg-ink-4' },
  { status: 'withdrawn', label: '已撤回', dot: 'bg-ink-4' },
  { status: 'dead_letter', label: '处理失败', dot: 'bg-hot' },
]

const EMPTY_QUEUE_SUMMARY: QueueSummary = {
  total: 0,
  by_status: {
    queued: 0,
    processing: 0,
    processed: 0,
    excluded: 0,
    withdrawn: 0,
    dead_letter: 0,
  },
  by_client: {},
  by_channel: {
    mcp_explicit: 0,
    manual_portal: 0,
    codex_archive: 0,
    claude_exit: 0,
    legacy_scan: 0,
  },
  estimated_batches: 0,
}

export const WorkflowsPage: React.FC<Props> = ({ refreshToken, onGlobalRefresh, intent }) => {
  const toast = useToast()
  const [runs, setRuns] = useState<WorkflowRunRecord[]>([])
  const [sessions, setSessions] = useState<SessionRecord[]>([])
  const [queueItems, setQueueItems] = useState<DistillQueueItem[]>([])
  const [queueSummary, setQueueSummary] = useState<QueueSummary>(EMPTY_QUEUE_SUMMARY)
  const [dicts, setDicts] = useState<Record<string, DictItem[]>>({})
  const [loadedOnce, setLoadedOnce] = useState(false)
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null)
  const [highlightedRunId, setHighlightedRunId] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string>('P00_PREPARE')
  const [detailTab, setDetailTab] = useState<WorkflowDetailTab>('stages')
  const [runEvents, setRunEvents] = useState<AuditEventRecord[]>([])
  const [approvalSel, setApprovalSel] = useState<Record<string, ApprovalSelection>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [activeTab, setActiveTab] = useState<'queue' | 'pipeline' | null>(
    intent?.runsTab ?? (intent?.runId ? 'pipeline' : intent?.queueStatus ? 'queue' : null)
  )
  const [queueStatusFilter, setQueueStatusFilter] = useState(intent?.queueStatus ?? '')
  const [syncing, setSyncing] = useState(false)
  const [pendingJumpRunId, setPendingJumpRunId] = useState<string | null>(intent?.runId ?? null)

  const [showNewSession, setShowNewSession] = useState(false)
  const [newClient, setNewClient] = useState(() => {
    try {
      return localStorage.getItem(LAST_CLIENT_STORAGE_KEY) || 'claude-code'
    } catch {
      return 'claude-code'
    }
  })
  const [newTitle, setNewTitle] = useState('')
  const [newSummary, setNewSummary] = useState('')
  const [feishuLabel, setFeishuLabel] = useState('未配置')
  const [runQuery, setRunQuery] = useState('')
  const [runStatusFilter, setRunStatusFilter] = useState('all')
  const [runTriggerFilter, setRunTriggerFilter] = useState('all')

  // Sliding underline indicator for the dual workspace tabs
  const tabListRef = useRef<HTMLDivElement>(null)
  const queueTabRef = useRef<HTMLButtonElement>(null)
  const pipeTabRef = useRef<HTMLButtonElement>(null)
  const [tabBar, setTabBar] = useState<{ x: number; w: number; ready: boolean }>({
    x: 0,
    w: 0,
    ready: false,
  })
  const [tabAnimated, setTabAnimated] = useState(false)

  // Apply explicit deep-link navigation intents
  useEffect(() => {
    if (!intent?.nonce) return
    if (intent.runId) {
      setActiveTab('pipeline')
      setPendingJumpRunId(intent.runId)
    } else if (intent.runsTab) {
      setActiveTab(intent.runsTab)
    }
    if (intent.queueStatus !== undefined) {
      setQueueStatusFilter(intent.queueStatus)
    }
  }, [intent?.nonce, intent?.runId, intent?.runsTab, intent?.queueStatus])

  useEffect(() => {
    api
      .feishuCheckStatus()
      .then((st) => {
        if (st.enabled === false) {
          setFeishuLabel('未启用')
          return
        }
        if (st.interactive === false) {
          setFeishuLabel('仅文本通知（未开启交互卡片）')
          return
        }
        if (st.listener === null || st.listener === undefined) {
          setFeishuLabel('未配置')
          return
        }
        if (st.valid && st.listener.state === 'ready') {
          setFeishuLabel('链路已验证，可在飞书卡片上直接审批')
        } else if (st.listener.state === 'ready') {
          setFeishuLabel('监听就绪（自检未通过，审批走文本通知）')
        } else {
          const stateLabel = labelOf(FEISHU_LISTENER_STATE_LABELS, st.listener.state, '未知')
          setFeishuLabel(`监听${stateLabel}，审批走文本通知`)
        }
      })
      .catch(() => setFeishuLabel('未配置'))
  }, [refreshToken])

  const loadData = useCallback(async () => {
    const [runsOutcome, sessOutcome, queueOutcome, dictOutcome] = await Promise.allSettled([
      api.listRuns(),
      api.listSessions(),
      api.listQueue(),
      api.listDicts(),
    ])

    const errors: string[] = []
    let loadedRuns: WorkflowRunRecord[] | null = null
    let loadedQueueSummary: QueueSummary | null = null

    if (runsOutcome.status === 'fulfilled') {
      loadedRuns = runsOutcome.value.runs
      setRuns(loadedRuns)
    } else {
      errors.push(runsOutcome.reason?.message || '加载整理批次失败')
    }

    if (sessOutcome.status === 'fulfilled') {
      setSessions(sessOutcome.value.sessions)
    }

    if (queueOutcome.status === 'fulfilled') {
      setQueueItems(queueOutcome.value.items)
      loadedQueueSummary = queueOutcome.value.summary
      setQueueSummary(loadedQueueSummary)
    } else {
      const msg = String(queueOutcome.reason?.message || '')
      if (msg === 'Not Found' || msg.includes('404')) {
        errors.push('后台服务版本较旧，请重启 flywheel serve 后刷新页面。')
      } else {
        errors.push(`加载待整理队列失败：${msg || '未知错误'}`)
      }
    }

    if (dictOutcome.status === 'fulfilled') {
      setDicts(dictOutcome.value.dicts)
    }

    setError(errors.length > 0 ? errors.join('；') : null)
    setLoadedOnce(true)

    if (loadedRuns) {
      setActiveTab((prev) => {
        if (prev !== null) return prev
        const awaiting = loadedRuns!.some((run) => run.status === 'AWAITING_APPROVAL')
        const hasBacklog = (loadedQueueSummary?.by_status.queued ?? 0) > 0
        return awaiting || !hasBacklog ? 'pipeline' : 'queue'
      })

      setApprovalSel((prev) => {
        const next = { ...prev }
        for (const r of loadedRuns!) {
          const count = r.planned_writes.length
          const cur = next[r.run_id]
          if (!cur || cur.initCount !== count) {
            next[r.run_id] = {
              selected: r.planned_writes.map((_, idx) => idx),
              initCount: count,
            }
          }
        }
        return next
      })

      setExpandedRunId((prev) => {
        if (prev && loadedRuns!.some((r) => r.run_id === prev)) return prev
        const awaiting = loadedRuns!.find((r) => r.status === 'AWAITING_APPROVAL')
        return awaiting ? awaiting.run_id : null
      })
    }
  }, [])

  useEffect(() => {
    loadData()
  }, [refreshToken, loadData])

  useEffect(() => {
    if (!expandedRunId) {
      setRunEvents([])
      return
    }
    api
      .getRunEvents(expandedRunId)
      .then((res) => setRunEvents(res.events))
      .catch(() => setRunEvents([]))
  }, [expandedRunId, refreshToken])

  const awaitingCount = runs.filter((r) => r.status === 'AWAITING_APPROVAL').length
  const showQueueTab = activeTab === 'queue'
  const tabResolved = activeTab !== null

  const measureTabBar = useCallback(() => {
    if (!tabResolved) return
    const el = showQueueTab ? queueTabRef.current : pipeTabRef.current
    if (!el) return
    const next = { x: el.offsetLeft, w: el.offsetWidth, ready: true }
    setTabBar((prev) =>
      prev.ready && prev.x === next.x && prev.w === next.w ? prev : next
    )
  }, [showQueueTab, tabResolved])

  useLayoutEffect(() => {
    if (!tabResolved) return
    measureTabBar()
    const raf = requestAnimationFrame(() => setTabAnimated(true))
    return () => cancelAnimationFrame(raf)
  }, [measureTabBar, tabResolved, queueSummary.by_status.queued, runs.length, awaitingCount])

  useEffect(() => {
    const container = tabListRef.current
    if (!container || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => measureTabBar())
    ro.observe(container)
    return () => ro.disconnect()
  }, [measureTabBar])

  const clientLabels = useMemo(
    () => dictLabels(dicts, 'client_type', CLIENT_LABELS),
    [dicts]
  )
  const clientPartitions = useMemo(
    () =>
      Object.entries(queueSummary.by_client).sort(
        (a, b) => (b[1].total ?? 0) - (a[1].total ?? 0)
      ),
    [queueSummary]
  )

  const triggerRun = async (autoApprove: boolean | null = null) => {
    setBusy(true)
    setError(null)
    try {
      const res = await api.startRun(
        { phase: 'auto', auto_approve: autoApprove },
        { background: true }
      )
      setActiveTab('pipeline')
      if (res.run) {
        setExpandedRunId(res.run.run_id)
        setSelectedNodeId(res.run.status === 'AWAITING_APPROVAL' ? 'P04_WRITE_VERIFY' : 'P06_REPORT')
        setDetailTab(res.run.status === 'AWAITING_APPROVAL' ? 'stages' : 'digest')
      } else if (res.run_id) {
        setExpandedRunId(res.run_id)
        setSelectedNodeId('P00_PREPARE')
        setDetailTab('stages')
        setPendingJumpRunId(res.run_id)
      }
      toast.push({ tone: 'info', message: '已启动新一轮整理，进度将实时同步' })
      await loadData()
      onGlobalRefresh()
    } catch (e: unknown) {
      toast.push({ tone: 'error', message: errorMessage(e, '启动整理失败') })
    } finally {
      setBusy(false)
    }
  }

  const triggerQueueIds = async (queueIds: string[]) => {
    if (queueIds.length === 0) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.startRun(
        { phase: 'auto', queue_ids: queueIds },
        { background: true }
      )
      setActiveTab('pipeline')
      const runId = res.run?.run_id ?? res.run_id ?? null
      if (runId) {
        setExpandedRunId(runId)
        setSelectedNodeId('P00_PREPARE')
        setDetailTab('stages')
        setPendingJumpRunId(runId)
      }
      toast.push({ tone: 'info', message: `已将 ${queueIds.length} 段对话插队送入整理` })
      await loadData()
      onGlobalRefresh()
    } catch (e: unknown) {
      toast.push({ tone: 'error', message: errorMessage(e, '插队整理失败') })
    } finally {
      setBusy(false)
    }
  }

  const syncArchives = async () => {
    setSyncing(true)
    setError(null)
    try {
      const res = await api.syncQueue()
      const report = res.sync ?? {}
      const scanned = Number(report.scanned ?? 0)
      const enqueued = Number(report.enqueued ?? 0)
      const excluded = Number(report.excluded ?? 0)
      toast.push(
        enqueued > 0
          ? `已同步归档：扫描 ${scanned} 个文件，新加入 ${enqueued} 段待整理对话${
              excluded > 0 ? `（跳过 ${excluded} 段闲聊）` : ''
            }`
          : `已同步归档：扫描 ${scanned} 个文件，没有新的待整理对话`
      )
      await loadData()
      onGlobalRefresh()
    } catch (e: unknown) {
      toast.push({ tone: 'error', message: errorMessage(e, '同步归档失败') })
    } finally {
      setSyncing(false)
    }
  }

  const jumpToRun = (runId: string) => {
    setActiveTab('pipeline')
    setPendingJumpRunId(runId)
  }

  const handleApprove = async (run: WorkflowRunRecord) => {
    const indices = approvalSel[run.run_id]?.selected ?? run.planned_writes.map((_, i) => i)
    if (indices.length === 0) return
    setBusy(true)
    setError(null)
    try {
      await api.approveRun(run.run_id, indices, { background: true })
      setDetailTab('digest')
      toast.push(`已同意写入 ${indices.length} 项建议，正在落盘核验…`)
      await loadData()
      onGlobalRefresh()
    } catch (e: unknown) {
      toast.push({ tone: 'error', message: errorMessage(e, '审批提交失败') })
    } finally {
      setBusy(false)
    }
  }

  const handleReject = async (run: WorkflowRunRecord) => {
    setBusy(true)
    setError(null)
    try {
      await api.rejectRun(run.run_id, '用户在沉淀与审批页放弃本次写入')
      toast.push({ tone: 'info', message: '已放弃本批写入，相关对话已退回待整理队列' })
      await loadData()
      onGlobalRefresh()
    } catch (e: unknown) {
      toast.push({ tone: 'error', message: errorMessage(e, '取消写入失败') })
    } finally {
      setBusy(false)
    }
  }

  const togglePlannedIdx = (runId: string, idx: number) => {
    setApprovalSel((prev) => {
      const cur = prev[runId]?.selected ?? []
      return {
        ...prev,
        [runId]: {
          selected: cur.includes(idx) ? cur.filter((i) => i !== idx) : [...cur, idx],
          initCount: prev[runId]?.initCount ?? 0,
        },
      }
    })
  }

  const setAllPlanned = (run: WorkflowRunRecord, selectAll: boolean) => {
    setApprovalSel((prev) => ({
      ...prev,
      [run.run_id]: {
        selected: selectAll ? run.planned_writes.map((_, i) => i) : [],
        initCount: run.planned_writes.length,
      },
    }))
  }

  const submitNewSession = async (data: ManualSessionInput) => {
    setBusy(true)
    setError(null)
    try {
      try {
        localStorage.setItem(LAST_CLIENT_STORAGE_KEY, data.client)
      } catch {
        // ignore
      }
      await api.registerSession({
        client: data.client,
        session_id: `manual-${Date.now().toString(36)}`,
        title: data.title,
        project: 'generic',
        summary: data.summary || data.title,
        content: `User: ${data.title}\nAssistant: ${data.summary || data.title}`,
        tags: ['manual'],
      })
      setNewTitle('')
      setNewSummary('')
      setShowNewSession(false)
      toast.push(`已加入待整理队列：「${data.title}」`)
      await loadData()
      onGlobalRefresh()
    } catch (e: unknown) {
      toast.push({ tone: 'error', message: errorMessage(e, '添加对话失败') })
    } finally {
      setBusy(false)
    }
  }

  const toggleRow = (run: WorkflowRunRecord, nodeId?: string) => {
    if (nodeId) {
      setExpandedRunId(run.run_id)
      setSelectedNodeId(nodeId)
      setDetailTab('stages')
      return
    }
    if (expandedRunId === run.run_id) {
      setExpandedRunId(null)
    } else {
      setExpandedRunId(run.run_id)
      setSelectedNodeId(
        run.status === 'AWAITING_APPROVAL'
          ? 'P04_WRITE_VERIFY'
          : run.nodes[0]?.node_id || 'P00_PREPARE'
      )
      setDetailTab(run.status === 'PASS' ? 'digest' : 'stages')
    }
  }

  const filteredRuns = useMemo(() => {
    const q = runQuery.trim().toLowerCase()
    return [...runs]
      .filter((r) => (runStatusFilter === 'all' ? true : r.status === runStatusFilter))
      .filter((r) => (runTriggerFilter === 'all' ? true : r.trigger === runTriggerFilter))
      .filter((r) =>
        !q
          ? true
          : `${r.batch_date || ''} ${r.run_id} ${r.phase} ${labelOf(PHASE_LABELS, r.phase)} ${r.trigger} ${labelOf(TRIGGER_LABELS, r.trigger)} ${r.runner_mode} ${r.error || ''}`
              .toLowerCase()
              .includes(q)
      )
      .sort((a, b) =>
        String(b.started_at || b.created_at || '').localeCompare(
          String(a.started_at || a.created_at || '')
        )
      )
  }, [runs, runQuery, runStatusFilter, runTriggerFilter])

  const runsPager = usePagination(filteredRuns, 10)
  const latestRunId = runs[0]?.run_id
  const hasRunFilters =
    Boolean(runQuery.trim()) || runStatusFilter !== 'all' || runTriggerFilter !== 'all'

  useEffect(() => {
    if (!pendingJumpRunId) return
    const targetRunId = pendingJumpRunId
    const index = filteredRuns.findIndex((run) => run.run_id === targetRunId)
    if (index < 0) {
      const exists = runs.some((run) => run.run_id === targetRunId)
      if (exists && hasRunFilters) {
        setRunQuery('')
        setRunStatusFilter('all')
        setRunTriggerFilter('all')
      } else if (loadedOnce && !exists) {
        setPendingJumpRunId(null)
      }
      return
    }
    const targetPage = Math.floor(index / runsPager.pageSize) + 1
    if (runsPager.page !== targetPage) {
      runsPager.setPage(targetPage)
      return
    }
    const run = runs.find((entry) => entry.run_id === targetRunId)
    if (run) {
      setExpandedRunId(run.run_id)
      setSelectedNodeId(
        run.status === 'AWAITING_APPROVAL'
          ? 'P04_WRITE_VERIFY'
          : run.nodes[0]?.node_id || 'P00_PREPARE'
      )
      setDetailTab(run.status === 'PASS' ? 'digest' : 'stages')
    }
    setHighlightedRunId(targetRunId)
    requestAnimationFrame(() => {
      const row = document.getElementById(`run-row-${targetRunId}`)
      row?.scrollIntoView({ behavior: preferredScrollBehavior(), block: 'center' })
    })
    const clearTimer = window.setTimeout(() => setHighlightedRunId(null), 1350)
    setPendingJumpRunId(null)
    return () => window.clearTimeout(clearTimer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    pendingJumpRunId,
    filteredRuns,
    runs,
    runsPager.page,
    runsPager.pageSize,
    hasRunFilters,
  ])

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-4">
      {/* Header */}
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">沉淀与审批</h1>
          <p className="text-[13.5px] text-ink-3 mt-1">
            汇集各工具的对话记录，自动筛掉简短闲聊，提炼值得留存的知识笔记与避坑规则。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={syncArchives}
            disabled={syncing || busy}
            className="pressable inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] font-medium text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
          >
            {syncing ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Archive className="w-3.5 h-3.5" />
            )}
            <span>{syncing ? '同步中…' : '同步归档'}</span>
          </button>
          <button
            type="button"
            onClick={() => setShowNewSession((v) => !v)}
            aria-expanded={showNewSession}
            className={`pressable inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border text-[12.5px] font-medium ${
              showNewSession
                ? 'border-accent/45 bg-accent-soft text-accent-ink'
                : 'border-line bg-surface text-ink-2 hover:border-line-strong hover:text-ink'
            }`}
          >
            <Plus
              className={`w-3.5 h-3.5 transition-transform duration-200 ease-[var(--ease-out-quint)] ${
                showNewSession ? 'rotate-45' : ''
              }`}
            />
            <span>{showNewSession ? '收起补录' : '手工补录'}</span>
          </button>
          <button
            type="button"
            onClick={() => triggerRun(null)}
            disabled={busy}
            className="pressable inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink disabled:opacity-50"
          >
            {busy ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Play className="w-3.5 h-3.5" />
            )}
            <span>立即整理一轮</span>
          </button>
        </div>
      </header>

      {error && (
        <div className="animate-shake px-4 py-3 rounded-[var(--radius-card)] bg-hot-soft border border-hot/30 text-[12.5px] text-hot">
          {error}
        </div>
      )}

      {/* Smooth collapsible manual quick-entry form */}
      <Collapse open={showNewSession}>
        <div className="pb-1">
          <ManualSessionModal
            open={showNewSession}
            busy={busy}
            client={newClient}
            title={newTitle}
            summary={newSummary}
            onClientChange={setNewClient}
            onTitleChange={setNewTitle}
            onSummaryChange={setNewSummary}
            onClose={() => setShowNewSession(false)}
            onSubmit={submitNewSession}
          />
        </div>
      </Collapse>

      {/* Persistent queue overview strip (clickable status filter pills) */}
      <div className="card px-3.5 py-3 space-y-2.5">
        <div className="flex flex-wrap items-center gap-1.5">
          {KPI_STATUSES.map((kpi) => {
            const count = queueSummary.by_status[kpi.status] ?? 0
            const active = showQueueTab && queueStatusFilter === kpi.status
            return (
              <button
                key={kpi.status}
                type="button"
                aria-pressed={active}
                onClick={() => {
                  setActiveTab('queue')
                  setQueueStatusFilter((prev) => (prev === kpi.status ? '' : kpi.status))
                }}
                title={active ? '再次点击取消筛选' : `筛选「${kpi.label}」`}
                className={`pressable inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-[12px] ${
                  active
                    ? 'border-accent/45 bg-accent-soft font-semibold text-accent-ink'
                    : 'border-line bg-surface text-ink-3 hover:border-line-strong hover:text-ink'
                }`}
              >
                <span className={`size-1.5 rounded-full ${kpi.dot}`} />
                <span>{kpi.label}</span>
                <AnimatedNumber value={count} className="font-semibold text-ink" />
              </button>
            )
          })}
          <span className="ml-auto text-[12px] text-ink-4">
            {queueSummary.estimated_batches > 0 ? (
              <>
                预计约{' '}
                <AnimatedNumber
                  value={queueSummary.estimated_batches}
                  className="font-semibold text-ink-2"
                />{' '}
                轮整理完毕
              </>
            ) : (
              '待整理队列已清空'
            )}
          </span>
        </div>

        {clientPartitions.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 border-t border-line-soft pt-2 text-[11.5px] text-ink-4">
            <span>来源工具：</span>
            {clientPartitions.map(([client, bucket]) => (
              <span key={client} className="chip num">
                <span className="font-medium text-ink-2">
                  {labelOf(clientLabels, client, client)}
                </span>
                <span className="text-ink-4">待整理</span>
                <span className="font-semibold text-ink">{bucket.queued ?? 0}</span>
                <span className="text-ink-4">/</span>
                <span className="text-ink-4">已沉淀</span>
                <span className="font-semibold text-ink">{bucket.processed ?? 0}</span>
              </span>
            ))}
            <span className="ml-auto">
              共 <AnimatedNumber value={queueSummary.total} className="text-ink-2" /> 段对话
            </span>
          </div>
        )}
      </div>

      {/* Dual tab switcher with measured GPU-sliding underline */}
      <div
        ref={tabListRef}
        className="relative flex flex-wrap items-center gap-1 border-b border-line"
        role="tablist"
        aria-label="沉淀工作区视图"
      >
        {tabBar.ready && (
          <span
            aria-hidden="true"
            className={`pointer-events-none absolute bottom-0 left-0 h-[2px] rounded-full bg-accent ${
              tabAnimated
                ? 'transition-[transform,width] duration-[220ms] ease-[var(--ease-out-quint)]'
                : ''
            }`}
            style={{
              transform: `translate3d(${tabBar.x}px, 0, 0)`,
              width: `${tabBar.w}px`,
            }}
          />
        )}
        <button
          ref={queueTabRef}
          type="button"
          role="tab"
          aria-selected={showQueueTab}
          onClick={() => setActiveTab('queue')}
          className={`pressable inline-flex items-center gap-1.5 px-3 py-2 text-[13px] ${
            showQueueTab ? 'font-semibold text-ink' : 'text-ink-3 hover:text-ink'
          }`}
        >
          <Inbox className="w-4 h-4" />
          <span>待整理对话</span>
          <span className="num inline-flex h-[18px] min-w-[20px] items-center justify-center rounded-full bg-bg-sunk px-1.5 text-[11px] font-semibold text-ink-2">
            {queueSummary.by_status.queued}
          </span>
        </button>
        <button
          ref={pipeTabRef}
          type="button"
          role="tab"
          aria-selected={tabResolved && !showQueueTab}
          onClick={() => setActiveTab('pipeline')}
          className={`pressable inline-flex items-center gap-1.5 px-3 py-2 text-[13px] ${
            tabResolved && !showQueueTab ? 'font-semibold text-ink' : 'text-ink-3 hover:text-ink'
          }`}
        >
          <GitBranch className="w-4 h-4" />
          <span>整理批次</span>
          <span className="num inline-flex h-[18px] min-w-[20px] items-center justify-center rounded-full bg-bg-sunk px-1.5 text-[11px] font-semibold text-ink-2">
            {runs.length}
          </span>
          {awaitingCount > 0 && (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-soft px-1.5 py-0.5 text-[11px] font-medium text-amber-ink">
              <span className="status-dot-live bg-amber" />
              {awaitingCount} 待审批
            </span>
          )}
        </button>
      </div>

      {!loadedOnce ? (
        <div className="card p-5 space-y-3" aria-busy="true">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="skeleton-block h-14 w-full" />
          ))}
        </div>
      ) : showQueueTab ? (
        <div key="queue-panel" className="animate-tab-enter">
          <QueueInventoryPanel
            items={queueItems}
            dicts={dicts}
            statusFilter={queueStatusFilter}
            onStatusFilterChange={setQueueStatusFilter}
            busy={busy}
            onRefresh={loadData}
            onTriggerQueueIds={triggerQueueIds}
            onJumpToRun={jumpToRun}
          />
        </div>
      ) : (
        <div key="pipeline-panel" className="animate-tab-enter space-y-3">
          {runs.length > 0 && (
            <ListToolbar
              query={runQuery}
              onQueryChange={(q) => {
                setRunQuery(q)
                runsPager.resetPage()
              }}
              placeholder="按日期、批次号或错误信息搜索…"
              enableSlashShortcut
              hasActiveFilters={hasRunFilters}
              onResetFilters={() => {
                setRunQuery('')
                setRunStatusFilter('all')
                setRunTriggerFilter('all')
                runsPager.resetPage()
              }}
            >
              <select
                aria-label="批次状态筛选"
                value={runStatusFilter}
                onChange={(e) => {
                  setRunStatusFilter(e.target.value)
                  runsPager.resetPage()
                }}
                className="field-input h-8 px-2.5 text-[12px]"
              >
                <option value="all">全部状态</option>
                <option value="AWAITING_APPROVAL">待审批</option>
                <option value="PASS">已通过</option>
                <option value="FAILED">已失败</option>
                <option value="PARTIAL">部分完成</option>
                <option value="CANCELLED">已取消</option>
                <option value="RUNNING">运行中</option>
              </select>
              <select
                aria-label="触发方式筛选"
                value={runTriggerFilter}
                onChange={(e) => {
                  setRunTriggerFilter(e.target.value)
                  runsPager.resetPage()
                }}
                className="field-input h-8 px-2.5 text-[12px]"
              >
                <option value="all">全部触发方式</option>
                <option value="scheduled">定时调度</option>
                <option value="api">手动触发</option>
                <option value="cli">命令行触发</option>
              </select>
            </ListToolbar>
          )}

          {filteredRuns.length === 0 ? (
            <div className="card p-12 text-center space-y-3">
              <p className="text-[13.5px] text-ink-4">
                {runs.length === 0
                  ? '还没有整理批次，点击右上角「立即整理一轮」开始第一次沉淀。'
                  : '没有符合当前筛选条件的整理批次。'}
              </p>
              {hasRunFilters && (
                <button
                  type="button"
                  onClick={() => {
                    setRunQuery('')
                    setRunStatusFilter('all')
                    setRunTriggerFilter('all')
                  }}
                  className="pressable inline-flex items-center h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-2 hover:border-line-strong hover:text-ink"
                >
                  清空筛选条件
                </button>
              )}
            </div>
          ) : (
            <div className="card overflow-hidden">
              <div className="divide-y divide-line">
                {runsPager.pageItems.map((run, idx) => (
                  <div
                    key={run.run_id}
                    id={`run-row-${run.run_id}`}
                    className={
                      highlightedRunId === run.run_id ? 'animate-row-highlight' : ''
                    }
                  >
                    <RunPipelineRow
                      run={run}
                      isLatest={run.run_id === latestRunId}
                      isExpanded={expandedRunId === run.run_id}
                      batchNo={
                        run.batch_seq ||
                        filteredRuns.length - (runsPager.page - 1) * runsPager.pageSize - idx
                      }
                      selectedNodeId={selectedNodeId}
                      detailTab={detailTab}
                      sessions={sessions}
                      events={runEvents}
                      approvedIndices={approvalSel[run.run_id]?.selected ?? []}
                      busy={busy}
                      feishuLabel={feishuLabel}
                      onToggleRow={toggleRow}
                      onTabChange={setDetailTab}
                      onSelectNode={setSelectedNodeId}
                      onToggleIndex={(pIdx) => togglePlannedIdx(run.run_id, pIdx)}
                      onSelectAll={(selectAll) => setAllPlanned(run, selectAll)}
                      onApprove={() => handleApprove(run)}
                      onReject={() => handleReject(run)}
                    />
                  </div>
                ))}
              </div>
              <div className="px-4 pb-3">
                <PaginationBar
                  total={runsPager.total}
                  page={runsPager.page}
                  pageSize={runsPager.pageSize}
                  totalPages={runsPager.totalPages}
                  onPageChange={runsPager.setPage}
                  onPageSizeChange={runsPager.setPageSize}
                />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
