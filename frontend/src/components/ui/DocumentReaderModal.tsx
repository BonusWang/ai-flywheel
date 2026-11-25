import React, { useEffect, useState } from 'react'
import { api } from '../../api'
import { MarkdownView } from '../MarkdownView'
import { CopyButton } from '../CodeBlock'
import { Modal } from './Modal'

/**
 * Shared knowledge-note reader modal used by both HomePage and AssetsFreshnessTab.
 * Handles loading skeleton and read error gracefully instead of failing silently.
 */
export const DocumentReaderModal: React.FC<{
  docPath: string | null
  title?: string
  onClose: () => void
}> = ({ docPath, title, onClose }) => {
  const [content, setContent] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!docPath) {
      setContent(null)
      setError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .readKnowledgeDoc(docPath)
      .then((res) => {
        if (!cancelled) setContent(res.content)
      })
      .catch((e: any) => {
        if (!cancelled) setError(e?.message || '读取知识笔记失败')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [docPath])

  return (
    <Modal
      open={Boolean(docPath)}
      onClose={onClose}
      title={title || docPath || '知识笔记'}
      subtitle={docPath ? <span className="mono">{docPath}</span> : undefined}
      headerActions={docPath ? <CopyButton text={docPath} label="复制路径" /> : undefined}
      maxWidthClass="max-w-2xl"
    >
      {loading ? (
        <div className="space-y-3 py-2" aria-busy="true">
          <div className="skeleton-block h-5 w-2/5" />
          <div className="skeleton-block h-3.5 w-full" />
          <div className="skeleton-block h-3.5 w-5/6" />
          <div className="skeleton-block h-3.5 w-4/6" />
          <div className="skeleton-block h-24 w-full mt-4" />
        </div>
      ) : error ? (
        <div className="rounded-[var(--radius-control)] border border-hot/30 bg-hot-soft px-4 py-3 text-[13px] text-hot">
          {error}
        </div>
      ) : content !== null ? (
        <MarkdownView content={content} />
      ) : null}
    </Modal>
  )
}
