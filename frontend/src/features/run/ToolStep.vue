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
      <span class="tool-name">{{ tool.name }}</span>
      <span class="tool-target" v-if="tool.target">{{ tool.target }}</span>
      <span v-if="durationText" class="tool-duration">{{ durationText }}</span>
      <el-icon class="row-chevron"><component :is="open ? 'ArrowDown' : 'ArrowRight'" /></el-icon>
    </div>

    <!-- 详情：入参 / 出参 / 错误（小字，不干扰主流程阅读） -->
    <div v-if="open" class="tool-detail">
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
 * 点击行可展开入参 / 结果 / 错误详情。
 */
import { computed, ref } from 'vue'
import type { ToolBlock } from './blocks'

const props = defineProps<{ tool: ToolBlock }>()

const open = ref(false)

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
.tool-name { font-weight: 500; color: #303133; flex-shrink: 0; font-family: 'Consolas', monospace; }
.tool-step.is-running .tool-name { color: #409eff; }
.tool-step.is-failed .tool-name { color: #f56c6c; }
.tool-target {
  color: #909399; font-family: 'Consolas', monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1;
}
.tool-duration { color: #b0b3b8; font-size: 11px; flex-shrink: 0; }
.row-chevron { color: #c0c4cc; font-size: 11px; flex-shrink: 0; }

.tool-detail { padding: 2px 6px 8px 28px; }
.detail-block { margin-top: 4px; }
.detail-label { font-size: 11px; color: #909399; margin-bottom: 2px; }
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
