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

  // Quick register test session form
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
        setSelectedRunId((prev) => (prev && runsRes.runs.some((r) => r.run_id === prev) ? prev : runsRes.runs[0].run_id))
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

  const selectedNode = activeRun?.nodes.find((n) => n.node_id === selectedNodeId) || activeRun?.nodes[0] || null

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
    setApprovedIndices((prev) => (prev.includes(idx) ? prev.filter((i) => i !== idx) : [...prev, idx]))
  }

  return (
    <div className="space-y-5">
      {/* Toolbar */}
      <div className="bg-white border border-slate-200/90 rounded-2xl p-4 shadow-xs flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2.5">
          <span className="text-xs font-semibold text-slate-600">运行记录：</span>
          <select
            value={activeRun?.run_id || ''}
            onChange={(e) => setSelectedRunId(e.target.value)}
            className="text-xs font-mono px-3 py-1.5 rounded-lg border border-slate-200 bg-slate-50 text-slate-800"
          >
            {runs.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id} ({r.status}) · {r.runner_mode}
              </option>
            ))}
          </select>
          {activeRun && <StatusBadge status={activeRun.status} />}
          {activeRun && (
            <span className="text-[11px] font-mono text-slate-500">
              锁状态: {activeRun.lock_released ? '已安全释放' : '独占持有中'}
            </span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => setShowNewSession((v) => !v)}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl border border-slate-200 bg-white text-xs font-medium text-slate-700 hover:bg-slate-50 cursor-pointer"
          >
            <Plus className="w-3.5 h-3.5" />
            模拟登记新会话
          </button>
          <button
            onClick={() => triggerRun(false)}
            disabled={busy}
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-xl bg-slate-900 text-white text-xs font-medium hover:bg-slate-800 disabled:opacity-50 cursor-pointer"
          >
            <Play className="w-3.5 h-3.5" />
            运行一轮（审核放行模式）
          </button>
          <button
            onClick={() => triggerRun(true)}
            disabled={busy}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50 cursor-pointer"
          >
            <FastForward className="w-3.5 h-3.5" />
            全自动运行
          </button>
        </div>
      </div>

      {error && (
        <div className="px-4 py-3 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700">
          {error}
        </div>
      )}

      {showNewSession && (
        <form
          onSubmit={submitNewSession}
          className="bg-white border border-blue-200 rounded-2xl p-4 shadow-xs grid grid-cols-1 md:grid-cols-12 gap-3"
        >
          <div className="md:col-span-2">
            <label className="block text-[11px] text-slate-500 mb-1">来源客户端</label>
            <select
              value={newClient}
              onChange={(e) => setNewClient(e.target.value)}
              className="w-full px-2.5 py-1.5 text-xs rounded-lg border border-slate-200 bg-slate-50"
            >
              <option value="codex">codex</option>
              <option value="claude-code">claude-code</option>
              <option value="claude">claude</option>
            </select>
          </div>
          <div className="md:col-span-4">
            <label className="block text-[11px] text-slate-500 mb-1">会话标题</label>
            <input
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              className="w-full px-2.5 py-1.5 text-xs rounded-lg border border-slate-200"
            />
          </div>
          <div className="md:col-span-4">
            <label className="block text-[11px] text-slate-500 mb-1">事实摘要</label>
            <input
              value={newSummary}
              onChange={(e) => setNewSummary(e.target.value)}
              className="w-full px-2.5 py-1.5 text-xs rounded-lg border border-slate-200"
            />
          </div>
          <div className="md:col-span-2 flex items-end">
            <button
              type="submit"
              disabled={busy}
              className="w-full py-1.5 rounded-lg bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 cursor-pointer"
            >
              入队待审
            </button>
          </div>
        </form>
      )}

      {/* Interactive Approval Gate Panel when paused */}
      {activeRun && activeRun.status === 'AWAITING_APPROVAL' && (
        <div className="bg-amber-50/80 border border-amber-300 rounded-2xl p-5 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2.5">
              <ShieldCheck className="w-5 h-5 text-amber-700" />
              <div>
                <h3 className="text-sm font-semibold text-amber-950">
                  P03 写入计划已生成 — 等待你审核放行进入 P04–P07
                </h3>
                <p className="text-xs text-amber-800">
                  勾选批准的项目将按 Write-Set 受控写入知识库并完成 SHA-256 回读校验；新规则以 `unverified_active` 上线。
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={handleReject}
                disabled={busy}
                className="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-white border border-amber-300 text-amber-900 text-xs font-medium hover:bg-amber-100 cursor-pointer"
              >
                <XCircle className="w-3.5 h-3.5" />
                拒绝并释锁
              </button>
              <button
                onClick={handleApprove}
                disabled={busy}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl bg-emerald-600 text-white text-xs font-semibold hover:bg-emerald-700 cursor-pointer"
              >
                <Check className="w-4 h-4" />
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
                  className={`rounded-xl border p-3.5 bg-white ${
                    checked ? 'border-emerald-300' : 'border-slate-200 opacity-60'
                  }`}
                >
                  <label className="flex items-start gap-2.5 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => togglePlannedIdx(idx)}
                      className="mt-1 rounded"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs font-semibold text-slate-900">{pw.title}</span>
                        <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">
                          {pw.target_kind} · {pw.action}
                        </span>
                      </div>
                      {pw.relative_path && (
                        <div className="text-[11px] font-mono text-blue-700 mt-0.5">{pw.relative_path}</div>
                      )}
                      <div className="text-xs text-slate-500 mt-1">{pw.summary}</div>
                      <pre className="mt-2 text-[11px] bg-slate-50 border border-slate-200/80 rounded-lg p-2.5 whitespace-pre-wrap max-h-36 overflow-y-auto text-slate-700">
                        {pw.content_preview}
                      </pre>
                    </div>
                  </label>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* P00-P07 DAG Canvas */}
      {activeRun ? (
        <PipelineDagCanvas
          nodes={activeRun.nodes}
          selectedNodeId={selectedNodeId}
          onSelectNode={setSelectedNodeId}
        />
      ) : (
        <div className="bg-white border border-slate-200 rounded-2xl p-10 text-center text-sm text-slate-500">
          暂无工作流运行记录，点击右上角“运行一轮”启动 P00–P07。
        </div>
      )}

      {/* Node Inspector + Session Queue */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
        {/* Node Evidence Inspector */}
        <div className="lg:col-span-6 bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <FileCode2 className="w-4 h-4 text-blue-600" />
              <h3 className="text-sm font-semibold text-slate-900">
                节点回执与证据下钻：{selectedNode ? `${selectedNode.node_id} (${selectedNode.name})` : '--'}
              </h3>
            </div>
            {selectedNode && <StatusBadge status={selectedNode.status} />}
          </div>
          {selectedNode ? (
            <div className="space-y-3 text-xs">
              <div className="grid grid-cols-2 gap-2 font-mono text-[11px] bg-slate-50 p-3 rounded-xl border border-slate-200/70">
                <div>Actor: {selectedNode.actor}</div>
                <div>Duration: {selectedNode.duration_ms ? `${selectedNode.duration_ms}ms` : '--'}</div>
                <div className="col-span-2 truncate">
                  Receipt Hash: {selectedNode.receipt_hash || '未生成'}
                </div>
              </div>
              <div>
                <div className="font-semibold text-slate-700 mb-1">节点摘要</div>
                <p className="text-slate-600">{selectedNode.summary || '暂无摘要'}</p>
              </div>
              {selectedNode.error && (
                <div className="p-2.5 rounded-lg bg-rose-50 text-rose-700 border border-rose-200">
                  错误：{selectedNode.error}
                </div>
              )}
              {selectedNode.internal_checks.length > 0 && (
                <div>
                  <div className="font-semibold text-slate-700 mb-1.5">内部检查项（internal_checks）</div>
                  <div className="space-y-1.5">
                    {selectedNode.internal_checks.map((chk, i) => (
                      <div
                        key={i}
                        className="flex items-center justify-between px-3 py-1.5 rounded-lg bg-slate-50 border border-slate-200/60"
                      >
                        <span className="text-slate-700 flex items-center gap-1.5">
                          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-600" />
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
                  <div className="font-semibold text-slate-700 mb-1">产出物 / Write-Set</div>
                  <div className="flex flex-wrap gap-1.5">
                    {selectedNode.artifacts.map((art) => (
                      <span
                        key={art}
                        className="font-mono text-[11px] px-2 py-0.5 rounded bg-emerald-50 text-emerald-800 border border-emerald-200"
                      >
                        {art}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : null}
        </div>

        {/* Registered Sessions Queue */}
        <div className="lg:col-span-6 bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-semibold text-slate-900">已登记多端会话队列（{sessions.length}）</h3>
            <span className="text-[11px] text-slate-400">来源：MCP 登记 / 薄 Hook / P00 扫盘</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-slate-200 text-slate-500">
                  <th className="pb-2 font-medium">客户端 / 会话</th>
                  <th className="pb-2 font-medium">优先级</th>
                  <th className="pb-2 font-medium">状态</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {sessions.slice(0, 8).map((s) => (
                  <tr key={s.session_key}>
                    <td className="py-2.5 pr-3">
                      <div className="font-medium text-slate-800 line-clamp-1">{s.title || s.session_key}</div>
                      <div className="text-[11px] font-mono text-slate-400">
                        {s.client} · {s.project}
                      </div>
                    </td>
                    <td className="py-2.5 pr-2 font-mono text-[11px] text-slate-600">{s.priority}</td>
                    <td className="py-2.5">
                      <StatusBadge status={s.status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  )
}
