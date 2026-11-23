import React, { useEffect, useState } from 'react'
import { ArrowUpRight, BookOpen, ChevronRight, FileText, Play, X, Zap } from 'lucide-react'
import type { PageKey } from '../App'
import { api } from '../api'
import { FlywheelLoopDiagram } from '../components/FlywheelLoopDiagram'
import { AsideCard, ReadingLayout } from '../components/PageLayout'
import { StatusBadge } from '../components/StatusBadge'
import type { KnowledgeDocSummary, OverviewResponse, PreflightResponse } from '../types'

interface Props {
  overview: OverviewResponse | null
  onNavigate: (page: PageKey) => void
  onRefresh: () => void
}

function todayFormatted() {
  const now = new Date()
  const week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][now.getDay()]
  return `${now.getMonth() + 1}月${now.getDate()}日 · ${week}`
}

export const HomePage: React.FC<Props> = ({ overview, onNavigate, onRefresh }) => {
  const [docs, setDocs] = useState<KnowledgeDocSummary[]>([])
  const [sinkInfo, setSinkInfo] = useState<{ sink_type: string; root: string } | null>(null)
  const [readingDoc, setReadingDoc] = useState<{
    doc_path: string
    content: string
    sha256: string
  } | null>(null)

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
      const res = await api.preflight({
        task: prompt.trim(),
        project: project.trim() || 'generic',
        client: 'portal-sandbox',
      })
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
    return <div className="py-20 text-center text-[13px] text-ink-4">正在加载飞轮个人站数据...</div>
  }

  const { site, metrics, latest_run, recent_rules } = overview
  const brand = site.brand
  const activeRulesCount =
    (metrics.rules_by_status.verified_active || 0) +
    (metrics.rules_by_status.unverified_active || 0)

  const aside = (
    <>
      {/* Live Preflight Sandbox in the AIHOT right rail */}
      <AsideCard
        title={
          <span className="inline-flex items-center gap-1.5">
            <Zap className="w-3.5 h-3.5 text-accent" />
            在线激活沙箱
          </span>
        }
      >
        <p className="text-[12px] leading-relaxed text-ink-3 mb-3">
          模拟 AI 工具开局调用 <code className="mono rounded-[var(--radius-mark)] bg-bg-sunk px-1 py-0.5 text-[11px]">preflight_activate</code>，验证策略与规则命中。
        </p>
        <form onSubmit={runPreflightTest} className="space-y-2">
          <input
            value={project}
            onChange={(e) => setProject(e.target.value)}
            placeholder="项目域 (generic)"
            className="field-input w-full px-2.5 py-1.5 text-[12px]"
          />
          <textarea
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="输入任务描述..."
            className="field-input w-full px-2.5 py-1.5 text-[12px]"
          />
          <button
            type="submit"
            disabled={pfLoading}
            className="w-full py-2 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink disabled:opacity-50 transition-colors"
          >
            {pfLoading ? '正在激活预检...' : '执行一次真实 Preflight 激活'}
          </button>
        </form>

        {pfResult && (
          <div className="mt-3.5 pt-3 border-t border-line-soft space-y-2">
            <div className="flex items-center justify-between text-[11px] mono text-ink-4">
              <span className="truncate">Receipt: {pfResult.receipt_id}</span>
              <span className="px-1.5 py-0.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink font-semibold">
                {pfResult.anti_laziness_mode}
              </span>
            </div>
            <pre className="mono text-[11.5px] leading-[1.65] bg-bg-sunk border border-line-soft rounded-[var(--radius-control)] p-3 whitespace-pre-wrap max-h-64 overflow-y-auto text-ink-2">
              {pfResult.compact_context}
            </pre>
          </div>
        )}
      </AsideCard>

      {/* Quick status & active rules summary */}
      <AsideCard title="活跃规则摘要">
        <div className="space-y-2.5">
          {recent_rules.slice(0, 4).map((r) => (
            <div key={r.rule_id} className="pb-2.5 border-b border-line-soft last:border-b-0 last:pb-0">
              <div className="flex items-start justify-between gap-2">
                <span className="text-[12.5px] font-medium text-ink leading-snug">{r.title}</span>
              </div>
              <div className="mt-1 flex items-center justify-between text-[11px] text-ink-4">
                <StatusBadge status={r.status} />
                <span className="num">命中 {r.hit_count} · 通过 {r.natural_pass_count}</span>
              </div>
            </div>
          ))}
        </div>
        <button
          type="button"
          onClick={() => onNavigate('rules')}
          className="mt-3 inline-flex items-center gap-0.5 text-[12px] font-semibold text-accent hover:text-accent-ink"
        >
          查看全部规则与复盘台账
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </AsideCard>

      <AsideCard title="机器可读入口" className="!bg-panel-quiet">
        <ul className="space-y-2 text-[12.5px]">
          {[
            ['MCP Streamable HTTP', '/mcp'],
            ['站点说明 llms.txt', '/llms.txt'],
            ['公开统一接口 API v1', '/api/v1/overview'],
          ].map(([label, href]) => (
            <li key={href} className="flex items-center justify-between">
              <span className="text-ink-3">{label}</span>
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                className="mono inline-flex items-center gap-0.5 text-[11.5px] text-accent hover:text-accent-ink"
              >
                {href}
                <ArrowUpRight className="w-3 h-3" />
              </a>
            </li>
          ))}
        </ul>
      </AsideCard>
    </>
  )

  return (
    <ReadingLayout aside={aside}>
      <div className="space-y-6">
        {/* Editorial Page Header (AIHOT style: clean title + date, no heavy banner box) */}
        <header className="pb-4 border-b border-line">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">
              {brand.name}
            </h1>
            <span className="num text-[12.5px] text-ink-4">{todayFormatted()}</span>
          </div>
          <p className="mt-2 text-[14.5px] text-ink-2">{brand.motto}</p>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <span className="text-[12.5px] text-ink-4">{brand.subtitle}</span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => onNavigate('workflows')}
                className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink transition-colors"
              >
                <Play className="w-3.5 h-3.5" />
                工作流画布
              </button>
              <button
                type="button"
                onClick={() => onNavigate('connect')}
                className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-ink-2 text-[12.5px] font-medium hover:border-line-strong hover:text-ink transition-colors"
              >
                Agent 接入
              </button>
            </div>
          </div>
        </header>

        {/* Quiet Editorial KPI Strip (hairline divided, tabular numbers) */}
        <section className="card grid grid-cols-2 sm:grid-cols-4 divide-y sm:divide-y-0 sm:divide-x divide-line-soft">
          <div className="p-4">
            <div className="text-[11.5px] text-ink-4">上下文激活</div>
            <div className="num text-[22px] font-semibold text-ink mt-0.5">
              {metrics.preflight_activations}
            </div>
            <div className="text-[11px] text-ink-4 mt-0.5">
              已探测 Skill {metrics.probed_skills_count}
            </div>
          </div>
          <div className="p-4">
            <div className="text-[11.5px] text-ink-4">活跃规则（已验/总）</div>
            <div className="num text-[22px] font-semibold text-accent-ink mt-0.5">
              {metrics.rules_by_status.verified_active || 0} / {activeRulesCount}
            </div>
            <div className="text-[11px] text-ink-4 mt-0.5">
              待修订 {metrics.rules_by_status.needs_revision || 0}
            </div>
          </div>
          <div className="p-4">
            <div className="text-[11.5px] text-ink-4">真实验证率</div>
            <div className="num text-[22px] font-semibold text-ok-ink mt-0.5">
              {metrics.real_verification_rate !== null ? `${metrics.real_verification_rate}%` : '—'}
            </div>
            <div className="text-[11px] text-ink-4 mt-0.5">
              自然回执 {metrics.natural_outcomes_total} 条
            </div>
          </div>
          <div className="p-4">
            <div className="text-[11.5px] text-ink-4">待复盘异常</div>
            <div className="num text-[22px] font-semibold text-ink mt-0.5">
              {metrics.open_retro_count}
            </div>
            <div className="text-[11px] text-ink-4 mt-0.5">
              待审会话 {metrics.sessions_pending}/{metrics.sessions_total}
            </div>
          </div>
        </section>

        {/* 8-Step Interactive Flywheel */}
        <FlywheelLoopDiagram steps={site.loop_steps || []} />

        {/* Latest Digest Paper Card */}
        <section className="card p-5">
          <div className="flex items-center justify-between pb-3 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <FileText className="w-4 h-4 text-accent" />
              <h2 className="text-[15px] font-bold text-ink">最新飞轮运行日报</h2>
            </div>
            {latest_run && <StatusBadge status={latest_run.status} />}
          </div>
          {latest_run ? (
            <div className="mt-3 space-y-2.5">
              <div className="mono text-[11.5px] text-ink-4 flex flex-wrap gap-3">
                <span>Run: {latest_run.run_id}</span>
                <span>Phase: {latest_run.phase}</span>
                <span>Runner: {latest_run.runner_mode}</span>
                <span>Sessions: {latest_run.admitted_sessions.length}</span>
              </div>
              <pre className="text-[13px] leading-[1.75] text-ink-2 bg-bg-sunk/60 border border-line-soft rounded-[var(--radius-control)] p-4 whitespace-pre-wrap overflow-x-auto">
                {latest_run.digest_markdown || '运行进行中，尚未生成终态日报。'}
              </pre>
            </div>
          ) : (
            <p className="text-[13px] text-ink-4 py-4">暂无运行记录。</p>
          )}
        </section>

        {/* Knowledge Sink Feed (AIHOT clean hairline list) */}
        <section className="card p-5">
          <div className="flex items-center justify-between pb-3 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <BookOpen className="w-4 h-4 text-ok" />
              <h2 className="text-[15px] font-bold text-ink">知识库沉淀文档</h2>
            </div>
            {sinkInfo && <span className="chip mono">{sinkInfo.sink_type}</span>}
          </div>
          {docs.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-4">当前知识库暂无文档。</p>
          ) : (
            <div className="divide-y divide-line-soft mt-1">
              {docs.slice(0, 8).map((d) => (
                <button
                  key={d.doc_path}
                  type="button"
                  onClick={() => openDoc(d.doc_path)}
                  className="w-full text-left py-3 px-2 -mx-2 rounded-[var(--radius-tile)] hover:bg-bg-sunk/70 transition-colors flex items-center justify-between gap-3"
                >
                  <div className="min-w-0">
                    <div className="text-[14px] font-medium text-ink truncate">{d.title}</div>
                    <div className="mono text-[11.5px] text-ink-4 truncate mt-0.5">{d.doc_path}</div>
                  </div>
                  <span className="num text-[11.5px] text-ink-4 shrink-0">
                    {(d.size_bytes / 1024).toFixed(1)} KB
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>
      </div>

      {/* Document Reader Sheet */}
      {readingDoc && (
        <div className="fixed inset-0 z-50 bg-ink/35 backdrop-blur-[1px] flex items-center justify-center p-4">
          <div className="card max-w-2xl w-full max-h-[82vh] flex flex-col overflow-hidden">
            <div className="px-5 py-3.5 border-b border-line flex items-center justify-between bg-surface-2">
              <div className="min-w-0">
                <div className="mono text-[13px] font-semibold text-ink truncate">
                  {readingDoc.doc_path}
                </div>
                <div className="mono text-[11px] text-ink-4">
                  SHA-256: {readingDoc.sha256.slice(0, 24)}...
                </div>
              </div>
              <button
                type="button"
                onClick={() => setReadingDoc(null)}
                className="p-1.5 rounded-[var(--radius-control)] hover:bg-bg-sunk text-ink-3"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <pre className="p-5 text-[13px] leading-[1.8] text-ink-2 whitespace-pre-wrap overflow-y-auto">
              {readingDoc.content}
            </pre>
          </div>
        </div>
      )}
    </ReadingLayout>
  )
}
