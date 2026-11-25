import React, { useEffect, useState } from 'react'
import { ArrowUp } from 'lucide-react'
import { preferredScrollBehavior } from '../../lib/labels'

/**
 * Appears smoothly at bottom-right only when the user has actually scrolled down.
 * Respects `prefers-reduced-motion` when scrolling back to top.
 */
export const BackToTop: React.FC<{ threshold?: number }> = ({ threshold = 420 }) => {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    let ticking = false
    const update = () => {
      ticking = false
      setVisible(window.scrollY > threshold)
    }
    const onScroll = () => {
      if (!ticking) {
        ticking = true
        requestAnimationFrame(update)
      }
    }
    update()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [threshold])

  return (
    <button
      type="button"
      aria-label="回到顶部"
      tabIndex={visible ? 0 : -1}
      onClick={() => window.scrollTo({ top: 0, behavior: preferredScrollBehavior() })}
      className={`pressable fixed right-5 bottom-[74px] lg:bottom-6 z-30 inline-flex items-center gap-1 rounded-full border border-line bg-surface/92 backdrop-blur-md px-3 py-1.5 text-[12px] font-medium text-ink-2 shadow-[var(--shadow-soft)] hover:border-line-strong hover:text-ink transition-[opacity,transform] duration-[200ms] ease-[var(--ease-out-quint)] ${
        visible
          ? 'opacity-100 translate-y-0 pointer-events-auto'
          : 'opacity-0 translate-y-2 pointer-events-none'
      }`}
    >
      <ArrowUp className="w-3.5 h-3.5" />
      <span>回到顶部</span>
    </button>
  )
}
