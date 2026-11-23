import React, { useCallback, useEffect, useState } from 'react'
import {
  GitBranch,
  Home,
  Lock,
  Plug,
  Radio,
  RefreshCw,
  Shield,
} from 'lucide-react'
import { api } from './api'
import { ConnectPolicyPage } from './pages/ConnectPolicyPage'
import { HomePage } from './pages/HomePage'
import { RulesRetroPage } from './pages/RulesRetroPage'
import { WorkflowsPage } from './pages/WorkflowsPage'
import type { OverviewResponse } from './types'

type TabKey = 'home' | 'workflows' | 'rules' | 'connect'

export function App() {
  const [tab, setTab] = useState<TabKey>('home')
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

  const navItems: Array<{ id: TabKey; label: string; icon: React.FC<{ className?: string }> }> = [
    { id: 'home', label: '飞轮个人站', icon: Home },
    { id: 'workflows', label: '工作流可视化', icon: GitBranch },
    { id: 'rules', label: '规则与复盘台账', icon: Shield },
    { id: 'connect', label: '接入与策略中心', icon: Plug },
  ]

  return (
    <div className="min-h-screen flex flex-col bg-[#F7F7F5]">
      <header className="sticky top-0 z-30 bg-white/90 backdrop-blur border-b border-slate-200/90">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between gap-4">
          <button
            onClick={() => setTab('home')}
            className="flex items-center gap-2.5 text-left cursor-pointer"
          >
            <span className="w-8 h-8 rounded-xl bg-slate-900 text-white flex items-center justify-center shadow-xs">
              <RefreshCw className="w-4 h-4" />
            </span>
            <div className="leading-tight">
              <div className="text-sm font-bold text-slate-900 tracking-tight">AI Flywheel</div>
              <div className="text-[10px] text-slate-500 font-mono">MCP · P00–P07</div>
            </div>
          </button>

          <nav className="flex items-center gap-1 overflow-x-auto">
            {navItems.map((item) => {
              const active = tab === item.id
              return (
                <button
                  key={item.id}
                  onClick={() => setTab(item.id)}
                  className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors whitespace-nowrap cursor-pointer ${
                    active
                      ? 'bg-slate-900 text-white'
                      : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                  }`}
                >
                  <item.icon className="w-3.5 h-3.5" />
                  {item.label}
                </button>
              )
            })}
          </nav>

          <div className="hidden md:flex items-center gap-2 text-[11px]">
            {overview?.workflow_lock && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-50 text-amber-800 border border-amber-200 font-mono">
                <Lock className="w-3 h-3" />
                锁持有中
              </span>
            )}
            <span
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border font-mono ${
                sseConnected
                  ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                  : 'bg-slate-100 text-slate-500 border-slate-200'
              }`}
            >
              <Radio className="w-3 h-3" />
              {sseConnected ? 'SSE Live' : 'Offline'}
            </span>
          </div>
        </div>
      </header>

      <main className="flex-1 max-w-6xl w-full mx-auto px-4 sm:px-6 py-6">
        {tab === 'home' && (
          <HomePage overview={overview} onNavigate={setTab} onRefresh={refreshAll} />
        )}
        {tab === 'workflows' && (
          <WorkflowsPage refreshToken={refreshToken} onGlobalRefresh={refreshAll} />
        )}
        {tab === 'rules' && (
          <RulesRetroPage refreshToken={refreshToken} onGlobalRefresh={refreshAll} />
        )}
        {tab === 'connect' && <ConnectPolicyPage onGlobalRefresh={refreshAll} />}
      </main>

      <footer className="border-t border-slate-200/80 bg-white/70 py-3.5 text-center text-[11px] text-slate-500">
        AI Flywheel · Open-Source Evidence-Driven Collaboration OS ·{' '}
        <a href="/llms.txt" target="_blank" rel="noreferrer" className="underline hover:text-slate-800">
          /llms.txt
        </a>{' '}
        ·{' '}
        <a href="/api/v1/overview" target="_blank" rel="noreferrer" className="underline hover:text-slate-800">
          /api/v1/overview
        </a>
      </footer>
    </div>
  )
}

export default App
