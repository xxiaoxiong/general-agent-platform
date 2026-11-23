import { Marked } from 'marked'
import { hasVisibleText } from './blocks.ts'

function escapeHtml(value: string) {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

const renderer = new Marked({
  breaks: true,
  gfm: true,
  renderer: {
    paragraph(text: string) {
      // 模型在工具调用间有时会输出不可见占位行。保留正常换行和代码，只移除正文里的占位。
      const content = text.split('<br>').filter(line => !line.trim() || hasVisibleText(line)).join('<br>')
      return content.trim() ? '<p>' + content + '</p>\n' : ''
    },
    code(code: string, info?: string) {
      const language = escapeHtml(info?.trim().split(/\s+/, 1)[0] || '纯文本')
      return `<div class="code-block"><div class="code-toolbar"><span>${language}</span><button type="button" class="copy-code">复制</button></div><pre><code>${escapeHtml(code)}</code></pre></div>`
    },
    table(header: string, body: string) {
      return `<div class="table-scroll" tabindex="0" role="region" aria-label="表格内容"><table><thead>${header}</thead><tbody>${body}</tbody></table></div>`
    },
    link(href: string, title: string | null | undefined, text: string) {
      const label = title ? ` title="${escapeHtml(title)}"` : ''
      return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer"${label}>${text}</a>`
    },
  },
})

export function renderMessageMarkdown(text: string): string {
  return renderer.parse(text, { async: false }) as string
}
