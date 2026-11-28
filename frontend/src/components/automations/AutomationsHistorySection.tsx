import React, { useMemo, useState } from 'react'
import { ArrowRight, Calendar } from 'lucide-react'
import type { NavigateFn } from '../../App'
import {
  PHASE_LABELS,
  TRIGGER_LABELS,
  formatDuration,
  formatShanghaiTimestamp,
  labelOf,
} from '../../lib/labels'
import type { AutomationsSnapshot } from '../../types'
import { ListToolbar, PaginationBar, usePagination } from '../ListControls'
import { PillTabs } from '../PageLayout'
import { StatusBadge } from '../StatusBadge'

interface Props {
  recentRuns: AutomationsSnapshot['recent_runs']
  onNavigate: NavigateFn
}

export const AutomationsHistorySection: React.FC<Props> = ({ recentRuns, onNavigate }) => {
  const [historyFilter, setHistoryFilter] = useState<'all' | 'scheduled'>('all')
  const [historyQuery, setHistoryQuery] = useState('')
  const [historyStatus, setHistoryStatus] = useState('all')

  const filteredRuns = useMemo(() => {
    const q = historyQuery.trim().toLowerCase()
    return [...recentRuns]
      .filter((r) => (historyFilter === 'scheduled' ? r.trigger === 'scheduled' : true))
      .filter((r) => (historyStatus === 'all' ? true : r.status === historyStatus))
      .filter((r) =>
        !q
          ? true
          : `${r.batch_date || ''} #${r.batch_seq} ${r.run_id} ${r.phase} ${labelOf(PHASE_LABELS, r.phase)} ${r.trigger} ${labelOf(TRIGGER_LABELS, r.trigger)} ${r.error || ''}`
              .toLowerCase()
              .includes(q)
      )
      .sort((a, b) =>
        String(b.started_at || b.created_at || '').localeCompare(
          String(a.started_at || a.created_at || '')
        )
      )
  }, [recentRuns, historyFilter, historyStatus, historyQuery])

  const hasHistoryFilters = Boolean(historyQuery.trim()) || historyStatus !== 'all'
  const historyPager = usePagination(filteredRuns, 10)

  return (
    <section
      id="automation-sec-history"
      className="card p-5 sm:p-6 space-y-4 scroll-mt-20 lg:scroll-mt-6"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-[16px] font-semibold text-ink flex items-center gap-2">
            <Calendar className="w-4 h-4 text-accent" />
            <span>执行记录</span>
          </h2>
          <p className="mt-0.5 text-[12.5px] text-ink-3">
            点任意批次的「查看批次」直接定位到该批明细与审批抽屉。
          </p>
        </div>
        <PillTabs
          items={[
            { key: 'all', label: '全部批次', count: recentRuns.length },
            {
              key: 'scheduled',
              label: '仅定时触发',
              count: recentRuns.filter((r) => r.trigger === 'scheduled').length,
            },
          ]}
          active={historyFilter}
          onChange={(k) => {
            setHistoryFilter(k as 'all' | 'scheduled')
            historyPager.resetPage()
          }}
        />
      </div>

      {recentRuns.length > 0 && (
        <ListToolbar
          query={historyQuery}
          onQueryChange={(q) => {
            setHistoryQuery(q)
            historyPager.resetPage()
          }}
          placeholder="按日期、批次号或错误信息搜索…"
          hasActiveFilters={hasHistoryFilters}
          onResetFilters={() => {
            setHistoryQuery('')
            setHistoryStatus('all')
            historyPager.resetPage()
          }}
        >
          <select
            aria-label="执行记录状态筛选"
            value={historyStatus}
            onChange={(e) => {
              setHistoryStatus(e.target.value)
              historyPager.resetPage()
            }}
            className="field-input h-8 px-2.5 text-[12px]"
          >
            <option value="all">全部状态</option>
            <option value="PASS">已通过</option>
            <option value="AWAITING_APPROVAL">待审批</option>
            <option value="FAILED">已失败</option>
            <option value="PARTIAL">部分完成</option>
            <option value="CANCELLED">已取消</option>
          </select>
        </ListToolbar>
      )}

      {filteredRuns.length === 0 ? (
        <div className="rounded-[var(--radius-tile)] border border-dashed border-line py-8 text-center text-[13px] text-ink-4">
          {historyFilter === 'scheduled'
            ? '暂无定时调度触发的历史批次；到达预定时间后会自动记录在这里。'
            : '暂无符合条件的运行记录。'}
        </div>
      ) : (
        <div className="space-y-2">
          <div className="divide-y divide-line-soft rounded-[var(--radius-tile)] border border-line-soft overflow-hidden">
            {historyPager.pageItems.map((run) => (
              <div
                key={run.run_id}
                className="flex flex-wrap items-center justify-between gap-3 bg-surface px-4 py-3 hover:bg-bg-sunk/60 transition-colors duration-[150ms]"
              >
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="num font-semibold text-[13px] text-ink">
                      {run.batch_date} #{String(run.batch_seq).padStart(2, '0')}
                    </span>
                    <StatusBadge status={run.status} />
                    <span className="rounded bg-bg-sunk px-2 py-0.5 text-[11px] font-medium text-ink-3">
                      {labelOf(TRIGGER_LABELS, run.trigger)}
                    </span>
                    <span className="rounded bg-bg-sunk px-2 py-0.5 text-[11px] font-medium text-ink-3">
                      {labelOf(PHASE_LABELS, run.phase)}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px] text-ink-4 num">
                    <span>{formatShanghaiTimestamp(run.started_at || run.created_at)}</span>
                    <span>·</span>
                    <span>整理 {run.admitted_count} 段对话</span>
                    <span>·</span>
                    <span>拟写入 {run.planned_writes_count} 项</span>
                    {run.duration_ms > 0 && (
                      <>
                        <span>·</span>
                        <span>
                          耗时{' '}
                          {formatDuration(run.started_at, run.finished_at, [
                            { duration_ms: run.duration_ms },
                          ])}
                        </span>
                      </>
                    )}
                  </div>
                  {run.error && (
                    <div className="text-[12px] text-hot truncate max-w-[680px]">
                      失败原因：{run.error}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() =>
                    onNavigate('runs', { runsTab: 'pipeline', runId: run.run_id })
                  }
                  className="pressable inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 py-1 text-[12px] font-medium text-ink-2 hover:text-accent-ink hover:border-accent/40"
                >
                  <span>查看批次</span>
                  <ArrowRight className="w-3.5 h-3.5" />
                </button>
              </div>
            ))}
          </div>
          <PaginationBar
            total={historyPager.total}
            page={historyPager.page}
            pageSize={historyPager.pageSize}
            totalPages={historyPager.totalPages}
            onPageChange={historyPager.setPage}
            onPageSizeChange={historyPager.setPageSize}
          />
        </div>
      )}
    </section>
  )
}
