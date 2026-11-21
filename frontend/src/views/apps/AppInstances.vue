<template>
  <div class="page-container">
    <PageHeader title="我的应用" description="管理已部署的应用、资源绑定和访问状态。">
      <template #actions>
        <el-button :icon="Blocks" type="primary" @click="router.push('/apps')">浏览应用市场</el-button>
      </template>
    </PageHeader>

    <section class="data-surface">
      <div class="page-toolbar">
        <el-input v-model="query" class="instance-search" :prefix-icon="Search" placeholder="搜索我的应用" aria-label="搜索我的应用" clearable />
        <span class="resource-meta instance-count">{{ instances.length }} 个应用</span>
      </div>
      <el-table :data="visibleInstances" v-loading="loading" row-key="id">
        <el-table-column label="应用" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="instance-name">
              <component :is="row.workflow_id ? WorkflowIcon : Bot" :size="17" />
              <div class="min-w-0">
                <div class="resource-name text-ellipsis">{{ row.name }}</div>
                <div v-if="row.description" class="resource-meta text-ellipsis">{{ row.description }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" min-width="110">
          <template #default="{ row }">{{ row.workflow_id ? '工作流应用' : '对话应用' }}</template>
        </el-table-column>
        <el-table-column label="访问范围" width="95">
          <template #default="{ row }"><el-tag size="small" type="info">{{ row.is_public ? '公开' : '私有' }}</el-tag></template>
        </el-table-column>
        <el-table-column label="状态" width="92">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '启用' : '已停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" align="right">
          <template #default="{ row }">
            <div class="instance-actions">
              <el-button v-if="row.workflow_id" size="small" :icon="Play" @click="router.push(`/workflows/${row.workflow_id}/run`)">运行</el-button>
              <el-button v-else size="small" :icon="MessageSquare" @click="openChat(row)">对话</el-button>
              <el-button size="small" text @click="editInstance(row)">配置</el-button>
              <el-button size="small" text type="danger" @click="deleteInstance(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState v-if="failed" title="应用列表加载失败" :icon="CircleAlert">
            <el-button @click="loadData">重试</el-button>
          </EmptyState>
          <EmptyState v-else-if="!loading" :title="query ? '没有匹配的应用' : '暂无已部署的应用'"
            :description="query ? '请更换搜索关键词。' : '在应用市场选择模板，配置资源后即可部署。'" :icon="Blocks">
            <el-button v-if="!query" type="primary" @click="router.push('/apps')">浏览应用市场</el-button>
          </EmptyState>
        </template>
      </el-table>
    </section>

    <el-dialog v-model="chatDialogVisible" :title="chatInstance?.name" width="min(700px, calc(100vw - 32px))" :close-on-click-modal="false">
      <div class="app-chat">
        <div ref="appMsgRef" class="app-messages" role="log" :aria-busy="appStreaming">
          <EmptyState v-if="!appMessages.length" title="开始对话" :icon="MessageSquare"
            :description="chatInstance?.model_config_id ? '在下方输入消息。' : '先在应用配置中绑定模型，再开始对话。'" />
          <div v-for="(message, index) in appMessages" :key="index" class="app-msg" :class="message.role">
            <span class="message-role">{{ message.role === 'user' ? '你' : '应用' }}</span>
            <div class="markdown-body" v-html="renderMarkdown(message.content)" />
          </div>
          <div v-if="appStreaming" class="app-msg assistant">
            <span class="message-role">应用 · 正在响应</span>
            <div v-if="appStreamContent" class="markdown-body" v-html="renderMarkdown(appStreamContent)" />
          </div>
          <p v-if="chatFailed" class="chat-error" role="status">对话暂时不可用，请检查绑定的模型后重试。</p>
        </div>
        <form class="app-composer" @submit.prevent="sendAppMessage">
          <el-input v-model="appInput" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }"
            aria-label="输入消息" placeholder="输入消息，Ctrl + Enter 发送"
            @keydown.ctrl.enter.prevent="sendAppMessage" />
          <el-button type="primary" :icon="Send" native-type="submit" :loading="appStreaming"
            :disabled="!chatInstance?.model_config_id || !appInput.trim()">发送</el-button>
        </form>
      </div>
    </el-dialog>

    <el-dialog v-model="editDialogVisible" title="应用配置" width="min(520px, calc(100vw - 32px))">
      <el-form :model="editForm" label-position="top">
        <el-form-item label="应用名称" required><el-input v-model="editForm.name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="editForm.description" type="textarea" :rows="2" /></el-form-item>
        <AppBindingFields v-model="editForm" :models="models" :bases="bases" :workflows="workflows"
          :loading="bindingLoading" :failed="bindingFailed" :allow-clear="false" @retry="loadBindings" />
        <div class="instance-switches">
          <el-form-item label="状态"><el-switch v-model="editForm.is_active" active-text="启用" inactive-text="停用" /></el-form-item>
          <el-form-item label="公开访问"><el-switch v-model="editForm.is_public" active-text="公开" inactive-text="私有" /></el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { Blocks, Bot, CircleAlert, MessageSquare, Play, Search, Send, Workflow as WorkflowIcon } from '@lucide/vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { appApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { AppInstance } from '@/types'
import AppBindingFields from './AppBindingFields.vue'
import { useAppResources, type AppBindings } from './useAppResources'

interface EditForm extends AppBindings {
  name: string
  description: string
  is_active: boolean
  is_public: boolean
}

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

const router = useRouter()
const instances = ref<AppInstance[]>([])
const query = ref('')
const loading = ref(true)
const failed = ref(false)
const visibleInstances = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return instances.value.filter(instance => [instance.name, instance.description ?? ''].some(value => value.toLowerCase().includes(keyword)))
})

const {
  models, bases, workflows,
  loading: bindingLoading, failed: bindingFailed, load: loadBindings,
} = useAppResources()

const chatDialogVisible = ref(false)
const chatInstance = ref<AppInstance | null>(null)
const appMessages = ref<ChatMessage[]>([])
const appInput = ref('')
const appStreaming = ref(false)
const appStreamContent = ref('')
const chatFailed = ref(false)
const appMsgRef = ref<HTMLElement>()
let streamAbort: AbortController | undefined

const editDialogVisible = ref(false)
const editingInstance = ref<AppInstance | null>(null)
const saving = ref(false)
const editForm = ref<EditForm>({
  name: '', description: '', model_config_id: null, knowledge_base_id: null, workflow_id: null, is_active: true, is_public: false,
})

function renderMarkdown(text: string) {
  return DOMPurify.sanitize(marked.parse(text, { async: false }) as string)
}

async function loadData() {
  loading.value = true
  failed.value = false
  try {
    const res = await appApi.listInstances({ page_size: 100 })
    instances.value = res.items
  } catch {
    failed.value = true
    instances.value = []
  } finally {
    loading.value = false
  }
}

function openChat(inst: AppInstance) {
  stopChat()
  chatInstance.value = inst
  appMessages.value = []
  appInput.value = ''
  chatFailed.value = false
  chatDialogVisible.value = true
}

function stopChat() {
  streamAbort?.abort()
  streamAbort = undefined
  appStreaming.value = false
  appStreamContent.value = ''
}

function shouldFollowStream() {
  const view = appMsgRef.value
  return !view || view.scrollHeight - view.scrollTop - view.clientHeight < 80
}

async function scrollToEnd() {
  await nextTick()
  if (appMsgRef.value) appMsgRef.value.scrollTop = appMsgRef.value.scrollHeight
}

async function sendAppMessage(event?: Event) {
  if (event instanceof KeyboardEvent && event.isComposing) return
  const inst = chatInstance.value
  const content = appInput.value.trim()
  if (!inst?.model_config_id || !content || appStreaming.value) return

  const controller = new AbortController()
  streamAbort = controller
  appMessages.value.push({ role: 'user', content })
  appInput.value = ''
  appStreaming.value = true
  appStreamContent.value = ''
  chatFailed.value = false
  await scrollToEnd()

  try {
    const response = await fetch('/api/v1/models/chat', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${localStorage.getItem('access_token')}`,
      },
      signal: controller.signal,
      body: JSON.stringify({
        model_config_id: inst.model_config_id,
        messages: appMessages.value,
        stream: true,
        ...(inst.knowledge_base_id && { knowledge_base_id: inst.knowledge_base_id }),
      }),
    })
    if (!response.ok || !response.body) throw new Error('对话请求失败')

    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
    let buffer = ''
    let complete = false
    try {
      while (!complete && !controller.signal.aborted) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += value
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        const follow = shouldFollowStream()
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const data = line.slice(6).trim()
          if (data === '[DONE]') {
            complete = true
            break
          }
          const entry: { type?: string; content?: string } = JSON.parse(data)
          if (entry.type === 'error') throw new Error('模型返回错误')
          if (entry.type === 'content') appStreamContent.value += entry.content ?? ''
        }
        if (follow) await scrollToEnd()
      }
    } finally {
      await reader.cancel().catch(() => undefined)
    }
    if (controller.signal.aborted) return
    if (!complete) throw new Error('响应未完成')
    if (appStreamContent.value) appMessages.value.push({ role: 'assistant', content: appStreamContent.value })
  } catch {
    if (!controller.signal.aborted) chatFailed.value = true
  } finally {
    if (streamAbort === controller) stopChat()
  }
}

function editInstance(inst: AppInstance) {
  editingInstance.value = inst
  editForm.value = {
    name: inst.name, description: inst.description || '',
    model_config_id: inst.model_config_id ?? null,
    knowledge_base_id: inst.knowledge_base_id ?? null,
    workflow_id: inst.workflow_id ?? null,
    is_active: inst.is_active, is_public: inst.is_public,
  }
  editDialogVisible.value = true
  void loadBindings()
}

async function saveEdit() {
  if (!editingInstance.value || saving.value) return
  if (!editForm.value.name.trim()) return ElMessage.warning('请输入应用名称')
  saving.value = true
  try {
    const updated = await appApi.updateInstance(editingInstance.value.id, editForm.value)
    instances.value = instances.value.map(instance => instance.id === updated.id ? updated : instance)
    ElMessage.success('应用配置已保存')
    editDialogVisible.value = false
  } catch {
    // 请求层已提示失败原因，保留表单方便重试。
  } finally {
    saving.value = false
  }
}

async function deleteInstance(inst: AppInstance) {
  try {
    await confirmDestructive(`删除应用「${inst.name}」？`, '删除应用')
    await appApi.deleteInstance(inst.id)
    instances.value = instances.value.filter(instance => instance.id !== inst.id)
    ElMessage.success('应用已删除')
  } catch {
    // 取消确认或请求层已提示失败原因。
  }
}

watch(chatDialogVisible, visible => {
  if (!visible) stopChat()
})

onMounted(loadData)
onUnmounted(stopChat)
</script>

<style scoped>
.instance-search { width: min(100%, 260px); }
.instance-count { margin-inline-start: auto; }
.instance-name { display: flex; align-items: center; gap: 10px; }
.instance-name svg { flex: none; color: var(--ui-muted); }
.instance-actions { display: flex; justify-content: flex-end; align-items: center; gap: 4px; }
.instance-actions :deep(.el-button + .el-button) { margin-inline-start: 0; }
.instance-actions :deep(.el-button.is-text) { padding-inline: 6px; }
.instance-switches { display: flex; flex-wrap: wrap; gap: 16px; }
.app-chat { display: flex; flex-direction: column; height: min(62dvh, 500px); min-height: 240px; }
.app-messages { flex: 1; min-height: 0; overflow: auto; padding: 8px 2px; }
.app-msg { display: flex; flex-direction: column; margin-bottom: 16px; overflow-wrap: anywhere; }
.app-msg.user { align-items: end; }
.app-msg.user .markdown-body { background: var(--ui-subtle); border-radius: var(--ui-radius-md); padding: 8px 12px; max-width: 82%; }
.app-msg.assistant .markdown-body { width: 100%; }
.message-role { font-size: 12px; color: var(--ui-muted); padding-bottom: 4px; }
.app-composer { border-top: 1px solid var(--ui-border); padding-top: 12px; display: flex; align-items: end; gap: 8px; }
.chat-error { color: var(--ui-danger); background: var(--ui-danger-soft); padding: 8px 12px; border-radius: var(--ui-radius-sm); }
</style>
