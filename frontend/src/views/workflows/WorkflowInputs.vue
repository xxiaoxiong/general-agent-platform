<template>
  <el-form v-if="fields.length" label-position="top" class="workflow-inputs">
    <el-form-item v-for="(field, index) in fields" :key="field.key + '-' + index" :label="field.label || field.key">
      <el-select v-if="field.type === 'select'" :model-value="modelValue[field.key]" :placeholder="field.placeholder || '请选择'"
        @update:model-value="update(field.key, $event)">
        <el-option v-for="option in options(field.options)" :key="option" :label="option" :value="option" />
      </el-select>
      <el-input v-else :model-value="modelValue[field.key]" :type="field.type === 'textarea' ? 'textarea' : 'text'"
        :rows="4" :placeholder="field.placeholder || '请输入' + (field.label || field.key)"
        @update:model-value="update(field.key, $event)" />
    </el-form-item>
  </el-form>
  <p v-else class="no-inputs">该流程无需输入参数。</p>
</template>

<script setup lang="ts">
import type { WorkflowInputField } from './workflowGraph'

const props = defineProps<{ fields: WorkflowInputField[]; modelValue: Record<string, string> }>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, string>] }>()

function update(key: string, value: string) {
  emit('update:modelValue', { ...props.modelValue, [key]: value })
}

function options(value?: string) {
  return [...new Set((value || '').split(',').map(option => option.trim()))]
}
</script>

<style scoped>
.workflow-inputs :deep(.el-select) { width: 100%; }
.no-inputs { color: var(--ui-secondary); font-size: 13px; margin: 0; }
</style>
