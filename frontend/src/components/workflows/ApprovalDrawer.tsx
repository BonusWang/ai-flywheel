import React, { useEffect } from 'react'
import { Check, Loader2 } from 'lucide-react'
import { MarkdownView } from '../MarkdownView'
import { ConfirmButton } from '../ui/ConfirmButton'
import { SkillUpdatePanel } from './SkillUpdatePanel'
import type { WorkflowRunRecord } from '../../types'

/**
 * The in-row approval drawer of one batch:
 * - Two-step ConfirmButton on "整批放弃" so a misclick never cancels a batch.
 * - "同意并写入" disables when 0 items are selected ("请至少勾选 1 项").
 * - Smooth card check-state transitions and optional select-all toggle.
 * - Cmd/Ctrl + Enter approves currently selected items.
 */
export const ApprovalDrawer: React.FC<{
  run: WorkflowRunRecord
  active?: boolean
  approvedIndices: number[]
  busy: boolean
  feishuLabel: string
  onToggleIndex: (idx: number) => void
  onSelectAll?: (selectAll: boolean) => void
  onApprove: () => void
  onReject: () => void
}> = ({
  run,
  active = true,
  approvedIndices,
  busy,
  feishuLabel,
  onToggleIndex,
  onSelectAll,
  onApprove,
  onReject,
}) => {
  const total = run.planned_writes.length
  const selectedCount = approvedIndices.length
  const allSelected = total > 0 && selectedCount === total
  const noneSelected = selectedCount === 0

  useEffect(() => {
    if (!active) return
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && !busy && !noneSelected) {
        const tag = (e.target as HTMLElement | null)?.tagName
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
        e.preventDefault()
        onApprove()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [active, busy, noneSelected, onApprove])

  return (
    <div className="rounded-[var(--radius-card)] border border-amber/40 bg-amber-soft/55 p-4 space-y-3.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[14px] font-semibold text-ink">
              本批整理出 {total} 项建议，确认后才会写入
            </span>
            {total > 1 && onSelectAll && (
              <button
                type="button"
                onClick={() => onSelectAll(!allSelected)}
                className="pressable text-[11.5px] font-medium text-accent hover:text-accent-ink"
              >
                {allSelected ? '取消全选' : '全部勾选'}
              </button>
            )}
          </div>
          <p className="text-[12px] text-ink-3 mt-0.5">
            勾选同意的条目将写入知识库或生效为避坑规则；若整批都不需要可点击「整批放弃」。
          </p>
          <p className="text-[11.5px] text-ink-4 mt-0.5" data-testid="feishu-link-status">
            飞书审批：{feishuLabel}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ConfirmButton
            label="整批放弃"
            confirmLabel="确认整批放弃？"
            disabled={busy}
            onConfirm={onReject}
            title="放弃本批所有写入计划，会话将退回待整理队列"
            className="h-8 px-3 rounded-[var(--radius-control)] border border-line-strong bg-surface text-[12px] text-ink-2 hover:border-hot/50 hover:text-hot disabled:opacity-50"
            confirmClassName="h-8 px-3 rounded-[var(--radius-control)] border border-hot bg-hot text-white text-[12px] font-semibold"
          />
          <button
            type="button"
            onClick={onApprove}
            disabled={busy || noneSelected}
            title={noneSelected ? '请至少勾选 1 项写入建议' : '快捷键 Cmd/Ctrl + Enter'}
            className="pressable inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-ok text-ok-contrast text-[12px] font-semibold hover:opacity-90 disabled:opacity-45"
          >
            {busy ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <Check className="w-3.5 h-3.5" />
            )}
            <span className="num">
              {busy
                ? '正在写入…'
                : noneSelected
                  ? '请至少勾选 1 项'
                  : `同意并写入 (${selectedCount}/${total})`}
            </span>
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {run.planned_writes.map((pw, pIdx) => {
          const checked = approvedIndices.includes(pIdx)
          const isSkillUpdate =
            pw.target_kind === 'skill_improvement' && pw.action === 'update'
          return (
            <label
              key={pIdx}
              className={`card p-3.5 flex items-start gap-2.5 cursor-pointer transition-[border-color,opacity,transform] duration-[150ms] ease-[var(--ease-out-quint)] ${
                checked
                  ? 'border-ok/45 opacity-100 scale-100'
                  : 'opacity-55 scale-[0.99] hover:opacity-75'
              }`}
            >
              <input
                type="checkbox"
                checked={checked}
                onChange={() => onToggleIndex(pIdx)}
                className="mt-1 size-3.5 accent-accent cursor-pointer"
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[13px] font-semibold text-ink">{pw.title}</span>
                  <span
                    className={`chip ${
                      pw.target_kind === 'skill_improvement'
                        ? 'bg-accent-soft text-accent-ink border-accent/30 font-semibold'
                        : ''
                    }`}
                  >
                    {pw.target_kind === 'skill_improvement'
                      ? pw.action === 'update'
                        ? '优化 Skill'
                        : '新建 Skill'
                      : pw.target_kind === 'flywheel_rule'
                        ? '避坑规则'
                        : '知识笔记'}
                  </span>
                </div>
                {pw.approval_reason && (
                  <div className="text-[11.5px] text-amber-ink mt-1">
                    需确认原因：{pw.approval_reason}
                  </div>
                )}
                {pw.relative_path && (
                  <div className="mono text-[11px] text-accent-ink mt-0.5 truncate">
                    {pw.relative_path}
                  </div>
                )}
                <p className="text-[12px] text-ink-3 mt-1">{pw.summary}</p>
                {pw.content_preview && (
                  <div className="mt-2">
                    {isSkillUpdate && (
                      <div className="text-[11px] text-ink-4 mb-1">
                        新增小节（追加到文件末尾，原有内容不动）
                      </div>
                    )}
                    <div className="rounded-[var(--radius-control)] bg-bg-sunk border border-line-soft p-2.5 max-h-32 overflow-y-auto">
                      <MarkdownView content={pw.content_preview} />
                    </div>
                  </div>
                )}
                {isSkillUpdate && <SkillUpdatePanel pw={pw} />}
              </div>
            </label>
          )
        })}
      </div>
    </div>
  )
}
