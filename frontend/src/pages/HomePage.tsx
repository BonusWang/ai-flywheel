import React, { useEffect, useState } from 'react'
import {
  Activity,
  BookOpen,
  CheckCircle2,
  FileText,
  Play,
  ShieldAlert,
  Sparkles,
  Zap,
  X,
} from 'lucide-react'
import { api } from '../api'
import { FlywheelLoopDiagram } from '../components/FlywheelLoopDiagram'
import { StatusBadge } from '../components/StatusBadge'
import type { KnowledgeDocSummary, OverviewResponse, PreflightResponse } from '../types'

interface Props {
  overview: OverviewResponse | null
  onNavigate: (tab: 'home' | 'workflows' | 'rules' | 'connect') => void
  onRefresh: () => void
}

export const HomePage: React.FC<Props> = ({ overview, onNavigate, onRefresh }) => {
  const [docs, setDocs] = useState<KnowledgeDocSummary[]>([])
  const [sinkInfo, setSinkInfo] = useState<{ sink_type: string; root: string } | null>(null)
  const [readingDoc, setReadingDoc] = useState<{ doc_path: string; content: string; sha256: string } | null>(null)

  const [prompt, setPrompt] = useState('修复 MCP 接口并发锁并做真实验证')
  const [project, setProject] = useState('ai-flywheel')
  const [pfResult, setPfResult] = useState<PreflightResponse | null>(null)
  const [pfLoading, setPfLoading] = useState(false)

  useEffect(() => {
    api
      .listKnowledge()
      .then((res) => {
        setDocs(res.documents)
        setSinkInfo({ sink_type: res.sink_type, root: res.root })
      })
      .catch(() => {})
  }, [overview?.metrics.workflow_runs_total])

  const runPreflightTest = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!prompt.trim()) return
    setPfLoading(true)
    try {
      const res = await api.preflight({ task: prompt.trim(), project: project.trim() || 'generic', client: 'portal-sandbox' })
      setPfResult(res)
      onRefresh()
    } finally {
      setPfLoading(false)
    }
  }

  const openDoc = async (path: string) => {
    try {
      const data = await api.readKnowledgeDoc(path)
      setReadingDoc(data)
    } catch {
      // ignore
    }
  }

  if (!overview) {
    return <div className="py-20 text-center text-sm text-slate-500">正在加载飞轮个人站数据...</div>
  }

  const { site, metrics, latest_run } = overview
  const brand = site.brand

  const statCards = [
    {
      label: '上下文激活次数',
      value: metrics.preflight_activations,
      sub: `探测本地 Skill ${metrics.probed_skills_count} 个`,
      icon: Zap,
      tone: 'text-blue-600 bg-blue-50',
    },
    {
      label: '活跃规则（两级）',
      value: `${metrics.rules_by_status.verified_active || 0} / ${(metrics.rules_by_status.verified_active || 0) + (metrics.rules_by_status.unverified_active || 0)}`,
      sub: `已验证 / 活跃总计（复发待修 ${metrics.rules_by_status.needs_revision || 0}）`,
      icon: CheckCircle2,
      tone: 'text-emerald-600 bg-emerald-50',
    },
    {
      label: '真实验证率',
      value: metrics.real_verification_rate !== null ? `${metrics.real_verification_rate}%` : '—',
      sub: `自然结果回执 ${metrics.natural_outcomes_total} 条`,
      icon: Activity,
      tone: 'text-indigo-600 bg-indigo-50',
    },
    {
      label: '待复盘异常线索',
      value: metrics.open_retro_count,
      sub: `待审查会话 ${metrics.sessions_pending} / ${metrics.sessions_total}`,
      icon: ShieldAlert,
      tone: metrics.open_retro_count > 0 ? 'text-amber-600 bg-amber-50' : 'text-slate-600 bg-slate-100',
    },
  ]

  return (
    <div className="space-y-6">
      {/* Editorial Hero */}
      <section className="bg-white border border-slate-200/90 rounded-2xl p-6 shadow-xs">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-5">
          <div className="space-y-2 max-w-2xl">
            <div className="inline-flex items-center gap-2 text-xs font-medium px-2.5 py-1 rounded-full bg-slate-100 text-slate-700 border border-slate-200">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
              {brand.owner_display} · v{brand.version} · Local-First MCP Runtime
            </div>
            <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-slate-900">
              {brand.name}
              <span className="text-slate-400 font-normal ml-2 text-lg sm:text-xl">AI 协作飞轮</span>
            </h1>
            <p className="text-sm sm:text-base font-medium text-slate-800">{brand.motto}</p>
            <p className="text-xs sm:text-sm text-slate-500">{brand.subtitle}</p>
          </div>
          <div className="flex flex-wrap gap-2.5">
            <button
              onClick={() => onNavigate('workflows')}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-slate-900 text-white text-sm font-medium hover:bg-slate-800 transition-colors cursor-pointer"
            >
              <Play className="w-4 h-4" />
              工作流可视化画布
            </button>
            <button
              onClick={() => onNavigate('connect')}
              className="inline-flex items-center gap-2 px-4 py-2.5 rounded-xl bg-white border border-slate-300 text-slate-800 text-sm font-medium hover:bg-slate-50 transition-colors cursor-pointer"
            >
              <Sparkles className="w-4 h-4 text-blue-600" />
              MCP 接入与策略中心
            </button>
          </div>
        </div>
      </section>

      {/* KPI Strip */}
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
        {statCards.map((c) => (
          <div
            key={c.label}
            className="bg-white border border-slate-200/90 rounded-2xl p-4 shadow-xs flex items-start justify-between"
          >
            <div>
              <div className="text-xs font-medium text-slate-500">{c.label}</div>
              <div className="text-2xl font-bold text-slate-900 mt-1 font-mono">{c.value}</div>
              <div className="text-[11px] text-slate-500 mt-1">{c.sub}</div>
            </div>
            <div className={`w-9 h-9 rounded-xl flex items-center justify-center ${c.tone}`}>
              <c.icon className="w-4 h-4" />
            </div>
          </div>
        ))}
      </section>

      {/* 8-Step Interactive Flywheel */}
      <FlywheelLoopDiagram steps={site.loop_steps || []} />

      {/* Bottom Two-Column Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-5">
        {/* Left: Latest Digest + Knowledge Sink Docs */}
        <div className="lg:col-span-7 space-y-5">
          <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <FileText className="w-4 h-4 text-slate-600" />
                <h2 className="text-sm font-semibold text-slate-900">最新飞轮运行日报</h2>
              </div>
              {latest_run && <StatusBadge status={latest_run.status} />}
            </div>
            {latest_run ? (
              <div className="space-y-3">
                <div className="text-xs text-slate-500 font-mono flex flex-wrap gap-3">
                  <span>Run: {latest_run.run_id}</span>
                  <span>Phase: {latest_run.phase}</span>
                  <span>Runner: {latest_run.runner_mode}</span>
                  <span>Sessions: {latest_run.admitted_sessions.length}</span>
                </div>
                <pre className="text-xs leading-relaxed text-slate-700 bg-slate-50 border border-slate-200/80 rounded-xl p-3.5 whitespace-pre-wrap overflow-x-auto">
                  {latest_run.digest_markdown || '运行进行中，尚未生成终态日报。'}
                </pre>
              </div>
            ) : (
              <div className="text-xs text-slate-500 py-4">
                暂无运行记录。可前往工作流画布触发第一轮 P00–P07 流水线。
              </div>
            )}
          </div>

          <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <BookOpen className="w-4 h-4 text-emerald-600" />
                <h2 className="text-sm font-semibold text-slate-900">知识库沉淀产物（与策略解耦）</h2>
              </div>
              {sinkInfo && (
                <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-100 text-slate-600">
                  {sinkInfo.sink_type}
                </span>
              )}
            </div>
            {docs.length === 0 ? (
              <p className="text-xs text-slate-500 py-3">当前知识库暂无文档。</p>
            ) : (
              <div className="divide-y divide-slate-100">
                {docs.slice(0, 6).map((d) => (
                  <button
                    key={d.doc_path}
                    onClick={() => openDoc(d.doc_path)}
                    className="w-full text-left py-2.5 px-2 -mx-2 rounded-lg hover:bg-slate-50 transition-colors flex items-center justify-between gap-3 cursor-pointer"
                  >
                    <div className="min-w-0">
                      <div className="text-sm font-medium text-slate-800 truncate">{d.title}</div>
                      <div className="text-[11px] font-mono text-slate-400 truncate">{d.doc_path}</div>
                    </div>
                    <span className="text-[11px] text-slate-400 shrink-0">
                      {(d.size_bytes / 1024).toFixed(1)} KB
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Right: Live Preflight Sandbox */}
        <div className="lg:col-span-5">
          <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs h-full flex flex-col">
            <div className="flex items-center gap-2 mb-1">
              <Zap className="w-4 h-4 text-blue-600" />
              <h2 className="text-sm font-semibold text-slate-900">在线激活沙箱（Preflight Tester）</h2>
            </div>
            <p className="text-xs text-slate-500 mb-3.5">
              模拟 AI 工具开局调用 <code className="text-slate-700">flywheel_preflight_activate</code>，实时验证飞轮内置反偷懒策略、个人偏好与活跃先例命中。
            </p>
            <form onSubmit={runPreflightTest} className="space-y-2.5">
              <input
                value={project}
                onChange={(e) => setProject(e.target.value)}
                placeholder="项目域 (如 generic / ai-flywheel)"
                className="w-full px-3 py-2 text-xs rounded-xl border border-slate-200 bg-slate-50 focus:bg-white focus:outline-none focus:border-blue-400"
              />
              <textarea
                rows={3}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="输入任意任务描述，测试策略与规则激活..."
                className="w-full px-3 py-2 text-xs rounded-xl border border-slate-200 bg-slate-50 focus:bg-white focus:outline-none focus:border-blue-400"
              />
              <button
                type="submit"
                disabled={pfLoading}
                className="w-full py-2 rounded-xl bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors cursor-pointer"
              >
                {pfLoading ? '正在激活预检...' : '执行一次真实 Preflight 激活'}
              </button>
            </form>

            {pfResult && (
              <div className="mt-4 space-y-2.5 flex-1">
                <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] font-mono text-slate-500">
                  <span>Receipt: {pfResult.receipt_id}</span>
                  <span className="px-2 py-0.5 rounded bg-blue-50 text-blue-700 font-semibold">
                    mode={pfResult.anti_laziness_mode}
                  </span>
                </div>
                <pre className="text-[11px] leading-relaxed bg-slate-950 text-slate-100 rounded-xl p-3.5 whitespace-pre-wrap max-h-72 overflow-y-auto">
                  {pfResult.compact_context}
                </pre>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Knowledge Document Preview Modal */}
      {readingDoc && (
        <div className="fixed inset-0 z-50 bg-slate-900/40 backdrop-blur-[1px] flex items-center justify-center p-4">
          <div className="bg-white border border-slate-200 rounded-2xl max-w-2xl w-full max-h-[82vh] flex flex-col shadow-xl">
            <div className="px-5 py-3.5 border-b border-slate-200 flex items-center justify-between">
              <div>
                <div className="text-sm font-semibold text-slate-900 font-mono">{readingDoc.doc_path}</div>
                <div className="text-[11px] text-slate-400 font-mono">SHA-256: {readingDoc.sha256.slice(0, 20)}...</div>
              </div>
              <button
                onClick={() => setReadingDoc(null)}
                className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-500 cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <pre className="p-5 text-xs leading-relaxed text-slate-800 whitespace-pre-wrap overflow-y-auto">
              {readingDoc.content}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}
