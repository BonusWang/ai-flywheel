import React, { useState } from 'react'
import { ArrowRight, RefreshCw } from 'lucide-react'
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
    <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
        <div className="flex items-center gap-2">
          <span className="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-blue-50 text-blue-600">
            <RefreshCw className="w-4 h-4" />
          </span>
          <div>
            <h2 className="text-sm font-semibold text-slate-900">8 步 AI 协作飞轮闭环</h2>
            <p className="text-xs text-slate-500">前台执行（MCP + 薄 Hook）↔ 后台自主加工（P00–P07 流水线）</p>
          </div>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <span className="inline-flex items-center gap-1.5 text-blue-700">
            <span className="w-2 h-2 rounded-full bg-blue-500" />
            前台协作 1–4
          </span>
          <span className="inline-flex items-center gap-1.5 text-emerald-700">
            <span className="w-2 h-2 rounded-full bg-emerald-500" />
            后台进化 5–8
          </span>
        </div>
      </div>

      <div className="space-y-3">
        {/* Frontstage row */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5">
          {frontstage.map((s) => {
            const isSel = selected === s.step
            return (
              <button
                key={s.step}
                onClick={() => setSelected(s.step)}
                className={`text-left p-3 rounded-xl border transition-all cursor-pointer ${
                  isSel
                    ? 'bg-blue-50/70 border-blue-400 ring-2 ring-blue-100'
                    : 'bg-slate-50/60 border-slate-200 hover:border-blue-300'
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[11px] font-mono font-semibold text-blue-600">
                    STEP 0{s.step}
                  </span>
                  <ArrowRight className="w-3.5 h-3.5 text-slate-400" />
                </div>
                <div className="text-sm font-semibold text-slate-900">{s.title}</div>
                <div className="text-xs text-slate-500 mt-0.5 line-clamp-1">{s.short}</div>
              </button>
            )
          })}
        </div>

        {/* Backstage row */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-2.5">
          {backstage.map((s) => {
            const isSel = selected === s.step
            return (
              <button
                key={s.step}
                onClick={() => setSelected(s.step)}
                className={`text-left p-3 rounded-xl border transition-all cursor-pointer ${
                  isSel
                    ? 'bg-emerald-50/70 border-emerald-400 ring-2 ring-emerald-100'
                    : 'bg-slate-50/60 border-slate-200 hover:border-emerald-300'
                }`}
              >
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[11px] font-mono font-semibold text-emerald-700">
                    STEP 0{s.step}
                  </span>
                  <ArrowRight className="w-3.5 h-3.5 text-slate-400" />
                </div>
                <div className="text-sm font-semibold text-slate-900">{s.title}</div>
                <div className="text-xs text-slate-500 mt-0.5 line-clamp-1">{s.short}</div>
              </button>
            )
          })}
        </div>
      </div>

      {activeStep && (
        <div className="mt-3.5 px-3.5 py-2.5 rounded-xl bg-slate-900 text-slate-100 text-xs flex flex-wrap items-center justify-between gap-2">
          <div>
            <span className="font-mono text-blue-300 mr-2">0{activeStep.step} · {activeStep.title}</span>
            <span className="text-slate-300">{activeStep.short}</span>
          </div>
          <span className="text-[11px] px-2 py-0.5 rounded bg-slate-800 text-slate-300">
            {activeStep.phase === 'frontstage' ? '触发点：Codex / Claude Code / Claude MCP' : '触发点：ai-flywheel 后台 P00–P07 引擎'}
          </span>
        </div>
      )}
    </div>
  )
}
