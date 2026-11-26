<template>
  <div class="rich-card mermaid-card">
    <header class="rich-toolbar">
      <span class="rich-label">Mermaid 图表</span>
      <div class="rich-actions">
        <button v-if="state === 'ready'" type="button" class="rich-btn" @click="view = view === 'chart' ? 'source' : 'chart'">
          {{ view === 'chart' ? '源码' : '图表' }}
        </button>
        <button type="button" class="rich-btn" @click="copySource">{{ copyLabel }}</button>
        <button v-if="state === 'ready' && view === 'chart'" type="button" class="rich-btn" @click="openFullscreen">放大</button>
      </div>
    </header>

    <div v-if="state === 'loading'" class="rich-state">
      <span class="rich-spinner" aria-hidden="true" /><span>图表渲染中…</span>
    </div>
    <div v-else-if="state === 'error'" class="rich-state is-error" role="alert">
      <CircleAlert :size="14" /><span>图表渲染失败：{{ error }}</span>
    </div>

    <div v-if="view === 'source' || state === 'error'" class="rich-source">
      <pre><code>{{ code }}</code></pre>
    </div>
    <div v-else-if="state === 'ready'" class="rich-canvas mermaid-canvas" v-html="svg"></div>

    <Teleport to="body">
      <div v-if="fullscreen" class="rich-modal" role="dialog" aria-modal="true" aria-label="Mermaid 图表放大查看" @click.self="closeFullscreen">
        <div class="rich-modal-card">
          <header class="rich-toolbar">
            <span class="rich-label">Mermaid 图表</span>
            <div class="rich-actions">
              <button type="button" class="rich-btn" title="缩小" @click="scale = Math.max(0.5, Math.round((scale - 0.2) * 10) / 10)">－</button>
              <span class="rich-zoom">{{ Math.round(scale * 100) }}%</span>
              <button type="button" class="rich-btn" title="放大" @click="scale = Math.min(3, Math.round((scale + 0.2) * 10) / 10)">＋</button>
              <button type="button" class="rich-btn" @click="scale = 1">重置</button>
              <button type="button" class="rich-btn" @click="closeFullscreen">关闭</button>
            </div>
          </header>
          <div class="rich-modal-canvas">
            <div class="rich-modal-svg" :style="zoomStyle" v-html="svg"></div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { CircleAlert } from '@lucide/vue'
import { renderMermaidSvg } from './mermaid'

const props = defineProps<{ code: string; theme?: 'light' | 'dark' }>()

const state = ref<'loading' | 'ready' | 'error'>('loading')
const svg = ref('')
const error = ref('')
const view = ref<'chart' | 'source'>('chart')
const copyLabel = ref('复制源码')
const fullscreen = ref(false)
const scale = ref(1)

let renderToken = 0
let copyTimer = 0

const zoomStyle = computed(() => ({ transform: `scale(${scale.value})`, transformOrigin: 'top center' }))

async function draw() {
  const token = ++renderToken
  state.value = 'loading'
  error.value = ''
  try {
    const output = await renderMermaidSvg(props.code, props.theme === 'dark' ? 'dark' : 'light')
    if (token !== renderToken) return
    svg.value = output
    state.value = 'ready'
  } catch (err) {
    if (token !== renderToken) return
    error.value = err instanceof Error ? err.message : String(err)
    state.value = 'error'
  }
}

watch(() => [props.code, props.theme], draw, { immediate: true })

async function copySource() {
  try {
    await navigator.clipboard.writeText(props.code)
    copyLabel.value = '已复制'
  } catch {
    copyLabel.value = '复制失败'
  }
  window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => { copyLabel.value = '复制源码' }, 1400)
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeFullscreen()
}

function openFullscreen() {
  scale.value = 1
  fullscreen.value = true
  document.addEventListener('keydown', onKeydown)
}

function closeFullscreen() {
  fullscreen.value = false
  document.removeEventListener('keydown', onKeydown)
}

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  window.clearTimeout(copyTimer)
})
</script>

<style scoped>
.rich-card { margin: 12px 0; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); overflow: hidden; background: var(--ui-bg); }
.rich-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 9px 4px 12px; background: var(--ui-subtle); border-bottom: 1px solid var(--ui-border); font-size: 12px; color: var(--ui-muted); }
.rich-label { display: inline-flex; align-items: center; gap: 6px; font-weight: 550; }
.rich-actions { display: inline-flex; align-items: center; gap: 2px; }
.rich-btn { background: transparent; color: var(--ui-secondary); border: 0; border-radius: var(--ui-radius-sm); padding: 3px 8px; font-size: 12px; cursor: pointer; }
.rich-btn:hover { background: var(--ui-hover); color: var(--ui-accent); }
.rich-state { display: flex; align-items: center; gap: 8px; padding: 14px 16px; color: var(--ui-muted); font-size: 13px; }
.rich-state.is-error { color: var(--ui-danger, #dc2626); }
.rich-spinner { width: 12px; height: 12px; border: 2px solid var(--ui-border-strong); border-top-color: var(--ui-accent); border-radius: 50%; animation: rich-spin .8s linear infinite; }
@keyframes rich-spin { to { transform: rotate(360deg); } }
.rich-source { max-height: 340px; overflow: auto; border-top: 1px solid var(--ui-border); }
.rich-source pre { margin: 0; padding: 12px; background: var(--ui-bg); overflow: auto; font: 12px/1.6 var(--ui-font-mono); }
.rich-source code { background: transparent; padding: 0; }
.rich-canvas { padding: 12px; overflow: auto; }
.mermaid-canvas :deep(svg) { display: block; max-width: 100%; height: auto; margin: 0 auto; }
.rich-modal { position: fixed; inset: 0; z-index: 3000; display: flex; align-items: center; justify-content: center; background: rgba(15, 23, 42, .55); backdrop-filter: blur(2px); }
.rich-modal-card { display: flex; flex-direction: column; width: 90vw; height: 86vh; background: var(--ui-bg); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); overflow: hidden; box-shadow: 0 24px 64px rgba(15, 23, 42, .35); }
.rich-modal-canvas { flex: 1; min-height: 0; overflow: auto; padding: 16px; }
.rich-modal-svg { display: flex; justify-content: center; transition: transform .12s ease-out; }
.rich-modal-svg :deep(svg) { max-width: none; }
.rich-zoom { min-width: 42px; text-align: center; color: var(--ui-secondary); font-variant-numeric: tabular-nums; }
@media (max-width: 768px) {
  .rich-modal-card { width: 96vw; height: 92vh; }
}
</style>
