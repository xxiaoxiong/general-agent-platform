<template>
  <div ref="root" class="response-markdown" v-html="html" @click="copyCode"></div>
</template>

<script setup lang="ts">
import { computed, createApp, h, nextTick, onBeforeUnmount, onMounted, ref, watch, type App } from 'vue'
import DOMPurify from 'dompurify'
import { renderMessageDocument } from './messageMarkdown'
import MermaidBlock from './rich-content/MermaidBlock.vue'
import ChartBlock from './rich-content/ChartBlock.vue'
import { useTheme } from '@/lib/theme'

const props = defineProps<{ text: string; streaming?: boolean }>()

const { theme } = useTheme()
const root = ref<HTMLElement | null>(null)
const rendered = computed(() => renderMessageDocument(props.text, { streaming: Boolean(props.streaming) }))
const html = computed(() => DOMPurify.sanitize(rendered.value.html, { ADD_ATTR: ['target'] }))

/** 已挂载的富内容实例：消息更新 / 卸载时逐一销毁，避免 ECharts、ResizeObserver 泄漏 */
let mountedApps: Array<{ app: App }> = []

function disposeRichContent() {
  for (const item of mountedApps) {
    try { item.app.unmount() } catch { /* 宿主节点可能已被 v-html 替换，忽略卸载异常 */ }
  }
  mountedApps = []
}

async function mountRichContent() {
  if (!rendered.value.embeds.length) return
  await nextTick()
  const host = root.value
  if (!host) return
  for (const embed of rendered.value.embeds) {
    // 只挂载仍存在于当前 DOM 中的占位节点（净化或更新可能移除占位）
    const target = host.querySelector<HTMLElement>(`[data-embed-index="${embed.index}"]`)
    if (!target) continue
    const app = createApp({
      render: () => embed.kind === 'mermaid'
        ? h(MermaidBlock, { code: embed.source, theme: theme.value })
        : h(ChartBlock, { code: embed.source, theme: theme.value }),
    })
    app.mount(target)
    mountedApps.push({ app })
  }
}

async function refreshRichContent() {
  disposeRichContent()
  await mountRichContent()
}

watch(() => [props.text, props.streaming], () => { void refreshRichContent() }, { flush: 'post' })
onMounted(() => { void mountRichContent() })
onBeforeUnmount(disposeRichContent)

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
/* 大表格：表头吸顶 + 限高滚动，所有行仍保留（不静默丢行） */
.response-markdown :deep(.table-scroll--large) { max-height: 520px; }
.response-markdown :deep(.table-scroll--large th) { position: sticky; top: 0; z-index: 1; }
.response-markdown :deep(img) { max-width: 100%; height: auto; border-radius: var(--ui-radius-md); }
.response-markdown :deep(hr) { margin: 16px 0; border: 0; border-top: 1px solid var(--ui-border); }
.response-markdown :deep(a) { color: var(--ui-accent); text-decoration: underline; text-underline-offset: .14em; }
/* 流式阶段的富内容占位：不展示半成品源码，也不空白 */
.response-markdown :deep(.rich-pending) {
  display: flex; align-items: center; gap: 8px; margin: 12px 0; padding: 14px 16px;
  border: 1px dashed var(--ui-border-strong); border-radius: var(--ui-radius-md); color: var(--ui-muted); font-size: 13px;
}
.response-markdown :deep(.rich-pending-dot) {
  width: 8px; height: 8px; border-radius: 50%; background: var(--ui-accent); animation: rich-pulse 1s ease-in-out infinite;
}
@keyframes rich-pulse { 0%, 100% { opacity: .25; transform: scale(.8); } 50% { opacity: 1; transform: scale(1); } }
</style>
