import React, { useState } from 'react'
import { RefreshCw, X } from 'lucide-react'
import { api } from '../../api'
import { MarkdownView } from '../MarkdownView'
import type { CoreCardPayload } from '../../types'

/**
 * The shared FLYWHEEL-CORE.md preview and rebuild modal.
 *
 * Used by 「今日态势」 and 「规则与进化」. It owns the rebuilding state and calls
 * `api.refreshCoreCard()` itself; the rebuilt card is handed back through
 * `onRebuilt` so the page keeps owning the card data it shows elsewhere.
 */
export const CoreCardModal: React.FC<{
  open: boolean
  card: CoreCardPayload | null
  onClose: () => void
  onRebuilt?: (updated: CoreCardPayload) => void
}> = ({ open, card, onClose, onRebuilt }) => {
  const [refreshing, setRefreshing] = useState(false)

  if (!open) return null

  const handleRefresh = async () => {
    if (refreshing) return
    setRefreshing(true)
    try {
      const updated = await api.refreshCoreCard()
      onRebuilt?.(updated)
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 bg-ink/35 backdrop-blur-[1px] flex items-center justify-center p-4">
      <div className="card max-w-3xl w-full max-h-[85vh] flex flex-col overflow-hidden">
        <div className="px-5 py-3.5 border-b border-line flex items-center justify-between gap-3 bg-surface-2">
          <div className="min-w-0">
            <div className="text-[14px] font-semibold text-ink">
              跨端核心索引卡（FLYWHEEL-CORE.md）
            </div>
            {card && (
              <div className="text-[11.5px] text-ink-4 num">
                {card.lines} / 200 行 · {(card.bytes / 1024).toFixed(1)} / 24 KB · 来源：
                {card.source || 'file'}
              </div>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={handleRefresh}
              disabled={refreshing}
              className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-[12px] text-ink-2 hover:text-ink disabled:opacity-50"
            >
              <RefreshCw className={`w-3 h-3 ${refreshing ? 'animate-spin' : ''}`} />
              立即重建
            </button>
            <button
              type="button"
              onClick={onClose}
              className="p-1.5 rounded-[var(--radius-control)] hover:bg-bg-sunk text-ink-3"
              aria-label="关闭"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
        <div className="p-5 overflow-y-auto">
          {card ? (
            <MarkdownView content={card.content} />
          ) : (
            <p className="text-[13px] text-ink-4">暂无核心索引卡内容。</p>
          )}
        </div>
      </div>
    </div>
  )
}
