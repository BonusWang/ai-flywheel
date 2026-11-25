import React, { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { api } from '../../api'
import { MarkdownView } from '../MarkdownView'
import type { PlannedWrite, SkillPreview } from '../../types'

/** The Skill a planned write targets, read off its relative path ("<slug>/SKILL.md"). */
export function skillSlugOf(pw: PlannedWrite): string {
  const raw = (pw.relative_path || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '')
  const parts = raw.split('/').filter(Boolean)
  if (parts.length >= 2 && parts[parts.length - 1].toLowerCase() === 'skill.md') {
    return parts[parts.length - 2]
  }
  return parts[0] || ''
}

/**
 * The existing Skill an update would append to: its size, its section outline and — on
 * demand — its full text, so the approver can compare the old file with the new section.
 */
export const SkillUpdatePanel: React.FC<{ pw: PlannedWrite }> = ({ pw }) => {
  const slug = skillSlugOf(pw)
  const [preview, setPreview] = useState<SkillPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (!slug) return
    let cancelled = false
    setLoading(true)
    api
      .skillPreview(slug)
      .then((data) => {
        if (!cancelled) setPreview(data)
      })
      .catch((e: any) => {
        if (!cancelled) setError(e.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [slug])

  return (
    <div className="mt-2 rounded-[var(--radius-control)] border border-line-soft bg-bg-sunk/60 p-2.5 space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11.5px]">
        <span className="font-semibold text-ink-2">现有文件</span>
        {loading && <span className="text-ink-4">读取中…</span>}
        {!loading && preview && (
          <span className="num text-ink-3">
            {preview.line_count} 行 · {preview.headings.length} 个小节 · 全部保留
          </span>
        )}
        {!loading && !preview && !error && <span className="text-ink-4">未能读取</span>}
      </div>
      {preview && preview.headings.length > 0 && (
        <ul className="text-[11.5px] text-ink-3 space-y-0.5">
          {preview.headings.map((heading) => (
            <li key={heading} className="truncate">
              <span className="mono text-ink-4">##</span> {heading}
            </li>
          ))}
        </ul>
      )}
      {preview && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="inline-flex items-center gap-1 text-[11.5px] text-accent-ink hover:underline"
        >
          {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          {expanded ? '收起现有全文' : '对照现有全文'}
        </button>
      )}
      {expanded && preview && (
        <div className="max-h-48 overflow-y-auto rounded-[var(--radius-control)] bg-surface border border-line-soft p-2">
          <MarkdownView content={preview.content} />
        </div>
      )}
      {error && <div className="text-[11.5px] text-hot">读取现有文件失败：{error}</div>}
    </div>
  )
}
