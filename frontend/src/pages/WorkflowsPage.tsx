import React, { useEffect, useMemo, useState } from 'react'
import { Archive, GitBranch, Inbox, Play, Plus } from 'lucide-react'
import { api } from '../api'
import { ListToolbar, PaginationBar, usePagination } from '../components/ListControls'
import {
  ManualSessionModal,
  type ManualSessionInput,
} from '../components/workflows/ManualSessionModal'
import { QueueInventoryPanel } from '../components/workflows/QueueInventoryPanel'
import { RunPipelineRow } from '../components/workflows/RunPipelineRow'
import type { WorkflowDetailTab } from '../components/workflows/RunDetailPanel'
import {
  CLIENT_LABELS,
  FEISHU_LISTENER_STATE_LABELS,
  dictLabels,
  labelOf,
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
}

/**
 * The approver's selection per run.
 *
 * `initCount` is how many planned writes the selection was built from. A run
 * that is still RUNNING has no writes yet; when it reaches AWAITING_APPROVAL
 * it has N of them. Without the count the selection would stay the empty one
 * it was initialized with, and "同意并写入" would approve nothing.
 */
interface ApprovalSelection {
  selected: number[]
  initCount: number
}

/** The inventory strip's six clickable status counters, in lifecycle order. */
const KPI_STATUSES: Array<{
  status: keyof QueueSummary['by_status']
  label: string
  dot: string
}> = [
  { status: 'queued', label: '待处理库存', dot: 'bg-accent' },
  { status: 'processing', label: '处理中', dot: 'bg-amber' },
  { status: 'processed', label: '已沉淀', dot: 'bg-ok' },
  { status: 'excluded', label: '已排除', dot: 'bg-ink-4' },
  { status: 'withdrawn', label: '已撤回', dot: 'bg-ink-4' },
  { status: 'dead_letter', label: '异常死信', dot: 'bg-hot' },
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

export const WorkflowsPage: React.FC<Props> = ({ refreshToken, onGlobalRefresh }) => {
  const [runs, setRuns] = useState<WorkflowRunRecord[]>([])
  const [sessions, setSessions] = useState<SessionRecord[]>([])
  const [queueItems, setQueueItems] = useState<DistillQueueItem[]>([])
  const [queueSummary, setQueueSummary] = useState<QueueSummary>(EMPTY_QUEUE_SUMMARY)
  const [dicts, setDicts] = useState<Record<string, DictItem[]>>({})
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string>('P00_PREPARE')
  const [detailTab, setDetailTab] = useState<WorkflowDetailTab>('stages')
  const [runEvents, setRunEvents] = useState<AuditEventRecord[]>([])
  const [approvalSel, setApprovalSel] = useState<Record<string, ApprovalSelection>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // `null` means "the default has not been resolved yet": the first load picks
  // queue vs. pipeline once, and after that only an explicit click moves it,
  // so a live refresh never flips the tab out from under the user.
  const [activeTab, setActiveTab] = useState<'queue' | 'pipeline' | null>(null)
  const [queueStatusFilter, setQueueStatusFilter] = useState('')
  const [syncing, setSyncing] = useState(false)
  const [syncNotice, setSyncNotice] = useState<string | null>(null)
  const [pendingJumpRunId, setPendingJumpRunId] = useState<string | null>(null)

  const [showNewSession, setShowNewSession] = useState(false)
  const [newClient, setNewClient] = useState('claude-code')
  const [newTitle, setNewTitle] = useState('')
  const [newSummary, setNewSummary] = useState('')
  const [feishuLabel, setFeishuLabel] = useState('未配置')
  const [runQuery, setRunQuery] = useState('')
  const [runStatusFilter, setRunStatusFilter] = useState('all')
  const [runTriggerFilter, setRunTriggerFilter] = useState('all')

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

  const loadData = async () => {
    try {
      const [runsRes, sessRes, queueRes, dictRes] = await Promise.all([
        api.listRuns(),
        api.listSessions(),
        api.listQueue(),
        api.listDicts(),
      ])
      setRuns(runsRes.runs)
      setSessions(sessRes.sessions)
      setQueueItems(queueRes.items)
      setQueueSummary(queueRes.summary)
      setDicts(dictRes.dicts)

      // Resolve the default tab exactly once: a batch waiting for approval is
      // the most urgent thing on the page, otherwise a non-empty backlog opens
      // the queue. A refresh never reaches this branch again for that session.
      setActiveTab((prev) => {
        if (prev !== null) return prev
        const awaiting = runsRes.runs.some((run) => run.status === 'AWAITING_APPROVAL')
        const hasBacklog = (queueRes.summary.by_status.queued ?? 0) > 0
        return awaiting || !hasBacklog ? 'pipeline' : 'queue'
      })

      // Default to approving every planned write. The selection is rebuilt
      // whenever the write count changes, so a run that reaches the approval
      // gate with N writes is approved as a whole instead of nothing.
      setApprovalSel((prev) => {
        const next = { ...prev }
        for (const r of runsRes.runs) {
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

      // Auto-expand any run waiting for approval on first load
      setExpandedRunId((prev) => {
        if (prev && runsRes.runs.some((r) => r.run_id === prev)) return prev
        const awaiting = runsRes.runs.find((r) => r.status === 'AWAITING_APPROVAL')
        return awaiting ? awaiting.run_id : null
      })
    } catch (e: any) {
      setError(e.message)
    }
  }

  useEffect(() => {
    loadData()
  }, [refreshToken])

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

  useEffect(() => {
    if (!syncNotice) return
    const timer = window.setTimeout(() => setSyncNotice(null), 8000)
    return () => window.clearTimeout(timer)
  }, [syncNotice])

  const awaitingCount = runs.filter((r) => r.status === 'AWAITING_APPROVAL').length
  const showQueueTab = activeTab === 'queue'

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
      // Background: the answer comes at once and the batch follows on the SSE
      // stream, so a long run no longer holds the request open.
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
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  /** Claim exactly these inventory items on the next batch (插队). */
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
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
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
      const withdrawn = Number(report.withdrawn ?? 0)
      setSyncNotice(
        `已同步归档：扫描 ${scanned} 个归档文件，新入队 ${enqueued} 条，规则排除 ${excluded} 条，撤回 ${withdrawn} 条${
          res.bridged > 0 ? `，补录历史会话 ${res.bridged} 条` : ''
        }。`
      )
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setSyncing(false)
    }
  }

  /** Switch to the pipeline tab, expand the batch and scroll it into view. */
  const jumpToRun = (runId: string) => {
    setActiveTab('pipeline')
    setPendingJumpRunId(runId)
  }

  const handleApprove = async (run: WorkflowRunRecord) => {
    setBusy(true)
    setError(null)
    try {
      const indices = approvalSel[run.run_id]?.selected ?? run.planned_writes.map((_, i) => i)
      await api.approveRun(run.run_id, indices, { background: true })
      setDetailTab('digest')
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const handleReject = async (run: WorkflowRunRecord) => {
    setBusy(true)
    setError(null)
    try {
      await api.rejectRun(run.run_id, '用户在每日沉淀列表中取消本次写入')
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
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

  const submitNewSession = async (data: ManualSessionInput) => {
    setBusy(true)
    setError(null)
    try {
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
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
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
          : `${r.batch_date || ''} ${r.run_id} ${r.phase} ${r.trigger} ${r.runner_mode} ${r.error || ''}`
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

  // Land a queue-row "batch" click on the pipeline tab: expand the run, move
  // the pager to its page, then scroll it into view. Runs that have not loaded
  // yet stay pending until the next refresh brings them.
  useEffect(() => {
    if (!pendingJumpRunId) return
    const targetRunId = pendingJumpRunId
    const index = filteredRuns.findIndex((run) => run.run_id === targetRunId)
    if (index < 0) {
      const exists = runs.some((run) => run.run_id === targetRunId)
      if (exists && (runQuery || runStatusFilter !== 'all' || runTriggerFilter !== 'all')) {
        // The active query hides the run: clear the filters so the jump lands.
        setRunQuery('')
        setRunStatusFilter('all')
        setRunTriggerFilter('all')
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
    const row = document.getElementById(`run-row-${targetRunId}`)
    if (row) {
      row.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
    setPendingJumpRunId(null)
    // The effect intentionally follows the pager and filter state it acts on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    pendingJumpRunId,
    filteredRuns,
    runs,
    runsPager.page,
    runsPager.pageSize,
    runQuery,
    runStatusFilter,
    runTriggerFilter,
  ])

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-4">
      {/* Header */}
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">沉淀与审批</h1>
          <p className="text-[13.5px] text-ink-3 mt-1">
            工作流是流水线，队列是库存：顶部常驻库存条盯住积压与清仓进度，下方双 Tab 管理待沉淀队列与 P00–P07 批次。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={syncArchives}
            disabled={syncing || busy}
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] font-medium text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
          >
            <Archive className="w-3.5 h-3.5" />
            {syncing ? '同步中…' : '同步归档'}
          </button>
          <button
            type="button"
            onClick={() => setShowNewSession((v) => !v)}
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] font-medium text-ink-2 hover:border-line-strong hover:text-ink"
          >
            <Plus className="w-3.5 h-3.5" />
            手工补录对话
          </button>
          <button
            type="button"
            onClick={() => triggerRun(null)}
            disabled={busy}
            className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink disabled:opacity-50"
          >
            <Play className="w-3.5 h-3.5" />
            立即跑一轮
          </button>
        </div>
      </header>

      {syncNotice && (
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2.5 rounded-[var(--radius-card)] bg-ok-soft border border-ok/30 text-[12.5px] text-ok-ink">
          <span>{syncNotice}</span>
          <button
            type="button"
            onClick={() => setSyncNotice(null)}
            className="text-[11.5px] text-ok-ink/80 hover:text-ok-ink"
          >
            关闭
          </button>
        </div>
      )}

      {error && (
        <div className="px-4 py-3 rounded-[var(--radius-card)] bg-hot-soft border border-hot/30 text-[12.5px] text-hot">
          {error}
        </div>
      )}

      {/* Persistent inventory & progress strip, visible on both tabs */}
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
                title={`切换到沉淀队列并筛选「${kpi.label}」`}
                className={`inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-[12px] transition-colors ${
                  active
                    ? 'border-accent/45 bg-accent-soft font-semibold text-accent-ink'
                    : 'border-line bg-surface text-ink-3 hover:border-line-strong hover:text-ink'
                }`}
              >
                <span className={`size-1.5 rounded-full ${kpi.dot}`} />
                <span>{kpi.label}</span>
                <span className="num font-semibold text-ink">{count}</span>
              </button>
            )
          })}
          <span className="ml-auto text-[12px] text-ink-4">
            {queueSummary.estimated_batches > 0 ? (
              <>
                预计约{' '}
                <span className="num font-semibold text-ink-2">
                  {queueSummary.estimated_batches}
                </span>{' '}
                批清空当前库存
              </>
            ) : (
              '当前库存已清空'
            )}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-1.5 border-t border-line-soft pt-2 text-[11.5px] text-ink-4">
          <span>按接入工具：</span>
          {clientPartitions.length === 0 ? (
            <span>暂无库存分片</span>
          ) : (
            clientPartitions.map(([client, bucket]) => (
              <span key={client} className="chip num">
                <span className="font-medium text-ink-2">
                  {labelOf(clientLabels, client, client)}：
                </span>
                <span className="text-ink-4">待处理</span>
                <span className="font-semibold text-ink">{bucket.queued ?? 0}</span>
                <span className="text-ink-4">/</span>
                <span className="text-ink-4">已沉淀</span>
                <span className="font-semibold text-ink">{bucket.processed ?? 0}</span>
              </span>
            ))
          )}
          <span className="ml-auto">
            共 <span className="num text-ink-2">{queueSummary.total}</span> 条库存记录
          </span>
        </div>
      </div>

      {/* Dual tab switcher: inventory vs. pipeline */}
      <div
        className="flex flex-wrap items-center gap-1 border-b border-line"
        role="tablist"
        aria-label="沉淀工作区视图"
      >
        <button
          type="button"
          role="tab"
          aria-selected={showQueueTab}
          onClick={() => setActiveTab('queue')}
          className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13px] transition-colors ${
            showQueueTab
              ? 'border-accent font-semibold text-ink'
              : 'border-transparent text-ink-3 hover:text-ink'
          }`}
        >
          <Inbox className="w-4 h-4" />
          沉淀队列（库存）
          <span className="num inline-flex h-[18px] min-w-[20px] items-center justify-center rounded-full bg-bg-sunk px-1.5 text-[11px] font-semibold text-ink-2">
            {queueSummary.by_status.queued}
          </span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={!showQueueTab}
          onClick={() => setActiveTab('pipeline')}
          className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13px] transition-colors ${
            !showQueueTab
              ? 'border-accent font-semibold text-ink'
              : 'border-transparent text-ink-3 hover:text-ink'
          }`}
        >
          <GitBranch className="w-4 h-4" />
          执行流水线（P00–P07）
          <span className="num inline-flex h-[18px] min-w-[20px] items-center justify-center rounded-full bg-bg-sunk px-1.5 text-[11px] font-semibold text-ink-2">
            {runs.length}
          </span>
          {awaitingCount > 0 && (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-soft px-1.5 text-[11px] font-medium text-amber-ink">
              <span className="size-1.5 rounded-full bg-amber" />
              {awaitingCount} 待审批
            </span>
          )}
        </button>
      </div>

      {showNewSession && (
        <ManualSessionModal
          busy={busy}
          client={newClient}
          title={newTitle}
          summary={newSummary}
          onClientChange={setNewClient}
          onTitleChange={setNewTitle}
          onSummaryChange={setNewSummary}
          onSubmit={submitNewSession}
        />
      )}

      {showQueueTab ? (
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
      ) : (
        <>
          {/* Search, filter & sort bar */}
          <ListToolbar
            query={runQuery}
            onQueryChange={(q) => {
              setRunQuery(q)
              runsPager.resetPage()
            }}
            placeholder="按日期、批次 ID、执行模式或错误信息搜索…"
          >
            <select
              aria-label="批次状态筛选"
              value={runStatusFilter}
              onChange={(e) => {
                setRunStatusFilter(e.target.value)
                runsPager.resetPage()
              }}
              className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
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
              className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
            >
              <option value="all">全部触发</option>
              <option value="scheduled">定时调度</option>
              <option value="api">手动触发</option>
              <option value="cli">CLI 触发</option>
            </select>
          </ListToolbar>

          {/* GitLab-CI-style Daily Batch Pipeline Rows */}
          {filteredRuns.length === 0 ? (
            <div className="card p-12 text-center text-[13.5px] text-ink-4">
              {runs.length === 0
                ? '还没有沉淀批次，点击右上角「立即跑一轮」开始第一次整理。'
                : '没有符合当前查询或筛选条件的批次记录。'}
            </div>
          ) : (
            <div className="card overflow-hidden">
              <div className="divide-y divide-line">
                {runsPager.pageItems.map((run, idx) => (
                  <div key={run.run_id} id={`run-row-${run.run_id}`}>
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
                      onToggleIndex={(idx) => togglePlannedIdx(run.run_id, idx)}
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
        </>
      )}
    </div>
  )
}
