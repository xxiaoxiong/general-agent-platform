<template>
  <div class="page-container workflow-list">
    <PageHeader title="流程编排" description="使用节点搭建自动化工作流，调试后运行或发布为应用。">
      <template #actions>
        <UiButton variant="primary" :loading="creating" @click="createWorkflow"><Plus v-if="!creating" />新建流程</UiButton>
      </template>
    </PageHeader>

    <section class="data-surface workflow-table" aria-label="工作流列表" :aria-busy="loading">
      <div class="page-toolbar workflow-filters">
        <div class="filter-controls">
          <el-input v-model="keyword" class="workflow-search" clearable :prefix-icon="Search" placeholder="搜索流程名称"
            aria-label="搜索流程名称" @input="searchWorkflows" />
          <el-select v-model="filterCategory" class="workflow-categories" placeholder="全部分类" aria-label="按分类筛选"
            clearable @change="applyFilters">
            <el-option v-for="(label, value) in categories" :key="value" :label="label" :value="value" />
          </el-select>
        </div>
        <span v-if="!loadError" class="resource-count" aria-live="polite">{{ loading ? '加载中' : `共 ${total} 个工作流` }}</span>
      </div>

      <ErrorState v-if="loadError" title="读取工作流失败" description="请检查网络连接或访问权限后重试。" @retry="loadData" />
      <div v-else class="workflow-table-body">
        <el-table :data="workflows" v-loading="loading" row-key="id">
          <el-table-column label="流程名称" min-width="245">
            <template #default="{ row }">
              <router-link :to="`/workflows/${row.id}/editor`" class="workflow-name">
                <span class="workflow-type-icon"><WorkflowIcon :size="17" /></span>
                <span class="workflow-name-text">
                  <strong :title="row.name">{{ row.name }}</strong>
                  <small v-if="row.description" :title="row.description">{{ row.description }}</small>
                </span>
              </router-link>
            </template>
          </el-table-column>
          <el-table-column label="分类" width="112">
            <template #default="{ row }">{{ categories[row.category] || row.category || '—' }}</template>
          </el-table-column>
          <el-table-column label="触发方式" width="105">
            <template #default="{ row }">{{ triggerLabels[row.trigger_type] || row.trigger_type || '—' }}</template>
          </el-table-column>
          <el-table-column label="版本" width="76">
            <template #default="{ row }"><span class="workflow-version">v{{ row.version }}</span></template>
          </el-table-column>
          <el-table-column label="状态" width="105">
            <template #default="{ row }">
              <span class="workflow-status" :class="{ published: row.is_published }">
                <span />{{ row.is_published ? '已发布' : '草稿' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="154">
            <template #default="{ row }"><time :datetime="row.updated_at" class="workflow-date">{{ formatDate(row.updated_at) }}</time></template>
          </el-table-column>
          <el-table-column label="操作" width="196" align="right">
            <template #default="{ row }">
              <el-button text size="small" :icon="SquarePen" @click="router.push(`/workflows/${row.id}/editor`)">编辑</el-button>
              <el-button text size="small" :icon="Play" @click="router.push(`/workflows/${row.id}/run`)">运行</el-button>
              <el-button text size="small" type="danger" :icon="Trash2" :loading="deletingId === row.id"
                :disabled="deletingId !== null" :aria-label="`删除 ${row.name}`" title="删除"
                @click="deleteWorkflow(row)" />
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState v-if="!loading" :icon="WorkflowIcon" :title="hasFilters ? '没有匹配的工作流' : '暂无工作流'"
              :description="hasFilters ? '调整搜索词或分类后再试。' : '新建工作流，从开始和结束节点搭建流程。'">
              <UiButton v-if="hasFilters" @click="resetFilters">清除筛选</UiButton>
            </EmptyState>
          </template>
        </el-table>
      </div>

      <footer v-if="!loadError && total > pageSize" class="workflow-footer">
        <el-pagination v-model:current-page="page" :page-size="pageSize" :total="total"
          layout="prev, pager, next" @current-change="loadData" />
      </footer>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useDebounceFn } from '@vueuse/core'
import { ElMessage } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { Play, Plus, Search, SquarePen, Trash2, Workflow as WorkflowIcon } from '@lucide/vue'
import dayjs from 'dayjs'
import { workflowApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import UiButton from '@/components/ui/UiButton.vue'
import type { Workflow } from '@/types'

const router = useRouter()
const workflows = ref<Workflow[]>([])
const loading = ref(false)
const loadError = ref(false)
const creating = ref(false)
const deletingId = ref<number | null>(null)
const total = ref(0)
const page = ref(1)
const pageSize = 20
const keyword = ref('')
const filterCategory = ref('')
const hasFilters = computed(() => Boolean(keyword.value.trim() || filterCategory.value))
let fetchSequence = 0

const categories: Record<string, string> = {
  legal: '合同法务', finance: '财税金融', hr: '人力资源', procurement: '采购供应链', marketing: '市场营销',
  service: '客户服务', office: '政务办公', compliance: '合规管理', tech: '研发技术', other: '其他',
}
const triggerLabels: Record<string, string> = { manual: '手动', schedule: '定时', webhook: 'Webhook' }

function formatDate(value?: string) {
  return value && dayjs(value).isValid() ? dayjs(value).format('YYYY-MM-DD HH:mm') : '—'
}

async function loadData() {
  const requestId = ++fetchSequence
  loading.value = true
  loadError.value = false
  try {
    const result = await workflowApi.list({
      page: page.value, page_size: pageSize,
      keyword: keyword.value.trim() || undefined,
      category: filterCategory.value || undefined,
    })
    if (requestId !== fetchSequence) return
    workflows.value = result.items
    total.value = result.total
  } catch {
    if (requestId === fetchSequence) loadError.value = true
  } finally {
    if (requestId === fetchSequence) loading.value = false
  }
}

function applyFilters() {
  page.value = 1
  return loadData()
}

const searchWorkflows = useDebounceFn(applyFilters, 230)

function resetFilters() {
  keyword.value = ''
  filterCategory.value = ''
  void applyFilters()
}

async function createWorkflow() {
  if (creating.value) return
  creating.value = true
  try {
    const workflow = await workflowApi.create({
      name: '新建流程',
      definition: { nodes: [
        { id: 'start', type: 'start', config: {}, position: { x: 100, y: 200 } },
        { id: 'end', type: 'end', config: {}, position: { x: 600, y: 200 } },
      ], edges: [] },
    })
    await router.push(`/workflows/${workflow.id}/editor`)
  } catch {
    // 请求层提供失败反馈。
  } finally {
    creating.value = false
  }
}

async function deleteWorkflow(workflow: Workflow) {
  if (deletingId.value !== null) return
  try {
    await confirmDestructive(`确认删除流程「${workflow.name}」？`, '删除工作流')
  } catch { return }
  deletingId.value = workflow.id
  try {
    await workflowApi.delete(workflow.id)
    ElMessage.success('工作流已删除')
    if (workflows.value.length === 1 && page.value > 1) page.value--
    await loadData()
  } catch {
    // 请求层提供失败反馈。
  } finally {
    deletingId.value = null
  }
}

onMounted(loadData)
</script>

<style scoped>
.workflow-table { min-width: 0; }
.workflow-filters,.filter-controls { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.workflow-filters { padding: 12px 14px; border-bottom: 1px solid var(--ui-border); }
.workflow-search { width: min(100%, 244px); }
.workflow-categories { width: 142px; }
.resource-count { font-size: 12px; color: var(--ui-secondary); white-space: nowrap; }
.workflow-name { display: flex; align-items: center; gap: 10px; min-width: 0; color: var(--ui-text); text-decoration: none; }
.workflow-name:hover strong { color: var(--ui-accent); }
.workflow-type-icon {
  display: grid; place-items: center; flex: 0 0 32px; height: 32px; border-radius: var(--ui-radius-md);
  background: var(--ui-accent-soft); color: var(--ui-accent);
}
.workflow-name-text { display: grid; min-width: 0; line-height: 1.45; }
.workflow-name strong,.workflow-name small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workflow-name strong { font-size: 13px; font-weight: 640; }
.workflow-name small { font-size: 12px; color: var(--ui-secondary); }
.workflow-version,.workflow-date { color: var(--ui-secondary); white-space: nowrap; font-variant-numeric: tabular-nums; }
.workflow-version { font-family: var(--ui-font-mono); font-size: 12px; }
.workflow-status { display: inline-flex; align-items: center; gap: 6px; color: var(--ui-secondary); white-space: nowrap; font-size: 12px; }
.workflow-status > span { width: 6px; height: 6px; border-radius: 50%; background: var(--ui-muted); }
.workflow-status.published { color: var(--ui-success); }
.workflow-status.published > span { background: var(--ui-success); }
.workflow-table :deep(.el-button + .el-button) { margin-left: 0; }
.workflow-table :deep(.el-table__cell) { padding-block: 10px; }
.workflow-footer { display: flex; justify-content: flex-end; padding: 12px; border-top: 1px solid var(--ui-border); }
</style>
