import type { DictItem } from '../types'

export const STAGE_PLAIN_NAMES: Record<string, string> = {
  P00_PREPARE: '准备检查',
  P01_BEGIN: '选定对话',
  P02_REVIEW: '回顾对话',
  P03_ROUTE_PLAN: '整理建议',
  P04_WRITE_VERIFY: '写入核验',
  P05_SETTLE: '更新台账',
  P06_REPORT: '生成日报',
  P07_DELIVER: '推送收尾',
}

export const APPROVAL_MODE_LABELS: Record<string, string> = {
  smart: '智能审批',
  always: '每次确认',
  auto: '全部自动',
}

export const RUNNER_MODE_LABELS: Record<string, string> = {
  api: 'AI 模型整理',
  cli: '本地命令行',
  dry_run: '本地快速演练',
}

/**
 * The runner mode a run used, spelled out.
 *
 * The recorded mode is `api:<model>` when a model did the work, so the plain
 * mode table alone would show nothing useful: the model is the part worth
 * naming. `api` / `cli` / `dry_run` keep their own labels.
 */
export function formatRunnerMode(mode: string): string {
  const raw = (mode || '').trim()
  if (!raw) return '—'
  if (raw.startsWith('api:')) {
    const model = raw.slice('api:'.length).trim()
    return model ? `API 模型 · ${model}` : 'API 模型整理'
  }
  return RUNNER_MODE_LABELS[raw] ?? raw
}

export const RETRO_KIND_LABELS: Record<string, string> = {
  retry_loop: '重复报错提醒',
  missing_evidence: '缺少实测证据',
  blocker: '执行受阻',
  high_risk_tool: '高风险操作',
  rule_recurrence: '问题再次出现',
  custom: '自定义信号',
}

/** Fallback labels for every distill-queue status, used when no dictionary is loaded. */
export const QUEUE_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  processing: '处理中',
  processed: '已沉淀',
  excluded: '已排除',
  withdrawn: '已撤回',
  dead_letter: '死信',
}

/** Fallback labels for every ingress channel, used when no dictionary is loaded. */
export const INGRESS_CHANNEL_LABELS: Record<string, string> = {
  mcp_explicit: 'MCP 显式入队',
  manual_portal: '控制台手动入队',
  codex_archive: 'Codex 归档',
  claude_exit: 'Claude 退出',
  legacy_scan: '历史扫描',
}

export const POLICY_SOURCE_LABELS: Record<string, string> = {
  default: '系统默认',
  profile_override: '个人配置覆盖',
  ui_override: '网页自定义',
}

export const CLIENT_LABELS: Record<string, string> = {
  codex: 'Codex',
  'claude-code': 'Claude Code',
  claude: 'Claude Desktop',
  'portal-sandbox': '自测模拟',
  generic: '通用',
}

export const TRIGGER_LABELS: Record<string, string> = {
  scheduled: '定时调度',
  api: '手动触发',
  cli: '命令行触发',
  manual: '手工补录',
  seed: '示例数据',
  test: '演练',
}

/** The Feishu click listener's own states, in plain Chinese. */
export const FEISHU_LISTENER_STATE_LABELS: Record<string, string> = {
  ready: '就绪',
  starting: '启动中',
  backoff: '重连中',
  stopped: '已停止',
}

export const PHASE_LABELS: Record<string, string> = {
  daily: '每日沉淀',
  weekly: '周五复盘',
  both: '每日 + 周复盘',
  auto: '自动（按日期）',
}

export const SINK_TYPE_LABELS: Record<string, string> = {
  builtin_kb: '内置笔记库',
  local_markdown_wiki: '本地知识库',
}

/** Every rule status has its own label: "active or not" is not the question. */
export const RULE_STATUS_LABELS: Record<string, string> = {
  verified_active: '已验证生效',
  unverified_active: '试运行（待自然验证）',
  needs_revision: '需修订',
  dormant: '已休眠',
  retired: '已停用',
}

export function labelOf(
  map: Record<string, string>,
  key: string | null | undefined,
  fallback?: string
): string {
  if (!key) return fallback ?? '—'
  return map[key] ?? fallback ?? key
}

/**
 * Merge the unified dictionary labels over the local fallback table.
 *
 * `sys_dict_items` is the single display source the backend seeds on startup;
 * the local tables in this file only cover the case where the dictionary has
 * not loaded yet, so a badge never falls back to a raw enum code.
 */
export function dictLabels(
  dicts: Record<string, DictItem[]> | null | undefined,
  dictType: string,
  fallback: Record<string, string>
): Record<string, string> {
  const merged: Record<string, string> = { ...fallback }
  for (const item of dicts?.[dictType] ?? []) {
    if (item.code && item.label_zh) {
      merged[item.code] = item.label_zh
    }
  }
  return merged
}

export function formatDateYMD(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const diffSec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000))
  if (diffSec < 60) return '刚刚'
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin} 分钟前`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour} 小时前`
  const diffDay = Math.floor(diffHour / 24)
  if (diffDay === 1) return '昨天'
  if (diffDay < 7) return `${diffDay} 天前`
  return formatDateYMD(iso)
}

export function formatDuration(
  startedAt: string | null | undefined,
  finishedAt: string | null | undefined,
  nodes?: Array<{ duration_ms: number | null }>
): string {
  let ms = 0
  if (startedAt && finishedAt) {
    const a = new Date(startedAt).getTime()
    const b = new Date(finishedAt).getTime()
    if (!Number.isNaN(a) && !Number.isNaN(b) && b >= a) {
      ms = b - a
    }
  }
  if (ms === 0 && nodes) {
    ms = nodes.reduce((sum, n) => sum + (n.duration_ms || 0), 0)
  }
  const totalSec = Math.max(1, Math.round(ms / 1000))
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}
