<template>
  <div class="approval-panel">
    <div class="approval-title">⏸ 等待人工审批</div>
    <div v-for="(action, i) in actions" :key="i" class="approval-action">
      <div class="action-tool">{{ action.tool || action.tool_name || '操作' }}</div>
      <div v-if="action.summary" class="action-summary">{{ action.summary }}</div>
      <div v-if="action.action_id" class="action-id">action_id: {{ action.action_id }}</div>
    </div>
    <div class="approval-btns">
      <el-button type="primary" size="small" :loading="deciding" @click="decide('approve')">
        批准执行
      </el-button>
      <el-button type="danger" size="small" plain :loading="deciding" @click="decide('reject')">
        拒绝
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * ApprovalPanel（§8.4 8）：显示动作和参数摘要，按 action_id 审批。
 * 决策必须携带稳定 action_id（FR-06）。
 */
import { ref } from 'vue'

const props = defineProps<{
  runId: string
  actions: any[]
}>()

const emit = defineEmits<{
  (e: 'decided', type: 'approve' | 'reject'): void
}>()

const deciding = ref(false)

async function decide(type: 'approve' | 'reject') {
  if (deciding.value) return
  deciding.value = true
  try {
    const { runApi } = await import('@/api/v2')
    await runApi.resume(
      props.runId,
      props.actions.map((a) => ({ type, action_id: a.action_id || null })),
    )
    emit('decided', type)
  } finally {
    deciding.value = false
  }
}
</script>

<style scoped>
.approval-panel {
  border: 1px solid #e6a23c;
  background: #fdf6ec;
  border-radius: 8px;
  padding: 12px;
  margin: 8px 0;
}
.approval-title { font-weight: 600; color: #e6a23c; margin-bottom: 8px; }
.approval-action {
  background: #fff;
  border-radius: 6px;
  padding: 8px;
  margin-bottom: 6px;
  font-size: 13px;
}
.action-tool { font-weight: 500; color: #303133; }
.action-summary { color: #606266; margin-top: 2px; white-space: pre-wrap; }
.action-id { color: #c0c4cc; font-size: 11px; margin-top: 2px; font-family: monospace; }
.approval-btns { margin-top: 8px; }
</style>
