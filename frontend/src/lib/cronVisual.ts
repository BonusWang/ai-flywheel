export type CronPreset = 'daily' | 'weekdays' | 'friday' | 'custom'

export const HOURS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'))
export const MINUTES = ['00', '05', '10', '15', '20', '25', '30', '35', '40', '45', '50', '55']

export function parseCronToVisual(expr: string): {
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

export function buildCronFromVisual(
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

export function humanPreview(preset: CronPreset, hour: string, minute: string): string {
  if (preset === 'daily') return `每天 ${hour}:${minute} · 上海时间`
  if (preset === 'weekdays') return `工作日 ${hour}:${minute} · 上海时间`
  if (preset === 'friday') return `每周五 ${hour}:${minute} · 上海时间`
  return '自定义排程 · 上海时间'
}

/** How long after its time a scheduled batch may still run, as one short phrase. */
export function formatCatchupWindow(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(Number(seconds))) return '—'
  const total = Math.max(0, Math.round(Number(seconds)))
  if (total % 3600 === 0 && total > 0) return `${total / 3600} 小时`
  if (total % 60 === 0 && total > 0) return `${total / 60} 分钟`
  return `${total} 秒`
}
