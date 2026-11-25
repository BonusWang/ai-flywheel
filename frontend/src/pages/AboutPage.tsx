import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowRight, Wrench } from 'lucide-react'
import type { PageKey } from '../App'
import { api } from '../api'
import { CopyButton } from '../components/CodeBlock'
import { FlywheelRiver } from '../components/FlywheelRiver'
import { labelOf, RULE_STATUS_LABELS } from '../lib/labels'
import type { AboutPayload, RiverSource, RuleRecord } from '../types'

interface Props {
  onNavigate: (page: PageKey) => void
  refreshToken: number
}

const NO_SOURCES: RiverSource[] = []

const STAGE_CELL = [
  'sm:pr-6 lg:pr-6',
  'border-t sm:border-l sm:border-t-0 sm:pl-6 lg:px-6',
  'border-t sm:pr-6 lg:border-l lg:border-t-0 lg:px-6',
  'border-t sm:border-l sm:pl-6 lg:border-t-0 lg:px-6',
]

function formatFigure(n: number): { value: string; unit: string } {
  return n >= 10_000
    ? { value: (n / 10_000).toFixed(1).replace(/\.0$/, ''), unit: '万' }
    : { value: n.toLocaleString('en-US'), unit: '' }
}

function Figure({ n, unit }: { n: number; unit: string }) {
  const f = formatFigure(n)
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="num text-[30px] font-black leading-none tracking-[-0.03em] text-ink xl:text-[36px]">
        {f.value}
      </span>
      <span className="text-[13px] text-ink-3">
        {f.unit}
        {unit}
      </span>
    </div>
  )
}

function Kicker({ children }: { children: React.ReactNode }) {
  return (
    <div className="inline-flex items-center gap-2 text-[11.5px] font-semibold tracking-[0.18em] text-accent uppercase">
      <span className="inline-block h-[2px] w-4 rounded-full bg-accent" />
      <span>{children}</span>
    </div>
  )
}

function LatestRule({
  rule,
  onClick,
  className = '',
}: {
  rule: RuleRecord | undefined
  onClick: () => void
  className?: string
}) {
  if (!rule) return null
  return (
    <button
      type="button"
      onClick={onClick}
      className={`group block text-left ${className}`}
    >
      <span className="text-[11px] font-semibold tracking-[0.2em] text-accent">活跃先例规则</span>
      <span key={rule.rule_id} className="animate-fade-up mt-1.5 block">
        <span className="line-clamp-2 text-[13.5px] font-semibold leading-[1.55] text-ink transition-colors group-hover:text-accent">
          {rule.title}
        </span>
        <span className="mt-1 block truncate text-[12px] text-ink-4">
          {labelOf(RULE_STATUS_LABELS, rule.status)} · {rule.domain}
        </span>
      </span>
    </button>
  )
}

export const AboutPage: React.FC<Props> = ({ onNavigate, refreshToken }) => {
  const [data, setData] = useState<AboutPayload | null>(null)
  const [focus, setFocus] = useState<number | null>(null)
  const [at, setAt] = useState(0)
  const shown = useRef(0)

  useEffect(() => {
    api
      .about()
      .then((res) => setData(res))
      .catch(() => {})
  }, [refreshToken])

  const sources = useMemo(() => data?.river_sources ?? NO_SOURCES, [data])
  const rules = data?.recent_rules ?? []

  const onArrive = useCallback(() => {
    const now = Date.now()
    if (now - shown.current < 2800 || rules.length < 2) return
    shown.current = now
    setAt((i) => (i + 1) % rules.length)
  }, [rules.length])

  if (!data) {
    return <div className="py-20 text-center text-[13px] text-ink-4">正在加载关于页与接口清单...</div>
  }

  const { site, metrics, endpoints, mcp_tools } = data
  const brand = site.brand
  const verifiedRules = metrics.rules_by_status.verified_active || 0
  const unverifiedRules = metrics.rules_by_status.unverified_active || 0
  const activeRules = verifiedRules + unverifiedRules

  const stages = [
    {
      no: '01',
      title: '启程激活',
      figure: <Figure n={metrics.preflight_activations} unit="次预检" />,
      text: '任务开局通过 preflight_activate 注入反偷懒契约、协作偏好与命中先例，并签发不可伪造的哈希回执；交付时由 record_outcome 执行 real / test / static 证据门禁，没有真实证据不给过。',
      note:
        metrics.probed_skills_count > 0
          ? `当前已探测 ${metrics.probed_skills_count} 个本地可路由技能`
          : '支持 Codex、Claude Code、Claude Desktop 统一接入',
    },
    {
      no: '02',
      title: '异常与复盘',
      figure: <Figure n={metrics.open_retro_count} unit="条待复盘" />,
      text: '执行中触发重复报错死循环、高风险操作或验证回归时，自动记录异常信号并推入复盘队列；普通失误先按根因聚类，满 3 次才晋升为规则。',
      note: `累计登记 ${metrics.sessions_total} 个会话 · 待处理 ${metrics.sessions_pending} 个`,
    },
    {
      no: '03',
      title: '规则沉淀',
      figure: <Figure n={activeRules} unit="条活跃规则" />,
      text: '新规则先以「试运行」立即生效防复发，经跨会话自然真实验证后晋升「已验证生效」；分歧与反例保留不覆盖，并按 30/90/180 天新鲜度衰减、周五蒸馏持续做减法。',
      note: `已验证 ${verifiedRules} 条 · 待自然验证 ${unverifiedRules} 条`,
    },
    {
      no: '04',
      title: '闭环运行',
      figure: <Figure n={metrics.workflow_runs_total} unit="次工作流" />,
      text: 'P00–P07 受控沉淀流水线定时或按需审查会话：P02/P03 严格只读，审批通过后 P04 才执行唯一受控写入，把有效规律安全回写至知识库与防线。',
      note: '同时暴露 MCP、Hook、REST API 与 /llms.txt',
    },
  ]

  return (
    <div className="mx-auto max-w-[var(--page-max-reading)] pb-14 pt-2 lg:pt-1">
      {/* 1. AIHOT-inspired Editorial Hero */}
      <header className="grid items-end gap-8 lg:grid-cols-[minmax(0,1fr)_auto]">
        <div>
          <Kicker>关于 {brand.name}</Kicker>
          <h1 className="mt-5 text-[30px] font-black leading-[1.2] tracking-[-0.03em] text-ink [text-wrap:balance] sm:text-[40px] xl:text-[48px]">
            让每次 AI 协作都有证据，
            <br />
            <span className="text-accent">把踩过的坑变成防线。</span>
          </h1>
          <p className="mt-5 max-w-[40em] text-[15.5px] leading-[1.85] text-ink-3 xl:text-[16.5px]">
            {brand.motto}。作为面向多 AI 编码工具的本地优先「
            <span className="font-medium text-ink">治理与进化控制面</span>
            」，以执行纪律与实证门禁、抗熵增进化引擎和受控审批流水线，围绕「
            <span className="font-medium text-ink">{brand.subtitle}</span>
            」把跨客户端的{' '}
            <span className="num font-semibold text-ink">{metrics.preflight_activations}</span>{' '}
            次预检激活与{' '}
            <span className="num font-semibold text-ink">{activeRules}</span>{' '}
            条先例规则收拢成一套实时可验证的协作飞轮——以可审计、可回溯的工程闭环守住交付质量，而不是黑盒聊天记忆库。
          </p>
        </div>
        <div className="flex flex-wrap gap-3 lg:pb-2">
          <button
            type="button"
            onClick={() => onNavigate('runs')}
            className="inline-flex h-10 items-center gap-1.5 rounded-[var(--radius-control)] bg-accent px-4 text-[14px] font-semibold text-accent-contrast transition-colors hover:bg-accent-ink"
          >
            查看沉淀与审批 <ArrowRight className="w-4 h-4" />
          </button>
          <button
            type="button"
            onClick={() => onNavigate('settings')}
            className="inline-flex h-10 items-center gap-1.5 rounded-[var(--radius-control)] border border-line bg-surface px-4 text-[14px] font-medium text-ink transition-colors hover:border-line-strong hover:bg-bg-sunk"
          >
            接入与设置
          </button>
        </div>
      </header>

      {/* 2. Interactive Signal River + 4-Stage Real-Time Metrics */}
      <section aria-labelledby="how-flywheel-works" className="mt-10 xl:mt-14">
        <h2 id="how-flywheel-works" className="sr-only">
          {brand.name} 怎么工作
        </h2>
        <FlywheelRiver
          sources={sources}
          focus={focus}
          onArrive={onArrive}
          className="h-[220px] sm:h-[290px] lg:h-[340px] 2xl:h-[390px]"
        >
          <LatestRule
            rule={rules[at]}
            onClick={() => onNavigate('rules')}
            className="absolute left-[75%] top-[calc(42%+42px)] hidden w-[25%] px-6 lg:block"
          />
        </FlywheelRiver>
        <p className="sr-only">
          示意图：左侧每条线代表一次 AI 会话、异常信号或先例规则；线汇成复盘簇，经过真实验证闸门，只有带真实运行证据的规律进入右侧活跃防线与知识库。
        </p>
        <LatestRule
          rule={rules[at]}
          onClick={() => onNavigate('rules')}
          className="mt-2 border-t border-line pt-4 lg:hidden"
        />

        <ol className="mt-4 grid grid-cols-1 border-t border-line-strong sm:grid-cols-2 lg:mt-0 lg:grid-cols-4">
          {stages.map((s, i) => (
            <li
              key={s.no}
              tabIndex={0}
              onPointerEnter={() => setFocus(i)}
              onPointerLeave={() => setFocus(null)}
              onFocus={() => setFocus(i)}
              onBlur={() => setFocus(null)}
              className={`border-line py-6 outline-none transition-colors ${STAGE_CELL[i]} ${
                focus === i ? 'bg-accent-softer' : ''
              }`}
            >
              <div className="flex items-baseline gap-2.5">
                <span className="num text-[12px] font-bold tracking-[0.12em] text-accent">
                  {s.no}
                </span>
                <h3 className="text-[17px] font-bold text-ink">{s.title}</h3>
              </div>
              <div className="mt-4">{s.figure}</div>
              <p className="mt-3 text-[13.5px] leading-[1.8] text-ink-3">{s.text}</p>
              <p className="mt-3 text-[12px] text-ink-4">{s.note}</p>
            </li>
          ))}
        </ol>
      </section>

      {/* 3. MCP Protocol & Capabilities (2-Column Editorial Layout) */}
      <section
        aria-labelledby="mcp-protocol"
        className="mt-16 grid gap-10 border-t border-line pt-12 xl:mt-20 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.08fr)] lg:gap-14"
      >
        <div className="min-w-0">
          <Kicker>MCP PROTOCOL · 标准接入协议</Kicker>
          <h2
            id="mcp-protocol"
            className="mt-4 text-[24px] font-black leading-[1.3] tracking-[-0.02em] text-ink xl:text-[30px]"
          >
            标准 MCP 接入端点
          </h2>
          <p className="mt-3.5 text-[14.5px] leading-[1.85] text-ink-3">
            支持 Claude Code、Codex、Claude Desktop、Antigravity 等任意 MCP 客户端旁路接入，通过 Streamable HTTP 或 Stdio CLI 直接调用执行期门禁与后台沉淀能力。
          </p>

          {/* MCP Endpoints */}
          <div className="mt-6 space-y-2.5">
            <div className="text-[12px] font-semibold tracking-[0.06em] text-ink-4 uppercase">
              MCP Endpoint
            </div>
            <div className="card flex items-center justify-between gap-3 px-4 py-3">
              <div className="min-w-0">
                <div className="text-[11.5px] text-ink-4">Streamable HTTP</div>
                <div className="mono mt-0.5 truncate text-[13px] font-medium text-ink">
                  {endpoints.mcp_http_url}
                </div>
              </div>
              <CopyButton text={endpoints.mcp_http_url} className="shrink-0" />
            </div>
            <div className="card flex items-center justify-between gap-3 px-4 py-3">
              <div className="min-w-0">
                <div className="text-[11.5px] text-ink-4">Stdio CLI</div>
                <div className="mono mt-0.5 truncate text-[13px] font-medium text-ink">
                  {endpoints.mcp_stdio_cmd}
                </div>
              </div>
              <CopyButton text={endpoints.mcp_stdio_cmd} className="shrink-0" />
            </div>
          </div>
        </div>

        {/* Right Column: Available MCP Tools */}
        <div className="min-w-0">
          <div className="flex items-center justify-between">
            <Kicker>MCP TOOLS · {mcp_tools.length} 个核心工具</Kicker>
            <button
              type="button"
              onClick={() => onNavigate('settings')}
              className="inline-flex items-center gap-1 text-[12.5px] font-medium text-accent hover:underline"
            >
              配置客户端接入 <ArrowRight className="w-3.5 h-3.5" />
            </button>
          </div>
          <h3 className="mt-4 text-[20px] font-bold leading-snug text-ink xl:text-[24px]">
            可用 MCP 工具清单
          </h3>
          <p className="mt-2 text-[13.5px] leading-[1.75] text-ink-3">
            覆盖「登记 → 启程预检 → 异常上报 → 证据回执 → 会话入队 → 全局态势」完整执行闭环。
          </p>

          <ul className="mt-5 divide-y divide-line border-y border-line">
            {mcp_tools.map((tool, idx) => (
              <li key={tool.name} className="py-3.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="num text-[11px] font-bold text-ink-4">
                      {String(idx + 1).padStart(2, '0')}
                    </span>
                    <code className="mono truncate text-[13.5px] font-semibold text-accent">
                      {tool.name}
                    </code>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <span className="chip">{tool.phase}</span>
                    <span className="text-[12px] font-medium text-ink-2">{tool.title_zh}</span>
                  </div>
                </div>
                <p className="mt-1.5 pl-6 text-[13px] leading-[1.7] text-ink-3">
                  {tool.description_zh || tool.description}
                </p>
              </li>
            ))}
          </ul>

          <div className="mt-5 card-quiet flex items-center justify-between gap-4 px-4 py-3.5">
            <div className="flex items-start gap-2.5 min-w-0">
              <Wrench className="w-4 h-4 shrink-0 text-accent mt-0.5" />
              <div className="text-[12.5px] leading-[1.7] text-ink-3">
                所有工具共享统一硬门禁：声明使用某条规则但缺少对应{' '}
                <code className="mono text-[11.5px] text-ink-2">real</code>{' '}
                运行证据时将拒绝回执，避免「伪闭环」污染先例库。
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* 4. Boundary Notice Well & Minimal Footer */}
      <p className="mt-14 well rounded-[var(--radius-card)] px-5 py-4 text-[13px] leading-[1.85] text-ink-3">
        <strong className="font-semibold text-ink">{brand.name}</strong>{' '}
        是面向多 AI 编码工具的本地优先「治理与进化控制面」：以执行纪律与实证门禁、抗熵增进化引擎和受控审批流水线守住交付质量与资产新鲜度。工程上采用「外层接入与模型路由 × 公共动态飞轮引擎 × 私有个人配置层 × 长期知识库」3+1 分层解耦架构：本引擎只承载通用方法论、先例路由、回执验真与 P00–P07 审批工作流，私有偏好与个人知识库通过外部目录挂载并默认开启审批写门禁；公开导出接口（
        <a
          href="/api/v1/overview"
          target="_blank"
          rel="noreferrer"
          className="mono text-accent hover:underline"
        >
          /api/v1/overview
        </a>
        ）自动脱敏本地路径。
      </p>

      <footer className="mt-8 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-5 text-[12.5px] text-ink-4">
        <span>
          {brand.name} · v{brand.version} · {brand.tagline}
        </span>
        <nav className="flex flex-wrap gap-5" aria-label="机器可读端点">
          <a href="/llms.txt" target="_blank" rel="noreferrer" className="transition-colors hover:text-accent">
            llms.txt
          </a>
          <a href="/api/v1/about" target="_blank" rel="noreferrer" className="transition-colors hover:text-accent">
            About JSON
          </a>
          <a href="/api/v1/overview" target="_blank" rel="noreferrer" className="transition-colors hover:text-accent">
            API v1
          </a>
        </nav>
      </footer>
    </div>
  )
}
