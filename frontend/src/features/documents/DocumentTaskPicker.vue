<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronDown, Files } from '@lucide/vue'
import { documentError, documentProfilesApi, documentsApi, type DocumentAsset, type DocumentMode, type DocumentProfile } from '@/api/documents'
import { documentTaskModes, type DocumentTaskSelection } from './taskModes'

defineProps<{ disabled?: boolean }>()
const selection = defineModel<DocumentTaskSelection>({ required: true })
const open = ref(false)
const page = ref(1)
const total = ref(0)
const profiles = ref<DocumentProfile[]>([])
const documents = ref<DocumentAsset[]>([])
const loading = ref(false)
const error = ref('')
const pageSize = 30
const label = computed(() => selection.value.mode === 'auto'
  ? '文档任务'
  : documentTaskModes.find((mode) => mode.value === selection.value.mode)?.label)
const contextMode = computed(() => ['document_generate', 'document_edit'].includes(selection.value.mode))
const contextLabel = computed(() => selection.value.title || (selection.value.documentId
  ? `文档 #${selection.value.documentId}${selection.value.version ? ` · v${selection.value.version}` : ''}`
  : selection.value.profileId ? `规范 #${selection.value.profileId}` : ''))

function chooseMode(mode: DocumentMode) {
  if (selection.value.mode !== mode) selection.value = { mode }
  if (!['document_generate', 'document_edit'].includes(mode)) open.value = false
}

function chooseProfile(value: string) {
  const profile = profiles.value.find((item) => String(item.id) === value)
  selection.value = { mode: 'document_generate', profileId: profile?.id, title: profile?.name }
  open.value = false
}

function chooseDocument(value: string) {
  const asset = documents.value.find((item) => String(item.id) === value)
  selection.value = { mode: 'document_edit', documentId: asset?.id, version: asset?.current_version, title: asset?.title }
  open.value = false
}

watch([open, () => selection.value.mode], () => { page.value = 1 })
watch([open, () => selection.value.mode, page], async ([visible, mode, selectedPage], _, onCleanup) => {
  if (!visible || !contextMode.value) return
  let active = true
  onCleanup(() => { active = false })
  profiles.value = []
  documents.value = []
  loading.value = true
  error.value = ''
  try {
    const params = { page: selectedPage, page_size: pageSize }
    if (mode === 'document_generate') {
      const result = await documentProfilesApi.list({ ...params, status: 'published' })
      if (active) {
        profiles.value = result.items.filter((item) => item.status === 'published' && item.published_version)
        total.value = result.total
      }
    } else {
      const result = await documentsApi.list(params)
      if (active) {
        documents.value = result.items
        total.value = result.total
      }
    }
  } catch (err) {
    if (active) error.value = documentError(err, '无法加载文档中心数据')
  } finally {
    if (active) loading.value = false
  }
})
</script>

<template>
  <el-popover v-model:visible="open" trigger="click" placement="top-start" :width="290" :disabled="disabled">
    <template #reference>
      <button type="button" class="task-trigger" :class="{ selected: selection.mode !== 'auto' }" :disabled="disabled"
        :aria-label="contextLabel ? `${label}，${contextLabel}` : label" :title="contextLabel || label">
        <Files :size="16" /><span>{{ label }}</span><ChevronDown :size="12" />
      </button>
    </template>
    <div class="task-sheet">
      <span class="task-caption">任务类型</span>
      <div class="mode-options">
        <button v-for="mode in documentTaskModes" :key="mode.value" type="button" :aria-pressed="selection.mode === mode.value"
          @click="chooseMode(mode.value)">{{ mode.label }}</button>
      </div>
      <template v-if="contextMode">
        <p v-if="contextLabel" class="task-context">{{ contextLabel }}<template v-if="selection.title && selection.version"> · v{{ selection.version }}</template></p>
        <p v-if="loading" role="status">正在加载…</p>
        <p v-else-if="error" class="task-error" role="alert">{{ error }}</p>
        <label v-else-if="selection.mode === 'document_generate'">
          已发布规范
          <select :value="selection.profileId || ''" @change="chooseProfile(($event.target as HTMLSelectElement).value)">
            <option value="">使用消息描述匹配规范</option>
            <option v-for="item in profiles" :key="item.id" :value="item.id">{{ item.name }} · v{{ item.published_version }}</option>
          </select>
        </label>
        <label v-else>
          原文档
          <select :value="selection.documentId || ''" @change="chooseDocument(($event.target as HTMLSelectElement).value)">
            <option value="">从消息和会话识别文档</option>
            <option v-for="item in documents" :key="item.id" :value="item.id">{{ item.title }} · v{{ item.current_version }}</option>
          </select>
        </label>
        <div v-if="!loading && !error && total > pageSize" class="task-pages">
          <button type="button" :disabled="page === 1" @click="page--">上一页</button>
          <span>{{ page }} / {{ Math.ceil(total / pageSize) }}</span>
          <button type="button" :disabled="page * pageSize >= total" @click="page++">下一页</button>
        </div>
      </template>
      <RouterLink :to="{ name: 'Documents' }" @click="open = false">打开文档中心</RouterLink>
    </div>
  </el-popover>
</template>

<style scoped>
.task-trigger { min-height: 30px; padding: 4px 6px; display: flex; align-items: center; gap: 4px; border: 0; border-radius: var(--ui-radius-sm); background: none; color: var(--ui-secondary); font-size: 12px; white-space: nowrap; cursor: pointer; }
.task-trigger:hover, .task-trigger.selected { color: var(--ui-accent); background: var(--ui-accent-soft); }
.task-trigger:disabled { opacity: .55; cursor: default; }
.task-trigger svg { flex: none; }
.task-sheet { display: grid; gap: 9px; }
.task-caption, .task-sheet label { font-size: 12px; color: var(--ui-secondary); }
.mode-options { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; }
.task-sheet button { border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); background: var(--ui-bg); color: var(--ui-text); padding: 5px; cursor: pointer; }
.task-sheet button:hover, .task-sheet button[aria-pressed=true] { border-color: var(--ui-accent); color: var(--ui-accent); }
.task-sheet button:disabled { opacity: .45; cursor: default; }
.task-sheet label { display: grid; gap: 4px; }
.task-sheet select { width: 100%; min-height: 31px; border: 1px solid var(--ui-border); background: var(--ui-bg); color: var(--ui-text); border-radius: var(--ui-radius-sm); }
.task-context, .task-sheet p { margin: 0; font-size: 12px; overflow-wrap: anywhere; }
.task-context, .task-sheet a { color: var(--ui-accent); }
.task-error { color: var(--ui-danger); }
.task-pages { display: flex; align-items: center; justify-content: space-between; font-size: 12px; }
@media (max-width: 540px) { .task-trigger:not(.selected) span { display: none; } }
</style>
