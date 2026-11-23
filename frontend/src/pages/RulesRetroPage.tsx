import React, { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Plus, RefreshCcw } from 'lucide-react'
import { api } from '../api'
import { PillTabs } from '../components/PageLayout'
import { StatusBadge } from '../components/StatusBadge'
import type { RetroItem, RuleRecord } from '../types'

interface Props {
  refreshToken: number
  onGlobalRefresh: () => void
}

export const RulesRetroPage: React.FC<Props> = ({ refreshToken, onGlobalRefresh }) => {
  const [rules, setRules] = useState<RuleRecord[]>([])
  const [retros, setRetros] = useState<RetroItem[]>([])
  const [filter, setFilter] = useState<string>('all')
  const [showCreate, setShowCreate] = useState(false)

  const [title, setTitle] = useState('')
  const [triggers, setTriggers] = useState('')
  const [checklist, setChecklist] = useState('')
  const [forbidden, setForbidden] = useState('')

  const loadAll = async () => {
    const [rRes, qRes] = await Promise.all([api.listRules(), api.listRetro()])
    setRules(rRes.rules)
    setRetros(qRes.items)
  }

  useEffect(() => {
    loadAll().catch(() => {})
  }, [refreshToken])

  const filteredRules = filter === 'all' ? rules : rules.filter((r) => r.status === filter)

  const tabItems = [
    { key: 'all', label: '全部规则', count: rules.length },
    {
      key: 'verified_active',
      label: '已验证生效',
      count: rules.filter((r) => r.status === 'verified_active').length,
    },
    {
      key: 'unverified_active',
      label: '待自然验证',
      count: rules.filter((r) => r.status === 'unverified_active').length,
    },
    {
      key: 'needs_revision',
      label: '复发待修订',
      count: rules.filter((r) => r.status === 'needs_revision').length,
    },
    {
      key: 'retired',
      label: '已退役',
      count: rules.filter((r) => r.status === 'retired').length,
    },
  ]

  const handleTransition = async (ruleId: string, status: RuleRecord['status']) => {
    await api.transitionRule(ruleId, status, 'Web 控制台人工复核流转')
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
  }

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">规则与复盘台账</h1>
          <p className="text-[13px] text-ink-3 mt-1">
            两级激活生命周期（<code className="mono text-[12px]">unverified_active → verified_active / needs_revision</code>）与异常复盘队列
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate((v) => !v)}
          className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink"
        >
          <Plus className="w-3.5 h-3.5" />
          新建活跃规则
        </button>
      </header>

      <div className="flex items-center justify-between flex-wrap gap-3">
        <PillTabs items={tabItems} active={filter} onChange={setFilter} />
      </div>

      {showCreate && (
        <form
          onSubmit={submitRule}
          className="card p-5 grid grid-cols-1 md:grid-cols-2 gap-3 text-[12.5px]"
        >
          <input
            required
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="规则标题（如：部署前必须核对构建产物环境指向）"
            className="field-input px-3 py-2"
          />
          <input
            value={triggers}
            onChange={(e) => setTriggers(e.target.value)}
            placeholder="触发关键词（逗号分隔，如：deploy, 构建, dist）"
            className="field-input px-3 py-2"
          />
          <textarea
            rows={2}
            value={checklist}
            onChange={(e) => setChecklist(e.target.value)}
            placeholder="必查清单（每行一条）"
            className="field-input px-3 py-2"
          />
          <textarea
            rows={2}
            value={forbidden}
            onChange={(e) => setForbidden(e.target.value)}
            placeholder="禁用路线（每行一条）"
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
              注册为 unverified_active
            </button>
          </div>
        </form>
      )}

      {/* Rules list */}
      <section className="space-y-3">
        {filteredRules.map((rule) => (
          <article key={rule.rule_id} className="card p-5">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="flex items-center gap-2 flex-wrap">
                  <h2 className="text-[15px] font-semibold text-ink">{rule.title}</h2>
                  <StatusBadge status={rule.status} />
                  <span className="mono text-[11.5px] text-ink-4">
                    {rule.rule_id} · v{rule.version}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {rule.triggers.map((t) => (
                    <span
                      key={t}
                      className="mono text-[11px] px-2 py-0.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink"
                    >
                      #{t}
                    </span>
                  ))}
                </div>
              </div>
              <div className="flex items-center gap-3 mono text-[11.5px] text-ink-4">
                <span>命中 {rule.hit_count}</span>
                <span className="text-ok-ink">自然通过 {rule.natural_pass_count}</span>
                <span className="text-hot">复发 {rule.recurrence_count}</span>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3.5 text-[12.5px]">
              <div className="bg-bg-sunk/70 border border-line-soft rounded-[var(--radius-control)] p-3">
                <div className="text-[12px] font-semibold text-ink-3 mb-1">必查清单</div>
                <ul className="list-disc pl-4 space-y-0.5 text-ink-2">
                  {rule.checklist.map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </div>
              <div className="bg-hot-soft/45 border border-hot/15 rounded-[var(--radius-control)] p-3">
                <div className="text-[12px] font-semibold text-hot mb-1">禁用路线</div>
                <ul className="list-disc pl-4 space-y-0.5 text-ink-2">
                  {rule.forbidden_routes.length > 0 ? (
                    rule.forbidden_routes.map((f, i) => <li key={i}>{f}</li>)
                  ) : (
                    <li className="text-ink-4">暂无显式禁用路线</li>
                  )}
                </ul>
              </div>
            </div>

            <div className="mt-3 pt-3 border-t border-line-soft flex flex-wrap items-center justify-between gap-2 text-[12px]">
              <span className="mono text-[11.5px] text-ink-4">
                来源指针：{rule.source_pointers.join(', ') || '飞轮内置'}
              </span>
              <div className="flex flex-wrap gap-1.5">
                {rule.status === 'needs_revision' && (
                  <button
                    type="button"
                    onClick={() => handleTransition(rule.rule_id, 'unverified_active')}
                    className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink hover:bg-accent/15 font-medium"
                  >
                    <RefreshCcw className="w-3 h-3" />
                    修订重新上线
                  </button>
                )}
                {rule.status === 'unverified_active' && (
                  <button
                    type="button"
                    onClick={() => handleTransition(rule.rule_id, 'verified_active')}
                    className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink hover:opacity-85 font-medium"
                  >
                    <CheckCircle2 className="w-3 h-3" />
                    标记已验证
                  </button>
                )}
                {rule.status !== 'retired' && (
                  <button
                    type="button"
                    onClick={() => handleTransition(rule.rule_id, 'retired')}
                    className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 hover:text-ink"
                  >
                    退役
                  </button>
                )}
              </div>
            </div>
          </article>
        ))}
      </section>

      {/* Retrospective Queue */}
      <section className="card p-5">
        <div className="flex items-center gap-2 pb-3 border-b border-line-soft">
          <AlertTriangle className="w-4 h-4 text-amber-ink" />
          <h2 className="text-[15px] font-bold text-ink">
            异常复盘队列（Retrospective Queue · <span className="num">{retros.length}</span>）
          </h2>
        </div>
        {retros.length === 0 ? (
          <p className="text-[13px] text-ink-4 py-4">暂无异常信号，飞轮运行平稳。</p>
        ) : (
          <div className="divide-y divide-line-soft mt-1">
            {retros.map((item) => (
              <div key={item.retro_id} className="py-3 flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-1 min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <StatusBadge status={item.status} />
                    <span className="chip mono">{item.kind}</span>
                    <span className="mono text-[11.5px] text-ink-4">
                      {item.client} · {item.session_key}
                    </span>
                  </div>
                  <p className="text-[13px] text-ink-2">{item.summary}</p>
                </div>
                {item.status === 'open' && (
                  <div className="flex items-center gap-1.5 shrink-0">
                    <button
                      type="button"
                      onClick={() => handleRetroStatus(item.retro_id, 'resolved')}
                      className="h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[12px] font-medium"
                    >
                      标记已闭环
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
  )
}
