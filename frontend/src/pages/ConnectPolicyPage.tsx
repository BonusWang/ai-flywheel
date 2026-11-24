import React, { useEffect, useState } from 'react'
import { Save, Zap } from 'lucide-react'
import { api } from '../api'
import { CodeBlock, CopyButton } from '../components/CodeBlock'
import { PillTabs } from '../components/PageLayout'
import type { ConnectPayload, PreflightResponse } from '../types'

interface Props {
  initialTab?: 'connect' | 'storage' | 'test'
  onGlobalRefresh: () => void
}

export const ConnectPolicyPage: React.FC<Props> = ({
  initialTab = 'connect',
  onGlobalRefresh,
}) => {
  const [tab, setTab] = useState<'connect' | 'storage' | 'test'>(initialTab)
  const [connectData, setConnectData] = useState<ConnectPayload | null>(null)
  const [selectedClient, setSelectedClient] = useState('codex')
  const [notice, setNotice] = useState<string | null>(null)
  // Settings that only take effect after the service restarts.
  const [restartNotice, setRestartNotice] = useState<string[]>([])

  // Test sandbox
  const [pfPrompt, setPfPrompt] = useState('修复接口报错并做真实验证')
  const [pfProject, setPfProject] = useState('generic')
  const [pfResult, setPfResult] = useState<PreflightResponse | null>(null)
  const [pfLoading, setPfLoading] = useState(false)

  // Storage & model config
  const [overrideDir, setOverrideDir] = useState('')
  const [skillsDirs, setSkillsDirs] = useState('')
  const [kbType, setKbType] = useState('builtin_kb')
  const [kbRoot, setKbRoot] = useState('./data/knowledge')
  const [runnerMode, setRunnerMode] = useState('dry_run')
  const [apiBaseUrl, setApiBaseUrl] = useState('')
  const [apiModel, setApiModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [apiKeyConfigured, setApiKeyConfigured] = useState(false)
  const [cliCmd, setCliCmd] = useState('')
  const [cliArgs, setCliArgs] = useState('')
  const [approvalMode, setApprovalMode] = useState('smart')
  const [fridayWeekly, setFridayWeekly] = useState(true)
  const [workflowEnabled, setWorkflowEnabled] = useState(true)
  const [skillWriteEnabled, setSkillWriteEnabled] = useState(true)
  const [scheduleEnabled, setScheduleEnabled] = useState(false)
  const [feishuEnabled, setFeishuEnabled] = useState(false)
  const [feishuUserId, setFeishuUserId] = useState('')
  const [feishuNotifyFyi, setFeishuNotifyFyi] = useState(false)

  useEffect(() => {
    setTab(initialTab)
  }, [initialTab])

  const loadAll = async () => {
    const cRes = await api.connectInfo()
    setConnectData(cRes)
    const cfg = cRes.config || {}
    setOverrideDir(cfg.profile?.override_dir || '')
    setSkillsDirs((cfg.capabilities?.skills_dirs || []).join('\n'))
    setKbType(cfg.knowledge_sink?.type || 'builtin_kb')
    setKbRoot(cfg.knowledge_sink?.root || './data/knowledge')
    setRunnerMode(cfg.runner?.mode || 'dry_run')
    setApiBaseUrl(cfg.runner?.api?.base_url || '')
    setApiModel(cfg.runner?.api?.model || '')
    setApiKey('')
    setApiKeyConfigured(Boolean(cfg.runner?.api?.api_key_configured))
    setCliCmd(cfg.runner?.cli?.command || '')
    setCliArgs((cfg.runner?.cli?.args || []).join(' '))
    // The effective mode, not the configured one: `auto` needs the safety
    // valve open, so a configured `auto` behind a closed valve really runs as
    // `smart`. The dropdown must say what will happen.
    const configuredMode = String(cfg.workflow?.approval_mode || 'always')
    const workflowAuto = Boolean(cfg.workflow?.auto_approve_writes)
    const valveAuto = Boolean(cfg.safety_valves?.auto_approve_writes)
    const resolvedMode =
      configuredMode === 'auto'
        ? valveAuto
          ? 'auto'
          : 'smart'
        : workflowAuto && valveAuto
          ? 'auto'
          : configuredMode
    setApprovalMode(resolvedMode)
    setFridayWeekly(cfg.workflow?.friday_weekly_enabled !== false)
    setWorkflowEnabled(cfg.safety_valves?.workflow_enabled !== false)
    setSkillWriteEnabled(cfg.safety_valves?.skill_write_enabled !== false)
    setScheduleEnabled(Boolean(cfg.workflow?.schedule_enabled))
    setFeishuEnabled(Boolean(cfg.delivery?.feishu_cli?.enabled))
    setFeishuUserId(cfg.delivery?.feishu_cli?.user_id || '')
    setFeishuNotifyFyi(Boolean(cfg.delivery?.feishu_cli?.notify_on_fyi))
  }

  useEffect(() => {
    loadAll().catch(() => {})
  }, [])

  const showToast = (msg: string) => {
    setNotice(msg)
    setTimeout(() => setNotice(null), 3000)
  }

  const executePreflight = async () => {
    if (!pfPrompt.trim() || pfLoading) return
    setPfLoading(true)
    try {
      const res = await api.preflight({
        task: pfPrompt.trim(),
        project: pfProject.trim() || 'generic',
        client: 'portal-sandbox',
      })
      setPfResult(res)
      onGlobalRefresh()
    } finally {
      setPfLoading(false)
    }
  }

  const saveAdapters = async () => {
    const apiPatch: Record<string, string> = {
      base_url: apiBaseUrl.trim(),
      model: apiModel.trim(),
    }
    if (apiKey.trim()) {
      apiPatch.api_key = apiKey.trim()
    }
    const saved = await api.updateConfig({
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
        api: apiPatch,
        cli: {
          command: cliCmd.trim(),
          args: cliArgs.trim() ? cliArgs.trim().split(/\s+/) : [],
        },
      },
      workflow: {
        schedule_enabled: scheduleEnabled,
        friday_weekly_enabled: fridayWeekly,
        approval_mode: approvalMode,
        auto_approve_writes: approvalMode === 'auto',
      },
      delivery: {
        feishu_cli: {
          enabled: feishuEnabled,
          user_id: feishuUserId.trim(),
          notify_on_fyi: feishuNotifyFyi,
        },
      },
      safety_valves: {
        workflow_enabled: workflowEnabled,
        skill_write_enabled: skillWriteEnabled,
        auto_approve_writes: approvalMode === 'auto',
      },
    })
    await loadAll()
    onGlobalRefresh()
    if (saved.restart_required) {
      setRestartNotice(saved.restart_reasons || [])
      showToast('设置已保存（部分需重启后生效）')
    } else {
      setRestartNotice([])
      showToast('设置已保存')
    }
  }

  const clientObj =
    connectData?.guides.clients.find((c) => c.id === selectedClient) ||
    connectData?.guides.clients[0]
  const mcpTools = connectData?.mcp_tools || []

  return (
    <div className="mx-auto max-w-[var(--page-max-wide)] space-y-5">
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">接入与设置</h1>
          <p className="text-[13.5px] text-ink-3 mt-1">
            连接你的 AI 工具、指定笔记存放目录与后台整理模型，配好一次即可长期使用。
          </p>
        </div>
        <div className="flex items-center gap-2">
          {notice && (
            <span className="px-3 py-1 rounded-full bg-ok-soft text-ok-ink text-[12px] border border-ok/25">
              {notice}
            </span>
          )}
          {tab === 'storage' && (
            <button
              type="button"
              onClick={saveAdapters}
              className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink"
            >
              <Save className="w-3.5 h-3.5" />
              保存设置
            </button>
          )}
        </div>
      </header>

      <PillTabs
        items={[
          { key: 'connect', label: '连接 AI 工具' },
          { key: 'storage', label: '目录与模型' },
          { key: 'test', label: '模拟测试' },
        ]}
        active={tab}
        onChange={(k) => setTab(k as 'connect' | 'storage' | 'test')}
      />

      {restartNotice.length > 0 && (
        <div className="rounded-[var(--radius-card)] border border-amber/40 bg-amber-soft/55 px-4 py-3 text-[12.5px] text-ink-2">
          <div className="font-semibold text-ink">
            设置已保存，但以下几项要重启服务后才生效：
          </div>
          <ul className="mt-1.5 list-disc pl-5 space-y-0.5 text-ink-3">
            {restartNotice.map((reason) => (
              <li key={reason} className="mono text-[11.5px]">
                {reason}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Tab 1: Connect AI Clients */}
      {tab === 'connect' && connectData && (
        <div className="max-w-3xl space-y-5">
          <div className="card p-4 flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[12px] text-ink-4">本地服务地址（MCP）</div>
              <code className="mono text-[13.5px] font-medium text-ink">
                {connectData.guides.endpoints.mcp_http_url}
              </code>
            </div>
            <CopyButton text={connectData.guides.endpoints.mcp_http_url} label="复制地址" />
          </div>

          <PillTabs
            items={connectData.guides.clients.map((c) => ({ key: c.id, label: c.name }))}
            active={selectedClient}
            onChange={setSelectedClient}
          />

          {clientObj && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-[16px] font-bold text-ink">{clientObj.name} 配置方法</h2>
                <span className="chip">{clientObj.loop_guarantee}</span>
              </div>
              <p className="text-[13px] text-ink-3">{clientObj.notes}</p>
              <CodeBlock title={`第 1 步 · ${clientObj.mcp_label}`} code={clientObj.mcp_config} />
              <CodeBlock title={`第 2 步 · ${clientObj.hook_label}`} code={clientObj.hook_config} />
            </div>
          )}

          {mcpTools.length > 0 && (
            <section className="card p-5">
              <h3 className="text-[14.5px] font-bold text-ink mb-3">
                连上后，AI 工具会多出这 {mcpTools.length} 个能力
              </h3>
              <ul className="space-y-2 text-[13px] leading-[1.7] text-ink-2">
                {mcpTools.map((tool) => (
                  <li key={tool.name} className="flex gap-2">
                    <span className="mt-[9px] size-1 shrink-0 rounded-full bg-accent" />
                    <span>
                      <strong className="font-semibold text-ink">{tool.title_zh}</strong>（
                      <code className="mono text-[11.5px] text-ink-3">{tool.name}</code>
                      ）：{tool.description_zh || tool.description}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}

      {/* Tab 2: Directories, Model & Schedule */}
      {tab === 'storage' && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 text-[12.5px]">
          <section className="card p-5 space-y-3">
            <h2 className="text-[14.5px] font-bold text-ink">1. 个人配置与技能目录</h2>
            <div>
              <label className="block text-ink-3 mb-1">个人规矩覆盖目录（选填）</label>
              <input
                value={overrideDir}
                onChange={(e) => setOverrideDir(e.target.value)}
                placeholder="例如：/path/to/your-home-repo/flywheel-profile"
                className="field-input mono w-full px-3 py-2 text-[12px]"
              />
            </div>
            <div>
              <label className="block text-ink-3 mb-1">本地技能目录（每行一个，只读识别）</label>
              <textarea
                rows={2}
                value={skillsDirs}
                onChange={(e) => setSkillsDirs(e.target.value)}
                placeholder="例如：/path/to/your-home-repo/skills"
                className="field-input mono w-full p-2.5 text-[12px]"
              />
            </div>
          </section>

          <section className="card p-5 space-y-3">
            <h2 className="text-[14.5px] font-bold text-ink">2. 笔记保存位置</h2>
            <div>
              <label className="block text-ink-3 mb-1">保存方式</label>
              <select
                value={kbType}
                onChange={(e) => setKbType(e.target.value)}
                className="field-input w-full px-3 py-2 text-[12.5px]"
              >
                <option value="builtin_kb">使用自带的笔记文件夹（开箱即用）</option>
                <option value="local_markdown_wiki">保存到我自己的本地知识库目录</option>
              </select>
            </div>
            <div>
              <label className="block text-ink-3 mb-1">笔记根目录</label>
              <input
                value={kbRoot}
                onChange={(e) => setKbRoot(e.target.value)}
                className="field-input mono w-full px-3 py-2 text-[12px]"
              />
            </div>
          </section>

          <section className="card p-5 space-y-3">
            <h2 className="text-[14.5px] font-bold text-ink">3. 后台整理使用的模型</h2>
            <div>
              <label className="block text-ink-3 mb-1">运行方式</label>
              <select
                value={runnerMode}
                onChange={(e) => setRunnerMode(e.target.value)}
                className="field-input w-full px-3 py-2 text-[12.5px]"
              >
                <option value="api">调用模型 API（推荐：接本地网关或云端模型）</option>
                <option value="cli">调用本机命令行工具</option>
                <option value="dry_run">不调模型，仅做本地快速演练</option>
              </select>
            </div>
            {runnerMode === 'api' && (
              <div className="space-y-2.5">
                <div>
                  <label className="block text-ink-3 mb-1">接口地址（Base URL）</label>
                  <input
                    value={apiBaseUrl}
                    onChange={(e) => setApiBaseUrl(e.target.value)}
                    placeholder="http://127.0.0.1:3425/v1"
                    className="field-input mono w-full px-3 py-2 text-[12px]"
                  />
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                  <div>
                    <label className="block text-ink-3 mb-1">模型名或路由组</label>
                    <input
                      value={apiModel}
                      onChange={(e) => setApiModel(e.target.value)}
                      placeholder="group/cheap-first"
                      className="field-input mono w-full px-3 py-2 text-[12px]"
                    />
                  </div>
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <label className="text-ink-3">API 密钥</label>
                      {apiKeyConfigured && (
                        <span className="text-[11px] text-ok-ink font-medium">
                          已配置 · 留空不变
                        </span>
                      )}
                    </div>
                    <input
                      type="password"
                      value={apiKey}
                      onChange={(e) => setApiKey(e.target.value)}
                      placeholder={apiKeyConfigured ? '留空即保留当前密钥' : '输入 API Key'}
                      className="field-input mono w-full px-3 py-2 text-[12px]"
                    />
                  </div>
                </div>
              </div>
            )}
            {runnerMode === 'cli' && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
                <div>
                  <label className="block text-ink-3 mb-1">命令名</label>
                  <input
                    value={cliCmd}
                    onChange={(e) => setCliCmd(e.target.value)}
                    placeholder="如 claude / codex"
                    className="field-input mono w-full px-3 py-2 text-[12px]"
                  />
                </div>
                <div>
                  <label className="block text-ink-3 mb-1">参数模板</label>
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
            <h2 className="text-[14.5px] font-bold text-ink">4. 定时、审批与飞书通知</h2>
            <div>
              <label className="block text-ink-3 mb-1">审批方式</label>
              <select
                value={approvalMode}
                onChange={(e) => setApprovalMode(e.target.value)}
                className="field-input w-full px-3 py-2 text-[12.5px]"
              >
                <option value="smart">智能审批（推荐：常规笔记与规则自动通过，仅新建/修改 Skill 或重要变更需确认）</option>
                <option value="always">每次写入都要我手动确认</option>
                <option value="auto">全部自动通过并落盘</option>
              </select>
            </div>
            <label className="flex items-center justify-between gap-3 p-2.5 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft cursor-pointer">
              <div>
                <div className="font-medium text-ink">每天 20:00 自动整理（周五附带周复盘）</div>
                <div className="text-[11.5px] text-ink-4">周一至周四每日沉淀，周五自动叠加本周重复流程与技能复盘</div>
              </div>
              <input
                type="checkbox"
                checked={scheduleEnabled && fridayWeekly}
                onChange={(e) => {
                  setScheduleEnabled(e.target.checked)
                  setFridayWeekly(e.target.checked)
                }}
                className="accent-accent"
              />
            </label>
            <div className="p-2.5 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft space-y-2">
              <label className="flex items-center justify-between gap-3 cursor-pointer">
                <div>
                  <div className="font-medium text-ink">重要待审批通过飞书 CLI 通知我</div>
                  <div className="text-[11.5px] text-ink-4">平时自动通过不打扰，有 Skill 变更或报错才发飞书消息</div>
                </div>
                <input
                  type="checkbox"
                  checked={feishuEnabled}
                  onChange={(e) => setFeishuEnabled(e.target.checked)}
                  className="accent-accent"
                />
              </label>
              {feishuEnabled && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 pt-1">
                  <input
                    value={feishuUserId}
                    onChange={(e) => setFeishuUserId(e.target.value)}
                    placeholder="飞书接收人 open_id（ou_xxx）"
                    className="field-input mono px-2.5 py-1.5 text-[12px]"
                  />
                  <label className="inline-flex items-center gap-2 text-[12px] text-ink-3 cursor-pointer px-1">
                    <input
                      type="checkbox"
                      checked={feishuNotifyFyi}
                      onChange={(e) => setFeishuNotifyFyi(e.target.checked)}
                      className="accent-accent"
                    />
                    <span>常规自动通过时也发简报</span>
                  </label>
                </div>
              )}
            </div>
            <div className="flex flex-wrap gap-4 pt-1 text-[12px] text-ink-2">
              <label className="inline-flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={workflowEnabled}
                  onChange={(e) => setWorkflowEnabled(e.target.checked)}
                  className="accent-accent"
                />
                <span>允许后台沉淀运行</span>
              </label>
              <label className="inline-flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={skillWriteEnabled}
                  onChange={(e) => setSkillWriteEnabled(e.target.checked)}
                  className="accent-accent"
                />
                <span>允许批准后写入 Skill 目录</span>
              </label>
            </div>
          </section>
        </div>
      )}

      {/* Tab 3: Self-Test Sandbox */}
      {tab === 'test' && (
        <div className="max-w-2xl card p-5 space-y-4">
          <div className="flex items-center gap-2">
            <Zap className="w-4 h-4 text-accent" />
            <h2 className="text-[15px] font-bold text-ink">模拟一次开局提醒</h2>
          </div>
          <p className="text-[13px] text-ink-3">
            输入一句你准备让 AI 做的任务，看看系统会自动给 AI 注入哪些规矩和避坑规则。
          </p>
          <div className="space-y-2.5">
            <input
              value={pfProject}
              onChange={(e) => setPfProject(e.target.value)}
              placeholder="项目名（选填）"
              className="field-input w-full px-3 py-2 text-[13px]"
            />
            <textarea
              rows={3}
              value={pfPrompt}
              onChange={(e) => setPfPrompt(e.target.value)}
              placeholder="输入你想测试的任务描述..."
              className="field-input w-full px-3 py-2 text-[13px]"
            />
            <button
              type="button"
              onClick={executePreflight}
              disabled={pfLoading}
              className="inline-flex items-center gap-1.5 h-9 px-4 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[13px] font-medium hover:bg-accent-ink disabled:opacity-50"
            >
              <Zap className="w-3.5 h-3.5" />
              {pfLoading ? '正在测试...' : '运行模拟测试'}
            </button>
          </div>
          {pfResult && (
            <div className="pt-3 border-t border-line-soft space-y-2">
              <div className="text-[12px] text-ink-4">
                匹配模式：<strong className="text-ink">{pfResult.anti_laziness_mode}</strong> · 命中规则{' '}
                <strong className="num text-ink">{pfResult.matched_rules.length}</strong> 条
              </div>
              <pre className="mono text-[12px] leading-[1.65] bg-bg-sunk border border-line-soft rounded-[var(--radius-control)] p-3.5 whitespace-pre-wrap max-h-80 overflow-y-auto text-ink-2">
                {pfResult.compact_context}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
