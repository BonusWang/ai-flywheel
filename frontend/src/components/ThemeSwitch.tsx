import React, { useEffect, useState } from 'react'
import { Monitor, Moon, Sun } from 'lucide-react'

type Choice = 'dark' | 'system' | 'light'

const OPTIONS: Array<{ key: Choice; label: string; icon: React.ReactNode }> = [
  { key: 'dark', label: '深色', icon: <Moon className="w-3.5 h-3.5" /> },
  { key: 'system', label: '跟随系统', icon: <Monitor className="w-3.5 h-3.5" /> },
  { key: 'light', label: '浅色', icon: <Sun className="w-3.5 h-3.5" /> },
]

function applyTheme(choice: Choice) {
  if (choice === 'system') {
    localStorage.removeItem('flywheel-theme')
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light')
  } else {
    localStorage.setItem('flywheel-theme', choice)
    document.documentElement.setAttribute('data-theme', choice)
  }
}

export const ThemeSwitch: React.FC<{ className?: string }> = ({ className = '' }) => {
  const [current, setCurrent] = useState<Choice>('system')

  useEffect(() => {
    const saved = localStorage.getItem('flywheel-theme')
    if (saved === 'dark' || saved === 'light') {
      setCurrent(saved)
    } else {
      setCurrent('system')
    }
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
        className="absolute inset-y-[3px] left-[3px] w-[calc((100%-6px)/3)] rounded-full border border-line bg-surface shadow-[var(--shadow-card)] transition-transform duration-200 ease-[var(--ease-out-quart)]"
        style={{ transform: `translateX(${index * 100}%)` }}
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
          className={`relative z-10 flex items-center justify-center rounded-full transition-colors duration-150 ${
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
