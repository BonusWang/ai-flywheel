import React, { useEffect, useState } from 'react'
import {
  BookOpen,
  CalendarClock,
  ChevronRight,
  FileText,
  GitBranch,
  PauseCircle,
  Shield,
  X,
} from 'lucide-react'
import type { PageKey } from '../App'
import { api } from '../api'
import { MarkdownView } from '../components/MarkdownView'
import { AsideCard, ReadingLayout } from '../components/PageLayout'
import { StatusBadge } from '../components/StatusBadge'
import { formatDateYMD, labelOf, RULE_STATUS_LABELS, SINK_TYPE_LABELS } from '../lib/labels'
import type { KnowledgeDocSummary, OverviewResponse } from '../types'

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

export const HomePage: React.FC<Props> = ({ overview, onNavigate }) => {
  const [docs, setDocs] = useState<KnowledgeDocSummary[]>([])
  const [docsLoaded, setDocsLoaded] = useState(false)
  const [sinkInfo, setSinkInfo] = useState<{ sink_type: string; root: string } | null>(null)
  const [readingDoc, setReadingDoc] = useState<{
    doc_path: string
    content: string
    sha256: string
  } | null>(null)

  useEffect(() => {
    api
      .listKnowledge()
      .then((res) => {
        setDocs(res.documents)
        setSinkInfo({ sink_type: res.sink_type, root: res.root })
      })
      .catch(() => {})
      .finally(() => setDocsLoaded(true))
  }, [overview?.metrics.workflow_runs_total])

  const openDoc = async (path: string) => {
    try {
      const data = await api.readKnowledgeDoc(path)
      setReadingDoc(data)
    } catch {
      // ignore
    }
  }

  if (!overview) {
    return <div className="py-20 text-center text-[13px] text-ink-4">正在加载每日成果...</div>
  }

  const { site, metrics, latest_run, recent_rules } = overview
  const brand = site.brand
  const verifiedRules = metrics.rules_by_status.verified_active || 0
  const unverifiedRules = metrics.rules_by_status.unverified_active || 0
  const activeRulesCount = verifiedRules + unverifiedRules
  const awaitingApproval = latest_run?.status === 'AWAITING_APPROVAL'

  const aside = (
    <>
      {/* Card 1: Active rules (plain & scannable) */}
      <AsideCard
        title={
          <span className="inline-flex items-center gap-1.5">
            <Shield className="w-3.5 h-3.5 text-accent" />
            生效中的避坑规则
          </span>
        }
      >
        {recent_rules.length === 0 ? (
          <p className="text-[12.5px] text-ink-4">还没有沉淀出避坑规则。</p>
        ) : (
          <div className="space-y-3">
            {recent_rules.slice(0, 4).map((r) => (
              <div
                key={r.rule_id}
                className="pb-2.5 border-b border-line-soft last:border-b-0 last:pb-0"
              >
                <div className="text-[13px] font-medium text-ink leading-snug">{r.title}</div>
                <div className="mt-1.5 flex items-center justify-between gap-2 text-[11.5px] text-ink-4">
                  <span className="text-ink-3">{labelOf(RULE_STATUS_LABELS, r.status)}</span>
                  <span className="num">已提醒 {r.hit_count} 次</span>
                </div>
              </div>
            ))}
          </div>
        )}
        <button
          type="button"
          onClick={() => onNavigate('rules')}
          className="mt-3.5 inline-flex items-center gap-0.5 text-[12px] font-semibold text-accent hover:text-accent-ink"
        >
          查看全部规则与规矩
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </AsideCard>

      {/* Card 2: Daily sedimentation rhythm */}
      <AsideCard
        title={
          <span className="inline-flex items-center gap-1.5">
            <CalendarClock className="w-3.5 h-3.5 text-accent" />
            每日沉淀节奏
          </span>
        }
        className="!bg-panel-quiet"
      >
        <div className="space-y-2.5 text-[12.5px]">
          <div className="flex items-center justify-between gap-2">
            <span className="text-ink-3">自动整理</span>
            <span className="text-ink font-medium">每天 20:00（周五加周复盘）</span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-ink-3">最近批次</span>
            {latest_run ? (
              <span className="inline-flex items-center gap-1.5">
                <span className="num text-ink-2">{formatDateYMD(latest_run.started_at)}</span>
                <StatusBadge status={latest_run.status} />
              </span>
            ) : (
              <span className="text-ink-4">暂无</span>
            )}
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-ink-3">已沉淀规则</span>
            <span className="num text-ink font-medium">
              {activeRulesCount} 条（{verifiedRules} 条已生效）
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={() => onNavigate('runs')}
          className="mt-3.5 inline-flex items-center gap-0.5 text-[12px] font-semibold text-accent hover:text-accent-ink"
        >
          打开每日沉淀流水线
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </AsideCard>
    </>
  )

  return (
    <ReadingLayout aside={aside}>
      <div className="space-y-6">
        {/* 1. Calm AIHOT-style Header */}
        <header className="pb-4 border-b border-line">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">每日成果</h1>
            <span className="num text-[12.5px] text-ink-4">{todayFormatted()}</span>
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
            <p className="text-[13.5px] text-ink-3">
              {brand.name} · 每天把 AI 协作里的有效结论整理成笔记和避坑规则。
            </p>
            <button
              type="button"
              onClick={() => onNavigate('runs')}
              className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-ink-2 text-[12.5px] font-medium hover:border-line-strong hover:text-ink transition-colors"
            >
              <GitBranch className="w-3.5 h-3.5 text-accent" />
              每日沉淀流水线
            </button>
          </div>
        </header>

        {/* 2. Only surface a calm prompt when a batch actually needs the maintainer's approval */}
        {awaitingApproval && latest_run && (
          <section className="rounded-[var(--radius-card)] border border-amber/40 bg-amber-soft/50 px-4 py-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-[13px] text-ink">
              <PauseCircle className="w-4 h-4 text-amber-ink shrink-0" />
              <span>
                <strong className="num font-semibold">{formatDateYMD(latest_run.started_at)}</strong>{' '}
                批次整理出了{' '}
                <strong className="num font-semibold">{latest_run.planned_writes.length}</strong>{' '}
                项写入建议，等你确认
              </span>
            </div>
            <button
              type="button"
              onClick={() => onNavigate('runs')}
              className="inline-flex items-center gap-1 h-7 px-3 rounded-[var(--radius-control)] bg-amber text-white text-[12px] font-semibold hover:opacity-90"
            >
              去审批
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </section>
        )}

        {/* 3. Latest Digest (Clean Readable Report) */}
        <section className="card p-5">
          <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <FileText className="w-4 h-4 text-accent" />
              <h2 className="text-[15.5px] font-bold text-ink">最近一次沉淀总结</h2>
            </div>
            {latest_run && (
              <div className="flex items-center gap-2">
                <span className="num text-[12px] text-ink-4">
                  {formatDateYMD(latest_run.started_at)}
                </span>
                <StatusBadge status={latest_run.status} />
              </div>
            )}
          </div>
          {latest_run ? (
            <div className="mt-3.5">
              {latest_run.digest_markdown ? (
                <MarkdownView content={latest_run.digest_markdown} />
              ) : (
                <p className="text-[13px] text-ink-4 py-2">
                  这一批还在等待确认，审批完成后会在这里展示总结内容。
                </p>
              )}
              <div className="mt-4 pt-2.5 border-t border-line-soft flex justify-end">
                <button
                  type="button"
                  onClick={() => onNavigate('runs')}
                  className="inline-flex items-center gap-0.5 text-[12px] font-medium text-accent hover:text-accent-ink"
                >
                  查看这一批的完整进度
                  <ChevronRight className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          ) : (
            <p className="text-[13px] text-ink-4 py-6 text-center">
              还没有沉淀记录，去「每日沉淀」启动第一轮吧。
            </p>
          )}
        </section>

        {/* 4. Recently Saved Notes */}
        <section className="card p-5">
          <div className="flex items-center justify-between pb-3 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <BookOpen className="w-4 h-4 text-ok" />
              <h2 className="text-[15.5px] font-bold text-ink">近期沉淀的笔记</h2>
            </div>
            {sinkInfo && (
              <span className="chip">{labelOf(SINK_TYPE_LABELS, sinkInfo.sink_type)}</span>
            )}
          </div>
          {!docsLoaded ? (
            <p className="text-[13px] text-ink-4 py-6 text-center">正在加载近期笔记...</p>
          ) : docs.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-6 text-center">知识库里还没有笔记文档。</p>
          ) : (
            <div className="divide-y divide-line-soft mt-1">
              {docs.slice(0, 10).map((d) => (
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

      {/* Note Reader Modal */}
      {readingDoc && (
        <div className="fixed inset-0 z-50 bg-ink/35 backdrop-blur-[1px] flex items-center justify-center p-4">
          <div className="card max-w-2xl w-full max-h-[82vh] flex flex-col overflow-hidden">
            <div className="px-5 py-3.5 border-b border-line flex items-center justify-between bg-surface-2">
              <div className="mono text-[13px] font-semibold text-ink truncate">
                {readingDoc.doc_path}
              </div>
              <button
                type="button"
                onClick={() => setReadingDoc(null)}
                className="p-1.5 rounded-[var(--radius-control)] hover:bg-bg-sunk text-ink-3"
                aria-label="关闭"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="p-5 overflow-y-auto">
              <MarkdownView content={readingDoc.content} />
            </div>
          </div>
        </div>
      )}
    </ReadingLayout>
  )
}
