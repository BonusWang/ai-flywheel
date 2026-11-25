import React from 'react'
import { StatusBadge } from '../StatusBadge'
import { CLIENT_LABELS, formatDateYMD, labelOf } from '../../lib/labels'
import type { OutcomeRecord } from '../../types'

/**
 * The "证据台账" tab: every outcome submitted through flywheel_record_outcome,
 * with its real / test / static checks and the rules, skills and knowledge cards
 * the task actually exercised.
 */
export const OutcomesLedgerTab: React.FC<{ outcomes: OutcomeRecord[] }> = ({ outcomes }) => (
  <section className="card p-5 space-y-3">
    <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
      <div>
        <h2 className="text-[15px] font-bold text-ink">执行期证据验收台账（Outcomes）</h2>
        <p className="text-[12px] text-ink-4 mt-0.5">
          记录每次通过 flywheel_record_outcome 提交的真实/测试/静态检查项与规则复测晋升效果。
        </p>
      </div>
      <span className="chip num">最近 {outcomes.length} 条</span>
    </div>
    {outcomes.length === 0 ? (
      <p className="text-[13px] text-ink-4 py-8 text-center">
        暂无证据验收记录。当 AI 工具交付任务并调用 flywheel_record_outcome 后会在此留痕。
      </p>
    ) : (
      <div className="divide-y divide-line-soft">
        {outcomes.map((o) => (
          <article key={o.outcome_id} className="py-3.5 space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={o.status} />
                <span className="chip">{labelOf(CLIENT_LABELS, o.client)}</span>
                <span className="chip">{o.origin === 'natural' ? '自然任务' : o.origin}</span>
                <span className="mono text-[11px] text-ink-4">{o.outcome_id}</span>
              </div>
              <span className="num text-[11.5px] text-ink-4">
                {formatDateYMD(o.created_at)}
              </span>
            </div>
            <p className="text-[13.5px] text-ink leading-relaxed">{o.summary}</p>
            {o.checks && o.checks.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {o.checks.map((chk, idx) => (
                  <span
                    key={idx}
                    className={`text-[11px] px-2 py-0.5 rounded-[var(--radius-mark)] border ${
                      chk.result === 'pass'
                        ? 'bg-ok-soft/70 text-ok-ink border-ok/25'
                        : chk.result === 'fail'
                          ? 'bg-hot-soft text-hot border-hot/25'
                          : 'bg-bg-sunk text-ink-3 border-line-soft'
                    }`}
                  >
                    [{chk.kind}] {chk.name}: {chk.result}
                  </span>
                ))}
              </div>
            )}
            {(o.applied_rule_ids.length > 0 ||
              o.used_skills.length > 0 ||
              o.used_knowledge_paths.length > 0) && (
              <div className="flex flex-wrap gap-3 text-[11.5px] text-ink-4">
                {o.applied_rule_ids.length > 0 && (
                  <span>核验规则：{o.applied_rule_ids.join(', ')}</span>
                )}
                {o.used_skills.length > 0 && (
                  <span>实测 Skill：{o.used_skills.join(', ')}</span>
                )}
                {o.used_knowledge_paths.length > 0 && (
                  <span>续期经验卡：{o.used_knowledge_paths.length} 篇</span>
                )}
              </div>
            )}
          </article>
        ))}
      </div>
    )}
  </section>
)
