import React, { useEffect, useState } from 'react'
import { Monitor, Moon, Sun } from 'lucide-react'

type Choice = 'dark' | 'system' | 'light'

const OPTIONS: Array<{ key: Choice; label: string; icon: React.ReactNode }> = [
  { key: 'dark', label: '深色', icon: <Moon className="w-3.5 h-3.5" /> },
  { key: 'system', label: '跟随系统', icon: <Monitor className="w-3.5 h-3.5" /> },
  { key: 'light', label: '浅色', icon: <Sun className="w-3.5 h-3.5" /> },
]

const THEME_EVENT = 'flywheel-theme-change'
let transitionTimer: number | null = null

function readSavedChoice(): Choice {
  if (typeof window === 'undefined') return 'system'
  const saved = localStorage.getItem('flywheel-theme')
  if (saved === 'dark' || saved === 'light') return saved
  return 'system'
}

function applyTheme(choice: Choice) {
  const root = document.documentElement
  root.classList.add('theme-transitioning')
  if (transitionTimer) window.clearTimeout(transitionTimer)
  transitionTimer = window.setTimeout(() => {
    root.classList.remove('theme-transitioning')
    transitionTimer = null
  }, 210)

  if (choice === 'system') {
    localStorage.removeItem('flywheel-theme')
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches
    root.setAttribute('data-theme', dark ? 'dark' : 'light')
  } else {
    localStorage.setItem('flywheel-theme', choice)
    root.setAttribute('data-theme', choice)
  }
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: choice }))
}

export const ThemeSwitch: React.FC<{ className?: string }> = ({ className = '' }) => {
  const [current, setCurrent] = useState<Choice>(readSavedChoice)

  useEffect(() => {
    const onSync = () => setCurrent(readSavedChoice())
    window.addEventListener(THEME_EVENT, onSync)
    return () => window.removeEventListener(THEME_EVENT, onSync)
  }, [])

  const index = OPTIONS.findIndex((o) => o.key === current)

  return (
    <div
      role="radiogroup"
      aria-label="外观"
      className={`relative grid h-[34px] grid-cols-3 rounded-full border border-line bg-bg-sunk p-[3px] ${className}`}
    >
      <span
        aria-hidden="true"
        className="absolute inset-y-[3px] left-[3px] w-[calc((100%-6px)/3)] rounded-full border border-line bg-surface shadow-[var(--shadow-card)] transition-transform duration-[220ms] ease-[var(--ease-out-quint)]"
        style={{ transform: `translate3d(${index * 100}%, 0, 0)` }}
      />
      {OPTIONS.map((o) => (
        <button
          key={o.key}
          type="button"
          role="radio"
          aria-checked={current === o.key}
          title={o.label}
          onClick={() => {
            setCurrent(o.key)
            applyTheme(o.key)
          }}
          className={`pressable-sm relative z-10 flex items-center justify-center rounded-full transition-colors duration-150 ${
            current === o.key ? 'text-ink' : 'text-ink-4 hover:text-ink-2'
          }`}
        >
          {o.icon}
          <span className="sr-only">{o.label}</span>
        </button>
      ))}
    </div>
  )
}

/** Compact single-button theme switcher for mobile / narrow header. */
export const ThemeCycleButton: React.FC<{ className?: string }> = ({ className = '' }) => {
  const [current, setCurrent] = useState<Choice>(readSavedChoice)

  useEffect(() => {
    const onSync = () => setCurrent(readSavedChoice())
    window.addEventListener(THEME_EVENT, onSync)
    return () => window.removeEventListener(THEME_EVENT, onSync)
  }, [])

  const nextChoice = (): Choice => {
    const isDarkNow = document.documentElement.getAttribute('data-theme') === 'dark'
    return isDarkNow ? 'light' : 'dark'
  }

  const isDark =
    current === 'dark' ||
    (current === 'system' &&
      typeof document !== 'undefined' &&
      document.documentElement.getAttribute('data-theme') === 'dark')

  return (
    <button
      type="button"
      onClick={() => {
        const next = nextChoice()
        setCurrent(next)
        applyTheme(next)
      }}
      title={isDark ? '切换为浅色外观' : '切换为深色外观'}
      aria-label={isDark ? '切换为浅色外观' : '切换为深色外观'}
      className={`pressable-sm flex size-8 items-center justify-center rounded-[var(--radius-control)] border border-line bg-surface text-ink-3 hover:border-line-strong hover:text-ink ${className}`}
    >
      {isDark ? (
        <Sun key="sun" className="w-4 h-4 animate-check-pop" />
      ) : (
        <Moon key="moon" className="w-4 h-4 animate-check-pop" />
      )}
    </button>
  )
}
