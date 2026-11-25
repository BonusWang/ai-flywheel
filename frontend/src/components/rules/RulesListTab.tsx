import React, { useState } from 'react'
import { AlertTriangle, CheckCircle2, RefreshCcw } from 'lucide-react'
import { api } from '../../api'
import { StatusBadge } from '../StatusBadge'
import type { DissentRecord, RuleRecord } from '../../types'

/**
 * The "避坑规则" tab: the create-rule form, the rule cards, their status
 * transitions and the dissent / exception records attached to each rule.
 *
 * The filter and the "新增一条规则" button live in the page header; this tab
 * receives the already-filtered rules and whether the create form is open.
 */
export const RulesListTab: React.FC<{
  rules: RuleRecord[]
  dissentsByRule: Record<string, DissentRecord[]>
  showCreate: boolean
  onCloseCreate: () => void
  onChanged: () => Promise<void>
  onToast: (msg: string) => void
}> = ({ rules, dissentsByRule, showCreate, onCloseCreate, onChanged, onToast }) => {
  const [title, setTitle] = useState('')
  const [triggers, setTriggers] = useState('')
  const [checklist, setChecklist] = useState('')
  const [forbidden, setForbidden] = useState('')

  const [dissentRuleId, setDissentRuleId] = useState<string | null>(null)
  const [dissentClaim, setDissentClaim] = useState('')
  const [dissentScope, setDissentScope] = useState('')

  const handleTransition = async (ruleId: string, status: RuleRecord['status']) => {
    await api.transitionRule(ruleId, status, '人工调整规则状态')
    await onChanged()
    if (status === 'unverified_active') {
      onToast('规则已唤醒并重新进入试运行')
    }
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
    await onChanged()
    onToast('已记下例外/分歧，下次命中规则时会一并提醒')
  }

  const handleDissentStatus = async (dissentId: string, status: 'merged' | 'dismissed') => {
    await api.updateDissentStatus(dissentId, status)
    await onChanged()
    onToast(status === 'merged' ? '分歧已标记为吸收' : '分歧已忽略')
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
    onCloseCreate()
    await onChanged()
    onToast('已新增一条试运行规则')
  }

  return (
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
              onClick={onCloseCreate}
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
        {rules.map((rule) => (
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
                {(rule.supersedes_rule_id || rule.effective_from) && (
                  <div className="flex flex-wrap items-center gap-1.5 mt-2 text-[11.5px] text-ink-4">
                    <span>规则沿革：</span>
                    {rule.supersedes_rule_id && (
                      <span className="px-2 py-0.5 rounded-[var(--radius-mark)] bg-bg-sunk text-ink-2 border border-line-soft">
                        替代 {rule.supersedes_rule_id}
                      </span>
                    )}
                    {rule.effective_from && (
                      <span className="px-2 py-0.5 rounded-[var(--radius-mark)] bg-bg-sunk text-ink-2 border border-line-soft num">
                        生效期 {rule.effective_from.slice(0, 10)} ~{' '}
                        {rule.effective_to ? rule.effective_to.slice(0, 10) : '现行'}
                      </span>
                    )}
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
  )
}
