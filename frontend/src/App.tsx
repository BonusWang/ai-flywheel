import React, { useCallback, useEffect, useState } from 'react'
import {
  GitBranch,
  Home,
  Lock,
  Plug,
  Radio,
  RefreshCw,
  Shield,
  ShieldCheck,
  Sliders,
} from 'lucide-react'
import { api } from './api'
import { ThemeSwitch } from './components/ThemeSwitch'
import { ConnectPolicyPage } from './pages/ConnectPolicyPage'
import { HomePage } from './pages/HomePage'
import { RulesRetroPage } from './pages/RulesRetroPage'
import { WorkflowsPage } from './pages/WorkflowsPage'
import type { OverviewResponse } from './types'

export type PageKey = 'home' | 'workflows' | 'rules' | 'connect' | 'policies' | 'adapters'

interface NavSection {
  title: string
  items: Array<{
    key: PageKey
    label: string
    icon: React.FC<{ className?: string }>
    dot?: boolean
  }>
}

function TodayLabel() {
  const now = new Date()
  const week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][now.getDay()]
  return (
    <span className="num text-[12.5px] text-ink-4">
      {now.getMonth() + 1}月{now.getDate()}日 · {week}
    </span>
  )
}

export function App() {
  const [page, setPage] = useState<PageKey>('home')
  const [overview, setOverview] = useState<OverviewResponse | null>(null)
  const [sseConnected, setSseConnected] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)

  const refreshAll = useCallback(() => {
    api
      .overview()
      .then((data) => setOverview(data))
      .catch(() => {})
    setRefreshToken((n) => n + 1)
  }, [])

  useEffect(() => {
    refreshAll()
    const es = new EventSource('/api/events')
    es.addEventListener('connected', () => setSseConnected(true))
    const onLive = () => refreshAll()
    es.addEventListener('run_started', onLive)
    es.addEventListener('node_update', onLive)
    es.addEventListener('awaiting_approval', onLive)
    es.addEventListener('run_completed', onLive)
    es.addEventListener('run_failed', onLive)
    es.addEventListener('run_rejected', onLive)
    es.onerror = () => setSseConnected(false)
    return () => es.close()
  }, [refreshAll])

  const brandName = overview?.site?.brand?.name || 'AI Flywheel'
  const hasApprovalWaiting = overview?.latest_run?.status === 'AWAITING_APPROVAL'
  const hasOpenRetro = (overview?.metrics?.open_retro_count || 0) > 0

  const sections: NavSection[] = [
    {
      title: '飞轮',
      items: [
        { key: 'home', label: '精选总览', icon: Home },
        { key: 'workflows', label: '工作流画布', icon: GitBranch, dot: hasApprovalWaiting },
        { key: 'rules', label: '规则与复盘', icon: Shield, dot: hasOpenRetro },
      ],
    },
    {
      title: '接入与治理',
      items: [
        { key: 'connect', label: 'Agent 接入', icon: Plug },
        { key: 'policies', label: '策略中心', icon: ShieldCheck },
        { key: 'adapters', label: '三体挂载', icon: Sliders },
      ],
    },
  ]

  const mobileTabs: Array<{ key: PageKey; label: string; icon: React.FC<{ className?: string }> }> = [
    { key: 'home', label: '总览', icon: Home },
    { key: 'workflows', label: '工作流', icon: GitBranch },
    { key: 'rules', label: '规则', icon: Shield },
    { key: 'connect', label: '接入', icon: Plug },
    { key: 'policies', label: '策略', icon: ShieldCheck },
  ]

  return (
    <div className="min-h-dvh flex bg-bg text-ink">
      {/* Desktop Sticky Left Sidebar (AIHOT 180px signature shell) */}
      <aside className="sticky top-0 hidden h-dvh w-[184px] shrink-0 flex-col border-r border-line bg-sidebar px-3 pb-3.5 pt-6 lg:flex">
        <button
          type="button"
          onClick={() => setPage('home')}
          className="mb-4 flex h-[46px] items-center gap-2 px-1.5 text-left text-ink"
        >
          <span className="flex size-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-accent text-accent-contrast">
            <RefreshCw className="w-3.5 h-3.5" />
          </span>
          <span className="min-w-0 truncate text-[15px] font-bold tracking-tight">
            {brandName}
          </span>
        </button>

        <nav className="-mx-1 flex-1 overflow-y-auto px-1" aria-label="主导航">
          {sections.map((section) => (
            <div key={section.title}>
              <div className="px-2.5 pb-1 pt-3.5 text-[11px] text-ink-4">{section.title}</div>
              <div className="flex flex-col gap-1">
                {section.items.map((item) => {
                  const isActive = page === item.key
                  const Icon = item.icon
                  return (
                    <button
                      key={item.key}
                      type="button"
                      onClick={() => setPage(item.key)}
                      aria-current={isActive ? 'page' : undefined}
                      className={`flex h-10 w-full items-center gap-2.5 rounded-[var(--radius-control)] px-2.5 text-[14px] transition-colors duration-150 ${
                        isActive
                          ? 'bg-accent/10 font-semibold text-ink dark:bg-accent-soft'
                          : 'font-medium text-ink-3 hover:bg-bg-sunk hover:text-ink'
                      }`}
                    >
                      <span
                        className={`flex w-[20px] shrink-0 justify-center ${
                          isActive ? 'text-accent' : ''
                        }`}
                      >
                        <Icon className="w-[17px] h-[17px]" />
                      </span>
                      <span className="min-w-0 truncate">{item.label}</span>
                      {item.dot && (
                        <span
                          className="ml-auto size-1.5 shrink-0 rounded-full bg-hot"
                          aria-label="有待处理事项"
                        />
                      )}
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className="mt-2 space-y-2.5 px-1 pt-2 border-t border-line-soft">
          <div className="flex items-center justify-between px-1.5 text-[11px] text-ink-4">
            <span className="inline-flex items-center gap-1.5">
              <Radio className={`w-3 h-3 ${sseConnected ? 'text-ok' : 'text-ink-4'}`} />
              <span className="mono">{sseConnected ? 'SSE Live' : 'Offline'}</span>
            </span>
            {overview?.workflow_lock && (
              <span className="inline-flex items-center gap-0.5 text-amber-ink" title="工作流独占锁持有中">
                <Lock className="w-3 h-3" />
              </span>
            )}
          </div>
          <ThemeSwitch className="mx-0.5" />
          <div className="flex items-center justify-between px-1.5 text-[10.5px] text-ink-4">
            <a href="/llms.txt" target="_blank" rel="noreferrer" className="hover:text-ink-2">
              llms.txt
            </a>
            <a href="/api/v1/overview" target="_blank" rel="noreferrer" className="hover:text-ink-2">
              API v1
            </a>
          </div>
        </div>
      </aside>

      {/* Main Content Column */}
      <div className="min-w-0 flex-1 flex flex-col">
        {/* Mobile Header */}
        <header className="flex h-14 items-center justify-between border-b border-line bg-sidebar px-4 lg:hidden">
          <button
            type="button"
            onClick={() => setPage('home')}
            className="flex items-center gap-2 text-ink"
          >
            <span className="flex size-6 items-center justify-center rounded-[var(--radius-mark)] bg-accent text-accent-contrast">
              <RefreshCw className="w-3.5 h-3.5" />
            </span>
            <span className="text-[15px] font-bold">{brandName}</span>
          </button>
          <TodayLabel />
        </header>

        <main className="flex-1 px-4 pb-20 pt-5 lg:px-10 lg:pb-10 lg:pt-8">
          {page === 'home' && (
            <HomePage overview={overview} onNavigate={setPage} onRefresh={refreshAll} />
          )}
          {page === 'workflows' && (
            <WorkflowsPage refreshToken={refreshToken} onGlobalRefresh={refreshAll} />
          )}
          {page === 'rules' && (
            <RulesRetroPage refreshToken={refreshToken} onGlobalRefresh={refreshAll} />
          )}
          {(page === 'connect' || page === 'policies' || page === 'adapters') && (
            <ConnectPolicyPage
              section={page}
              onChangeSection={setPage}
              onGlobalRefresh={refreshAll}
            />
          )}
        </main>
      </div>

      {/* Mobile Bottom Tab Bar */}
      <nav
        aria-label="移动端导航"
        className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-5 border-t border-line bg-sidebar/95 backdrop-blur py-1.5 lg:hidden"
      >
        {mobileTabs.map((t) => {
          const active = page === t.key
          const Icon = t.icon
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => setPage(t.key)}
              className={`flex flex-col items-center gap-0.5 py-1 text-[11px] ${
                active ? 'font-semibold text-accent' : 'text-ink-4'
              }`}
            >
              <Icon className="w-4 h-4" />
              <span>{t.label}</span>
            </button>
          )
        })}
      </nav>
    </div>
  )
}

export default App
