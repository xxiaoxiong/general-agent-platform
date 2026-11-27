<template>
  <div class="page-container">
    <RouterLink to="/knowledge" class="detail-back"><ArrowLeft :size="15" /> 知识库</RouterLink>
    <PageHeader :title="kb?.name || '知识库详情'" :description="kb?.description">
      <template #actions>
        <el-button :icon="Link" :disabled="!kb" @click="crawlDialogVisible = true">网页爬取</el-button>
        <el-button :icon="RefreshCw" :disabled="!kb || rebuilding" :loading="rebuilding" @click="rebuild">重建索引</el-button>
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
            accept=".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.markdown,.csv,.html,.htm,.rtf,.odt,.ods,.odp,.png,.jpg,.jpeg,.tif,.tiff"
            aria-label="上传文档">
            <div class="upload-prompt">
              <div class="upload-icon"><Upload :size="20" /></div>
              <div>
                <strong>拖放文件到此处，或<span>点击上传</span></strong>
                <p>PDF / 扫描件 / Word / Excel / PPT / Markdown / CSV / HTML / 图片 OCR，可一次上传多个。</p>
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
              <span class="resource-meta">
                {{ total }} 个 · {{ kb.indexed_chunk_count ?? 0 }} Chunk · {{ kb.vector_count }} 向量
              </span>
              <el-button text :icon="RefreshCw" :loading="loading" @click="loadDocs()">刷新</el-button>
            </div>
            <el-table :data="docs" v-loading="loading" row-key="id">
              <el-table-column label="文件" min-width="235" show-overflow-tooltip>
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
              <el-table-column label="构建" width="190">
                <template #default="{ row }">
                  <div class="build-cell">
                    <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
                    <span v-if="jobOf(row) && isActiveJob(row)" class="resource-meta build-stage">
                      {{ stageLabel(jobOf(row)) }} {{ Math.round((jobOf(row)?.progress || 0) * 100) }}%
                    </span>
                    <el-progress
                      v-if="jobOf(row) && isActiveJob(row)" :percentage="Math.round((jobOf(row)?.progress || 0) * 100)"
                      :show-text="false" :stroke-width="4" class="build-progress"
                    />
                  </div>
                  <el-tooltip v-if="row.error_msg" :content="row.error_msg" placement="top">
                    <span class="build-error">{{ row.error_code || 'error' }}</span>
                  </el-tooltip>
                  <el-tooltip v-if="row.warnings?.length" :content="row.warnings.join('\n')" placement="top">
                    <span class="build-warning">{{ row.warnings.length }} 条警告</span>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="解析规模" width="150">
                <template #default="{ row }">
                  <div class="resource-meta scale-cell">
                    <span v-if="row.page_count">{{ row.page_count }} 页</span>
                    <span v-if="row.sheet_count">{{ row.sheet_count }} Sheet</span>
                    <span v-if="row.slide_count">{{ row.slide_count }} Slide</span>
                    <span>{{ row.element_count || 0 }} 元素</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="chunk_count" label="Chunk" width="82" align="right" />
              <el-table-column label="更新时间" min-width="140">
                <template #default="{ row }">{{ dayjs(row.updated_at || row.created_at).format('MM-DD HH:mm') }}</template>
              </el-table-column>
              <el-table-column label="操作" width="200" align="right">
                <template #default="{ row }">
                  <div class="doc-actions">
                    <el-button size="small" text :disabled="!row.active_version_id && !row.element_count" @click="openPreview(row)">解析预览</el-button>
                    <el-button size="small" text :disabled="!row.chunk_count" @click="openChunks(row)">Chunk</el-button>
                    <el-button size="small" text @click="reindex(row)">重建</el-button>
                    <el-button size="small" text type="danger" @click="deleteDoc(row)">删除</el-button>
                  </div>
                </template>
              </el-table-column>
              <template #empty>
                <EmptyState v-if="failed" title="文档加载失败" :icon="CircleAlert">
                  <el-button @click="loadDocs()">重试</el-button>
                </EmptyState>
                <EmptyState v-else-if="!loading" title="暂无文档" description="上传文件或添加网页，索引完成后可查看解析结果与 Chunk。" :icon="FileText" />
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
            <div><dt>状态</dt><dd>{{ kb.status === 'active' ? '启用' : kb.status }}</dd></div>
            <div><dt>文档数量</dt><dd>{{ kb.doc_count }}</dd></div>
            <div><dt>已索引 Chunk</dt><dd>{{ kb.indexed_chunk_count ?? 0 }}</dd></div>
            <div><dt>向量数量</dt><dd>{{ kb.vector_count }}</dd></div>
            <div><dt>失败文档</dt><dd>{{ kb.failed_doc_count ?? 0 }}</dd></div>
            <div><dt>Embedding 模型</dt><dd>{{ kb.embedding_model || '平台默认' }}</dd></div>
            <div><dt>向量维度</dt><dd>{{ kb.embedding_dimension || '-' }}</dd></div>
            <div><dt>Active Generation</dt><dd>{{ kb.active_index_generation_id ?? '尚未构建' }}</dd></div>
            <div><dt>解析器</dt><dd>{{ kb.parser_profile || 'auto' }}</dd></div>
            <div><dt>Chunk Token</dt><dd>{{ kb.chunk_size_tokens ?? 512 }} / overlap {{ kb.chunk_overlap_tokens ?? 64 }}</dd></div>
            <div><dt>可见性</dt><dd>{{ visibilityLabel(kb.visibility) }}</dd></div>
            <div><dt>最近构建</dt><dd>{{ kb.last_ingested_at ? dayjs(kb.last_ingested_at).format('YYYY-MM-DD HH:mm') : '-' }}</dd></div>
          </dl>
        </section>
      </el-tab-pane>

      <el-tab-pane name="retrieval" label="检索测试" lazy>
        <KnowledgeRetrieval :key="kbId" :base-id="kbId" />
      </el-tab-pane>

      <el-tab-pane name="evaluation" label="质量评测" lazy>
        <KnowledgeEvaluationPanel :kb-id="kbId" :docs="docs" />
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

    <ParsedPreviewDrawer v-model="previewVisible" :doc="activeDoc" />
    <ChunkInspectorDrawer v-model="chunksVisible" :doc="activeDoc" />
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { ArrowLeft, CircleAlert, FileText, Link, RefreshCw, Upload, X } from '@lucide/vue'
import { knowledgeApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import KnowledgeRetrieval from './KnowledgeRetrieval.vue'
import KnowledgeEvaluationPanel from '@/features/knowledge/KnowledgeEvaluationPanel.vue'
import ParsedPreviewDrawer from '@/features/knowledge/ParsedPreviewDrawer.vue'
import ChunkInspectorDrawer from '@/features/knowledge/ChunkInspectorDrawer.vue'
import type { IngestionJob, KnowledgeBase, KnowledgeDocument } from '@/types'
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
const jobs = ref<Record<number, IngestionJob>>({})
const loading = ref(true)
const failed = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20
const uploads = ref<UploadEntry[]>([])
const previewVisible = ref(false)
const chunksVisible = ref(false)
const activeDoc = ref<KnowledgeDocument | null>(null)
const rebuilding = ref(false)
let nextUploadId = 0
let requestId = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false

const crawlDialogVisible = ref(false)
const crawlUrl = ref('')
const crawlTitle = ref('')
const crawling = ref(false)

const ACTIVE_STATUSES = new Set(['pending', 'queued', 'processing', 'deleting'])
const statusAppearances: Record<string, string> = {
  pending: 'info', queued: 'info', processing: 'warning', ready: 'success',
  partial: 'warning', done: 'success', failed: 'danger', deleting: 'warning',
  migration_required: 'warning',
}
const statusLabels: Record<string, string> = {
  pending: '待处理', queued: '排队中', processing: '构建中', ready: '已就绪',
  partial: '部分就绪', done: '已就绪', failed: '失败', deleting: '删除中',
  migration_required: '待迁移', deleted: '已删除',
}
const stageLabels: Record<string, string> = {
  queued: '排队', detecting: '检测', parsing: '解析', ocr: 'OCR', normalizing: '归一化',
  chunking: '切分', embedding: '向量化', indexing: '写入索引', validating: '校验',
  ready: '完成', failed: '失败',
}

function statusType(status: string): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  return (statusAppearances[status] || 'info') as 'primary' | 'success' | 'warning' | 'danger' | 'info'
}
function statusLabel(status: string) {
  return statusLabels[status] || status
}
function stageLabel(job?: IngestionJob) {
  return job ? stageLabels[job.stage] || job.stage : ''
}
function visibilityLabel(value?: string) {
  if (value === 'project') return '项目共享'
  if (value === 'organization') return '组织共享'
  return '私有'
}
function jobOf(doc: KnowledgeDocument) {
  return jobs.value[doc.id]
}
function isActiveJob(doc: KnowledgeDocument) {
  const job = jobOf(doc)
  return !!job && (job.status === 'queued' || job.status === 'running')
}

function formatSize(bytes: number) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

function schedulePoll() {
  clearTimeout(pollTimer)
  if (disposed) return
  const needsPoll = docs.value.some(doc => ACTIVE_STATUSES.has(doc.status))
    || Object.values(jobs.value).some(job => job.status === 'queued' || job.status === 'running')
  if (needsPoll) {
    pollTimer = setTimeout(() => loadDocs(true), 2000)
  }
}

async function loadJobs() {
  const pendingDocs = docs.value.filter(doc => ACTIVE_STATUSES.has(doc.status))
  const updated: Record<number, IngestionJob> = { ...jobs.value }
  for (const doc of pendingDocs) {
    try {
      updated[doc.id] = await knowledgeApi.getIngestion(doc.id)
    } catch {
      // 409/404：尚无 Job，忽略
    }
  }
  jobs.value = updated
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
    await loadJobs()
    if (id !== requestId || disposed) return
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

function openPreview(doc: KnowledgeDocument) {
  activeDoc.value = doc
  previewVisible.value = true
}

function openChunks(doc: KnowledgeDocument) {
  activeDoc.value = doc
  chunksVisible.value = true
}

async function reindex(doc: KnowledgeDocument) {
  try {
    await confirmDestructive(`重新解析并索引「${doc.filename}」？`, '重建索引')
    await knowledgeApi.reindexDocument(doc.id)
    ElMessage.success('已提交重建任务')
    await loadDocs()
  } catch {
    // 取消确认或请求层已提示失败原因。
  }
}

async function rebuild() {
  if (rebuilding.value) return
  try {
    await confirmDestructive('重建整个知识库索引（新 Generation，构建完成前旧索引继续可用）？', '重建知识库')
    rebuilding.value = true
    const result = await knowledgeApi.rebuildBase(kbId.value)
    ElMessage.success(`已创建 Generation ${result.generation_no}，共 ${result.jobs.length} 个构建任务`)
    await loadDocs()
  } catch {
    // 取消确认或请求层已提示失败原因。
  } finally {
    rebuilding.value = false
  }
}

async function deleteDoc(doc: KnowledgeDocument) {
  const base = kbId.value
  try {
    await confirmDestructive(`删除文档「${doc.filename}」及其索引？`, '删除文档')
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
  jobs.value = {}
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
.build-cell { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.build-stage { font-family: var(--ui-font-mono); font-size: 11px; }
.build-progress { width: 100%; }
.build-error { display: block; margin-top: 2px; color: var(--ui-danger); font-family: var(--ui-font-mono); font-size: 11px; overflow-wrap: anywhere; }
.build-warning { display: block; margin-top: 2px; color: var(--ui-warning); font-size: 11px; }
.scale-cell { display: flex; flex-wrap: wrap; gap: 4px 8px; }
.doc-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 2px; }
.doc-actions :deep(.el-button.is-text) { padding-inline: 4px; }
.list-footer { display: flex; justify-content: flex-end; padding: 12px; }
.kb-overview { padding: 20px; }
.kb-overview dl { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(170px, 100%), 1fr)); gap: 16px; margin-top: 16px; }
.kb-overview dl > div { padding: 12px; background: var(--ui-subtle); border-radius: var(--ui-radius-sm); }
.kb-overview dt { color: var(--ui-muted); margin-bottom: 6px; }
.kb-overview dd { font-weight: 600; overflow-wrap: anywhere; }
@media (max-width: 760px) { .doc-actions { justify-content: flex-start; } }
</style>
