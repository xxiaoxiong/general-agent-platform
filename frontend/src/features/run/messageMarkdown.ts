import { Marked } from 'marked'
import { hasVisibleText } from './blocks.ts'
import { detectRichKind } from './rich-content/registry.ts'
import type { RichEmbed, RichKind } from './rich-content/types.ts'

export interface RenderMessageOptions {
  /** 流式输出中：富内容显示"生成中"占位，不渲染未完成图表 */
  streaming?: boolean
  /** 关闭富内容识别（仅渲染普通 Markdown / 代码块），用于纯文本场景与回归测试 */
  allowRich?: boolean
}

export interface RenderedMessage {
  html: string
  /** 待挂载的富内容块；源码只保存在内存中，不写入 HTML，避免 XSS 与属性膨胀 */
  embeds: RichEmbed[]
}

function escapeHtml(value: string) {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

function codeBlockHtml(code: string, info?: string) {
  const language = escapeHtml(info?.trim().split(/\s+/, 1)[0] || '纯文本')
  return `<div class="code-block"><div class="code-toolbar"><span>${language}</span><button type="button" class="copy-code">复制</button></div><pre><code>${escapeHtml(code)}</code></pre></div>`
}

function richFenceHtml(kind: RichKind, index: number, streaming: boolean) {
  if (streaming) {
    // 流式阶段不渲染半成品图表，也不把未完成的源码当普通代码块展示
    return `<div class="rich-block rich-pending" data-rich-kind="${kind}"><span class="rich-pending-dot"></span><span>图表生成中…</span></div>`
  }
  return `<div class="rich-block rich-embed" data-embed-index="${index}" data-rich-kind="${kind}"></div>`
}

/** 大表格（>40 行）限制阅读高度并提供表头吸顶，避免整页滚动失控；不丢弃任何行 */
function tableHtml(header: string, body: string) {
  const rows = (body.match(/<tr[\s>]/g) || []).length
  const large = rows > 40 ? ' table-scroll--large' : ''
  return `<div class="table-scroll${large}" tabindex="0" role="region" aria-label="表格内容"><table><thead>${header}</thead><tbody>${body}</tbody></table></div>`
}

export function renderMessageDocument(text: string, options: RenderMessageOptions = {}): RenderedMessage {
  const embeds: RichEmbed[] = []
  const allowRich = options.allowRich !== false
  const streaming = Boolean(options.streaming)

  const renderer = new Marked({
    breaks: true,
    gfm: true,
    renderer: {
      paragraph(content: string) {
        // 模型在工具调用间有时会输出不可见占位行。保留正常换行和代码，只移除正文里的占位。
        const cleaned = content.split('<br>').filter(line => !line.trim() || hasVisibleText(line)).join('<br>')
        return cleaned.trim() ? '<p>' + cleaned + '</p>\n' : ''
      },
      code(code: string, info?: string) {
        const kind = allowRich ? detectRichKind(info) : null
        if (!kind) return codeBlockHtml(code, info)
        const index = embeds.length
        if (!streaming) embeds.push({ index, kind, source: code })
        return richFenceHtml(kind, index, streaming)
      },
      table(header: string, body: string) {
        return tableHtml(header, body)
      },
      link(href: string, title: string | null | undefined, text: string) {
        const label = title ? ` title="${escapeHtml(title)}"` : ''
        return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer"${label}>${text}</a>`
      },
    },
  })

  const html = renderer.parse(text, { async: false }) as string
  return { html, embeds }
}

/** 兼容旧调用：只渲染 Markdown 基础内容（不识别富内容围栏） */
export function renderMessageMarkdown(text: string): string {
  return renderMessageDocument(text, { allowRich: false }).html
}
