import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
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
import { ThemeCycleButton, ThemeSwitch } from './components/ThemeSwitch'
import { BackToTop } from './components/ui/BackToTop'
import { ToastProvider } from './components/ui/Toast'
import { formatShanghaiToday } from './lib/labels'
import { AboutPage } from './pages/AboutPage'
import { AutomationsPage } from './pages/AutomationsPage'
import { ConnectPolicyPage } from './pages/ConnectPolicyPage'
import { HomePage } from './pages/HomePage'
import { RulesRetroPage } from './pages/RulesRetroPage'
import { WorkflowsPage } from './pages/WorkflowsPage'
import type { OverviewResponse } from './types'

export type PageKey = 'home' | 'runs' | 'automations' | 'rules' | 'settings' | 'about'

export interface RouteIntent {
  runsTab?: 'queue' | 'pipeline'
  queueStatus?: string
  runId?: string
  rulesTab?: 'rules' | 'clues' | 'assets' | 'outcomes' | 'guidelines'
  cluesSub?: 'action' | 'observations' | 'dissents' | 'retros'
  assetsDocFilter?: 'all' | 'expired' | 'fresh'
  assetsSkillFilter?: 'all' | 'used' | 'untested' | 'failing'
  settingsTab?: 'connect' | 'storage' | 'test'
  /** Increments on each explicit navigation so deep-link effects always fire cleanly. */
  nonce?: number
}

export type NavigateFn = (page: PageKey, intent?: RouteIntent) => void

const PAGE_PATHS: Record<PageKey, string> = {
  home: '/',
  runs: '/runs',
  automations: '/automations',
  rules: '/rules',
  settings: '/settings',
  about: '/about',
}

function parseLocation(pathname: string, search: string): {
  page: PageKey
  intent: RouteIntent
} {
  const clean = pathname.replace(/\/+$/, '') || '/'
  const params = new URLSearchParams(search)
  const intent: RouteIntent = {}

  const tab = params.get('tab') || ''
  if (clean === '/workflows' || clean === '/runs') {
    if (tab === 'queue' || tab === 'pipeline') intent.runsTab = tab
    const status = params.get('status')
    if (status) intent.queueStatus = status
    const run = params.get('run')
    if (run) intent.runId = run
    return { page: 'runs', intent }
  }
  if (clean === '/automations' || clean === '/scheduler') {
    return { page: 'automations', intent }
  }
  if (clean === '/policies') {
    intent.rulesTab = 'guidelines'
    return { page: 'rules', intent }
  }
  if (clean === '/rules') {
    if (
      tab === 'rules' ||
      tab === 'clues' ||
      tab === 'assets' ||
      tab === 'outcomes' ||
      tab === 'guidelines'
    ) {
      intent.rulesTab = tab
    }
    const sub = params.get('sub')
    if (sub === 'action' || sub === 'observations' || sub === 'dissents' || sub === 'retros') {
      intent.cluesSub = sub
    }
    const doc = params.get('doc')
    if (doc === 'all' || doc === 'expired' || doc === 'fresh') intent.assetsDocFilter = doc
    const skill = params.get('skill')
    if (skill === 'all' || skill === 'used' || skill === 'untested' || skill === 'failing') {
      intent.assetsSkillFilter = skill
    }
    return { page: 'rules', intent }
  }
  if (clean === '/connect') {
    intent.settingsTab = 'connect'
    return { page: 'settings', intent }
  }
  if (clean === '/adapters') {
    intent.settingsTab = 'storage'
    return { page: 'settings', intent }
  }
  if (clean === '/settings') {
    if (tab === 'connect' || tab === 'storage' || tab === 'test') {
      intent.settingsTab = tab
    }
    return { page: 'settings', intent }
  }
  if (clean === '/about') {
    return { page: 'about', intent }
  }
  return { page: 'home', intent }
}

function buildUrl(page: PageKey, intent?: RouteIntent): string {
  const base = PAGE_PATHS[page]
  if (!intent) return base
  const params = new URLSearchParams()
  if (page === 'runs') {
    if (intent.runsTab) params.set('tab', intent.runsTab)
    if (intent.queueStatus) params.set('status', intent.queueStatus)
    if (intent.runId) params.set('run', intent.runId)
  } else if (page === 'rules') {
    if (intent.rulesTab) params.set('tab', intent.rulesTab)
    if (intent.cluesSub) params.set('sub', intent.cluesSub)
    if (intent.assetsDocFilter) params.set('doc', intent.assetsDocFilter)
    if (intent.assetsSkillFilter) params.set('skill', intent.assetsSkillFilter)
  } else if (page === 'settings') {
    if (intent.settingsTab) params.set('tab', intent.settingsTab)
  }
  const qs = params.toString()
  return qs ? `${base}?${qs}` : base
}

function TodayLabel() {
  return <span className="num text-[12px] text-ink-4">{formatShanghaiToday()}</span>
}

function AppShell() {
  const initial = parseLocation(window.location.pathname, window.location.search)
  const [page, setPageState] = useState<PageKey>(initial.page)
  const [routeIntent, setRouteIntent] = useState<RouteIntent>({ ...initial.intent, nonce: 1 })
  const [overview, setOverview] = useState<OverviewResponse | null>(null)
  const [sseConnected, setSseConnected] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)
  const [runsRefreshToken, setRunsRefreshToken] = useState(0)

  // Measured sliding indicator for desktop sidebar nav
  const navRef = useRef<HTMLElement>(null)
  const navItemRefs = useRef<Map<PageKey, HTMLButtonElement>>(new Map())
  const [navPill, setNavPill] = useState<{ y: number; h: number; visible: boolean }>({
    y: 0,
    h: 40,
    visible: false,
  })
  const [navAnimated, setNavAnimated] = useState(false)

  const navigate: NavigateFn = useCallback((next: PageKey, intent?: RouteIntent) => {
    setPageState(next)
    setRouteIntent({ ...(intent || {}), nonce: Date.now() })
    const targetUrl = buildUrl(next, intent)
    const currentUrl = `${window.location.pathname}${window.location.search}`
    if (currentUrl !== targetUrl) {
      window.history.pushState(null, '', targetUrl)
    }
    window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior })
  }, [])

  useEffect(() => {
    const onPopState = () => {
      const parsed = parseLocation(window.location.pathname, window.location.search)
      setPageState(parsed.page)
      setRouteIntent({ ...parsed.intent, nonce: Date.now() })
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const refreshOverview = useCallback(() => {
    api
      .overview()
      .then((data) => setOverview(data))
      .catch(() => {})
  }, [])

  const refreshAll = useCallback(() => {
    refreshOverview()
    setRefreshToken((n) => n + 1)
    setRunsRefreshToken((n) => n + 1)
  }, [refreshOverview])

  useEffect(() => {
    refreshAll()
    const es = new EventSource('/api/events')
    es.addEventListener('connected', () => setSseConnected(true))

    // Scoped SSE refresh: live node/queue updates refresh overview + runs/automations
    // without needlessly resetting unrelated governance/policy forms.
    const onRunLive = () => {
      refreshOverview()
      setRunsRefreshToken((n) => n + 1)
    }
    const onGlobalLive = () => refreshAll()

    es.addEventListener('run_started', onRunLive)
    es.addEventListener('node_update', onRunLive)
    es.addEventListener('awaiting_approval', onRunLive)
    es.addEventListener('queue_updated', onRunLive)
    es.addEventListener('run_completed', onGlobalLive)
    es.addEventListener('run_failed', onRunLive)
    es.addEventListener('run_rejected', onRunLive)
    es.addEventListener('automation_updated', onRunLive)
    es.onerror = () => setSseConnected(false)
    return () => es.close()
  }, [refreshAll, refreshOverview])

  const brandName = overview?.site?.brand?.name || 'AI Flywheel'
  const hasApprovalWaiting = overview?.latest_run?.status === 'AWAITING_APPROVAL'

  const mainNav: Array<{
    key: PageKey
    label: string
    shortLabel: string
    icon: React.FC<{ className?: string }>
    dot?: boolean
  }> = [
    { key: 'home', label: '今日态势', shortLabel: '态势', icon: Activity },
    {
      key: 'runs',
      label: '沉淀与审批',
      shortLabel: '沉淀',
      icon: GitBranch,
      dot: hasApprovalWaiting,
    },
    { key: 'automations', label: '自动化调度', shortLabel: '调度', icon: Clock },
    { key: 'rules', label: '规则与进化', shortLabel: '规则', icon: Shield },
    { key: 'settings', label: '接入与设置', shortLabel: '设置', icon: Settings },
  ]

  useLayoutEffect(() => {
    const btn = navItemRefs.current.get(page)
    if (!btn) {
      setNavPill((prev) => ({ ...prev, visible: false }))
      return
    }
    setNavPill({ y: btn.offsetTop, h: btn.offsetHeight, visible: true })
    const raf = requestAnimationFrame(() => setNavAnimated(true))
    return () => cancelAnimationFrame(raf)
  }, [page])

  const activeMobileIdx = mainNav.findIndex((m) => m.key === page)

  return (
    <div className="min-h-dvh flex bg-bg text-ink">
      {/* Minimal Desktop Left Sidebar (5 main + About) */}
      <aside className="sticky top-0 hidden h-dvh w-[204px] shrink-0 flex-col border-r border-line bg-sidebar px-3 pb-3.5 pt-6 lg:flex">
        <button
          type="button"
          onClick={() => navigate('home')}
          className="pressable mb-5 flex h-[44px] items-center gap-2 px-1.5 text-left text-ink rounded-[var(--radius-control)]"
        >
          <span className="flex size-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-accent text-accent-contrast">
            <RefreshCw className="w-3.5 h-3.5" />
          </span>
          <span className="min-w-0 truncate text-[14px] font-bold tracking-tight">
            {brandName}
          </span>
        </button>

        <nav
          ref={navRef}
          className="relative -mx-1 flex-1 overflow-y-auto px-1 flex flex-col gap-1"
          aria-label="主导航"
        >
          {/* Smooth sliding active pill */}
          {navPill.visible && (
            <span
              aria-hidden="true"
              className={`pointer-events-none absolute inset-x-1 top-0 rounded-[var(--radius-control)] bg-accent/10 dark:bg-accent-soft ${
                navAnimated
                  ? 'transition-[transform,height,opacity] duration-[220ms] ease-[var(--ease-out-quint)]'
                  : ''
              }`}
              style={{
                transform: `translate3d(0, ${navPill.y}px, 0)`,
                height: `${navPill.h}px`,
              }}
            />
          )}
          {mainNav.map((item) => {
            const isActive = page === item.key
            const Icon = item.icon
            return (
              <button
                key={item.key}
                ref={(node) => {
                  if (node) navItemRefs.current.set(item.key, node)
                  else navItemRefs.current.delete(item.key)
                }}
                type="button"
                onClick={() => navigate(item.key)}
                aria-current={isActive ? 'page' : undefined}
                className={`pressable relative z-10 flex h-10 w-full items-center gap-2.5 rounded-[var(--radius-control)] px-2.5 text-[14px] ${
                  isActive
                    ? 'font-semibold text-ink'
                    : 'font-medium text-ink-3 hover:bg-bg-sunk/70 hover:text-ink'
                }`}
              >
                <span
                  className={`flex w-[20px] shrink-0 justify-center transition-colors duration-150 ${
                    isActive ? 'text-accent' : ''
                  }`}
                >
                  <Icon className="w-[17px] h-[17px]" />
                </span>
                <span className="min-w-0 truncate">{item.label}</span>
                {item.dot && (
                  <span
                    className="status-dot-live ml-auto shrink-0 bg-amber"
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
            onClick={() => navigate('about')}
            aria-current={page === 'about' ? 'page' : undefined}
            className={`pressable flex h-9 w-full items-center gap-2.5 rounded-[var(--radius-control)] px-2 text-[13.5px] ${
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
                className={`size-1.5 rounded-full transition-colors duration-200 ${
                  sseConnected ? 'bg-ok' : 'status-dot-live bg-amber'
                }`}
              />
              <span>{sseConnected ? '实时同步中' : '重连中…'}</span>
            </span>
            <a
              href="/llms.txt"
              target="_blank"
              rel="noreferrer"
              className="hover:text-ink-2 transition-colors"
            >
              llms.txt
            </a>
          </div>
        </div>
      </aside>

      {/* Main Content Column */}
      <div className="min-w-0 flex-1 flex flex-col">
        {/* Mobile Header (now includes ThemeCycleButton so mobile can switch light/dark!) */}
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-line bg-sidebar/95 backdrop-blur-md px-4 lg:hidden">
          <button
            type="button"
            onClick={() => navigate('home')}
            className="pressable flex items-center gap-2 text-ink"
          >
            <span className="flex size-6 items-center justify-center rounded-[var(--radius-mark)] bg-accent text-accent-contrast">
              <RefreshCw className="w-3.5 h-3.5" />
            </span>
            <span className="text-[15px] font-bold">{brandName}</span>
          </button>
          <div className="flex items-center gap-2.5">
            <TodayLabel />
            <button
              type="button"
              onClick={() => navigate('about')}
              className={`pressable-sm px-2 py-1 rounded-[var(--radius-mark)] text-[12.5px] font-medium ${
                page === 'about' ? 'bg-accent-soft text-accent' : 'text-ink-3 hover:text-ink'
              }`}
            >
              关于
            </button>
            <ThemeCycleButton />
          </div>
        </header>

        <main className="flex-1 px-4 pb-24 pt-5 lg:px-10 lg:pb-10 lg:pt-8">
          <div key={page} className="animate-page-enter">
            {page === 'home' && (
              <HomePage overview={overview} onNavigate={navigate} onRefresh={refreshAll} />
            )}
            {page === 'runs' && (
              <WorkflowsPage
                refreshToken={runsRefreshToken}
                onGlobalRefresh={refreshAll}
                intent={routeIntent}
              />
            )}
            {page === 'automations' && (
              <AutomationsPage
                refreshToken={runsRefreshToken}
                onNavigate={navigate}
                onGlobalRefresh={refreshAll}
              />
            )}
            {page === 'rules' && (
              <RulesRetroPage
                refreshToken={refreshToken}
                onGlobalRefresh={refreshAll}
                intent={routeIntent}
              />
            )}
            {page === 'settings' && (
              <ConnectPolicyPage
                intent={routeIntent}
                onNavigate={navigate}
                onGlobalRefresh={refreshAll}
              />
            )}
            {page === 'about' && (
              <AboutPage onNavigate={navigate} refreshToken={refreshToken} />
            )}
          </div>
        </main>
      </div>

      {/* Mobile Bottom Tab Bar (5 clean items, sliding top accent bar + approval dot) */}
      <nav
        aria-label="移动端导航"
        className="fixed inset-x-0 bottom-0 z-40 border-t border-line bg-sidebar/95 backdrop-blur-md lg:hidden"
      >
        <div className="relative grid grid-cols-5 py-1.5">
          {activeMobileIdx >= 0 && (
            <span
              aria-hidden="true"
              className="pointer-events-none absolute top-0 left-0 h-[2px] w-[20%] px-4 transition-transform duration-[220ms] ease-[var(--ease-out-quint)]"
              style={{ transform: `translate3d(${activeMobileIdx * 100}%, 0, 0)` }}
            >
              <span className="block h-full w-full rounded-full bg-accent" />
            </span>
          )}
          {mainNav.map((t) => {
            const active = page === t.key
            const Icon = t.icon
            return (
              <button
                key={t.key}
                type="button"
                onClick={() => navigate(t.key)}
                aria-current={active ? 'page' : undefined}
                className={`pressable-sm relative flex flex-col items-center gap-0.5 py-1 text-[11px] ${
                  active ? 'font-semibold text-accent' : 'text-ink-4'
                }`}
              >
                <span className="relative">
                  <Icon className="w-[18px] h-[18px]" />
                  {t.dot && (
                    <span
                      aria-label="有批次等待审批"
                      className="status-dot-live absolute -top-0.5 -right-1.5 bg-amber ring-2 ring-sidebar"
                    />
                  )}
                </span>
                <span className="truncate">{t.shortLabel}</span>
              </button>
            )
          })}
        </div>
      </nav>

      <BackToTop />
    </div>
  )
}

export function App() {
  return (
    <ToastProvider>
      <AppShell />
    </ToastProvider>
  )
}

export default App
