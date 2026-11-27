<template>
  <div class="page-container">
    <PageHeader title="知识库" description="管理供 Agent 检索的文档、索引与知识片段。">
      <template #actions>
        <el-button type="primary" :icon="Plus" @click="openDialog()">新建知识库</el-button>
      </template>
    </PageHeader>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="知识库列表" name="bases">
        <section class="data-surface">
          <form class="page-toolbar" @submit.prevent="applySearch">
            <el-input v-model="keyword" class="base-search" placeholder="搜索知识库名称" aria-label="搜索知识库名称"
              :prefix-icon="Search" clearable @clear="applySearch" />
            <el-button native-type="submit">搜索</el-button>
            <span class="base-count resource-meta">{{ total }} 个知识库</span>
          </form>
          <el-table :data="kbList" v-loading="loading" row-key="id">
            <el-table-column label="知识库" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">
                <div class="base-name">
                  <span class="base-icon"><LibraryBig :size="17" /></span>
                  <div class="min-w-0">
                    <RouterLink :to="`/knowledge/${row.id}`" class="resource-name base-link">{{ row.name }}</RouterLink>
                    <div v-if="row.description" class="resource-meta text-ellipsis">{{ row.description }}</div>
                  </div>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="doc_count" label="文档" width="76" align="right" />
            <el-table-column label="Chunk" width="82" align="right">
              <template #default="{ row }">{{ row.indexed_chunk_count ?? 0 }}</template>
            </el-table-column>
            <el-table-column prop="vector_count" label="向量" width="80" align="right" />
            <el-table-column label="Embedding" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">
                <div class="resource-meta">{{ row.embedding_model || '平台默认' }}</div>
                <div class="resource-meta">
                  {{ row.embedding_dimension ? `${row.embedding_dimension} 维` : '维度待构建' }}
                  <span v-if="row.active_index_generation_id"> · G{{ row.active_index_generation_id }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="失败" width="70" align="right">
              <template #default="{ row }">
                <span :class="{ 'has-failure': (row.failed_doc_count || 0) > 0 }">{{ row.failed_doc_count || 0 }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="92">
              <template #default="{ row }">
                <el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '启用' : '停用' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="104" align="right">
              <template #default="{ row }">
                <div class="base-actions">
                  <el-button size="small" text @click="openDialog(row)">编辑</el-button>
                  <el-button size="small" text type="danger" @click="deleteKb(row)">删除</el-button>
                </div>
              </template>
            </el-table-column>
            <template #empty>
              <EmptyState v-if="failed" title="知识库加载失败" :icon="CircleAlert">
                <el-button @click="loadData">重试</el-button>
              </EmptyState>
              <EmptyState v-else-if="!loading" :title="appliedKeyword ? '没有匹配的知识库' : '暂无知识库'"
                :description="appliedKeyword ? '请更换关键词。' : '创建知识库后，上传文档并验证检索结果。'" :icon="LibraryBig">
                <el-button v-if="!appliedKeyword" type="primary" :icon="Plus" @click="openDialog()">新建知识库</el-button>
              </EmptyState>
            </template>
          </el-table>
          <div v-if="total > pageSize" class="list-footer">
            <el-pagination v-model:current-page="page" :page-size="pageSize" :total="total"
              layout="prev, pager, next" @current-change="loadData" />
          </div>
        </section>
      </el-tab-pane>
      <el-tab-pane label="检索测试" name="retrieval" lazy>
        <KnowledgeRetrieval :key="retrievalRevision" />
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑知识库' : '新建知识库'" width="min(480px, calc(100vw - 32px))">
      <el-form :model="form" label-position="top" @submit.prevent>
        <el-form-item label="名称" required><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="form.description" type="textarea" :rows="2" /></el-form-item>
        <div class="chunk-fields">
          <el-form-item label="Chunk Token 上限">
            <el-input-number v-model="form.chunk_size_tokens" :min="64" :max="4096" :step="64" />
          </el-form-item>
          <el-form-item label="Overlap Token">
            <el-input-number v-model="form.chunk_overlap_tokens" :min="0" :max="1024" :step="16" />
          </el-form-item>
        </div>
        <div class="chunk-fields">
          <el-form-item label="解析器">
            <el-select v-model="form.parser_profile" class="form-select">
              <el-option label="自动（Docling 优先）" value="auto" />
              <el-option label="Docling" value="docling" />
              <el-option label="原生解析器" value="native" />
            </el-select>
          </el-form-item>
          <el-form-item label="可见性">
            <el-select v-model="form.visibility" class="form-select">
              <el-option label="私有" value="private" />
              <el-option label="组织共享（只读）" value="organization" />
              <el-option label="项目共享" value="project" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item v-if="form.visibility === 'project'" label="项目 ID" required>
          <el-input-number v-model="form.project_id" :min="1" />
        </el-form-item>
        <p class="resource-meta">Chunk 与解析配置仅对之后的构建生效；修改后请重建索引。</p>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { CircleAlert, LibraryBig, Plus, Search } from '@lucide/vue'
import { knowledgeApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import KnowledgeRetrieval from './KnowledgeRetrieval.vue'
import type { KnowledgeBase } from '@/types'

const activeTab = ref('bases')
const retrievalRevision = ref(0)
const kbList = ref<KnowledgeBase[]>([])
const keyword = ref('')
const appliedKeyword = ref('')
const loading = ref(true)
const failed = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 12
const dialogVisible = ref(false)
const editing = ref<KnowledgeBase | null>(null)
const saving = ref(false)
const form = reactive({
  name: '',
  description: '',
  chunk_size_tokens: 512,
  chunk_overlap_tokens: 64,
  parser_profile: 'auto' as 'auto' | 'docling' | 'native',
  visibility: 'private' as 'private' | 'project' | 'organization',
  project_id: undefined as number | undefined,
})
let requestId = 0

async function loadData() {
  const id = ++requestId
  loading.value = true
  failed.value = false
  try {
    const res = await knowledgeApi.listBases({ page: page.value, page_size: pageSize, keyword: appliedKeyword.value || undefined })
    if (id !== requestId) return
    kbList.value = res.items
    total.value = res.total
  } catch {
    if (id !== requestId) return
    failed.value = true
    kbList.value = []
    total.value = 0
  } finally {
    if (id === requestId) loading.value = false
  }
}

function applySearch() {
  appliedKeyword.value = keyword.value.trim()
  page.value = 1
  return loadData()
}

function openDialog(kb?: KnowledgeBase) {
  editing.value = kb ?? null
  Object.assign(form, {
    name: kb?.name ?? '',
    description: kb?.description ?? '',
    chunk_size_tokens: kb?.chunk_size_tokens ?? 512,
    chunk_overlap_tokens: kb?.chunk_overlap_tokens ?? 64,
    parser_profile: kb?.parser_profile ?? 'auto',
    visibility: kb?.visibility ?? 'private',
    project_id: kb?.project_id ?? undefined,
  })
  dialogVisible.value = true
}

async function save() {
  if (saving.value) return
  if (!form.name.trim()) return ElMessage.warning('请输入名称')
  if (form.chunk_overlap_tokens >= form.chunk_size_tokens) {
    return ElMessage.warning('Overlap Token 必须小于 Chunk Token 上限')
  }
  if (form.visibility === 'project' && !form.project_id) {
    return ElMessage.warning('项目共享需要填写项目 ID')
  }
  saving.value = true
  try {
    const payload = {
      name: form.name.trim(),
      description: form.description,
      chunk_size_tokens: form.chunk_size_tokens,
      chunk_overlap_tokens: form.chunk_overlap_tokens,
      parser_profile: form.parser_profile,
      visibility: form.visibility,
      project_id: form.visibility === 'project' ? form.project_id : null,
    }
    if (editing.value) {
      await knowledgeApi.updateBase(editing.value.id, payload)
      ElMessage.success('知识库已更新，重建索引后生效')
    } else {
      await knowledgeApi.createBase(payload)
      ElMessage.success('知识库已创建')
      activeTab.value = 'bases'
      keyword.value = ''
      appliedKeyword.value = ''
      page.value = 1
    }
    dialogVisible.value = false
    retrievalRevision.value += 1
    await loadData()
  } catch {
    // 请求层已提示失败原因，保留表单方便重试。
  } finally {
    saving.value = false
  }
}

async function deleteKb(kb: KnowledgeBase) {
  try {
    await confirmDestructive(`删除知识库「${kb.name}」及其中的文档和索引？此操作不可撤销。`, '删除知识库')
    await knowledgeApi.deleteBase(kb.id)
    ElMessage.success('知识库已删除')
    retrievalRevision.value += 1
    if (kbList.value.length === 1 && page.value > 1) page.value -= 1
    await loadData()
  } catch {
    // 取消确认或请求层已提示失败原因。
  }
}

onMounted(loadData)
</script>

<style scoped>
.base-search { width: min(100%, 260px); }
.base-count { margin-inline-start: auto; }
.base-name { display: flex; gap: 10px; align-items: center; }
.base-icon {
  display: grid;
  place-items: center;
  flex: 0 0 34px;
  aspect-ratio: 1;
  border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-sm);
  background: var(--ui-subtle);
  color: var(--ui-secondary);
}
.base-link { display: inline-block; color: var(--ui-text); text-decoration: none; }
.base-link:hover { color: var(--ui-accent); text-decoration: underline; }
.base-actions { display: flex; justify-content: flex-end; gap: 4px; }
.base-actions :deep(.el-button + .el-button) { margin-inline-start: 0; }
.base-actions :deep(.el-button.is-text) { padding-inline: 6px; }
.chunk-fields { display: flex; flex-wrap: wrap; gap: 12px; }
.form-select { width: 190px; }
.has-failure { color: var(--ui-danger); font-weight: 600; }
.list-footer { display: flex; justify-content: flex-end; padding: 12px; }
</style>
