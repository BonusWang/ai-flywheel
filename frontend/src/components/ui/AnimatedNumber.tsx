import React, { useEffect, useRef, useState } from 'react'

/**
 * Smoothly interpolates numeric KPI changes (`tabular-nums`, GPU-friendly rAF)
 * and applies a brief subtle accent tint when live values update.
 * Immediately snaps under `prefers-reduced-motion: reduce`.
 */
export const AnimatedNumber: React.FC<{
  value: number
  suffix?: string
  className?: string
  format?: (n: number) => string
}> = ({ value, suffix = '', className = '', format }) => {
  const [display, setDisplay] = useState(value)
  const [pulsed, setPulsed] = useState(false)
  const prevRef = useRef(value)

  useEffect(() => {
    const from = prevRef.current
    if (from === value) return
    prevRef.current = value

    const reduce =
      typeof window !== 'undefined' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches

    if (reduce) {
      setDisplay(value)
      return
    }

    setPulsed(true)
    const pulseTimer = window.setTimeout(() => setPulsed(false), 380)

    const start = performance.now()
    const duration = 260
    let raf = 0

    const step = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      // Ease-out quint
      const eased = 1 - Math.pow(1 - t, 5)
      const current = from + (value - from) * eased
      setDisplay(t >= 1 ? value : Math.round(current))
      if (t < 1) {
        raf = requestAnimationFrame(step)
      }
    }
    raf = requestAnimationFrame(step)

    return () => {
      cancelAnimationFrame(raf)
      window.clearTimeout(pulseTimer)
    }
  }, [value])

  const text = format ? format(display) : String(display)

  return (
    <span
      className={`num transition-colors duration-[300ms] ease-[var(--ease-out-quint)] ${
        pulsed ? 'text-accent' : ''
      } ${className}`}
    >
      {text}
      {suffix}
    </span>
  )
}
