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
            <el-table-column prop="doc_count" label="文档" width="90" align="right" />
            <el-table-column prop="vector_count" label="向量" width="90" align="right" />
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
          <el-form-item label="分块大小"><el-input-number v-model="form.chunk_size" :min="100" :max="2000" /></el-form-item>
          <el-form-item label="分块重叠"><el-input-number v-model="form.chunk_overlap" :min="0" :max="500" /></el-form-item>
        </div>
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
const form = reactive({ name: '', description: '', chunk_size: 500, chunk_overlap: 50 })
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
    chunk_size: kb?.chunk_size ?? 500,
    chunk_overlap: kb?.chunk_overlap ?? 50,
  })
  dialogVisible.value = true
}

async function save() {
  if (saving.value) return
  if (!form.name.trim()) return ElMessage.warning('请输入名称')
  saving.value = true
  try {
    if (editing.value) {
      await knowledgeApi.updateBase(editing.value.id, form)
      ElMessage.success('知识库已更新')
    } else {
      await knowledgeApi.createBase(form)
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
.list-footer { display: flex; justify-content: flex-end; padding: 12px; }
</style>
