import React, { useEffect, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  BookOpen,
  CalendarClock,
  CheckCircle2,
  ChevronRight,
  FileText,
  GitBranch,
  Layers,
  PauseCircle,
  Shield,
  ShieldAlert,
} from 'lucide-react'
import type { NavigateFn } from '../App'
import { api } from '../api'
import { MarkdownView } from '../components/MarkdownView'
import { AsideCard, ReadingLayout } from '../components/PageLayout'
import { CoreCardModal } from '../components/rules/CoreCardModal'
import { StatusBadge } from '../components/StatusBadge'
import { AnimatedNumber } from '../components/ui/AnimatedNumber'
import { DocumentReaderModal } from '../components/ui/DocumentReaderModal'
import {
  APPROVAL_MODE_LABELS,
  FRESHNESS_TIER_SHORT,
  formatDateYMD,
  formatShanghaiToday,
  labelOf,
  RULE_STATUS_LABELS,
  SINK_TYPE_LABELS,
} from '../lib/labels'
import type {
  AutomationsSnapshot,
  CoreCardPayload,
  GrowthMetricsLive,
  KnowledgeDocSummary,
  OverviewResponse,
} from '../types'

interface Props {
  overview: OverviewResponse | null
  onNavigate: NavigateFn
  onRefresh: () => void
}

function HomeSkeleton() {
  const aside = (
    <>
      <div className="card p-5 space-y-3">
        <div className="skeleton-block h-4 w-28" />
        <div className="skeleton-block h-10 w-full" />
        <div className="skeleton-block h-10 w-full" />
      </div>
      <div className="card p-5 space-y-2.5">
        <div className="skeleton-block h-4 w-24" />
        <div className="skeleton-block h-12 w-full" />
      </div>
    </>
  )
  return (
    <ReadingLayout aside={aside}>
      <div className="space-y-5" aria-busy="true">
        <div className="pb-4 border-b border-line space-y-2.5">
          <div className="skeleton-block h-7 w-40" />
          <div className="skeleton-block h-4 w-80 max-w-full" />
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="card p-4 space-y-2.5">
              <div className="skeleton-block h-3.5 w-20" />
              <div className="skeleton-block h-7 w-24" />
              <div className="skeleton-block h-3 w-32" />
            </div>
          ))}
        </div>
        <div className="card p-5 space-y-3">
          <div className="skeleton-block h-5 w-44" />
          <div className="skeleton-block h-4 w-full" />
          <div className="skeleton-block h-4 w-5/6" />
          <div className="skeleton-block h-4 w-3/4" />
        </div>
      </div>
    </ReadingLayout>
  )
}

export const HomePage: React.FC<Props> = ({ overview, onNavigate }) => {
  const [docs, setDocs] = useState<KnowledgeDocSummary[]>([])
  const [docsLoaded, setDocsLoaded] = useState(false)
  const [sinkInfo, setSinkInfo] = useState<{ sink_type: string; root: string } | null>(null)
  const [automations, setAutomations] = useState<AutomationsSnapshot | null>(null)
  const [growth, setGrowth] = useState<GrowthMetricsLive | null>(null)
  const [coreCard, setCoreCard] = useState<CoreCardPayload | null>(null)
  const [showCoreCardModal, setShowCoreCardModal] = useState(false)
  const [readingDoc, setReadingDoc] = useState<{ path: string; title: string } | null>(null)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      api.listKnowledge().catch(() => null),
      api.automations().catch(() => null),
      api.growthMetrics().catch(() => null),
      api.coreCard().catch(() => null),
    ]).then(([kRes, aRes, gRes, cRes]) => {
      if (cancelled) return
      if (kRes) {
        setDocs(kRes.documents)
        setSinkInfo({ sink_type: kRes.sink_type, root: kRes.root })
      }
      if (aRes) setAutomations(aRes)
      if (gRes) setGrowth(gRes.live)
      if (cRes) setCoreCard(cRes)
      setDocsLoaded(true)
    })
    return () => {
      cancelled = true
    }
  }, [overview?.metrics.workflow_runs_total, overview?.metrics.preflight_activations])

  if (!overview) {
    return <HomeSkeleton />
  }

  const { site, metrics, latest_run, recent_rules, safety_valves } = overview
  const brand = site.brand
  const verifiedRules = metrics.rules_by_status.verified_active || 0
  const unverifiedRules = metrics.rules_by_status.unverified_active || 0
  const dormantRules = metrics.rules_by_status.dormant || 0
  const activeRulesCount = verifiedRules + unverifiedRules
  const awaitingApproval = latest_run?.status === 'AWAITING_APPROVAL'

  const openDissents = growth?.open_dissent_count ?? 0
  const expiredCards = growth?.expired_card_count ?? 0
  const staleSkills = growth?.stale_skill_count ?? 0
  const governanceAlertsCount =
    (awaitingApproval ? 1 : 0) +
    (openDissents > 0 ? 1 : 0) +
    (expiredCards > 0 ? 1 : 0) +
    (staleSkills > 0 ? 1 : 0)

  const sched = automations?.scheduler
  const policy = automations?.policy
  const scheduleSummaryText = sched
    ? sched.enabled
      ? `${sched.human_schedule.replace('（上海时间）', '')}${sched.friday_weekly_enabled ? ' · 含周五精简' : ''}`
      : '自动整理已暂停'
    : '读取中…'

  const effectiveApprovalLabel = policy
    ? labelOf(APPROVAL_MODE_LABELS, policy.effective_approval_mode, policy.effective_approval_mode)
    : safety_valves.auto_approve_writes
      ? '全部自动'
      : '智能审批'

  const p00Node = latest_run?.nodes.find((n) => n.node_id === 'P00_PREPARE')
  const triageReport = (p00Node?.payload as any)?.triage_report as
    | { checked?: number; skipped?: number; admitted?: number }
    | undefined

  const aside = (
    <>
      {/* Card 1: Active rules */}
      <AsideCard
        title={
          <span className="inline-flex items-center gap-1.5">
            <Shield className="w-3.5 h-3.5 text-accent" />
            高频生效避坑规则
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
          onClick={() => onNavigate('rules', { rulesTab: 'rules' })}
          className="pressable mt-3.5 inline-flex items-center gap-0.5 text-[12px] font-semibold text-accent hover:text-accent-ink"
        >
          查看全部避坑规则
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </AsideCard>

      {/* Card 2: Derived Core Index Card */}
      <AsideCard
        title={
          <span className="inline-flex items-center gap-1.5">
            <Layers className="w-3.5 h-3.5 text-accent" />
            AI 随身速查卡
          </span>
        }
      >
        <p className="text-[12px] text-ink-3 leading-relaxed">
          每次对话自动带给 AI 的核心规矩精要，严格控制在 200 行内不挤占上下文。
        </p>
        {coreCard && (
          <div className="mt-2.5 flex items-center justify-between text-[12px] text-ink-3">
            <span>当前占用</span>
            <span className="num font-medium text-ink">
              {coreCard.lines} / 200 行 · {(coreCard.bytes / 1024).toFixed(1)} KB
            </span>
          </div>
        )}
        <button
          type="button"
          onClick={() => setShowCoreCardModal(true)}
          className="pressable mt-3 inline-flex items-center gap-1 text-[12px] font-semibold text-accent hover:text-accent-ink"
        >
          预览速查卡内容
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </AsideCard>

      {/* Card 3: Live Scheduler & Safety Valves */}
      <AsideCard
        title={
          <span className="inline-flex items-center gap-1.5">
            <CalendarClock className="w-3.5 h-3.5 text-accent" />
            调度与护栏状态
          </span>
        }
        className="!bg-panel-quiet"
      >
        <div className="space-y-2.5 text-[12.5px]">
          <div className="flex items-start justify-between gap-2">
            <span className="text-ink-3 shrink-0">自动调度</span>
            <span className="text-ink font-medium text-right">{scheduleSummaryText}</span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-ink-3">写入审批</span>
            <span className="text-ink font-medium">{effectiveApprovalLabel}</span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-ink-3">运行护栏</span>
            <span className="text-ink-2 text-[12px]">
              {safety_valves.workflow_enabled ? '自动整理已开' : '自动整理已关'} ·{' '}
              {safety_valves.auto_approve_writes ? '允许全自动' : '写入前须确认'}
            </span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-ink-3">最近批次</span>
            {latest_run ? (
              <button
                type="button"
                onClick={() =>
                  onNavigate('runs', { runsTab: 'pipeline', runId: latest_run.run_id })
                }
                className="pressable inline-flex items-center gap-1.5 hover:opacity-85"
              >
                <span className="num text-ink-2">{formatDateYMD(latest_run.started_at)}</span>
                <StatusBadge status={latest_run.status} />
              </button>
            ) : (
              <span className="text-ink-4">暂无</span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={() => onNavigate('automations')}
          className="pressable mt-3.5 inline-flex items-center gap-0.5 text-[12px] font-semibold text-accent hover:text-accent-ink"
        >
          管理定时调度与周五精简
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </AsideCard>
    </>
  )

  return (
    <ReadingLayout aside={aside}>
      <div className="space-y-5">
        {/* 1. Header */}
        <header className="pb-4 border-b border-line">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">今日态势</h1>
            <span className="hidden lg:inline num text-[12.5px] text-ink-4">
              {formatShanghaiToday()}
            </span>
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
            <p className="text-[13.5px] text-ink-3">
              {brand.name} · 开工查规矩、完工交证据、沉淀经审批、经验抗腐烂。
            </p>
            {!awaitingApproval && (
              <button
                type="button"
                onClick={() =>
                  onNavigate('runs', {
                    runsTab: metrics.sessions_pending > 0 ? 'queue' : 'pipeline',
                    queueStatus: metrics.sessions_pending > 0 ? 'queued' : undefined,
                  })
                }
                className="pressable inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-ink-2 text-[12.5px] font-medium hover:border-line-strong hover:text-ink"
              >
                <GitBranch className="w-3.5 h-3.5 text-accent" />
                {metrics.sessions_pending > 0
                  ? `去整理待审对话 (${metrics.sessions_pending})`
                  : '沉淀与审批'}
              </button>
            )}
          </div>
        </header>

        {/* 2. Actionable Governance Alerts (every chip deep-links straight to the target view) */}
        {awaitingApproval && latest_run && (
          <section className="rounded-[var(--radius-card)] border border-amber/40 bg-amber-soft/50 px-4 py-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-[13px] text-ink">
              <PauseCircle className="w-4 h-4 text-amber-ink shrink-0" />
              <span>
                <strong className="num font-semibold">{formatDateYMD(latest_run.started_at)}</strong>{' '}
                批次整理出{' '}
                <strong className="num font-semibold">{latest_run.planned_writes.length}</strong>{' '}
                项写入建议，确认后才会落盘
              </span>
            </div>
            <button
              type="button"
              onClick={() =>
                onNavigate('runs', { runsTab: 'pipeline', runId: latest_run.run_id })
              }
              className="pressable inline-flex items-center gap-1 h-8 px-3.5 rounded-[var(--radius-control)] bg-amber text-amber-contrast text-[12.5px] font-semibold hover:opacity-90"
            >
              立即审批
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </section>
        )}

        {!awaitingApproval && governanceAlertsCount > 0 && (
          <section className="rounded-[var(--radius-card)] border border-line-strong bg-surface-2 px-4 py-2.5 flex flex-wrap items-center justify-between gap-2.5 text-[12.5px]">
            <div className="flex flex-wrap items-center gap-2 text-ink-2">
              <AlertTriangle className="w-4 h-4 text-amber-ink shrink-0" />
              <span>需要你留意：</span>
              {openDissents > 0 && (
                <button
                  type="button"
                  onClick={() =>
                    onNavigate('rules', { rulesTab: 'clues', cluesSub: 'dissents' })
                  }
                  className="pressable chip hover:border-accent/50 hover:text-ink"
                >
                  {openDissents} 条规则分歧待处理
                  <ChevronRight className="w-3 h-3 text-ink-4" />
                </button>
              )}
              {expiredCards > 0 && (
                <button
                  type="button"
                  onClick={() =>
                    onNavigate('rules', { rulesTab: 'assets', assetsDocFilter: 'expired' })
                  }
                  className="pressable chip hover:border-accent/50 hover:text-ink"
                >
                  {expiredCards} 篇笔记待复核
                  <ChevronRight className="w-3 h-3 text-ink-4" />
                </button>
              )}
              {staleSkills > 0 && (
                <button
                  type="button"
                  onClick={() =>
                    onNavigate('rules', { rulesTab: 'assets', assetsSkillFilter: 'failing' })
                  }
                  className="pressable chip hover:border-accent/50 hover:text-ink"
                >
                  {staleSkills} 个 Skill 待检修
                  <ChevronRight className="w-3 h-3 text-ink-4" />
                </button>
              )}
            </div>
          </section>
        )}

        {/* 3. 4-Card Clickable KPI Strip (direct deep-links + animated numbers) */}
        <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
          <button
            type="button"
            onClick={() => onNavigate('rules', { rulesTab: 'outcomes' })}
            className="card-interactive p-4 text-left"
          >
            <div className="flex items-center justify-between text-[11.5px] text-ink-4">
              <span>实测交付率</span>
              <CheckCircle2 className="w-3.5 h-3.5 text-ok" />
            </div>
            <div className="mt-1.5 text-[22px] font-bold text-ink num">
              {metrics.real_verification_rate !== null ? (
                <AnimatedNumber value={metrics.real_verification_rate} suffix="%" />
              ) : (
                '暂无样本'
              )}
            </div>
            <div className="mt-1 text-[11.5px] text-ink-4 num whitespace-nowrap truncate">
              开局提醒 {metrics.preflight_activations} · 验收 {metrics.natural_outcomes_total} 次
            </div>
          </button>

          <button
            type="button"
            onClick={() => onNavigate('rules', { rulesTab: 'rules' })}
            className="card-interactive p-4 text-left"
          >
            <div className="flex items-center justify-between text-[11.5px] text-ink-4">
              <span>规则健康度</span>
              <Shield className="w-3.5 h-3.5 text-accent" />
            </div>
            <div className="mt-1.5 text-[22px] font-bold text-ink num">
              <AnimatedNumber value={activeRulesCount} />{' '}
              <span className="text-[12.5px] font-normal text-ink-3">条生效</span>
            </div>
            <div className="mt-1 text-[11.5px] text-ink-4 num">
              {verifiedRules} 已生效 · {unverifiedRules} 试运行 · {dormantRules} 休眠
            </div>
          </button>

          <button
            type="button"
            onClick={() =>
              onNavigate('rules', {
                rulesTab: 'clues',
                cluesSub: growth && growth.red_count > 0 ? 'action' : 'observations',
              })
            }
            className="card-interactive p-4 text-left"
          >
            <div className="flex items-center justify-between text-[11.5px] text-ink-4">
              <span>错题观察池</span>
              <ShieldAlert className="w-3.5 h-3.5 text-amber-ink" />
            </div>
            <div className="mt-1.5 text-[22px] font-bold text-ink num">
              <AnimatedNumber
                value={growth ? growth.observing_pool_count : metrics.open_retro_count}
              />{' '}
              <span className="text-[12.5px] font-normal text-ink-3">待观察</span>
            </div>
            <div className="mt-1 flex items-center gap-1.5 text-[11.5px] text-ink-4 num whitespace-nowrap overflow-hidden">
              {growth && growth.red_count > 0 ? (
                <span className="inline-flex items-center gap-1 text-hot font-medium shrink-0">
                  <span className="size-1.5 rounded-full bg-hot" />
                  {growth.red_count} 条漏检
                </span>
              ) : (
                <span className="shrink-0">无高危漏检</span>
              )}
              <span className="shrink-0">·</span>
              <span className="truncate">
                {growth?.sample_sufficient
                  ? `自查拦截 ${Math.round(growth.self_catch_rate * 100)}%`
                  : `线索 ${metrics.open_retro_count}`}
              </span>
            </div>
          </button>

          <button
            type="button"
            onClick={() =>
              onNavigate('runs', {
                runsTab: metrics.sessions_pending > 0 ? 'queue' : 'pipeline',
                queueStatus: metrics.sessions_pending > 0 ? 'queued' : undefined,
              })
            }
            className="card-interactive p-4 text-left"
          >
            <div className="flex items-center justify-between text-[11.5px] text-ink-4">
              <span>待整理对话</span>
              <Activity className="w-3.5 h-3.5 text-accent" />
            </div>
            <div className="mt-1.5 text-[22px] font-bold text-ink num">
              <AnimatedNumber value={metrics.sessions_pending} />{' '}
              <span className="text-[12.5px] font-normal text-ink-3">段待处理</span>
            </div>
            <div className="mt-1 text-[11.5px] text-ink-4 truncate">
              {sched?.running
                ? `定时中 · ${sched.human_schedule.replace('（上海时间）', '')}`
                : '自动调度已暂停'}
            </div>
          </button>
        </section>

        {/* 4. Latest P00-P07 Digest */}
        <section className="card p-5">
          <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <FileText className="w-4 h-4 text-accent" />
              <h2 className="text-[15.5px] font-bold text-ink">最近一次整理摘要</h2>
            </div>
            {latest_run && (
              <div className="flex flex-wrap items-center gap-2">
                {triageReport && (triageReport.checked || 0) > 0 && (
                  <span className="chip num">
                    初筛 {triageReport.checked} → 跳过闲聊 {triageReport.skipped || 0}
                  </span>
                )}
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
                  这一批还在等待审批或整理中，完成后会在这里展示摘要报告。
                </p>
              )}
              <div className="mt-4 pt-2.5 border-t border-line-soft flex justify-end">
                <button
                  type="button"
                  onClick={() =>
                    onNavigate('runs', { runsTab: 'pipeline', runId: latest_run.run_id })
                  }
                  className="pressable inline-flex items-center gap-0.5 text-[12px] font-medium text-accent hover:text-accent-ink"
                >
                  查看本批完整进度与明细
                  <ChevronRight className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          ) : (
            <div className="py-6 text-center space-y-2.5">
              <p className="text-[13px] text-ink-4">
                还没有整理批次，启动第一轮即可自动提炼知识笔记与避坑规则。
              </p>
              <button
                type="button"
                onClick={() => onNavigate('runs')}
                className="pressable inline-flex items-center gap-1 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink"
              >
                前往沉淀与审批
                <ChevronRight className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </section>

        {/* 5. Recent Knowledge Notes */}
        <section className="card p-5">
          <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <BookOpen className="w-4 h-4 text-ok" />
              <h2 className="text-[15px] font-bold text-ink">最近沉淀的知识笔记</h2>
            </div>
            <div className="flex items-center gap-2">
              {sinkInfo && (
                <span className="chip">{labelOf(SINK_TYPE_LABELS, sinkInfo.sink_type)}</span>
              )}
              <button
                type="button"
                onClick={() => onNavigate('rules', { rulesTab: 'assets' })}
                className="pressable inline-flex items-center gap-0.5 text-[12px] text-accent hover:text-accent-ink font-medium"
              >
                保鲜明细
                <ChevronRight className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
          {!docsLoaded ? (
            <div className="space-y-2.5 pt-3" aria-busy="true">
              {[0, 1, 2].map((i) => (
                <div key={i} className="skeleton-block h-11 w-full" />
              ))}
            </div>
          ) : docs.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-6 text-center">当前目录还没有沉淀的知识笔记。</p>
          ) : (
            <div className="divide-y divide-line-soft mt-1">
              {docs.slice(0, 8).map((d) => (
                <button
                  key={d.doc_path}
                  type="button"
                  onClick={() => setReadingDoc({ path: d.doc_path, title: d.title })}
                  className="pressable w-full text-left py-2.5 px-2 -mx-2 rounded-[var(--radius-tile)] hover:bg-bg-sunk/70 flex items-center justify-between gap-3"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-[13.5px] font-medium text-ink truncate">{d.title}</span>
                      {d.is_expired && (
                        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[10.5px] font-semibold shrink-0">
                          <span className="size-1.5 rounded-full bg-amber" />
                          待复核
                        </span>
                      )}
                    </div>
                    <div className="mono text-[11.5px] text-ink-4 truncate mt-0.5">{d.doc_path}</div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0 text-[11.5px] text-ink-4 num">
                    {d.freshness_tier && (
                      <span className="chip text-[10.5px]">
                        {FRESHNESS_TIER_SHORT[d.freshness_tier] || d.freshness_tier}
                      </span>
                    )}
                    {d.size_bytes > 0 && <span>{(d.size_bytes / 1024).toFixed(1)} KB</span>}
                  </div>
                </button>
              ))}
            </div>
          )}
        </section>
      </div>

      {/* Shared Document Reader Modal */}
      <DocumentReaderModal
        docPath={readingDoc?.path ?? null}
        title={readingDoc?.title}
        onClose={() => setReadingDoc(null)}
      />

      {/* Core Card Modal */}
      <CoreCardModal
        open={showCoreCardModal}
        card={coreCard}
        onClose={() => setShowCoreCardModal(false)}
        onRebuilt={setCoreCard}
      />
    </ReadingLayout>
  )
}
