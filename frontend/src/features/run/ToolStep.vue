<template>
  <div
    class="tool-step"
    :class="{ 'is-running': tool.state === 'running', 'is-failed': tool.state === 'failed', 'is-open': open }"
  >
    <div class="tool-line" @click="open = !open">
      <span class="tool-state" :class="tool.state">
        <span v-if="tool.state === 'running'" class="mini-spin"></span>
        <template v-else>{{ tool.state === 'done' ? '✓' : '✗' }}</template>
      </span>
      <!-- Trae 式动词标签：已读取 / 已编辑 / 命令已执行 …（替代裸工具名，更直观） -->
      <span class="tool-name">{{ toolLabel }}</span>
      <!-- 文件类工具：文件名可点，点击在右侧临时区块预览文件内容 -->
      <a
        v-if="tool.filePath"
        class="tool-file"
        :title="tool.filePath"
        @click.stop="$emit('preview', tool.filePath!, fileLabel)"
      >{{ fileLabel }}</a>
      <span v-else-if="tool.target" class="tool-target">{{ tool.target }}</span>
      <!-- 编辑 diff 增删统计（Trae 式 +N -N） -->
      <span v-if="diffStat" class="tool-diff">
        <template v-if="diffStat.add != null"><span class="diff-add">+{{ diffStat.add }}</span></template>
        <template v-if="diffStat.del"><span class="diff-del"> -{{ diffStat.del }}</span></template>
      </span>
      <span v-if="durationText" class="tool-duration">{{ durationText }}</span>
      <el-icon class="row-chevron"><component :is="open ? 'ArrowDown' : 'ArrowRight'" /></el-icon>
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
.tool-step { border-radius: 6px; margin-bottom: 2px; }
.tool-line {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 6px;
  color: #606266;
  cursor: pointer;
  font-size: 12px;
}
.tool-line:hover { background: #eef1f5; }
.tool-step.is-open { background: #eef1f5; }
.tool-state { width: 14px; text-align: center; flex-shrink: 0; line-height: 18px; font-size: 12px; }
.tool-state.running { color: #409eff; display: inline-flex; justify-content: center; }
.tool-state.done { color: #67c23a; }
.tool-state.failed { color: #f56c6c; }
.mini-spin {
  width: 8px; height: 8px; border-radius: 50%;
  background: #409eff; animation: pulse 1.2s infinite ease-in-out;
}
@keyframes pulse { 0%, 100% { opacity: 0.35; transform: scale(0.8); } 50% { opacity: 1; transform: scale(1); } }
.tool-name { font-weight: 500; color: #303133; flex-shrink: 0; }
.tool-step.is-running .tool-name { color: #409eff; }
.tool-step.is-failed .tool-name { color: #f56c6c; }
.tool-target {
  color: #909399; font-family: 'Consolas', monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1;
}
/* 文件名链接：点击打开右侧预览 */
.tool-file {
  color: #409eff; font-family: 'Consolas', monospace; cursor: pointer;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1;
  text-decoration: none;
}
.tool-file:hover { text-decoration: underline; }
.tool-diff { flex-shrink: 0; font-family: 'Consolas', monospace; font-size: 11px; }
.diff-add { color: #67c23a; }
.diff-del { color: #f56c6c; }
.tool-duration { color: #b0b3b8; font-size: 11px; flex-shrink: 0; }
.row-chevron { color: #c0c4cc; font-size: 11px; flex-shrink: 0; }

.tool-detail { padding: 2px 6px 8px 28px; }
.detail-block { margin-top: 4px; }
.detail-label { font-size: 11px; color: #909399; margin-bottom: 2px; }
.detail-tool-name {
  font-family: 'Consolas', monospace; font-size: 11px; color: #5a6270;
  background: #fff; border: 1px solid #e4e7ed; border-radius: 6px; padding: 4px 8px;
}
.detail-label-err { color: #f56c6c; }
.detail-pre {
  margin: 0;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 6px;
  padding: 6px 8px;
  font-size: 11px;
  line-height: 1.5;
  color: #5a6270;
  max-height: 160px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: 'Consolas', monospace;
}
.detail-pre-err { background: #fff5f5; border-color: #fbc4c4; color: #c45656; }
</style>
