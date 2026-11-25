import React, { useEffect, useRef, useState } from 'react'

/**
 * Two-step inline confirmation button for destructive or irreversible actions.
 *
 * First click arms the button into a clear warning state (`confirmLabel`) and
 * auto-reverts after `timeoutMs` (default 4000ms), on `Escape`, on blur, or when `disabled`.
 */
export const ConfirmButton: React.FC<{
  label: React.ReactNode
  confirmLabel?: React.ReactNode
  onConfirm: () => void | Promise<void>
  disabled?: boolean
  timeoutMs?: number
  className?: string
  confirmClassName?: string
  title?: string
}> = ({
  label,
  confirmLabel = '再点一次确认',
  onConfirm,
  disabled = false,
  timeoutMs = 4000,
  className = '',
  confirmClassName = '',
  title,
}) => {
  const [armed, setArmed] = useState(false)
  const timerRef = useRef<number | null>(null)

  const disarm = () => {
    setArmed(false)
    if (timerRef.current) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }

  useEffect(() => {
    if (disabled && armed) disarm()
  }, [disabled, armed])

  useEffect(() => {
    if (!armed) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        disarm()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [armed])

  useEffect(() => {
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current)
    }
  }, [])

  const handleClick = () => {
    if (disabled) return
    if (!armed) {
      setArmed(true)
      timerRef.current = window.setTimeout(() => setArmed(false), timeoutMs)
      return
    }
    disarm()
    onConfirm()
  }

  const defaultConfirmCls =
    'border-hot bg-hot text-white hover:opacity-95 shadow-[var(--shadow-card)]'

  return (
    <button
      type="button"
      disabled={disabled}
      title={title}
      onClick={handleClick}
      onBlur={() => {
        if (armed) disarm()
      }}
      className={`pressable transition-[background-color,border-color,color,transform] duration-[150ms] ease-[var(--ease-out-quint)] ${
        armed ? confirmClassName || defaultConfirmCls : className
      }`}
    >
      {armed ? confirmLabel : label}
    </button>
  )
}
