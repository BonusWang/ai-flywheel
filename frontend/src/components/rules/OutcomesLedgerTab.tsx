import React, { useMemo, useState } from 'react'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { StatusBadge } from '../StatusBadge'
import { CLIENT_LABELS, formatDateYMD, labelOf } from '../../lib/labels'
import type { OutcomeRecord } from '../../types'

/**
 * The "证据台账" tab: every outcome submitted through flywheel_record_outcome,
 * sorted by created_at DESC with search, status/client filter, and 10-per-page pagination.
 */
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

  return (
    <section className="card p-5 space-y-3.5">
      <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
        <div>
          <h2 className="text-[15px] font-bold text-ink">执行期证据验收台账（Outcomes）</h2>
          <p className="text-[12px] text-ink-4 mt-0.5">
            记录每次通过 flywheel_record_outcome 提交的真实/测试/静态检查项与规则复测晋升效果。
          </p>
        </div>
        <span className="chip num">共 {outcomes.length} 条</span>
      </div>

      <ListToolbar
        query={query}
        onQueryChange={(q) => {
          setQuery(q)
          pager.resetPage()
        }}
        placeholder="搜索交付摘要、回执 ID、规则或 Skill…"
      >
        <select
          aria-label="结果状态筛选"
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value)
            pager.resetPage()
          }}
          className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
        >
          <option value="all">全部状态</option>
          <option value="success">成功</option>
          <option value="partial">部分完成</option>
          <option value="failed">失败</option>
        </select>
        {clients.length > 1 && (
          <select
            aria-label="客户端筛选"
            value={clientFilter}
            onChange={(e) => {
              setClientFilter(e.target.value)
              pager.resetPage()
            }}
            className="h-8 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[12px] text-ink"
          >
            <option value="all">全部客户端</option>
            {clients.map((c) => (
              <option key={c} value={c}>
                {labelOf(CLIENT_LABELS, c)}
              </option>
            ))}
          </select>
        )}
      </ListToolbar>

      {filtered.length === 0 ? (
        <p className="text-[13px] text-ink-4 py-8 text-center">
          {outcomes.length === 0
            ? '暂无证据验收记录。当 AI 工具交付任务并调用 flywheel_record_outcome 后会在此留痕。'
            : '没有符合筛选条件的证据记录。'}
        </p>
      ) : (
        <div className="divide-y divide-line-soft">
          {pager.pageItems.map((o) => (
            <article key={o.outcome_id} className="py-3.5 space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusBadge status={o.status} />
                  <span className="chip">{labelOf(CLIENT_LABELS, o.client)}</span>
                  <span className="chip">{o.origin === 'natural' ? '自然任务' : o.origin}</span>
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
                      [{chk.kind}] {chk.name}: {chk.result}
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
                    <span>续期经验卡：{o.used_knowledge_paths.length} 篇</span>
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
