import React, { useState } from 'react'
import { Loader2, Zap } from 'lucide-react'
import { api } from '../../api'
import { errorMessage } from '../../lib/errors'
import { ANTI_LAZINESS_MODE_LABELS, labelOf } from '../../lib/labels'
import type { PreflightResponse } from '../../types'
import { Collapse } from '../ui/Collapse'
import { useToast } from '../ui/Toast'

const TEST_PRESETS = [
  { label: '发布前核对配置', task: '准备发布上线，先核对环境配置并跑全量回归测试' },
  { label: '排查接口超时', task: '排查线上接口偶发超时并补充重试与单测证据' },
  { label: '批量数据库变更', task: '执行大表历史数据批量更新，注意分批提交与回滚保护' },
]

interface Props {
  onGlobalRefresh: () => void
}

/** Tab 3 of ConnectPolicyPage: interactive preflight activation simulator. */
export const ConnectSandboxTab: React.FC<Props> = ({ onGlobalRefresh }) => {
  const toast = useToast()
  const [pfPrompt, setPfPrompt] = useState('修复接口报错并做真实验证')
  const [pfProject, setPfProject] = useState('')
  const [pfResult, setPfResult] = useState<PreflightResponse | null>(null)
  const [pfLoading, setPfLoading] = useState(false)

  const executePreflight = async (overrideTask?: string) => {
    const promptText = (overrideTask ?? pfPrompt).trim()
    if (!promptText || pfLoading) return
    setPfLoading(true)
    try {
      const res = await api.preflight({
        task: promptText,
        project: pfProject.trim() || 'generic',
        client: 'portal-sandbox',
      })
      setPfResult(res)
      onGlobalRefresh()
    } catch (e: unknown) {
      toast.push({ tone: 'error', message: errorMessage(e, '模拟测试失败') })
    } finally {
      setPfLoading(false)
    }
  }

  return (
    <div key="tab-test" className="animate-tab-enter max-w-2xl card p-5 space-y-4">
      <div className="flex items-center gap-2">
        <Zap className="w-4 h-4 text-accent" />
        <h2 className="text-[15px] font-bold text-ink">模拟一次开局提醒</h2>
      </div>
      <p className="text-[13px] text-ink-3">
        输入你准备交给 AI 的任务，立即预览系统会自动注入哪些执行规矩与避坑规则。
      </p>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[11.5px] text-ink-4">试试常见场景：</span>
        {TEST_PRESETS.map((p) => (
          <button
            key={p.label}
            type="button"
            onClick={() => {
              setPfPrompt(p.task)
              executePreflight(p.task)
            }}
            className="pressable chip hover:border-accent/50 hover:text-ink"
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="space-y-2.5">
        <input
          value={pfProject}
          onChange={(e) => setPfProject(e.target.value)}
          placeholder="项目名（选填，留空则匹配全局通用规则）"
          className="field-input w-full px-3 py-2 text-[13px]"
        />
        <textarea
          rows={3}
          value={pfPrompt}
          onChange={(e) => setPfPrompt(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
              e.preventDefault()
              executePreflight()
            }
          }}
          placeholder="输入你想测试的任务描述（Cmd/Ctrl + Enter 运行）…"
          className="field-input w-full px-3 py-2 text-[13px]"
        />
        <button
          type="button"
          onClick={() => executePreflight()}
          disabled={pfLoading || !pfPrompt.trim()}
          className="pressable inline-flex items-center gap-1.5 h-9 px-4 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[13px] font-medium hover:bg-accent-ink disabled:opacity-50"
        >
          {pfLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5" />}
          <span>{pfLoading ? '正在模拟…' : '运行模拟测试'}</span>
        </button>
      </div>
      <Collapse open={Boolean(pfResult)}>
        {pfResult && (
          <div className="pt-3 border-t border-line-soft space-y-2">
            <div className="text-[12px] text-ink-4">
              匹配模式：
              <strong className="text-ink">
                {labelOf(
                  ANTI_LAZINESS_MODE_LABELS,
                  pfResult.anti_laziness_mode,
                  pfResult.anti_laziness_mode
                )}
              </strong>{' '}
              · 命中避坑规则{' '}
              <strong className="num text-ink">{pfResult.matched_rules.length}</strong> 条
            </div>
            <pre className="mono text-[12px] leading-[1.65] bg-bg-sunk border border-line-soft rounded-[var(--radius-control)] p-3.5 whitespace-pre-wrap max-h-80 overflow-y-auto text-ink-2">
              {pfResult.compact_context}
            </pre>
          </div>
        )}
      </Collapse>
    </div>
  )
}
