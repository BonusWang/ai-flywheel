import React from 'react'

/**
 * Unified tactile Apple-style toggle switch.
 * Replaces raw browser checkboxes for on/off settings across Automations,
 * ConnectPolicyPage, and PolicyStudioTab.
 */
export const Switch: React.FC<{
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  ariaLabel?: string
  size?: 'sm' | 'md'
}> = ({ checked, onChange, disabled = false, ariaLabel, size = 'md' }) => {
  const isSm = size === 'sm'
  const track = isSm ? 'h-5 w-9' : 'h-6 w-11'
  const thumb = isSm ? 'size-3.5' : 'size-4.5'
  const shift = isSm
    ? checked
      ? 'translate-x-4.5'
      : 'translate-x-0.5'
    : checked
      ? 'translate-x-5.5'
      : 'translate-x-0.5'

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`group relative inline-flex ${track} shrink-0 items-center rounded-full border transition-colors duration-[180ms] ease-[var(--ease-out-quint)] ${
        checked
          ? 'border-accent bg-accent'
          : 'border-line-strong bg-bg-muted hover:border-ink-4/50'
      } disabled:opacity-55`}
    >
      <span
        className={`inline-block ${thumb} rounded-full bg-white shadow-[0_1px_2px_rgba(0,0,0,0.18)] transition-transform duration-[200ms] ease-[var(--ease-out-back-subtle)] group-active:scale-x-110 ${shift}`}
      />
    </button>
  )
}
