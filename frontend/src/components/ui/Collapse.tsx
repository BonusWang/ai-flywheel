import React, { useEffect, useState } from 'react'

/**
 * Interruptible CSS-Grid collapse (`0fr` <-> `1fr`) with subtle opacity + micro-slide.
 *
 * - Uses `inert` when collapsed so keyboard Tab focus never leaks into hidden inputs/buttons.
 * - For `lazy` / `unmountOnExit`, mounts children first and expands on the next animation frame
 *   so even the very first open animates smoothly from `0fr` to `1fr`.
 */
export const Collapse: React.FC<{
  open: boolean
  children: React.ReactNode
  className?: string
  innerClassName?: string
  lazy?: boolean
  unmountOnExit?: boolean
}> = ({
  open,
  children,
  className = '',
  innerClassName = '',
  lazy = false,
  unmountOnExit = false,
}) => {
  const needsMountDelay = lazy || unmountOnExit
  const [hasOpened, setHasOpened] = useState(open)
  const [rendered, setRendered] = useState(open)
  const [expanded, setExpanded] = useState(open)

  useEffect(() => {
    let raf1 = 0
    let raf2 = 0
    let exitTimer: number | null = null

    if (open) {
      const wasUnmounted = (lazy && !hasOpened) || (unmountOnExit && !rendered)
      setHasOpened(true)
      setRendered(true)
      if (needsMountDelay && wasUnmounted) {
        // Mount at 0fr first, then transition to 1fr on next frame so first open animates smoothly
        raf1 = requestAnimationFrame(() => {
          raf2 = requestAnimationFrame(() => setExpanded(true))
        })
      } else {
        setExpanded(true)
      }
    } else {
      setExpanded(false)
      if (unmountOnExit) {
        exitTimer = window.setTimeout(() => setRendered(false), 280)
      }
    }

    return () => {
      if (raf1) cancelAnimationFrame(raf1)
      if (raf2) cancelAnimationFrame(raf2)
      if (exitTimer) window.clearTimeout(exitTimer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, unmountOnExit, lazy])

  const shouldRenderChildren = unmountOnExit ? rendered : lazy ? hasOpened : true

  return (
    <div
      aria-hidden={!open}
      inert={!open ? true : undefined}
      className={`grid transition-[grid-template-rows,opacity] duration-[260ms] ease-[var(--ease-out-quint)] ${
        expanded ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0 pointer-events-none'
      } ${className}`}
    >
      <div className="min-h-0 overflow-hidden">
        <div
          className={`transition-transform duration-[260ms] ease-[var(--ease-out-quint)] ${
            expanded ? 'translate-y-0' : '-translate-y-1'
          } ${innerClassName}`}
        >
          {shouldRenderChildren ? children : null}
        </div>
      </div>
    </div>
  )
}
