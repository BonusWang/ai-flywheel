import React, { useState } from 'react'
import { Check, Copy } from 'lucide-react'

export const CopyButton: React.FC<{ text: string; label?: string; className?: string }> = ({
  text,
  label = '复制',
  className = '',
}) => {
  const [copied, setCopied] = useState(false)
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
        setTimeout(() => setCopied(false), 1500)
      }}
      className={`inline-flex h-7 items-center gap-1 rounded-[var(--radius-mark)] border border-line bg-surface px-2 text-[12px] transition-colors ${
        copied ? 'text-ok' : 'text-ink-3 hover:border-line-strong hover:text-ink'
      } ${className}`}
    >
      {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
      {copied ? '已复制' : label}
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
