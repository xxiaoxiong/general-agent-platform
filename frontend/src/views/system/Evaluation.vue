<template>
  <div class="page-container evaluation-page">
    <PageHeader title="Agent 测评" description="管理评测任务和数据集，回查执行轨迹与版本表现。">
      <template #actions>
        <UiButton :loading="refreshing" @click="refreshAll"><RefreshCw />刷新</UiButton>
        <UiButton variant="primary" @click="openCreateRun"><Plus />新建测评</UiButton>
      </template>
    </PageHeader>

    <el-alert v-if="sectionError && !refreshing" class="section-alert" title="部分数据加载失败" type="error" show-icon :closable="false">
      <UiButton size="sm" @click="loadSection">重新加载当前分区</UiButton>
    </el-alert>

    <el-tabs v-model="tab" class="evaluation-tabs" aria-label="评测与运行视图">
      <!-- ── 测评任务 / 测评结果（面板内含两个 tab-pane 与相关弹层） ── -->
      <EvalRunsPanel ref="runsPanel" v-model:tab="tab" v-model:loading="loading"
        :section-loading="sectionLoading" :section-error="sectionError" />
      <!-- ── 能力总览 ── -->
      <EvalCapabilityPanel ref="capabilityPanel" :section-loading="sectionLoading" />
      <!-- ── 数据集 ── -->
      <el-tab-pane label="数据集" name="datasets">
        <EvalDatasetsPanel ref="datasetsPanel" v-model:loading="loading" :section-loading="sectionLoading" />
      </el-tab-pane>
      <!-- ── 测评套件 ── -->
      <EvalSuitesPanel ref="suitesPanel" :section-loading="sectionLoading" />
      <!-- ── 运行评估（概览 + 评估记录） ── -->
      <el-tab-pane label="运行评估" name="overview">
        <EvolutionOverviewPanel ref="overviewPanel" v-model:loading="loading" v-model:agent-id="agentId"
          v-model:window-hours="windowHours" :section-loading="sectionLoading" @reload="loadSection" />
      </el-tab-pane>
      <!-- ── 开发简报（客观数据） ── -->
      <el-tab-pane label="开发简报" name="briefing">
        <EvolutionBriefingPanel v-model:loading="loading" v-model:agent-id="agentId" v-model:window-hours="windowHours" />
      </el-tab-pane>
      <!-- ── 优化建议（报告 + 变更审计） ── -->
      <el-tab-pane label="优化建议" name="reports">
        <EvolutionChangesPanel ref="changesPanel" v-model:loading="loading" v-model:agent-id="agentId"
          v-model:window-hours="windowHours" :section-loading="sectionLoading" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { Plus, RefreshCw } from '@lucide/vue'
import PageHeader from '@/components/layout/PageHeader.vue'
import UiButton from '@/components/ui/UiButton.vue'
import EvalRunsPanel from './evaluation/EvalRunsPanel.vue'
import EvalCapabilityPanel from './evaluation/EvalCapabilityPanel.vue'
import EvalDatasetsPanel from './evaluation/EvalDatasetsPanel.vue'
import EvalSuitesPanel from './evaluation/EvalSuitesPanel.vue'
import EvolutionOverviewPanel from './evolution/EvolutionOverviewPanel.vue'
import EvolutionBriefingPanel from './evolution/EvolutionBriefingPanel.vue'
import EvolutionChangesPanel from './evolution/EvolutionChangesPanel.vue'

// ── 公共状态（tab 切换 / 公共筛选 / 加载刷新），各面板数据自管 ──
const tab = ref('runs')
const loading = ref(true)
const refreshing = ref(false)
const sectionError = ref(false)
const sectionLoading = ref(false)
let sectionRequest = 0
const agentId = ref<number | undefined>(undefined)
const windowHours = ref(168)

const runsPanel = ref<InstanceType<typeof EvalRunsPanel> | null>(null)
const capabilityPanel = ref<InstanceType<typeof EvalCapabilityPanel> | null>(null)
const datasetsPanel = ref<InstanceType<typeof EvalDatasetsPanel> | null>(null)
const suitesPanel = ref<InstanceType<typeof EvalSuitesPanel> | null>(null)
const overviewPanel = ref<InstanceType<typeof EvolutionOverviewPanel> | null>(null)
const changesPanel = ref<InstanceType<typeof EvolutionChangesPanel> | null>(null)

// 「新建测评」入口委托给测评面板（数据集空校验与弹窗状态在面板内）
function openCreateRun() {
  void runsPanel.value?.openCreateRun()
}

// 分区懒加载：按当前 Tab 把加载分发到对应面板（results / briefing 不预加载）
async function loadSection() {
  const current = ++sectionRequest
  sectionError.value = false
  let requests: Promise<unknown>[]
  switch (tab.value) {
    case 'runs': requests = [runsPanel.value?.loadAll() ?? Promise.resolve()]; break
    case 'capability': requests = [capabilityPanel.value?.loadAll() ?? Promise.resolve()]; break
    case 'datasets': requests = [datasetsPanel.value?.loadAll() ?? Promise.resolve()]; break
    case 'suites': requests = [suitesPanel.value?.loadAll() ?? Promise.resolve()]; break
    case 'overview': requests = [overviewPanel.value?.loadAll() ?? Promise.resolve()]; break
    case 'reports': requests = [changesPanel.value?.loadAll() ?? Promise.resolve()]; break
    default:
      sectionLoading.value = false
      return
  }
  sectionLoading.value = true
  const results = await Promise.allSettled(requests)
  if (current === sectionRequest) {
    sectionError.value = results.some(result => result.status === 'rejected')
    sectionLoading.value = false
  }
}
watch(tab, loadSection)

async function refreshAll() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const results = await Promise.allSettled([
      runsPanel.value?.loadAll() ?? Promise.resolve(),
      capabilityPanel.value?.loadAll() ?? Promise.resolve(),
      datasetsPanel.value?.loadAll() ?? Promise.resolve(),
      suitesPanel.value?.loadAll() ?? Promise.resolve(),
      overviewPanel.value?.loadAll() ?? Promise.resolve(),
      changesPanel.value?.loadAll() ?? Promise.resolve(),
    ])
    sectionError.value = results.some(result => result.status === 'rejected')
  } finally {
    refreshing.value = false
  }
}

onMounted(async () => {
  await loadSection()
  loading.value = false
})
</script>

<style scoped>
.section-alert { margin-bottom: var(--ui-space-4); }
.evaluation-tabs :deep(.el-tabs__header) { margin-bottom: var(--ui-space-5); }
.evaluation-tabs :deep(.el-tabs__item) { padding-inline: 14px; }
.evaluation-tabs :deep(.el-card h4) { margin-bottom: var(--ui-space-3); }
.evaluation-tabs :deep(.el-tab-pane > .el-card + .el-card) { margin-top: var(--ui-space-3); }
</style>
