import React, { useEffect, useState } from 'react'
import { ArrowUpRight, RotateCcw, Save } from 'lucide-react'
import type { PageKey } from '../App'
import { api } from '../api'
import { CodeBlock, CopyButton } from '../components/CodeBlock'
import { AsideCard, PillTabs, ReadingLayout } from '../components/PageLayout'
import type { ConnectPayload } from '../types'

interface Props {
  section: 'connect' | 'policies' | 'adapters'
  onChangeSection: (page: PageKey) => void
  onGlobalRefresh: () => void
}

const MCP_TOOLS: Array<[string, string]> = [
  ['flywheel_register_session', '登记 AI 客户端会话元数据与本地日志路径'],
  ['flywheel_preflight_activate', '开局获取反偷懒策略、个人偏好、命中规则与不可变回执 ID'],
  ['flywheel_report_signal', '上报死循环、高危操作、复发或阻塞信号入复盘队列'],
  ['flywheel_record_outcome', '提交分级验证回执（real/test/static/unknown），驱动规则晋升/降级'],
  ['flywheel_ingest_session', '无本地日志文件的客户端（如 Claude Desktop）直接推送会话摘要'],
  ['flywheel_status', '查询飞轮健康指标、活跃规则分布与最新 P00–P07 运行状态'],
]

export const ConnectPolicyPage: React.FC<Props> = ({ section, onGlobalRefresh }) => {
  const [connectData, setConnectData] = useState<ConnectPayload | null>(null)
  const [policies, setPolicies] = useState<Record<string, any>>({})
  const [selectedClient, setSelectedClient] = useState('codex')
  const [notice, setNotice] = useState<string | null>(null)

  const [antiEnabled, setAntiEnabled] = useState(true)
  const [baselineText, setBaselineText] = useState('')
  const [execText, setExecText] = useState('')
  const [addressAs, setAddressAs] = useState('')
  const [redlinesText, setRedlinesText] = useState('')
  const [retryMax, setRetryMax] = useState(2)

  const [overrideDir, setOverrideDir] = useState('')
  const [skillsDirs, setSkillsDirs] = useState('')
  const [kbType, setKbType] = useState('builtin_kb')
  const [kbRoot, setKbRoot] = useState('./data/knowledge')
  const [runnerMode, setRunnerMode] = useState('dry_run')
  const [apiBaseUrl, setApiBaseUrl] = useState('')
  const [apiModel, setApiModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [cliCmd, setCliCmd] = useState('')
  const [cliArgs, setCliArgs] = useState('')
  const [autoApprove, setAutoApprove] = useState(false)
  const [workflowEnabled, setWorkflowEnabled] = useState(true)
  const [scheduleEnabled, setScheduleEnabled] = useState(false)

  const loadAll = async () => {
    const [cRes, pRes] = await Promise.all([api.connectInfo(), api.listPolicies()])
    setConnectData(cRes)
    setPolicies(pRes.policies)

    const al = pRes.policies.anti_laziness || {}
    setAntiEnabled(al.enabled !== false)
    setBaselineText((al.baseline?.rules || []).join('\n'))
    setExecText((al.execution?.rules || []).join('\n'))

    const up = pRes.policies.user_preferences || {}
    setAddressAs(up.address_as || '')
    setRedlinesText((up.non_negotiables || []).join('\n'))

    const hg = pRes.policies.hard_gates || {}
    setRetryMax(Number(hg.retry_breaker?.max_same_cause_retries ?? 2))

    const cfg = cRes.config || {}
    setOverrideDir(cfg.profile?.override_dir || '')
    setSkillsDirs((cfg.capabilities?.skills_dirs || []).join('\n'))
    setKbType(cfg.knowledge_sink?.type || 'builtin_kb')
    setKbRoot(cfg.knowledge_sink?.root || './data/knowledge')
    setRunnerMode(cfg.runner?.mode || 'dry_run')
    setApiBaseUrl(cfg.runner?.api?.base_url || '')
    setApiModel(cfg.runner?.api?.model || '')
    setApiKey(cfg.runner?.api?.api_key || '')
    setCliCmd(cfg.runner?.cli?.command || '')
    setCliArgs((cfg.runner?.cli?.args || []).join(' '))
    setAutoApprove(
      Boolean(cfg.workflow?.auto_approve_writes && cfg.safety_valves?.auto_approve_writes)
    )
    setWorkflowEnabled(cfg.safety_valves?.workflow_enabled !== false)
    setScheduleEnabled(Boolean(cfg.workflow?.schedule_enabled))
  }

  useEffect(() => {
    loadAll().catch(() => {})
  }, [])

  const showToast = (msg: string) => {
    setNotice(msg)
    setTimeout(() => setNotice(null), 3200)
  }

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
    await loadAll()
    onGlobalRefresh()
    showToast('飞轮策略已保存并实时生效（外部 MCP 与 Preflight 立即同步）')
  }

  const resetAllPolicies = async () => {
    await Promise.all([
      api.resetPolicy('anti_laziness'),
      api.resetPolicy('user_preferences'),
      api.resetPolicy('hard_gates'),
    ])
    await loadAll()
    showToast('已恢复为 profile / Home Repo 继承策略')
  }

  const saveAdapters = async () => {
    await api.updateConfig({
      profile: {
        override_dir: overrideDir.trim() || null,
      },
      capabilities: {
        skills_dirs: skillsDirs.split('\n').map((s) => s.trim()).filter(Boolean),
      },
      knowledge_sink: {
        type: kbType,
        root: kbRoot.trim() || './data/knowledge',
      },
      runner: {
        mode: runnerMode,
        api: {
          base_url: apiBaseUrl.trim(),
          model: apiModel.trim(),
          api_key: apiKey.trim(),
        },
        cli: {
          command: cliCmd.trim(),
          args: cliArgs.trim() ? cliArgs.trim().split(/\s+/) : [],
        },
      },
      workflow: {
        schedule_enabled: scheduleEnabled,
        auto_approve_writes: autoApprove,
      },
      safety_valves: {
        workflow_enabled: workflowEnabled,
        auto_approve_writes: autoApprove,
      },
    })
    await loadAll()
    onGlobalRefresh()
    showToast('三体挂载与安全阀配置已持久化至 flywheel.yaml')
  }

  const clientObj =
    connectData?.guides.clients.find((c) => c.id === selectedClient) ||
    connectData?.guides.clients[0]

  // ---------------- SECTION 1: AIHOT-faithful /agent Connect Page ----------------
  if (section === 'connect') {
    const aside = (
      <>
        <AsideCard title="服务接入状态">
          <div className="space-y-2.5 text-[12.5px]">
            <div className="flex items-center justify-between">
              <span className="text-ink-3">MCP Streamable HTTP</span>
              <span className="inline-flex items-center gap-1.5 text-ok-ink font-medium">
                <span className="size-1.5 rounded-full bg-ok" />
                正常
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ink-3">MCP Stdio</span>
              <code className="mono text-[11.5px] text-ink-2">flywheel mcp-stdio</code>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-ink-3">已探测本地 Skills</span>
              <span className="num font-semibold text-ink">
                {connectData?.probed_skills.length ?? 0} 个
              </span>
            </div>
          </div>
        </AsideCard>

        <AsideCard title="机器可读入口" className="!bg-panel-quiet">
          <ul className="space-y-2.5 text-[12.5px]">
            {[
              ['llms.txt', '/llms.txt', '给大模型读的站点说明'],
              ['MCP Server', '/mcp', 'MCP 客户端的连接地址'],
              ['REST API v1', '/api/v1/overview', '统一读取层 JSON 接口'],
            ].map(([label, href, note]) => (
              <li key={href} className="pb-2 border-b border-line-soft last:border-b-0 last:pb-0">
                <a
                  href={href}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center justify-between font-medium text-ink hover:text-accent"
                >
                  <span>{label}</span>
                  <ArrowUpRight className="w-3.5 h-3.5 text-ink-4" />
                </a>
                <div className="text-[11.5px] text-ink-4 mt-0.5">{note}</div>
              </li>
            ))}
          </ul>
        </AsideCard>

        {connectData && connectData.probed_skills.length > 0 && (
          <AsideCard title={`已探测 Skills (${connectData.probed_skills.length})`}>
            <div className="flex flex-wrap gap-1.5 max-h-48 overflow-y-auto">
              {connectData.probed_skills.slice(0, 24).map((sk) => (
                <span
                  key={sk.name}
                  title={sk.description}
                  className="mono text-[11px] px-2 py-0.5 rounded-[var(--radius-mark)] bg-bg-sunk border border-line-soft text-ink-2"
                >
                  {sk.name}
                </span>
              ))}
              {connectData.probed_skills.length > 24 && (
                <span className="text-[11px] text-ink-4">
                  +{connectData.probed_skills.length - 24} 更多
                </span>
              )}
            </div>
          </AsideCard>
        )}
      </>
    )

    return (
      <ReadingLayout aside={aside}>
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">Agent 接入</h1>
          <p className="mt-2 text-[14.5px] text-ink-3">
            加一个本地 MCP 地址与极薄转发 Hook，让 Codex、Claude Code 与 Claude Desktop 共享同一套反偷懒策略、先例规则与后台 P00–P07 飞轮。
          </p>

          {connectData && (
            <>
              {/* AIHOT signature endpoint copy bar */}
              <div className="mt-5 flex items-center gap-2 rounded-[var(--radius-card)] border border-line bg-surface p-3">
                <code className="mono min-w-0 flex-1 truncate text-[13px] text-ink">
                  {connectData.guides.endpoints.mcp_http_url}
                </code>
                <CopyButton text={connectData.guides.endpoints.mcp_http_url} />
              </div>

              <div className="mt-6">
                <PillTabs
                  items={connectData.guides.clients.map((c) => ({ key: c.id, label: c.name }))}
                  active={selectedClient}
                  onChange={setSelectedClient}
                />
              </div>

              {clientObj && (
                <div className="mt-5">
                  <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                    <h2 className="text-[18px] font-bold text-ink">{clientObj.name} 接入配置</h2>
                    <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-[11.5px] bg-ok-soft text-ok-ink border border-ok/25">
                      {clientObj.loop_guarantee}
                    </span>
                  </div>
                  <p className="text-[13px] text-ink-3">{clientObj.notes}</p>

                  <CodeBlock title={`1. ${clientObj.mcp_label}`} code={clientObj.mcp_config} />
                  <CodeBlock title={`2. ${clientObj.hook_label}`} code={clientObj.hook_config} />
                </div>
              )}

              <section className="mt-8">
                <h3 className="mb-3 text-[16px] font-bold text-ink">连上后应看到这六个工具</h3>
                <ul className="space-y-2 text-[13.5px] leading-[1.75] text-ink-2">
                  {MCP_TOOLS.map(([name, desc]) => (
                    <li key={name} className="flex gap-2">
                      <span className="mt-[10px] size-1 shrink-0 rounded-full bg-ink-4" />
                      <span>
                        <code className="mono rounded-[var(--radius-mark)] bg-bg-sunk px-1.5 py-0.5 text-[0.88em] text-ink">
                          {name}
                        </code>
                        ：{desc}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            </>
          )}
        </div>
      </ReadingLayout>
    )
  }

  // ---------------- SECTION 2: Flywheel-Owned Policy Studio ----------------
  if (section === 'policies') {
    return (
      <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5">
        <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
          <div>
            <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">策略中心</h1>
            <p className="text-[13px] text-ink-3 mt-1">
              飞轮方法论设定独立于知识库托管（合并优先级：默认 <code className="mono">profile/</code> → Home Repo <code className="mono">override_dir</code> → 界面覆盖）
            </p>
          </div>
          <div className="flex items-center gap-2">
            {notice && (
              <span className="px-3 py-1 rounded-full bg-ok-soft text-ok-ink text-[12px] border border-ok/25">
                {notice}
              </span>
            )}
            <button
              type="button"
              onClick={resetAllPolicies}
              className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-3 hover:text-ink"
            >
              <RotateCcw className="w-3.5 h-3.5" />
              恢复继承默认
            </button>
            <button
              type="button"
              onClick={savePolicies}
              className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink"
            >
              <Save className="w-3.5 h-3.5" />
              保存并实时生效
            </button>
          </div>
        </header>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 text-[12.5px]">
          <section className="card p-5 space-y-3.5">
            <div className="flex items-center justify-between pb-2.5 border-b border-line-soft">
              <div>
                <h2 className="text-[14.5px] font-bold text-ink">反偷懒执行契约（Anti-Laziness）</h2>
                <span className="mono text-[11px] text-ink-4">
                  当前来源：{policies.anti_laziness?._source || 'default'}
                </span>
              </div>
              <label className="inline-flex items-center gap-2 cursor-pointer text-ink-2">
                <input
                  type="checkbox"
                  checked={antiEnabled}
                  onChange={(e) => setAntiEnabled(e.target.checked)}
                />
                <span>启用</span>
              </label>
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">基线规则（每条一行，所有任务注入）</label>
              <textarea
                rows={4}
                value={baselineText}
                onChange={(e) => setBaselineText(e.target.value)}
                className="field-input mono w-full p-2.5 text-[12px] leading-relaxed"
              />
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">执行模式规则（代码/调试/部署任务追加）</label>
              <textarea
                rows={6}
                value={execText}
                onChange={(e) => setExecText(e.target.value)}
                className="field-input mono w-full p-2.5 text-[12px] leading-relaxed"
              />
            </div>
          </section>

          <section className="card p-5 space-y-3.5">
            <div className="pb-2.5 border-b border-line-soft">
              <h2 className="text-[14.5px] font-bold text-ink">个人协作偏好与红线（不含隐私履历）</h2>
              <span className="mono text-[11px] text-ink-4">
                当前来源：{policies.user_preferences?._source || 'default'}
              </span>
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">AI 对你的称呼</label>
              <input
                value={addressAs}
                onChange={(e) => setAddressAs(e.target.value)}
                className="field-input w-full px-3 py-2 text-[13px]"
              />
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">不可让步的协作红线（每条一行）</label>
              <textarea
                rows={6}
                value={redlinesText}
                onChange={(e) => setRedlinesText(e.target.value)}
                className="field-input mono w-full p-2.5 text-[12px] leading-relaxed"
              />
            </div>
            <div>
              <label className="block text-ink-3 mb-1.5">
                死循环保护阈值（同工具同因最多重试次数，第 N+1 次触发告警）
              </label>
              <input
                type="number"
                min={1}
                max={5}
                value={retryMax}
                onChange={(e) => setRetryMax(Number(e.target.value))}
                className="field-input mono w-28 px-3 py-1.5"
              />
            </div>
          </section>
        </div>
      </div>
    )
  }

  // ---------------- SECTION 3: Three-Layer Adapters & Safety Valves ----------------
  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5 text-[12.5px]">
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">三体挂载与安全阀</h1>
          <p className="text-[13px] text-ink-3 mt-1">
            Home Repo · Wiki · 本地 CLI 全部通过被 gitignore 的 <code className="mono">flywheel.yaml</code> 指针挂载，不污染公开仓库
          </p>
        </div>
        <div className="flex items-center gap-2">
          {notice && (
            <span className="px-3 py-1 rounded-full bg-ok-soft text-ok-ink text-[12px] border border-ok/25">
              {notice}
            </span>
          )}
          <button
            type="button"
            onClick={saveAdapters}
            className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink"
          >
            <Save className="w-3.5 h-3.5" />
            保存配置
          </button>
        </div>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <section className="card p-5 space-y-3">
          <h2 className="text-[14.5px] font-bold text-ink">1. Home Repo & 本地 Skill 只读探测</h2>
          <div>
            <label className="block text-ink-3 mb-1">私有 Profile 覆盖目录</label>
            <input
              value={overrideDir}
              onChange={(e) => setOverrideDir(e.target.value)}
              placeholder="例如：E:/github/ai-home/flywheel-profile"
              className="field-input mono w-full px-3 py-2 text-[12px]"
            />
          </div>
          <div>
            <label className="block text-ink-3 mb-1">本地 Skills 探测目录（每行一个，只读扫描）</label>
            <textarea
              rows={2}
              value={skillsDirs}
              onChange={(e) => setSkillsDirs(e.target.value)}
              placeholder="例如：E:/github/ai-home/skills"
              className="field-input mono w-full p-2.5 text-[12px]"
            />
          </div>
        </section>

        <section className="card p-5 space-y-3">
          <h2 className="text-[14.5px] font-bold text-ink">2. 知识库落点适配器（与策略解耦）</h2>
          <div>
            <label className="block text-ink-3 mb-1">落点模式</label>
            <select
              value={kbType}
              onChange={(e) => setKbType(e.target.value)}
              className="field-input w-full px-3 py-2 text-[12.5px]"
            >
              <option value="builtin_kb">内置默认 Markdown 知识库（零配置开箱即用）</option>
              <option value="local_markdown_wiki">挂载外部本地 Markdown / Obsidian / Wiki 目录</option>
            </select>
          </div>
          <div>
            <label className="block text-ink-3 mb-1">知识库根目录路径</label>
            <input
              value={kbRoot}
              onChange={(e) => setKbRoot(e.target.value)}
              className="field-input mono w-full px-3 py-2 text-[12px]"
            />
          </div>
        </section>

        <section className="card p-5 space-y-3">
          <h2 className="text-[14.5px] font-bold text-ink">3. 后台执行大脑（OpenAI 兼容 API / 本地 CLI / Dry-run）</h2>
          <div>
            <label className="block text-ink-3 mb-1">执行模式</label>
            <select
              value={runnerMode}
              onChange={(e) => setRunnerMode(e.target.value)}
              className="field-input w-full px-3 py-2 text-[12.5px]"
            >
              <option value="api">OpenAI 兼容 API（可直连本地 Magpie 网关 / DeepSeek / Ollama）</option>
              <option value="cli">可配置本地 CLI 子进程模式（复用本机订阅额度）</option>
              <option value="dry_run">Dry-run 零成本本地启发式模式</option>
            </select>
          </div>
          {runnerMode === 'api' && (
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
              <div>
                <label className="block text-ink-3 mb-1">Base URL</label>
                <input
                  value={apiBaseUrl}
                  onChange={(e) => setApiBaseUrl(e.target.value)}
                  placeholder="http://127.0.0.1:3425/v1"
                  className="field-input mono w-full px-2.5 py-1.5 text-[12px]"
                />
              </div>
              <div>
                <label className="block text-ink-3 mb-1">Model / 路由组</label>
                <input
                  value={apiModel}
                  onChange={(e) => setApiModel(e.target.value)}
                  placeholder="group/cheap-first"
                  className="field-input mono w-full px-2.5 py-1.5 text-[12px]"
                />
              </div>
              <div>
                <label className="block text-ink-3 mb-1">API Key</label>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="magpie / sk-..."
                  className="field-input mono w-full px-2.5 py-1.5 text-[12px]"
                />
              </div>
            </div>
          )}
          {runnerMode === 'cli' && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              <div>
                <label className="block text-ink-3 mb-1">CLI 可执行命令/路径</label>
                <input
                  value={cliCmd}
                  onChange={(e) => setCliCmd(e.target.value)}
                  placeholder="如 claude / codex"
                  className="field-input mono w-full px-3 py-2 text-[12px]"
                />
              </div>
              <div>
                <label className="block text-ink-3 mb-1">参数模板（支持 {'{prompt}'}）</label>
                <input
                  value={cliArgs}
                  onChange={(e) => setCliArgs(e.target.value)}
                  placeholder="-p {prompt}"
                  className="field-input mono w-full px-3 py-2 text-[12px]"
                />
              </div>
            </div>
          )}
        </section>

        <section className="card p-5 space-y-3">
          <h2 className="text-[14.5px] font-bold text-ink">4. 定时调度与安全阀（Safety Valves）</h2>
          <label className="flex items-center justify-between p-3 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft cursor-pointer">
            <div>
              <div className="font-medium text-ink">启用每日 20:00 自动定时运转（schedule_enabled）</div>
              <div className="text-[11.5px] text-ink-4">到点自主跑 P00–P03，默认停在画布等你审批放行</div>
            </div>
            <input
              type="checkbox"
              checked={scheduleEnabled}
              onChange={(e) => setScheduleEnabled(e.target.checked)}
            />
          </label>
          <label className="flex items-center justify-between p-3 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft cursor-pointer">
            <div>
              <div className="font-medium text-ink">允许执行后台工作流（WORKFLOW_ENABLED）</div>
              <div className="text-[11.5px] text-ink-4">关闭时阻断所有 P00–P07 触发</div>
            </div>
            <input
              type="checkbox"
              checked={workflowEnabled}
              onChange={(e) => setWorkflowEnabled(e.target.checked)}
            />
          </label>
          <label className="flex items-center justify-between p-3 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft cursor-pointer">
            <div>
              <div className="font-medium text-ink">无人值守全自动写入（AUTO_APPROVE_WRITES）</div>
              <div className="text-[11.5px] text-ink-4">默认关闭（P03 后审核放行）；开启后直通 P04–P07</div>
            </div>
            <input
              type="checkbox"
              checked={autoApprove}
              onChange={(e) => setAutoApprove(e.target.checked)}
            />
          </label>
        </section>
      </div>
    </div>
  )
}
