<template>
  <div class="response-markdown" v-html="html" @click="copyCode"></div>
</template>

<script lang="ts">
import { Marked } from 'marked'

function escapeHtml(value: string) {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

const renderer = new Marked({
  breaks: true,
  gfm: true,
  renderer: {
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
</script>

<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'

const props = defineProps<{ text: string }>()
const html = computed(() => DOMPurify.sanitize(renderer.parse(props.text) as string, { ADD_ATTR: ['target'] }))

async function copyCode(event: MouseEvent) {
  const button = (event.target as HTMLElement | null)?.closest<HTMLButtonElement>('.copy-code')
  if (!button) return
  try {
    await navigator.clipboard.writeText(button.closest('.code-block')?.querySelector('code')?.textContent || '')
    button.textContent = '已复制'
  } catch {
    button.textContent = '复制失败'
  }
  window.setTimeout(() => { button.textContent = '复制' }, 1400)
}
</script>

<style scoped>
.response-markdown { min-width: 0; color: var(--ui-text); line-height: 1.75; font-size: 14px; overflow-wrap: anywhere; }
.response-markdown:empty { display: none; }
.response-markdown :deep(p), .response-markdown :deep(ul), .response-markdown :deep(ol) { margin: 0 0 .72em; }
.response-markdown :deep(> :last-child) { margin-bottom: 0; }
.response-markdown :deep(ul), .response-markdown :deep(ol) { padding-inline-start: 1.5em; }
.response-markdown :deep(ul) { list-style: disc; }
.response-markdown :deep(ol) { list-style: decimal; }
.response-markdown :deep(li > p) { margin: 0; }
.response-markdown :deep(li) { margin: .35em 0; }
.response-markdown :deep(h1),
.response-markdown :deep(h2),
.response-markdown :deep(h3),
.response-markdown :deep(h4),
.response-markdown :deep(h5),
.response-markdown :deep(h6) { margin: .95em 0 .38em; color: var(--ui-text); font-weight: 650; line-height: 1.4; }
.response-markdown :deep(> :first-child) { margin-top: 0; }
.response-markdown :deep(h1) { font-size: 22px; }
.response-markdown :deep(h2) { font-size: 18px; }
.response-markdown :deep(h3) { font-size: 16px; }
.response-markdown :deep(h4), .response-markdown :deep(h5), .response-markdown :deep(h6) { font-size: 14px; }
.response-markdown :deep(code) { background: var(--ui-subtle); padding: 2px 5px; border-radius: var(--ui-radius-sm); font: .91em var(--ui-font-mono); }
.response-markdown :deep(.code-block), .response-markdown :deep(.table-scroll) {
  max-width: 100%; margin: 12px 0; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); overflow: auto;
}
.response-markdown :deep(.code-toolbar) {
  display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 9px 4px 12px;
  color: var(--ui-muted); background: var(--ui-subtle); border-bottom: 1px solid var(--ui-border); font-size: 12px;
}
.response-markdown :deep(.copy-code) {
  background: transparent; color: var(--ui-secondary); border: 0; border-radius: var(--ui-radius-sm); padding: 3px 8px; cursor: pointer;
}
.response-markdown :deep(.copy-code:hover) { background: var(--ui-hover); color: var(--ui-accent); }
.response-markdown :deep(pre) { overflow: auto; margin: 0; padding: 12px; background: var(--ui-bg); overflow-wrap: normal; }
.response-markdown :deep(pre code) { padding: 0; background: transparent; line-height: 1.6; }
.response-markdown :deep(blockquote) { padding: 4px 12px; margin: 12px 0; border-inline-start: 3px solid var(--ui-border-strong); color: var(--ui-secondary); }
.response-markdown :deep(blockquote p:last-child) { margin: 0; }
.response-markdown :deep(table) { width: 100%; min-width: max-content; border-collapse: collapse; font-size: 13px; }
.response-markdown :deep(th), .response-markdown :deep(td) { text-align: start; border-bottom: 1px solid var(--ui-border); padding: 7px 12px; }
.response-markdown :deep(th) { background: var(--ui-subtle); }
.response-markdown :deep(tr:last-child td) { border-bottom: 0; }
.response-markdown :deep(img) { max-width: 100%; height: auto; border-radius: var(--ui-radius-md); }
.response-markdown :deep(hr) { margin: 16px 0; border: 0; border-top: 1px solid var(--ui-border); }
.response-markdown :deep(a) { color: var(--ui-accent); text-decoration: underline; text-underline-offset: .14em; }
</style>
