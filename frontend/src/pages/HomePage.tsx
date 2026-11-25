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
  X,
} from 'lucide-react'
import type { PageKey } from '../App'
import { api } from '../api'
import { MarkdownView } from '../components/MarkdownView'
import { AsideCard, ReadingLayout } from '../components/PageLayout'
import { CoreCardModal } from '../components/rules/CoreCardModal'
import { StatusBadge } from '../components/StatusBadge'
import {
  APPROVAL_MODE_LABELS,
  formatDateYMD,
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
  onNavigate: (page: PageKey) => void
  onRefresh: () => void
}

function todayFormatted() {
  const now = new Date()
  const week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][now.getDay()]
  return `${now.getMonth() + 1}月${now.getDate()}日 · ${week}`
}

const FRESHNESS_TIER_SHORT: Record<string, string> = {
  volatile: '30天',
  evolving: '90天',
  evergreen: '180天',
}

export const HomePage: React.FC<Props> = ({ overview, onNavigate }) => {
  const [docs, setDocs] = useState<KnowledgeDocSummary[]>([])
  const [docsLoaded, setDocsLoaded] = useState(false)
  const [sinkInfo, setSinkInfo] = useState<{ sink_type: string; root: string } | null>(null)
  const [automations, setAutomations] = useState<AutomationsSnapshot | null>(null)
  const [growth, setGrowth] = useState<GrowthMetricsLive | null>(null)
  const [coreCard, setCoreCard] = useState<CoreCardPayload | null>(null)
  const [showCoreCardModal, setShowCoreCardModal] = useState(false)
  const [readingDoc, setReadingDoc] = useState<{
    doc_path: string
    content: string
    sha256: string
  } | null>(null)

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

  const openDoc = async (path: string) => {
    try {
      const data = await api.readKnowledgeDoc(path)
      setReadingDoc(data)
    } catch {
      // ignore
    }
  }

  if (!overview) {
    return <div className="py-20 text-center text-[13px] text-ink-4">正在加载控制面态势...</div>
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
      ? `${sched.human_schedule}${sched.friday_weekly_enabled ? '（含周五蒸馏）' : ''}`
      : '自动调度已暂停（可随时手动触发）'
    : '读取调度状态中…'

  const effectiveApprovalLabel = policy
    ? labelOf(APPROVAL_MODE_LABELS, policy.effective_approval_mode, policy.effective_approval_mode)
    : safety_valves.auto_approve_writes
      ? '全部自动'
      : '智能审批'

  // Extract P00 triage stats from latest run if present
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
                  <span className="num">已拦截/提醒 {r.hit_count} 次</span>
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
          进入规则与进化中心
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </AsideCard>

      {/* Card 2: Derived Core Index Card */}
      <AsideCard
        title={
          <span className="inline-flex items-center gap-1.5">
            <Layers className="w-3.5 h-3.5 text-accent" />
            跨端核心索引卡
          </span>
        }
      >
        <p className="text-[12px] text-ink-3 leading-relaxed">
          通过 <code className="mono text-[11px] text-ink-2">flywheel://core-card</code>{' '}
          暴露给多端智能体的热态治理摘要（≤200 行硬上限）。
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
          className="mt-3 inline-flex items-center gap-1 text-[12px] font-semibold text-accent hover:text-accent-ink"
        >
          预览 FLYWHEEL-CORE.md
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </AsideCard>

      {/* Card 3: Live Scheduler & Safety Valves */}
      <AsideCard
        title={
          <span className="inline-flex items-center gap-1.5">
            <CalendarClock className="w-3.5 h-3.5 text-accent" />
            调度与安全阀实况
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
            <span className="text-ink-3">有效审批模式</span>
            <span className="text-ink font-medium">{effectiveApprovalLabel}</span>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-ink-3">安全阀护栏</span>
            <span className="text-ink-2 text-[12px]">
              {safety_valves.workflow_enabled ? '流水线开' : '流水线关'} ·{' '}
              {safety_valves.auto_approve_writes ? '允许全自动' : '强制审批否决'}
            </span>
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
        </div>
        <button
          type="button"
          onClick={() => onNavigate('automations')}
          className="mt-3.5 inline-flex items-center gap-0.5 text-[12px] font-semibold text-accent hover:text-accent-ink"
        >
          管理自动化调度与蒸馏
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </AsideCard>
    </>
  )

  return (
    <ReadingLayout aside={aside}>
      <div className="space-y-5">
        {/* 1. Control Plane Header */}
        <header className="pb-4 border-b border-line">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div className="flex items-center gap-2">
              <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">今日态势</h1>
              <span className="chip">治理与进化控制面</span>
            </div>
            <span className="num text-[12.5px] text-ink-4">{todayFormatted()}</span>
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
            <p className="text-[13.5px] text-ink-3">
              {brand.name} · 开工查规矩、完工交证据、沉淀经审批、经验抗腐烂。
            </p>
            <button
              type="button"
              onClick={() => onNavigate('runs')}
              className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-ink-2 text-[12.5px] font-medium hover:border-line-strong hover:text-ink transition-colors"
            >
              <GitBranch className="w-3.5 h-3.5 text-accent" />
              沉淀与审批台
            </button>
          </div>
        </header>

        {/* 2. Actionable Governance Alerts */}
        {awaitingApproval && latest_run && (
          <section className="rounded-[var(--radius-card)] border border-amber/40 bg-amber-soft/50 px-4 py-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-[13px] text-ink">
              <PauseCircle className="w-4 h-4 text-amber-ink shrink-0" />
              <span>
                <strong className="num font-semibold">{formatDateYMD(latest_run.started_at)}</strong>{' '}
                批次生成了{' '}
                <strong className="num font-semibold">{latest_run.planned_writes.length}</strong>{' '}
                项受控写入计划，等待你的审批
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

        {!awaitingApproval && governanceAlertsCount > 0 && (
          <section className="rounded-[var(--radius-card)] border border-line-strong bg-surface-2 px-4 py-2.5 flex flex-wrap items-center justify-between gap-3 text-[12.5px]">
            <div className="flex flex-wrap items-center gap-2 text-ink-2">
              <AlertTriangle className="w-4 h-4 text-amber-ink shrink-0" />
              <span>当前治理待办：</span>
              {openDissents > 0 && (
                <span className="chip">{openDissents} 条未决分歧</span>
              )}
              {expiredCards > 0 && (
                <span className="chip">{expiredCards} 篇经验卡待复核</span>
              )}
              {staleSkills > 0 && (
                <span className="chip">{staleSkills} 个 Skill 待检修/清理</span>
              )}
            </div>
            <button
              type="button"
              onClick={() => onNavigate('rules')}
              className="inline-flex items-center gap-0.5 text-[12px] font-semibold text-accent hover:text-accent-ink"
            >
              去处理
              <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </section>
        )}

        {/* 3. 4-Card Governance KPI Strip */}
        <section className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
          <div className="card p-4">
            <div className="flex items-center justify-between text-[11.5px] text-ink-4">
              <span>真实证据通过率</span>
              <CheckCircle2 className="w-3.5 h-3.5 text-ok" />
            </div>
            <div className="mt-1.5 text-[22px] font-bold text-ink num">
              {metrics.real_verification_rate !== null
                ? `${metrics.real_verification_rate}%`
                : '暂无样本'}
            </div>
            <div className="mt-1 text-[11.5px] text-ink-4 num">
              预检激活 {metrics.preflight_activations} 次 · 验收 {metrics.natural_outcomes_total} 次
            </div>
          </div>

          <div className="card p-4">
            <div className="flex items-center justify-between text-[11.5px] text-ink-4">
              <span>规则健康度</span>
              <Shield className="w-3.5 h-3.5 text-accent" />
            </div>
            <div className="mt-1.5 text-[22px] font-bold text-ink num">
              {activeRulesCount}{' '}
              <span className="text-[12.5px] font-normal text-ink-3">条生效</span>
            </div>
            <div className="mt-1 text-[11.5px] text-ink-4 num">
              {verifiedRules} 已验证 · {unverifiedRules} 试运行 · {dormantRules} 休眠
            </div>
          </div>

          <div className="card p-4">
            <div className="flex items-center justify-between text-[11.5px] text-ink-4">
              <span>错题观察池</span>
              <ShieldAlert className="w-3.5 h-3.5 text-amber-ink" />
            </div>
            <div className="mt-1.5 text-[22px] font-bold text-ink num">
              {growth ? growth.observing_pool_count : metrics.open_retro_count}{' '}
              <span className="text-[12.5px] font-normal text-ink-3">待观察</span>
            </div>
            <div className="mt-1 text-[11.5px] text-ink-4 num">
              {growth && growth.red_count > 0 ? (
                <span className="text-hot font-medium">🔴 {growth.red_count} 沉默错</span>
              ) : (
                <span>无沉默高危错</span>
              )}
              {' · '}
              {growth?.sample_sufficient
                ? `自查拦截 ${Math.round(growth.self_catch_rate * 100)}%`
                : `复盘线索 ${metrics.open_retro_count}`}
            </div>
          </div>

          <div className="card p-4">
            <div className="flex items-center justify-between text-[11.5px] text-ink-4">
              <span>会话与自动化</span>
              <Activity className="w-3.5 h-3.5 text-accent" />
            </div>
            <div className="mt-1.5 text-[22px] font-bold text-ink num">
              {metrics.sessions_pending}{' '}
              <span className="text-[12.5px] font-normal text-ink-3">待审会话</span>
            </div>
            <div className="mt-1 text-[11.5px] text-ink-4 truncate">
              {sched?.running ? `调度中 · ${sched.human_schedule.replace('（上海时间）', '')}` : '自动调度已暂停'}
            </div>
          </div>
        </section>

        {/* 4. Latest P00-P07 Governance Digest */}
        <section className="card p-5">
          <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <FileText className="w-4 h-4 text-accent" />
              <h2 className="text-[15.5px] font-bold text-ink">最近一次治理批次摘要</h2>
            </div>
            {latest_run && (
              <div className="flex flex-wrap items-center gap-2">
                {triageReport && (triageReport.checked || 0) > 0 && (
                  <span className="chip num">
                    初筛 {triageReport.checked} → 跳过噪音 {triageReport.skipped || 0}
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
                  这一批还在等待审批或执行中，完成后会在这里展示摘要报告。
                </p>
              )}
              <div className="mt-4 pt-2.5 border-t border-line-soft flex justify-end">
                <button
                  type="button"
                  onClick={() => onNavigate('runs')}
                  className="inline-flex items-center gap-0.5 text-[12px] font-medium text-accent hover:text-accent-ink"
                >
                  查看 8 节点流水线与审计轨迹
                  <ChevronRight className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          ) : (
            <p className="text-[13px] text-ink-4 py-6 text-center">
              还没有批次记录，去「沉淀与审批」启动第一轮治理流水线吧。
            </p>
          )}
        </section>

        {/* 5. Controlled Write Assets (secondary, with freshness status) */}
        <section className="card p-5">
          <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
            <div className="flex items-center gap-2">
              <BookOpen className="w-4 h-4 text-ok" />
              <h2 className="text-[15px] font-bold text-ink">近期受控写入的 Markdown 资产</h2>
            </div>
            <div className="flex items-center gap-2">
              {sinkInfo && (
                <span className="chip">{labelOf(SINK_TYPE_LABELS, sinkInfo.sink_type)}</span>
              )}
              <button
                type="button"
                onClick={() => onNavigate('rules')}
                className="text-[12px] text-accent hover:text-accent-ink font-medium"
              >
                保鲜明细 →
              </button>
            </div>
          </div>
          {!docsLoaded ? (
            <p className="text-[13px] text-ink-4 py-6 text-center">正在加载受控资产...</p>
          ) : docs.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-6 text-center">当前落点还没有受控写入的文档。</p>
          ) : (
            <div className="divide-y divide-line-soft mt-1">
              {docs.slice(0, 8).map((d) => (
                <button
                  key={d.doc_path}
                  type="button"
                  onClick={() => openDoc(d.doc_path)}
                  className="w-full text-left py-2.5 px-2 -mx-2 rounded-[var(--radius-tile)] hover:bg-bg-sunk/70 transition-colors flex items-center justify-between gap-3"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-[13.5px] font-medium text-ink truncate">{d.title}</span>
                      {d.is_expired && (
                        <span className="px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[10.5px] font-semibold shrink-0">
                          ⚠️ 待复核
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
                    <span>{(d.size_bytes / 1024).toFixed(1)} KB</span>
                  </div>
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
