import React, { useCallback, useEffect, useImperativeHandle, useMemo, useState } from 'react'
import { api } from '../../api'
import { labelOf, POLICY_SOURCE_LABELS } from '../../lib/labels'
import { Switch } from '../ui/Switch'
import { useToast } from '../ui/Toast'

export interface PolicyStudioActions {
  save: () => Promise<void>
  reset: () => Promise<void>
  discard: () => void
}

interface PolicySnapshot {
  antiEnabled: boolean
  baselineText: string
  execText: string
  addressAs: string
  redlinesText: string
  retryMax: number
}

function extractSnapshot(policies: Record<string, any>): PolicySnapshot {
  const al = policies.anti_laziness || {}
  const up = policies.user_preferences || {}
  const hg = policies.hard_gates || {}
  return {
    antiEnabled: al.enabled !== false,
    baselineText: (al.baseline?.rules || []).join('\n'),
    execText: (al.execution?.rules || []).join('\n'),
    addressAs: up.address_as || '',
    redlinesText: (up.non_negotiables || []).join('\n'),
    retryMax: Number(hg.retry_breaker?.max_same_cause_retries ?? 2),
  }
}

/**
 * The "执行规矩" tab:
 * - Protects unsaved user edits from background SSE refreshes (`!isDirty` guard).
 * - Reports dirty & busy state upward so the header save button reflects reality.
 * - Supports Cmd/Ctrl + S to save immediately.
 */
export const PolicyStudioTab: React.FC<{
  policies: Record<string, any>
  onChanged: () => Promise<void>
  onDirtyChange?: (dirty: boolean) => void
  onBusyChange?: (busy: boolean) => void
  actionsRef: React.RefObject<PolicyStudioActions | null>
}> = ({ policies, onChanged, onDirtyChange, onBusyChange, actionsRef }) => {
  const toast = useToast()
  const [antiEnabled, setAntiEnabled] = useState(true)
  const [baselineText, setBaselineText] = useState('')
  const [execText, setExecText] = useState('')
  const [addressAs, setAddressAs] = useState('')
  const [redlinesText, setRedlinesText] = useState('')
  const [retryMax, setRetryMax] = useState(2)
  const [saving, setSaving] = useState(false)

  const serverSnap = useMemo(() => extractSnapshot(policies), [policies])
  const [baseline, setBaseline] = useState<PolicySnapshot | null>(null)

  const isDirty = useMemo(() => {
    if (!baseline) return false
    return (
      antiEnabled !== baseline.antiEnabled ||
      baselineText !== baseline.baselineText ||
      execText !== baseline.execText ||
      addressAs !== baseline.addressAs ||
      redlinesText !== baseline.redlinesText ||
      retryMax !== baseline.retryMax
    )
  }, [baseline, antiEnabled, baselineText, execText, addressAs, redlinesText, retryMax])

  const applySnapshot = useCallback((snap: PolicySnapshot) => {
    setBaseline(snap)
    setAntiEnabled(snap.antiEnabled)
    setBaselineText(snap.baselineText)
    setExecText(snap.execText)
    setAddressAs(snap.addressAs)
    setRedlinesText(snap.redlinesText)
    setRetryMax(snap.retryMax)
  }, [])

  // Only sync server snapshot into local form when there are NO unsaved edits
  useEffect(() => {
    if (Object.keys(policies).length === 0) return
    if (!baseline || !isDirty) {
      applySnapshot(serverSnap)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverSnap])

  useEffect(() => {
    onDirtyChange?.(isDirty)
  }, [isDirty, onDirtyChange])

  useEffect(() => {
    onBusyChange?.(saving)
  }, [saving, onBusyChange])

  const savePolicies = useCallback(async () => {
    if (saving) return
    setSaving(true)
    try {
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
      setBaseline({
        antiEnabled,
        baselineText,
        execText,
        addressAs,
        redlinesText,
        retryMax,
      })
      await onChanged()
      toast.push('规矩已保存，下次对话立即生效')
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '保存规矩失败' })
    } finally {
      setSaving(false)
    }
  }, [saving, antiEnabled, baselineText, execText, addressAs, redlinesText, retryMax, onChanged, toast])

  const resetAllPolicies = useCallback(async () => {
    if (saving) return
    setSaving(true)
    try {
      await Promise.all([
        api.resetPolicy('anti_laziness'),
        api.resetPolicy('user_preferences'),
        api.resetPolicy('hard_gates'),
      ])
      setBaseline(null)
      await onChanged()
      toast.push('已恢复系统默认规矩')
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '恢复默认失败' })
    } finally {
      setSaving(false)
    }
  }, [saving, onChanged, toast])

  const discardChanges = useCallback(() => {
    applySnapshot(serverSnap)
    toast.push({ tone: 'info', message: '已放弃未保存的修改' })
  }, [applySnapshot, serverSnap, toast])

  useImperativeHandle(
    actionsRef,
    () => ({ save: savePolicies, reset: resetAllPolicies, discard: discardChanges }),
    [savePolicies, resetAllPolicies, discardChanges]
  )

  return (
    <div
      onKeyDown={(e) => {
        if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
          e.preventDefault()
          if (isDirty) savePolicies()
        }
      }}
      className="grid grid-cols-1 lg:grid-cols-2 gap-5 text-[12.5px]"
    >
      <section className="card p-5 space-y-3.5">
        <div className="flex items-center justify-between pb-2.5 border-b border-line-soft">
          <div>
            <h2 className="text-[14.5px] font-bold text-ink">AI 干活的基本要求</h2>
            <span className="text-[11.5px] text-ink-4">
              当前来源：{labelOf(POLICY_SOURCE_LABELS, policies.anti_laziness?._source, '系统默认')}
            </span>
          </div>
          <div className="inline-flex items-center gap-2 text-ink-2">
            <span className="text-[12px] text-ink-3">{antiEnabled ? '已开启' : '已关闭'}</span>
            <Switch
              size="sm"
              checked={antiEnabled}
              onChange={setAntiEnabled}
              ariaLabel="开启 AI 干活基本要求"
            />
          </div>
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
            placeholder="例如：Developer"
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
          <label className="block text-ink-3 mb-1.5">同一个错最多重试几次后停下提醒</label>
          <input
            type="number"
            min={1}
            max={5}
            value={retryMax}
            onChange={(e) => setRetryMax(Math.max(1, Math.min(5, Number(e.target.value) || 1)))}
            className="field-input num w-24 px-3 py-1.5"
          />
        </div>
      </section>
    </div>
  )
}
