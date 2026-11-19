<template>
  <div class="page-container">
    <RouterLink to="/knowledge" class="detail-back"><ArrowLeft :size="15" /> 知识库</RouterLink>
    <PageHeader :title="kb?.name || '知识库详情'" :description="kb?.description">
      <template #actions>
        <el-button :icon="Link" :disabled="!kb" @click="crawlDialogVisible = true">网页爬取</el-button>
      </template>
    </PageHeader>

    <el-skeleton v-if="loading && !kb" animated :rows="5" />
    <EmptyState v-else-if="!kb && failed" title="知识库加载失败" :icon="CircleAlert">
      <el-button @click="loadDocs()">重新加载</el-button>
    </EmptyState>
    <el-tabs v-else-if="kb" v-model="activeTab">
      <el-tab-pane name="documents" label="文档">
        <div class="documents">
          <el-upload class="document-drop" drag multiple :show-file-list="false" :before-upload="uploadDocument"
            accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.markdown,.csv"
            aria-label="上传文档">
            <div class="upload-prompt">
              <div class="upload-icon"><Upload :size="20" /></div>
              <div>
                <strong>拖放文件到此处，或<span>点击上传</span></strong>
                <p>支持 PDF、Office、Markdown、TXT 和 CSV，可一次上传多个。</p>
              </div>
            </div>
          </el-upload>

          <ul v-if="uploads.length" class="upload-list data-surface" aria-live="polite">
            <li v-for="item in uploads" :key="item.id">
              <FileText :size="16" />
              <span class="upload-name text-ellipsis">{{ item.name }}</span>
              <el-progress v-if="item.state === 'uploading'" :percentage="item.progress" class="upload-progress" :indeterminate="item.progress === 0" />
              <el-tag v-else size="small" :type="item.state === 'done' ? 'success' : 'danger'">
                {{ item.state === 'done' ? '已上传' : '上传失败' }}
              </el-tag>
              <el-button v-if="item.state !== 'uploading'" text size="small" :icon="X" :aria-label="`关闭 ${item.name} 上传反馈`" @click="dismissUpload(item.id)" />
            </li>
          </ul>

          <section class="data-surface">
            <div class="page-toolbar document-toolbar">
              <h2>文档</h2>
              <span class="resource-meta">{{ total }} 个 · {{ kb.vector_count }} 向量</span>
              <el-button text :icon="RefreshCw" :loading="loading" @click="loadDocs()">刷新</el-button>
            </div>
            <el-table :data="docs" v-loading="loading" row-key="id">
              <el-table-column label="文件" min-width="245" show-overflow-tooltip>
                <template #default="{ row }">
                  <div class="document-name">
                    <FileText :size="17" />
                    <div class="min-w-0">
                      <div class="resource-name text-ellipsis">{{ row.filename }}</div>
                      <div class="resource-meta">{{ row.file_type.toUpperCase() }} · {{ formatSize(row.file_size) }}</div>
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="105">
                <template #default="{ row }">
                  <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="chunk_count" label="分块数" width="90" align="right" />
              <el-table-column label="上传时间" min-width="152">
                <template #default="{ row }">{{ dayjs(row.created_at).format('YYYY-MM-DD HH:mm') }}</template>
              </el-table-column>
              <el-table-column label="操作" width="76" align="right">
                <template #default="{ row }">
                  <el-button size="small" text type="danger" @click="deleteDoc(row)">删除</el-button>
                </template>
              </el-table-column>
              <template #empty>
                <EmptyState v-if="failed" title="文档加载失败" :icon="CircleAlert">
                  <el-button @click="loadDocs()">重试</el-button>
                </EmptyState>
                <EmptyState v-else-if="!loading" title="暂无文档" description="上传文件或添加网页，完成索引后可运行检索测试。" :icon="FileText" />
              </template>
            </el-table>
            <div v-if="total > pageSize" class="list-footer">
              <el-pagination v-model:current-page="page" :page-size="pageSize" :total="total"
                layout="prev, pager, next" @current-change="loadDocs()" />
            </div>
          </section>
        </div>
      </el-tab-pane>
      <el-tab-pane name="overview" label="概览">
        <section class="data-surface kb-overview">
          <h2>索引配置</h2>
          <dl>
            <div><dt>状态</dt><dd>{{ kb.is_active ? '启用' : '停用' }}</dd></div>
            <div><dt>文档数量</dt><dd>{{ kb.doc_count }}</dd></div>
            <div><dt>向量数量</dt><dd>{{ kb.vector_count }}</dd></div>
            <div><dt>嵌入模型</dt><dd>{{ kb.embedding_model || '使用平台默认配置' }}</dd></div>
            <div><dt>分块大小</dt><dd>{{ kb.chunk_size }}</dd></div>
            <div><dt>分块重叠</dt><dd>{{ kb.chunk_overlap }}</dd></div>
          </dl>
        </section>
      </el-tab-pane>
      <el-tab-pane name="retrieval" label="检索测试" lazy>
        <KnowledgeRetrieval :key="kbId" :base-id="kbId" />
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="crawlDialogVisible" title="网页爬取" width="min(480px, calc(100vw - 32px))">
      <el-form label-position="top" @submit.prevent="doCrawl">
        <el-form-item label="网页 URL" required><el-input v-model="crawlUrl" placeholder="https://example.com/article" clearable /></el-form-item>
        <el-form-item label="标题（可选）"><el-input v-model="crawlTitle" placeholder="自定义文档标题" clearable /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="crawlDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="crawling" @click="doCrawl">开始爬取</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, CircleAlert, FileText, Link, RefreshCw, Upload, X } from '@lucide/vue'
import { knowledgeApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import KnowledgeRetrieval from './KnowledgeRetrieval.vue'
import type { KnowledgeBase, KnowledgeDocument } from '@/types'
import dayjs from 'dayjs'

interface UploadEntry {
  id: number
  name: string
  progress: number
  state: 'uploading' | 'done' | 'failed'
}

const route = useRoute()
const kbId = computed(() => Number(route.params.id))
const activeTab = ref('documents')
const kb = ref<KnowledgeBase | null>(null)
const docs = ref<KnowledgeDocument[]>([])
const loading = ref(true)
const failed = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20
const uploads = ref<UploadEntry[]>([])
let nextUploadId = 0
let requestId = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false

const crawlDialogVisible = ref(false)
const crawlUrl = ref('')
const crawlTitle = ref('')
const crawling = ref(false)

function formatSize(bytes: number) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

const statusAppearances = { pending: 'info', processing: 'warning', done: 'success', failed: 'danger' } as const
const statusLabels = { pending: '待处理', processing: '处理中', done: '已完成', failed: '失败' }
const statusType = (status: KnowledgeDocument['status']) => statusAppearances[status] || 'info'
const statusLabel = (status: KnowledgeDocument['status']) => statusLabels[status] || status

function schedulePoll() {
  clearTimeout(pollTimer)
  if (!disposed && docs.value.some(doc => doc.status === 'pending' || doc.status === 'processing')) {
    pollTimer = setTimeout(() => loadDocs(true), 2000)
  }
}

async function loadDocs(silent = false) {
  clearTimeout(pollTimer)
  const id = ++requestId
  const base = kbId.value
  if (!silent) loading.value = true
  failed.value = false
  try {
    const [res, baseInfo] = await Promise.all([
      knowledgeApi.listDocuments(base, { page: page.value, page_size: pageSize }),
      knowledgeApi.getBase(base),
    ])
    if (id !== requestId || disposed) return
    docs.value = res.items
    total.value = res.total
    kb.value = baseInfo
    schedulePoll()
  } catch {
    if (id !== requestId || disposed) return
    failed.value = true
    docs.value = []
  } finally {
    if (id === requestId && !disposed) loading.value = false
  }
}

function dismissUpload(id: number) {
  uploads.value = uploads.value.filter(item => item.id !== id)
}

function uploadDocument(file: File) {
  const base = kbId.value
  const entry = reactive<UploadEntry>({ id: ++nextUploadId, name: file.name, progress: 0, state: 'uploading' })
  uploads.value.push(entry)
  void sendUpload(base, file, entry)
  return false
}

async function sendUpload(base: number, file: File, item: UploadEntry) {
  try {
    await knowledgeApi.uploadDocument(base, file, event => {
      item.progress = event.total ? Math.round(event.loaded / event.total * 100) : 0
    })
    item.state = 'done'
    if (!disposed && kbId.value === base) await loadDocs()
  } catch {
    item.state = 'failed'
  }
}

async function deleteDoc(doc: KnowledgeDocument) {
  const base = kbId.value
  try {
    await ElMessageBox.confirm(`删除文档「${doc.filename}」及其索引？`, '删除文档', { type: 'warning', confirmButtonText: '删除' })
    await knowledgeApi.deleteDocument(base, doc.id)
    ElMessage.success('文档已删除')
    if (disposed || kbId.value !== base) return
    if (docs.value.length === 1 && page.value > 1) page.value -= 1
    await loadDocs()
  } catch {
    // 取消确认或请求层已提示失败原因。
  }
}

async function doCrawl() {
  if (crawling.value) return
  if (!crawlUrl.value.trim()) return ElMessage.warning('请输入网页 URL')
  const base = kbId.value
  crawling.value = true
  try {
    await knowledgeApi.crawlWebpage(base, { url: crawlUrl.value.trim(), title: crawlTitle.value.trim() || undefined })
    ElMessage.success('网页已提交索引')
    if (disposed || kbId.value !== base) return
    crawlDialogVisible.value = false
    crawlUrl.value = ''
    crawlTitle.value = ''
    activeTab.value = 'documents'
    await loadDocs()
  } catch {
    // 请求层已提示失败原因，保留表单方便重试。
  } finally {
    crawling.value = false
  }
}

watch(kbId, () => {
  kb.value = null
  docs.value = []
  uploads.value = []
  page.value = 1
  total.value = 0
  activeTab.value = 'documents'
  crawlDialogVisible.value = false
  crawlUrl.value = ''
  crawlTitle.value = ''
  void loadDocs()
}, { immediate: true })

onUnmounted(() => {
  disposed = true
  clearTimeout(pollTimer)
})
</script>

<style scoped>
.detail-back { display: inline-flex; align-items: center; gap: 6px; color: var(--ui-secondary); text-decoration: none; margin-bottom: 12px; }
.detail-back:hover { color: var(--ui-accent); }
.documents { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; }
.document-drop :deep(.el-upload-dragger) { padding: 18px; background: var(--ui-surface); }
.upload-prompt { display: flex; align-items: center; justify-content: center; text-align: left; gap: 12px; }
.upload-icon { display: grid; place-items: center; flex: 0 0 40px; aspect-ratio: 1; color: var(--ui-accent); background: var(--ui-accent-soft); border-radius: var(--ui-radius-md); }
.upload-prompt strong { font-size: 14px; color: var(--ui-text); }
.upload-prompt strong span { color: var(--ui-accent); }
.upload-prompt p { margin-top: 4px; color: var(--ui-muted); font-size: 12px; }
.upload-list { list-style: none; padding: 0 16px; }
.upload-list li { display: flex; align-items: center; gap: 8px; min-height: 42px; }
.upload-list li + li { border-top: 1px solid var(--ui-border); }
.upload-name { flex: 1; min-width: 0; }
.upload-progress { width: min(200px, 35%); }
.document-toolbar h2, .kb-overview h2 { font-size: 15px; }
.document-toolbar .el-button { margin-inline-start: auto; }
.document-name { display: flex; align-items: center; gap: 10px; }
.document-name svg { flex: none; color: var(--ui-muted); }
.list-footer { display: flex; justify-content: flex-end; padding: 12px; }
.kb-overview { padding: 20px; }
.kb-overview dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr)); gap: 16px; margin-top: 16px; }
.kb-overview dl > div { padding: 12px; background: var(--ui-subtle); border-radius: var(--ui-radius-sm); }
.kb-overview dt { color: var(--ui-muted); margin-bottom: 6px; }
.kb-overview dd { font-weight: 600; overflow-wrap: anywhere; }
</style>
