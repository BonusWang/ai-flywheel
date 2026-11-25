import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'

// Ref-counted body scroll lock so stacked modals never clobber overflow
let activeScrollLocks = 0
let savedBodyOverflow = ''

function lockBodyScroll() {
  if (typeof document === 'undefined') return
  if (activeScrollLocks === 0) {
    savedBodyOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
  }
  activeScrollLocks += 1
}

function unlockBodyScroll() {
  if (typeof document === 'undefined') return
  activeScrollLocks = Math.max(0, activeScrollLocks - 1)
  if (activeScrollLocks === 0) {
    document.body.style.overflow = savedBodyOverflow
  }
}

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Unified accessible modal / bottom-sheet primitive:
 * - Portaled to `document.body` so `fixed` is always viewport-anchored.
 * - Cancels both nested rAF frames cleanly on rapid close.
 * - Stable `onCloseRef` avoids tearing down listeners on parent SSE re-renders.
 * - Traps Tab focus inside the dialog and restores previous focus on close.
 */
export const Modal: React.FC<{
  open: boolean
  onClose: () => void
  title: React.ReactNode
  subtitle?: React.ReactNode
  headerActions?: React.ReactNode
  maxWidthClass?: string
  children: React.ReactNode
}> = ({
  open,
  onClose,
  title,
  subtitle,
  headerActions,
  maxWidthClass = 'max-w-2xl',
  children,
}) => {
  const [mounted, setMounted] = useState(open)
  const [visible, setVisible] = useState(false)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const dialogRef = useRef<HTMLDivElement>(null)
  const prevFocusRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    let raf1 = 0
    let raf2 = 0
    let exitTimer: number | null = null

    if (open) {
      setMounted(true)
      raf1 = requestAnimationFrame(() => {
        raf2 = requestAnimationFrame(() => setVisible(true))
      })
    } else {
      setVisible(false)
      exitTimer = window.setTimeout(() => setMounted(false), 210)
    }

    return () => {
      if (raf1) cancelAnimationFrame(raf1)
      if (raf2) cancelAnimationFrame(raf2)
      if (exitTimer) window.clearTimeout(exitTimer)
    }
  }, [open])

  // Lock body scroll for the full mounted lifecycle (including exit animation)
  useEffect(() => {
    if (!mounted) return
    lockBodyScroll()
    return () => unlockBodyScroll()
  }, [mounted])

  // Keyboard Escape + Tab focus trap + focus restoration
  useEffect(() => {
    if (!open) return
    prevFocusRef.current = document.activeElement as HTMLElement | null

    const focusTimer = window.setTimeout(() => {
      const container = dialogRef.current
      if (!container) return
      if (!container.contains(document.activeElement)) {
        container.focus({ preventScroll: true })
      }
    }, 20)

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onCloseRef.current()
        return
      }
      if (e.key === 'Tab' && dialogRef.current) {
        const focusables = Array.from(
          dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)
        )
        if (focusables.length === 0) {
          e.preventDefault()
          return
        }
        const first = focusables[0]!
        const last = focusables[focusables.length - 1]!
        if (e.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.clearTimeout(focusTimer)
      window.removeEventListener('keydown', onKeyDown)
      prevFocusRef.current?.focus?.({ preventScroll: true })
    }
  }, [open])

  if (!mounted || typeof document === 'undefined') return null

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-4"
    >
      {/* Backdrop */}
      <div
        aria-hidden="true"
        onClick={() => onCloseRef.current()}
        className={`fixed inset-0 bg-ink/35 backdrop-blur-[2px] transition-opacity duration-[180ms] ease-[var(--ease-out-quint)] ${
          visible ? 'opacity-100' : 'opacity-0'
        }`}
      />

      {/* Dialog / Sheet Card */}
      <div
        ref={dialogRef}
        tabIndex={-1}
        className={`relative z-10 card w-full ${maxWidthClass} max-h-[88dvh] sm:max-h-[84vh] flex flex-col overflow-hidden rounded-b-none sm:rounded-b-[var(--radius-card)] shadow-[var(--shadow-sheet)] outline-none transition-[opacity,transform] duration-[220ms] ease-[var(--ease-out-quint)] ${
          visible
            ? 'opacity-100 translate-y-0 sm:scale-100'
            : 'opacity-0 translate-y-4 sm:translate-y-1.5 sm:scale-[0.97]'
        }`}
      >
        <div className="px-5 py-3.5 border-b border-line flex items-center justify-between gap-3 bg-surface-2 shrink-0">
          <div className="min-w-0">
            <div className="text-[14px] font-semibold text-ink truncate">{title}</div>
            {subtitle && <div className="text-[11.5px] text-ink-4 mt-0.5 num">{subtitle}</div>}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {headerActions}
            <button
              type="button"
              onClick={() => onCloseRef.current()}
              className="pressable-sm p-1.5 rounded-[var(--radius-control)] hover:bg-bg-sunk text-ink-3 hover:text-ink"
              aria-label="关闭"
              title="关闭 (Esc)"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
        <div className="p-5 overflow-y-auto overscroll-contain flex-1">{children}</div>
      </div>
    </div>,
    document.body
  )
}
