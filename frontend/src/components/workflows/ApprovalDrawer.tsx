import React from 'react'
import { Check } from 'lucide-react'
import { MarkdownView } from '../MarkdownView'
import { SkillUpdatePanel } from './SkillUpdatePanel'
import type { WorkflowRunRecord } from '../../types'

/**
 * The in-row approval drawer of one batch.
 *
 * Every planned write is a checkbox card; a Skill update additionally shows the
 * existing file so the approver can compare before appending. The selection and the
 * approve/reject calls stay in the page — this component only renders and forwards.
 */
export const ApprovalDrawer: React.FC<{
  run: WorkflowRunRecord
  approvedIndices: number[]
  busy: boolean
  feishuLabel: string
  onToggleIndex: (idx: number) => void
  onApprove: () => void
  onReject: () => void
}> = ({ run, approvedIndices, busy, feishuLabel, onToggleIndex, onApprove, onReject }) => (
  <div className="rounded-[var(--radius-card)] border border-amber/40 bg-amber-soft/55 p-4 space-y-3.5">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <div className="text-[14px] font-semibold text-ink">
          本批整理出 {run.planned_writes.length} 项建议，确认后才会写入
        </div>
        <p className="text-[12px] text-ink-3 mt-0.5">
          勾选同意的条目将写入知识库或生效为避坑规则；不想保存可以取消。
        </p>
        <p className="text-[11.5px] text-ink-4 mt-0.5" data-testid="feishu-link-status">
          飞书审批：{feishuLabel}
        </p>
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onReject}
          disabled={busy}
          className="h-8 px-3 rounded-[var(--radius-control)] border border-line-strong bg-surface text-[12px] text-ink-2 hover:text-ink"
        >
          取消本次写入
        </button>
        <button
          type="button"
          onClick={onApprove}
          disabled={busy}
          className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-ok text-white text-[12px] font-semibold hover:opacity-90"
        >
          <Check className="w-3.5 h-3.5" />
          同意并写入 ({approvedIndices.length}/{run.planned_writes.length})
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
            className={`card p-3.5 flex items-start gap-2.5 cursor-pointer ${
              checked ? 'border-ok/45' : 'opacity-60'
            }`}
          >
            <input
              type="checkbox"
              checked={checked}
              onChange={() => onToggleIndex(pIdx)}
              className="mt-1 accent-accent"
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
