import React, { useEffect, useState } from 'react'
import { AlertTriangle, ArrowUpRight, CheckCircle2, Plus, RefreshCcw, RotateCcw, Save } from 'lucide-react'
import { api } from '../api'
import { PillTabs } from '../components/PageLayout'
import { StatusBadge } from '../components/StatusBadge'
import { CLIENT_LABELS, labelOf, POLICY_SOURCE_LABELS, RETRO_KIND_LABELS } from '../lib/labels'
import type {
  DissentRecord,
  GrowthMetricsLive,
  MistakeObservation,
  RetroItem,
  RuleRecord,
} from '../types'

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
  const [tab, setTab] = useState<'rules' | 'guidelines' | 'clues'>(initialTab)
  const [rules, setRules] = useState<RuleRecord[]>([])
  const [dissentsByRule, setDissentsByRule] = useState<Record<string, DissentRecord[]>>({})
  const [observations, setObservations] = useState<MistakeObservation[]>([])
  const [growth, setGrowth] = useState<GrowthMetricsLive | null>(null)
  const [retros, setRetros] = useState<RetroItem[]>([])
  const [filter, setFilter] = useState<string>('all')
  const [showCreate, setShowCreate] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [dissentRuleId, setDissentRuleId] = useState<string | null>(null)
  const [dissentClaim, setDissentClaim] = useState('')
  const [dissentScope, setDissentScope] = useState('')

  // New rule fields
  const [title, setTitle] = useState('')
  const [triggers, setTriggers] = useState('')
  const [checklist, setChecklist] = useState('')
  const [forbidden, setForbidden] = useState('')

  // My guidelines (merged Policy Studio)
  const [policies, setPolicies] = useState<Record<string, any>>({})
  const [antiEnabled, setAntiEnabled] = useState(true)
  const [baselineText, setBaselineText] = useState('')
  const [execText, setExecText] = useState('')
  const [addressAs, setAddressAs] = useState('')
  const [redlinesText, setRedlinesText] = useState('')
  const [retryMax, setRetryMax] = useState(2)

  useEffect(() => {
    setTab(initialTab)
  }, [initialTab])

  const loadAll = async () => {
    const [rRes, qRes, pRes, oRes, gRes] = await Promise.all([
      api.listRules(),
      api.listRetro(),
      api.listPolicies(),
      api.listObservations(),
      api.growthMetrics(),
    ])
    setRules(rRes.rules)
    setDissentsByRule(rRes.dissents_by_rule || {})
    setRetros(qRes.items)
    setPolicies(pRes.policies)
    setObservations(oRes.observations)
    setGrowth(gRes.live)

    const al = pRes.policies.anti_laziness || {}
    setAntiEnabled(al.enabled !== false)
    setBaselineText((al.baseline?.rules || []).join('\n'))
    setExecText((al.execution?.rules || []).join('\n'))

    const up = pRes.policies.user_preferences || {}
    setAddressAs(up.address_as || '')
    setRedlinesText((up.non_negotiables || []).join('\n'))

    const hg = pRes.policies.hard_gates || {}
    setRetryMax(Number(hg.retry_breaker?.max_same_cause_retries ?? 2))
  }

  useEffect(() => {
    loadAll().catch(() => {})
  }, [refreshToken])

  const showToast = (msg: string) => {
    setNotice(msg)
    setTimeout(() => setNotice(null), 3000)
  }

  const openClues = retros.filter((x) => x.status === 'open' || x.status === 'in_review')
  const observingPool = observations.filter((o) => o.status === 'observing')
  const filteredRules = filter === 'all' ? rules : rules.filter((r) => r.status === filter)

  const handleTransition = async (ruleId: string, status: RuleRecord['status']) => {
    await api.transitionRule(ruleId, status, '人工调整规则状态')
    await loadAll()
    onGlobalRefresh()
    if (status === 'unverified_active') {
      showToast('规则已唤醒并重新进入试运行')
    }
  }

  const handlePromoteObservation = async (obsId: string) => {
    await api.promoteObservation(obsId)
    await loadAll()
    onGlobalRefresh()
    showToast('已将该错题升级为试运行避坑规则')
  }

  const handleArchiveObservation = async (obsId: string) => {
    await api.archiveObservation(obsId)
    await loadAll()
    onGlobalRefresh()
  }

  const handleAddDissent = async (ruleId: string) => {
    if (!dissentClaim.trim()) return
    await api.createDissent({
      target_type: 'rule',
      target_id: ruleId,
      counter_claim: dissentClaim.trim(),
      condition_scope: dissentScope.trim(),
    })
    setDissentRuleId(null)
    setDissentClaim('')
    setDissentScope('')
    await loadAll()
    showToast('已记下例外/分歧，下次命中规则时会一并提醒')
  }

  const handleDissentStatus = async (dissentId: string, status: 'merged' | 'dismissed') => {
    await api.updateDissentStatus(dissentId, status)
    await loadAll()
    showToast(status === 'merged' ? '分歧已标记为吸收' : '分歧已忽略')
  }

  const handleRetroStatus = async (retroId: string, status: RetroItem['status']) => {
    await api.updateRetroStatus(retroId, status)
    await loadAll()
    onGlobalRefresh()
  }

  const submitRule = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim()) return
    await api.createRule({
      title: title.trim(),
      domain: 'engineering',
      triggers: triggers
        .split(/[,，]/)
        .map((s) => s.trim())
        .filter(Boolean),
      checklist: checklist
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean),
      forbidden_routes: forbidden
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean),
    })
    setTitle('')
    setTriggers('')
    setChecklist('')
    setForbidden('')
    setShowCreate(false)
    await loadAll()
    onGlobalRefresh()
    showToast('已新增一条试运行规则')
  }

  const savePolicies = async () => {
    await Promise.all([
      api.updatePolicy('anti_laziness', {
        enabled: antiEnabled,
        baseline: { rules: baselineText.split('\n').map((s) => s.trim()).filter(Boolean) },
        execution: { rules: execText.split('\n').map((s) => s.trim()).filter(Boolean) },
      }),
      api.updatePolicy('user_preferences', {
        address_as: addressAs.trim() || 'Developer',
        non_negotiables: redlinesText.split('\n').map((s) => s.trim()).filter(Boolean),
      }),
      api.updatePolicy('hard_gates', {
        retry_breaker: {
          max_same_cause_retries: retryMax,
          alert_at_attempt: retryMax + 1,
        },
      }),
    ])
    await loadAll()
    onGlobalRefresh()
    showToast('规矩已保存，下次对话立即生效')
  }

  const resetAllPolicies = async () => {
    await Promise.all([
      api.resetPolicy('anti_laziness'),
      api.resetPolicy('user_preferences'),
      api.resetPolicy('hard_gates'),
    ])
    await loadAll()
    showToast('已恢复默认设置')
  }

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">规则与规矩</h1>
          <p className="text-[13.5px] text-ink-3 mt-1">
            管理 AI 踩坑总结出来的避坑规则，以及你为 AI 定好的基本工作规矩。
          </p>
        </div>
        <div className="flex items-center gap-2">
          {notice && (
            <span className="px-3 py-1 rounded-full bg-ok-soft text-ok-ink text-[12px] border border-ok/25">
              {notice}
            </span>
          )}
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
                onClick={resetAllPolicies}
                className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-3 hover:text-ink"
              >
                <RotateCcw className="w-3.5 h-3.5" />
                恢复默认
              </button>
              <button
                type="button"
                onClick={savePolicies}
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

      {/* Main 3 clean tabs */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PillTabs
          items={[
            { key: 'rules', label: '避坑规则', count: rules.length },
            { key: 'guidelines', label: '我的规矩' },
            { key: 'clues', label: '错题观察池', count: observingPool.length + openClues.length },
          ]}
          active={tab}
          onChange={(k) => setTab(k as 'rules' | 'guidelines' | 'clues')}
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

      {/* Tab 1: Lessons & Rules */}
      {tab === 'rules' && (
        <>
          {showCreate && (
            <form
              onSubmit={submitRule}
              className="card p-5 grid grid-cols-1 md:grid-cols-2 gap-3 text-[12.5px]"
            >
              <input
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="一句话规则（例如：发布前先核对环境配置）"
                className="field-input px-3 py-2"
              />
              <input
                value={triggers}
                onChange={(e) => setTriggers(e.target.value)}
                placeholder="遇到哪些词时提醒（逗号分隔，例如：发布, 构建, 部署）"
                className="field-input px-3 py-2"
              />
              <textarea
                rows={2}
                value={checklist}
                onChange={(e) => setChecklist(e.target.value)}
                placeholder="要做什么（每行一条）"
                className="field-input px-3 py-2"
              />
              <textarea
                rows={2}
                value={forbidden}
                onChange={(e) => setForbidden(e.target.value)}
                placeholder="不要做什么（每行一条，选填）"
                className="field-input px-3 py-2"
              />
              <div className="md:col-span-2 flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowCreate(false)}
                  className="h-8 px-3 rounded-[var(--radius-control)] border border-line text-ink-3"
                >
                  取消
                </button>
                <button
                  type="submit"
                  className="h-8 px-4 rounded-[var(--radius-control)] bg-accent text-accent-contrast font-medium"
                >
                  保存规则
                </button>
              </div>
            </form>
          )}

          <section className="space-y-3">
            {filteredRules.map((rule) => (
              <article key={rule.rule_id} className="card p-5">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="flex items-center gap-2 flex-wrap">
                      <h2 className="text-[15px] font-semibold text-ink">{rule.title}</h2>
                      <StatusBadge status={rule.status} />
                    </div>
                    {rule.triggers.length > 0 && (
                      <div className="flex flex-wrap items-center gap-1.5 mt-2">
                        <span className="text-[11.5px] text-ink-4">触发词：</span>
                        {rule.triggers.map((t) => (
                          <span
                            key={t}
                            className="text-[11.5px] px-2 py-0.5 rounded-[var(--radius-mark)] bg-bg-sunk text-ink-2 border border-line-soft"
                          >
                            {t}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="text-[12px] text-ink-4 num">
                    已提醒 {rule.hit_count} 次 · 实测有效 {rule.natural_pass_count} 次
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3.5 text-[12.5px]">
                  <div className="bg-bg-sunk/70 border border-line-soft rounded-[var(--radius-control)] p-3">
                    <div className="text-[12px] font-semibold text-ink-3 mb-1">要做什么</div>
                    <ul className="list-disc pl-4 space-y-0.5 text-ink-2">
                      {rule.checklist.map((c, i) => (
                        <li key={i}>{c}</li>
                      ))}
                    </ul>
                  </div>
                  <div className="bg-hot-soft/40 border border-hot/15 rounded-[var(--radius-control)] p-3">
                    <div className="text-[12px] font-semibold text-hot mb-1">不要做什么</div>
                    <ul className="list-disc pl-4 space-y-0.5 text-ink-2">
                      {rule.forbidden_routes.length > 0 ? (
                        rule.forbidden_routes.map((f, i) => <li key={i}>{f}</li>)
                      ) : (
                        <li className="text-ink-4">无特别禁忌</li>
                      )}
                    </ul>
                  </div>
                </div>

                {/* Open dissents / counter-evidence attached to this rule */}
                {(dissentsByRule[rule.rule_id]?.length || 0) > 0 && (
                  <div className="mt-3 p-3 rounded-[var(--radius-control)] bg-amber-soft/55 border border-amber/30 text-[12px] space-y-2">
                    <div className="flex items-center gap-1.5 font-semibold text-amber-ink">
                      <AlertTriangle className="w-3.5 h-3.5" />
                      已知分歧 / 例外场景（保留供下次复核，不直接覆盖原规则）
                    </div>
                    {dissentsByRule[rule.rule_id].map((d) => (
                      <div
                        key={d.dissent_id}
                        className="flex flex-wrap items-start justify-between gap-2 pt-1.5 border-t border-amber/20"
                      >
                        <div className="min-w-0 flex-1 text-ink-2">
                          <span>{d.counter_claim}</span>
                          {d.condition_scope && (
                            <span className="ml-2 text-[11.5px] text-ink-4">
                              （例外条件：{d.condition_scope}）
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-1 shrink-0">
                          <button
                            type="button"
                            onClick={() => handleDissentStatus(d.dissent_id, 'merged')}
                            className="h-6 px-2 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[11px] font-medium"
                          >
                            已吸收
                          </button>
                          <button
                            type="button"
                            onClick={() => handleDissentStatus(d.dissent_id, 'dismissed')}
                            className="h-6 px-2 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[11px]"
                          >
                            忽略
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {dissentRuleId === rule.rule_id && (
                  <div className="mt-3 p-3 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line space-y-2 text-[12px]">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      <input
                        value={dissentClaim}
                        onChange={(e) => setDissentClaim(e.target.value)}
                        placeholder="写下这条规则什么时候不适用、或遇到什么反例"
                        className="field-input px-2.5 py-1.5"
                      />
                      <input
                        value={dissentScope}
                        onChange={(e) => setDissentScope(e.target.value)}
                        placeholder="例外条件（选填，例如：只读查询或本地脚本）"
                        className="field-input px-2.5 py-1.5"
                      />
                    </div>
                    <div className="flex justify-end gap-1.5">
                      <button
                        type="button"
                        onClick={() => {
                          setDissentRuleId(null)
                          setDissentClaim('')
                          setDissentScope('')
                        }}
                        className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3"
                      >
                        取消
                      </button>
                      <button
                        type="button"
                        onClick={() => handleAddDissent(rule.rule_id)}
                        className="h-7 px-3 rounded-[var(--radius-mark)] bg-accent text-accent-contrast font-medium"
                      >
                        保存分歧
                      </button>
                    </div>
                  </div>
                )}

                <div className="mt-3 pt-3 border-t border-line-soft flex flex-wrap items-center justify-between gap-2 text-[12px]">
                  <span className="text-[11.5px] text-ink-4 truncate max-w-[55%]">
                    来源：{rule.source_pointers.join('、') || '系统内置'}
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    <button
                      type="button"
                      onClick={() =>
                        setDissentRuleId((cur) => (cur === rule.rule_id ? null : rule.rule_id))
                      }
                      className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 hover:text-ink"
                    >
                      + 记例外
                    </button>
                    {(rule.status === 'needs_revision' || rule.status === 'dormant') && (
                      <button
                        type="button"
                        onClick={() => handleTransition(rule.rule_id, 'unverified_active')}
                        className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink font-medium"
                      >
                        <RefreshCcw className="w-3 h-3" />
                        {rule.status === 'dormant' ? '唤醒启用' : '重新启用'}
                      </button>
                    )}
                    {rule.status === 'unverified_active' && (
                      <button
                        type="button"
                        onClick={() => handleTransition(rule.rule_id, 'verified_active')}
                        className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink font-medium"
                      >
                        <CheckCircle2 className="w-3 h-3" />
                        设为已生效
                      </button>
                    )}
                    {rule.status !== 'retired' && (
                      <button
                        type="button"
                        onClick={() => handleTransition(rule.rule_id, 'retired')}
                        className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 hover:text-ink"
                      >
                        停用
                      </button>
                    )}
                  </div>
                </div>
              </article>
            ))}
          </section>
        </>
      )}

      {/* Tab 2: My Guidelines (Merged from Policy Studio) */}
      {tab === 'guidelines' && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 text-[12.5px]">
          <section className="card p-5 space-y-3.5">
            <div className="flex items-center justify-between pb-2.5 border-b border-line-soft">
              <div>
                <h2 className="text-[14.5px] font-bold text-ink">AI 干活的基本要求</h2>
                <span className="text-[11.5px] text-ink-4">
                  当前来源：{labelOf(POLICY_SOURCE_LABELS, policies.anti_laziness?._source, '系统默认')}
                </span>
              </div>
              <label className="inline-flex items-center gap-2 cursor-pointer text-ink-2">
                <input
                  type="checkbox"
                  checked={antiEnabled}
                  onChange={(e) => setAntiEnabled(e.target.checked)}
                  className="accent-accent"
                />
                <span>开启</span>
              </label>
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">每次对话都要遵守（每条一行）</label>
              <textarea
                rows={4}
                value={baselineText}
                onChange={(e) => setBaselineText(e.target.value)}
                className="field-input w-full p-2.5 text-[12.5px] leading-relaxed"
              />
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">写代码、排障、部署时额外遵守（每条一行）</label>
              <textarea
                rows={7}
                value={execText}
                onChange={(e) => setExecText(e.target.value)}
                className="field-input w-full p-2.5 text-[12.5px] leading-relaxed"
              />
            </div>
          </section>

          <section className="card p-5 space-y-3.5">
            <div className="pb-2.5 border-b border-line-soft">
              <h2 className="text-[14.5px] font-bold text-ink">个人称呼与红线</h2>
              <span className="text-[11.5px] text-ink-4">
                当前来源：{labelOf(POLICY_SOURCE_LABELS, policies.user_preferences?._source, '系统默认')}
              </span>
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">AI 怎么称呼你</label>
              <input
                value={addressAs}
                onChange={(e) => setAddressAs(e.target.value)}
                className="field-input w-full px-3 py-2 text-[13px]"
              />
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">绝对不能碰的红线（每条一行）</label>
              <textarea
                rows={8}
                value={redlinesText}
                onChange={(e) => setRedlinesText(e.target.value)}
                className="field-input w-full p-2.5 text-[12.5px] leading-relaxed"
              />
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">
                同一个错最多重试几次后停下提醒
              </label>
              <input
                type="number"
                min={1}
                max={5}
                value={retryMax}
                onChange={(e) => setRetryMax(Number(e.target.value))}
                className="field-input num w-24 px-3 py-1.5"
              />
            </div>
          </section>
        </div>
      )}

      {/* Tab 3: Mistake Observation Pool ("事不过三" + Red-Silent fast track) + Live Clues */}
      {tab === 'clues' && (
        <div className="space-y-5">
          <section className="card p-5">
            <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
              <div>
                <h2 className="text-[15px] font-bold text-ink">错题观察池（事不过三）</h2>
                <p className="text-[12px] text-ink-4 mt-0.5">
                  普通失误先在这里按根因聚类，累计出现 3 次自动晋升规则（防止规则膨胀）；高危沉默错误（🔴）可首次直通试运行。
                </p>
              </div>
              <span className="chip num">共 {observations.length} 条记录</span>
            </div>

            {observations.length === 0 ? (
              <p className="text-[13px] text-ink-4 py-8 text-center">
                观察池目前为空。每天 20:00 跑完沉淀批次后，AI 遇到的各类踩坑会自动归类到这里。
              </p>
            ) : (
              <div className="divide-y divide-line-soft mt-1">
                {observations.map((obs) => (
                  <div
                    key={obs.obs_id}
                    className="py-3.5 flex flex-wrap items-start justify-between gap-3"
                  >
                    <div className="space-y-1.5 min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <StatusBadge status={obs.severity_grade} />
                        <StatusBadge status={obs.status} />
                        <span className="chip num font-medium">
                          复现 {obs.occurrence_count}/3
                        </span>
                        <span className="mono text-[11.5px] text-ink-4">{obs.root_cause_key}</span>
                      </div>
                      <div className="text-[14px] font-semibold text-ink">{obs.title}</div>
                      {obs.thinking_root_cause && (
                        <p className="text-[12.5px] text-ink-2">
                          <span className="text-ink-4">思维根因：</span>
                          {obs.thinking_root_cause}
                        </p>
                      )}
                      {obs.truth && (
                        <p className="text-[12.5px] text-ink-3">
                          <span className="text-ink-4">正确做法：</span>
                          {obs.truth}
                        </p>
                      )}
                    </div>
                    {obs.status === 'observing' && (
                      <div className="flex items-center gap-1.5 shrink-0">
                        <button
                          type="button"
                          onClick={() => handlePromoteObservation(obs.obs_id)}
                          className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[12px] font-medium"
                        >
                          <ArrowUpRight className="w-3.5 h-3.5" />
                          立即升级为规则
                        </button>
                        <button
                          type="button"
                          onClick={() => handleArchiveObservation(obs.obs_id)}
                          className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px]"
                        >
                          归档
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card p-5">
            <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
              <h2 className="text-[14.5px] font-bold text-ink">实时异常线索</h2>
              <span className="text-[12px] text-ink-4">
                运行中捕获的卡死重试、规则复发等即时信号，会在每日批次中一并复盘。
              </span>
            </div>
            {retros.length === 0 ? (
              <p className="text-[13px] text-ink-4 py-6 text-center">暂无实时异常线索。</p>
            ) : (
              <div className="divide-y divide-line-soft mt-1">
                {retros.map((item) => (
                  <div
                    key={item.retro_id}
                    className="py-3.5 flex flex-wrap items-start justify-between gap-3"
                  >
                    <div className="space-y-1.5 min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <StatusBadge status={item.status} />
                        <span className="chip">{labelOf(RETRO_KIND_LABELS, item.kind)}</span>
                        <span className="chip">{labelOf(CLIENT_LABELS, item.client)}</span>
                      </div>
                      <p className="text-[13.5px] leading-[1.65] text-ink">{item.summary}</p>
                    </div>
                    {(item.status === 'open' || item.status === 'in_review') && (
                      <div className="flex items-center gap-1.5 shrink-0">
                        <button
                          type="button"
                          onClick={() => handleRetroStatus(item.retro_id, 'resolved')}
                          className="h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[12px] font-medium"
                        >
                          已处理
                        </button>
                        <button
                          type="button"
                          onClick={() => handleRetroStatus(item.retro_id, 'dismissed')}
                          className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px]"
                        >
                          忽略
                        </button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
