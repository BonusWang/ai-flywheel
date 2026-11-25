import React, { useEffect, useRef, useState } from 'react'
import { Check, Copy } from 'lucide-react'

export const CopyButton: React.FC<{ text: string; label?: string; className?: string }> = ({
  text,
  label = '复制',
  className = '',
}) => {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current) window.clearTimeout(timerRef.current)
    }
  }, [])

  return (
    <button
      type="button"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
        } catch {
          const ta = document.createElement('textarea')
          ta.value = text
          document.body.appendChild(ta)
          ta.select()
          document.execCommand('copy')
          ta.remove()
        }
        setCopied(true)
        if (timerRef.current) window.clearTimeout(timerRef.current)
        timerRef.current = window.setTimeout(() => setCopied(false), 1600)
      }}
      className={`pressable inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] border px-2 text-[12px] ${
        copied
          ? 'border-ok/35 bg-ok-soft text-ok-ink'
          : 'border-line bg-surface text-ink-3 hover:border-line-strong hover:text-ink'
      } ${className}`}
    >
      <span className="relative flex size-3.5 items-center justify-center">
        {copied ? (
          <Check key="check" className="w-3.5 h-3.5 animate-check-pop" />
        ) : (
          <Copy key="copy" className="w-3.5 h-3.5" />
        )}
      </span>
      <span>{copied ? '已复制' : label}</span>
    </button>
  )
}

export const CodeBlock: React.FC<{ code: string; title?: string; lang?: string }> = ({
  code,
  title,
  lang,
}) => {
  return (
    <div className="my-3 overflow-hidden rounded-[var(--radius-card)] border border-line bg-surface">
      <div className="flex items-center justify-between border-b border-line-soft px-3.5 py-2">
        <span className="text-[12px] text-ink-4">{title ?? lang ?? ''}</span>
        <CopyButton text={code} />
      </div>
      <pre className="mono overflow-x-auto bg-bg-sunk/70 px-4 py-3.5 text-[12.5px] leading-[1.7] text-ink-2">
        <code>{code}</code>
      </pre>
    </div>
  )
}
