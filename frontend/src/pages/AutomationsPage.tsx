import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ArrowRight,
  ArrowUp,
  Bell,
  Calendar,
  Check,
  Clock,
  Loader2,
  Play,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { api } from '../api'
import type { PageKey } from '../App'
import { PillTabs } from '../components/PageLayout'
import { StatusBadge } from '../components/StatusBadge'
import {
  APPROVAL_MODE_LABELS,
  FEISHU_LISTENER_STATE_LABELS,
  formatDuration,
  formatRunnerMode,
  labelOf,
  PHASE_LABELS,
  TRIGGER_LABELS,
} from '../lib/labels'
import type { AutomationsSnapshot } from '../types'

interface Props {
  refreshToken: number
  onNavigate: (page: PageKey) => void
  onGlobalRefresh: () => void
}

type CronPreset = 'daily' | 'weekdays' | 'friday' | 'custom'
type SectionKey = 'scheduler' | 'daily' | 'daemons' | 'history'

function parseCronToVisual(expr: string): {
  preset: CronPreset
  hour: string
  minute: string
} {
  const parts = expr.trim().split(/\s+/)
  if (parts.length === 5 && /^\d{1,2}$/.test(parts[0]) && /^\d{1,2}$/.test(parts[1])) {
    const minute = String(Number(parts[0])).padStart(2, '0')
    const hour = String(Number(parts[1])).padStart(2, '0')
    if (parts[2] === '*' && parts[3] === '*') {
      const dow = parts[4].toLowerCase()
      if (dow === '*') return { preset: 'daily', hour, minute }
      if (dow === '1-5' || dow === 'mon-fri') return { preset: 'weekdays', hour, minute }
      if (dow === '5' || dow === 'fri') return { preset: 'friday', hour, minute }
    }
  }
  return { preset: 'custom', hour: '20', minute: '00' }
}

function buildCronFromVisual(preset: CronPreset, hour: string, minute: string, fallback: string): string {
  const m = String(Number(minute) || 0)
  const h = String(Number(hour) || 0)
  if (preset === 'daily') return `${m} ${h} * * *`
  if (preset === 'weekdays') return `${m} ${h} * * 1-5`
  if (preset === 'friday') return `${m} ${h} * * 5`
  return fallback
}

function formatShanghaiTimestamp(iso: string | null | undefined): string {
  if (!iso) return '暂无'
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return iso
  const fmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
  return fmt.format(dt).replace(/\//g, '-')
}

const HOURS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'))
const MINUTES = ['00', '05', '10', '15', '20', '25', '30', '35', '40', '45', '50', '55']

export const AutomationsPage: React.FC<Props> = ({
  refreshToken,
  onNavigate,
  onGlobalRefresh,
}) => {
  const [data, setData] = useState<AutomationsSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [activeSection, setActiveSection] = useState<SectionKey>('scheduler')
  const [historyFilter, setHistoryFilter] = useState<'all' | 'scheduled'>('all')

  // Visual Cron editor state
  const [cronDraft, setCronDraft] = useState('0 20 * * *')
  const [preset, setPreset] = useState<CronPreset>('daily')
  const [hour, setHour] = useState('20')
  const [minute, setMinute] = useState('00')
  const [savingSchedule, setSavingSchedule] = useState(false)
  const [togglingScheduler, setTogglingScheduler] = useState(false)
  const [runningPhase, setRunningPhase] = useState<string | null>(null)
  const [recovering, setRecovering] = useState(false)

  const syncDraftFromSnapshot = useCallback((snap: AutomationsSnapshot) => {
    const expr = snap.scheduler.cron || '0 20 * * *'
    setCronDraft(expr)
    const parsed = parseCronToVisual(expr)
    setPreset(parsed.preset)
    setHour(parsed.hour)
    setMinute(parsed.minute)
  }, [])

  const load = useCallback(
    async (syncEditor = false) => {
      try {
        const snap = await api.automations()
        setData(snap)
        if (syncEditor) {
          syncDraftFromSnapshot(snap)
        }
        setError(null)
      } catch (e: any) {
        setError(e?.message || '加载自动化任务状态失败')
      } finally {
        setLoading(false)
      }
    },
    [syncDraftFromSnapshot]
  )

  useEffect(() => {
    load(true)
  }, [load])

  useEffect(() => {
    if (refreshToken > 0) {
      load(false)
    }
  }, [refreshToken, load])

  const scrollToSection = (key: SectionKey) => {
    setActiveSection(key)
    const el = document.getElementById(`automation-sec-${key}`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }

  const handlePresetChange = (nextPreset: CronPreset) => {
    setPreset(nextPreset)
    if (nextPreset !== 'custom') {
      setCronDraft(buildCronFromVisual(nextPreset, hour, minute, cronDraft))
    }
  }

  const handleHourChange = (nextHour: string) => {
    setHour(nextHour)
    const nextPreset = preset === 'custom' ? 'daily' : preset
    setPreset(nextPreset)
    setCronDraft(buildCronFromVisual(nextPreset, nextHour, minute, cronDraft))
  }

  const handleMinuteChange = (nextMinute: string) => {
    setMinute(nextMinute)
    const nextPreset = preset === 'custom' ? 'daily' : preset
    setPreset(nextPreset)
    setCronDraft(buildCronFromVisual(nextPreset, hour, nextMinute, cronDraft))
  }

  const handleCronInput = (val: string) => {
    setCronDraft(val)
    const parsed = parseCronToVisual(val)
    setPreset(parsed.preset)
    if (parsed.preset !== 'custom') {
      setHour(parsed.hour)
      setMinute(parsed.minute)
    }
  }

  const handleToggleAuto = async () => {
    if (!data || togglingScheduler) return
    setTogglingScheduler(true)
    setError(null)
    setNotice(null)
    try {
      const nextEnabled = !data.scheduler.enabled
      const res = await api.updateAutomationSchedule({ schedule_enabled: nextEnabled })
      setData(res.automations)
      setNotice(
        nextEnabled
          ? `后台定时调度器已开启（${res.automations.scheduler.human_schedule}），立即生效无需重启。`
          : '后台定时调度器已关闭；自动触发暂停，手动按钮仍可正常执行。'
      )
      onGlobalRefresh()
    } catch (e: any) {
      setError(e?.message || '切换自动调度失败')
    } finally {
      setTogglingScheduler(false)
    }
  }

  const handleSaveCron = async () => {
    if (!data || savingSchedule) return
    setSavingSchedule(true)
    setError(null)
    setNotice(null)
    try {
      const res = await api.updateAutomationSchedule({ daily_cron: cronDraft.trim() })
      setData(res.automations)
      syncDraftFromSnapshot(res.automations)
      setNotice(
        `定时规则已热更新为「${res.automations.scheduler.human_schedule}」（${res.automations.scheduler.cron}），无需重启服务。`
      )
      onGlobalRefresh()
    } catch (e: any) {
      setError(e?.message || '保存 Cron 表达式失败')
    } finally {
      setSavingSchedule(false)
    }
  }

  const handleToggleFridayWeekly = async () => {
    if (!data || savingSchedule) return
    setSavingSchedule(true)
    setError(null)
    setNotice(null)
    try {
      const nextVal = !data.scheduler.friday_weekly_enabled
      const res = await api.updateAutomationSchedule({ friday_weekly_enabled: nextVal })
      setData(res.automations)
      setNotice(
        nextVal
          ? '已开启周五自动并入周度 Dream 复盘。'
          : '已关闭周五自动并入周度复盘（仍可手动执行周度复盘）。'
      )
      onGlobalRefresh()
    } catch (e: any) {
      setError(e?.message || '切换周度复盘设置失败')
    } finally {
      setSavingSchedule(false)
    }
  }

  const handleManualRun = async (phase: 'auto' | 'daily' | 'weekly') => {
    if (runningPhase) return
    setRunningPhase(phase)
    setError(null)
    setNotice(null)
    try {
      const res = await api.startRun({ phase }, { background: true })
      const rid = res.run_id || res.run?.run_id || ''
      setNotice(`已触发「${labelOf(PHASE_LABELS, phase)}」沉淀任务${rid ? `（${rid}）` : ''}，后台执行中。`)
      onGlobalRefresh()
      await load(false)
    } catch (e: any) {
      setError(e?.message || '触发沉淀任务失败')
    } finally {
      setRunningPhase(null)
    }
  }

  const handleRecoveryNow = async () => {
    if (recovering) return
    setRecovering(true)
    setError(null)
    setNotice(null)
    try {
      const res = await api.triggerAutomationRecovery()
      setData(res.automations)
      setNotice(
        res.reaped_count > 0
          ? `巡检完成：已自动收割 ${res.reaped_count} 个陈旧中断批次并释放独占锁。`
          : '巡检完成：当前无心跳超时的中断批次，独占锁与审批卡状态正常。'
      )
      onGlobalRefresh()
    } catch (e: any) {
      setError(e?.message || '执行守护巡检失败')
    } finally {
      setRecovering(false)
    }
  }

  const cronDirty = useMemo(() => {
    if (!data) return false
    return cronDraft.trim() !== (data.scheduler.cron || '').trim()
  }, [cronDraft, data])

  const filteredRuns = useMemo(() => {
    if (!data) return []
    if (historyFilter === 'scheduled') {
      return data.recent_runs.filter((r) => r.trigger === 'scheduled')
    }
    return data.recent_runs
  }, [data, historyFilter])

  if (loading && !data) {
    return (
      <div className="mx-auto max-w-[var(--page-max-wide)] py-12 flex items-center justify-center gap-2 text-ink-3 text-[13.5px]">
        <Loader2 className="w-4 h-4 animate-spin text-accent" />
        <span>正在加载自动化任务状态…</span>
      </div>
    )
  }

  const sched = data?.scheduler
  const metrics = data?.metrics
  const policy = data?.policy
  const recovery = data?.daemons.recovery
  const feishu = data?.daemons.feishu
  const isRunning = Boolean(sched?.running)
  const isLocked = Boolean(data?.lock)

  const todayHeadline = (() => {
    if (!metrics) return '—'
    if (metrics.today_awaiting_count > 0) {
      return `待审批 (${metrics.today_awaiting_count} 批)`
    }
    if (metrics.today_passed_count > 0) {
      return `今日已沉淀 (${metrics.today_passed_count}/${metrics.today_runs_count})`
    }
    if (metrics.today_failed_count > 0) {
      return `今日异常 (${metrics.today_failed_count} 批)`
    }
    return sched?.enabled ? `今日待触发 · ${sched.human_schedule.replace('（上海时间）', '')}` : '今日未执行（自动已暂停）'
  })()

  const todayColorClass = (() => {
    if (!metrics) return 'text-ink'
    if (metrics.today_awaiting_count > 0) return 'text-amber-ink'
    if (metrics.today_passed_count > 0) return 'text-ok'
    if (metrics.today_failed_count > 0) return 'text-hot'
    return 'text-ink-2'
  })()

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5 pb-12">
      {/* Page Header */}
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-bold tracking-tight text-ink">自动化任务</h1>
          <p className="mt-1 text-[13px] text-ink-3">
            后台调度器、每日定时沉淀批、守护巡检与自动化执行记录（统一上海时区 Asia/Shanghai）。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] font-medium ${
              isRunning
                ? 'border-ok/30 bg-ok-soft text-ok-ink'
                : 'border-line bg-surface text-ink-3'
            }`}
          >
            <span
              className={`size-2 rounded-full ${isRunning ? 'bg-ok' : 'bg-ink-4'}`}
            />
            <span>{isRunning ? `调度运行中 · ${sched?.human_schedule}` : '自动调度已暂停'}</span>
          </span>
          <button
            type="button"
            onClick={() => load(false)}
            className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] border border-line bg-surface px-3 py-1.5 text-[12.5px] font-medium text-ink-2 hover:bg-bg-sunk transition-colors"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            <span>刷新</span>
          </button>
        </div>
      </header>

      {/* Section Anchor Tabs + Back to Top (aligned with reference layout) */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PillTabs
          items={[
            { key: 'scheduler', label: '后台调度器' },
            { key: 'daily', label: '每日与周期任务' },
            { key: 'daemons', label: '后台守护巡检' },
            { key: 'history', label: '执行记录', count: data?.recent_runs.length ?? 0 },
          ]}
          active={activeSection}
          onChange={(k) => scrollToSection(k as SectionKey)}
        />
        <button
          type="button"
          onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
          className="inline-flex items-center gap-1 rounded-full border border-line bg-surface px-3 py-1 text-[12px] text-ink-3 hover:text-ink transition-colors"
        >
          <ArrowUp className="w-3.5 h-3.5" />
          <span>回到顶部</span>
        </button>
      </div>

      {/* Feedback banners */}
      {error && (
        <div className="rounded-[var(--radius-card)] border border-hot/30 bg-hot-soft px-4 py-2.5 text-[13px] text-hot">
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-[var(--radius-card)] border border-ok/30 bg-ok-soft px-4 py-2.5 text-[13px] text-ok-ink flex items-center gap-2">
          <Check className="w-4 h-4 shrink-0" />
          <span>{notice}</span>
        </div>
      )}
      {policy && !policy.workflow_enabled && (
        <div className="rounded-[var(--radius-card)] border border-amber/30 bg-amber-soft px-4 py-2.5 text-[13px] text-amber-ink">
          安全阀 <code className="font-mono">safety_valves.workflow_enabled</code> 当前为关闭状态：定时与手动沉淀任务均会被阻止，请先在「接入与设置」中开启。
        </div>
      )}

      {/* Card 1: Background Scheduler Master Control */}
      <section
        id="automation-sec-scheduler"
        className="card p-5 sm:p-6 space-y-4 scroll-mt-6"
      >
        <div>
          <h2 className="text-[16px] font-semibold text-ink">后台调度器</h2>
          <p className="mt-1 text-[12.5px] text-ink-3">
            定时沉淀、周五周度复盘与守护巡检；关闭后不再自动执行，手动按钮仍可用（修改即时热生效，无需重启服务）。
          </p>
        </div>

        {/* Toggle Switch Row */}
        <div className="flex items-center gap-3">
          <span className="text-[13px] font-medium text-ink-2">自动执行</span>
          <button
            type="button"
            role="switch"
            aria-checked={Boolean(sched?.enabled)}
            disabled={togglingScheduler}
            onClick={handleToggleAuto}
            className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full border transition-colors ${
              sched?.enabled
                ? 'border-accent bg-accent'
                : 'border-line-strong bg-bg-muted'
            } disabled:opacity-60`}
          >
            <span
              className={`inline-block size-4.5 rounded-full bg-white shadow transition-transform ${
                sched?.enabled ? 'translate-x-5.5' : 'translate-x-0.5'
              }`}
            />
          </button>
          <span className="text-[12px] text-ink-4">
            {togglingScheduler
              ? '正在切换…'
              : sched?.enabled
              ? '已启用（到点自动运行 P00–P07）'
              : '已暂停（仅保留手动触发）'}
          </span>
        </div>

        {/* Live Status Strip */}
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-tile)] border border-line-soft bg-bg-sunk/70 px-4 py-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px]">
            <span className="inline-flex items-center gap-2 font-semibold text-ink">
              <span
                className={`size-2 rounded-full ${
                  isRunning ? 'bg-ok' : sched?.enabled ? 'bg-amber' : 'bg-ink-4'
                }`}
              />
              <span>
                后台定时调度器：
                {isRunning
                  ? '运行中'
                  : sched?.enabled && !sched.role_active
                  ? '非 serve 模式（仅配置）'
                  : '已暂停'}
              </span>
            </span>
            <span className="text-ink-3">
              · {sched?.human_schedule}
              {sched?.friday_weekly_enabled ? '（含周五周度复盘）' : ''} + {Math.round(recovery?.interval_seconds ?? 30)}s 中断收割
              {feishu?.enabled ? ' + 飞书通道' : ''}
            </span>
            <span className="num text-ink-4">
              上次：{formatShanghaiTimestamp(sched?.last_scheduled_run_at || sched?.last_run_at)} · 下次：
              {sched?.next_run_at
                ? formatShanghaiTimestamp(sched.next_run_at)
                : sched?.preview_next_run_at
                ? `暂停中（开启后 ${formatShanghaiTimestamp(sched.preview_next_run_at)}）`
                : '未排程'}
            </span>
          </div>
          <button
            type="button"
            disabled={recovering}
            onClick={handleRecoveryNow}
            className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] border border-line bg-surface px-3 py-1.5 text-[12.5px] font-medium text-ink-2 hover:bg-bg-muted transition-colors disabled:opacity-60"
          >
            {recovering ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <ShieldCheck className="w-3.5 h-3.5 text-accent" />
            )}
            <span>立即巡检收割</span>
          </button>
        </div>
      </section>

      {/* Card 2: Daily & Periodic Tasks */}
      <section
        id="automation-sec-daily"
        className="card p-5 sm:p-6 space-y-5 scroll-mt-6"
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line-soft pb-4">
          <div>
            <h2 className="text-[16px] font-semibold text-ink">每日与周期任务</h2>
            <p className="mt-1 text-[12.5px] text-ink-3">
              每日自动沉淀与周五周度 Dream 复盘；按上海时间排程，支持可视化调整 Cron 并即时热生效，也可一键手动触发。
            </p>
          </div>
          <button
            type="button"
            disabled={Boolean(runningPhase) || isLocked}
            onClick={() => handleManualRun('auto')}
            className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] bg-accent px-4 py-2 text-[13px] font-semibold text-accent-contrast hover:opacity-95 transition-opacity disabled:opacity-50"
          >
            {runningPhase === 'auto' ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Play className="w-3.5 h-3.5" />
            )}
            <span>{isLocked ? '已有批次执行中' : '一键执行自动沉淀'}</span>
          </button>
        </div>

        {/* Sub-block: Daily Automatic Sedimentation */}
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 className="text-[14.5px] font-semibold text-ink flex items-center gap-2">
                <Clock className="w-4 h-4 text-accent" />
                <span>每日自动沉淀批（P00–P07）</span>
              </h3>
              <p className="mt-0.5 text-[12.5px] text-ink-3">
                自动扫描会话、过滤噪音、调用模型审查与路由规划，按「{labelOf(APPROVAL_MODE_LABELS, policy?.effective_approval_mode || 'smart')}」策略受控落盘。
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={Boolean(runningPhase) || isLocked}
                onClick={() => handleManualRun('daily')}
                className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] bg-accent/12 border border-accent/25 px-3.5 py-1.5 text-[12.5px] font-semibold text-accent-ink hover:bg-accent/18 transition-colors disabled:opacity-50"
              >
                {runningPhase === 'daily' ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Play className="w-3.5 h-3.5" />
                )}
                <span>立即执行每日批</span>
              </button>
              <button
                type="button"
                disabled={Boolean(runningPhase) || isLocked}
                onClick={() => handleManualRun('weekly')}
                className="inline-flex items-center gap-1.5 rounded-[var(--radius-control)] border border-line bg-surface px-3.5 py-1.5 text-[12.5px] font-medium text-ink-2 hover:bg-bg-sunk transition-colors disabled:opacity-50"
              >
                {runningPhase === 'weekly' ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Sparkles className="w-3.5 h-3.5 text-amber" />
                )}
                <span>立即执行周度复盘</span>
              </button>
            </div>
          </div>

          {/* Visual Cron Editor + Friday Weekly Option Box */}
          <div className="rounded-[var(--radius-tile)] border border-line-soft bg-bg-sunk/55 p-4 space-y-3.5">
            <div className="flex flex-wrap items-end gap-3">
              <div>
                <label className="block text-[11.5px] font-medium text-ink-3 mb-1">
                  触发频率
                </label>
                <select
                  value={preset}
                  onChange={(e) => handlePresetChange(e.target.value as CronPreset)}
                  className="h-9 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[13px] text-ink focus:outline-none focus:border-accent"
                >
                  <option value="daily">每天</option>
                  <option value="weekdays">工作日（周一至周五）</option>
                  <option value="friday">每周五</option>
                  <option value="custom">自定义 5 段 Cron</option>
                </select>
              </div>

              {preset !== 'custom' && (
                <>
                  <div>
                    <label className="block text-[11.5px] font-medium text-ink-3 mb-1">
                      小时（上海时间）
                    </label>
                    <select
                      value={hour}
                      onChange={(e) => handleHourChange(e.target.value)}
                      className="h-9 num rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[13px] text-ink focus:outline-none focus:border-accent"
                    >
                      {HOURS.map((h) => (
                        <option key={h} value={h}>
                          {h} 时
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-[11.5px] font-medium text-ink-3 mb-1">
                      分钟
                    </label>
                    <select
                      value={MINUTES.includes(minute) ? minute : '00'}
                      onChange={(e) => handleMinuteChange(e.target.value)}
                      className="h-9 num rounded-[var(--radius-control)] border border-line bg-surface px-2.5 text-[13px] text-ink focus:outline-none focus:border-accent"
                    >
                      {MINUTES.map((m) => (
                        <option key={m} value={m}>
                          {m} 分
                        </option>
                      ))}
                    </select>
                  </div>
                </>
              )}

              <div className="flex-1 min-w-[180px]">
                <label className="block text-[11.5px] font-medium text-ink-3 mb-1">
                  Cron 表达式（分 时 日 月 周）
                </label>
                <input
                  type="text"
                  value={cronDraft}
                  onChange={(e) => handleCronInput(e.target.value)}
                  placeholder="0 20 * * *"
                  className="h-9 w-full font-mono rounded-[var(--radius-control)] border border-line bg-surface px-3 text-[13px] text-ink focus:outline-none focus:border-accent"
                />
              </div>

              <button
                type="button"
                disabled={!cronDirty || savingSchedule}
                onClick={handleSaveCron}
                className="h-9 inline-flex items-center gap-1.5 rounded-[var(--radius-control)] bg-accent px-4 text-[12.5px] font-semibold text-accent-contrast hover:opacity-95 transition-opacity disabled:opacity-45"
              >
                {savingSchedule ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Check className="w-3.5 h-3.5" />}
                <span>保存并热生效</span>
              </button>
            </div>

            <div className="border-t border-line-soft pt-3 flex flex-wrap items-center justify-between gap-2">
              <label className="inline-flex items-start gap-2.5 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={Boolean(sched?.friday_weekly_enabled)}
                  disabled={savingSchedule}
                  onChange={handleToggleFridayWeekly}
                  className="mt-0.5 size-4 rounded border-line accent-[var(--accent)]"
                />
                <span className="text-[12.5px]">
                  <span className="font-semibold text-ink">
                    周五自动并入周度 Dream 复盘
                  </span>{' '}
                  <span className="text-ok-ink">
                    （{sched?.friday_weekly_enabled ? '已启用 · 周五自动触发跨周聚类与规则蒸馏' : '已关闭 · 仅跑每日沉淀'}）
                  </span>
                  <span className="block text-[12px] text-ink-4 mt-0.5">
                    开启后，每周五的定时沉淀批（phase=auto）在完成 P05 结算时自动执行周度错题聚类与过时规则巡检；手动周度按钮不受此开关影响。
                  </span>
                </span>
              </label>
            </div>
          </div>

          {/* 2-Column Big KPI Tiles (directly matching reference screenshot bottom cards) */}
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="rounded-[var(--radius-tile)] border border-line bg-surface p-4 sm:p-5">
              <div className="text-[12px] font-medium text-ink-3">
                今日沉淀批状态（{metrics?.today_date || '—'}）
              </div>
              <div className={`mt-2 text-[22px] font-bold tracking-tight num ${todayColorClass}`}>
                {todayHeadline}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-4">
                <span>待审会话 {metrics?.pending_sessions ?? 0} 条</span>
                <span>·</span>
                <span>
                  审批策略：{labelOf(APPROVAL_MODE_LABELS, policy?.effective_approval_mode || 'smart')}
                </span>
              </div>
            </div>

            <div className="rounded-[var(--radius-tile)] border border-line bg-surface p-4 sm:p-5">
              <div className="text-[12px] font-medium text-ink-3">累计定时沉淀统计</div>
              <div className="mt-2 text-[22px] font-bold tracking-tight num text-amber-ink">
                {metrics?.total_scheduled_passed ?? 0} 次通过 / 共 {metrics?.total_scheduled_runs ?? 0} 次定时
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-4">
                <span>全部历史批次 {metrics?.total_runs ?? 0} 次</span>
                <span>·</span>
                <span>执行器：{formatRunnerMode(policy?.runner_mode || 'dry_run')}</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Card 3: Background Daemon Watchdogs */}
      <section
        id="automation-sec-daemons"
        className="card p-5 sm:p-6 space-y-4 scroll-mt-6"
      >
        <div>
          <h2 className="text-[16px] font-semibold text-ink">后台守护巡检</h2>
          <p className="mt-1 text-[12.5px] text-ink-3">
            仅在 <code className="font-mono text-[12px]">flywheel serve</code> 常驻进程中运行的守护任务：中断批次自动收割与飞书审批卡片长连接通道。
          </p>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          {/* Recovery Watchdog */}
          <div className="rounded-[var(--radius-tile)] border border-line-soft bg-bg-sunk/50 p-4 space-y-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 font-semibold text-[13.5px] text-ink">
                <ShieldCheck className="w-4 h-4 text-accent" />
                <span>中断批次收割（Recovery Watchdog）</span>
              </div>
              <span
                className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                  recovery?.role_active
                    ? 'bg-ok-soft text-ok-ink'
                    : 'bg-bg-muted text-ink-3'
                }`}
              >
                {recovery?.role_active ? '运行中' : '未激活'}
              </span>
            </div>
            <p className="text-[12px] text-ink-3">
              每 <span className="num font-medium">{Math.round(recovery?.interval_seconds ?? 30)}</span> 秒巡检一次，自动收割超过{' '}
              <span className="num font-medium">{recovery?.stale_after_seconds ?? 300}</span> 秒无心跳的 <code className="font-mono">RUNNING</code> 批次，释放独占锁并将未完成会话退回队列。
            </p>
            <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-[12px] text-ink-4">
              <span className="num">
                上次巡检：{formatShanghaiTimestamp(recovery?.last_pass_at)} · 累计收割 {recovery?.total_reaped_count ?? 0} 批
              </span>
              <button
                type="button"
                disabled={recovering}
                onClick={handleRecoveryNow}
                className="text-accent-ink hover:underline font-medium"
              >
                立即巡检
              </button>
            </div>
            {recovery?.last_error && (
              <div className="text-[11.5px] text-hot">最近错误：{recovery.last_error}</div>
            )}
          </div>

          {/* Feishu Outbox & Click Listener */}
          <div className="rounded-[var(--radius-tile)] border border-line-soft bg-bg-sunk/50 p-4 space-y-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 font-semibold text-[13.5px] text-ink">
                <Bell className="w-4 h-4 text-accent" />
                <span>飞书审批发件箱与点击监听</span>
              </div>
              <span
                className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                  feishu?.availability === 'ok'
                    ? 'bg-ok-soft text-ok-ink'
                    : feishu?.enabled
                    ? 'bg-amber-soft text-amber-ink'
                    : 'bg-bg-muted text-ink-3'
                }`}
              >
                {!feishu?.enabled
                  ? '未启用'
                  : feishu.availability === 'ok'
                  ? '交互就绪'
                  : feishu.interactive
                  ? labelOf(FEISHU_LISTENER_STATE_LABELS, feishu.listener_state)
                  : '仅文本通知'}
              </span>
            </div>
            <p className="text-[12px] text-ink-3">
              每 30 秒同步待审批卡片与退避重试；交互模式下通过单消费者长连接接收飞书卡片点击并原子认领执行。
            </p>
            <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-[12px] text-ink-4">
              <span>
                自检：{feishu?.check_valid ? '已通过' : feishu?.interactive ? '待自检' : '未开启交互模式'}
              </span>
              <button
                type="button"
                onClick={() => onNavigate('settings')}
                className="inline-flex items-center gap-1 text-accent-ink hover:underline font-medium"
              >
                <span>前往接入与设置</span>
                <ArrowRight className="w-3 h-3" />
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* Card 4: Recent Automation & Scheduled Execution History */}
      <section
        id="automation-sec-history"
        className="card p-5 sm:p-6 space-y-4 scroll-mt-6"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-[16px] font-semibold text-ink flex items-center gap-2">
              <Calendar className="w-4 h-4 text-accent" />
              <span>自动化执行记录</span>
            </h2>
            <p className="mt-0.5 text-[12.5px] text-ink-3">
              展示近期定时触发与手动触发的沉淀批次，点击可跳转至「每日沉淀」查看八节点详情与审批。
            </p>
          </div>
          <div className="flex items-center gap-2">
            <PillTabs
              items={[
                { key: 'all', label: '全部批次', count: data?.recent_runs.length ?? 0 },
                {
                  key: 'scheduled',
                  label: '仅定时触发',
                  count: (data?.recent_runs || []).filter((r) => r.trigger === 'scheduled').length,
                },
              ]}
              active={historyFilter}
              onChange={(k) => setHistoryFilter(k as 'all' | 'scheduled')}
            />
          </div>
        </div>

        {filteredRuns.length === 0 ? (
          <div className="rounded-[var(--radius-tile)] border border-dashed border-line py-8 text-center text-[13px] text-ink-4">
            {historyFilter === 'scheduled'
              ? '暂无定时调度触发的历史批次；到达预定时间后将自动记录在此。'
              : '暂无运行记录，可点击上方「一键执行自动沉淀」立即试跑。'}
          </div>
        ) : (
          <div className="divide-y divide-line-soft rounded-[var(--radius-tile)] border border-line-soft overflow-hidden">
            {filteredRuns.map((run) => (
              <div
                key={run.run_id}
                className="flex flex-wrap items-center justify-between gap-3 bg-surface px-4 py-3 hover:bg-bg-sunk/60 transition-colors"
              >
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="num font-semibold text-[13px] text-ink">
                      {run.batch_date} #{String(run.batch_seq).padStart(2, '0')}
                    </span>
                    <StatusBadge status={run.status} />
                    <span className="rounded bg-bg-sunk px-2 py-0.5 text-[11px] font-medium text-ink-3">
                      {labelOf(TRIGGER_LABELS, run.trigger)}
                    </span>
                    <span className="rounded bg-bg-sunk px-2 py-0.5 text-[11px] font-medium text-ink-3">
                      {labelOf(PHASE_LABELS, run.phase)}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px] text-ink-4 num">
                    <span>{formatShanghaiTimestamp(run.started_at || run.created_at)}</span>
                    <span>·</span>
                    <span>准入 {run.admitted_count} 会话</span>
                    <span>·</span>
                    <span>计划写入 {run.planned_writes_count} 项</span>
                    {run.duration_ms > 0 && (
                      <>
                        <span>·</span>
                        <span>
                          耗时 {formatDuration(run.started_at, run.finished_at, [{ duration_ms: run.duration_ms }])}
                        </span>
                      </>
                    )}
                  </div>
                  {run.error && (
                    <div className="text-[12px] text-hot truncate max-w-[680px]">
                      失败原因：{run.error}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => onNavigate('runs')}
                  className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 py-1 text-[12px] font-medium text-ink-2 hover:text-accent-ink hover:border-accent/40 transition-colors"
                >
                  <span>查看批次</span>
                  <ArrowRight className="w-3.5 h-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
