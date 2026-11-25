import React, { useCallback, useEffect, useState } from 'react'
import {
  Activity,
  Clock,
  GitBranch,
  Info,
  RefreshCw,
  Settings,
  Shield,
} from 'lucide-react'
import { api } from './api'
import { ThemeSwitch } from './components/ThemeSwitch'
import { AboutPage } from './pages/AboutPage'
import { AutomationsPage } from './pages/AutomationsPage'
import { ConnectPolicyPage } from './pages/ConnectPolicyPage'
import { HomePage } from './pages/HomePage'
import { RulesRetroPage } from './pages/RulesRetroPage'
import { WorkflowsPage } from './pages/WorkflowsPage'
import type { OverviewResponse } from './types'

export type PageKey = 'home' | 'runs' | 'automations' | 'rules' | 'settings' | 'about'

const PAGE_PATHS: Record<PageKey, string> = {
  home: '/',
  runs: '/runs',
  automations: '/automations',
  rules: '/rules',
  settings: '/settings',
  about: '/about',
}

function pageFromPathname(pathname: string): {
  page: PageKey
  rulesTab?: 'rules' | 'guidelines' | 'clues'
  settingsTab?: 'connect' | 'storage' | 'test'
} {
  const clean = pathname.replace(/\/+$/, '') || '/'
  if (clean === '/workflows' || clean === '/runs') return { page: 'runs' }
  if (clean === '/automations' || clean === '/scheduler') return { page: 'automations' }
  if (clean === '/policies') return { page: 'rules', rulesTab: 'guidelines' }
  if (clean === '/rules') return { page: 'rules', rulesTab: 'rules' }
  if (clean === '/connect') return { page: 'settings', settingsTab: 'connect' }
  if (clean === '/adapters') return { page: 'settings', settingsTab: 'storage' }
  if (clean === '/settings') return { page: 'settings', settingsTab: 'connect' }
  if (clean === '/about') return { page: 'about' }
  return { page: 'home' }
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
  const initial = pageFromPathname(window.location.pathname)
  const [page, setPageState] = useState<PageKey>(initial.page)
  const [rulesInitialTab, setRulesInitialTab] = useState<'rules' | 'guidelines' | 'clues'>(
    initial.rulesTab || 'rules'
  )
  const [settingsInitialTab, setSettingsInitialTab] = useState<'connect' | 'storage' | 'test'>(
    initial.settingsTab || 'connect'
  )
  const [overview, setOverview] = useState<OverviewResponse | null>(null)
  const [sseConnected, setSseConnected] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)

  const setPage = useCallback((next: PageKey) => {
    setPageState(next)
    const targetPath = PAGE_PATHS[next]
    if (window.location.pathname !== targetPath) {
      window.history.pushState(null, '', targetPath)
    }
    window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior })
  }, [])

  useEffect(() => {
    const onPopState = () => {
      const parsed = pageFromPathname(window.location.pathname)
      setPageState(parsed.page)
      if (parsed.rulesTab) setRulesInitialTab(parsed.rulesTab)
      if (parsed.settingsTab) setSettingsInitialTab(parsed.settingsTab)
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

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
    es.addEventListener('automation_updated', onLive)
    es.onerror = () => setSseConnected(false)
    return () => es.close()
  }, [refreshAll])

  const brandName = overview?.site?.brand?.name || 'AI Flywheel'
  const hasApprovalWaiting = overview?.latest_run?.status === 'AWAITING_APPROVAL'

  const mainNav: Array<{
    key: PageKey
    label: string
    icon: React.FC<{ className?: string }>
    dot?: boolean
  }> = [
    { key: 'home', label: '今日态势', icon: Activity },
    { key: 'runs', label: '沉淀与审批', icon: GitBranch, dot: hasApprovalWaiting },
    { key: 'automations', label: '自动化调度', icon: Clock },
    { key: 'rules', label: '规则与进化', icon: Shield },
    { key: 'settings', label: '接入与设置', icon: Settings },
  ]

  return (
    <div className="min-h-dvh flex bg-bg text-ink">
      {/* Minimal Desktop Left Sidebar (4 main + About) */}
      <aside className="sticky top-0 hidden h-dvh w-[204px] shrink-0 flex-col border-r border-line bg-sidebar px-3 pb-3.5 pt-6 lg:flex">
        <button
          type="button"
          onClick={() => setPage('home')}
          className="mb-5 flex h-[44px] items-center gap-2 px-1.5 text-left text-ink"
        >
          <span className="flex size-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-accent text-accent-contrast">
            <RefreshCw className="w-3.5 h-3.5" />
          </span>
          <span className="min-w-0 truncate text-[14px] font-bold tracking-tight">
            {brandName}
          </span>
        </button>

        <nav className="-mx-1 flex-1 overflow-y-auto px-1 flex flex-col gap-1" aria-label="主导航">
          {mainNav.map((item) => {
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
                    className="ml-auto size-2 shrink-0 rounded-full bg-amber"
                    title="有批次等待审批"
                  />
                )}
              </button>
            )
          })}
        </nav>

        {/* Bottom quiet area: About + Theme + Status */}
        <div className="mt-2 space-y-2.5 px-1 pt-2.5 border-t border-line-soft">
          <button
            type="button"
            onClick={() => setPage('about')}
            aria-current={page === 'about' ? 'page' : undefined}
            className={`flex h-9 w-full items-center gap-2.5 rounded-[var(--radius-control)] px-2 text-[13.5px] transition-colors ${
              page === 'about'
                ? 'bg-accent/10 font-semibold text-ink dark:bg-accent-soft'
                : 'font-medium text-ink-3 hover:bg-bg-sunk hover:text-ink'
            }`}
          >
            <Info className={`w-4 h-4 ${page === 'about' ? 'text-accent' : ''}`} />
            <span>关于</span>
          </button>

          <ThemeSwitch className="mx-0.5" />

          <div className="flex items-center justify-between px-1.5 text-[11px] text-ink-4">
            <span className="inline-flex items-center gap-1.5">
              <span
                className={`size-1.5 rounded-full ${sseConnected ? 'bg-ok' : 'bg-ink-4'}`}
              />
              <span>{sseConnected ? '服务正常' : '连接中'}</span>
            </span>
            <a href="/llms.txt" target="_blank" rel="noreferrer" className="hover:text-ink-2">
              llms.txt
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
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setPage('about')}
              className={`text-[12.5px] font-medium ${
                page === 'about' ? 'text-accent' : 'text-ink-3'
              }`}
            >
              关于
            </button>
            <TodayLabel />
          </div>
        </header>

        <main className="flex-1 px-4 pb-20 pt-5 lg:px-10 lg:pb-10 lg:pt-8">
          {page === 'home' && (
            <HomePage overview={overview} onNavigate={setPage} onRefresh={refreshAll} />
          )}
          {page === 'runs' && (
            <WorkflowsPage refreshToken={refreshToken} onGlobalRefresh={refreshAll} />
          )}
          {page === 'automations' && (
            <AutomationsPage
              refreshToken={refreshToken}
              onNavigate={setPage}
              onGlobalRefresh={refreshAll}
            />
          )}
          {page === 'rules' && (
            <RulesRetroPage
              refreshToken={refreshToken}
              onGlobalRefresh={refreshAll}
              initialTab={rulesInitialTab}
            />
          )}
          {page === 'settings' && (
            <ConnectPolicyPage
              initialTab={settingsInitialTab}
              onNavigate={setPage}
              onGlobalRefresh={refreshAll}
            />
          )}
          {page === 'about' && (
            <AboutPage onNavigate={setPage} refreshToken={refreshToken} />
          )}
        </main>
      </div>

      {/* Mobile Bottom Tab Bar (5 clean items) */}
      <nav
        aria-label="移动端导航"
        className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-5 border-t border-line bg-sidebar/95 backdrop-blur py-1.5 lg:hidden"
      >
        {mainNav.map((t) => {
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
