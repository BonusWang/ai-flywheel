import React, { useEffect, useRef, useState } from 'react'
import { Layers, Plus, RotateCcw, Save } from 'lucide-react'
import { api } from '../api'
import { PillTabs } from '../components/PageLayout'
import { AssetsFreshnessTab } from '../components/rules/AssetsFreshnessTab'
import { CluesObservationsTab } from '../components/rules/CluesObservationsTab'
import { CoreCardModal } from '../components/rules/CoreCardModal'
import { OutcomesLedgerTab } from '../components/rules/OutcomesLedgerTab'
import { PolicyStudioTab, type PolicyStudioActions } from '../components/rules/PolicyStudioTab'
import { RulesListTab } from '../components/rules/RulesListTab'
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
  initialTab?: 'rules' | 'guidelines' | 'clues'
}

export const RulesRetroPage: React.FC<Props> = ({
  refreshToken,
  onGlobalRefresh,
  initialTab = 'rules',
}) => {
  const [tab, setTab] = useState<RulesTabKey>(initialTab)
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
  const [notice, setNotice] = useState<string | null>(null)
  const policyActionsRef = useRef<PolicyStudioActions | null>(null)

  useEffect(() => {
    setTab(initialTab)
  }, [initialTab])

  const loadAll = async () => {
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
  }

  useEffect(() => {
    loadAll().catch(() => {})
  }, [refreshToken])

  const showToast = (msg: string) => {
    setNotice(msg)
    setTimeout(() => setNotice(null), 3000)
  }

  const reload = async () => {
    await loadAll()
    onGlobalRefresh()
  }

  const actionRequiredCount =
    observations.filter(
      (o) =>
        o.status === 'observing' &&
        (o.severity_grade === 'red_silent' || o.occurrence_count >= 3)
    ).length + dissents.filter((d) => d.status === 'open').length
  const filteredRules = filter === 'all' ? rules : rules.filter((r) => r.status === filter)

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">规则与进化</h1>
          <p className="text-[13.5px] text-ink-3 mt-1">
            统一管理避坑规则、错题与分歧、Skill 实效与经验保鲜、实证验收台账及核心索引卡。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {notice && (
            <span className="px-3 py-1 rounded-full bg-ok-soft text-ok-ink text-[12px] border border-ok/25">
              {notice}
            </span>
          )}
          <button
            type="button"
            onClick={() => setShowCoreCard(true)}
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-2 hover:text-ink"
          >
            <Layers className="w-3.5 h-3.5 text-accent" />
            核心索引卡{coreCard ? ` (${coreCard.lines}/200)` : ''}
          </button>
          {tab === 'rules' && (
            <button
              type="button"
              onClick={() => setShowCreate((v) => !v)}
              className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink"
            >
              <Plus className="w-3.5 h-3.5" />
              新增一条规则
            </button>
          )}
          {tab === 'guidelines' && (
            <>
              <button
                type="button"
                onClick={() => policyActionsRef.current?.reset()}
                className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-3 hover:text-ink"
              >
                <RotateCcw className="w-3.5 h-3.5" />
                恢复默认
              </button>
              <button
                type="button"
                onClick={() => policyActionsRef.current?.save()}
                className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink"
              >
                <Save className="w-3.5 h-3.5" />
                保存修改
              </button>
            </>
          )}
        </div>
      </header>

      {/* Compact 3-metric growth health bar */}
      {growth && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-[12.5px]">
          <div className="card px-4 py-3 flex items-center justify-between gap-2">
            <div>
              <div className="text-[11.5px] text-ink-4">错题观察池</div>
              <div className="mt-0.5 text-[15px] font-semibold text-ink num">
                {growth.observing_pool_count} 条待观察
                {growth.red_count > 0 && (
                  <span className="ml-2 text-[12px] text-hot font-medium">
                    🔴 {growth.red_count} 条沉默错
                  </span>
                )}
              </div>
            </div>
            <span className="text-[11.5px] text-ink-4 shrink-0 whitespace-nowrap">
              {growth.sample_sufficient
                ? `自查拦截 ${Math.round(growth.self_catch_rate * 100)}%`
                : '样本 < 5 条'}
            </span>
          </div>

          <div className="card px-4 py-3 flex items-center justify-between gap-2">
            <div>
              <div className="text-[11.5px] text-ink-4">规则健康度</div>
              <div className="mt-0.5 text-[15px] font-semibold text-ink num">
                {growth.active_rule_count} 条生效
                <span className="ml-2 text-[12px] text-ink-4 font-normal">
                  · {growth.dormant_rule_count} 条已休眠
                </span>
              </div>
            </div>
            <span className="text-[11.5px] text-ink-4 shrink-0 whitespace-nowrap">14天零命中休眠</span>
          </div>

          <div className="card px-4 py-3 flex items-center justify-between gap-2">
            <div>
              <div className="text-[11.5px] text-ink-4">分歧与经验保鲜</div>
              <div className="mt-0.5 text-[15px] font-semibold text-ink num">
                {growth.open_dissent_count} 条分歧
                <span className="ml-2 text-[12px] text-ink-4 font-normal">
                  · {growth.expired_card_count} 篇待复核
                </span>
              </div>
            </div>
            <span className="text-[11.5px] text-ink-4 shrink-0 whitespace-nowrap">反例保留不覆盖</span>
          </div>
        </div>
      )}

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
            { key: 'outcomes', label: '证据台账', count: outcomes.length },
            { key: 'guidelines', label: '执行规矩' },
          ]}
          active={tab}
          onChange={(k) => setTab(k as RulesTabKey)}
        />

        {tab === 'rules' && (
          <div className="flex flex-wrap gap-1.5 text-[12px]">
            {[
              ['all', '全部'],
              ['verified_active', '已生效'],
              ['unverified_active', '试运行'],
              ['needs_revision', '需修改'],
              ['dormant', '已休眠'],
              ['retired', '已停用'],
            ].map(([k, label]) => (
              <button
                key={k}
                type="button"
                onClick={() => setFilter(k)}
                className={`px-2.5 py-1 rounded-full transition-colors ${
                  filter === k
                    ? 'bg-accent-soft text-accent-ink font-semibold'
                    : 'text-ink-4 hover:text-ink hover:bg-bg-sunk'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* The rules and policy tabs keep their drafts while the user switches tabs,
          exactly like the page-level state they replaced. */}
      <div className={tab === 'rules' ? '' : 'hidden'}>
        <RulesListTab
          rules={filteredRules}
          dissentsByRule={dissentsByRule}
          showCreate={showCreate}
          onCloseCreate={() => setShowCreate(false)}
          onChanged={reload}
          onToast={showToast}
        />
      </div>

      {tab === 'clues' && (
        <CluesObservationsTab
          observations={observations}
          dissents={dissents}
          retros={retros}
          onChanged={reload}
          onToast={showToast}
        />
      )}

      {tab === 'assets' && (
        <AssetsFreshnessTab skills={skills} knowledgeDocs={knowledgeDocs} />
      )}

      {tab === 'outcomes' && <OutcomesLedgerTab outcomes={outcomes} />}

      <div className={tab === 'guidelines' ? '' : 'hidden'}>
        <PolicyStudioTab
          policies={policies}
          onChanged={reload}
          onToast={showToast}
          actionsRef={policyActionsRef}
        />
      </div>

      <CoreCardModal
        open={showCoreCard}
        card={coreCard}
        onClose={() => setShowCoreCard(false)}
        onRebuilt={(updated) => {
          setCoreCard(updated)
          showToast('已重建跨端核心索引卡 FLYWHEEL-CORE.md')
        }}
      />
    </div>
  )
}
