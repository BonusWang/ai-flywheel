import React, { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { api } from '../../api'
import { MarkdownView } from '../MarkdownView'
import { CopyButton } from '../CodeBlock'
import { Modal } from './Modal'
import { useToast } from './Toast'
import type { KnowledgeDocSummary } from '../../types'

export interface DocumentFreshnessMeta {
  freshness_tier?: string
  valid_until?: string | null
  last_verified_at?: string | null
  freshness_state?: string
  is_expired?: boolean
  surface_count?: number
  helped_count?: number
}

/**
 * Shared knowledge-note reader modal used by both HomePage and AssetsFreshnessTab.
 * Handles loading skeleton and read error gracefully instead of failing silently.
 * When freshness is passed, provides lightweight control-plane ledger management
 * with zero modifications to source files.
 */
export const DocumentReaderModal: React.FC<{
  docPath: string | null
  title?: string
  freshness?: DocumentFreshnessMeta | null
  onFreshnessUpdated?: (updated: KnowledgeDocSummary) => void
  onClose: () => void
}> = ({ docPath, title, freshness, onFreshnessUpdated, onClose }) => {
  const toast = useToast()
  const [content, setContent] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [currentFreshness, setCurrentFreshness] = useState<DocumentFreshnessMeta | null>(
    freshness || null
  )
  const [actionBusy, setActionBusy] = useState(false)

  useEffect(() => {
    setCurrentFreshness(freshness || null)
  }, [freshness, docPath])

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

  const handleVerify = async () => {
    if (!docPath || actionBusy) return
    setActionBusy(true)
    try {
      const res = await api.updateKnowledgeFreshness({
        doc_path: docPath,
        action: 'verify',
      })
      setCurrentFreshness(res.card)
      onFreshnessUpdated?.(res.card)
      toast.push('已确认笔记仍有效，保鲜期已顺延')
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '更新保鲜状态失败' })
    } finally {
      setActionBusy(false)
    }
  }

  const handleArchive = async () => {
    if (!docPath || actionBusy) return
    setActionBusy(true)
    try {
      const res = await api.updateKnowledgeFreshness({
        doc_path: docPath,
        action: 'archive',
      })
      setCurrentFreshness(res.card)
      onFreshnessUpdated?.(res.card)
      toast.push({
        message: '已移出待复核（台账已归档）',
        tone: 'info',
        undoLabel: '撤销',
        onUndo: async () => {
          const undoRes = await api.updateKnowledgeFreshness({
            doc_path: docPath,
            action: 'restore',
          })
          setCurrentFreshness(undoRes.card)
          onFreshnessUpdated?.(undoRes.card)
        },
      })
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '归档操作失败' })
    } finally {
      setActionBusy(false)
    }
  }

  const handleRestore = async () => {
    if (!docPath || actionBusy) return
    setActionBusy(true)
    try {
      const res = await api.updateKnowledgeFreshness({
        doc_path: docPath,
        action: 'restore',
      })
      setCurrentFreshness(res.card)
      onFreshnessUpdated?.(res.card)
      toast.push('已恢复保鲜跟踪')
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '恢复保鲜跟踪失败' })
    } finally {
      setActionBusy(false)
    }
  }

  const handleTierChange = async (tier: 'volatile' | 'evolving' | 'evergreen') => {
    if (!docPath || actionBusy) return
    setActionBusy(true)
    try {
      const res = await api.updateKnowledgeFreshness({
        doc_path: docPath,
        action: 'set_tier',
        freshness_tier: tier,
      })
      setCurrentFreshness(res.card)
      onFreshnessUpdated?.(res.card)
      const tierName = tier === 'volatile' ? '30天' : tier === 'evolving' ? '90天' : '180天'
      toast.push(`已将保鲜档位调整为 ${tierName}`)
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '调整档位失败' })
    } finally {
      setActionBusy(false)
    }
  }

  const isArchived =
    currentFreshness?.freshness_state === 'archived' ||
    currentFreshness?.freshness_state === 'superseded'

  return (
    <Modal
      open={Boolean(docPath)}
      onClose={onClose}
      title={title || docPath || '知识笔记'}
      subtitle={docPath ? <span className="mono">{docPath}</span> : undefined}
      headerActions={docPath ? <CopyButton text={docPath} label="复制路径" /> : undefined}
      maxWidthClass="max-w-2xl"
    >
      {currentFreshness && (
        <div className="rounded-[var(--radius-control)] border border-line-soft bg-bg-sunk/50 p-3 mb-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              {isArchived ? (
                <span className="px-1.5 py-0.5 rounded bg-bg-sunk text-ink-4 text-[11px] font-medium border border-line-soft">
                  已归档
                </span>
              ) : currentFreshness.is_expired ? (
                <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-amber-soft text-amber-ink text-[11px] font-semibold">
                  <span className="size-1.5 rounded-full bg-amber" />
                  待复核
                </span>
              ) : (
                <span className="px-1.5 py-0.5 rounded bg-ok-soft text-ok-ink text-[11px] font-medium">
                  有效
                </span>
              )}
              <span className="text-[12px] text-ink-3 num">
                {currentFreshness.valid_until ? `有效至 ${currentFreshness.valid_until}` : '无有效期限'}
              </span>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <select
                aria-label="保鲜期档位"
                value={currentFreshness.freshness_tier || 'evolving'}
                disabled={actionBusy}
                onChange={(e) =>
                  handleTierChange(e.target.value as 'volatile' | 'evolving' | 'evergreen')
                }
                className="field-input h-7 px-2 text-[11.5px]"
              >
                <option value="volatile">30天 (易变)</option>
                <option value="evolving">90天 (演进)</option>
                <option value="evergreen">180天 (常青)</option>
              </select>

              <button
                type="button"
                disabled={actionBusy}
                onClick={handleVerify}
                className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-control)] bg-accent-soft border border-accent/25 text-[11.5px] font-semibold text-accent-ink hover:bg-accent/18 disabled:opacity-50"
              >
                {actionBusy && <Loader2 className="w-3 h-3 animate-spin" />}
                <span>确认仍有效（顺延）</span>
              </button>

              {isArchived ? (
                <button
                  type="button"
                  disabled={actionBusy}
                  onClick={handleRestore}
                  className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-[11.5px] font-medium text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
                >
                  <span>恢复保鲜跟踪</span>
                </button>
              ) : (
                <button
                  type="button"
                  disabled={actionBusy}
                  onClick={handleArchive}
                  className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-control)] border border-line bg-surface text-[11.5px] font-medium text-ink-3 hover:text-hot hover:border-hot/40 disabled:opacity-50"
                >
                  <span>移出待复核（归档）</span>
                </button>
              )}
            </div>
          </div>
          <p className="text-[11px] text-ink-4">
            提示：仅更新飞轮台账，不改动知识库源文件。
          </p>
        </div>
      )}

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
