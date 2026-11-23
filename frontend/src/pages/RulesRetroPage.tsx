import React, { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Plus, RefreshCcw, Shield } from 'lucide-react'
import { api } from '../api'
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

  const counts = {
    all: rules.length,
    verified_active: rules.filter((r) => r.status === 'verified_active').length,
    unverified_active: rules.filter((r) => r.status === 'unverified_active').length,
    needs_revision: rules.filter((r) => r.status === 'needs_revision').length,
    retired: rules.filter((r) => r.status === 'retired').length,
  }

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
    <div className="space-y-6">
      {/* Two-Tier Lifecycle Funnel */}
      <section className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <div className="flex items-center gap-2">
            <Shield className="w-4 h-4 text-blue-600" />
            <div>
              <h2 className="text-sm font-semibold text-slate-900">两级规则激活生命周期看板</h2>
              <p className="text-xs text-slate-500">
                新沉淀以 <code className="text-slate-700">unverified_active</code> 上线 → 自然任务真实验证通过晋升 <code className="text-slate-700">verified_active</code> → 复发降级 <code className="text-slate-700">needs_revision</code>
              </p>
            </div>
          </div>
          <button
            onClick={() => setShowCreate((v) => !v)}
            className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-xl bg-slate-900 text-white text-xs font-medium hover:bg-slate-800 cursor-pointer"
          >
            <Plus className="w-3.5 h-3.5" />
            新建活跃规则
          </button>
        </div>

        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2.5">
          {(
            [
              ['all', '全部规则', counts.all],
              ['verified_active', '已验证生效', counts.verified_active],
              ['unverified_active', '待自然验证', counts.unverified_active],
              ['needs_revision', '复发待修订', counts.needs_revision],
              ['retired', '已退役归档', counts.retired],
            ] as const
          ).map(([key, label, num]) => (
            <button
              key={key}
              onClick={() => setFilter(key)}
              className={`text-left p-3 rounded-xl border transition-all cursor-pointer ${
                filter === key
                  ? 'border-slate-900 bg-slate-900 text-white'
                  : 'border-slate-200 bg-slate-50 text-slate-800 hover:border-slate-300'
              }`}
            >
              <div className={`text-[11px] ${filter === key ? 'text-slate-300' : 'text-slate-500'}`}>
                {label}
              </div>
              <div className="text-xl font-bold font-mono mt-0.5">{num}</div>
            </button>
          ))}
        </div>

        {showCreate && (
          <form onSubmit={submitRule} className="mt-4 pt-4 border-t border-slate-200 grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
            <input
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="规则标题（如：部署前必须核对构建产物环境指向）"
              className="px-3 py-2 rounded-xl border border-slate-200"
            />
            <input
              value={triggers}
              onChange={(e) => setTriggers(e.target.value)}
              placeholder="触发关键词（逗号分隔，如：deploy, 构建, dist）"
              className="px-3 py-2 rounded-xl border border-slate-200"
            />
            <textarea
              rows={2}
              value={checklist}
              onChange={(e) => setChecklist(e.target.value)}
              placeholder="必查清单（每行一条）"
              className="px-3 py-2 rounded-xl border border-slate-200"
            />
            <textarea
              rows={2}
              value={forbidden}
              onChange={(e) => setForbidden(e.target.value)}
              placeholder="禁用路线（每行一条）"
              className="px-3 py-2 rounded-xl border border-slate-200"
            />
            <div className="md:col-span-2 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowCreate(false)}
                className="px-3 py-1.5 rounded-lg border border-slate-200 text-slate-600 cursor-pointer"
              >
                取消
              </button>
              <button
                type="submit"
                className="px-4 py-1.5 rounded-lg bg-blue-600 text-white font-medium cursor-pointer"
              >
                注册为 unverified_active
              </button>
            </div>
          </form>
        )}

        {/* Rule Cards */}
        <div className="mt-4 space-y-3">
          {filteredRules.map((rule) => (
            <div
              key={rule.rule_id}
              className="border border-slate-200 rounded-xl p-4 hover:border-slate-300 transition-colors"
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm font-semibold text-slate-900">{rule.title}</span>
                    <StatusBadge status={rule.status} />
                    <span className="text-[11px] font-mono text-slate-400">
                      {rule.rule_id} · v{rule.version}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-1.5 mt-2">
                    {rule.triggers.map((t) => (
                      <span
                        key={t}
                        className="text-[11px] px-2 py-0.5 rounded-md bg-blue-50 text-blue-700 font-mono"
                      >
                        #{t}
                      </span>
                    ))}
                  </div>
                </div>
                <div className="flex items-center gap-3 text-[11px] font-mono text-slate-500">
                  <span>命中 {rule.hit_count}</span>
                  <span className="text-emerald-700">自然通过 {rule.natural_pass_count}</span>
                  <span className="text-rose-700">复发 {rule.recurrence_count}</span>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3 text-xs">
                <div className="bg-slate-50 rounded-lg p-2.5">
                  <div className="font-semibold text-slate-700 mb-1">必查清单</div>
                  <ul className="list-disc pl-4 space-y-0.5 text-slate-600">
                    {rule.checklist.map((c, i) => (
                      <li key={i}>{c}</li>
                    ))}
                  </ul>
                </div>
                <div className="bg-rose-50/50 rounded-lg p-2.5">
                  <div className="font-semibold text-rose-800 mb-1">禁用路线</div>
                  <ul className="list-disc pl-4 space-y-0.5 text-rose-700">
                    {rule.forbidden_routes.length > 0 ? (
                      rule.forbidden_routes.map((f, i) => <li key={i}>{f}</li>)
                    ) : (
                      <li className="text-slate-400">暂无显式禁用路线</li>
                    )}
                  </ul>
                </div>
              </div>

              <div className="mt-3 pt-2.5 border-t border-slate-100 flex flex-wrap items-center justify-between gap-2 text-xs">
                <div className="text-[11px] text-slate-400 font-mono">
                  来源指针：{rule.source_pointers.join(', ') || '飞轮内置'}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {rule.status === 'needs_revision' && (
                    <button
                      onClick={() => handleTransition(rule.rule_id, 'unverified_active')}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-blue-50 text-blue-700 hover:bg-blue-100 font-medium cursor-pointer"
                    >
                      <RefreshCcw className="w-3 h-3" />
                      修订重新上线
                    </button>
                  )}
                  {rule.status === 'unverified_active' && (
                    <button
                      onClick={() => handleTransition(rule.rule_id, 'verified_active')}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-emerald-50 text-emerald-700 hover:bg-emerald-100 font-medium cursor-pointer"
                    >
                      <CheckCircle2 className="w-3 h-3" />
                      标记已验证
                    </button>
                  )}
                  {rule.status !== 'retired' && (
                    <button
                      onClick={() => handleTransition(rule.rule_id, 'retired')}
                      className="px-2.5 py-1 rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200 cursor-pointer"
                    >
                      退役
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Anomaly Retro Queue */}
      <section className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs">
        <div className="flex items-center gap-2 mb-3">
          <AlertTriangle className="w-4 h-4 text-amber-600" />
          <h2 className="text-sm font-semibold text-slate-900">异常复盘队列（Retrospective Queue · {retros.length}）</h2>
        </div>
        {retros.length === 0 ? (
          <p className="text-xs text-slate-500 py-4">暂无异常信号，飞轮运行平稳。</p>
        ) : (
          <div className="divide-y divide-slate-100">
            {retros.map((item) => (
              <div key={item.retro_id} className="py-3 flex flex-wrap items-start justify-between gap-3">
                <div className="space-y-1 min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <StatusBadge status={item.status} />
                    <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-slate-100 text-slate-700">
                      {item.kind}
                    </span>
                    <span className="text-[11px] font-mono text-slate-400">
                      {item.client} · {item.session_key}
                    </span>
                  </div>
                  <p className="text-xs text-slate-800">{item.summary}</p>
                </div>
                {item.status === 'open' && (
                  <div className="flex items-center gap-1.5 shrink-0">
                    <button
                      onClick={() => handleRetroStatus(item.retro_id, 'resolved')}
                      className="px-2.5 py-1 rounded-lg bg-emerald-50 text-emerald-700 text-xs font-medium hover:bg-emerald-100 cursor-pointer"
                    >
                      标记已闭环
                    </button>
                    <button
                      onClick={() => handleRetroStatus(item.retro_id, 'dismissed')}
                      className="px-2.5 py-1 rounded-lg bg-slate-100 text-slate-600 text-xs hover:bg-slate-200 cursor-pointer"
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
