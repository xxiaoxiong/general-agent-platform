<template>
  <div
    class="tool-step"
    :class="{ 'is-running': tool.state === 'running', 'is-failed': tool.state === 'failed', 'is-open': open }"
  >
    <div class="tool-line">
      <button type="button" class="tool-toggle" :aria-expanded="open" @click="open = !open">
        <span class="tool-state" :class="tool.state">
          <LoaderCircle v-if="tool.state === 'running'" class="mini-spin" :size="14" />
          <CircleCheck v-else-if="tool.state === 'done'" :size="14" />
          <CircleX v-else :size="14" />
        </span>
        <span class="tool-name">{{ toolLabel }}</span>
        <span v-if="tool.target && !tool.filePath" class="tool-target" :title="tool.target">{{ tool.target }}</span>
        <ChevronDown class="row-chevron" :class="{ expanded: open }" :size="14" />
      </button>
      <button
        v-if="tool.filePath"
        type="button"
        class="tool-file"
        :title="tool.filePath"
        @click="$emit('preview', tool.filePath!, fileLabel)"
      >{{ fileLabel }}</button>
      <!-- 编辑 diff 增删统计（Trae 式 +N -N） -->
      <span v-if="diffStat" class="tool-diff">
        <template v-if="diffStat.add != null"><span class="diff-add">+{{ diffStat.add }}</span></template>
        <template v-if="diffStat.del"><span class="diff-del"> -{{ diffStat.del }}</span></template>
      </span>
      <span v-if="durationText" class="tool-duration">{{ durationText }}</span>
    </div>

    <!-- 详情：工具名 / 入参 / 出参 / 错误（小字，不干扰主流程阅读） -->
    <div v-if="open" class="tool-detail">
      <div class="detail-block">
        <div class="detail-label">工具</div>
        <div class="detail-tool-name">{{ tool.name }}</div>
      </div>
      <div v-if="inputText" class="detail-block">
        <div class="detail-label">入参</div>
        <pre class="detail-pre">{{ inputText }}</pre>
      </div>
      <div v-if="tool.error" class="detail-block">
        <div class="detail-label detail-label-err">错误</div>
        <pre class="detail-pre detail-pre-err">{{ tool.error }}</pre>
      </div>
      <div v-else-if="outputText" class="detail-block">
        <div class="detail-label">结果</div>
        <pre class="detail-pre">{{ outputText }}</pre>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 单个工具执行步骤行（穿插在对话流中的小字实时步骤）：
 * 点击行可展开入参 / 结果 / 错误详情；文件类工具的文件名可点（右侧预览）。
 */
import { computed, ref } from 'vue'
import { ChevronDown, CircleCheck, CircleX, LoaderCircle } from '@lucide/vue'
import type { ToolBlock } from './blocks'

const props = defineProps<{ tool: ToolBlock }>()
const emit = defineEmits<{ preview: [path: string, name: string] }>()

const open = ref(false)

/** 文件名（basename），行内展示 + 供预览面板标题 */
const fileLabel = computed(() => {
  const p = props.tool.filePath || ''
  return p.replace(/\\/g, '/').split('/').filter(Boolean).pop() || p
})

/**
 * Trae 式动词标签：把裸工具名映射为直观的中文动作（已完成 / 执行中两态），
 * 如 read_file -> 「已读取」/「正在读取」，execute -> 「命令已执行」/「命令执行中」。
 */
const TOOL_VERBS: Record<string, [string, string]> = {
  read_file: ['已读取', '正在读取'],
  write_file: ['已写入', '正在写入'],
  edit_file: ['已编辑', '正在编辑'],
  ls: ['已列出', '正在列出'],
  glob: ['已搜索', '正在搜索'],
  grep: ['已搜索', '正在搜索'],
  execute: ['命令已执行', '命令执行中'],
  python_exec: ['代码已执行', '代码执行中'],
  delete_file: ['已删除', '正在删除'],
  delete: ['已删除', '正在删除'],
  web_search: ['已搜索网络', '正在搜索网络'],
  http_request: ['请求已发送', '请求发送中'],
}
const toolLabel = computed(() => {
  if (props.tool.state === 'failed') return '工具执行失败'
  const verbs = TOOL_VERBS[props.tool.name] || ['已完成', '正在执行']
  return props.tool.state === 'running' ? verbs[1] : verbs[0]
})

function toDetailText(value: any, limit = 600): string {
  if (value == null) return ''
  let text: string
  if (typeof value === 'string') text = value
  else {
    try { text = JSON.stringify(value, null, 2) } catch { text = String(value) }
  }
  text = text.trim()
  return text.length > limit ? text.slice(0, limit) + `\n…（共 ${text.length} 字符）` : text
}

const inputText = computed(() => toDetailText(props.tool.input))
const outputText = computed(() => toDetailText(props.tool.output))
const durationText = computed(() => {
  const ms = props.tool.durationMs || 0
  return ms > 0 ? `${(ms / 1000).toFixed(1)}s` : ''
})

/**
 * 编辑 diff 增删统计（Trae 式 +N -N）：从工具入参按行数推导。
 * edit_file 用 old/new 行数；write_file 用写入内容行数（+N）。拿不到则不展示。
 */
const diffStat = computed(() => {
  const inp: any = props.tool.input
  if (!inp || typeof inp !== 'object') return null
  const lines = (s: any) => (typeof s === 'string' && s ? s.split('\n').length : 0)
  if (props.tool.name === 'edit_file') {
    const oldS = inp.old_string ?? inp.old ?? inp.old_str
    const newS = inp.new_string ?? inp.new ?? inp.new_str
    if (typeof oldS === 'string' || typeof newS === 'string') {
      return { add: lines(newS), del: lines(oldS) }
    }
    return null
  }
  if (props.tool.name === 'write_file') {
    const content = inp.content ?? inp.text
    if (typeof content === 'string' && content) return { add: lines(content), del: 0 }
    return null
  }
  return null
})
</script>

<style scoped>
.tool-step { min-width: 0; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-surface); }
.tool-step + .tool-step { margin-top: 4px; }
.tool-line { display: flex; align-items: center; gap: 7px; padding: 3px 7px; color: var(--ui-secondary); min-width: 0; font-size: 12px; }
.tool-toggle {
  display: inline-flex; align-items: center; gap: 6px; min-width: 0;
  border: 0; border-radius: var(--ui-radius-sm); background: transparent; color: var(--ui-secondary); padding: 4px;
  cursor: pointer; text-align: start; line-height: 1.35;
}
.tool-toggle:hover { color: var(--ui-text); background: var(--ui-hover); }
.tool-state { display: inline-flex; flex: none; }
.tool-state.running { color: var(--ui-info); }
.tool-state.done { color: var(--ui-success); }
.tool-state.failed { color: var(--ui-danger); }
.mini-spin { animation: tools-spin 1s linear infinite; }
@keyframes tools-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .mini-spin { animation: none; } }
.tool-name { color: var(--ui-text); font-weight: 550; }
.tool-step.is-running .tool-name { color: var(--ui-info); }
.tool-step.is-failed .tool-name { color: var(--ui-danger); }
.tool-target, .tool-file {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
  font-family: var(--ui-font-mono);
}
.tool-target { color: var(--ui-muted); }
.tool-file {
  max-width: 44%; padding: 2px 5px; border: 0; border-radius: var(--ui-radius-sm);
  background: var(--ui-accent-soft); color: var(--ui-accent); cursor: pointer;
}
.tool-file:hover { text-decoration: underline; }
.tool-diff, .tool-duration { flex: none; font-family: var(--ui-font-mono); font-size: 12px; }
.diff-add { color: var(--ui-success); }
.diff-del { color: var(--ui-danger); }
.tool-duration { color: var(--ui-muted); margin-inline-start: auto; }
.row-chevron { color: var(--ui-muted); flex: none; transform: rotate(-90deg); }
.row-chevron.expanded { transform: none; }
.tool-detail { padding: 0 10px 10px; }
.detail-block { margin-top: 6px; }
.detail-label { font-size: 12px; color: var(--ui-muted); margin-bottom: 3px; }
.detail-tool-name, .detail-pre {
  background: var(--ui-bg); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm);
  padding: 6px 8px; color: var(--ui-secondary); font-family: var(--ui-font-mono); font-size: 12px;
}
.detail-pre { margin: 0; line-height: 1.5; max-height: 180px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.detail-label-err, .detail-pre-err { color: var(--ui-danger); }
.detail-pre-err { background: var(--ui-danger-soft); }
</style>
