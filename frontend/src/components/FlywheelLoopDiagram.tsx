import React, { useState } from 'react'
import { ArrowRight } from 'lucide-react'
import type { LoopStep } from '../types'

interface Props {
  steps: LoopStep[]
}

export const FlywheelLoopDiagram: React.FC<Props> = ({ steps }) => {
  const [selected, setSelected] = useState<number>(2)
  const frontstage = steps.filter((s) => s.phase === 'frontstage')
  const backstage = steps.filter((s) => s.phase === 'backstage')
  const activeStep = steps.find((s) => s.step === selected) || steps[0]

  return (
    <section className="card p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2 pb-3.5 border-b border-line-soft">
        <div>
          <h2 className="text-[15px] font-bold text-ink">8 步 AI 协作飞轮闭环</h2>
          <p className="text-[12.5px] text-ink-4 mt-0.5">
            前台执行（MCP + 极薄 Hook）↔ 后台自主进化（P00–P07 固定流水线）
          </p>
        </div>
        <div className="flex items-center gap-3 text-[12px]">
          <span className="inline-flex items-center gap-1.5 text-accent-ink">
            <span className="size-1.5 rounded-full bg-accent" />
            前台 01–04
          </span>
          <span className="inline-flex items-center gap-1.5 text-ok-ink">
            <span className="size-1.5 rounded-full bg-ok" />
            后台 05–08
          </span>
        </div>
      </div>

      <div className="mt-4 space-y-2.5">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
          {frontstage.map((s) => {
            const isSel = selected === s.step
            return (
              <button
                key={s.step}
                type="button"
                onClick={() => setSelected(s.step)}
                className={`text-left p-3 rounded-[var(--radius-tile)] border transition-colors ${
                  isSel
                    ? 'bg-accent-soft border-accent/50'
                    : 'bg-bg-sunk/55 border-line-soft hover:border-line-strong hover:bg-bg-sunk'
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="mono text-[11px] font-semibold text-accent-ink">0{s.step}</span>
                  <ArrowRight className="w-3 h-3 text-ink-4" />
                </div>
                <div className="text-[13.5px] font-semibold text-ink">{s.title}</div>
                <div className="text-[12px] text-ink-3 mt-0.5 line-clamp-1">{s.short}</div>
              </button>
            )
          })}
        </div>

        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2">
          {backstage.map((s) => {
            const isSel = selected === s.step
            return (
              <button
                key={s.step}
                type="button"
                onClick={() => setSelected(s.step)}
                className={`text-left p-3 rounded-[var(--radius-tile)] border transition-colors ${
                  isSel
                    ? 'bg-ok-soft border-ok/45'
                    : 'bg-bg-sunk/55 border-line-soft hover:border-line-strong hover:bg-bg-sunk'
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="mono text-[11px] font-semibold text-ok-ink">0{s.step}</span>
                  <ArrowRight className="w-3 h-3 text-ink-4" />
                </div>
                <div className="text-[13.5px] font-semibold text-ink">{s.title}</div>
                <div className="text-[12px] text-ink-3 mt-0.5 line-clamp-1">{s.short}</div>
              </button>
            )
          })}
        </div>
      </div>

      {activeStep && (
        <div className="mt-3.5 px-3.5 py-2.5 rounded-[var(--radius-control)] border border-line-soft bg-bg-sunk/70 text-[12.5px] flex flex-wrap items-center justify-between gap-2">
          <div className="text-ink-2">
            <span className="mono font-semibold text-accent-ink mr-2">
              STEP 0{activeStep.step} · {activeStep.title}
            </span>
            <span>{activeStep.short}</span>
          </div>
          <span className="chip">
            {activeStep.phase === 'frontstage'
              ? '触发点：Codex / Claude Code / Claude MCP'
              : '触发点：ai-flywheel 后台 P00–P07 引擎'}
          </span>
        </div>
      )}
    </section>
  )
}
