import React from 'react'
import { ArrowUpRight } from 'lucide-react'
import { api } from '../../api'
import { StatusBadge } from '../StatusBadge'
import { CLIENT_LABELS, labelOf, RETRO_KIND_LABELS } from '../../lib/labels'
import type { MistakeObservation, RetroItem } from '../../types'

/**
 * The "错题与分歧" tab: the mistake observation pool ("事不过三" slow track with
 * the red-silent fast track) and the live retro clues reported by running tools.
 */
export const CluesObservationsTab: React.FC<{
  observations: MistakeObservation[]
  retros: RetroItem[]
  onChanged: () => Promise<void>
  onToast: (msg: string) => void
}> = ({ observations, retros, onChanged, onToast }) => {
  const handlePromoteObservation = async (obsId: string) => {
    await api.promoteObservation(obsId)
    await onChanged()
    onToast('已将该错题升级为试运行避坑规则')
  }

  const handleArchiveObservation = async (obsId: string) => {
    await api.archiveObservation(obsId)
    await onChanged()
  }

  const handleRetroStatus = async (retroId: string, status: RetroItem['status']) => {
    await api.updateRetroStatus(retroId, status)
    await onChanged()
  }

  return (
    <div className="space-y-5">
      <section className="card p-5">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
          <div>
            <h2 className="text-[15px] font-bold text-ink">错题观察池（事不过三）</h2>
            <p className="text-[12px] text-ink-4 mt-0.5">
              普通失误先在这里按根因聚类，累计出现 3 次自动晋升规则（防止规则膨胀）；高危沉默错误（🔴）可首次直通试运行。
            </p>
          </div>
          <span className="chip num">共 {observations.length} 条记录</span>
        </div>

        {observations.length === 0 ? (
          <p className="text-[13px] text-ink-4 py-8 text-center">
            观察池目前为空。每天 20:00 跑完沉淀批次后，AI 遇到的各类踩坑会自动归类到这里。
          </p>
        ) : (
          <div className="divide-y divide-line-soft mt-1">
            {observations.map((obs) => (
              <div
                key={obs.obs_id}
                className="py-3.5 flex flex-wrap items-start justify-between gap-3"
              >
                <div className="space-y-1.5 min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <StatusBadge status={obs.severity_grade} />
                    <StatusBadge status={obs.status} />
                    <span className="chip num font-medium">复现 {obs.occurrence_count}/3</span>
                    <span className="mono text-[11.5px] text-ink-4">{obs.root_cause_key}</span>
                  </div>
                  <div className="text-[14px] font-semibold text-ink">{obs.title}</div>
                  {obs.thinking_root_cause && (
                    <p className="text-[12.5px] text-ink-2">
                      <span className="text-ink-4">思维根因：</span>
                      {obs.thinking_root_cause}
                    </p>
                  )}
                  {obs.truth && (
                    <p className="text-[12.5px] text-ink-3">
                      <span className="text-ink-4">正确做法：</span>
                      {obs.truth}
                    </p>
                  )}
                </div>
                {obs.status === 'observing' && (
                  <div className="flex items-center gap-1.5 shrink-0">
                    <button
                      type="button"
                      onClick={() => handlePromoteObservation(obs.obs_id)}
                      className="inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink text-[12px] font-medium"
                    >
                      <ArrowUpRight className="w-3.5 h-3.5" />
                      立即升级为规则
                    </button>
                    <button
                      type="button"
                      onClick={() => handleArchiveObservation(obs.obs_id)}
                      className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px]"
                    >
                      归档
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="card p-5">
        <div className="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-line-soft">
          <h2 className="text-[14.5px] font-bold text-ink">实时异常线索</h2>
          <span className="text-[12px] text-ink-4">
            运行中捕获的卡死重试、规则复发等即时信号，会在每日批次中一并复盘。
          </span>
        </div>
        {retros.length === 0 ? (
          <p className="text-[13px] text-ink-4 py-6 text-center">暂无实时异常线索。</p>
        ) : (
          <div className="divide-y divide-line-soft mt-1">
            {retros.map((item) => (
              <div
                key={item.retro_id}
                className="py-3.5 flex flex-wrap items-start justify-between gap-3"
              >
                <div className="space-y-1.5 min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <StatusBadge status={item.status} />
                    <span className="chip">{labelOf(RETRO_KIND_LABELS, item.kind)}</span>
                    <span className="chip">{labelOf(CLIENT_LABELS, item.client)}</span>
                  </div>
                  <p className="text-[13.5px] leading-[1.65] text-ink">{item.summary}</p>
                </div>
                {(item.status === 'open' || item.status === 'in_review') && (
                  <div className="flex items-center gap-1.5 shrink-0">
                    <button
                      type="button"
                      onClick={() => handleRetroStatus(item.retro_id, 'resolved')}
                      className="h-7 px-2.5 rounded-[var(--radius-mark)] bg-ok-soft text-ok-ink text-[12px] font-medium"
                    >
                      已处理
                    </button>
                    <button
                      type="button"
                      onClick={() => handleRetroStatus(item.retro_id, 'dismissed')}
                      className="h-7 px-2.5 rounded-[var(--radius-mark)] border border-line text-ink-3 text-[12px]"
                    >
                      忽略
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
