<template>
  <section class="result-view" aria-label="执行结果">
    <header class="result-summary">
      <el-tag :type="result.status === 'success' ? 'success' : result.status === 'failed' ? 'danger' : 'info'">
        {{ statuses[result.status] }}
      </el-tag>
      <span v-if="result.duration_ms != null">{{ result.duration_ms.toLocaleString('zh-CN') }} ms</span>
      <span>{{ nodeEntries.length }} 个节点</span>
      <span v-if="errors" class="result-warning">{{ errors }} 个节点失败</span>
      <div class="summary-actions"><slot name="actions" /></div>
    </header>

    <p v-if="result.error_msg" role="alert" class="result-error">{{ result.error_msg }}</p>

    <article v-if="finalOutput" class="result-document">
      <header class="result-heading">
        <FileText :size="16" />
        <h3>{{ documentResult?.doc_title || '最终输出' }}</h3>
        <el-button size="small" text :icon="Copy" @click="copyOutput">复制</el-button>
        <slot name="output-actions" :content="finalOutput" :is-document="Boolean(documentResult)" />
      </header>
      <MessageMarkdown class="workflow-markdown" :text="finalOutput" />
    </article>

    <section v-if="nodeEntries.length" class="result-steps" aria-label="节点执行详情">
      <h3>节点执行详情</h3>
      <details v-for="[id, node] in nodeEntries" :key="id">
        <summary>
          <component :is="node.status === 'ok' ? CircleCheck : node.status === 'error' ? CircleAlert : CircleMinus"
            :class="['step-status', node.status]" :size="16" />
          <strong>{{ nodeLabels[id] || id }}</strong>
          <span v-if="node.condition_result !== undefined">分支 {{ node.condition_result ? 'True' : 'False' }}</span>
          <span v-if="Array.isArray(node.results) && typeof node.query === 'string'">{{ node.results.length }} 条检索结果</span>
          <span>{{ node.status === 'ok' ? '成功' : node.status === 'error' ? '失败' : node.status === 'skipped' ? '跳过' : node.status }}</span>
          <ChevronDown :size="14" class="step-toggle" />
        </summary>
        <p v-if="node.error" class="step-error">{{ node.error }}</p>
        <MessageMarkdown v-if="typeof node.content === 'string' && node.content" class="workflow-markdown" :text="node.content" />
        <ul v-else-if="Array.isArray(node.results) && typeof node.query === 'string' && node.results.length" class="search-matches">
          <li v-for="(match, index) in node.results" :key="index">
            <div><strong>{{ match.filename || '检索片段' }}</strong><span v-if="typeof match.score === 'number'">{{ (match.score * 100).toFixed(0) }}%</span></div>
            <p v-if="match.content">{{ match.content }}</p>
          </li>
        </ul>
        <details v-if="node.content || node.query" class="raw-details">
          <summary>原始输出 <ChevronDown :size="14" class="step-toggle" /></summary>
          <pre class="step-json">{{ JSON.stringify(node, null, 2) }}</pre>
        </details>
        <pre v-else class="step-json">{{ JSON.stringify(node, null, 2) }}</pre>
      </details>
    </section>
    <p v-else-if="!result.error_msg" class="result-message">本次执行没有返回节点结果。</p>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ChevronDown, CircleAlert, CircleCheck, CircleMinus, Copy, FileText } from '@lucide/vue'
import { ElMessage } from 'element-plus'
import MessageMarkdown from '@/features/run/MessageMarkdown.vue'
import type { WorkflowExecution } from '@/types'

const props = defineProps<{ result: WorkflowExecution; nodeLabels: Record<string, string> }>()
const statuses = { success: '执行完成', failed: '执行失败', running: '执行中', pending: '等待执行' }
const nodeEntries = computed(() => Object.entries(props.result.node_results || {}))
const errors = computed(() => nodeEntries.value.filter(([, node]) => node.status === 'error').length)
const documentResult = computed(() => nodeEntries.value.find(([, node]) => node.is_document && typeof node.content === 'string' && node.content)?.[1])
const finalOutput = computed<string>(() => {
  const outputNodes = nodeEntries.value.filter(([, node]) => typeof node.content === 'string' && node.content)
  return documentResult.value?.content || outputNodes[outputNodes.length - 1]?.[1].content || ''
})

async function copyOutput() {
  try {
    await navigator.clipboard.writeText(finalOutput.value)
    ElMessage.success('已复制输出')
  } catch {
    ElMessage.error('无法复制，请选中内容后手动复制')
  }
}
</script>

<style scoped>
.result-view { display: grid; gap: 16px; min-width: 0; }
.result-summary,.result-heading { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.result-summary { font-size: 12px; color: var(--ui-secondary); }
.summary-actions { margin-left: auto; }
.result-warning,.step-status.error,.step-error { color: var(--ui-danger); }
.result-error {
  margin: 0; background: var(--ui-danger-soft); color: var(--ui-danger); padding: 12px;
  border-radius: var(--ui-radius-md); overflow-wrap: anywhere;
}
.result-document {
  min-width: 0; border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-lg); background: var(--ui-surface); overflow: hidden;
}
.result-heading { padding: 12px 16px; border-bottom: 1px solid var(--ui-border); background: var(--ui-subtle); }
.result-heading h3 { flex: 1; margin: 0; min-width: 60px; font-size: 14px; }
.result-heading :deep(.el-button + .el-button) { margin-left: 0; }
.result-steps { min-width: 0; }
.result-steps h3 { margin: 0 0 8px; font-size: 14px; }
.result-steps details { background: var(--ui-surface); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); margin-bottom: 6px; overflow: hidden; }
.result-steps summary {
  display: flex; align-items: center; gap: 8px; padding: 10px 12px;
  list-style: none; cursor: pointer; font-size: 12px; color: var(--ui-secondary);
}
.result-steps summary::-webkit-details-marker { display: none; }
.result-steps summary:hover { background: var(--ui-hover); }
.result-steps details[open] summary { border-bottom: 1px solid var(--ui-border); }
.step-status,.step-toggle { flex-shrink: 0; }
.step-status.ok { color: var(--ui-success); }
.step-toggle { transition: transform var(--ui-duration) var(--ui-ease); }
details[open] > summary .step-toggle { transform: rotate(180deg); }
.result-steps strong { flex: 1; min-width: 0; color: var(--ui-text); overflow-wrap: anywhere; font-size: 13px; }
.step-error { margin: 8px 12px; overflow-wrap: anywhere; }
.step-json {
  margin: 8px 12px 12px; padding: 10px; max-height: 320px; overflow: auto; font: 12px/1.6 var(--ui-font-mono);
  background: var(--ui-subtle); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm);
}
.raw-details { margin: 8px 12px 12px; }
.raw-details summary { justify-content: space-between; }
.search-matches { display: grid; gap: 8px; list-style: none; margin: 0; padding: 12px; }
.search-matches li { border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); padding: 8px 10px; background: var(--ui-subtle); }
.search-matches li > div { display: flex; gap: 8px; align-items: baseline; font-size: 12px; color: var(--ui-secondary); }
.search-matches p { margin: 5px 0 0; max-height: 150px; overflow: auto; overflow-wrap: anywhere; white-space: pre-wrap; font-size: 13px; }
.result-message { margin: 0; color: var(--ui-secondary); font-size: 13px; }
.workflow-markdown { padding: 12px 16px; }
</style>
