<template>
  <el-form-item label="绑定模型">
    <el-select v-model="model.model_config_id" placeholder="选择模型（可选）" filterable :clearable="allowClear" class="w-full" :loading="loading">
      <el-option v-for="item in models" :key="item.id" :label="item.display_name" :value="item.id" />
    </el-select>
  </el-form-item>
  <el-form-item label="绑定知识库">
    <el-select v-model="model.knowledge_base_id" placeholder="选择知识库（可选）" filterable :clearable="allowClear" class="w-full" :loading="loading">
      <el-option v-for="item in bases" :key="item.id" :label="item.name" :value="item.id" />
    </el-select>
  </el-form-item>
  <el-form-item label="绑定工作流">
    <el-select v-model="model.workflow_id" placeholder="选择工作流（可选）" filterable :clearable="allowClear" class="w-full" :loading="loading">
      <el-option v-for="item in workflows" :key="item.id" :label="item.name" :value="item.id" />
    </el-select>
    <div v-if="model.workflow_id" class="resource-meta">绑定后，可从我的应用直接运行工作流。</div>
  </el-form-item>
  <el-alert v-if="failed" title="部分绑定选项加载失败" type="warning" :closable="false">
    <el-button size="small" text @click="emit('retry')">重新加载</el-button>
  </el-alert>
</template>

<script setup lang="ts" generic="T extends AppBindings">
import type { KnowledgeBase, ModelConfig, Workflow } from '@/types'
import type { AppBindings } from './useAppResources'

defineProps<{
  models: ModelConfig[]
  bases: KnowledgeBase[]
  workflows: Workflow[]
  loading: boolean
  failed: boolean
  allowClear: boolean
}>()
const model = defineModel<T>({ required: true })
const emit = defineEmits<{ retry: [] }>()
</script>
