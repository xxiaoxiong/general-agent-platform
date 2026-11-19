<template>
  <div class="page-container workflow-run">
    <PageHeader :title="workflow?.name || '运行工作流'" :description="workflow?.description || '填写输入参数，查看输出与节点执行详情。'">
      <template #eyebrow>
        <router-link to="/workflows" class="run-back"><ArrowLeft :size="14" /> 流程编排</router-link>
      </template>
      <template #actions>
        <UiButton v-if="workflow" @click="router.push(`/workflows/${workflowId}/editor`)"><SquarePen />编辑流程</UiButton>
      </template>
    </PageHeader>

    <div v-if="loading" class="run-state" role="status"><LoaderCircle class="run-spin" :size="25" /><strong>正在加载工作流</strong></div>
    <div v-else-if="loadError" class="run-state" role="alert">
      <CircleAlert :size="25" />
      <strong>工作流加载失败</strong>
      <el-button @click="loadWorkflow">重试</el-button>
    </div>

    <div v-else-if="workflow" class="run-layout">
      <aside class="run-controls">
        <section class="run-panel">
          <header class="run-panel-heading"><SlidersHorizontal :size="16" /><h2>运行参数</h2></header>
          <div class="run-panel-body">
            <fieldset :disabled="running" class="run-fields">
              <p v-if="!workflow.is_active" class="run-failure" role="status">此工作流未启用。</p>
              <WorkflowInputs v-model="formData" :fields="fields" />
            </fieldset>
            <UiButton variant="primary" class="run-button" :loading="running" :disabled="!workflow.is_active"
              @click="doRun"><Play v-if="!running" />运行工作流</UiButton>
          </div>
        </section>
        <section v-if="history.length" class="run-panel" aria-label="本次浏览的运行记录">
          <header class="run-panel-heading"><History :size="16" /><h2>本次运行记录</h2></header>
          <div class="run-history">
            <button v-for="item in history" :key="item.id" type="button" class="history-row" :aria-pressed="result?.id === item.id"
              :disabled="running" @click="viewExecution(item)">
              <component :is="item.status === 'success' ? CircleCheck : CircleAlert" :size="15" :class="item.status" />
              <span>{{ formatTime(item.created_at) }}</span>
              <small v-if="item.duration_ms != null">{{ item.duration_ms }} ms</small>
            </button>
          </div>
        </section>
      </aside>

      <section class="run-panel run-output" :aria-busy="running">
        <header class="run-panel-heading"><ListTree :size="16" /><h2>执行结果</h2></header>
        <div v-if="running" class="run-state" role="status">
          <LoaderCircle class="run-spin" :size="25" />
          <strong>正在执行工作流</strong>
          <span>执行结束后将在此显示输出。</span>
        </div>
        <div v-else-if="runError" class="run-state" role="alert">
          <CircleAlert :size="25" />
          <strong>运行请求失败</strong>
          <span>可以调整参数后再次运行。</span>
        </div>
        <div v-else-if="result" class="run-panel-body">
          <WorkflowResults :result="result" :node-labels="nodeLabels">
            <template #actions><el-button text size="small" @click="result = undefined">清空</el-button></template>
            <template #output-actions="{ content, isDocument }">
              <el-button :type="isDocument ? 'primary' : 'default'" :icon="Download" :loading="downloading" size="small"
                @click="downloadDoc(content)">下载 Word</el-button>
            </template>
          </WorkflowResults>
        </div>
        <div v-else class="run-state">
          <span class="empty-icon"><ListTree :size="24" /></span>
          <strong>等待运行</strong>
          <span>开始运行后，可查看输出、耗时和节点详情。</span>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import dayjs from 'dayjs'
import { ArrowLeft, CircleAlert, CircleCheck, Download, History, ListTree, LoaderCircle, Play, SlidersHorizontal, SquarePen } from '@lucide/vue'
import { workflowApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import UiButton from '@/components/ui/UiButton.vue'
import type { Workflow, WorkflowExecution } from '@/types'
import WorkflowInputs from './WorkflowInputs.vue'
import WorkflowResults from './WorkflowResults.vue'
import { inputFields, type WorkflowNode } from './workflowGraph'
import { getNodeType } from './workflowNodes'

const route = useRoute()
const router = useRouter()
const workflowId = computed(() => Number(route.params.id))
const workflow = ref<Workflow>()
const nodes = computed<WorkflowNode[]>(() => workflow.value?.definition?.nodes || [])
const fields = computed(() => inputFields(nodes.value))
const nodeLabels = computed(() => Object.fromEntries(nodes.value.map(node => [
  node.id, node.config?.label || node.label || getNodeType(node.type)?.label || node.id,
])))
const loading = ref(false)
const loadError = ref(false)
const running = ref(false)
const runError = ref(false)
const downloading = ref(false)
const formData = ref<Record<string, string>>({})
const result = ref<WorkflowExecution>()
const history = ref<WorkflowExecution[]>([])
let runSequence = 0

function formatTime(date: string) {
  return date && dayjs(date).isValid() ? dayjs(date).format('HH:mm:ss') : '—'
}

async function doRun() {
  if (running.value || !workflow.value?.is_active) return
  const sequence = ++runSequence
  running.value = true
  result.value = undefined
  runError.value = false
  try {
    const execution = await workflowApi.execute(workflowId.value, { input_data: formData.value })
    if (sequence !== runSequence) return
    result.value = execution
    history.value = [execution, ...history.value].slice(0, 10)
  } catch {
    if (sequence === runSequence) runError.value = true
  } finally {
    if (sequence === runSequence) running.value = false
  }
}

function viewExecution(execution: WorkflowExecution) {
  runError.value = false
  result.value = execution
}

async function downloadDoc(content: string) {
  if (!workflow.value || downloading.value) return
  const name = workflow.value.name || '工作流输出文档'
  downloading.value = true
  try {
    const file = await workflowApi.exportDoc(workflowId.value, { content, title: name, filename: workflow.value.name || 'output' })
    const url = URL.createObjectURL(file as unknown as Blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${name}.docx`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    ElMessage.success('Word 文档已生成')
  } catch {
    // 请求层提供失败反馈。
  } finally {
    downloading.value = false
  }
}

async function loadWorkflow() {
  const id = workflowId.value
  ++runSequence
  loading.value = true
  loadError.value = false
  running.value = false
  runError.value = false
  workflow.value = undefined
  result.value = undefined
  history.value = []
  try {
    const data = await workflowApi.get(id)
    if (id !== workflowId.value) return
    workflow.value = data
    formData.value = Object.fromEntries(fields.value.map(field => [field.key, '']))
  } catch {
    if (id === workflowId.value) loadError.value = true
  } finally {
    if (id === workflowId.value) loading.value = false
  }
}

watch(workflowId, loadWorkflow, { immediate: true })
</script>

<style scoped>
.run-layout { display: grid; grid-template-columns: minmax(220px, 290px) minmax(0, 1fr); align-items: start; gap: 16px; }
.run-back { display: inline-flex; gap: 6px; align-items: center; color: var(--ui-secondary); text-decoration: none; }
.run-back:hover { color: var(--ui-accent); }
.run-controls { display: grid; gap: 12px; min-width: 0; }
.run-panel { min-width: 0; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-lg); background: var(--ui-surface); overflow: hidden; }
.run-panel-heading { display: flex; align-items: center; gap: 8px; padding: 12px 14px; border-bottom: 1px solid var(--ui-border); color: var(--ui-secondary); }
.run-panel-heading h2 { margin: 0; color: var(--ui-text); font-size: 14px; font-weight: 650; }
.run-panel-body { padding: 14px; }
.run-fields { min-width: 0; margin: 0; padding: 0; border: 0; }
.run-fields:disabled { pointer-events: none; opacity: .7; }
.run-button { margin-top: 16px; width: 100%; }
.run-history { padding: 5px; }
.history-row {
  display: flex; align-items: center; gap: 8px; width: 100%;
  border: 0; background: transparent; color: var(--ui-secondary); border-radius: var(--ui-radius-sm);
  padding: 7px 9px; text-align: left; font: inherit; font-size: 13px; cursor: pointer;
}
.history-row:hover,.history-row[aria-pressed='true'] { background: var(--ui-hover); color: var(--ui-text); }
.history-row:disabled { cursor: default; opacity: .65; }
.history-row .success { color: var(--ui-success); }
.history-row .failed { color: var(--ui-danger); }
.history-row small { margin-left: auto; color: var(--ui-secondary); font-variant-numeric: tabular-nums; }
.run-state {
  display: grid; justify-items: center; align-content: center; gap: 9px; text-align: center;
  min-height: 285px; padding: 18px; color: var(--ui-secondary); font-size: 13px;
}
.run-state strong { color: var(--ui-text); font-size: 15px; }
.empty-icon {
  display: grid; place-items: center; width: 44px; height: 44px;
  color: var(--ui-accent); background: var(--ui-accent-soft); border-radius: var(--ui-radius-lg);
}
.run-spin { color: var(--ui-accent); animation: run-spin 1s linear infinite; }
@keyframes run-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .run-spin { animation: none; } }
.run-failure { background: var(--ui-danger-soft); color: var(--ui-danger); border-radius: var(--ui-radius-md); padding: 8px; margin: 0 0 12px; }
@media (max-width: 840px) { .run-layout { grid-template-columns: minmax(0, 1fr); } }
</style>
