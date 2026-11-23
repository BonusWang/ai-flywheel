import React, { useEffect, useState } from 'react'
import {
  Check,
  Copy,
  Cpu,
  Plug,
  RotateCcw,
  Save,
  ShieldCheck,
  Sliders,
} from 'lucide-react'
import { api } from '../api'
import type { ConnectPayload } from '../types'

interface Props {
  onGlobalRefresh: () => void
}

export const ConnectPolicyPage: React.FC<Props> = ({ onGlobalRefresh }) => {
  const [subTab, setSubTab] = useState<'connect' | 'policies' | 'adapters'>('connect')
  const [connectData, setConnectData] = useState<ConnectPayload | null>(null)
  const [policies, setPolicies] = useState<Record<string, any>>({})
  const [selectedClient, setSelectedClient] = useState('codex')
  const [copiedKey, setCopiedKey] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Editable policy fields
  const [antiEnabled, setAntiEnabled] = useState(true)
  const [baselineText, setBaselineText] = useState('')
  const [execText, setExecText] = useState('')
  const [addressAs, setAddressAs] = useState('')
  const [redlinesText, setRedlinesText] = useState('')
  const [retryMax, setRetryMax] = useState(2)

  // Editable adapter/config fields
  const [overrideDir, setOverrideDir] = useState('')
  const [skillsDirs, setSkillsDirs] = useState('')
  const [kbType, setKbType] = useState('builtin_kb')
  const [kbRoot, setKbRoot] = useState('./data/knowledge')
  const [runnerMode, setRunnerMode] = useState('dry_run')
  const [cliCmd, setCliCmd] = useState('')
  const [cliArgs, setCliArgs] = useState('')
  const [autoApprove, setAutoApprove] = useState(false)
  const [workflowEnabled, setWorkflowEnabled] = useState(true)

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
    setCliCmd(cfg.runner?.cli?.command || '')
    setCliArgs((cfg.runner?.cli?.args || []).join(' '))
    setAutoApprove(Boolean(cfg.workflow?.auto_approve_writes && cfg.safety_valves?.auto_approve_writes))
    setWorkflowEnabled(cfg.safety_valves?.workflow_enabled !== false)
  }

  useEffect(() => {
    loadAll().catch(() => {})
  }, [])

  const copyText = async (key: string, text: string) => {
    await navigator.clipboard.writeText(text)
    setCopiedKey(key)
    setTimeout(() => setCopiedKey(null), 1800)
  }

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
    showToast('已恢复为 profile/ 默认策略设定')
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
        cli: {
          command: cliCmd.trim(),
          args: cliArgs.trim() ? cliArgs.trim().split(/\s+/) : [],
        },
      },
      workflow: {
        auto_approve_writes: autoApprove,
      },
      safety_valves: {
        workflow_enabled: workflowEnabled,
        auto_approve_writes: autoApprove,
      },
    })
    await loadAll()
    onGlobalRefresh()
    showToast('适配器与安全阀配置已持久化至 flywheel.yaml')
  }

  const clientObj = connectData?.guides.clients.find((c) => c.id === selectedClient) || connectData?.guides.clients[0]

  return (
    <div className="space-y-5">
      {/* Sub-navigation */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="inline-flex p-1 rounded-xl bg-white border border-slate-200 shadow-xs">
          {(
            [
              ['connect', '三端 MCP 接入配置', Plug],
              ['policies', '飞轮策略中心（反偷懒/偏好）', ShieldCheck],
              ['adapters', '知识库 / Home Repo / CLI 适配', Sliders],
            ] as const
          ).map(([id, label, Icon]) => (
            <button
              key={id}
              onClick={() => setSubTab(id)}
              className={`inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-xs font-medium transition-colors cursor-pointer ${
                subTab === id ? 'bg-slate-900 text-white' : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              <Icon className="w-3.5 h-3.5" />
              {label}
            </button>
          ))}
        </div>

        {notice && (
          <div className="px-3.5 py-1.5 rounded-xl bg-emerald-50 border border-emerald-200 text-xs font-medium text-emerald-800">
            {notice}
          </div>
        )}
      </div>

      {/* TAB 1: Client MCP & Thin Hook Connect Guides */}
      {subTab === 'connect' && connectData && (
        <div className="space-y-5">
          <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs">
            <h2 className="text-sm font-semibold text-slate-900 mb-3">统一接入端点（对标 AIHOT /agent 体验）</h2>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
              {[
                ['MCP Streamable HTTP', connectData.guides.endpoints.mcp_http_url],
                ['MCP Stdio 命令', connectData.guides.endpoints.mcp_stdio_cmd],
                ['Agent 发现文档 (llms.txt)', connectData.guides.endpoints.llms_txt_url],
              ].map(([label, val]) => (
                <div key={label} className="p-3 rounded-xl bg-slate-50 border border-slate-200/80">
                  <div className="text-[11px] text-slate-500 mb-1">{label}</div>
                  <div className="flex items-center justify-between gap-2">
                    <code className="text-xs text-slate-800 truncate">{val}</code>
                    <button
                      onClick={() => copyText(label, val)}
                      className="p-1 rounded hover:bg-slate-200 text-slate-500 shrink-0 cursor-pointer"
                      title="复制"
                    >
                      {copiedKey === label ? <Check className="w-3.5 h-3.5 text-emerald-600" /> : <Copy className="w-3.5 h-3.5" />}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                {connectData.guides.clients.map((c) => (
                  <button
                    key={c.id}
                    onClick={() => setSelectedClient(c.id)}
                    className={`px-3.5 py-1.5 rounded-xl text-xs font-semibold border transition-colors cursor-pointer ${
                      selectedClient === c.id
                        ? 'bg-blue-600 text-white border-blue-600'
                        : 'bg-slate-50 text-slate-700 border-slate-200 hover:bg-slate-100'
                    }`}
                  >
                    {c.name}
                  </button>
                ))}
              </div>
              {clientObj && (
                <span className="text-xs px-2.5 py-1 rounded-full bg-emerald-50 text-emerald-800 border border-emerald-200">
                  {clientObj.loop_guarantee}
                </span>
              )}
            </div>

            {clientObj && (
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-slate-800">1. {clientObj.mcp_label}</span>
                    <button
                      onClick={() => copyText(`mcp-${clientObj.id}`, clientObj.mcp_config)}
                      className="inline-flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700 cursor-pointer"
                    >
                      {copiedKey === `mcp-${clientObj.id}` ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                      复制
                    </button>
                  </div>
                  <pre className="p-3.5 rounded-xl bg-slate-950 text-slate-100 text-xs overflow-x-auto leading-relaxed">
                    {clientObj.mcp_config}
                  </pre>
                </div>

                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-slate-800">2. {clientObj.hook_label}</span>
                    <button
                      onClick={() => copyText(`hook-${clientObj.id}`, clientObj.hook_config)}
                      className="inline-flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700 cursor-pointer"
                    >
                      {copiedKey === `hook-${clientObj.id}` ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                      复制
                    </button>
                  </div>
                  <pre className="p-3.5 rounded-xl bg-slate-950 text-slate-100 text-xs overflow-x-auto leading-relaxed max-h-64">
                    {clientObj.hook_config}
                  </pre>
                </div>

                <div className="lg:col-span-2 text-xs text-slate-500 bg-slate-50 rounded-xl px-3.5 py-2.5 border border-slate-200/70">
                  说明：{clientObj.notes}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* TAB 2: Flywheel-Owned Policy Studio */}
      {subTab === 'policies' && (
        <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs space-y-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold text-slate-900">飞轮方法论策略中心（与知识库完全解耦）</h2>
              <p className="text-xs text-slate-500">
                所有反偷懒规则、个人协作偏好与死循环阈值均由飞轮服务独立托管；修改后无需重启，MCP 与薄 Hook 下次调用立即生效。
              </p>
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={resetAllPolicies}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl border border-slate-200 text-xs text-slate-600 hover:bg-slate-50 cursor-pointer"
              >
                <RotateCcw className="w-3.5 h-3.5" />
                恢复默认
              </button>
              <button
                onClick={savePolicies}
                className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-xl bg-slate-900 text-white text-xs font-medium hover:bg-slate-800 cursor-pointer"
              >
                <Save className="w-3.5 h-3.5" />
                保存策略并生效
              </button>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 text-xs">
            <div className="space-y-3 p-4 rounded-xl bg-slate-50 border border-slate-200/80">
              <div className="flex items-center justify-between">
                <span className="font-semibold text-slate-900">反偷懒执行契约（Anti-Laziness）</span>
                <label className="inline-flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={antiEnabled}
                    onChange={(e) => setAntiEnabled(e.target.checked)}
                  />
                  <span>启用</span>
                </label>
              </div>
              <div>
                <label className="block text-slate-500 mb-1">基线规则（每条一行，所有任务注入）</label>
                <textarea
                  rows={4}
                  value={baselineText}
                  onChange={(e) => setBaselineText(e.target.value)}
                  className="w-full p-2.5 rounded-lg border border-slate-200 bg-white font-mono text-[11px]"
                />
              </div>
              <div>
                <label className="block text-slate-500 mb-1">执行模式规则（代码/调试/部署任务追加）</label>
                <textarea
                  rows={6}
                  value={execText}
                  onChange={(e) => setExecText(e.target.value)}
                  className="w-full p-2.5 rounded-lg border border-slate-200 bg-white font-mono text-[11px]"
                />
              </div>
            </div>

            <div className="space-y-3 p-4 rounded-xl bg-slate-50 border border-slate-200/80">
              <div className="font-semibold text-slate-900">
                个人协作偏好与红线（不含隐私履历，来源：{policies.user_preferences?._source || 'default'}）
              </div>
              <div>
                <label className="block text-slate-500 mb-1">AI 对你的称呼</label>
                <input
                  value={addressAs}
                  onChange={(e) => setAddressAs(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 bg-white"
                />
              </div>
              <div>
                <label className="block text-slate-500 mb-1">不可让步的协作红线（每条一行）</label>
                <textarea
                  rows={5}
                  value={redlinesText}
                  onChange={(e) => setRedlinesText(e.target.value)}
                  className="w-full p-2.5 rounded-lg border border-slate-200 bg-white font-mono text-[11px]"
                />
              </div>
              <div>
                <label className="block text-slate-500 mb-1">
                  死循环保护阈值（同工具同因最多重试次数，第 N+1 次触发告警）
                </label>
                <input
                  type="number"
                  min={1}
                  max={5}
                  value={retryMax}
                  onChange={(e) => setRetryMax(Number(e.target.value))}
                  className="w-28 px-3 py-1.5 rounded-lg border border-slate-200 bg-white font-mono"
                />
              </div>
            </div>
          </div>
        </div>
      )}

      {/* TAB 3: Home Repo / Knowledge Sink / Local CLI Runner / Safety Valves */}
      {subTab === 'adapters' && (
        <div className="bg-white border border-slate-200/90 rounded-2xl p-5 shadow-xs space-y-5 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Cpu className="w-4 h-4 text-blue-600" />
              <div>
                <h2 className="text-sm font-semibold text-slate-900">三体挂载配置与安全阀（Home Repo · Wiki · 本地 CLI）</h2>
                <p className="text-xs text-slate-500">
                  全部持久化至本地被 gitignore 的 <code>flywheel.yaml</code>，绝不污染开源仓库。
                </p>
              </div>
            </div>
            <button
              onClick={saveAdapters}
              className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-xl bg-slate-900 text-white text-xs font-medium hover:bg-slate-800 cursor-pointer"
            >
              <Save className="w-3.5 h-3.5" />
              保存配置
            </button>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="p-4 rounded-xl bg-slate-50 border border-slate-200/80 space-y-3">
              <div className="font-semibold text-slate-900">1. Home Repo & 本地 Skill 只读探测</div>
              <div>
                <label className="block text-slate-500 mb-1">私有 Profile 覆盖目录（例：E:/github/ai-home/flywheel-profile）</label>
                <input
                  value={overrideDir}
                  onChange={(e) => setOverrideDir(e.target.value)}
                  placeholder="留空则使用默认脱敏 profile/"
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 bg-white font-mono"
                />
              </div>
              <div>
                <label className="block text-slate-500 mb-1">本地 Skills 探测目录（每行一个，仅只读扫描 SKILL.md）</label>
                <textarea
                  rows={2}
                  value={skillsDirs}
                  onChange={(e) => setSkillsDirs(e.target.value)}
                  placeholder="例如：E:/github/ai-home/skills"
                  className="w-full p-2.5 rounded-lg border border-slate-200 bg-white font-mono"
                />
              </div>
            </div>

            <div className="p-4 rounded-xl bg-slate-50 border border-slate-200/80 space-y-3">
              <div className="font-semibold text-slate-900">2. 知识库落点适配器（与策略解耦）</div>
              <div>
                <label className="block text-slate-500 mb-1">落点模式</label>
                <select
                  value={kbType}
                  onChange={(e) => setKbType(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 bg-white"
                >
                  <option value="builtin_kb">内置默认 Markdown 知识库（零配置开箱即用）</option>
                  <option value="local_markdown_wiki">挂载外部本地 Markdown / Obsidian / Wiki 目录</option>
                </select>
              </div>
              <div>
                <label className="block text-slate-500 mb-1">知识库根目录路径</label>
                <input
                  value={kbRoot}
                  onChange={(e) => setKbRoot(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 bg-white font-mono"
                />
              </div>
            </div>

            <div className="p-4 rounded-xl bg-slate-50 border border-slate-200/80 space-y-3">
              <div className="font-semibold text-slate-900">3. 后台执行大脑（本地 CLI 子进程 / Dry-run）</div>
              <div>
                <label className="block text-slate-500 mb-1">执行模式</label>
                <select
                  value={runnerMode}
                  onChange={(e) => setRunnerMode(e.target.value)}
                  className="w-full px-3 py-2 rounded-lg border border-slate-200 bg-white"
                >
                  <option value="dry_run">Dry-run 零成本本地启发式模式（无需安装外部 CLI）</option>
                  <option value="cli">可配置本地 CLI 子进程模式（复用本机订阅额度）</option>
                </select>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <div>
                  <label className="block text-slate-500 mb-1">CLI 可执行命令/路径</label>
                  <input
                    value={cliCmd}
                    onChange={(e) => setCliCmd(e.target.value)}
                    placeholder="如 claude / codex"
                    className="w-full px-3 py-2 rounded-lg border border-slate-200 bg-white font-mono"
                  />
                </div>
                <div>
                  <label className="block text-slate-500 mb-1">参数模板（支持 {'{prompt}'}）</label>
                  <input
                    value={cliArgs}
                    onChange={(e) => setCliArgs(e.target.value)}
                    placeholder="-p {prompt}"
                    className="w-full px-3 py-2 rounded-lg border border-slate-200 bg-white font-mono"
                  />
                </div>
              </div>
            </div>

            <div className="p-4 rounded-xl bg-slate-50 border border-slate-200/80 space-y-3">
              <div className="font-semibold text-slate-900">4. 显式安全阀（Safety Valves）</div>
              <label className="flex items-center justify-between p-2.5 rounded-lg bg-white border border-slate-200 cursor-pointer">
                <div>
                  <div className="font-medium text-slate-800">允许执行后台工作流（WORKFLOW_ENABLED）</div>
                  <div className="text-[11px] text-slate-500">关闭时阻断所有 P00–P07 触发</div>
                </div>
                <input
                  type="checkbox"
                  checked={workflowEnabled}
                  onChange={(e) => setWorkflowEnabled(e.target.checked)}
                />
              </label>
              <label className="flex items-center justify-between p-2.5 rounded-lg bg-white border border-slate-200 cursor-pointer">
                <div>
                  <div className="font-medium text-slate-800">无人值守全自动写入（AUTO_APPROVE_WRITES）</div>
                  <div className="text-[11px] text-slate-500">默认关闭（P03 后审核放行）；开启后直通 P04–P07</div>
                </div>
                <input
                  type="checkbox"
                  checked={autoApprove}
                  onChange={(e) => setAutoApprove(e.target.checked)}
                />
              </label>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
