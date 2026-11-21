<template>
  <div class="page-container">
    <PageHeader title="提示词模板库" description="维护可复用的提示词、变量和版本。">
      <template #actions>
        <el-button :icon="FlaskConical" @click="router.push('/prompts/debugger')">调试器</el-button>
        <el-button :icon="Plus" type="primary" @click="openCreate">新建模板</el-button>
      </template>
    </PageHeader>

    <section class="data-surface filter-surface">
      <form class="page-toolbar" @submit.prevent="applyFilters">
        <el-input v-model="keyword" class="template-search" :prefix-icon="Search" placeholder="搜索模板名称或内容"
          aria-label="搜索模板" clearable @clear="applyFilters" />
        <el-button native-type="submit">搜索</el-button>
        <el-select v-model="category" class="category-select" clearable placeholder="所有分类" aria-label="模板分类" @change="applyFilters">
          <el-option v-for="c in categories" :key="c.value" :label="c.label" :value="c.value" />
        </el-select>
        <el-checkbox v-model="myOnly" @change="applyFilters">仅我的</el-checkbox>
        <span class="resource-meta result-count">{{ total }} 个模板</span>
      </form>
    </section>

    <el-skeleton v-if="loading" animated :rows="5" />
    <EmptyState v-else-if="failed" title="模板加载失败" :icon="CircleAlert">
      <el-button @click="loadData">重新加载</el-button>
    </EmptyState>
    <EmptyState v-else-if="!templates.length" :title="hasFilters ? '没有匹配的模板' : '暂无提示词模板'"
      :description="hasFilters ? '试试更换关键词或清除筛选条件。' : '创建模板后，可在调试器填写变量并比较模型输出。'" :icon="FileText">
      <el-button v-if="hasFilters" @click="resetFilters">清除筛选</el-button>
      <el-button v-else type="primary" :icon="Plus" @click="openCreate">新建模板</el-button>
    </EmptyState>

    <div v-else class="template-grid">
      <article v-for="tpl in templates" :key="tpl.id" class="data-surface template-card">
        <header>
          <div class="min-w-0">
            <h2>{{ tpl.name }}</h2>
            <p v-if="tpl.description">{{ tpl.description }}</p>
          </div>
          <span class="version-label mono">v{{ tpl.version }}</span>
        </header>
        <div class="template-meta">
          <el-tag size="small" type="info">{{ getCategoryLabel(tpl.category) }}</el-tag>
          <el-tag v-if="tpl.is_public" size="small" type="info">公开</el-tag>
          <span class="resource-meta">使用 {{ tpl.usage_count }} 次</span>
        </div>
        <pre class="prompt-preview">{{ tpl.content.slice(0, 180) }}{{ tpl.content.length > 180 ? '…' : '' }}</pre>
        <div v-if="tpl.variables?.length" class="template-meta">
          <el-tag v-for="v in tpl.variables" :key="v" size="small" effect="plain" type="info" class="mono">
            &#123;&#123;{{ v }}&#125;&#125;
          </el-tag>
        </div>
        <footer>
          <el-button size="small" :icon="FlaskConical" @click="debugTemplate(tpl)">调试</el-button>
          <el-button size="small" text @click="viewVersions(tpl)">历史</el-button>
          <template v-if="tpl.owner_id === auth.user?.id">
            <el-button size="small" text @click="editTemplate(tpl)">编辑</el-button>
            <el-button size="small" text type="danger" @click="deleteTemplate(tpl)">删除</el-button>
          </template>
        </footer>
      </article>
    </div>

    <div v-if="total > pageSize" class="list-footer">
      <el-pagination v-model:current-page="page" :page-size="pageSize" :total="total"
        layout="prev, pager, next" @current-change="loadData" />
    </div>

    <!-- Create/Edit Dialog -->
    <el-dialog v-model="editDialogVisible" :title="editingId ? '编辑模板' : '新建模板'" width="min(700px, calc(100vw - 32px))" top="5vh">
      <el-form :model="form" label-position="top">
        <el-form-item label="模板名称" required>
          <el-input v-model="form.name" placeholder="简洁描述用途" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="form.category" class="w-full">
            <el-option v-for="c in categories" :key="c.value" :label="c.label" :value="c.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="提示词内容" required>
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="9"
            class="prompt-input"
            placeholder="使用 {{变量名}} 定义变量，如：请分析以下内容：{{content}}"
          />
        </el-form-item>
        <el-form-item label="检测到变量">
          <div v-if="detectedVars.length">
            <el-tag v-for="v in detectedVars" :key="v" class="variable-tag">&#123;&#123;{{ v }}&#125;&#125;</el-tag>
          </div>
          <span v-else class="resource-meta">暂未检测到变量</span>
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="form.tags" placeholder="逗号分隔，如: 分析,报告,总结" />
        </el-form-item>
        <el-form-item label="公开">
          <el-switch v-model="form.is_public" />
          <span class="resource-meta ms-2">公开后其他用户可查看和使用</span>
        </el-form-item>
        <el-form-item v-if="editingId" label="修改说明">
          <el-input v-model="form.change_note" placeholder="简述本次修改内容（可选）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTemplate">保存</el-button>
      </template>
    </el-dialog>


    <el-dialog v-model="versionDialogVisible" :title="`版本历史 · ${currentTemplate?.name ?? ''}`" width="min(640px, calc(100vw - 32px))">
      <el-skeleton v-if="versionsLoading" animated :rows="4" />
      <EmptyState v-else-if="versionsFailed" title="历史版本加载失败" :icon="CircleAlert">
        <el-button v-if="currentTemplate" @click="viewVersions(currentTemplate)">重试</el-button>
      </EmptyState>
      <EmptyState v-else-if="!versions.length" title="暂无历史版本" :icon="History" />
      <div v-else class="version-list">
        <article v-for="ver in versions" :key="ver.id" class="version-entry">
          <header>
            <el-tag size="small" type="info">v{{ ver.version }}</el-tag>
            <strong>{{ ver.change_note || '无修改说明' }}</strong>
            <time class="resource-meta">{{ new Date(ver.created_at).toLocaleString() }}</time>
          </header>
          <pre class="prompt-preview version-content">{{ ver.content }}</pre>
          <el-button v-if="currentTemplate?.owner_id === auth.user?.id" size="small"
            :loading="restoringVersion === ver.id" :disabled="restoringVersion != null" @click="restoreVersion(ver)">恢复此版本</el-button>
        </article>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { CircleAlert, FileText, FlaskConical, History, Plus, Search } from '@lucide/vue'
import { promptApi } from '@/api'
import { useAuthStore } from '@/stores/auth'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import { categories, detectVariables, getCategoryLabel, type PromptTemplate, type PromptVersion } from './promptPresentation'

const router = useRouter()
const auth = useAuthStore()
const templates = ref<PromptTemplate[]>([])
const loading = ref(true)
const failed = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20
const keyword = ref('')
const appliedKeyword = ref('')
const category = ref('')
const myOnly = ref(false)
let requestId = 0

const hasFilters = computed(() => !!(appliedKeyword.value || category.value || myOnly.value))

async function loadData() {
  const id = ++requestId
  loading.value = true
  failed.value = false
  try {
    const res = await promptApi.list({
      page: page.value,
      page_size: pageSize,
      keyword: appliedKeyword.value || undefined,
      category: category.value || undefined,
      include_public: !myOnly.value,
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

function applyFilters() {
  appliedKeyword.value = keyword.value.trim()
  page.value = 1
  return loadData()
}

function resetFilters() {
  keyword.value = ''
  category.value = ''
  myOnly.value = false
  return applyFilters()
}

const editDialogVisible = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const form = reactive({
  name: '', description: '', category: 'general', content: '', is_public: false, tags: '', change_note: '',
})
const detectedVars = computed(() => detectVariables(form.content))

function openCreate() {
  editingId.value = null
  Object.assign(form, { name: '', description: '', category: 'general', content: '', is_public: false, tags: '', change_note: '' })
  editDialogVisible.value = true
}

function editTemplate(tpl: PromptTemplate) {
  editingId.value = tpl.id
  Object.assign(form, {
    name: tpl.name, description: tpl.description || '',
    category: tpl.category, content: tpl.content,
    is_public: tpl.is_public, tags: tpl.tags || '', change_note: '',
  })
  editDialogVisible.value = true
}

async function saveTemplate() {
  if (saving.value) return
  if (!form.name.trim()) return ElMessage.warning('请输入模板名称')
  if (!form.content.trim()) return ElMessage.warning('请输入提示词内容')
  saving.value = true
  try {
    if (editingId.value) {
      await promptApi.update(editingId.value, form)
      ElMessage.success('模板已更新')
    } else {
      await promptApi.create(form)
      ElMessage.success('模板已创建')
      page.value = 1
    }
    editDialogVisible.value = false
    await loadData()
  } catch {
    // 请求层已提示失败原因，保留表单方便重试。
  } finally {
    saving.value = false
  }
}

async function deleteTemplate(tpl: PromptTemplate) {
  try {
    await confirmDestructive(`删除模板「${tpl.name}」及其历史版本？`, '删除模板')
    await promptApi.delete(tpl.id)
    ElMessage.success('模板已删除')
    if (templates.value.length === 1 && page.value > 1) page.value -= 1
    await loadData()
  } catch {
    // 取消确认或请求层已提示失败原因。
  }
}

const versionDialogVisible = ref(false)
const versionsLoading = ref(false)
const versionsFailed = ref(false)
const versions = ref<PromptVersion[]>([])
const currentTemplate = ref<PromptTemplate | null>(null)
const restoringVersion = ref<number | null>(null)
let versionRequestId = 0

async function viewVersions(tpl: PromptTemplate) {
  const id = ++versionRequestId
  currentTemplate.value = tpl
  versionDialogVisible.value = true
  versionsLoading.value = true
  versionsFailed.value = false
  try {
    const snapshots = await promptApi.listVersions(tpl.id)
    if (id === versionRequestId) versions.value = snapshots
  } catch {
    if (id === versionRequestId) versionsFailed.value = true
  } finally {
    if (id === versionRequestId) versionsLoading.value = false
  }
}

async function restoreVersion(ver: PromptVersion) {
  const tpl = currentTemplate.value
  if (!tpl || restoringVersion.value != null) return
  restoringVersion.value = ver.id
  try {
    await ElMessageBox.confirm(`将「${tpl.name}」的提示词恢复到 v${ver.version}？这会创建一个新版本。`, '恢复版本', {
      type: 'warning', confirmButtonText: '恢复',
    })
    await promptApi.update(tpl.id, { content: ver.content, change_note: `恢复至版本 v${ver.version}` })
    ElMessage.success('历史版本已恢复')
    versionDialogVisible.value = false
    await loadData()
  } catch {
    // 取消确认或请求层已提示失败原因。
  } finally {
    restoringVersion.value = null
  }
}

function debugTemplate(tpl: PromptTemplate) {
  router.push({ path: '/prompts/debugger', query: { template_id: tpl.id } })
}

onMounted(loadData)
</script>

<style scoped>
.filter-surface { margin-bottom: 16px; }
.template-search { width: min(100%, 275px); }
.category-select { width: 144px; }
.result-count { margin-inline-start: auto; }
.template-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(340px, 100%), 1fr)); gap: 12px; }
.template-card { padding: 16px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.template-card header, .version-entry header { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }
.template-card h2 { font-size: 15px; font-weight: 650; overflow-wrap: anywhere; }
.template-card header p { font-size: 13px; color: var(--ui-secondary); line-height: 1.5; overflow-wrap: anywhere; margin-top: 4px; }
.template-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.version-label { flex: none; color: var(--ui-muted); font-size: 12px; }
.prompt-preview { background: var(--ui-subtle); color: var(--ui-secondary); padding: 10px 12px; border-radius: var(--ui-radius-sm); font-family: var(--ui-font-mono); font-size: 12px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 112px; overflow: auto; }
.template-card footer { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; margin-top: auto; padding-top: 12px; border-top: 1px solid var(--ui-border); }
.template-card footer :deep(.el-button + .el-button) { margin-inline-start: 0; }
.list-footer { display: flex; justify-content: flex-end; padding: 12px 0; }
.variable-tag { margin: 2px; }
.prompt-input :deep(textarea) { font-family: var(--ui-font-mono); line-height: 1.65; }
.version-list { display: grid; gap: 12px; }
.version-entry { border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); padding: 12px; }
.version-entry header { flex-wrap: wrap; align-items: center; }
.version-entry time { margin-inline-start: auto; }
.version-content { margin: 10px 0; max-height: 160px; }
</style>
