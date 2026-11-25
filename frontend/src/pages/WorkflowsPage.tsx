import React, { useEffect, useState } from 'react'
import { Plus, Play } from 'lucide-react'
import { api } from '../api'
import { ManualSessionModal, type ManualSessionInput } from '../components/workflows/ManualSessionModal'
import { RunPipelineRow } from '../components/workflows/RunPipelineRow'
import type { WorkflowDetailTab } from '../components/workflows/RunDetailPanel'
import { FEISHU_LISTENER_STATE_LABELS, labelOf } from '../lib/labels'
import type { AuditEventRecord, SessionRecord, WorkflowRunRecord } from '../types'

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

export const WorkflowsPage: React.FC<Props> = ({ refreshToken, onGlobalRefresh }) => {
  const [runs, setRuns] = useState<WorkflowRunRecord[]>([])
  const [sessions, setSessions] = useState<SessionRecord[]>([])
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string>('P00_PREPARE')
  const [detailTab, setDetailTab] = useState<WorkflowDetailTab>('stages')
  const [runEvents, setRunEvents] = useState<AuditEventRecord[]>([])
  const [approvalSel, setApprovalSel] = useState<Record<string, ApprovalSelection>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [showNewSession, setShowNewSession] = useState(false)
  const [newClient, setNewClient] = useState('claude-code')
  const [newTitle, setNewTitle] = useState('')
  const [newSummary, setNewSummary] = useState('')
  const [feishuLabel, setFeishuLabel] = useState('未配置')

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
      const [runsRes, sessRes] = await Promise.all([api.listRuns(), api.listSessions()])
      setRuns(runsRes.runs)
      setSessions(sessRes.sessions)

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

  const pendingSessionsCount = sessions.filter((s) => s.status === 'pending').length
  const awaitingCount = runs.filter((r) => r.status === 'AWAITING_APPROVAL').length

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
      if (res.run) {
        setExpandedRunId(res.run.run_id)
        setSelectedNodeId(res.run.status === 'AWAITING_APPROVAL' ? 'P04_WRITE_VERIFY' : 'P06_REPORT')
        setDetailTab(res.run.status === 'AWAITING_APPROVAL' ? 'stages' : 'digest')
      } else if (res.run_id) {
        setExpandedRunId(res.run_id)
        setSelectedNodeId('P00_PREPARE')
        setDetailTab('stages')
      }
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

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5">
      {/* Header */}
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">沉淀与审批</h1>
          <p className="text-[13.5px] text-ink-3 mt-1">
            P00–P07 受控沉淀流水线与人机审批台；每行是一个批次，点开即可核验噪音漏斗、模型遥测与写入计划。
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

      {/* GitLab-CI-style Daily Batch Pipeline Rows */}
      {runs.length === 0 ? (
        <div className="card p-12 text-center text-[13.5px] text-ink-4">
          还没有沉淀批次，点击右上角「立即跑一轮」开始第一次整理。
        </div>
      ) : (
        <div className="card divide-y divide-line overflow-hidden">
          {runs.map((run, idx) => (
            <RunPipelineRow
              key={run.run_id}
              run={run}
              isLatest={idx === 0}
              isExpanded={expandedRunId === run.run_id}
              batchNo={run.batch_seq || runs.length - idx}
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
          ))}
        </div>
      )}
    </div>
  )
}
