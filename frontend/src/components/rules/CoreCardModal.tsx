import React, { useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from '../../api'
import { errorMessage } from '../../lib/errors'
import { MarkdownView } from '../MarkdownView'
import { Modal } from '../ui/Modal'
import { useToast } from '../ui/Toast'
import type { CoreCardPayload } from '../../types'

/**
 * The shared FLYWHEEL-CORE.md preview and rebuild modal.
 *
 * Used by 「今日态势」 and 「规则与进化」. Built on top of the unified Modal
 * primitive (Esc, backdrop close, smooth enter/exit sheet animation).
 */
export const CoreCardModal: React.FC<{
  open: boolean
  card: CoreCardPayload | null
  onClose: () => void
  onRebuilt?: (updated: CoreCardPayload) => void
}> = ({ open, card, onClose, onRebuilt }) => {
  const [refreshing, setRefreshing] = useState(false)
  const toast = useToast()

  const handleRefresh = async () => {
    if (refreshing) return
    setRefreshing(true)
    try {
      const updated = await api.refreshCoreCard()
      onRebuilt?.(updated)
      toast.push('已重建 AI 随身速查卡（FLYWHEEL-CORE.md）')
    } catch (e: unknown) {
      toast.push({ tone: 'error', message: errorMessage(e, '重建速查卡失败') })
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="AI 随身速查卡（FLYWHEEL-CORE.md）"
      subtitle={
        card
          ? `${card.lines} / 200 行 · ${(card.bytes / 1024).toFixed(1)} / 24 KB`
          : undefined
      }
      headerActions={
        <button
          type="button"
          onClick={handleRefresh}
          disabled={refreshing}
          className="pressable inline-flex items-center gap-1.5 h-7 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-[12px] text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
        >
          <RefreshCw className={`w-3 h-3 ${refreshing ? 'animate-spin' : ''}`} />
          <span>{refreshing ? '重建中…' : '立即重建'}</span>
        </button>
      }
      maxWidthClass="max-w-3xl"
    >
      {card ? (
        <MarkdownView content={card.content} />
      ) : (
        <p className="text-[13px] text-ink-4">暂无速查卡内容。</p>
      )}
    </Modal>
  )
}
