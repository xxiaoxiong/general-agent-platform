<template>
  <div class="run-timeline">
    <div class="timeline-header" @click="expanded = !expanded">
      <span class="timeline-title">任务进度</span>
      <span class="timeline-status" :class="statusClass">{{ statusText }}</span>
      <el-icon class="toggle"><component :is="expanded ? 'ArrowDown' : 'ArrowRight'" /></el-icon>
    </div>

    <div v-if="expanded" class="timeline-body">
      <div v-if="planItems.length" class="tl-section">
        <div class="tl-section-title">计划</div>
        <div v-for="(item, i) in planItems" :key="i" class="tl-plan-item">
          <span class="plan-mark">{{ planMark(item.status) }}</span>
          <span>{{ item.content }}</span>
        </div>
      </div>

      <div v-if="toolActivities.length" class="tl-section">
        <div class="tl-section-title">工具活动</div>
        <div v-for="act in toolActivities" :key="act.key" class="tl-tool">
          <span class="tool-state" :class="act.state">{{ toolStateMark(act.state) }}</span>
          <span class="tool-name">{{ act.name }}</span>
          <span class="tool-summary">{{ act.summary }}</span>
          <span v-if="act.durationText" class="tool-duration">{{ act.durationText }}</span>
        </div>
      </div>

      <div v-if="subagentActivities.length" class="tl-section">
        <div class="tl-section-title">子代理</div>
        <div v-for="act in subagentActivities" :key="act.key" class="tl-tool">
          <span class="tool-state" :class="act.state">{{ act.state === 'done' ? '✓' : '…' }}</span>
          <span class="tool-name">{{ act.name }}</span>
        </div>
      </div>

      <div v-if="errorText" class="tl-section">
        <div class="tl-section-title">错误</div>
        <div class="tl-error">{{ errorText }}</div>
      </div>

      <div class="tl-section tl-meta">
        <span v-if="traceId" class="tl-meta-item">trace: {{ traceId.slice(0, 8) }}</span>
        <span v-if="lastSeq > 0" class="tl-meta-item">seq: {{ lastSeq }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * Run Timeline（任务书 §8.3）：普通用户看计划/工具/状态/错误；
 * 不展示模型原始隐式推理文本（assistant.delta 只进最终回答区）。
 */
import { computed, ref } from 'vue'
import type { RunEventEnvelope } from '@/api/v2'

const props = defineProps<{
  events: RunEventEnvelope[]
  status: string
}>()

const expanded = ref(true)

const statusText = computed(() => {
  const map: Record<string, string> = {
    running: '执行中', completed: '已完成', failed: '失败', cancelled: '已取消',
    waiting_approval: '待审批', needs_reconciliation: '待核对', pending: '排队中',
    limit_exceeded: '超出限额', suspended: '已挂起',
  }
  return map[props.status] || props.status
})

const statusClass = computed(() => ({
  'is-running': props.status === 'running' || props.status === 'pending',
  'is-done': props.status === 'completed',
  'is-error': ['failed', 'limit_exceeded'].includes(props.status),
  'is-waiting': ['waiting_approval', 'needs_reconciliation'].includes(props.status),
}))

const planItems = computed(() => {
  const plan = [...props.events].reverse().find((e) => e.event === 'plan.updated')
  return (plan?.data?.todos as any[]) || []
})

interface ToolActivity {
  key: string
  name: string
  summary: string
  state: 'running' | 'done' | 'failed'
  durationText: string
  startedAt?: number
}

const toolActivities = computed<ToolActivity[]>(() => {
  const map = new Map<string, ToolActivity>()
  for (const ev of props.events) {
    if (ev.event === 'tool.started') {
      const key = ev.data?.tool_call_id || `${ev.data?.tool_name}-${ev.sequence}`
      map.set(key, {
        key,
        name: String(ev.data?.tool_name || '工具'),
        summary: String(ev.data?.summary || ''),
        state: 'running',
        durationText: '',
        startedAt: Date.parse(ev.timestamp) || undefined,
      })
    } else if (ev.event === 'tool.completed' || ev.event === 'tool.failed') {
      const key = ev.data?.tool_call_id || `${ev.data?.tool_name}-${ev.sequence}`
      const prev = map.get(key)
      const startedAt = prev?.startedAt
      const duration = startedAt ? Date.parse(ev.timestamp) - startedAt : 0
      map.set(key, {
        key,
        name: String(ev.data?.tool_name || prev?.name || '工具'),
        summary: String(ev.data?.summary || prev?.summary || ''),
        state: ev.event === 'tool.completed' ? 'done' : 'failed',
        durationText: duration > 0 ? `${(duration / 1000).toFixed(1)}s` : '',
        startedAt,
      })
    }
  }
  return [...map.values()]
})

const subagentActivities = computed(() => {
  const map = new Map<string, { key: string; name: string; state: 'running' | 'done' }>()
  for (const ev of props.events) {
    if (ev.event === 'subagent.started') {
      const name = String(ev.data?.name || '子代理')
      map.set(name, { key: name, name, state: 'running' })
    } else if (ev.event === 'subagent.completed') {
      const name = String(ev.data?.name || '子代理')
      map.set(name, { key: name, name, state: 'done' })
    }
  }
  return [...map.values()]
})

const errorText = computed(() => {
  const failed = [...props.events].reverse().find((e) => e.event === 'run.failed')
  return (failed?.data?.error as string) || ''
})

const traceId = computed(() => props.events[0]?.trace_id || '')
const lastSeq = computed(() => props.events[props.events.length - 1]?.sequence || 0)

function planMark(status: string) {
  return { completed: '[x]', in_progress: '[>]' }[status] || '[ ]'
}
function toolStateMark(state: string) {
  return { running: '…', done: '✓', failed: '✗' }[state] || '·'
}
</script>

<style scoped>
.run-timeline {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fafbfc;
  font-size: 13px;
}
.timeline-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
}
.timeline-title { font-weight: 600; color: #303133; }
.timeline-status { font-size: 12px; }
.timeline-status.is-running { color: #409eff; }
.timeline-status.is-done { color: #67c23a; }
.timeline-status.is-error { color: #f56c6c; }
.timeline-status.is-waiting { color: #e6a23c; }
.toggle { margin-left: auto; color: #909399; }
.timeline-body { padding: 0 12px 10px; }
.tl-section { margin-top: 8px; }
.tl-section-title { font-size: 12px; color: #909399; margin-bottom: 4px; }
.tl-plan-item { padding: 2px 0; color: #606266; }
.plan-mark { font-family: monospace; margin-right: 6px; }
.tl-tool {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  color: #606266;
}
.tool-state { width: 14px; text-align: center; }
.tool-state.running { color: #409eff; }
.tool-state.done { color: #67c23a; }
.tool-state.failed { color: #f56c6c; }
.tool-name { font-weight: 500; color: #303133; }
.tool-summary { color: #909399; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tool-duration { color: #b0b3b8; font-size: 12px; }
.tl-error { color: #f56c6c; white-space: pre-wrap; }
.tl-meta { display: flex; gap: 12px; color: #c0c4cc; font-size: 11px; }
</style>
