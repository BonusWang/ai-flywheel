import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { CheckCircle2, ChevronRight, Loader2, RefreshCw, Save, Shield } from 'lucide-react'
import type { NavigateFn, RouteIntent } from '../App'
import { api } from '../api'
import { ConnectClientsTab } from '../components/connect/ConnectClientsTab'
import { ConnectSandboxTab } from '../components/connect/ConnectSandboxTab'
import { PillTabs } from '../components/PageLayout'
import { Collapse } from '../components/ui/Collapse'
import { Switch } from '../components/ui/Switch'
import { useToast } from '../components/ui/Toast'
import { errorMessage } from '../lib/errors'
import { FEISHU_LISTENER_STATE_LABELS, labelOf } from '../lib/labels'
import type { ConnectPayload, FeishuCheckStatus } from '../types'

interface Props {
  intent?: RouteIntent
  onNavigate?: NavigateFn
  onGlobalRefresh: () => void
}

interface StorageDraft {
  overrideDir: string
  skillsDirs: string
  kbType: string
  kbRoot: string
  runnerMode: string
  apiBaseUrl: string
  apiModel: string
  cliCmd: string
  cliArgs: string
  approvalMode: string
  workflowEnabled: boolean
  skillWriteEnabled: boolean
  feishuEnabled: boolean
  feishuUserId: string
  feishuNotifyFyi: boolean
  feishuInteractive: boolean
  feishuApproverIds: string
}

export const ConnectPolicyPage: React.FC<Props> = ({
  intent,
  onNavigate,
  onGlobalRefresh,
}) => {
  const toast = useToast()
  const [tab, setTab] = useState<'connect' | 'storage' | 'test'>(intent?.settingsTab || 'connect')
  const [connectData, setConnectData] = useState<ConnectPayload | null>(null)
  const [restartNotice, setRestartNotice] = useState<string[]>([])
  const [saving, setSaving] = useState(false)

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
  const [workflowEnabled, setWorkflowEnabled] = useState(true)
  const [skillWriteEnabled, setSkillWriteEnabled] = useState(true)
  const [scheduleEnabled, setScheduleEnabled] = useState(false)
  const [dailyCron, setDailyCron] = useState('0 20 * * *')
  const [feishuEnabled, setFeishuEnabled] = useState(false)
  const [feishuUserId, setFeishuUserId] = useState('')
  const [feishuNotifyFyi, setFeishuNotifyFyi] = useState(false)
  const [feishuInteractive, setFeishuInteractive] = useState(false)
  const [feishuApproverIds, setFeishuApproverIds] = useState('')
  const [feishuCheck, setFeishuCheck] = useState<FeishuCheckStatus | null>(null)
  const [feishuCheckBusy, setFeishuCheckBusy] = useState(false)
  const [feishuCheckError, setFeishuCheckError] = useState<string | null>(null)

  const [baseline, setBaseline] = useState<StorageDraft | null>(null)

  useEffect(() => {
    if (!intent?.nonce || !intent.settingsTab) return
    setTab(intent.settingsTab)
  }, [intent?.nonce, intent?.settingsTab])

  const applyDraft = useCallback((d: StorageDraft) => {
    setBaseline(d)
    setOverrideDir(d.overrideDir)
    setSkillsDirs(d.skillsDirs)
    setKbType(d.kbType)
    setKbRoot(d.kbRoot)
    setRunnerMode(d.runnerMode)
    setApiBaseUrl(d.apiBaseUrl)
    setApiModel(d.apiModel)
    setApiKey('')
    setCliCmd(d.cliCmd)
    setCliArgs(d.cliArgs)
    setApprovalMode(d.approvalMode)
    setWorkflowEnabled(d.workflowEnabled)
    setSkillWriteEnabled(d.skillWriteEnabled)
    setFeishuEnabled(d.feishuEnabled)
    setFeishuUserId(d.feishuUserId)
    setFeishuNotifyFyi(d.feishuNotifyFyi)
    setFeishuInteractive(d.feishuInteractive)
    setFeishuApproverIds(d.feishuApproverIds)
  }, [])

  const loadAll = useCallback(async () => {
    const cRes = await api.connectInfo()
    setConnectData(cRes)
    const cfg = cRes.config || {}
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

    setApiKeyConfigured(Boolean(cfg.runner?.api?.api_key_configured))
    setScheduleEnabled(Boolean(cfg.workflow?.schedule_enabled))
    setDailyCron(String(cfg.workflow?.daily_cron || '0 20 * * *'))

    applyDraft({
      overrideDir: cfg.profile?.override_dir || '',
      skillsDirs: (cfg.capabilities?.skills_dirs || []).join('\n'),
      kbType: cfg.knowledge_sink?.type || 'builtin_kb',
      kbRoot: cfg.knowledge_sink?.root || './data/knowledge',
      runnerMode: cfg.runner?.mode || 'dry_run',
      apiBaseUrl: cfg.runner?.api?.base_url || '',
      apiModel: cfg.runner?.api?.model || '',
      cliCmd: cfg.runner?.cli?.command || '',
      cliArgs: (cfg.runner?.cli?.args || []).join(' '),
      approvalMode: resolvedMode,
      workflowEnabled: cfg.safety_valves?.workflow_enabled !== false,
      skillWriteEnabled: cfg.safety_valves?.skill_write_enabled !== false,
      feishuEnabled: Boolean(cfg.delivery?.feishu_cli?.enabled),
      feishuUserId: cfg.delivery?.feishu_cli?.user_id || '',
      feishuNotifyFyi: Boolean(cfg.delivery?.feishu_cli?.notify_on_fyi),
      feishuInteractive: Boolean(cfg.delivery?.feishu_cli?.interactive),
      feishuApproverIds: (cfg.delivery?.feishu_cli?.approver_open_ids || []).join(', '),
    })

    api
      .feishuCheckStatus()
      .then((st) => setFeishuCheck(st))
      .catch(() => setFeishuCheck(null))
  }, [applyDraft])

  useEffect(() => {
    loadAll().catch(() => {})
  }, [loadAll])

  useEffect(() => {
    if (!feishuCheck?.running) return
    const timer = window.setInterval(() => {
      api
        .feishuCheckStatus()
        .then((st) => {
          setFeishuCheck(st)
          if (!st.running) onGlobalRefresh()
        })
        .catch(() => {})
    }, 2000)
    return () => window.clearInterval(timer)
  }, [feishuCheck?.running, onGlobalRefresh])

  const isDirty = useMemo(() => {
    if (!baseline) return false
    if (apiKey.trim()) return true
    return (
      overrideDir !== baseline.overrideDir ||
      skillsDirs !== baseline.skillsDirs ||
      kbType !== baseline.kbType ||
      kbRoot !== baseline.kbRoot ||
      runnerMode !== baseline.runnerMode ||
      apiBaseUrl !== baseline.apiBaseUrl ||
      apiModel !== baseline.apiModel ||
      cliCmd !== baseline.cliCmd ||
      cliArgs !== baseline.cliArgs ||
      approvalMode !== baseline.approvalMode ||
      workflowEnabled !== baseline.workflowEnabled ||
      skillWriteEnabled !== baseline.skillWriteEnabled ||
      feishuEnabled !== baseline.feishuEnabled ||
      feishuUserId !== baseline.feishuUserId ||
      feishuNotifyFyi !== baseline.feishuNotifyFyi ||
      feishuInteractive !== baseline.feishuInteractive ||
      feishuApproverIds !== baseline.feishuApproverIds
    )
  }, [
    baseline,
    overrideDir,
    skillsDirs,
    kbType,
    kbRoot,
    runnerMode,
    apiBaseUrl,
    apiModel,
    apiKey,
    cliCmd,
    cliArgs,
    approvalMode,
    workflowEnabled,
    skillWriteEnabled,
    feishuEnabled,
    feishuUserId,
    feishuNotifyFyi,
    feishuInteractive,
    feishuApproverIds,
  ])

  const saveAdapters = useCallback(async () => {
    if (saving || !isDirty) return
    setSaving(true)
    try {
      const apiPatch: Record<string, string> = {
        base_url: apiBaseUrl.trim(),
        model: apiModel.trim(),
      }
      if (apiKey.trim()) apiPatch.api_key = apiKey.trim()
      const saved = await api.updateConfig({
        profile: { override_dir: overrideDir.trim() || null },
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
          approval_mode: approvalMode,
          auto_approve_writes: approvalMode === 'auto',
        },
        delivery: {
          feishu_cli: {
            enabled: feishuEnabled,
            user_id: feishuUserId.trim(),
            notify_on_fyi: feishuNotifyFyi,
            interactive: feishuInteractive,
            approver_open_ids: feishuApproverIds
              .split(/[\s,，]+/)
              .map((s) => s.trim())
              .filter(Boolean),
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
        toast.push({ tone: 'warn', message: '设置已保存，部分项需重启服务后生效' })
      } else {
        setRestartNotice([])
        toast.push('设置已保存并立即生效')
      }
    } catch (e: unknown) {
      toast.push({ tone: 'error', message: errorMessage(e, '保存设置失败') })
    } finally {
      setSaving(false)
    }
  }, [
    saving,
    isDirty,
    apiBaseUrl,
    apiModel,
    apiKey,
    overrideDir,
    skillsDirs,
    kbType,
    kbRoot,
    runnerMode,
    cliCmd,
    cliArgs,
    approvalMode,
    feishuEnabled,
    feishuUserId,
    feishuNotifyFyi,
    feishuInteractive,
    feishuApproverIds,
    workflowEnabled,
    skillWriteEnabled,
    loadAll,
    onGlobalRefresh,
    toast,
  ])

  const runFeishuCheck = async () => {
    if (feishuCheckBusy || feishuCheck?.running) return
    setFeishuCheckError(null)
    setFeishuCheckBusy(true)
    try {
      await api.updateConfig({
        delivery: {
          feishu_cli: {
            enabled: feishuEnabled,
            user_id: feishuUserId.trim(),
            notify_on_fyi: feishuNotifyFyi,
            interactive: feishuInteractive,
            approver_open_ids: feishuApproverIds
              .split(/[\s,，]+/)
              .map((s) => s.trim())
              .filter(Boolean),
          },
        },
      })
      setBaseline((prev) =>
        prev
          ? {
              ...prev,
              feishuEnabled,
              feishuUserId: feishuUserId.trim(),
              feishuNotifyFyi,
              feishuInteractive,
              feishuApproverIds,
            }
          : prev
      )
      await api.startFeishuCheck(120)
      const st = await api.feishuCheckStatus()
      setFeishuCheck({ ...st, running: true })
      toast.push({ tone: 'info', message: '已发出飞书测试卡片，请在飞书点击「我收到了」' })
    } catch (err: unknown) {
      setFeishuCheckError(errorMessage(err, '飞书链路自检失败'))
    } finally {
      setFeishuCheckBusy(false)
    }
  }

  return (
    <div
      onKeyDown={(e) => {
        if (tab === 'storage' && (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
          e.preventDefault()
          saveAdapters()
        }
      }}
      className="mx-auto max-w-[var(--page-max-wide)] space-y-5"
    >
      <header className="flex flex-wrap items-end justify-between gap-3 pb-4 border-b border-line">
        <div>
          <h1 className="text-[24px] font-semibold leading-[1.3] text-ink">接入与设置</h1>
          <p className="text-[13.5px] text-ink-3 mt-1">
            连接你的 AI 工具、指定笔记目录与后台整理模型，配好一次即可长期运行。
          </p>
        </div>
        {tab === 'storage' && (
          <div className="flex items-center gap-2">
            {isDirty && (
              <button
                type="button"
                disabled={saving}
                onClick={() => {
                  if (baseline) applyDraft(baseline)
                  toast.push({ tone: 'info', message: '已还原未保存的设置' })
                }}
                className="pressable h-8 px-3 rounded-[var(--radius-control)] border border-line bg-surface text-[12.5px] text-ink-3 hover:text-ink"
              >
                放弃修改
              </button>
            )}
            <button
              type="button"
              disabled={!isDirty || saving}
              onClick={saveAdapters}
              className="pressable inline-flex items-center gap-1.5 h-8 px-3.5 rounded-[var(--radius-control)] bg-accent text-accent-contrast text-[12.5px] font-medium hover:bg-accent-ink disabled:opacity-45"
            >
              {saving ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Save className="w-3.5 h-3.5" />
              )}
              <span>{saving ? '保存中…' : isDirty ? '保存设置' : '已同步'}</span>
            </button>
          </div>
        )}
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

      <Collapse open={restartNotice.length > 0}>
        <div className="rounded-[var(--radius-card)] border border-amber/40 bg-amber-soft/55 px-4 py-3 text-[12.5px] text-ink-2">
          <div className="font-semibold text-ink">
            设置已保存，以下几项需重启后台服务后生效：
          </div>
          <ul className="mt-1.5 list-disc pl-5 space-y-0.5 text-ink-3">
            {restartNotice.map((reason) => (
              <li key={reason} className="mono text-[11.5px]">
                {reason}
              </li>
            ))}
          </ul>
        </div>
      </Collapse>

      {/* Tab 1: Connect AI Clients (kept mounted so selectedClient persists across tab switches) */}
      <div hidden={tab !== 'connect'} className={tab === 'connect' ? undefined : 'hidden'}>
        <ConnectClientsTab connectData={connectData} />
      </div>

      {/* Tab 2: Safety Valves, Directories, Model & Channels */}
      {tab === 'storage' && (
        <div key="tab-storage" className="animate-tab-enter space-y-4 text-[12.5px]">
          <section className="card p-5 space-y-3.5 border-accent/25">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Shield className="w-4 h-4 text-accent" />
                <h2 className="text-[14.5px] font-bold text-ink">1. 运行护栏与审批方式</h2>
              </div>
              <span className="chip">写入前护栏优先于自动模式</span>
            </div>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div>
                <label className="block text-ink-3 mb-1">写入审批方式</label>
                <select
                  value={approvalMode}
                  onChange={(e) => setApprovalMode(e.target.value)}
                  className="field-input w-full px-3 py-2 text-[12.5px]"
                >
                  <option value="smart">
                    智能审批（推荐：常规笔记与规则自动通过，仅 Skill 或高风险变更需确认）
                  </option>
                  <option value="always">每次写入都要我手动确认</option>
                  <option value="auto">全部自动通过并落盘</option>
                </select>
              </div>
              <div className="flex flex-col justify-center gap-3 pt-1 text-[12.5px] text-ink-2">
                <div className="flex items-center justify-between gap-3">
                  <span>开启后台自动整理</span>
                  <Switch
                    size="sm"
                    checked={workflowEnabled}
                    onChange={setWorkflowEnabled}
                    ariaLabel="开启后台自动整理"
                  />
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span>允许审批后追加写入本地 Skill 文件</span>
                  <Switch
                    size="sm"
                    checked={skillWriteEnabled}
                    onChange={setSkillWriteEnabled}
                    ariaLabel="允许追加写入本地 Skill 文件"
                  />
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft px-3 py-2 text-[12px]">
              <span className="text-ink-3">
                后台定时调度：
                <strong className="text-ink font-medium ml-1">
                  {scheduleEnabled ? `已开启（${dailyCron}）` : '已暂停'}
                </strong>
              </span>
              {onNavigate && (
                <button
                  type="button"
                  onClick={() => onNavigate('automations')}
                  className="pressable inline-flex items-center gap-0.5 text-accent font-semibold hover:text-accent-ink"
                >
                  前往「自动化调度」调整
                  <ChevronRight className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          </section>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <section className="card p-5 space-y-3">
              <h2 className="text-[14.5px] font-bold text-ink">2. 个人策略与技能目录</h2>
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
              <h2 className="text-[14.5px] font-bold text-ink">3. 知识笔记目录</h2>
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
              <h2 className="text-[14.5px] font-bold text-ink">4. 后台整理模型</h2>
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
              <Collapse open={runnerMode === 'api'}>
                <div className="space-y-2.5 pt-0.5">
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
              </Collapse>
              <Collapse open={runnerMode === 'cli'}>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 pt-0.5">
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
              </Collapse>
            </section>

            <section className="card p-5 space-y-3">
              <h2 className="text-[14.5px] font-bold text-ink">5. 外部审批与通知（可选）</h2>
              <div className="p-3 rounded-[var(--radius-control)] bg-bg-sunk/70 border border-line-soft space-y-2.5">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <div className="font-medium text-ink">重要待审批通过飞书通知我</div>
                    <div className="text-[11.5px] text-ink-4">
                      平时自动通过不打扰，有 Skill 变更或报错才发飞书消息
                    </div>
                  </div>
                  <Switch
                    size="sm"
                    checked={feishuEnabled}
                    onChange={setFeishuEnabled}
                    ariaLabel="开启飞书通知"
                  />
                </div>
                <Collapse open={feishuEnabled}>
                  <div className="space-y-2.5 pt-2 border-t border-line-soft/70">
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 items-center">
                      <input
                        value={feishuUserId}
                        onChange={(e) => setFeishuUserId(e.target.value)}
                        placeholder="飞书接收人 open_id（ou_xxx）"
                        className="field-input mono px-2.5 py-1.5 text-[12px]"
                      />
                      <div className="flex items-center justify-between gap-2 text-[12px] text-ink-3 px-1">
                        <span>常规自动通过时也发简报</span>
                        <Switch
                          size="sm"
                          checked={feishuNotifyFyi}
                          onChange={setFeishuNotifyFyi}
                          ariaLabel="常规通过也发简报"
                        />
                      </div>
                    </div>
                    <div className="flex items-start justify-between gap-3 pt-2 border-t border-line-soft/70">
                      <div>
                        <div className="text-[12.5px] font-medium text-ink">
                          开启飞书交互审批卡片（在飞书内直接点击批准/放弃）
                        </div>
                        <div className="text-[11.5px] text-ink-4">
                          需要机器人身份与私聊 open_id，并通过下方链路自检
                        </div>
                      </div>
                      <Switch
                        size="sm"
                        checked={feishuInteractive}
                        onChange={setFeishuInteractive}
                        ariaLabel="开启飞书交互卡片"
                      />
                    </div>
                    <Collapse open={feishuInteractive}>
                      <div className="space-y-2 pt-1">
                        <input
                          value={feishuApproverIds}
                          onChange={(e) => setFeishuApproverIds(e.target.value)}
                          placeholder="额外审批人 open_id（选填，逗号分隔）"
                          className="field-input mono w-full px-2.5 py-1.5 text-[12px]"
                        />
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded-[var(--radius-control)] bg-surface border border-line-soft px-3 py-2 text-[12px]">
                          <div className="space-y-0.5">
                            <div className="flex items-center gap-1.5 text-ink-2">
                              {feishuCheck?.valid ? (
                                <CheckCircle2 className="w-3.5 h-3.5 text-ok-ink shrink-0" />
                              ) : (
                                <RefreshCw
                                  className={`w-3.5 h-3.5 text-ink-4 shrink-0 ${
                                    feishuCheck?.running ? 'animate-spin text-accent' : ''
                                  }`}
                                />
                              )}
                              <span>
                                {feishuCheck?.running
                                  ? '自检中：请到飞书点击测试卡上的「我收到了」'
                                  : feishuCheck?.valid
                                    ? '飞书交互审批链路已验证通过'
                                    : `监听状态：${labelOf(
                                        FEISHU_LISTENER_STATE_LABELS,
                                        feishuCheck?.listener?.state || 'stopped',
                                        '未知'
                                      )} · 待自检`}
                              </span>
                            </div>
                            {!feishuCheck?.running && !feishuCheck?.valid && feishuCheck?.reason && (
                              <div className="text-[11px] text-ink-4">
                                上次说明：{feishuCheck.reason}
                              </div>
                            )}
                            {feishuCheckError && (
                              <div className="text-[11px] text-hot">{feishuCheckError}</div>
                            )}
                          </div>
                          <button
                            type="button"
                            onClick={runFeishuCheck}
                            disabled={
                              feishuCheckBusy ||
                              Boolean(feishuCheck?.running) ||
                              !feishuUserId.trim()
                            }
                            className="pressable inline-flex items-center gap-1.5 h-7 px-2.5 rounded-[var(--radius-control)] border border-line bg-bg-sunk text-ink text-[12px] font-medium hover:border-accent hover:text-accent-ink disabled:opacity-50"
                          >
                            <RefreshCw
                              className={`w-3 h-3 ${
                                feishuCheckBusy || feishuCheck?.running ? 'animate-spin' : ''
                              }`}
                            />
                            <span>
                              {feishuCheck?.running
                                ? '等待飞书点击…'
                                : feishuCheck?.valid
                                  ? '重新自检'
                                  : '保存并自检'}
                            </span>
                          </button>
                        </div>
                      </div>
                    </Collapse>
                  </div>
                </Collapse>
              </div>
            </section>
          </div>
        </div>
      )}

      {/* Tab 3: Self-Test Sandbox (kept mounted so sandbox inputs & preview persist across tab switches) */}
      <div hidden={tab !== 'test'} className={tab === 'test' ? undefined : 'hidden'}>
        <ConnectSandboxTab onGlobalRefresh={onGlobalRefresh} />
      </div>
    </div>
  )
}
