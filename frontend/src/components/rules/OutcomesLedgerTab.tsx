import React, { useMemo, useState } from 'react'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { StatusBadge } from '../StatusBadge'
import { CLIENT_LABELS, formatDateYMD, labelOf } from '../../lib/labels'
import type { OutcomeRecord } from '../../types'

const CHECK_KIND_LABELS: Record<string, string> = {
  real: '实测',
  test: '单测',
  static: '静态',
}

const CHECK_RESULT_LABELS: Record<string, string> = {
  pass: '通过',
  fail: '失败',
  skip: '跳过',
}

export const OutcomesLedgerTab: React.FC<{ outcomes: OutcomeRecord[] }> = ({ outcomes }) => {
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [clientFilter, setClientFilter] = useState('all')

  const clients = useMemo(
    () => Array.from(new Set(outcomes.map((o) => o.client).filter(Boolean))),
    [outcomes]
  )

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    return outcomes
      .filter((o) => (statusFilter === 'all' ? true : o.status === statusFilter))
      .filter((o) => (clientFilter === 'all' ? true : o.client === clientFilter))
      .filter((o) =>
        !q
          ? true
          : `${o.summary} ${o.outcome_id} ${o.client} ${o.applied_rule_ids.join(' ')} ${o.used_skills.join(' ')}`
              .toLowerCase()
              .includes(q)
      )
      .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
  }, [outcomes, query, statusFilter, clientFilter])

  const pager = usePagination(filtered, 10)
  const hasFilters = Boolean(query.trim()) || statusFilter !== 'all' || clientFilter !== 'all'

  return (
    <section className="card p-5 space-y-3.5">
      <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
        <div>
          <h2 className="text-[15px] font-bold text-ink">完工验收记录</h2>
          <p className="text-[12px] text-ink-4 mt-0.5">
            每次任务交付时提交的实测/单测检查证据，用于验证避坑规则是否真实有效。
          </p>
        </div>
        <span className="chip num">共 {outcomes.length} 条</span>
      </div>

      {outcomes.length > 0 && (
        <ListToolbar
          query={query}
          onQueryChange={(q) => {
            setQuery(q)
            pager.resetPage()
          }}
          placeholder="搜索交付摘要、回执号、规则或 Skill…"
          enableSlashShortcut
          hasActiveFilters={hasFilters}
          onResetFilters={() => {
            setQuery('')
            setStatusFilter('all')
            setClientFilter('all')
            pager.resetPage()
          }}
        >
          <select
            aria-label="验收状态筛选"
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value)
              pager.resetPage()
            }}
            className="field-input h-8 px-2.5 text-[12px]"
          >
            <option value="all">全部状态</option>
            <option value="success">已通过</option>
            <option value="partial">部分完成</option>
            <option value="failed">未通过</option>
          </select>
          {clients.length > 1 && (
            <select
              aria-label="来源工具筛选"
              value={clientFilter}
              onChange={(e) => {
                setClientFilter(e.target.value)
                pager.resetPage()
              }}
              className="field-input h-8 px-2.5 text-[12px]"
            >
              <option value="all">全部工具</option>
              {clients.map((c) => (
                <option key={c} value={c}>
                  {labelOf(CLIENT_LABELS, c)}
                </option>
              ))}
            </select>
          )}
        </ListToolbar>
      )}

      {filtered.length === 0 ? (
        <p className="text-[13px] text-ink-4 py-8 text-center">
          {outcomes.length === 0
            ? '暂无完工验收记录，AI 工具交付任务并回传实测证据后会出现在这里。'
            : '没有符合筛选条件的验收记录。'}
        </p>
      ) : (
        <div className="divide-y divide-line-soft">
          {pager.pageItems.map((o) => (
            <article key={o.outcome_id} className="py-3.5 space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge status={o.status} />
                  <span className="chip">{labelOf(CLIENT_LABELS, o.client)}</span>
                  <span className="chip">{o.origin === 'natural' ? '真实任务' : o.origin}</span>
                  <span className="mono text-[11px] text-ink-4">{o.outcome_id}</span>
                </div>
                <span className="num text-[11.5px] text-ink-4">
                  {formatDateYMD(o.created_at)}
                </span>
              </div>
              <p className="text-[13.5px] text-ink leading-relaxed">{o.summary}</p>
              {o.checks && o.checks.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {o.checks.map((chk, idx) => (
                    <span
                      key={idx}
                      className={`text-[11px] px-2 py-0.5 rounded-[var(--radius-mark)] border ${
                        chk.result === 'pass'
                          ? 'bg-ok-soft/70 text-ok-ink border-ok/25'
                          : chk.result === 'fail'
                            ? 'bg-hot-soft text-hot border-hot/25'
                            : 'bg-bg-sunk text-ink-3 border-line-soft'
                      }`}
                    >
                      [{CHECK_KIND_LABELS[chk.kind] || chk.kind}] {chk.name}：
                      {CHECK_RESULT_LABELS[chk.result] || chk.result}
                    </span>
                  ))}
                </div>
              )}
              {(o.applied_rule_ids.length > 0 ||
                o.used_skills.length > 0 ||
                o.used_knowledge_paths.length > 0) && (
                <div className="flex flex-wrap gap-3 text-[11.5px] text-ink-4">
                  {o.applied_rule_ids.length > 0 && (
                    <span>核验规则：{o.applied_rule_ids.join(', ')}</span>
                  )}
                  {o.used_skills.length > 0 && (
                    <span>实测 Skill：{o.used_skills.join(', ')}</span>
                  )}
                  {o.used_knowledge_paths.length > 0 && (
                    <span>顺延保鲜笔记：{o.used_knowledge_paths.length} 篇</span>
                  )}
                </div>
              )}
            </article>
          ))}
        </div>
      )}

      <PaginationBar
        total={pager.total}
        page={pager.page}
        pageSize={pager.pageSize}
        totalPages={pager.totalPages}
        onPageChange={pager.setPage}
        onPageSizeChange={pager.setPageSize}
      />
    </section>
  )
}
