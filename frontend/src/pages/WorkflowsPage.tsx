import React, { useEffect, useState } from 'react'
import {
  Calendar,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Clock,
  Loader2,
  Minus,
  Pause,
  Play,
  Plus,
  X,
  XCircle,
} from 'lucide-react'
import { api } from '../api'
import { MarkdownView } from '../components/MarkdownView'
import { PillTabs } from '../components/PageLayout'
import { StatusBadge } from '../components/StatusBadge'
import {
  APPROVAL_MODE_LABELS,
  CLIENT_LABELS,
  formatDateYMD,
  formatDuration,
  formatRelativeTime,
  labelOf,
  PHASE_LABELS,
  RUNNER_MODE_LABELS,
  STAGE_PLAIN_NAMES,
  TRIGGER_LABELS,
} from '../lib/labels'
import type { AuditEventRecord, NodeExecution, SessionRecord, WorkflowRunRecord } from '../types'

interface Props {
  refreshToken: number
  onGlobalRefresh: () => void
}

function RunStatusPill({ status }: { status: WorkflowRunRecord['status'] }) {
  switch (status) {
    case 'PASS':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-ok/35 bg-ok-soft text-ok-ink text-[12px] font-semibold min-w-[78px] justify-center">
          <CheckCircle2 className="w-3.5 h-3.5" />
          已通过
        </span>
      )
    case 'AWAITING_APPROVAL':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-amber/45 bg-amber-soft text-amber-ink text-[12px] font-semibold min-w-[78px] justify-center">
          <Pause className="w-3.5 h-3.5" />
          待审批
        </span>
      )
    case 'RUNNING':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-accent/40 bg-accent-soft text-accent-ink text-[12px] font-semibold min-w-[78px] justify-center">
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
          运行中
        </span>
      )
    case 'FAILED':
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-hot/40 bg-hot-soft text-hot text-[12px] font-semibold min-w-[78px] justify-center">
          <XCircle className="w-3.5 h-3.5" />
          已失败
        </span>
      )
    default:
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-[var(--radius-mark)] border border-line-strong bg-bg-sunk text-ink-3 text-[12px] font-semibold min-w-[78px] justify-center">
          <Minus className="w-3.5 h-3.5" />
          已取消
        </span>
      )
  }
}

function StageDot({
  node,
  active,
  onClick,
}: {
  node: NodeExecution
  active: boolean
  onClick: () => void
}) {
  const plainName = STAGE_PLAIN_NAMES[node.node_id] || node.name
  const shortCode = node.node_id.split('_')[0]
  const tooltip = `${shortCode} ${plainName}：${node.summary || '尚未执行'}`

  let ringCls = 'border-line-strong bg-surface text-ink-4'
  let icon: React.ReactNode = <span className="size-1.5 rounded-full bg-line-strong" />

  if (node.status === 'PASS') {
    ringCls = 'border-ok bg-surface text-ok'
    icon = <Check className="w-3 h-3 stroke-[2.6]" />
  } else if (node.status === 'AWAITING_APPROVAL') {
    ringCls = 'border-amber bg-amber-soft text-amber-ink animate-pulse'
    icon = <Pause className="w-3 h-3 stroke-[2.5]" />
  } else if (node.status === 'RUNNING') {
    ringCls = 'border-accent bg-accent-soft text-accent'
    icon = <Loader2 className="w-3 h-3 animate-spin" />
  } else if (node.status === 'FAILED') {
    ringCls = 'border-hot bg-hot-soft text-hot'
    icon = <X className="w-3 h-3 stroke-[2.6]" />
  } else if (node.status === 'SKIPPED') {
    ringCls = 'border-line bg-bg-sunk text-ink-4'
    icon = <Minus className="w-3 h-3" />
  }

  return (
    <button
      type="button"
      title={tooltip}
      aria-label={tooltip}
      onClick={onClick}
      className={`relative flex size-6 shrink-0 items-center justify-center rounded-full border-[1.5px] transition-transform hover:scale-110 ${ringCls} ${
        active ? 'ring-2 ring-accent/45 scale-110' : ''
      }`}
    >
      {icon}
    </button>
  )
}

export const WorkflowsPage: React.FC<Props> = ({ refreshToken, onGlobalRefresh }) => {
  const [runs, setRuns] = useState<WorkflowRunRecord[]>([])
  const [sessions, setSessions] = useState<SessionRecord[]>([])
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string>('P00_PREPARE')
  const [detailTab, setDetailTab] = useState<'stages' | 'digest' | 'sessions' | 'events'>('stages')
  const [runEvents, setRunEvents] = useState<AuditEventRecord[]>([])
  const [approvedMap, setApprovedMap] = useState<Record<string, number[]>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [showNewSession, setShowNewSession] = useState(false)
  const [newClient, setNewClient] = useState('claude-code')
  const [newTitle, setNewTitle] = useState('')
  const [newSummary, setNewSummary] = useState('')

  const loadData = async () => {
    try {
      const [runsRes, sessRes] = await Promise.all([api.listRuns(), api.listSessions()])
      setRuns(runsRes.runs)
      setSessions(sessRes.sessions)

      // Auto-select all planned writes by default for awaiting runs
      setApprovedMap((prev) => {
        const next = { ...prev }
        for (const r of runsRes.runs) {
          if (!(r.run_id in next)) {
            next[r.run_id] = r.planned_writes.map((_, idx) => idx)
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

  const pendingSessionsCount = sessions.filter((s) => s.status === 'pending').length
  const awaitingCount = runs.filter((r) => r.status === 'AWAITING_APPROVAL').length

  const triggerRun = async (autoApprove: boolean | null = null) => {
    setBusy(true)
    setError(null)
    try {
      const res = await api.startRun({ phase: 'auto', auto_approve: autoApprove })
      setExpandedRunId(res.run.run_id)
      setSelectedNodeId(res.run.status === 'AWAITING_APPROVAL' ? 'P04_WRITE_VERIFY' : 'P06_REPORT')
      setDetailTab(res.run.status === 'AWAITING_APPROVAL' ? 'stages' : 'digest')
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const handleApprove = async (run: WorkflowRunRecord) => {
    setBusy(true)
    setError(null)
    try {
      const indices = approvedMap[run.run_id] ?? run.planned_writes.map((_, i) => i)
      await api.approveRun(run.run_id, indices)
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
    setApprovedMap((prev) => {
      const cur = prev[runId] ?? []
      return {
        ...prev,
        [runId]: cur.includes(idx) ? cur.filter((i) => i !== idx) : [...cur, idx],
      }
    })
  }

  const submitNewSession = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newTitle.trim()) return
    setBusy(true)
    setError(null)
    try {
      await api.registerSession({
        client: newClient,
        session_id: `manual-${Date.now().toString(36)}`,
        title: newTitle.trim(),
        project: 'general',
        summary: newSummary.trim() || newTitle.trim(),
        content: `User: ${newTitle.trim()}\nAssistant: ${newSummary.trim() || newTitle.trim()}`,
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

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5">
      {/* Header */}
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">每日沉淀</h1>
          <p className="text-[13.5px] text-ink-3 mt-1">
            每天 20:00 自动整理当天的 AI 对话（周五附带本周复盘）；每行是一个批次，点开即可查看进度与审批写入。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
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

      {/* Quiet status summary line */}
      <div className="flex flex-wrap items-center justify-between gap-2 text-[12.5px] text-ink-3">
        <div className="flex flex-wrap items-center gap-3">
          <span>
            共 <strong className="num font-semibold text-ink">{runs.length}</strong> 个批次
          </span>
          {awaitingCount > 0 && (
            <span className="inline-flex items-center gap-1.5 text-amber-ink font-medium">
              <span className="size-1.5 rounded-full bg-amber" />
              {awaitingCount} 个批次等你审批
            </span>
          )}
          <span>
            当前还有 <strong className="num font-semibold text-ink">{pendingSessionsCount}</strong> 段新对话等待下一轮整理
          </span>
        </div>
        <span className="text-[12px] text-ink-4">
          节点顺序：准备 → 选对话 → 回顾 → 整理建议 → 写入核验 → 更新台账 → 生成日报 → 推送
        </span>
      </div>

      {error && (
        <div className="px-4 py-3 rounded-[var(--radius-card)] bg-hot-soft border border-hot/30 text-[12.5px] text-hot">
          {error}
        </div>
      )}

      {showNewSession && (
        <form
          onSubmit={submitNewSession}
          className="card p-4 grid grid-cols-1 md:grid-cols-12 gap-3"
        >
          <div className="md:col-span-2">
            <label className="block text-[11.5px] text-ink-4 mb-1">来源工具</label>
            <select
              value={newClient}
              onChange={(e) => setNewClient(e.target.value)}
              className="field-input w-full px-2.5 py-1.5 text-[12px]"
            >
              <option value="codex">Codex</option>
              <option value="claude-code">Claude Code</option>
              <option value="claude">Claude Desktop</option>
            </select>
          </div>
          <div className="md:col-span-4">
            <label className="block text-[11.5px] text-ink-4 mb-1">这件事的标题</label>
            <input
              required
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="例如：排查接口超时并加上重试机制"
              className="field-input w-full px-2.5 py-1.5 text-[12px]"
            />
          </div>
          <div className="md:col-span-4">
            <label className="block text-[11.5px] text-ink-4 mb-1">关键结论（选填）</label>
            <input
              value={newSummary}
              onChange={(e) => setNewSummary(e.target.value)}
              placeholder="一句话记下踩坑原因和有效解法"
              className="field-input w-full px-2.5 py-1.5 text-[12px]"
            />
          </div>
          <div className="md:col-span-2 flex items-end">
            <button
              type="submit"
              disabled={busy}
              className="w-full h-8 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12px] font-medium hover:bg-accent-ink"
            >
              加入待整理
            </button>
          </div>
        </form>
      )}

      {/* GitLab-CI-style Daily Batch Pipeline Rows */}
      {runs.length === 0 ? (
        <div className="card p-12 text-center text-[13.5px] text-ink-4">
          还没有沉淀批次，点击右上角「立即跑一轮」开始第一次整理。
        </div>
      ) : (
        <div className="card divide-y divide-line overflow-hidden">
          {runs.map((run, idx) => {
            const isLatest = idx === 0
            const isExpanded = expandedRunId === run.run_id
            const isAwaiting = run.status === 'AWAITING_APPROVAL'
            const approvedIndices = approvedMap[run.run_id] ?? []
            const activeNode =
              run.nodes.find((n) => n.node_id === selectedNodeId) || run.nodes[0] || null
            const batchDate = run.batch_date || formatDateYMD(run.started_at)
            const batchNo = run.batch_seq || runs.length - idx
            const batchSessions = sessions.filter((s) =>
              run.admitted_sessions.includes(s.session_key)
            )

            return (
              <div
                key={run.run_id}
                className={isAwaiting ? 'bg-amber-soft/20' : 'bg-surface'}
              >
                {/* Pipeline Row (GitLab style) */}
                <div className="px-4 py-3.5 flex flex-wrap lg:flex-nowrap items-center gap-4 hover:bg-bg-sunk/40 transition-colors">
                  {/* 1. Status Pill */}
                  <div className="shrink-0">
                    <RunStatusPill status={run.status} />
                  </div>

                  {/* 2. Date & Batch Meta */}
                  <div className="min-w-[190px] lg:w-[250px] shrink-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="num text-[14px] font-semibold text-ink">{batchDate}</span>
                      <span className="mono text-[12px] text-ink-4">
                        #{String(batchNo).padStart(2, '0')}
                      </span>
                      {isLatest && (
                        <span className="px-1.5 py-0.2 rounded-[var(--radius-mark)] bg-ok text-white text-[10.5px] font-semibold">
                          最新
                        </span>
                      )}
                    </div>
                    <div className="mt-1 flex items-center gap-2 text-[12px] text-ink-3 flex-wrap">
                      <span>{labelOf(PHASE_LABELS, run.phase, '每日沉淀')}</span>
                      <span className="text-ink-4">·</span>
                      <span>{labelOf(TRIGGER_LABELS, run.trigger)}</span>
                      <span className="text-ink-4">·</span>
                      <span>{run.admitted_sessions.length} 段对话</span>
                    </div>
                  </div>

                  {/* 3. Horizontal 8-Stage Connected Nodes */}
                  <div className="flex items-center flex-1 min-w-[260px] overflow-x-auto py-1">
                    {run.nodes.map((node, nIdx) => {
                      const prevDone = node.status === 'PASS'
                      return (
                        <React.Fragment key={node.node_id}>
                          <StageDot
                            node={node}
                            active={isExpanded && detailTab === 'stages' && selectedNodeId === node.node_id}
                            onClick={() => toggleRow(run, node.node_id)}
                          />
                          {nIdx < run.nodes.length - 1 && (
                            <span
                              className={`h-[1.5px] w-4 sm:w-5 shrink-0 ${
                                prevDone ? 'bg-ok/60' : 'bg-line-strong'
                              }`}
                            />
                          )}
                        </React.Fragment>
                      )
                    })}
                  </div>

                  {/* 4. Duration & Relative Time */}
                  <div className="hidden sm:flex flex-col items-end text-[11.5px] text-ink-4 shrink-0 min-w-[92px]">
                    <span className="mono inline-flex items-center gap-1 text-ink-3">
                      <Clock className="w-3 h-3" />
                      {formatDuration(run.started_at, run.finished_at, run.nodes)}
                    </span>
                    <span className="inline-flex items-center gap-1 mt-0.5">
                      <Calendar className="w-3 h-3" />
                      {formatRelativeTime(run.started_at)}
                    </span>
                  </div>

                  {/* 5. Action Button (Inline Approval or Expand) */}
                  <div className="shrink-0 ml-auto lg:ml-0">
                    {isAwaiting ? (
                      <button
                        type="button"
                        onClick={() => toggleRow(run)}
                        className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] bg-amber text-white text-[12px] font-semibold hover:opacity-90"
                      >
                        审批本批 ({run.planned_writes.length})
                        {isExpanded ? (
                          <ChevronUp className="w-3.5 h-3.5" />
                        ) : (
                          <ChevronDown className="w-3.5 h-3.5" />
                        )}
                      </button>
                    ) : (
                      <button
                        type="button"
                        onClick={() => toggleRow(run)}
                        className="inline-flex items-center gap-1 h-8 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-[12px] text-ink-3 hover:border-line-strong hover:text-ink"
                      >
                        {isExpanded ? '收起' : '查看'}
                        {isExpanded ? (
                          <ChevronUp className="w-3.5 h-3.5" />
                        ) : (
                          <ChevronDown className="w-3.5 h-3.5" />
                        )}
                      </button>
                    )}
                  </div>
                </div>

                {/* Expanded Inline Batch Detail & Approval Drawer */}
                {isExpanded && (
                  <div className="border-t border-line-soft bg-bg-sunk/45 px-5 py-4 space-y-4">
                    {/* Inline Approval Section (attached directly to this date's batch!) */}
                    {isAwaiting && (
                      <div className="rounded-[var(--radius-card)] border border-amber/40 bg-amber-soft/55 p-4 space-y-3.5">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <div className="text-[14px] font-semibold text-ink">
                              本批整理出 {run.planned_writes.length} 项建议，确认后才会写入
                            </div>
                            <p className="text-[12px] text-ink-3 mt-0.5">
                              勾选同意的条目将写入知识库或生效为避坑规则；不想保存可以取消。
                            </p>
                          </div>
                          <div className="flex items-center gap-2">
                            <button
                              type="button"
                              onClick={() => handleReject(run)}
                              disabled={busy}
                              className="h-8 px-3 rounded-[var(--radius-control)] border border-line-strong bg-surface text-[12px] text-ink-2 hover:text-ink"
                            >
                              取消本次写入
                            </button>
                            <button
                              type="button"
                              onClick={() => handleApprove(run)}
                              disabled={busy}
                              className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-ok text-white text-[12px] font-semibold hover:opacity-90"
                            >
                              <Check className="w-3.5 h-3.5" />
                              同意并写入 ({approvedIndices.length}/{run.planned_writes.length})
                            </button>
                          </div>
                        </div>

                        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                          {run.planned_writes.map((pw, pIdx) => {
                            const checked = approvedIndices.includes(pIdx)
                            return (
                              <label
                                key={pIdx}
                                className={`card p-3.5 flex items-start gap-2.5 cursor-pointer ${
                                  checked ? 'border-ok/45' : 'opacity-60'
                                }`}
                              >
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  onChange={() => togglePlannedIdx(run.run_id, pIdx)}
                                  className="mt-1 accent-accent"
                                />
                                <div className="min-w-0 flex-1">
                                  <div className="flex items-center justify-between gap-2">
                                    <span className="text-[13px] font-semibold text-ink">
                                      {pw.title}
                                    </span>
                                    <span
                                      className={`chip ${
                                        pw.target_kind === 'skill_improvement'
                                          ? 'bg-accent-soft text-accent-ink border-accent/30 font-semibold'
                                          : ''
                                      }`}
                                    >
                                      {pw.target_kind === 'skill_improvement'
                                        ? pw.action === 'update'
                                          ? '优化 Skill'
                                          : '新建 Skill'
                                        : pw.target_kind === 'flywheel_rule'
                                          ? '避坑规则'
                                          : '知识笔记'}
                                    </span>
                                  </div>
                                  {pw.approval_reason && (
                                    <div className="text-[11.5px] text-amber-ink mt-1">
                                      需确认原因：{pw.approval_reason}
                                    </div>
                                  )}
                                  {pw.relative_path && (
                                    <div className="mono text-[11px] text-accent-ink mt-0.5 truncate">
                                      {pw.relative_path}
                                    </div>
                                  )}
                                  <p className="text-[12px] text-ink-3 mt-1">{pw.summary}</p>
                                  {pw.content_preview && (
                                    <div className="mt-2 rounded-[var(--radius-control)] bg-bg-sunk border border-line-soft p-2.5 max-h-32 overflow-y-auto">
                                      <MarkdownView content={pw.content_preview} />
                                    </div>
                                  )}
                                </div>
                              </label>
                            )
                          })}
                        </div>
                      </div>
                    )}

                    {/* Detail sub-tabs */}
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <PillTabs
                        items={[
                          { key: 'stages', label: '各环节进度' },
                          { key: 'digest', label: '本批日报' },
                          {
                            key: 'sessions',
                            label: '涉及对话',
                            count: run.admitted_sessions.length,
                          },
                          {
                            key: 'events',
                            label: '操作记录',
                            count: runEvents.length || undefined,
                          },
                        ]}
                        active={detailTab}
                        onChange={(k) =>
                          setDetailTab(k as 'stages' | 'digest' | 'sessions' | 'events')
                        }
                      />
                      <span className="text-[12px] text-ink-4">
                        {labelOf(APPROVAL_MODE_LABELS, run.approval_mode, '智能审批')} ·{' '}
                        {labelOf(RUNNER_MODE_LABELS, run.runner_mode)} ·{' '}
                        <span className="mono">{run.run_id}</span>
                      </span>
                    </div>

                    {detailTab === 'stages' && (
                      <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
                        {/* Compact stage list */}
                        <div className="lg:col-span-5 grid grid-cols-2 gap-2">
                          {run.nodes.map((node) => {
                            const isSel = activeNode?.node_id === node.node_id
                            const code = node.node_id.split('_')[0]
                            return (
                              <button
                                key={node.node_id}
                                type="button"
                                onClick={() => setSelectedNodeId(node.node_id)}
                                className={`text-left px-3 py-2 rounded-[var(--radius-control)] border transition-colors ${
                                  isSel
                                    ? 'bg-surface border-accent ring-1 ring-accent/35'
                                    : 'bg-surface/70 border-line-soft hover:border-line-strong'
                                }`}
                              >
                                <div className="flex items-center justify-between gap-1">
                                  <span className="mono text-[11px] text-ink-4">{code}</span>
                                  <StatusBadge status={node.status} />
                                </div>
                                <div className="text-[12.5px] font-medium text-ink mt-1 truncate">
                                  {STAGE_PLAIN_NAMES[node.node_id] || node.name}
                                </div>
                              </button>
                            )
                          })}
                        </div>

                        {/* Selected stage detail */}
                        <div className="lg:col-span-7 card p-4 space-y-2.5 text-[12.5px]">
                          {activeNode ? (
                            <>
                              <div className="flex items-center justify-between gap-2 pb-2 border-b border-line-soft">
                                <span className="font-semibold text-ink">
                                  {activeNode.node_id.split('_')[0]} ·{' '}
                                  {STAGE_PLAIN_NAMES[activeNode.node_id] || activeNode.name}
                                </span>
                                <StatusBadge status={activeNode.status} />
                              </div>
                              <p className="text-ink-2 leading-relaxed">
                                {activeNode.summary || '这一步尚未执行。'}
                              </p>
                              {activeNode.error && (
                                <div className="p-2.5 rounded-[var(--radius-control)] bg-hot-soft text-hot border border-hot/25">
                                  出错原因：{activeNode.error}
                                </div>
                              )}
                              {activeNode.artifacts.length > 0 && (
                                <div>
                                  <div className="text-[11.5px] text-ink-4 mb-1">产出文件</div>
                                  <div className="flex flex-wrap gap-1.5">
                                    {activeNode.artifacts.map((art) => (
                                      <span
                                        key={art}
                                        className="mono text-[11px] px-2 py-0.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink border border-ok/25"
                                      >
                                        {art}
                                      </span>
                                    ))}
                                  </div>
                                </div>
                              )}
                              {activeNode.internal_checks.length > 0 && (
                                <div className="pt-1">
                                  <div className="text-[11.5px] text-ink-4 mb-1">自动检查项</div>
                                  <div className="flex flex-wrap gap-1.5">
                                    {activeNode.internal_checks.map((chk, cIdx) => (
                                      <span key={cIdx} className="chip">
                                        {chk.name} · {chk.status === 'PASS' ? '通过' : chk.status}
                                      </span>
                                    ))}
                                  </div>
                                </div>
                              )}
                              {activeNode.node_id === 'P05_SETTLE' &&
                                activeNode.payload &&
                                typeof activeNode.payload === 'object' &&
                                (activeNode.payload as any).dream && (
                                  <div className="mt-2 p-3 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft space-y-1.5">
                                    <div className="text-[12px] font-semibold text-ink">
                                      周五蒸馏与减法摘要（{(activeNode.payload as any).dream.period_key}）
                                    </div>
                                    <div className="flex flex-wrap gap-2 text-[11.5px] text-ink-3 num">
                                      <span>
                                        自动休眠规则：{((activeNode.payload as any).dream.auto_dormant_rules || []).length} 条
                                      </span>
                                      <span>·</span>
                                      <span>
                                        过期需复核经验卡：{((activeNode.payload as any).dream.expired_cards || []).length} 篇
                                      </span>
                                      <span>·</span>
                                      <span>
                                        待收敛分歧：{(activeNode.payload as any).dream.open_dissents_count ?? 0} 条
                                      </span>
                                    </div>
                                    {((activeNode.payload as any).dream.proposals || []).length > 0 && (
                                      <ul className="list-disc pl-4 space-y-0.5 text-[12px] text-ink-2">
                                        {((activeNode.payload as any).dream.proposals as string[]).map(
                                          (p, pIdx) => (
                                            <li key={pIdx}>{p}</li>
                                          )
                                        )}
                                      </ul>
                                    )}
                                  </div>
                                )}
                            </>
                          ) : null}
                        </div>
                      </div>
                    )}

                    {detailTab === 'digest' && (
                      <div className="card p-4">
                        {run.digest_markdown ? (
                          <MarkdownView content={run.digest_markdown} />
                        ) : (
                          <p className="text-[13px] text-ink-4">
                            本批尚未完成全部步骤，审批通过后会自动生成总结日报。
                          </p>
                        )}
                      </div>
                    )}

                    {detailTab === 'sessions' && (
                      <div className="card p-4">
                        {batchSessions.length === 0 ? (
                          <p className="text-[13px] text-ink-4">
                            本批共纳入 {run.admitted_sessions.length} 段对话记录。
                          </p>
                        ) : (
                          <div className="divide-y divide-line-soft">
                            {batchSessions.map((s) => (
                              <div
                                key={s.session_key}
                                className="py-2.5 flex items-center justify-between gap-3"
                              >
                                <div className="min-w-0">
                                  <div className="text-[13px] font-medium text-ink truncate">
                                    {s.title || s.session_key}
                                  </div>
                                  <div className="text-[11.5px] text-ink-4">
                                    {labelOf(CLIENT_LABELS, s.client)}
                                    {s.summary ? ` · ${s.summary}` : ''}
                                  </div>
                                </div>
                                <StatusBadge status={s.status} />
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                    {detailTab === 'events' && (
                      <div className="card p-4">
                        {runEvents.length === 0 ? (
                          <p className="text-[13px] text-ink-4">暂无审计记录。</p>
                        ) : (
                          <div className="divide-y divide-line-soft">
                            {runEvents.map((ev) => (
                              <div
                                key={ev.seq}
                                className="py-2 flex flex-wrap items-center justify-between gap-2 text-[12.5px]"
                              >
                                <div className="flex items-center gap-2 min-w-0">
                                  <span className="chip mono">{ev.event_type}</span>
                                  <span className="text-ink-2">{ev.summary}</span>
                                </div>
                                <span className="num text-[11.5px] text-ink-4">
                                  {formatRelativeTime(ev.created_at)}
                                </span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
