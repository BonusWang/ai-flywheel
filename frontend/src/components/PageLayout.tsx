import React from 'react'

export const ReadingLayout: React.FC<{
  children: React.ReactNode
  aside?: React.ReactNode
  className?: string
}> = ({ children, aside, className = '' }) => (
  <div
    className={`mx-auto grid max-w-[var(--page-max-reading)] gap-8 pb-12 lg:grid-cols-[minmax(0,1fr)_300px] lg:gap-10 2xl:grid-cols-[minmax(0,1fr)_330px] ${className}`}
  >
    <div className="min-w-0">{children}</div>
    {aside && (
      <aside className="min-w-0 space-y-4 lg:sticky lg:top-6 lg:self-start">{aside}</aside>
    )}
  </div>
)

export const AsideCard: React.FC<{
  title: React.ReactNode
  children: React.ReactNode
  className?: string
}> = ({ title, children, className = '' }) => (
  <section className={`card p-5 ${className}`}>
    <h2 className="text-[13px] font-semibold text-ink">{title}</h2>
    <div className="mt-3">{children}</div>
  </section>
)

export const PillTabs: React.FC<{
  items: Array<{ key: string; label: string; count?: number }>
  active: string
  onChange: (key: string) => void
}> = ({ items, active, onChange }) => (
  <div className="inline-flex flex-wrap items-center gap-1 rounded-full border border-line bg-bg-sunk p-1">
    {items.map((it) => {
      const isAct = active === it.key
      return (
        <button
          key={it.key}
          type="button"
          onClick={() => onChange(it.key)}
          className={`inline-flex items-center gap-1.5 rounded-full px-3.5 py-1 text-[12.5px] font-medium transition-colors ${
            isAct
              ? 'bg-surface text-ink shadow-[var(--shadow-card)] border border-line'
              : 'text-ink-3 hover:text-ink border border-transparent'
          }`}
        >
          <span>{it.label}</span>
          {it.count !== undefined && (
            <span className={`num text-[11px] ${isAct ? 'text-accent' : 'text-ink-4'}`}>
              {it.count}
            </span>
          )}
        </button>
      )
    })}
  </div>
)
