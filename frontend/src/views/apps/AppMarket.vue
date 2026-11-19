<template>
  <div class="page-container">
    <PageHeader title="应用市场" description="部署 AI 应用模板，或直接运行已发布的工作流。">
      <template #actions>
        <el-button :icon="PanelsTopLeft" @click="router.push('/apps/instances')">我的应用</el-button>
      </template>
    </PageHeader>

    <section class="data-surface filters">
      <form class="page-toolbar" @submit.prevent="applySearch">
        <el-input v-model="keyword" :prefix-icon="Search" :placeholder="mainTab === 'apps' ? '搜索应用模板' : '搜索已发布工作流'"
          aria-label="在应用市场搜索" class="market-search" clearable @clear="applySearch" />
        <el-button native-type="submit">搜索</el-button>
        <el-select v-if="mainTab === 'apps'" v-model="activeCategory" class="category-select" clearable
          placeholder="所有分类" aria-label="应用分类" @change="applySearch">
          <el-option v-for="(label, key) in categoryLabels" :key="key" :label="label" :value="key" />
        </el-select>
        <span class="resource-meta item-count">{{ mainTab === 'apps' ? total : filteredWorkflows.length }} 项</span>
      </form>
    </section>

    <el-tabs v-model="mainTab">
      <el-tab-pane label="AI 应用" name="apps">
        <el-skeleton v-if="loading" :rows="5" animated />
        <EmptyState v-else-if="failed" title="应用市场加载失败" :icon="CircleAlert">
          <el-button @click="loadData">重新加载</el-button>
        </EmptyState>
        <EmptyState v-else-if="!templates.length" :title="searchApplied || activeCategory ? '没有匹配的应用' : '暂无应用模板'"
          :description="searchApplied || activeCategory ? '请更换关键词或清除分类筛选。' : undefined" :icon="Blocks" />
        <div v-else class="market-grid">
          <article v-for="app in templates" :key="app.id" class="data-surface market-card">
            <header>
              <div class="app-mark"><Blocks :size="18" /></div>
              <div class="min-w-0">
                <h2>{{ app.display_name }}</h2>
                <span class="resource-meta">{{ categoryLabels[app.category] || app.category }}</span>
              </div>
            </header>
            <p v-if="app.description" class="app-description">{{ app.description }}</p>
            <div v-if="app.tags" class="app-tags">
              <el-tag v-for="tag in splitTags(app.tags)" :key="tag" type="info" size="small" effect="plain">{{ tag }}</el-tag>
            </div>
            <footer>
              <span class="resource-meta">{{ app.usage_count }} 次使用</span>
              <el-button :icon="Rocket" size="small" @click="deployApp(app)">部署应用</el-button>
            </footer>
          </article>
        </div>
        <div v-if="total > pageSize" class="market-footer">
          <el-pagination v-model:current-page="page" :page-size="pageSize" :total="total"
            layout="prev, pager, next" @current-change="loadData" />
        </div>
      </el-tab-pane>
      <el-tab-pane label="工作流应用" name="workflows" lazy>
        <div class="workflow-note">
          <span class="resource-meta">已发布的工作流可直接运行。</span>
          <el-button text @click="router.push('/workflows')">管理工作流</el-button>
        </div>
        <el-skeleton v-if="workflowLoading" :rows="5" animated />
        <EmptyState v-else-if="workflowFailed" title="工作流应用加载失败" :icon="CircleAlert">
          <el-button @click="loadWorkflows">重新加载</el-button>
        </EmptyState>
        <EmptyState v-else-if="!filteredWorkflows.length" :title="searchApplied ? '没有匹配的工作流' : '暂无已发布的工作流'"
          :description="searchApplied ? '请尝试其他关键词。' : '工作流发布后会显示在这里。'" :icon="WorkflowIcon" />
        <div v-else class="market-grid">
          <article v-for="item in filteredWorkflows" :key="item.workflow.id" class="data-surface market-card">
            <header>
              <div class="app-mark"><WorkflowIcon :size="18" /></div>
              <div class="min-w-0">
                <h2>{{ item.workflow.name }}</h2>
                <span class="resource-meta">{{ item.workflow.category || '工作流' }} · v{{ item.workflow.version }}</span>
              </div>
            </header>
            <p v-if="item.workflow.description" class="app-description">{{ item.workflow.description }}</p>
            <div v-if="item.inputFields.length" class="app-tags">
              <span class="resource-meta">输入参数</span>
              <el-tag v-for="field in item.inputFields.slice(0, 3)" :key="field.key" type="info" size="small" effect="plain">
                {{ field.label || field.key }}
              </el-tag>
              <span v-if="item.inputFields.length > 3" class="resource-meta">+{{ item.inputFields.length - 3 }}</span>
            </div>
            <footer>
              <el-tag type="success" size="small">已发布</el-tag>
              <el-button :icon="Play" size="small" @click="router.push(`/workflows/${item.workflow.id}/run`)">运行</el-button>
            </footer>
          </article>
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="deployDialogVisible" :title="`部署应用 · ${deployingApp?.display_name ?? ''}`"
      width="min(520px, calc(100vw - 32px))">
      <el-form :model="deployForm" label-position="top">
        <el-form-item label="应用名称" required><el-input v-model="deployForm.name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="deployForm.description" type="textarea" :rows="2" /></el-form-item>
        <AppBindingFields v-model="deployForm" :models="models" :bases="bases" :workflows="bindingWorkflows"
          :loading="bindingLoading" :failed="bindingFailed" :allow-clear="true" @retry="loadBindings" />
        <el-form-item label="公开访问"><el-switch v-model="deployForm.is_public" active-text="公开" inactive-text="私有" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deployDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="deploying" @click="confirmDeploy">部署</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Blocks, CircleAlert, PanelsTopLeft, Play, Rocket, Search, Workflow as WorkflowIcon } from '@lucide/vue'
import { appApi, workflowApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { AppTemplate, Workflow } from '@/types'
import AppBindingFields from './AppBindingFields.vue'
import { useAppResources, type AppBindings } from './useAppResources'

interface DeployForm extends AppBindings {
  name: string
  description: string
  is_public: boolean
}

interface InputField {
  key: string
  label?: string
}

const router = useRouter()
const templates = ref<AppTemplate[]>([])
const loading = ref(true)
const failed = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 12
const keyword = ref('')
const searchApplied = ref('')
const activeCategory = ref('')
const mainTab = ref('apps')
const workflowLoading = ref(false)
const workflowFailed = ref(false)
const workflowsLoaded = ref(false)
const publishedWorkflows = ref<Workflow[]>([])
let requestId = 0

const categoryLabels: Record<string, string> = {
  legal: '法务合规', finance: '财税金融', hr: '人力资源',
  procurement: '采购供应链', marketing: '市场营销', service: '客户服务',
  office: '政务办公', compliance: '合规管理', tech: '研发技术',
}

const {
  models, bases, workflows,
  loading: bindingLoading, failed: bindingFailed, load: loadBindings,
} = useAppResources()
const bindingWorkflows = computed(() => workflows.value.filter(workflow => workflow.is_published))

const filteredWorkflows = computed(() => {
  const query = searchApplied.value.toLowerCase()
  return publishedWorkflows.value.filter(workflow =>
    !query || [workflow.name, workflow.description ?? ''].some(value => value.toLowerCase().includes(query)),
  ).map(workflow => {
    const nodes = workflow.definition?.nodes as Array<{ type: string; config?: { input_fields?: InputField[] } }> | undefined
    const inputFields = nodes?.find(node => node.type === 'start')?.config?.input_fields ?? []
    return { workflow, inputFields }
  })
})

function splitTags(value: string) {
  return [...new Set(value.split(',').map(tag => tag.trim()).filter(Boolean))]
}

function applySearch() {
  searchApplied.value = keyword.value.trim()
  if (mainTab.value === 'apps') {
    page.value = 1
    void loadData()
  }
}

watch(mainTab, tab => {
  if (tab === 'workflows') {
    if (!workflowsLoaded.value) void loadWorkflows()
  } else {
    void loadData()
  }
})

async function loadData() {
  const id = ++requestId
  loading.value = true
  failed.value = false
  try {
    const res = await appApi.listTemplates({
      page: page.value,
      page_size: pageSize,
      category: activeCategory.value || undefined,
      keyword: searchApplied.value || undefined,
    })
    if (id !== requestId) return
    templates.value = res.items
    total.value = res.total
  } catch {
    if (id === requestId) failed.value = true
  } finally {
    if (id === requestId) loading.value = false
  }
}

async function loadWorkflows() {
  if (workflowLoading.value) return
  workflowLoading.value = true
  workflowFailed.value = false
  try {
    const res = await workflowApi.list({ page_size: 100, is_published: true })
    publishedWorkflows.value = res.items.filter(workflow => workflow.is_published)
    workflowsLoaded.value = true
  } catch {
    workflowFailed.value = true
  } finally {
    workflowLoading.value = false
  }
}

const deployDialogVisible = ref(false)
const deployingApp = ref<AppTemplate | null>(null)
const deploying = ref(false)
const deployForm = ref<DeployForm>({
  name: '', description: '', model_config_id: null, knowledge_base_id: null, workflow_id: null, is_public: false,
})

function deployApp(app: AppTemplate) {
  deployingApp.value = app
  deployForm.value = {
    name: app.display_name, description: app.description || '',
    model_config_id: null, knowledge_base_id: null, workflow_id: null, is_public: false,
  }
  deployDialogVisible.value = true
  void loadBindings()
}

async function confirmDeploy() {
  if (!deployingApp.value || deploying.value) return
  if (!deployForm.value.name.trim()) return ElMessage.warning('请输入应用名称')
  deploying.value = true
  try {
    await appApi.createInstance({ template_id: deployingApp.value.id, ...deployForm.value })
    ElMessage.success('应用已部署')
    deployDialogVisible.value = false
    await router.push('/apps/instances')
  } catch {
    // 请求层已提示失败原因，保留表单方便重试。
  } finally {
    deploying.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.filters { margin-bottom: 16px; }
.market-search { width: min(100%, 260px); }
.category-select { width: 150px; }
.item-count { margin-inline-start: auto; }
.market-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr)); gap: 12px; }
.market-card { padding: 16px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.market-card header { display: flex; gap: 10px; align-items: center; }
.market-card h2 { font-size: 15px; font-weight: 650; overflow-wrap: anywhere; }
.app-mark { display: grid; place-items: center; flex: 0 0 36px; aspect-ratio: 1; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-subtle); color: var(--ui-secondary); }
.app-description { color: var(--ui-secondary); font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.app-tags { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
.market-card footer { border-top: 1px solid var(--ui-border); margin-top: auto; padding-top: 12px; display: flex; justify-content: space-between; gap: 8px; align-items: center; }
.market-footer { display: flex; justify-content: flex-end; padding: 12px 0; }
.workflow-note { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: -4px 0 12px; }
</style>
