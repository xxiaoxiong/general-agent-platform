<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ChevronRight, Cpu, LibraryBig, ListChecks, MessageSquare, SquarePen, Store, Workflow } from '@lucide/vue'
import dayjs from 'dayjs'
import { useAuthStore } from '@/stores/auth'
import { appApi, knowledgeApi, modelApi, workflowApi } from '@/api'
import { conversationApi, type Conversation } from '@/api/v2'
import UiButton from '@/components/ui/UiButton.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import PageHeader from '@/components/layout/PageHeader.vue'

const auth = useAuthStore()
const greeting = computed(() => {
  const name = auth.user?.full_name || auth.user?.username
  return name ? `欢迎回来，${name}` : '工作空间概览'
})
const date = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'full' }).format(new Date())

const resources = [
  { label: '模型供应商', icon: Cpu, path: '/models', load: async () => (await modelApi.listProviders()).length },
  { label: '知识库', icon: LibraryBig, path: '/knowledge', load: async () => (await knowledgeApi.listBases({ page_size: 1 })).total },
  { label: '工作流', icon: Workflow, path: '/workflows', load: async () => (await workflowApi.list({ page_size: 1 })).total },
  { label: '应用实例', icon: Store, path: '/apps/instances', load: async () => (await appApi.listInstances({ page_size: 1 })).total },
]
const quickActions = [
  { label: '流程编排', description: '构建和调试可复用的自动化流程', icon: Workflow, path: '/workflows' },
  { label: '管理知识库', description: '上传文档并测试知识检索', icon: LibraryBig, path: '/knowledge' },
  { label: '提示词调试', description: '用选定模型试运行提示词', icon: SquarePen, path: '/prompts/debugger' },
  { label: '应用市场', description: '从已有模板部署应用', icon: Store, path: '/apps' },
]

const counts = ref<Record<string, number>>({})
const sessions = ref<Conversation[]>([])
const resourcesLoading = ref(true)
const sessionsLoading = ref(true)
const sessionsError = ref(false)
const countError = computed(() => !resourcesLoading.value && resources.some(resource => counts.value[resource.path] === undefined))

async function loadResources() {
  resourcesLoading.value = true
  try {
    const values = await Promise.allSettled(resources.map(resource => resource.load()))
    values.forEach((value, index) => {
      if (value.status === 'fulfilled') counts.value[resources[index].path] = value.value
    })
  } finally {
    resourcesLoading.value = false
  }
}

async function loadSessions() {
  sessionsLoading.value = true
  sessionsError.value = false
  try {
    sessions.value = (await conversationApi.list({ page_size: 5 })).items.slice(0, 5)
  } catch {
    sessionsError.value = true
  } finally {
    sessionsLoading.value = false
  }
}

onMounted(() => {
  void loadResources()
  void loadSessions()
})
</script>

<template>
  <div class="page-container dashboard">
    <PageHeader :title="greeting" :description="date">
      <template #actions>
        <UiButton as-child variant="primary"><RouterLink to="/workspace"><MessageSquare />打开智能体对话</RouterLink></UiButton>
      </template>
    </PageHeader>

    <section class="dashboard-section" aria-labelledby="resource-heading" :aria-busy="resourcesLoading">
      <div class="section-bar">
        <h2 id="resource-heading" class="section-heading">资源概览</h2>
        <UiButton v-if="countError" variant="ghost" size="sm" @click="loadResources">重试未加载指标</UiButton>
      </div>
      <div class="resource-grid">
        <RouterLink v-for="resource in resources" :key="resource.path" :to="resource.path" class="metric-link data-surface">
          <div class="metric-caption"><component :is="resource.icon" :size="16" /><span>{{ resource.label }}</span><ChevronRight :size="14" /></div>
          <el-skeleton-item v-if="resourcesLoading && counts[resource.path] === undefined" variant="text" class="metric-skeleton" />
          <strong v-else class="dashboard-value" :title="counts[resource.path] === undefined ? '暂不可用' : undefined">{{ counts[resource.path] ?? '—' }}</strong>
        </RouterLink>
      </div>
    </section>

    <div class="dashboard-columns">
      <section class="dashboard-section" aria-labelledby="shortcuts-heading">
        <div class="section-bar"><h2 id="shortcuts-heading" class="section-heading">快捷入口</h2></div>
        <div class="data-surface">
          <RouterLink v-for="action in quickActions" :key="action.path" :to="action.path" class="dashboard-row">
            <span class="row-icon"><component :is="action.icon" :size="17" aria-hidden="true" /></span>
            <span class="row-content"><strong>{{ action.label }}</strong><small>{{ action.description }}</small></span>
            <ChevronRight :size="15" class="row-end" aria-hidden="true" />
          </RouterLink>
        </div>
      </section>

      <section class="dashboard-section" aria-labelledby="recent-heading" :aria-busy="sessionsLoading">
        <div class="section-bar"><h2 id="recent-heading" class="section-heading">近期对话</h2></div>
        <div class="data-surface">
          <div v-if="sessionsLoading" class="session-skeleton"><el-skeleton :rows="3" animated /></div>
          <ErrorState v-else-if="sessionsError" title="无法加载近期对话" @retry="loadSessions" />
          <EmptyState v-else-if="!sessions.length" :icon="ListChecks" title="还没有对话" description="打开智能体对话，创建会话后可在这里查看。">
            <UiButton as-child size="sm"><RouterLink to="/workspace">打开对话</RouterLink></UiButton>
          </EmptyState>
          <template v-else>
            <RouterLink v-for="session in sessions" :key="session.id" :to="{ path: '/workspace', query: { conversation: session.id } }" class="dashboard-row">
              <span class="row-icon"><MessageSquare :size="16" /></span>
              <span class="row-content">
                <strong class="text-ellipsis">{{ session.title || '未命名会话' }}</strong>
                <small>{{ dayjs(session.updated_at || session.created_at).format('MM-DD HH:mm') }} · {{ session.message_count }} 条消息</small>
              </span>
              <span v-if="session.active_run_id" class="active-session">任务进行中</span>
              <ChevronRight v-else :size="15" class="row-end" aria-hidden="true" />
            </RouterLink>
          </template>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dashboard { max-width: 1260px; }
.dashboard-section { min-width: 0; }
.section-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; min-height: 27px; }
.resource-grid, .dashboard-columns { display: grid; gap: 14px; }
.resource-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.dashboard-columns { grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 32px; }
.metric-link { display: flex; flex-direction: column; gap: 11px; padding: 15px 17px; min-height: 104px; text-decoration: none; color: var(--ui-text); }
.metric-link:hover { border-color: var(--ui-border-strong); background: var(--ui-subtle); }
.metric-caption { display: flex; gap: 8px; align-items: center; color: var(--ui-muted); font-size: var(--ui-size-sm); }
.metric-caption svg { flex: none; }
.metric-caption span { flex: 1; }
.dashboard-value { font-size: 1.75rem; font-weight: var(--ui-weight-semibold); font-variant-numeric: tabular-nums; line-height: 1.15; }
.metric-skeleton { width: 34px; height: 27px; }
.dashboard-row { display: flex; align-items: center; gap: 12px; padding: 13px 14px; color: var(--ui-text); text-decoration: none; }
.dashboard-row + .dashboard-row { border-top: var(--ui-line); }
.dashboard-row:hover { background: var(--ui-subtle); }
.dashboard-row:focus-visible { outline-offset: -2px; }
.row-icon {
  display: inline-grid; place-items: center; flex: none; width: 34px; height: 34px;
  border: var(--ui-line); border-radius: var(--ui-radius-md); background: var(--ui-bg); color: var(--ui-accent);
}
.row-content { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.row-content strong { font-weight: var(--ui-weight-semibold); }
.row-content small { font-size: var(--ui-size-xs); color: var(--ui-muted); }
.row-end { flex: none; color: var(--ui-muted); }
.session-skeleton { padding: 20px; }
.active-session { flex: none; font-size: var(--ui-size-xs); color: var(--ui-info); background: var(--ui-info-soft); padding: 2px 7px; border-radius: var(--ui-radius-sm); }

@media (max-width: 1120px) {
  .resource-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 680px) {
  .dashboard-columns { grid-template-columns: minmax(0, 1fr); margin-top: 24px; }
}
</style>
