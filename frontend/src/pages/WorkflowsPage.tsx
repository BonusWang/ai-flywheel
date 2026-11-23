import React, { useEffect, useState } from 'react'
import {
  Check,
  CheckCircle2,
  FastForward,
  FileCode2,
  Play,
  Plus,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import { api } from '../api'
import { PipelineDagCanvas } from '../components/PipelineDagCanvas'
import { StatusBadge } from '../components/StatusBadge'
import type { SessionRecord, WorkflowRunRecord } from '../types'

interface Props {
  refreshToken: number
  onGlobalRefresh: () => void
}

export const WorkflowsPage: React.FC<Props> = ({ refreshToken, onGlobalRefresh }) => {
  const [runs, setRuns] = useState<WorkflowRunRecord[]>([])
  const [sessions, setSessions] = useState<SessionRecord[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string>('P00_PREPARE')
  const [approvedIndices, setApprovedIndices] = useState<number[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [showNewSession, setShowNewSession] = useState(false)
  const [newClient, setNewClient] = useState('claude-code')
  const [newTitle, setNewTitle] = useState('排查数据库迁移脚本重复执行导致锁表')
  const [newSummary, setNewSummary] = useState('定位脚本缺少幂等校验导致报错，增加版本表校验并在本地真实复测通过。')

  const loadData = async () => {
    try {
      const [runsRes, sessRes] = await Promise.all([api.listRuns(), api.listSessions()])
      setRuns(runsRes.runs)
      setSessions(sessRes.sessions)
      if (runsRes.runs.length > 0) {
        setSelectedRunId((prev) =>
          prev && runsRes.runs.some((r) => r.run_id === prev) ? prev : runsRes.runs[0].run_id
        )
      }
    } catch (e: any) {
      setError(e.message)
    }
  }

  useEffect(() => {
    loadData()
  }, [refreshToken])

  const activeRun = runs.find((r) => r.run_id === selectedRunId) || runs[0] || null

  useEffect(() => {
    if (activeRun) {
      setApprovedIndices(activeRun.planned_writes.map((_, idx) => idx))
    }
  }, [activeRun?.run_id, activeRun?.planned_writes.length])

  const selectedNode =
    activeRun?.nodes.find((n) => n.node_id === selectedNodeId) || activeRun?.nodes[0] || null

  const triggerRun = async (autoApprove: boolean) => {
    setBusy(true)
    setError(null)
    try {
      const res = await api.startRun({ phase: 'daily', auto_approve: autoApprove })
      setSelectedRunId(res.run.run_id)
      setSelectedNodeId(res.run.status === 'AWAITING_APPROVAL' ? 'P04_WRITE_VERIFY' : 'P07_DELIVER')
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const handleApprove = async () => {
    if (!activeRun) return
    setBusy(true)
    setError(null)
    try {
      await api.approveRun(activeRun.run_id, approvedIndices)
      setSelectedNodeId('P07_DELIVER')
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const handleReject = async () => {
    if (!activeRun) return
    setBusy(true)
    setError(null)
    try {
      await api.rejectRun(activeRun.run_id)
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const submitNewSession = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    try {
      await api.registerSession({
        client: newClient,
        session_id: `manual-${Date.now().toString(36)}`,
        title: newTitle,
        project: 'ai-flywheel',
        summary: newSummary,
        content: `User: ${newTitle}\nAssistant: ${newSummary}`,
        tags: ['fix', 'verification'],
      })
      setShowNewSession(false)
      await loadData()
      onGlobalRefresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const togglePlannedIdx = (idx: number) => {
    setApprovedIndices((prev) =>
      prev.includes(idx) ? prev.filter((i) => i !== idx) : [...prev, idx]
    )
  }

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">工作流画布</h1>
          <p className="text-[13px] text-ink-3 mt-1">
            P00–P07 自主编排状态机、审核放行门禁与多端会话队列实时观测
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setShowNewSession((v) => !v)}
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] font-medium text-ink-2 hover:border-line-strong hover:text-ink"
          >
            <Plus className="w-3.5 h-3.5" />
            模拟登记新会话
          </button>
          <button
            type="button"
            onClick={() => triggerRun(false)}
            disabled={busy}
            className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink disabled:opacity-50"
          >
            <Play className="w-3.5 h-3.5" />
            运行一轮（审核放行模式）
          </button>
          <button
            type="button"
            onClick={() => triggerRun(true)}
            disabled={busy}
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-accent/40 bg-accent-soft text-accent-ink text-[12.5px] font-medium hover:bg-accent/15 disabled:opacity-50"
          >
            <FastForward className="w-3.5 h-3.5" />
            全自动运行
          </button>
        </div>
      </header>

      {/* Run selector strip */}
      <div className="card px-4 py-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-[12.5px] text-ink-4">运行记录</span>
          <select
            value={activeRun?.run_id || ''}
            onChange={(e) => setSelectedRunId(e.target.value)}
            className="field-input mono text-[12px] px-2.5 py-1.5"
          >
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id} ({r.status}) · {r.runner_mode}
              </option>
            ))}
          </select>
          {activeRun && <StatusBadge status={activeRun.status} />}
          {activeRun && (
            <span className="mono text-[11.5px] text-ink-4">
              锁状态: {activeRun.lock_released ? '已安全释放' : '独占持有中'}
            </span>
          )}
        </div>
        {activeRun && (
          <span className="mono text-[11.5px] text-ink-4">
            准入会话 {activeRun.admitted_sessions.length} 条 · 触发源 {activeRun.trigger}
          </span>
        )}
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
            <label className="block text-[11.5px] text-ink-4 mb-1">来源客户端</label>
            <select
              value={newClient}
              onChange={(e) => setNewClient(e.target.value)}
              className="field-input w-full px-2.5 py-1.5 text-[12px]"
            >
              <option value="codex">codex</option>
              <option value="claude-code">claude-code</option>
              <option value="claude">claude</option>
            </select>
          </div>
          <div className="md:col-span-4">
            <label className="block text-[11.5px] text-ink-4 mb-1">会话标题</label>
            <input
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              className="field-input w-full px-2.5 py-1.5 text-[12px]"
            />
          </div>
          <div className="md:col-span-4">
            <label className="block text-[11.5px] text-ink-4 mb-1">事实摘要</label>
            <input
              value={newSummary}
              onChange={(e) => setNewSummary(e.target.value)}
              className="field-input w-full px-2.5 py-1.5 text-[12px]"
            />
          </div>
          <div className="md:col-span-2 flex items-end">
            <button
              type="submit"
              disabled={busy}
              className="w-full h-8 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12px] font-medium hover:bg-accent-ink"
            >
              入队待审
            </button>
          </div>
        </form>
      )}

      {/* Approval Gate Panel */}
      {activeRun && activeRun.status === 'AWAITING_APPROVAL' && (
        <section className="rounded-[var(--radius-card)] border border-amber/45 bg-amber-soft/60 p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              <ShieldCheck className="w-5 h-5 text-amber-ink" />
              <div>
                <h2 className="text-[14.5px] font-semibold text-ink">
                  P03 写入计划已生成 — 等待审核放行进入 P04–P07
                </h2>
                <p className="text-[12px] text-ink-3">
                  勾选批准的项目将按 Write-Set 受控写入知识库并完成 SHA-256 回读校验；新规则以 `unverified_active` 上线。
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={handleReject}
                disabled={busy}
                className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line-strong bg-surface text-ink-2 text-[12px] font-medium hover:text-ink"
              >
                <XCircle className="w-3.5 h-3.5" />
                拒绝并释锁
              </button>
              <button
                type="button"
                onClick={handleApprove}
                disabled={busy}
                className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-ok text-white text-[12px] font-semibold hover:opacity-90"
              >
                <Check className="w-3.5 h-3.5" />
                批准放行（{approvedIndices.length}/{activeRun.planned_writes.length}）
              </button>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {activeRun.planned_writes.map((pw, idx) => {
              const checked = approvedIndices.includes(idx)
              return (
                <div
                  key={idx}
                  className={`card p-3.5 ${checked ? 'border-ok/45' : 'opacity-60'}`}
                >
                  <label className="flex items-start gap-2.5 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => togglePlannedIdx(idx)}
                      className="mt-1"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[13px] font-semibold text-ink">{pw.title}</span>
                        <span className="chip mono">
                          {pw.target_kind} · {pw.action}
                        </span>
                      </div>
                      {pw.relative_path && (
                        <div className="mono text-[11.5px] text-accent-ink mt-0.5">
                          {pw.relative_path}
                        </div>
                      )}
                      <div className="text-[12px] text-ink-3 mt-1">{pw.summary}</div>
                      <pre className="mono mt-2 text-[11.5px] bg-bg-sunk border border-line-soft rounded-[var(--radius-control)] p-2.5 whitespace-pre-wrap max-h-36 overflow-y-auto text-ink-2">
                        {pw.content_preview}
                      </pre>
                    </div>
                  </label>
                </div>
              )
            })}
          </div>
        </section>
      )}

      {/* P00-P07 DAG */}
      {activeRun ? (
        <PipelineDagCanvas
          nodes={activeRun.nodes}
          selectedNodeId={selectedNodeId}
          onSelectNode={setSelectedNodeId}
        />
      ) : (
        <div className="card p-10 text-center text-[13px] text-ink-4">
          暂无工作流运行记录，点击右上角“运行一轮”启动 P00–P07。
        </div>
      )}

      {/* Evidence Inspector + Session Queue */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
        <section className="lg:col-span-6 card p-5">
          <div className="flex items-center justify-between pb-3 mb-3 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <FileCode2 className="w-4 h-4 text-accent" />
              <h3 className="text-[14px] font-bold text-ink">
                节点回执下钻：{selectedNode ? `${selectedNode.node_id} (${selectedNode.name})` : '--'}
              </h3>
            </div>
            {selectedNode && <StatusBadge status={selectedNode.status} />}
          </div>
          {selectedNode && (
            <div className="space-y-3 text-[12.5px]">
              <div className="grid grid-cols-2 gap-2 mono text-[11.5px] bg-bg-sunk p-3 rounded-[var(--radius-control)] border border-line-soft text-ink-3">
                <div>Actor: {selectedNode.actor}</div>
                <div>Duration: {selectedNode.duration_ms ? `${selectedNode.duration_ms}ms` : '--'}</div>
                <div className="col-span-2 truncate">
                  Receipt Hash: {selectedNode.receipt_hash || '未生成'}
                </div>
              </div>
              <div>
                <div className="text-[12px] font-semibold text-ink-3 mb-1">节点摘要</div>
                <p className="text-ink-2">{selectedNode.summary || '暂无摘要'}</p>
              </div>
              {selectedNode.error && (
                <div className="p-2.5 rounded-[var(--radius-control)] bg-hot-soft text-hot border border-hot/25">
                  错误：{selectedNode.error}
                </div>
              )}
              {selectedNode.internal_checks.length > 0 && (
                <div>
                  <div className="text-[12px] font-semibold text-ink-3 mb-1.5">
                    内部检查项（internal_checks）
                  </div>
                  <div className="space-y-1.5">
                    {selectedNode.internal_checks.map((chk, i) => (
                      <div
                        key={i}
                        className="flex items-center justify-between px-3 py-1.5 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft"
                      >
                        <span className="text-ink-2 flex items-center gap-1.5">
                          <CheckCircle2 className="w-3.5 h-3.5 text-ok" />
                          {chk.name}
                        </span>
                        <StatusBadge status={chk.status} />
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {selectedNode.artifacts.length > 0 && (
                <div>
                  <div className="text-[12px] font-semibold text-ink-3 mb-1.5">产出物 / Write-Set</div>
                  <div className="flex flex-wrap gap-1.5">
                    {selectedNode.artifacts.map((art) => (
                      <span
                        key={art}
                        className="mono text-[11.5px] px-2 py-0.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink border border-ok/25"
                      >
                        {art}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </section>

        <section className="lg:col-span-6 card p-5">
          <div className="flex items-center justify-between pb-3 mb-3 border-b border-line-soft">
            <h3 className="text-[14px] font-bold text-ink">
              已登记多端会话队列（<span className="num">{sessions.length}</span>）
            </h3>
            <span className="text-[11.5px] text-ink-4">MCP 登记 · 薄 Hook · P00 扫盘</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-[12.5px]">
              <thead>
                <tr className="border-b border-line-soft text-ink-4 text-[11.5px]">
                  <th className="pb-2 font-medium">客户端 / 会话</th>
                  <th className="pb-2 font-medium">优先级</th>
                  <th className="pb-2 font-medium">状态</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-soft">
                {sessions.slice(0, 8).map((s) => (
                  <tr key={s.session_key}>
                    <td className="py-2.5 pr-3">
                      <div className="font-medium text-ink line-clamp-1">
                        {s.title || s.session_key}
                      </div>
                      <div className="mono text-[11px] text-ink-4">
                        {s.client} · {s.project}
                      </div>
                    </td>
                    <td className="py-2.5 pr-2 mono text-[11.5px] text-ink-3">{s.priority}</td>
                    <td className="py-2.5">
                      <StatusBadge status={s.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  )
}
