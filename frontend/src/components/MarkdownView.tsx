import React from 'react'

function renderInline(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = []
  const regex = /(`[^`]+`|\*\*[^*]+\*\*)/g
  let lastIndex = 0
  let match: RegExpExecArray | null
  let k = 0

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index))
    }
    const token = match[0]
    if (token.startsWith('`') && token.endsWith('`')) {
      parts.push(
        <code
          key={k++}
          className="mono rounded-[var(--radius-mark)] bg-bg-sunk px-1.5 py-0.5 text-[0.88em] text-ink border border-line-soft"
        >
          {token.slice(1, -1)}
        </code>
      )
    } else if (token.startsWith('**') && token.endsWith('**')) {
      parts.push(
        <strong key={k++} className="font-semibold text-ink">
          {token.slice(2, -2)}
        </strong>
      )
    }
    lastIndex = regex.lastIndex
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex))
  }
  return parts
}

export const MarkdownView: React.FC<{ content: string; className?: string }> = ({
  content,
  className = '',
}) => {
  // Strip leading YAML frontmatter (`---\n...\n---`) so notes read cleanly without raw metadata walls
  const cleaned = content.replace(/\r\n/g, '\n').replace(/^---\n[\s\S]*?\n---\n*/, '')
  const lines = cleaned.split('\n')
  const blocks: React.ReactNode[] = []
  let i = 0
  let key = 0

  while (i < lines.length) {
    const line = lines[i]!
    const trimmed = line.trim()

    if (!trimmed) {
      i++
      continue
    }

    // Fenced code block
    if (trimmed.startsWith('```')) {
      const codeLines: string[] = []
      i++
      while (i < lines.length && !lines[i]!.trim().startsWith('```')) {
        codeLines.push(lines[i]!)
        i++
      }
      if (i < lines.length) i++
      blocks.push(
        <pre
          key={key++}
          className="mono overflow-x-auto rounded-[var(--radius-control)] border border-line-soft bg-bg-sunk px-3.5 py-3 text-[12px] leading-[1.65] text-ink-2"
        >
          <code>{codeLines.join('\n')}</code>
        </pre>
      )
      continue
    }

    // Horizontal rule
    if (/^(-{3,}|\*{3,})$/.test(trimmed)) {
      blocks.push(<hr key={key++} className="border-line-soft my-2" />)
      i++
      continue
    }

    // Headings
    const headingMatch = /^(#{1,4})\s+(.+)$/.exec(trimmed)
    if (headingMatch) {
      const level = headingMatch[1]!.length
      const text = headingMatch[2]!
      if (level === 1) {
        blocks.push(
          <h2 key={key++} className="text-[16px] font-bold text-ink pt-1">
            {renderInline(text)}
          </h2>
        )
      } else if (level === 2) {
        blocks.push(
          <h3 key={key++} className="text-[14px] font-semibold text-ink pt-2 border-t border-line-soft first:border-t-0 first:pt-0">
            {renderInline(text)}
          </h3>
        )
      } else {
        blocks.push(
          <h4 key={key++} className="text-[13px] font-semibold text-ink-2 pt-1">
            {renderInline(text)}
          </h4>
        )
      }
      i++
      continue
    }

    // Blockquote
    if (trimmed.startsWith('>')) {
      const quoteLines: string[] = []
      while (i < lines.length && lines[i]!.trim().startsWith('>')) {
        quoteLines.push(lines[i]!.trim().replace(/^>\s?/, ''))
        i++
      }
      blocks.push(
        <blockquote
          key={key++}
          className="border-l-2 border-accent/60 bg-bg-sunk/60 pl-3.5 pr-3 py-2 text-[13px] text-ink-3 rounded-r-[var(--radius-mark)]"
        >
          {quoteLines.map((ql, idx) => (
            <p key={idx} className={idx > 0 ? 'mt-1' : ''}>
              {renderInline(ql)}
            </p>
          ))}
        </blockquote>
      )
      continue
    }

    // Unordered list
    if (/^[-*+]\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && /^[-*+]\s+/.test(lines[i]!.trim())) {
        items.push(lines[i]!.trim().replace(/^[-*+]\s+/, ''))
        i++
      }
      blocks.push(
        <ul key={key++} className="space-y-1.5 pl-4 list-disc text-[13px] leading-[1.7] text-ink-2">
          {items.map((it, idx) => (
            <li key={idx}>{renderInline(it)}</li>
          ))}
        </ul>
      )
      continue
    }

    // Ordered list
    if (/^\d+\.\s+/.test(trimmed)) {
      const items: string[] = []
      while (i < lines.length && /^\d+\.\s+/.test(lines[i]!.trim())) {
        items.push(lines[i]!.trim().replace(/^\d+\.\s+/, ''))
        i++
      }
      blocks.push(
        <ol key={key++} className="space-y-1.5 pl-4 list-decimal text-[13px] leading-[1.7] text-ink-2">
          {items.map((it, idx) => (
            <li key={idx}>{renderInline(it)}</li>
          ))}
        </ol>
      )
      continue
    }

    // Paragraph (group consecutive plain lines)
    const para: string[] = []
    while (
      i < lines.length &&
      lines[i]!.trim() &&
      !/^(#{1,4}\s+|[-*+]\s+|\d+\.\s+|>|```|---|\*\*\*)/.test(lines[i]!.trim())
    ) {
      para.push(lines[i]!.trim())
      i++
    }
    blocks.push(
      <p key={key++} className="text-[13px] leading-[1.75] text-ink-2">
        {renderInline(para.join(' '))}
      </p>
    )
  }

  return <div className={`space-y-2.5 ${className}`}>{blocks}</div>
}
