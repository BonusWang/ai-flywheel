import React, { useEffect, useState } from 'react'
import { CheckCircle2, Plus, RefreshCcw, RotateCcw, Save } from 'lucide-react'
import { api } from '../api'
import { PillTabs } from '../components/PageLayout'
import { StatusBadge } from '../components/StatusBadge'
import { CLIENT_LABELS, labelOf, POLICY_SOURCE_LABELS, RETRO_KIND_LABELS } from '../lib/labels'
import type { RetroItem, RuleRecord } from '../types'

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
  const [retros, setRetros] = useState<RetroItem[]>([])
  const [filter, setFilter] = useState<string>('all')
  const [showCreate, setShowCreate] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

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
    const [rRes, qRes, pRes] = await Promise.all([
      api.listRules(),
      api.listRetro(),
      api.listPolicies(),
    ])
    setRules(rRes.rules)
    setRetros(qRes.items)
    setPolicies(pRes.policies)

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
  const filteredRules = filter === 'all' ? rules : rules.filter((r) => r.status === filter)

  const handleTransition = async (ruleId: string, status: RuleRecord['status']) => {
    await api.transitionRule(ruleId, status, '人工调整规则状态')
    await loadAll()
    onGlobalRefresh()
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

      {/* Main 3 clean tabs */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PillTabs
          items={[
            { key: 'rules', label: '避坑规则', count: rules.length },
            { key: 'guidelines', label: '我的规矩' },
            { key: 'clues', label: '问题线索', count: openClues.length },
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

                <div className="mt-3 pt-3 border-t border-line-soft flex flex-wrap items-center justify-between gap-2 text-[12px]">
                  <span className="text-[11.5px] text-ink-4 truncate max-w-[65%]">
                    来源：{rule.source_pointers.join('、') || '系统内置'}
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    {rule.status === 'needs_revision' && (
                      <button
                        type="button"
                        onClick={() => handleTransition(rule.rule_id, 'unverified_active')}
                        className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink font-medium"
                      >
                        <RefreshCcw className="w-3 h-3" />
                        重新启用
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

      {/* Tab 3: Problem Clues (Quiet input material for daily batches) */}
      {tab === 'clues' && (
        <section className="card p-5">
          <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
            <h2 className="text-[15px] font-bold text-ink">收集到的问题线索</h2>
            <span className="text-[12px] text-ink-4">
              这些线索会在每天 20:00 的批次里自动一起回顾，也可以手动标记处理。
            </span>
          </div>
          {retros.length === 0 ? (
            <p className="text-[13px] text-ink-4 py-8 text-center">暂无问题线索。</p>
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
      )}
    </div>
  )
}
