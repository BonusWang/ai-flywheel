import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Layers, Loader2, Plus, RotateCcw, Save } from 'lucide-react'
import type { RouteIntent } from '../App'
import { api } from '../api'
import { PillTabs } from '../components/PageLayout'
import { AssetsFreshnessTab } from '../components/rules/AssetsFreshnessTab'
import { CluesObservationsTab } from '../components/rules/CluesObservationsTab'
import { CoreCardModal } from '../components/rules/CoreCardModal'
import { OutcomesLedgerTab } from '../components/rules/OutcomesLedgerTab'
import { PolicyStudioTab, type PolicyStudioActions } from '../components/rules/PolicyStudioTab'
import { RulesListTab } from '../components/rules/RulesListTab'
import { AnimatedNumber } from '../components/ui/AnimatedNumber'
import { ConfirmButton } from '../components/ui/ConfirmButton'
import { useToast } from '../components/ui/Toast'
import { RULE_STATUS_LABELS } from '../lib/labels'
import type {
  CoreCardPayload,
  DissentRecord,
  GrowthMetricsLive,
  KnowledgeDocSummary,
  MistakeObservation,
  OutcomeRecord,
  RetroItem,
  RuleRecord,
  SkillAssetRecord,
} from '../types'

type RulesTabKey = 'rules' | 'clues' | 'assets' | 'outcomes' | 'guidelines'

interface Props {
  refreshToken: number
  onGlobalRefresh: () => void
  intent?: RouteIntent
}

const RULE_STATUS_ORDER: Array<'all' | RuleRecord['status']> = [
  'all',
  'verified_active',
  'unverified_active',
  'needs_revision',
  'dormant',
  'retired',
]

export const RulesRetroPage: React.FC<Props> = ({
  refreshToken,
  onGlobalRefresh,
  intent,
}) => {
  const toast = useToast()
  const [tab, setTab] = useState<RulesTabKey>(intent?.rulesTab || 'rules')
  const [loadedOnce, setLoadedOnce] = useState(false)
  const [rules, setRules] = useState<RuleRecord[]>([])
  const [dissentsByRule, setDissentsByRule] = useState<Record<string, DissentRecord[]>>({})
  const [dissents, setDissents] = useState<DissentRecord[]>([])
  const [observations, setObservations] = useState<MistakeObservation[]>([])
  const [growth, setGrowth] = useState<GrowthMetricsLive | null>(null)
  const [retros, setRetros] = useState<RetroItem[]>([])
  const [skills, setSkills] = useState<SkillAssetRecord[]>([])
  const [knowledgeDocs, setKnowledgeDocs] = useState<KnowledgeDocSummary[]>([])
  const [outcomes, setOutcomes] = useState<OutcomeRecord[]>([])
  const [coreCard, setCoreCard] = useState<CoreCardPayload | null>(null)
  const [policies, setPolicies] = useState<Record<string, any>>({})
  const [showCoreCard, setShowCoreCard] = useState(false)
  const [filter, setFilter] = useState<string>('all')
  const [showCreate, setShowCreate] = useState(false)
  const [policyDirty, setPolicyDirty] = useState(false)
  const [policyBusy, setPolicyBusy] = useState(false)
  const policyActionsRef = useRef<PolicyStudioActions | null>(null)

  useEffect(() => {
    if (!intent?.nonce) return
    if (intent.rulesTab) setTab(intent.rulesTab)
  }, [intent?.nonce, intent?.rulesTab])

  const loadAll = useCallback(async () => {
    const [rRes, dRes, qRes, pRes, oRes, gRes, sRes, kRes, outRes, ccRes] = await Promise.all([
      api.listRules(),
      api.listDissents().catch(() => ({ dissents: [] })),
      api.listRetro(),
      api.listPolicies(),
      api.listObservations(),
      api.growthMetrics(),
      api.listSkills().catch(() => ({ skills: [], probed_count: 0 })),
      api.listKnowledge().catch(() => ({ sink_type: '', root: '', documents: [] })),
      api.listOutcomes().catch(() => ({ outcomes: [] })),
      api.coreCard().catch(() => null),
    ])
    setRules(rRes.rules)
    setDissentsByRule(rRes.dissents_by_rule || {})
    setDissents(dRes.dissents || [])
    setRetros(qRes.items)
    setPolicies(pRes.policies)
    setObservations(oRes.observations)
    setGrowth(gRes.live)
    setSkills(sRes.skills)
    setKnowledgeDocs(kRes.documents)
    setOutcomes(outRes.outcomes)
    if (ccRes) setCoreCard(ccRes)
    setLoadedOnce(true)
  }, [])

  useEffect(() => {
    loadAll().catch(() => setLoadedOnce(true))
  }, [refreshToken, loadAll])

  const reload = useCallback(async () => {
    await loadAll()
    onGlobalRefresh()
  }, [loadAll, onGlobalRefresh])

  const actionRequiredCount =
    observations.filter(
      (o) =>
        o.status === 'observing' &&
        (o.severity_grade === 'red_silent' || o.occurrence_count >= 3)
    ).length + dissents.filter((d) => d.status === 'open').length

  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = { all: rules.length }
    for (const r of rules) {
      counts[r.status] = (counts[r.status] || 0) + 1
    }
    return counts
  }, [rules])

  // Hide empty secondary status chips so users never click into dead ends
  const visibleStatusFilters = useMemo(
    () =>
      RULE_STATUS_ORDER.filter(
        (k) =>
          k === 'all' ||
          k === filter ||
          k === 'verified_active' ||
          k === 'unverified_active' ||
          (statusCounts[k] || 0) > 0
      ),
    [filter, statusCounts]
  )

  const filteredRules = filter === 'all' ? rules : rules.filter((r) => r.status === filter)

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">规则与进化</h1>
          <p className="text-[13.5px] text-ink-3 mt-1">
            管理避坑规则、错题与分歧、Skill 使用效果与笔记保鲜、完工验收记录及 AI 执行规矩。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setShowCoreCard(true)}
            className="pressable inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-2 hover:border-line-strong hover:text-ink"
          >
            <Layers className="w-3.5 h-3.5 text-accent" />
            <span>随身速查卡{coreCard ? ` (${coreCard.lines}/200)` : ''}</span>
          </button>
          {tab === 'rules' && (
            <button
              type="button"
              onClick={() => setShowCreate((v) => !v)}
              aria-expanded={showCreate}
              className={`pressable inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] text-[12.5px] font-medium ${
                showCreate
                  ? 'border border-accent/45 bg-accent-soft text-accent-ink'
                  : 'bg-accent text-accent-contrast hover:bg-accent-ink'
              }`}
            >
              <Plus
                className={`w-3.5 h-3.5 transition-transform duration-200 ease-[var(--ease-out-quint)] ${
                  showCreate ? 'rotate-45' : ''
                }`}
              />
              <span>{showCreate ? '收起表单' : '新增规则'}</span>
            </button>
          )}
          {tab === 'guidelines' && (
            <>
              {policyDirty && (
                <button
                  type="button"
                  disabled={policyBusy}
                  onClick={() => policyActionsRef.current?.discard()}
                  className="pressable h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-3 hover:text-ink"
                >
                  放弃修改
                </button>
              )}
              <ConfirmButton
                label={
                  <span className="inline-flex items-center gap-1.5">
                    <RotateCcw className="w-3.5 h-3.5" />
                    恢复默认
                  </span>
                }
                confirmLabel="确认恢复默认？"
                disabled={policyBusy}
                onConfirm={() => policyActionsRef.current?.reset()}
                className="h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-3 hover:border-hot/50 hover:text-hot disabled:opacity-50"
                confirmClassName="h-8 px-3 rounded-[var(--radius-control)] border border-hot bg-hot text-white text-[12.5px] font-semibold"
              />
              <button
                type="button"
                disabled={!policyDirty || policyBusy}
                onClick={() => policyActionsRef.current?.save()}
                className="pressable inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink disabled:opacity-45"
              >
                {policyBusy ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Save className="w-3.5 h-3.5" />
                )}
                <span>{policyBusy ? '保存中…' : policyDirty ? '保存修改' : '已同步'}</span>
              </button>
            </>
          )}
        </div>
      </header>

      {/* Compact 3-metric growth health bar */}
      {growth ? (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-[12.5px]">
          <button
            type="button"
            onClick={() => setTab('clues')}
            className="card-interactive px-4 py-3 flex items-center justify-between gap-2 text-left"
          >
            <div>
              <div className="text-[11.5px] text-ink-4">错题观察池</div>
              <div className="mt-0.5 text-[15px] font-semibold text-ink num flex items-center gap-2 flex-wrap">
                <span>
                  <AnimatedNumber value={growth.observing_pool_count} /> 条待观察
                </span>
                {growth.red_count > 0 && (
                  <span className="inline-flex items-center gap-1 text-[12px] text-hot font-medium">
                    <span className="size-1.5 rounded-full bg-hot" />
                    {growth.red_count} 条漏检
                  </span>
                )}
              </div>
            </div>
            <span className="text-[11.5px] text-ink-4 shrink-0 whitespace-nowrap">
              {growth.sample_sufficient
                ? `自查拦截 ${Math.round(growth.self_catch_rate * 100)}%`
                : '样本 < 5 条'}
            </span>
          </button>

          <button
            type="button"
            onClick={() => setTab('rules')}
            className="card-interactive px-4 py-3 flex items-center justify-between gap-2 text-left"
          >
            <div>
              <div className="text-[11.5px] text-ink-4">规则健康度</div>
              <div className="mt-0.5 text-[15px] font-semibold text-ink num">
                <AnimatedNumber value={growth.active_rule_count} /> 条生效
                <span className="ml-2 text-[12px] text-ink-4 font-normal">
                  · {growth.dormant_rule_count} 条休眠
                </span>
              </div>
            </div>
            <span className="text-[11.5px] text-ink-4 shrink-0 whitespace-nowrap">
              14天零命中休眠
            </span>
          </button>

          <button
            type="button"
            onClick={() => setTab(growth.open_dissent_count > 0 ? 'clues' : 'assets')}
            className="card-interactive px-4 py-3 flex items-center justify-between gap-2 text-left"
          >
            <div>
              <div className="text-[11.5px] text-ink-4">分歧与笔记保鲜</div>
              <div className="mt-0.5 text-[15px] font-semibold text-ink num">
                <AnimatedNumber value={growth.open_dissent_count} /> 条分歧
                <span className="ml-2 text-[12px] text-ink-4 font-normal">
                  · {growth.expired_card_count} 篇待复核
                </span>
              </div>
            </div>
            <span className="text-[11.5px] text-ink-4 shrink-0 whitespace-nowrap">
              反例保留不覆盖
            </span>
          </button>
        </div>
      ) : !loadedOnce ? (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <div key={i} className="card px-4 py-3.5">
              <div className="skeleton-block h-10 w-full" />
            </div>
          ))}
        </div>
      ) : null}

      {/* Main 5 governance tabs */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PillTabs
          items={[
            { key: 'rules', label: '避坑规则', count: rules.length },
            { key: 'clues', label: '错题与分歧', count: actionRequiredCount },
            {
              key: 'assets',
              label: 'Skill 与保鲜',
              count: skills.length + knowledgeDocs.filter((d) => d.is_expired).length,
            },
            { key: 'outcomes', label: '完工验收', count: outcomes.length },
            { key: 'guidelines', label: '执行规矩' },
          ]}
          active={tab}
          onChange={(k) => setTab(k as RulesTabKey)}
        />

        {tab === 'rules' && rules.length > 0 && (
          <div className="flex flex-wrap gap-1.5 text-[12px]">
            {visibleStatusFilters.map((k) => {
              const label = k === 'all' ? '全部' : RULE_STATUS_LABELS[k] || k
              const count = statusCounts[k] || 0
              const active = filter === k
              return (
                <button
                  key={k}
                  type="button"
                  onClick={() => setFilter(k)}
                  className={`pressable inline-flex items-center gap-1 px-2.5 py-1 rounded-full ${
                    active
                      ? 'bg-accent-soft text-accent-ink font-semibold'
                      : 'text-ink-4 hover:text-ink hover:bg-bg-sunk'
                  }`}
                >
                  <span>{label}</span>
                  <span className="num text-[11px] opacity-75">{count}</span>
                </button>
              )
            })}
          </div>
        )}
      </div>

      {/* Keep Rules and Policy drafts alive across tab switches */}
      <div className={tab === 'rules' ? 'animate-tab-enter space-y-4' : 'hidden'}>
        <RulesListTab
          rules={filteredRules}
          dissentsByRule={dissentsByRule}
          showCreate={showCreate}
          onCloseCreate={() => setShowCreate(false)}
          onChanged={reload}
          onResetStatusFilter={filter !== 'all' ? () => setFilter('all') : undefined}
        />
      </div>

      {tab === 'clues' && (
        <div key="clues-tab" className="animate-tab-enter">
          <CluesObservationsTab
            observations={observations}
            dissents={dissents}
            retros={retros}
            initialSubView={intent?.cluesSub}
            intentNonce={intent?.nonce}
            onChanged={reload}
          />
        </div>
      )}

      {tab === 'assets' && (
        <div key="assets-tab" className="animate-tab-enter">
          <AssetsFreshnessTab
            skills={skills}
            knowledgeDocs={knowledgeDocs}
            initialDocFilter={intent?.assetsDocFilter}
            initialSkillFilter={intent?.assetsSkillFilter}
            intentNonce={intent?.nonce}
            onChanged={reload}
          />
        </div>
      )}

      {tab === 'outcomes' && (
        <div key="outcomes-tab" className="animate-tab-enter">
          <OutcomesLedgerTab outcomes={outcomes} />
        </div>
      )}

      <div className={tab === 'guidelines' ? 'animate-tab-enter' : 'hidden'}>
        <PolicyStudioTab
          policies={policies}
          onChanged={reload}
          onDirtyChange={setPolicyDirty}
          onBusyChange={setPolicyBusy}
          actionsRef={policyActionsRef}
        />
      </div>

      <CoreCardModal
        open={showCoreCard}
        card={coreCard}
        onClose={() => setShowCoreCard(false)}
        onRebuilt={(updated) => {
          setCoreCard(updated)
          toast.push('已重建 AI 随身速查卡（FLYWHEEL-CORE.md）')
        }}
      />
    </div>
  )
}
