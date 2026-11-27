import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowRight,
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
import type { NavigateFn } from '../App'
import { ListToolbar, PaginationBar, usePagination } from '../components/ListControls'
import { PillTabs } from '../components/PageLayout'
import { StatusBadge } from '../components/StatusBadge'
import { AnimatedNumber } from '../components/ui/AnimatedNumber'
import { Collapse } from '../components/ui/Collapse'
import { ConfirmButton } from '../components/ui/ConfirmButton'
import { Switch } from '../components/ui/Switch'
import { useToast } from '../components/ui/Toast'
import {
  APPROVAL_MODE_LABELS,
  FEISHU_LISTENER_STATE_LABELS,
  formatDuration,
  formatRunnerMode,
  formatShanghaiTimestamp,
  labelOf,
  PHASE_LABELS,
  preferredScrollBehavior,
  TRIGGER_LABELS,
} from '../lib/labels'
import type { AutomationsSnapshot, DreamResult, DreamStatusPayload } from '../types'

interface Props {
  refreshToken: number
  onNavigate: NavigateFn
  onGlobalRefresh: () => void
}

type CronPreset = 'daily' | 'weekdays' | 'friday' | 'custom'
type SectionKey = 'scheduler' | 'daily' | 'daemons' | 'history'

const HOURS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'))
const MINUTES = ['00', '05', '10', '15', '20', '25', '30', '35', '40', '45', '50', '55']

function parseCronToVisual(expr: string): {
  preset: CronPreset
  hour: string
  minute: string
} {
  const parts = expr.trim().split(/\s+/)
  if (parts.length === 5 && /^\d{1,2}$/.test(parts[0]) && /^\d{1,2}$/.test(parts[1])) {
    const minute = String(Number(parts[0])).padStart(2, '0')
    const hour = String(Number(parts[1])).padStart(2, '0')
    if (MINUTES.includes(minute) && HOURS.includes(hour) && parts[2] === '*' && parts[3] === '*') {
      const dow = parts[4].toLowerCase()
      if (dow === '*') return { preset: 'daily', hour, minute }
      if (dow === '1-5' || dow === 'mon-fri') return { preset: 'weekdays', hour, minute }
      if (dow === '5' || dow === 'fri') return { preset: 'friday', hour, minute }
    }
  }
  return { preset: 'custom', hour: '20', minute: '00' }
}

function buildCronFromVisual(
  preset: CronPreset,
  hour: string,
  minute: string,
  fallback: string
): string {
  const m = String(Number(minute) || 0)
  const h = String(Number(hour) || 0)
  if (preset === 'daily') return `${m} ${h} * * *`
  if (preset === 'weekdays') return `${m} ${h} * * 1-5`
  if (preset === 'friday') return `${m} ${h} * * 5`
  return fallback
}

function humanPreview(preset: CronPreset, hour: string, minute: string): string {
  if (preset === 'daily') return `每天 ${hour}:${minute} · 上海时间`
  if (preset === 'weekdays') return `工作日 ${hour}:${minute} · 上海时间`
  if (preset === 'friday') return `每周五 ${hour}:${minute} · 上海时间`
  return '自定义排程 · 上海时间'
}

/** How long after its time a scheduled batch may still run, as one short phrase. */
function formatCatchupWindow(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(Number(seconds))) return '—'
  const total = Math.max(0, Math.round(Number(seconds)))
  if (total % 3600 === 0 && total > 0) return `${total / 3600} 小时`
  if (total % 60 === 0 && total > 0) return `${total / 60} 分钟`
  return `${total} 秒`
}

function AutomationsSkeleton() {
  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5" aria-busy="true">
      <div className="pb-4 border-b border-line space-y-2">
        <div className="skeleton-block h-7 w-36" />
        <div className="skeleton-block h-4 w-80 max-w-full" />
      </div>
      <div className="skeleton-block h-9 w-96 max-w-full rounded-full" />
      <div className="card p-6 space-y-3">
        <div className="skeleton-block h-5 w-32" />
        <div className="skeleton-block h-12 w-full" />
      </div>
      <div className="card p-6 space-y-4">
        <div className="skeleton-block h-5 w-44" />
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="skeleton-block h-28 w-full" />
          <div className="skeleton-block h-28 w-full" />
        </div>
      </div>
    </div>
  )
}

export const AutomationsPage: React.FC<Props> = ({
  refreshToken,
  onNavigate,
  onGlobalRefresh,
}) => {
  const toast = useToast()
  const [data, setData] = useState<AutomationsSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
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
  const [dreamStatus, setDreamStatus] = useState<DreamStatusPayload | null>(null)
  const [dreamResult, setDreamResult] = useState<DreamResult | null>(null)
  const [dreamBusy, setDreamBusy] = useState(false)
  // When the snapshot on screen was read. The catch-up deadline is compared with it.
  const [fetchedAt, setFetchedAt] = useState(0)

  const syncDraftFromSnapshot = useCallback((snap: AutomationsSnapshot) => {
    const expr = snap.scheduler.cron || '0 20 * * *'
    setCronDraft(expr)
    const parsed = parseCronToVisual(expr)
    setPreset(parsed.preset)
    setHour(parsed.hour)
    setMinute(parsed.minute)
  }, [])

  const load = useCallback(
    async (syncEditor = false, showSpinner = false) => {
      if (showSpinner) setRefreshing(true)
      try {
        const [snap, dream] = await Promise.all([
          api.automations(),
          api.getDream().catch(() => null),
        ])
        setData(snap)
        setFetchedAt(Date.now())
        if (dream) setDreamStatus(dream)
        if (syncEditor) syncDraftFromSnapshot(snap)
        setError(null)
      } catch (e: any) {
        setError(e?.message || '加载自动化调度状态失败')
      } finally {
        setLoading(false)
        setRefreshing(false)
      }
    },
    [syncDraftFromSnapshot]
  )

  useEffect(() => {
    load(true)
  }, [load])

  useEffect(() => {
    if (refreshToken > 0) load(false)
  }, [refreshToken, load])

  const scrollToSection = (key: SectionKey) => {
    setActiveSection(key)
    const el = document.getElementById(`automation-sec-${key}`)
    if (el) el.scrollIntoView({ behavior: preferredScrollBehavior(), block: 'start' })
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
    if (parsed.preset !== 'custom') {
      setHour(parsed.hour)
      setMinute(parsed.minute)
    }
  }

  const handleToggleAuto = async (nextEnabled: boolean) => {
    if (!data || togglingScheduler) return
    setTogglingScheduler(true)
    setError(null)
    try {
      const res = await api.updateAutomationSchedule({ schedule_enabled: nextEnabled })
      setData(res.automations)
      toast.push(
        nextEnabled
          ? `自动调度已开启（${res.automations.scheduler.human_schedule}），立即生效`
          : '自动调度已暂停，手动整理仍随时可用'
      )
      onGlobalRefresh()
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '切换自动调度失败' })
    } finally {
      setTogglingScheduler(false)
    }
  }

  const handleSaveCron = async () => {
    if (!data || savingSchedule) return
    setSavingSchedule(true)
    setError(null)
    try {
      const res = await api.updateAutomationSchedule({ daily_cron: cronDraft.trim() })
      setData(res.automations)
      syncDraftFromSnapshot(res.automations)
      toast.push(`定时规则已更新为「${res.automations.scheduler.human_schedule}」，立即生效`)
      onGlobalRefresh()
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '保存定时规则失败' })
    } finally {
      setSavingSchedule(false)
    }
  }

  const handleToggleFridayWeekly = async (nextVal: boolean) => {
    if (!data || savingSchedule) return
    setSavingSchedule(true)
    setError(null)
    try {
      const res = await api.updateAutomationSchedule({ friday_weekly_enabled: nextVal })
      setData(res.automations)
      toast.push(
        nextVal
          ? '已开启周五自动附带规则与笔记体检'
          : '已关闭周五自动附带体检（仍可随时手动预览或执行）'
      )
      onGlobalRefresh()
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '切换设置失败' })
    } finally {
      setSavingSchedule(false)
    }
  }

  const handleManualRun = async (phase: 'auto' | 'weekly') => {
    if (runningPhase) return
    setRunningPhase(phase)
    setError(null)
    try {
      const res = await api.startRun({ phase }, { background: true })
      const rid = res.run_id || res.run?.run_id || ''
      toast.push({
        tone: 'info',
        message: `已启动「${labelOf(PHASE_LABELS, phase)}」任务，后台整理中`,
      })
      onGlobalRefresh()
      await load(false)
      if (rid) {
        onNavigate('runs', { runsTab: 'pipeline', runId: rid })
      }
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '启动整理任务失败' })
    } finally {
      setRunningPhase(null)
    }
  }

  const handleRecoveryNow = async () => {
    if (recovering) return
    setRecovering(true)
    setError(null)
    try {
      const res = await api.triggerAutomationRecovery()
      setData(res.automations)
      toast.push(
        res.reaped_count > 0
          ? `巡检完成：已自动恢复 ${res.reaped_count} 个卡住的批次并释放锁`
          : '巡检完成：没有卡住的批次，运行状态正常'
      )
      onGlobalRefresh()
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '执行巡检失败' })
    } finally {
      setRecovering(false)
    }
  }

  const handleDreamAction = async (dryRun: boolean) => {
    if (dreamBusy) return
    setDreamBusy(true)
    setError(null)
    try {
      const res = await api.runDream(dryRun)
      setDreamResult(res.result)
      const retiredCount = res.result.auto_archived_cards?.length || 0
      toast.push(
        dryRun
          ? `预览完成：待休眠 ${res.result.auto_dormant_rules.length} 条零命中规则，${retiredCount > 0 ? `待自动退场 ${retiredCount} 篇超期零助益笔记，` : ''}待复核 ${res.result.expired_cards.length} 篇笔记（未改动数据）`
          : `精简完成：已休眠 ${res.result.auto_dormant_rules.length} 条零命中规则，${retiredCount > 0 ? `已自动归档 ${retiredCount} 篇超期零助益笔记，` : ''}标记 ${res.result.expired_cards.length} 篇待复核笔记`
      )
      await load(false)
      if (!dryRun) onGlobalRefresh()
    } catch (e: any) {
      toast.push({ tone: 'error', message: e?.message || '执行周五精简失败' })
    } finally {
      setDreamBusy(false)
    }
  }

  const cronDirty = useMemo(() => {
    if (!data) return false
    return cronDraft.trim() !== (data.scheduler.cron || '').trim()
  }, [cronDraft, data])

  const [historyQuery, setHistoryQuery] = useState('')
  const [historyStatus, setHistoryStatus] = useState('all')

  const filteredRuns = useMemo(() => {
    if (!data) return []
    const q = historyQuery.trim().toLowerCase()
    return [...data.recent_runs]
      .filter((r) => (historyFilter === 'scheduled' ? r.trigger === 'scheduled' : true))
      .filter((r) => (historyStatus === 'all' ? true : r.status === historyStatus))
      .filter((r) =>
        !q
          ? true
          : `${r.batch_date || ''} ${r.run_id} ${r.phase} ${labelOf(PHASE_LABELS, r.phase)} ${r.trigger} ${labelOf(TRIGGER_LABELS, r.trigger)} ${r.error || ''}`
              .toLowerCase()
              .includes(q)
      )
      .sort((a, b) =>
        String(b.started_at || b.created_at || '').localeCompare(
          String(a.started_at || a.created_at || '')
        )
      )
  }, [data, historyFilter, historyQuery, historyStatus])

  const historyPager = usePagination(filteredRuns, 10)
  const hasHistoryFilters = Boolean(historyQuery.trim()) || historyStatus !== 'all'

  if (loading && !data) {
    return <AutomationsSkeleton />
  }

  const sched = data?.scheduler
  const metrics = data?.metrics
  const policy = data?.policy
  const recovery = data?.daemons.recovery
  const feishu = data?.daemons.feishu
  const isRunning = Boolean(sched?.running)
  const isLocked = Boolean(data?.lock)

  // How a scheduled fire this process was not awake for is handled.
  const catchupEnabled = sched?.catchup_missed_schedule === true
  const catchupStateLabel =
    sched?.catchup_missed_schedule === true
      ? '已开启'
      : sched?.catchup_missed_schedule === false
        ? '已关闭'
        : '未知'
  const catchupWindow = formatCatchupWindow(sched?.misfire_grace_seconds)
  const catchupWindowKnown = sched?.misfire_grace_seconds != null
  const missedToday = Boolean(sched?.missed_today)
  // The fire still counts as missed after a person ran a batch by hand — only a
  // scheduled one clears it — unless that attempt failed or was cancelled.
  const lastRunStatus = sched?.last_run_status ?? null
  const manualRunSinceFire =
    missedToday &&
    Boolean(sched?.scheduled_today) &&
    Boolean(sched?.last_run_at) &&
    sched?.last_run_trigger !== 'scheduled' &&
    Date.parse(String(sched?.last_run_at)) >= Date.parse(String(sched?.scheduled_today))
  const missedFireHandledByHand =
    manualRunSinceFire && lastRunStatus !== 'FAILED' && lastRunStatus !== 'CANCELLED'
  const manualRunInProgress =
    missedFireHandledByHand && (lastRunStatus === 'PENDING' || lastRunStatus === 'RUNNING')
  // The recovery loop catches a lost fire up only until the grace window closes.
  // The backend states that as a deadline, so compare it with the clock at the
  // moment the snapshot was read.
  const catchupWindowClosed =
    Boolean(sched?.catchup_window_until) &&
    fetchedAt > Date.parse(String(sched?.catchup_window_until))
  const missedSchedule = (sched?.human_schedule || '').replace('（上海时间）', '')
  const missedLead = `${missedSchedule} 的定时批次今天没有运行。`
  const missedGrace = catchupWindowKnown ? `，补跑宽限 ${catchupWindow}` : ''
  const missedFire = !missedToday
    ? null
    : missedFireHandledByHand
      ? {
          headline: manualRunInProgress ? '今日定时已错过 · 手动补跑进行中' : '今日定时已错过 · 已手动补跑',
          title: manualRunInProgress ? '当日定时已错过，手动补跑进行中' : '当日定时已错过，已手动补跑',
          detail: manualRunInProgress
            ? `${missedLead}已在 ${formatShanghaiTimestamp(sched?.last_run_at)} 手动发起整理，正在运行。`
            : `${missedLead}已在 ${formatShanghaiTimestamp(sched?.last_run_at)} 手动整理。`,
          actionable: false,
        }
      : policy && !policy.workflow_enabled
        ? {
            headline: '今日定时已错过 · 自动整理已暂停',
            title: '当日定时已错过，自动整理已暂停',
            detail: `${missedLead}后台自动整理总开关已关闭，定时与补跑都已暂停；开启后再手动执行一轮。`,
            actionable: false,
          }
        : !catchupEnabled
          ? {
              headline: '今日定时已错过 · 待手动执行',
              title: '当日定时已错过，自动补跑已关闭',
              detail: `${missedLead}自动补跑已关闭${missedGrace}，后台巡检不会补跑这一轮，请手动执行一轮。`,
              actionable: true,
            }
          : !sched?.role_active
            ? {
                headline: '今日定时已错过 · 待手动执行',
                title: '当日定时已错过，当前进程不会自动补跑',
                detail: `${missedLead}自动补跑已开启，但当前进程不常驻后台，不会自动补跑；请手动执行一轮。`,
                actionable: true,
              }
            : catchupWindowClosed
              ? {
                  headline: '今日定时已错过 · 待手动执行',
                  title: '当日定时已错过，补跑窗口已结束',
                  detail: `${missedLead}自动补跑已开启${missedGrace}，但补跑窗口已结束，后台巡检不会再补跑这一轮；请手动执行一轮。`,
                  actionable: true,
                }
              : {
                  headline: '今日定时已错过 · 等待补跑',
                  title: '当日定时已错过，等待补跑或手动执行',
                  detail: `${missedLead}自动补跑已开启${missedGrace}${
                    sched?.catchup_window_until
                      ? `，补跑窗口至 ${formatShanghaiTimestamp(sched.catchup_window_until)}`
                      : ''
                  }：如仍有待整理内容，后台巡检会补跑一次，也可以现在手动执行。`,
                  actionable: true,
                }

  const todayHeadline = (() => {
    if (!metrics) return '—'
    if (metrics.today_awaiting_count > 0) return `待审批 (${metrics.today_awaiting_count} 批)`
    if (missedFire) return missedFire.headline
    if (metrics.today_passed_count > 0)
      return `今日已整理 (${metrics.today_passed_count}/${metrics.today_runs_count})`
    if (metrics.today_failed_count > 0) return `今日异常 (${metrics.today_failed_count} 批)`
    return sched?.enabled
      ? `今日待触发 · ${sched.human_schedule.replace('（上海时间）', '')}`
      : '今日未执行（自动已暂停）'
  })()

  const todayColorClass = (() => {
    if (!metrics) return 'text-ink'
    if (metrics.today_awaiting_count > 0) return 'text-amber-ink'
    if (missedToday) return 'text-amber-ink'
    if (metrics.today_passed_count > 0) return 'text-ok'
    if (metrics.today_failed_count > 0) return 'text-hot'
    return 'text-ink-2'
  })()

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5 pb-12">
      {/* Unified Page Header */}
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">自动化调度</h1>
          <p className="mt-1 text-[13.5px] text-ink-3">
            设置每日定时整理、周五规则精简体检与后台异常恢复（统一按上海时间运行）。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] font-medium ${
              isRunning
                ? 'border-ok/30 bg-ok-soft text-ok-ink'
                : 'border-line bg-surface text-ink-3'
            }`}
          >
            <span
              className={`size-2 rounded-full ${isRunning ? 'status-dot-live bg-ok' : 'bg-ink-4'}`}
            />
            <span>{isRunning ? `定时运行中 · ${sched?.human_schedule}` : '自动调度已暂停'}</span>
          </span>
          <button
            type="button"
            disabled={refreshing}
            onClick={() => load(false, true)}
            className="pressable inline-flex items-center gap-1.5 h-8 rounded-[var(--radius-control)] border border-line bg-surface px-3 text-[12.5px] font-medium text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-60"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            <span>刷新</span>
          </button>
        </div>
      </header>

      {/* Section Anchor Tabs */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PillTabs
          items={[
            { key: 'scheduler', label: '定时总开关' },
            { key: 'daily', label: '每日与周五精简' },
            { key: 'daemons', label: '后台守护' },
            { key: 'history', label: '执行记录', count: data?.recent_runs.length ?? 0 },
          ]}
          active={activeSection}
          onChange={(k) => scrollToSection(k as SectionKey)}
        />
      </div>

      {error && (
        <div className="animate-shake rounded-[var(--radius-card)] border border-hot/30 bg-hot-soft px-4 py-2.5 text-[13px] text-hot">
          {error}
        </div>
      )}

      {policy && !policy.workflow_enabled && (
        <div className="rounded-[var(--radius-card)] border border-amber/35 bg-amber-soft px-4 py-2.5 text-[13px] text-amber-ink flex flex-wrap items-center justify-between gap-2">
          <span>
            后台自动整理总开关当前已关闭，定时与手动任务均会暂停。
          </span>
          <button
            type="button"
            onClick={() => onNavigate('settings', { settingsTab: 'storage' })}
            className="pressable inline-flex items-center gap-1 h-7 px-2.5 rounded-[var(--radius-mark)] bg-amber text-amber-contrast text-[12px] font-semibold hover:opacity-90"
          >
            <span>立即前往开启</span>
            <ArrowRight className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* Card 1: Background Scheduler Master Control */}
      <section
        id="automation-sec-scheduler"
        className="card p-5 sm:p-6 space-y-4 scroll-mt-20 lg:scroll-mt-6"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-[16px] font-semibold text-ink">后台定时调度</h2>
            <p className="mt-1 text-[12.5px] text-ink-3">
              到点自动整理对话并按你的审批偏好落盘；关闭后暂停自动触发，修改立即生效无需重启。
            </p>
          </div>
          <div className="flex items-center gap-2.5">
            <span className="text-[12.5px] font-medium text-ink-2">
              {togglingScheduler
                ? '切换中…'
                : sched?.enabled
                  ? '已开启'
                  : '已暂停'}
            </span>
            <Switch
              checked={Boolean(sched?.enabled)}
              disabled={togglingScheduler}
              onChange={handleToggleAuto}
              ariaLabel="自动执行总开关"
            />
          </div>
        </div>

        {/* A scheduled fire that was lost: say so, and offer the way out. */}
        {missedFire && (
          <div
            role="alert"
            className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-tile)] border border-amber/45 bg-amber-soft px-4 py-3"
          >
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-[13.5px] font-semibold text-amber-ink">
                <AlertTriangle className="w-4 h-4 shrink-0" />
                <span>{missedFire.title}</span>
              </div>
              <p className="mt-1 text-[12.5px] text-amber-ink">{missedFire.detail}</p>
            </div>
            {missedFire.actionable && (
              <button
                type="button"
                disabled={Boolean(runningPhase) || isLocked}
                onClick={() => handleManualRun('auto')}
                className="pressable inline-flex items-center gap-1.5 h-8 shrink-0 px-3.5 rounded-[var(--radius-control)] bg-amber text-amber-contrast text-[12.5px] font-semibold hover:opacity-90 disabled:opacity-50"
              >
                {runningPhase === 'auto' ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Play className="w-3.5 h-3.5" />
                )}
                <span>{isLocked ? '已有批次整理中' : '手动补跑一轮'}</span>
              </button>
            )}
          </div>
        )}

        {/* Live Status Strip */}
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius-tile)] border border-line-soft bg-bg-sunk/70 px-4 py-3 text-[12.5px]">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="inline-flex items-center gap-2 font-semibold text-ink">
              <span
                className={`size-2 rounded-full ${
                  isRunning ? 'bg-ok' : sched?.enabled ? 'bg-amber' : 'bg-ink-4'
                }`}
              />
              <span>
                {isRunning
                  ? '运行中'
                  : sched?.enabled && !sched.role_active
                    ? '后台服务未常驻（仅保存排程设置）'
                    : '已暂停'}
              </span>
            </span>
            <span className="text-ink-3">
              · {sched?.human_schedule}
              {sched?.friday_weekly_enabled ? '（含周五精简体检）' : ''}
              {feishu?.enabled ? ' · 已接飞书通知' : ''}
            </span>
          </div>
          <span className="num text-[12px] text-ink-4">
            上次：{formatShanghaiTimestamp(sched?.last_scheduled_run_at || sched?.last_run_at)} ·
            下次：
            {sched?.next_run_at
              ? formatShanghaiTimestamp(sched.next_run_at)
              : sched?.preview_next_run_at
                ? `暂停中（开启后 ${formatShanghaiTimestamp(sched.preview_next_run_at)}）`
                : '未排程'}
          </span>
          {/* When this process came up, and how a fire it slept through is caught up. */}
          <div className="flex w-full flex-wrap items-center gap-x-4 gap-y-1 border-t border-line-soft pt-2 text-[12px] text-ink-3">
            <span>
              服务启动
              <span className="num ml-1.5 font-medium text-ink-2">
                {formatShanghaiTimestamp(sched?.started_at)}
              </span>
            </span>
            <span>
              补跑宽限
              <span className="num ml-1.5 font-medium text-ink-2">{catchupWindow}</span>
              <span className="mx-1.5">·</span>
              自动补跑
              <span className="ml-1.5 font-medium text-ink-2">{catchupStateLabel}</span>
            </span>
          </div>
        </div>
      </section>

      {/* Card 2: Daily & Periodic Tasks */}
      <section
        id="automation-sec-daily"
        className="card p-5 sm:p-6 space-y-5 scroll-mt-20 lg:scroll-mt-6"
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line-soft pb-4">
          <div>
            <h2 className="text-[16px] font-semibold text-ink">每日整理与周五精简</h2>
            <p className="mt-1 text-[12.5px] text-ink-3">
              按「{labelOf(APPROVAL_MODE_LABELS, policy?.effective_approval_mode || 'smart')}」策略整理对话，系统会自动按日期决定是否附带周五精简体检。
            </p>
          </div>
          <button
            type="button"
            disabled={Boolean(runningPhase) || isLocked}
            onClick={() => handleManualRun('auto')}
            className="pressable inline-flex items-center gap-1.5 h-9 rounded-[var(--radius-control)] bg-accent px-4 text-[13px] font-semibold text-accent-contrast hover:bg-accent-ink disabled:opacity-50"
          >
            {runningPhase === 'auto' ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Play className="w-3.5 h-3.5" />
            )}
            <span>{isLocked ? '已有批次整理中' : '立即整理一轮'}</span>
          </button>
        </div>

        {/* Visual Schedule Editor */}
        <div className="rounded-[var(--radius-tile)] border border-line-soft bg-bg-sunk/55 p-4 space-y-3.5">
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label className="block text-[11.5px] font-medium text-ink-3 mb-1">触发频率</label>
              <select
                value={preset}
                onChange={(e) => handlePresetChange(e.target.value as CronPreset)}
                className="field-input h-9 px-2.5 text-[13px]"
              >
                <option value="daily">每天</option>
                <option value="weekdays">工作日（周一至周五）</option>
                <option value="friday">每周五</option>
                <option value="custom">自定义 Cron</option>
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
                    className="field-input h-9 num px-2.5 text-[13px]"
                  >
                    {HOURS.map((h) => (
                      <option key={h} value={h}>
                        {h} 时
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-[11.5px] font-medium text-ink-3 mb-1">分钟</label>
                  <select
                    value={MINUTES.includes(minute) ? minute : '00'}
                    onChange={(e) => handleMinuteChange(e.target.value)}
                    className="field-input h-9 num px-2.5 text-[13px]"
                  >
                    {MINUTES.map((m) => (
                      <option key={m} value={m}>
                        {m} 分
                      </option>
                    ))}
                  </select>
                </div>
                <div className="h-9 flex items-center">
                  <span className="chip text-[12px] text-ink-2">
                    <Clock className="w-3 h-3 text-accent" />
                    {humanPreview(preset, hour, minute)}
                  </span>
                </div>
              </>
            )}

            <div className="ml-auto flex items-center gap-2">
              {cronDirty && (
                <button
                  type="button"
                  onClick={() => data && syncDraftFromSnapshot(data)}
                  className="pressable h-9 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-3 hover:text-ink"
                >
                  还原
                </button>
              )}
              <button
                type="button"
                disabled={!cronDirty || savingSchedule}
                onClick={handleSaveCron}
                className="pressable h-9 inline-flex items-center gap-1.5 rounded-[var(--radius-control)] bg-accent px-4 text-[12.5px] font-semibold text-accent-contrast hover:bg-accent-ink disabled:opacity-45"
              >
                {savingSchedule ? (
                  <Loader2 className="w-3.5 h-3.5 animate-spin" />
                ) : (
                  <Check className="w-3.5 h-3.5" />
                )}
                <span>{cronDirty ? '保存排程' : '已生效'}</span>
              </button>
            </div>
          </div>

          {/* Raw Cron input only appears when user chooses Custom */}
          <Collapse open={preset === 'custom'}>
            <div className="pt-1">
              <label className="block text-[11.5px] font-medium text-ink-3 mb-1">
                自定义 5 段 Cron 表达式（分 时 日 月 周）
              </label>
              <input
                type="text"
                value={cronDraft}
                onChange={(e) => handleCronInput(e.target.value)}
                placeholder="0 20 * * *"
                className="field-input h-9 w-full max-w-md font-mono px-3 text-[13px]"
              />
            </div>
          </Collapse>

          <div className="border-t border-line-soft pt-3 flex items-center justify-between gap-3">
            <div className="text-[12.5px]">
              <div className="font-semibold text-ink">周五顺带做一次规则与笔记体检</div>
              <p className="text-[12px] text-ink-4 mt-0.5">
                开启后，每周五定时整理结束时自动休眠 14 天零命中的试运行规则并标记过保鲜期的笔记。
              </p>
            </div>
            <Switch
              size="sm"
              checked={Boolean(sched?.friday_weekly_enabled)}
              disabled={savingSchedule}
              onChange={handleToggleFridayWeekly}
              ariaLabel="周五自动附带精简体检"
            />
          </div>
        </div>

        {/* 2-Column KPI Tiles */}
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-[var(--radius-tile)] border border-line bg-surface p-4 sm:p-5">
            <div className="text-[12px] font-medium text-ink-3">
              今日整理状态（{metrics?.today_date || '—'}）
            </div>
            <div className={`mt-2 text-[22px] font-bold tracking-tight num ${todayColorClass}`}>
              {todayHeadline}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-4">
              <span>待整理对话 {metrics?.pending_sessions ?? 0} 段</span>
              <span>·</span>
              <span>
                审批方式：{labelOf(APPROVAL_MODE_LABELS, policy?.effective_approval_mode || 'smart')}
              </span>
            </div>
          </div>

          <div className="rounded-[var(--radius-tile)] border border-line bg-surface p-4 sm:p-5">
            <div className="text-[12px] font-medium text-ink-3">累计定时整理统计</div>
            <div className="mt-2 text-[22px] font-bold tracking-tight num text-amber-ink">
              <AnimatedNumber value={metrics?.total_scheduled_passed ?? 0} /> 次通过 / 共{' '}
              <AnimatedNumber value={metrics?.total_scheduled_runs ?? 0} /> 次定时
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-4">
              <span>全部历史批次 {metrics?.total_runs ?? 0} 次</span>
              <span>·</span>
              <span>整理模型：{formatRunnerMode(policy?.runner_mode || 'dry_run')}</span>
            </div>
          </div>
        </div>

        {/* Friday Consolidation Card (Preview-first + guarded execution) */}
        {(() => {
          const activeDream = dreamResult || dreamStatus?.preview
          const latestEv = dreamStatus?.latest_event
          return (
            <div className="rounded-[var(--radius-tile)] border border-line bg-bg-sunk/40 p-4 sm:p-5 space-y-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <Sparkles className="w-4 h-4 text-accent" />
                    <h3 className="text-[14px] font-semibold text-ink">周五定期精简</h3>
                    {activeDream?.dry_run && <span className="chip text-[11px]">预览结果</span>}
                  </div>
                  <p className="mt-1 text-[12px] text-ink-3">
                    自动休眠 14 天零命中的试运行规则、扫描过保鲜期的笔记，并找出重复规则与长期未用的 Skill。
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    disabled={dreamBusy}
                    onClick={() => handleDreamAction(true)}
                    className="pressable inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] bg-accent-soft border border-accent/25 text-[12px] font-semibold text-accent-ink hover:bg-accent/18 disabled:opacity-50"
                  >
                    {dreamBusy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                    <span>预览精简建议</span>
                  </button>
                  <ConfirmButton
                    label="执行精简"
                    confirmLabel="确认写入精简结果？"
                    disabled={dreamBusy}
                    onConfirm={() => handleDreamAction(false)}
                    className="h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12px] font-medium text-ink-2 hover:border-line-strong hover:text-ink disabled:opacity-50"
                    confirmClassName="h-8 px-3 rounded-[var(--radius-control)] border border-amber bg-amber text-amber-contrast text-[12px] font-semibold"
                  />
                  <button
                    type="button"
                    disabled={Boolean(runningPhase) || isLocked}
                    onClick={() => handleManualRun('weekly')}
                    className="pressable h-8 px-2.5 rounded-[var(--radius-control)] text-[12px] text-ink-3 hover:text-ink hover:bg-bg-sunk disabled:opacity-50"
                  >
                    单独跑周复盘
                  </button>
                </div>
              </div>

              {activeDream && (
                <div className="flex flex-wrap gap-3 text-[12px] text-ink-2 num">
                  <span>周期：{activeDream.period_key}</span>
                  <span>·</span>
                  <span>待休眠规则：{activeDream.auto_dormant_rules.length} 条</span>
                  {activeDream.auto_archived_cards && activeDream.auto_archived_cards.length > 0 && (
                    <>
                      <span>·</span>
                      <span className="text-amber-ink font-medium">
                        自动退场笔记：{activeDream.auto_archived_cards.length} 篇
                      </span>
                    </>
                  )}
                  <span>·</span>
                  <span>待复核笔记：{activeDream.expired_cards.length} 篇</span>
                  <span>·</span>
                  <span>重复规则候选：{activeDream.merge_candidates.length} 对</span>
                  <span>·</span>
                  <span>Skill 提醒：{activeDream.skill_alerts.length} 个</span>
                  <span>·</span>
                  <span>待处理分歧：{activeDream.open_dissents_count} 条</span>
                </div>
              )}

              {activeDream && activeDream.proposals.length > 0 && (
                <ul className="list-disc pl-5 space-y-1 text-[12.5px] text-ink-2">
                  {activeDream.proposals.map((item, idx) => (
                    <li key={idx}>{item}</li>
                  ))}
                </ul>
              )}

              {latestEv && (
                <div className="pt-2 border-t border-line-soft text-[11.5px] text-ink-4 num">
                  最近正式执行（{formatShanghaiTimestamp(latestEv.created_at)}）：{latestEv.summary}
                </div>
              )}
            </div>
          )
        })()}
      </section>

      {/* Card 3: Background Watchdogs */}
      <section
        id="automation-sec-daemons"
        className="card p-5 sm:p-6 space-y-4 scroll-mt-20 lg:scroll-mt-6"
      >
        <div>
          <h2 className="text-[16px] font-semibold text-ink">后台守护</h2>
          <p className="mt-1 text-[12.5px] text-ink-3">
            常驻服务期间自动看护中断批次与飞书审批通道，异常时自动清理恢复。
          </p>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          {/* Recovery Watchdog */}
          <div className="rounded-[var(--radius-tile)] border border-line-soft bg-bg-sunk/50 p-4 space-y-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 font-semibold text-[13.5px] text-ink">
                <ShieldCheck className="w-4 h-4 text-accent" />
                <span>异常中断自动恢复</span>
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
              每 <span className="num font-medium">{Math.round(recovery?.interval_seconds ?? 30)}</span> 秒巡检一次，自动清理超过{' '}
              <span className="num font-medium">{recovery?.stale_after_seconds ?? 300}</span> 秒无响应的卡住批次，释放锁并把未完成对话退回队列。
            </p>
            <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-[12px] text-ink-4">
              <span className="num">
                上次巡检：{formatShanghaiTimestamp(recovery?.last_pass_at)} · 累计恢复{' '}
                {recovery?.total_reaped_count ?? 0} 批
              </span>
              <button
                type="button"
                disabled={recovering}
                onClick={handleRecoveryNow}
                className="pressable inline-flex items-center gap-1 text-accent-ink hover:underline font-medium disabled:opacity-50"
              >
                {recovering && <Loader2 className="w-3 h-3 animate-spin" />}
                <span>清理卡住的批次</span>
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
                <span>飞书消息与卡片审批</span>
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
              支持在飞书卡片上直接点击「同意 / 放弃」，结果实时同步回本地，平时自动通过不打扰。
            </p>
            <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-[12px] text-ink-4">
              <span>
                链路自检：{feishu?.check_valid ? '已通过' : feishu?.interactive ? '待自检' : '未开启交互卡片'}
              </span>
              <button
                type="button"
                onClick={() => onNavigate('settings', { settingsTab: 'storage' })}
                className="pressable inline-flex items-center gap-1 text-accent-ink hover:underline font-medium"
              >
                <span>前往配置飞书</span>
                <ArrowRight className="w-3 h-3" />
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* Card 4: Recent Execution History */}
      <section
        id="automation-sec-history"
        className="card p-5 sm:p-6 space-y-4 scroll-mt-20 lg:scroll-mt-6"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-[16px] font-semibold text-ink flex items-center gap-2">
              <Calendar className="w-4 h-4 text-accent" />
              <span>执行记录</span>
            </h2>
            <p className="mt-0.5 text-[12.5px] text-ink-3">
              点任意批次的「查看批次」直接定位到该批明细与审批抽屉。
            </p>
          </div>
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
            onChange={(k) => {
              setHistoryFilter(k as 'all' | 'scheduled')
              historyPager.resetPage()
            }}
          />
        </div>

        {(data?.recent_runs.length ?? 0) > 0 && (
          <ListToolbar
            query={historyQuery}
            onQueryChange={(q) => {
              setHistoryQuery(q)
              historyPager.resetPage()
            }}
            placeholder="按日期、批次号或错误信息搜索…"
            hasActiveFilters={hasHistoryFilters}
            onResetFilters={() => {
              setHistoryQuery('')
              setHistoryStatus('all')
              historyPager.resetPage()
            }}
          >
            <select
              aria-label="执行记录状态筛选"
              value={historyStatus}
              onChange={(e) => {
                setHistoryStatus(e.target.value)
                historyPager.resetPage()
              }}
              className="field-input h-8 px-2.5 text-[12px]"
            >
              <option value="all">全部状态</option>
              <option value="PASS">已通过</option>
              <option value="AWAITING_APPROVAL">待审批</option>
              <option value="FAILED">已失败</option>
              <option value="PARTIAL">部分完成</option>
              <option value="CANCELLED">已取消</option>
            </select>
          </ListToolbar>
        )}

        {filteredRuns.length === 0 ? (
          <div className="rounded-[var(--radius-tile)] border border-dashed border-line py-8 text-center text-[13px] text-ink-4">
            {historyFilter === 'scheduled'
              ? '暂无定时调度触发的历史批次；到达预定时间后会自动记录在这里。'
              : '暂无符合条件的运行记录。'}
          </div>
        ) : (
          <div className="space-y-2">
            <div className="divide-y divide-line-soft rounded-[var(--radius-tile)] border border-line-soft overflow-hidden">
              {historyPager.pageItems.map((run) => (
                <div
                  key={run.run_id}
                  className="flex flex-wrap items-center justify-between gap-3 bg-surface px-4 py-3 hover:bg-bg-sunk/60 transition-colors duration-[150ms]"
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
                      <span>整理 {run.admitted_count} 段对话</span>
                      <span>·</span>
                      <span>拟写入 {run.planned_writes_count} 项</span>
                      {run.duration_ms > 0 && (
                        <>
                          <span>·</span>
                          <span>
                            耗时{' '}
                            {formatDuration(run.started_at, run.finished_at, [
                              { duration_ms: run.duration_ms },
                            ])}
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
                    onClick={() =>
                      onNavigate('runs', { runsTab: 'pipeline', runId: run.run_id })
                    }
                    className="pressable inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-line bg-surface px-2.5 py-1 text-[12px] font-medium text-ink-2 hover:text-accent-ink hover:border-accent/40"
                  >
                    <span>查看批次</span>
                    <ArrowRight className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
            <PaginationBar
              total={historyPager.total}
              page={historyPager.page}
              pageSize={historyPager.pageSize}
              totalPages={historyPager.totalPages}
              onPageChange={historyPager.setPage}
              onPageSizeChange={historyPager.setPageSize}
            />
          </div>
        )}
      </section>
    </div>
  )
}
