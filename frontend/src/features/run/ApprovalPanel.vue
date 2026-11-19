<template>
  <section class="approval-panel" aria-label="审批请求">
    <h3 class="approval-title"><ShieldAlert :size="16" />等待人工审批</h3>
    <div v-for="(action, i) in actions" :key="action.action_id || i" class="approval-action">
      <div class="action-tool">{{ action.tool || action.tool_name || '操作' }}</div>
      <div v-if="action.summary" class="action-summary">{{ action.summary }}</div>
    </div>
    <div class="approval-btns">
      <UiButton variant="primary" size="sm" :loading="deciding === 'approve'" :disabled="!!deciding" @click="$emit('decide', 'approve')">
        批准执行
      </UiButton>
      <UiButton variant="danger" size="sm" :loading="deciding === 'reject'" :disabled="!!deciding" @click="$emit('decide', 'reject')">
        拒绝
      </UiButton>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ShieldAlert } from '@lucide/vue'
import UiButton from '@/components/ui/UiButton.vue'

defineProps<{
  actions: any[]
  deciding?: 'approve' | 'reject'
}>()

defineEmits<{
  decide: [type: 'approve' | 'reject']
}>()
</script>

<style scoped>
.approval-panel {
  border: var(--ui-line);
  border-inline-start: 3px solid var(--ui-warning);
  background: var(--ui-warning-soft);
  border-radius: var(--ui-radius-md);
  padding: 12px;
}
.approval-title { display: flex; align-items: center; gap: 7px; font-size: 14px; font-weight: 600; color: var(--ui-warning); margin-bottom: 8px; }
.approval-action {
  background: var(--ui-surface);
  border-radius: var(--ui-radius-sm);
  padding: 8px;
  margin-bottom: 6px;
  font-size: 13px;
}
.action-tool { font-weight: 600; color: var(--ui-text); }
.action-summary { color: var(--ui-secondary); margin-top: 2px; white-space: pre-wrap; overflow-wrap: anywhere; }
.approval-btns { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
</style>
