import React, { useEffect, useRef } from 'react'
import { Loader2 } from 'lucide-react'
import { CLIENT_LABELS } from '../../lib/labels'

export interface ManualSessionInput {
  client: string
  title: string
  summary: string
}

const CLIENT_OPTIONS = ['codex', 'claude-code', 'claude', 'generic'] as const

/**
 * The manual session quick-entry form.
 * Auto-focuses the title input when opened, closes on Escape, and preserves draft text.
 */
export const ManualSessionModal: React.FC<{
  open?: boolean
  busy: boolean
  client: string
  title: string
  summary: string
  onClientChange: (value: string) => void
  onTitleChange: (value: string) => void
  onSummaryChange: (value: string) => void
  onClose?: () => void
  onSubmit: (data: ManualSessionInput) => void | Promise<void>
}> = ({
  open = true,
  busy,
  client,
  title,
  summary,
  onClientChange,
  onTitleChange,
  onSummaryChange,
  onClose,
  onSubmit,
}) => {
  const titleRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    const timer = window.setTimeout(() => titleRef.current?.focus(), 140)
    return () => window.clearTimeout(timer)
  }, [open])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim()) return
    onSubmit({ client, title: title.trim(), summary: summary.trim() })
  }

  return (
    <form
      onSubmit={handleSubmit}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && onClose) {
          e.stopPropagation()
          onClose()
        }
      }}
      className="card p-4 grid grid-cols-1 md:grid-cols-12 gap-3"
    >
      <div className="md:col-span-2">
        <label className="block text-[11.5px] text-ink-4 mb-1">来源工具</label>
        <select
          value={client}
          onChange={(e) => onClientChange(e.target.value)}
          className="field-input w-full h-8 px-2.5 text-[12px]"
        >
          {CLIENT_OPTIONS.map((key) => (
            <option key={key} value={key}>
              {CLIENT_LABELS[key] || key}
            </option>
          ))}
        </select>
      </div>
      <div className="md:col-span-4">
        <label className="block text-[11.5px] text-ink-4 mb-1">这件事的标题</label>
        <input
          ref={titleRef}
          required
          value={title}
          onChange={(e) => onTitleChange(e.target.value)}
          placeholder="例如：排查接口超时并加上重试机制"
          className="field-input w-full h-8 px-2.5 text-[12px]"
        />
      </div>
      <div className="md:col-span-4">
        <label className="block text-[11.5px] text-ink-4 mb-1">关键结论（选填）</label>
        <input
          value={summary}
          onChange={(e) => onSummaryChange(e.target.value)}
          placeholder="一句话记下踩坑原因和有效解法"
          className="field-input w-full h-8 px-2.5 text-[12px]"
        />
      </div>
      <div className="md:col-span-2 flex items-end">
        <button
          type="submit"
          disabled={busy || !title.trim()}
          className="pressable inline-flex items-center justify-center gap-1.5 w-full h-8 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12px] font-medium hover:bg-accent-ink disabled:opacity-50"
        >
          {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
          <span>加入待整理</span>
        </button>
      </div>
    </form>
  )
}
