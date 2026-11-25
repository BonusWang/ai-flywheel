import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

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

/**
 * Segmented PillTabs with a measured GPU-sliding indicator pill.
 *
 * Uses `offsetLeft`, `offsetTop`, `offsetWidth`, `offsetHeight` so it glides
 * smoothly both horizontally and if wrapped across two rows on narrow viewports,
 * and seamlessly redirects mid-flight when clicked rapidly.
 */
export const PillTabs: React.FC<{
  items: Array<{ key: string; label: string; count?: number }>
  active: string
  onChange: (key: string) => void
  className?: string
}> = ({ items, active, onChange, className = '' }) => {
  const containerRef = useRef<HTMLDivElement>(null)
  const btnRefs = useRef<Map<string, HTMLButtonElement>>(new Map())
  const [pill, setPill] = useState<{
    x: number
    y: number
    w: number
    h: number
    ready: boolean
  }>({ x: 0, y: 0, w: 0, h: 0, ready: false })
  const [animated, setAnimated] = useState(false)

  const itemsSignature = items.map((it) => `${it.key}:${it.label}:${it.count ?? ''}`).join('|')

  const measure = useCallback(() => {
    const btn = btnRefs.current.get(active)
    if (!btn) return
    const next = {
      x: btn.offsetLeft,
      y: btn.offsetTop,
      w: btn.offsetWidth,
      h: btn.offsetHeight,
      ready: true,
    }
    setPill((prev) =>
      prev.ready &&
      prev.x === next.x &&
      prev.y === next.y &&
      prev.w === next.w &&
      prev.h === next.h
        ? prev
        : next
    )
  }, [active])

  useLayoutEffect(() => {
    measure()
    const raf = requestAnimationFrame(() => setAnimated(true))
    return () => cancelAnimationFrame(raf)
  }, [measure, itemsSignature])

  useEffect(() => {
    const el = containerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => measure())
    ro.observe(el)
    return () => ro.disconnect()
  }, [measure])

  return (
    <div
      ref={containerRef}
      role="tablist"
      className={`relative inline-flex flex-wrap items-center gap-1 rounded-[var(--radius-panel)] sm:rounded-full border border-line bg-bg-sunk p-1 ${className}`}
    >
      {pill.ready && (
        <span
          aria-hidden="true"
          className={`pointer-events-none absolute top-0 left-0 rounded-full border border-line bg-surface shadow-[var(--shadow-card)] ${
            animated
              ? 'transition-[transform,width,height] duration-[220ms] ease-[var(--ease-out-quint)]'
              : ''
          }`}
          style={{
            transform: `translate3d(${pill.x}px, ${pill.y}px, 0)`,
            width: `${pill.w}px`,
            height: `${pill.h}px`,
          }}
        />
      )}
      {items.map((it) => {
        const isAct = active === it.key
        return (
          <button
            key={it.key}
            ref={(node) => {
              if (node) btnRefs.current.set(it.key, node)
              else btnRefs.current.delete(it.key)
            }}
            type="button"
            role="tab"
            aria-selected={isAct}
            onClick={() => onChange(it.key)}
            className={`pressable relative z-10 inline-flex items-center gap-1.5 rounded-full px-3.5 py-1 text-[12.5px] font-medium border border-transparent ${
              isAct ? 'text-ink' : 'text-ink-3 hover:text-ink'
            }`}
          >
            <span>{it.label}</span>
            {it.count !== undefined && (
              <span
                className={`num text-[11px] transition-colors duration-150 ${
                  isAct ? 'text-accent font-semibold' : 'text-ink-4'
                }`}
              >
                {it.count}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
