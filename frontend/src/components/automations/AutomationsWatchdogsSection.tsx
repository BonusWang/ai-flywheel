import React from 'react'
import { ArrowRight, Bell, Loader2, ShieldCheck } from 'lucide-react'
import type { NavigateFn } from '../../App'
import {
  FEISHU_LISTENER_STATE_LABELS,
  formatShanghaiTimestamp,
  labelOf,
} from '../../lib/labels'
import type { AutomationsSnapshot } from '../../types'

interface Props {
  recovery: AutomationsSnapshot['daemons']['recovery'] | undefined
  feishu: AutomationsSnapshot['daemons']['feishu'] | undefined
  recovering: boolean
  onRecoveryNow: () => void
  onNavigate: NavigateFn
}

export const AutomationsWatchdogsSection: React.FC<Props> = ({
  recovery,
  feishu,
  recovering,
  onRecoveryNow,
  onNavigate,
}) => (
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
            onClick={onRecoveryNow}
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
)
