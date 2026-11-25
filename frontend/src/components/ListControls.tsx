import React, { useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, Search, X } from 'lucide-react'

const PAGE_SIZE_OPTIONS = [10, 20, 50] as const

export function usePagination<T>(items: T[], defaultPageSize = 10) {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(defaultPageSize)

  const total = items.length
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const safePage = Math.min(page, totalPages)

  useEffect(() => {
    if (page > totalPages) {
      setPage(totalPages)
    }
  }, [page, totalPages])

  const pageItems = useMemo(() => {
    const start = (safePage - 1) * pageSize
    return items.slice(start, start + pageSize)
  }, [items, safePage, pageSize])

  const resetPage = () => setPage(1)

  const changePageSize = (nextSize: number) => {
    setPageSize(nextSize)
    setPage(1)
  }

  return {
    page: safePage,
    setPage,
    pageSize,
    setPageSize: changePageSize,
    total,
    totalPages,
    pageItems,
    resetPage,
  }
}

export const ListToolbar: React.FC<{
  query: string
  onQueryChange: (q: string) => void
  placeholder?: string
  sortHint?: string
  children?: React.ReactNode
  actions?: React.ReactNode
}> = ({
  query,
  onQueryChange,
  placeholder = '按关键词搜索…',
  sortHint = '默认按创建时间倒序',
  children,
  actions,
}) => (
  <div className="flex flex-wrap items-center justify-between gap-2.5">
    <div className="flex flex-wrap items-center gap-2 flex-1 min-w-[220px]">
      <div className="relative flex-1 min-w-[180px] max-w-[320px]">
        <Search className="w-3.5 h-3.5 text-ink-4 absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" />
        <input
          type="text"
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder={placeholder}
          className="h-8 w-full pl-8 pr-7 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink placeholder:text-ink-4 focus:outline-none focus:border-accent"
        />
        {query && (
          <button
            type="button"
            aria-label="清空搜索"
            onClick={() => onQueryChange('')}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-ink-4 hover:text-ink"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        )}
      </div>
      {children}
    </div>
    <div className="flex flex-wrap items-center gap-2 shrink-0">
      {sortHint && <span className="text-[11.5px] text-ink-4">{sortHint}</span>}
      {actions}
    </div>
  </div>
)

export const PaginationBar: React.FC<{
  total: number
  page: number
  pageSize: number
  totalPages: number
  onPageChange: (page: number) => void
  onPageSizeChange: (size: number) => void
}> = ({ total, page, pageSize, totalPages, onPageChange, onPageSizeChange }) => {
  if (total === 0) return null

  const start = (page - 1) * pageSize + 1
  const end = Math.min(total, page * pageSize)

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-line-soft text-[12px] text-ink-3">
      <div className="flex items-center gap-2 num">
        <span>
          显示 <strong className="text-ink font-medium">{start}–{end}</strong> / 共{' '}
          <strong className="text-ink font-medium">{total}</strong> 条
        </span>
        <span className="text-ink-4">·</span>
        <label className="inline-flex items-center gap-1.5 text-ink-4">
          <span>每页</span>
          <select
            aria-label="每页显示条数"
            value={pageSize}
            onChange={(e) => onPageSizeChange(Number(e.target.value))}
            className="h-7 rounded-[var(--radius-mark)] border border-line bg-surface px-1.5 text-[12px] text-ink num focus:outline-none focus:border-accent"
          >
            {PAGE_SIZE_OPTIONS.map((opt) => (
              <option key={opt} value={opt}>
                {opt} 条
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="flex items-center gap-1.5 num">
        <button
          type="button"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
          className="inline-flex items-center gap-0.5 h-7 px-2 rounded-[var(--radius-mark)] border border-line bg-surface text-ink-2 hover:border-line-strong disabled:opacity-40"
        >
          <ChevronLeft className="w-3.5 h-3.5" />
          上一页
        </button>
        <span className="px-2 text-ink-3">
          {page} / {totalPages}
        </span>
        <button
          type="button"
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
          className="inline-flex items-center gap-0.5 h-7 px-2 rounded-[var(--radius-mark)] border border-line bg-surface text-ink-2 hover:border-line-strong disabled:opacity-40"
        >
          下一页
          <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  )
}
