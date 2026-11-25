import React, { useEffect, useImperativeHandle, useState } from 'react'
import { api } from '../../api'
import { labelOf, POLICY_SOURCE_LABELS } from '../../lib/labels'

/** The two header actions the page triggers on the policy form. */
export interface PolicyStudioActions {
  save: () => Promise<void>
  reset: () => Promise<void>
}

/**
 * The "执行规矩" tab: anti-laziness baseline and execution rules, the personal
 * address form and red lines, and the retry breaker.
 *
 * The form state lives here and is re-synced from the loaded policies on every
 * reload. The page header keeps the "保存修改 / 恢复默认" buttons; they reach this
 * form through `actionsRef`, so the action bar does not move.
 */
export const PolicyStudioTab: React.FC<{
  policies: Record<string, any>
  onChanged: () => Promise<void>
  onToast: (msg: string) => void
  actionsRef: React.RefObject<PolicyStudioActions | null>
}> = ({ policies, onChanged, onToast, actionsRef }) => {
  const [antiEnabled, setAntiEnabled] = useState(true)
  const [baselineText, setBaselineText] = useState('')
  const [execText, setExecText] = useState('')
  const [addressAs, setAddressAs] = useState('')
  const [redlinesText, setRedlinesText] = useState('')
  const [retryMax, setRetryMax] = useState(2)

  useEffect(() => {
    const al = policies.anti_laziness || {}
    setAntiEnabled(al.enabled !== false)
    setBaselineText((al.baseline?.rules || []).join('\n'))
    setExecText((al.execution?.rules || []).join('\n'))

    const up = policies.user_preferences || {}
    setAddressAs(up.address_as || '')
    setRedlinesText((up.non_negotiables || []).join('\n'))

    const hg = policies.hard_gates || {}
    setRetryMax(Number(hg.retry_breaker?.max_same_cause_retries ?? 2))
  }, [policies])

  const savePolicies = async () => {
    await Promise.all([
      api.updatePolicy('anti_laziness', {
        enabled: antiEnabled,
        baseline: { rules: baselineText.split('\n').map((s) => s.trim()).filter(Boolean) },
        execution: { rules: execText.split('\n').map((s) => s.trim()).filter(Boolean) },
      }),
      api.updatePolicy('user_preferences', {
        address_as: addressAs.trim() || 'Developer',
        non_negotiables: redlinesText.split('\n').map((s) => s.trim()).filter(Boolean),
      }),
      api.updatePolicy('hard_gates', {
        retry_breaker: {
          max_same_cause_retries: retryMax,
          alert_at_attempt: retryMax + 1,
        },
      }),
    ])
    await onChanged()
    onToast('规矩已保存，下次对话立即生效')
  }

  const resetAllPolicies = async () => {
    await Promise.all([
      api.resetPolicy('anti_laziness'),
      api.resetPolicy('user_preferences'),
      api.resetPolicy('hard_gates'),
    ])
    await onChanged()
    onToast('已恢复默认设置')
  }

  useImperativeHandle(actionsRef, () => ({ save: savePolicies, reset: resetAllPolicies }))

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 text-[12.5px]">
      <section className="card p-5 space-y-3.5">
        <div className="flex items-center justify-between pb-2.5 border-b border-line-soft">
          <div>
            <h2 className="text-[14.5px] font-bold text-ink">AI 干活的基本要求</h2>
            <span className="text-[11.5px] text-ink-4">
              当前来源：{labelOf(POLICY_SOURCE_LABELS, policies.anti_laziness?._source, '系统默认')}
            </span>
          </div>
          <label className="inline-flex items-center gap-2 cursor-pointer text-ink-2">
            <input
              type="checkbox"
              checked={antiEnabled}
              onChange={(e) => setAntiEnabled(e.target.checked)}
              className="accent-accent"
            />
            <span>开启</span>
          </label>
        </div>
        <div>
          <label className="block text-ink-3 mb-1.5">每次对话都要遵守（每条一行）</label>
          <textarea
            rows={4}
            value={baselineText}
            onChange={(e) => setBaselineText(e.target.value)}
            className="field-input w-full p-2.5 text-[12.5px] leading-relaxed"
          />
        </div>
        <div>
          <label className="block text-ink-3 mb-1.5">写代码、排障、部署时额外遵守（每条一行）</label>
          <textarea
            rows={7}
            value={execText}
            onChange={(e) => setExecText(e.target.value)}
            className="field-input w-full p-2.5 text-[12.5px] leading-relaxed"
          />
        </div>
      </section>

      <section className="card p-5 space-y-3.5">
        <div className="pb-2.5 border-b border-line-soft">
          <h2 className="text-[14.5px] font-bold text-ink">个人称呼与红线</h2>
          <span className="text-[11.5px] text-ink-4">
            当前来源：{labelOf(POLICY_SOURCE_LABELS, policies.user_preferences?._source, '系统默认')}
          </span>
        </div>
        <div>
          <label className="block text-ink-3 mb-1.5">AI 怎么称呼你</label>
          <input
            value={addressAs}
            onChange={(e) => setAddressAs(e.target.value)}
            className="field-input w-full px-3 py-2 text-[13px]"
          />
        </div>
        <div>
          <label className="block text-ink-3 mb-1.5">绝对不能碰的红线（每条一行）</label>
          <textarea
            rows={8}
            value={redlinesText}
            onChange={(e) => setRedlinesText(e.target.value)}
            className="field-input w-full p-2.5 text-[12.5px] leading-relaxed"
          />
        </div>
        <div>
          <label className="block text-ink-3 mb-1.5">
            同一个错最多重试几次后停下提醒
          </label>
          <input
            type="number"
            min={1}
            max={5}
            value={retryMax}
            onChange={(e) => setRetryMax(Number(e.target.value))}
            className="field-input num w-24 px-3 py-1.5"
          />
        </div>
      </section>
    </div>
  )
}
