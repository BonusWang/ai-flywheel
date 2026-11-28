import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { AlertCircle, CheckCircle2, Info, Loader2, RotateCcw, X } from 'lucide-react'
import { errorMessage } from '../../lib/errors'

export type ToastTone = 'ok' | 'error' | 'info' | 'warn'

export interface ToastOptions {
  message: string
  tone?: ToastTone
  durationMs?: number
  undoLabel?: string
  onUndo?: () => Promise<void> | void
}

interface ToastItem {
  id: number
  message: string
  tone: ToastTone
  durationMs: number
  undoLabel?: string
  onUndo?: () => Promise<void> | void
  closing: boolean
  undoing: boolean
}

interface ToastContextValue {
  push: (opts: ToastOptions | string) => number
  dismiss: (id: number) => void
}

const ToastContext = createContext<ToastContextValue>({
  push: () => 0,
  dismiss: () => {},
})

export function useToast(): ToastContextValue {
  return useContext(ToastContext)
}

const TONE_ICON: Record<ToastTone, React.ReactNode> = {
  ok: <CheckCircle2 className="w-4 h-4 text-ok shrink-0 animate-check-pop" />,
  error: <AlertCircle className="w-4 h-4 text-hot shrink-0" />,
  warn: <AlertCircle className="w-4 h-4 text-amber-ink shrink-0" />,
  info: <Info className="w-4 h-4 text-accent shrink-0" />,
}

const TONE_BORDER: Record<ToastTone, string> = {
  ok: 'border-ok/35',
  error: 'border-hot/40',
  warn: 'border-amber/40',
  info: 'border-line-strong',
}

export const ToastProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const idRef = useRef(1)
  const timersRef = useRef<Map<number, number>>(new Map())
  const exitTimersRef = useRef<Map<number, number>>(new Map())

  const clearItemTimers = useCallback((id: number) => {
    const t1 = timersRef.current.get(id)
    if (t1) {
      window.clearTimeout(t1)
      timersRef.current.delete(id)
    }
    const t2 = exitTimersRef.current.get(id)
    if (t2) {
      window.clearTimeout(t2)
      exitTimersRef.current.delete(id)
    }
  }, [])

  const removeNow = useCallback(
    (id: number) => {
      clearItemTimers(id)
      setToasts((prev) => prev.filter((t) => t.id !== id))
    },
    [clearItemTimers]
  )

  const dismiss = useCallback(
    (id: number) => {
      const existing = timersRef.current.get(id)
      if (existing) {
        window.clearTimeout(existing)
        timersRef.current.delete(id)
      }
      setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, closing: true } : t)))
      if (!exitTimersRef.current.has(id)) {
        const exitTimer = window.setTimeout(() => removeNow(id), 180)
        exitTimersRef.current.set(id, exitTimer)
      }
    },
    [removeNow]
  )

  const scheduleDismiss = useCallback(
    (id: number, ms: number) => {
      if (ms <= 0) return
      const existing = timersRef.current.get(id)
      if (existing) window.clearTimeout(existing)
      const timer = window.setTimeout(() => dismiss(id), ms)
      timersRef.current.set(id, timer)
    },
    [dismiss]
  )

  const pauseDismiss = useCallback((id: number) => {
    const existing = timersRef.current.get(id)
    if (existing) {
      window.clearTimeout(existing)
      timersRef.current.delete(id)
    }
  }, [])

  const push = useCallback(
    (input: ToastOptions | string) => {
      const opts: ToastOptions = typeof input === 'string' ? { message: input } : input
      const id = idRef.current++
      const tone: ToastTone = opts.tone ?? 'ok'
      const defaultDur = tone === 'error' ? 7000 : opts.onUndo ? 5500 : 3600
      const durationMs = opts.durationMs ?? defaultDur

      const next: ToastItem = {
        id,
        message: opts.message,
        tone,
        durationMs,
        undoLabel: opts.undoLabel || '撤销',
        onUndo: opts.onUndo,
        closing: false,
        undoing: false,
      }
      setToasts((prev) => {
        if (prev.length >= 4) {
          for (const evicted of prev.slice(0, prev.length - 3)) {
            clearItemTimers(evicted.id)
          }
        }
        return [...prev.slice(-3), next]
      })
      scheduleDismiss(id, durationMs)
      return id
    },
    [clearItemTimers, scheduleDismiss]
  )

  const handleUndo = useCallback(
    async (item: ToastItem) => {
      if (!item.onUndo || item.undoing) return
      pauseDismiss(item.id)
      setToasts((prev) => prev.map((t) => (t.id === item.id ? { ...t, undoing: true } : t)))
      try {
        await item.onUndo()
        dismiss(item.id)
      } catch (e: unknown) {
        dismiss(item.id)
        const msg = errorMessage(e, '')
        push({
          tone: 'error',
          message: msg ? `撤销失败：${msg}` : '撤销失败，请稍后重试',
        })
      }
    },
    [dismiss, pauseDismiss, push]
  )

  useEffect(() => {
    const timers = timersRef.current
    const exitTimers = exitTimersRef.current
    return () => {
      timers.forEach((timer) => window.clearTimeout(timer))
      timers.clear()
      exitTimers.forEach((timer) => window.clearTimeout(timer))
      exitTimers.clear()
    }
  }, [])

  const ctxValue = useMemo(() => ({ push, dismiss }), [push, dismiss])

  return (
    <ToastContext.Provider value={ctxValue}>
      {children}
      {/* Floating non-blocking Toast Viewport (top-right on desktop, below header on mobile — zero overlap with BackToTop or Bulk Island) */}
      <div
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed inset-x-3 top-[64px] z-50 flex flex-col items-center gap-2 sm:inset-x-auto sm:top-5 sm:right-5 sm:items-end"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            role={t.tone === 'error' ? 'alert' : 'status'}
            onMouseEnter={() => pauseDismiss(t.id)}
            onMouseLeave={() => {
              if (!t.closing && !t.undoing) scheduleDismiss(t.id, Math.min(t.durationMs, 2600))
            }}
            className={`pointer-events-auto flex items-center gap-2.5 max-w-[440px] w-full sm:w-auto rounded-[var(--radius-card)] border ${TONE_BORDER[t.tone]} bg-surface/95 backdrop-blur-md px-3.5 py-2.5 text-[12.5px] text-ink shadow-[var(--shadow-soft)] transition-[opacity,transform] duration-[200ms] ease-[var(--ease-out-quint)] ${
              t.closing
                ? 'opacity-0 -translate-y-2 scale-[0.97]'
                : 'animate-fade-up opacity-100 translate-y-0 scale-100'
            }`}
          >
            {TONE_ICON[t.tone]}
            <span className="min-w-0 flex-1 leading-snug">{t.message}</span>
            {t.onUndo && (
              <button
                type="button"
                disabled={t.undoing}
                onClick={() => handleUndo(t)}
                className="pressable inline-flex items-center gap-1 h-6 px-2 rounded-[var(--radius-mark)] bg-accent-soft text-accent-ink font-semibold text-[11.5px] hover:bg-accent/18 shrink-0 disabled:opacity-60"
              >
                {t.undoing ? (
                  <Loader2 className="w-3 h-3 animate-spin" />
                ) : (
                  <RotateCcw className="w-3 h-3" />
                )}
                {t.undoLabel}
              </button>
            )}
            <button
              type="button"
              onClick={() => dismiss(t.id)}
              aria-label="关闭提示"
              className="pressable-sm p-1 -mr-1 rounded-[var(--radius-mark)] text-ink-4 hover:text-ink hover:bg-bg-sunk shrink-0"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
