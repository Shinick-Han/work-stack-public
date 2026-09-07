import type { ReactNode } from 'react'

const ASCII_PUNCT = '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~'

export function decodeInertText(value: string): string {
  const parts: string[] = []
  let index = 0
  while (index < value.length) {
    if (value.startsWith('&amp;', index)) {
      parts.push('&')
      index += 5
      continue
    }
    if (value.startsWith('&lt;', index)) {
      parts.push('<')
      index += 4
      continue
    }
    if (value.startsWith('&gt;', index)) {
      parts.push('>')
      index += 4
      continue
    }
    const next = value[index + 1]
    if (value[index] === '\\' && next !== undefined && ASCII_PUNCT.includes(next)) {
      parts.push(next)
      index += 2
      continue
    }
    parts.push(value[index])
    index += 1
  }
  return parts.join('')
}

type DailyBlock =
  | { type: 'h1' | 'h2' | 'h3' | 'p'; text: string }
  | { type: 'ul'; items: string[] }

export function parseDailyV1Markdown(markdown: string): DailyBlock[] {
  const blocks: DailyBlock[] = []
  let items: string[] | null = null
  const flush = () => {
    if (items) {
      blocks.push({ type: 'ul', items })
      items = null
    }
  }
  for (const line of markdown.replace(/\r\n/g, '\n').split('\n')) {
    if (line.startsWith('### ')) {
      flush()
      blocks.push({ type: 'h3', text: line.slice(4) })
      continue
    }
    if (line.startsWith('## ')) {
      flush()
      blocks.push({ type: 'h2', text: line.slice(3) })
      continue
    }
    if (line.startsWith('# ')) {
      flush()
      blocks.push({ type: 'h1', text: line.slice(2) })
      continue
    }
    if (line.startsWith('- ')) {
      items ??= []
      items.push(line.slice(2))
      continue
    }
    flush()
    if (line !== '') blocks.push({ type: 'p', text: line })
  }
  flush()
  return blocks
}

function textNode(value: string): ReactNode {
  return decodeInertText(value)
}

export function DailyReportDocument({ markdown }: { markdown: string }) {
  const blocks = parseDailyV1Markdown(markdown)
  return (
    <div className="daily-report-document">
      {blocks.map((block, index) => {
        if (block.type === 'ul') {
          return (
            <ul key={index}>
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{textNode(item)}</li>
              ))}
            </ul>
          )
        }
        if (block.type === 'h1') return <h1 key={index}>{textNode(block.text)}</h1>
        if (block.type === 'h2') return <h2 key={index}>{textNode(block.text)}</h2>
        if (block.type === 'h3') return <h3 key={index}>{textNode(block.text)}</h3>
        return <p key={index}>{textNode(block.text)}</p>
      })}
    </div>
  )
}
