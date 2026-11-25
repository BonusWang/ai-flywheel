import React from 'react'

export interface ManualSessionInput {
  client: string
  title: string
  summary: string
}

/**
 * The manual session quick-entry form.
 *
 * It renders inline as a card (not as an overlay) exactly where the page shows it.
 * The field values stay in the page so a draft survives hiding and reopening the
 * form; submitting hands a normalized payload back to the page, which owns the API
 * call, the error banner and the refresh.
 */
export const ManualSessionModal: React.FC<{
  busy: boolean
  client: string
  title: string
  summary: string
  onClientChange: (value: string) => void
  onTitleChange: (value: string) => void
  onSummaryChange: (value: string) => void
  onSubmit: (data: ManualSessionInput) => void | Promise<void>
}> = ({
  busy,
  client,
  title,
  summary,
  onClientChange,
  onTitleChange,
  onSummaryChange,
  onSubmit,
}) => {
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim()) return
    onSubmit({ client, title: title.trim(), summary: summary.trim() })
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="card p-4 grid grid-cols-1 md:grid-cols-12 gap-3"
    >
      <div className="md:col-span-2">
        <label className="block text-[11.5px] text-ink-4 mb-1">来源工具</label>
        <select
          value={client}
          onChange={(e) => onClientChange(e.target.value)}
          className="field-input w-full px-2.5 py-1.5 text-[12px]"
        >
          <option value="codex">Codex</option>
          <option value="claude-code">Claude Code</option>
          <option value="claude">Claude Desktop</option>
        </select>
      </div>
      <div className="md:col-span-4">
        <label className="block text-[11.5px] text-ink-4 mb-1">这件事的标题</label>
        <input
          required
          value={title}
          onChange={(e) => onTitleChange(e.target.value)}
          placeholder="例如：排查接口超时并加上重试机制"
          className="field-input w-full px-2.5 py-1.5 text-[12px]"
        />
      </div>
      <div className="md:col-span-4">
        <label className="block text-[11.5px] text-ink-4 mb-1">关键结论（选填）</label>
        <input
          value={summary}
          onChange={(e) => onSummaryChange(e.target.value)}
          placeholder="一句话记下踩坑原因和有效解法"
          className="field-input w-full px-2.5 py-1.5 text-[12px]"
        />
      </div>
      <div className="md:col-span-2 flex items-end">
        <button
          type="submit"
          disabled={busy}
          className="w-full h-8 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12px] font-medium hover:bg-accent-ink"
        >
          加入待整理
        </button>
      </div>
    </form>
  )
}
