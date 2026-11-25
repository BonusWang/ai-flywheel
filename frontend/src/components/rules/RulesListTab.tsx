import React, { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, Loader2, RefreshCcw } from 'lucide-react'
import { api } from '../../api'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { StatusBadge } from '../StatusBadge'
import { Collapse } from '../ui/Collapse'
import { useToast } from '../ui/Toast'
import type { DissentRecord, RuleRecord } from '../../types'

export const RulesListTab: React.FC<{
  rules: RuleRecord[]
  dissentsByRule: Record<string, DissentRecord[]>
  showCreate: boolean
  onCloseCreate: () => void
  onChanged: () => Promise<void>
  onResetStatusFilter?: () => void
}> = ({
  rules,
  dissentsByRule,
  showCreate,
  onCloseCreate,
  onChanged,
  onResetStatusFilter,
}) => {
  const toast = useToast()
  const [title, setTitle] = useState('')
  const [triggers, setTriggers] = useState('')
  const [checklist, setChecklist] = useState('')
  const [forbidden, setForbidden] = useState('')
  const [creating, setCreating] = useState(false)

  const [dissentRuleId, setDissentRuleId] = useState<string | null>(null)
  const [dissentClaim, setDissentClaim] = useState('')
  const [dissentScope, setDissentScope] = useState('')
  const [ruleQuery, setRuleQuery] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)

  const createTitleRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!showCreate) return
    const timer = window.setTimeout(() => createTitleRef.current?.focus(), 140)
    return () => window.clearTimeout(timer)
  }, [showCreate])

  const visibleRules = useMemo(() => {
    const q = ruleQuery.trim().toLowerCase()
    return [...rules]
      .filter((r) =>
        !q
          ? true
          : `${r.title} ${r.domain} ${r.triggers.join(' ')} ${r.checklist.join(' ')}`
              .toLowerCase()
              .includes(q)
      )
      .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
  }, [rules, ruleQuery])

  const rulesPager = usePagination(visibleRules, 10)
  const hasFilters = Boolean(ruleQuery.trim()) || Boolean(onResetStatusFilter)

  const handleTransition = async (rule: RuleRecord, nextStatus: RuleRecord['status']) => {
    const prevStatus = rule.status
    setBusyId(rule.rule_id)
    try {
      await api.transitionRule(rule.rule_id, nextStatus, '人工调整规则状态')
      await onChanged()
      const msg =
        nextStatus === 'retired'
          ? `已停用规则：「${rule.title}」`
          : nextStatus === 'verified_active'
            ? `已设为正式生效：「${rule.title}」`
            : `已重新启用并进入试运行：「${rule.title}」`
      toast.push({
        message: msg,
        onUndo: async () => {
          await api.transitionRule(rule.rule_id, prevStatus, '撤销状态调整')
          await onChanged()
          toast.push('已撤销规则状态调整')
        },
      })
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '更新规则状态失败' })
    } finally {
      setBusyId(null)
    }
  }

  const handleAddDissent = async (ruleId: string) => {
    if (!dissentClaim.trim()) return
    setBusyId(ruleId)
    try {
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
      toast.push('已记下例外场景，下次命中规则时会一并提醒')
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '保存例外失败' })
    } finally {
      setBusyId(null)
    }
  }

  const handleDissentStatus = async (dissentId: string, status: 'merged' | 'dismissed') => {
    setBusyId(dissentId)
    try {
      await api.updateDissentStatus(dissentId, status)
      await onChanged()
      toast.push({
        message: status === 'merged' ? '已标记为纳入规则' : '已忽略该例外分歧',
        onUndo: async () => {
          await api.updateDissentStatus(dissentId, 'open')
          await onChanged()
          toast.push('已撤销，恢复为待处理分歧')
        },
      })
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '更新分歧状态失败' })
    } finally {
      setBusyId(null)
    }
  }

  const submitRule = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim() || creating) return
    setCreating(true)
    try {
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
      toast.push('已新增一条试运行避坑规则')
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '新增规则失败' })
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="space-y-4">
      <Collapse open={showCreate}>
        <form
          onSubmit={submitRule}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.stopPropagation()
              onCloseCreate()
            }
          }}
          className="card p-5 grid grid-cols-1 md:grid-cols-2 gap-3 text-[12.5px]"
        >
          <input
            ref={createTitleRef}
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
              className="pressable h-8 px-3 rounded-[var(--radius-control)] border border-line text-ink-3 hover:text-ink"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={creating || !title.trim()}
              className="pressable inline-flex items-center gap-1.5 h-8 px-4 rounded-[var(--radius-control)] bg-accent text-accent-contrast font-medium hover:bg-accent-ink disabled:opacity-50"
            >
              {creating && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              <span>保存规则</span>
            </button>
          </div>
        </form>
      </Collapse>

      {(rules.length > 0 || ruleQuery) && (
        <ListToolbar
          query={ruleQuery}
          onQueryChange={(q) => {
            setRuleQuery(q)
            rulesPager.resetPage()
          }}
          placeholder="搜索规则标题、触发词或检查项…"
          enableSlashShortcut
          hasActiveFilters={hasFilters}
          onResetFilters={() => {
            setRuleQuery('')
            onResetStatusFilter?.()
            rulesPager.resetPage()
          }}
        />
      )}

      <section className="space-y-3">
        {visibleRules.length === 0 ? (
          <div className="card p-10 text-center space-y-3">
            <p className="text-[13px] text-ink-4">没有符合条件的避坑规则。</p>
            {hasFilters && (
              <button
                type="button"
                onClick={() => {
                  setRuleQuery('')
                  onResetStatusFilter?.()
                }}
                className="pressable inline-flex items-center h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-2 hover:border-line-strong hover:text-ink"
              >
                查看全部规则
              </button>
            )}
          </div>
        ) : (
          rulesPager.pageItems.map((rule) => {
            const hasForbidden = rule.forbidden_routes.length > 0
            const isDissentOpen = dissentRuleId === rule.rule_id
            const canRevive =
              rule.status === 'needs_revision' ||
              rule.status === 'dormant' ||
              rule.status === 'retired'
            return (
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
                        <span>版本沿革：</span>
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

                {/* Only render the red "不要做什么" column when forbidden routes actually exist */}
                <div
                  className={`grid grid-cols-1 ${hasForbidden ? 'md:grid-cols-2' : ''} gap-3 mt-3.5 text-[12.5px]`}
                >
                  <div className="bg-bg-sunk/70 border border-line-soft rounded-[var(--radius-control)] p-3">
                    <div className="text-[12px] font-semibold text-ink-3 mb-1">要做什么</div>
                    <ul className="list-disc pl-4 space-y-0.5 text-ink-2">
                      {rule.checklist.length > 0 ? (
                        rule.checklist.map((c, i) => <li key={i}>{c}</li>)
                      ) : (
                        <li className="text-ink-4">遵守标题约定</li>
                      )}
                    </ul>
                  </div>
                  {hasForbidden && (
                    <div className="bg-hot-soft/40 border border-hot/15 rounded-[var(--radius-control)] p-3">
                      <div className="text-[12px] font-semibold text-hot mb-1">不要做什么</div>
                      <ul className="list-disc pl-4 space-y-0.5 text-ink-2">
                        {rule.forbidden_routes.map((f, i) => (
                          <li key={i}>{f}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>

                {/* Open dissents / exceptions attached to this rule */}
                {(dissentsByRule[rule.rule_id]?.length || 0) > 0 && (
                  <div className="mt-3 p-3 rounded-[var(--radius-control)] bg-amber-soft/55 border border-amber/30 text-[12px] space-y-2">
                    <div className="flex items-center gap-1.5 font-semibold text-amber-ink">
                      <AlertTriangle className="w-3.5 h-3.5" />
                      已知例外场景（保留供下次复核，不直接覆盖原规则）
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
                              （适用边界：{d.condition_scope}）
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-1 shrink-0">
                          <button
                            type="button"
                            disabled={busyId === d.dissent_id}
                            onClick={() => handleDissentStatus(d.dissent_id, 'merged')}
                            className="pressable h-6 px-2 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[11px] font-medium disabled:opacity-50"
                          >
                            已纳入规则
                          </button>
                          <button
                            type="button"
                            disabled={busyId === d.dissent_id}
                            onClick={() => handleDissentStatus(d.dissent_id, 'dismissed')}
                            className="pressable h-6 px-2 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[11px] hover:text-ink disabled:opacity-50"
                          >
                            忽略
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                <Collapse open={isDissentOpen} unmountOnExit>
                  <div
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') {
                        e.stopPropagation()
                        setDissentRuleId(null)
                      }
                    }}
                    className="mt-3 p-3 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line space-y-2 text-[12px]"
                  >
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                      <input
                        autoFocus
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
                        className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 hover:text-ink"
                      >
                        取消
                      </button>
                      <button
                        type="button"
                        disabled={!dissentClaim.trim() || busyId === rule.rule_id}
                        onClick={() => handleAddDissent(rule.rule_id)}
                        className="pressable h-7 px-3 rounded-[var(--radius-mark)] bg-accent text-accent-contrast font-medium disabled:opacity-50"
                      >
                        保存例外
                      </button>
                    </div>
                  </div>
                </Collapse>

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
                      className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 hover:border-line-strong hover:text-ink"
                    >
                      {isDissentOpen ? '收起例外' : '+ 记例外'}
                    </button>
                    {canRevive && (
                      <button
                        type="button"
                        disabled={busyId === rule.rule_id}
                        onClick={() => handleTransition(rule, 'unverified_active')}
                        className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink font-medium hover:bg-accent/18 disabled:opacity-50"
                      >
                        <RefreshCcw className="w-3 h-3" />
                        {rule.status === 'dormant' ? '唤醒启用' : '重新启用'}
                      </button>
                    )}
                    {rule.status === 'unverified_active' && (
                      <button
                        type="button"
                        disabled={busyId === rule.rule_id}
                        onClick={() => handleTransition(rule, 'verified_active')}
                        className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink font-medium hover:opacity-90 disabled:opacity-50"
                      >
                        <CheckCircle2 className="w-3 h-3" />
                        设为已生效
                      </button>
                    )}
                    {rule.status !== 'retired' && (
                      <button
                        type="button"
                        disabled={busyId === rule.rule_id}
                        onClick={() => handleTransition(rule, 'retired')}
                        className="pressable h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-4 hover:border-line-strong hover:text-ink disabled:opacity-50"
                      >
                        停用
                      </button>
                    )}
                  </div>
                </div>
              </article>
            )
          })
        )}
      </section>

      <PaginationBar
        total={rulesPager.total}
        page={rulesPager.page}
        pageSize={rulesPager.pageSize}
        totalPages={rulesPager.totalPages}
        onPageChange={rulesPager.setPage}
        onPageSizeChange={rulesPager.setPageSize}
      />
    </div>
  )
}
