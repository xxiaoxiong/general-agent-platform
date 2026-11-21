<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { Archive, BookOpen, CircleAlert, Download, Eye, FilePenLine, Files, FileText, History, ListChecks, Plus, Trash2 } from '@lucide/vue'
import {
  documentError, documentProfilesApi, documentsApi, downloadDocument,
  type DocumentAsset, type DocumentProfile, type DocumentTask, type DocumentVersion,
  type ProfileDefinition, type ProfileVersion, type ProfileVisibility,
} from '@/api/documents'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import UiButton from '@/components/ui/UiButton.vue'
import DocumentPdfPreview from '@/features/documents/DocumentPdfPreview.vue'
import DocumentVersionChanges from '@/features/documents/DocumentVersionChanges.vue'
import ProfileForm from '@/features/documents/ProfileForm.vue'
import { createProfileDefinition } from '@/features/documents/profileDefinition'
import { documentTaskLabels } from '@/features/documents/taskModes'
import { useAuthStore } from '@/stores/auth'

type Tab = 'assets' | 'profiles' | 'tasks'
const tabs: Array<{ key: Tab; label: string }> = [
  { key: 'assets', label: '文档' }, { key: 'profiles', label: '文档规范' }, { key: 'tasks', label: '任务记录' },
]
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const tab = ref<Tab>('assets')
const pageSize = 12
const pages = reactive<Record<Tab, number>>({ assets: 1, profiles: 1, tasks: 1 })
const assets = ref<DocumentAsset[]>([])
const profiles = ref<DocumentProfile[]>([])
const tasks = ref<DocumentTask[]>([])
const profileNames = reactive(new Map<number, string>())
const total = ref(0)
const listLoading = ref(false)
const listError = ref('')
let listRevision = 0

const profileStatuses = { draft: '草稿', published: '已发布', archived: '已归档' }
const taskStatuses: Record<string, string> = {
  pending: '排队中', running: '执行中', waiting_input: '等待补充', waiting_approval: '等待审批',
  completed: '已完成', failed: '失败', cancelled: '已停止', suspended: '已挂起',
  needs_reconciliation: '等待处理', limit_exceeded: '达到执行上限',
}
const dateFormat = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
const formatDate = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value)) ? dateFormat.format(new Date(value)) : '—'

async function loadList() {
  const revision = ++listRevision
  const selected = tab.value
  const params = { page: pages[selected], page_size: pageSize }
  listLoading.value = true
  listError.value = ''
  try {
    if (selected === 'assets') {
      const result = await documentsApi.list(params)
      if (revision !== listRevision) return
      assets.value = result.items
      total.value = result.total
      for (const id of new Set(result.items.map(item => item.profile_id))) {
        if (id === null || profileNames.has(id)) continue
        void documentProfilesApi.get(id).then((profile) => profileNames.set(id, profile.name)).catch(() => {})
      }
    } else if (selected === 'profiles') {
      const result = await documentProfilesApi.list(params)
      if (revision !== listRevision) return
      profiles.value = result.items
      for (const profile of result.items) profileNames.set(profile.id, profile.name)
      total.value = result.total
    } else {
      const result = await documentsApi.tasks(params)
      if (revision !== listRevision) return
      tasks.value = result.items
      total.value = result.total
    }
    if (pages[selected] > 1 && (pages[selected] - 1) * pageSize >= total.value) {
      pages[selected] = Math.max(1, Math.ceil(total.value / pageSize))
    }
  } catch (error) {
    if (revision === listRevision) listError.value = documentError(error, '加载失败，请重试')
  } finally {
    if (revision === listRevision) listLoading.value = false
  }
}

function changeTab(value: string | number) {
  const next = value === 'profiles' || value === 'tasks' ? value : 'assets'
  tab.value = next
  void router.replace({ query: next === 'assets' ? {} : { tab: next } })
}

watch([tab, () => pages[tab.value]], loadList, { immediate: true })

const historyOpen = ref(false)
const historyLoading = ref(false)
const historyError = ref('')
const historyKind = ref<'asset' | 'profile'>('asset')
const historyAsset = ref<DocumentAsset>()
const historyProfile = ref<DocumentProfile>()
const documentVersions = ref<DocumentVersion[]>([])
const profileVersions = ref<ProfileVersion[]>([])
const versionNumber = ref<number>()
const selectedDocumentVersion = computed(() => documentVersions.value.find(item => item.version_no === versionNumber.value))
const selectedProfileVersion = computed(() => profileVersions.value.find(item => item.version_no === versionNumber.value))
let historyRevision = 0

async function showHistory(kind: 'asset' | 'profile', id: number, version?: number) {
  const revision = ++historyRevision
  historyKind.value = kind
  historyOpen.value = true
  historyLoading.value = true
  historyError.value = ''
  historyAsset.value = undefined
  historyProfile.value = undefined
  documentVersions.value = []
  profileVersions.value = []
  versionNumber.value = undefined
  try {
    if (kind === 'asset') {
      const [asset, versions] = await Promise.all([documentsApi.get(id), documentsApi.versions(id)])
      if (revision !== historyRevision) return
      historyAsset.value = asset
      documentVersions.value = versions.items
    } else {
      const [profile, versions] = await Promise.all([documentProfilesApi.get(id), documentProfilesApi.versions(id)])
      if (revision !== historyRevision) return
      historyProfile.value = profile
      profileVersions.value = versions.items
    }
    const versions = kind === 'asset' ? documentVersions.value : profileVersions.value
    versionNumber.value = versions.find(item => item.version_no === version)?.version_no ?? versions[0]?.version_no
  } catch (error) {
    if (revision === historyRevision) historyError.value = documentError(error, '无法读取版本历史')
  } finally {
    if (revision === historyRevision) historyLoading.value = false
  }
}

function positiveId(value: unknown) {
  const id = typeof value === 'string' && /^\d+$/.test(value) ? Number(value) : 0
  return Number.isSafeInteger(id) && id > 0 ? id : undefined
}

watch(() => [route.query.tab, route.query.document, route.query.profile, route.query.version], () => {
  const asset = positiveId(route.query.document)
  const profile = positiveId(route.query.profile)
  tab.value = profile || route.query.tab === 'profiles' ? 'profiles' : route.query.tab === 'tasks' ? 'tasks' : 'assets'
  if (asset) void showHistory('asset', asset, positiveId(route.query.version))
  else if (profile) void showHistory('profile', profile, positiveId(route.query.version))
}, { immediate: true })

function closeHistory() {
  historyRevision++
  if (route.name === 'Documents' && (route.query.document || route.query.profile)) {
    void router.replace({ query: tab.value === 'assets' ? {} : { tab: tab.value } })
  }
}

const previewOpen = ref(false)
const previewTarget = ref<{ asset: DocumentAsset; version: number }>()
const previewTitle = computed(() => previewTarget.value
  ? `${previewTarget.value.asset.title} · v${previewTarget.value.version}`
  : '文档预览')
const downloads = reactive(new Set<string>())

function previewVersion(asset: DocumentAsset, version = asset.current_version) {
  previewTarget.value = { asset, version }
  previewOpen.value = true
}

async function downloadVersion(asset: DocumentAsset, version?: DocumentVersion) {
  const number = version?.version_no ?? asset.current_version
  const key = `${asset.id}:${number}`
  if (downloads.has(key)) return
  downloads.add(key)
  try {
    const metadata = version ?? (await documentsApi.versions(asset.id)).items.find(item => item.version_no === number)
    await downloadDocument(asset.id, number, metadata?.filename || `${asset.title}.docx`)
  } catch (error) {
    ElMessage.error(documentError(error, 'Word 文档下载失败'))
  } finally {
    downloads.delete(key)
  }
}

function editLink(asset: DocumentAsset, version = asset.current_version) {
  return { name: 'Workspace', query: { document: String(asset.id), version: String(version) } }
}

const editorOpen = ref(false)
const editing = ref<DocumentProfile>()
const editingDefinition = ref<ProfileDefinition>(createProfileDefinition())
const editorError = ref('')
const editorBusy = ref(false)
const visibility = ref<ProfileVisibility>('private')
const changeSummary = ref('')
const mutating = ref<string>()

function openEditor(profile?: DocumentProfile) {
  editing.value = profile
  editingDefinition.value = profile?.definition ?? createProfileDefinition()
  visibility.value = profile?.visibility ?? 'private'
  changeSummary.value = ''
  editorError.value = ''
  editorOpen.value = true
}

async function saveProfile(definition: ProfileDefinition) {
  if (editorBusy.value) return
  editorBusy.value = true
  editorError.value = ''
  const profile = editing.value
  const payload = {
    definition, visibility: visibility.value, change_summary: changeSummary.value.trim(),
    expected_version: profile?.current_version,
  }
  try {
    const saved = profile ? await documentProfilesApi.update(profile.id, payload) : await documentProfilesApi.create(payload)
    editorOpen.value = false
    if (historyOpen.value && historyProfile.value?.id === saved.id) void showHistory('profile', saved.id, saved.current_version)
    void loadList()
    ElMessage.success(`规范已保存为 v${saved.current_version}`)
  } catch (error) {
    editorError.value = documentError(error, '无法保存文档规范')
  } finally {
    editorBusy.value = false
  }
}

async function changeProfile(profile: DocumentProfile, operation: 'publish' | 'archive' | 'remove') {
  if (mutating.value) return
  const label = { publish: '发布', archive: '归档', remove: '删除' }[operation]
  mutating.value = `profile:${profile.id}`
  try {
    try {
      const description = operation === 'publish'
        ? `发布「${profile.name}」v${profile.current_version} 到${profile.visibility === 'organization' ? '组织' : '个人'}可用规范？`
        : `确定${label}「${profile.name}」？`
      if (operation === 'remove') await confirmDestructive(description, '删除文档规范')
      else await ElMessageBox.confirm(description, `${label}文档规范`, { type: 'warning', confirmButtonText: label, cancelButtonText: '取消' })
    } catch { return }
    if (operation === 'publish') await documentProfilesApi.publish(profile.id, profile.current_version)
    else await documentProfilesApi[operation](profile.id)
    if (historyProfile.value?.id === profile.id) {
      if (operation === 'remove') historyOpen.value = false
      else void showHistory('profile', profile.id, profile.current_version)
    }
    void loadList()
    ElMessage.success(`规范已${label}`)
  } catch (error) {
    ElMessage.error(documentError(error, `${label}失败`))
  } finally {
    mutating.value = undefined
  }
}

async function removeAsset(asset: DocumentAsset) {
  if (mutating.value) return
  mutating.value = `asset:${asset.id}`
  try {
    try {
      await confirmDestructive(`删除「${asset.title}」及其全部版本？`, '删除文档', { cancelButtonText: '保留' })
    } catch { return }
    await documentsApi.remove(asset.id)
    if (historyAsset.value?.id === asset.id) historyOpen.value = false
    void loadList()
    ElMessage.success('文档已删除')
  } catch (error) {
    ElMessage.error(documentError(error, '删除失败'))
  } finally {
    mutating.value = undefined
  }
}
</script>

<template>
  <div class="page-container document-center">
    <PageHeader title="文档中心" description="查看对话生成的 Word 文档、版本历史和可复用的文档规范。">
      <template #actions>
        <UiButton variant="primary" v-if="tab === 'profiles'" @click="openEditor()"><Plus />新建规范</UiButton>
        <UiButton v-else as-child><RouterLink :to="{ name: 'Workspace' }"><FilePenLine />前往对话</RouterLink></UiButton>
      </template>
    </PageHeader>
    <el-tabs :model-value="tab" @update:model-value="changeTab">
      <el-tab-pane v-for="itemTab in tabs" :key="itemTab.key" :label="itemTab.label" :name="itemTab.key">
        <section v-if="tab === itemTab.key" class="data-surface center-list" :aria-label="itemTab.label">
          <p v-if="listLoading" class="center-state" role="status">正在加载…</p>
          <EmptyState v-else-if="listError" :title="listError" :icon="CircleAlert">
            <UiButton size="sm" @click="loadList">重试</UiButton>
          </EmptyState>
          <template v-else-if="tab === 'assets'">
            <article v-for="item in assets" :key="item.id" class="center-row">
              <span class="item-icon"><FileText :size="21" /></span>
              <div class="row-copy">
                <button type="button" class="item-title" @click="showHistory('asset', item.id)">{{ item.title }}</button>
                <div class="row-meta">
                  <span>DOCX · v{{ item.current_version }}</span>
                  <RouterLink v-if="item.profile_id" :to="{ query: { tab: 'profiles', profile: String(item.profile_id) } }">{{ profileNames.get(item.profile_id) || `规范 #${item.profile_id}` }}</RouterLink>
                  <span>创建 {{ formatDate(item.created_at) }}</span><span>修改 {{ formatDate(item.updated_at) }}</span>
                </div>
              </div>
              <div class="row-actions">
                <UiButton size="sm" @click="previewVersion(item)"><Eye />预览</UiButton>
                <UiButton size="sm" variant="ghost" :loading="downloads.has(`${item.id}:${item.current_version}`)" @click="downloadVersion(item)"><Download />下载</UiButton>
                <UiButton size="sm" variant="ghost" @click="showHistory('asset', item.id)"><History />版本历史</UiButton>
                <UiButton size="sm" variant="ghost" as-child><RouterLink :to="editLink(item)"><FilePenLine />继续修改</RouterLink></UiButton>
              </div>
            </article>
            <EmptyState v-if="!assets.length" title="暂无文档" description="在智能体对话中生成 Word 文档后，可在这里查看和管理版本。" :icon="Files">
              <UiButton size="sm" as-child><RouterLink :to="{ name: 'Workspace' }">前往对话</RouterLink></UiButton>
            </EmptyState>
          </template>
          <template v-else-if="tab === 'profiles'">
            <article v-for="profile in profiles" :key="profile.id" class="center-row">
              <span class="item-icon profile-icon"><ListChecks :size="21" /></span>
              <div class="row-copy">
                <button type="button" class="item-title" @click="showHistory('profile', profile.id)">{{ profile.name }}</button>
                <p v-if="profile.description" class="row-description">{{ profile.description }}</p>
                <div class="row-meta">
                  <span class="status-tag" :data-status="profile.status">{{ profileStatuses[profile.status] }}</span>
                  <span>{{ profile.visibility === 'organization' ? '组织' : '私有' }}</span>
                  <span>当前 v{{ profile.current_version }}</span><span v-if="profile.published_version">已发布 v{{ profile.published_version }}</span>
                  <span>{{ profile.definition.sections.length }} 个章节</span>
                </div>
              </div>
              <div class="row-actions">
                <UiButton size="sm" @click="showHistory('profile', profile.id)"><BookOpen />详情和版本</UiButton>
                <UiButton v-if="profile.can_manage && profile.status !== 'archived'" size="sm" variant="ghost" @click="openEditor(profile)"><FilePenLine />编辑</UiButton>
                <UiButton v-if="profile.status === 'published'" size="sm" variant="ghost" as-child>
                  <RouterLink :to="{ name: 'Workspace', query: { profile: String(profile.id) } }">按规范生成</RouterLink>
                </UiButton>
              </div>
            </article>
            <EmptyState v-if="!profiles.length" title="暂无文档规范" description="手动创建规范，或在对话中从样例文档提炼章节与写作规则。" :icon="ListChecks">
              <UiButton size="sm" @click="openEditor()"><Plus />新建规范</UiButton>
            </EmptyState>
          </template>
          <template v-else>
            <article v-for="task in tasks" :key="task.id" class="center-row">
              <span class="item-icon"><History :size="21" /></span>
              <div class="row-copy">
                <strong class="item-title">{{ documentTaskLabels[task.task_type] || task.task_type }}</strong>
                <div class="row-meta">
                  <span class="status-tag" :data-status="task.status">{{ taskStatuses[task.status] || task.status }}</span>
                  <span>开始 {{ formatDate(task.created_at) }}</span><span v-if="task.finished_at">结束 {{ formatDate(task.finished_at) }}</span>
                </div>
              </div>
              <UiButton v-if="task.session_id" size="sm" as-child>
                <RouterLink :to="{ name: 'Workspace', query: { conversation: String(task.session_id), run: task.id } }">打开关联对话</RouterLink>
              </UiButton>
            </article>
            <EmptyState v-if="!tasks.length" title="暂无文档任务" :icon="History" />
          </template>
          <div v-if="!listLoading && !listError && total > pageSize" class="center-pages">
            <el-pagination v-model:current-page="pages[tab]" :page-size="pageSize" :total="total" layout="prev, pager, next" />
          </div>
        </section>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="historyOpen" :title="historyProfile?.name || historyAsset?.title || '版本历史'"
      width="min(800px, calc(100vw - 24px))" append-to-body destroy-on-close @close="closeHistory">
      <p v-if="historyLoading" class="center-state" role="status">正在加载版本历史…</p>
      <p v-else-if="historyError" class="inline-error" role="alert">{{ historyError }}</p>
      <template v-else>
        <div class="version-header">
          <label>版本历史
            <select v-model.number="versionNumber">
              <option v-for="version in historyKind === 'asset' ? documentVersions : profileVersions" :key="version.id" :value="version.version_no">
                v{{ version.version_no }} · {{ formatDate(version.created_at) }}
              </option>
            </select>
          </label>
          <span v-if="historyProfile" class="status-tag" :data-status="historyProfile.status">{{ profileStatuses[historyProfile.status] }} · {{ historyProfile.visibility === 'organization' ? '组织' : '私有' }}</span>
        </div>
        <template v-if="historyAsset && selectedDocumentVersion">
          <DocumentVersionChanges :version="selectedDocumentVersion" />
          <div class="detail-actions">
            <UiButton size="sm" @click="previewVersion(historyAsset, selectedDocumentVersion.version_no)"><Eye />预览</UiButton>
            <UiButton size="sm" :loading="downloads.has(`${historyAsset.id}:${selectedDocumentVersion.version_no}`)" @click="downloadVersion(historyAsset, selectedDocumentVersion)"><Download />下载</UiButton>
            <UiButton size="sm" as-child><RouterLink :to="editLink(historyAsset, selectedDocumentVersion.version_no)"><FilePenLine />继续修改此版本</RouterLink></UiButton>
            <UiButton v-if="historyAsset.session_id && selectedDocumentVersion.version_no === 1" size="sm" variant="ghost" as-child>
              <RouterLink :to="{ name: 'Workspace', query: { conversation: String(historyAsset.session_id), run: selectedDocumentVersion.created_by_run_id || undefined } }">打开来源对话</RouterLink>
            </UiButton>
            <UiButton size="sm" variant="danger" :loading="mutating === `asset:${historyAsset.id}`" @click="removeAsset(historyAsset)"><Trash2 />删除文档</UiButton>
          </div>
        </template>
        <template v-else-if="historyProfile && selectedProfileVersion">
          <p>{{ selectedProfileVersion.definition.description }}</p>
          <p v-if="selectedProfileVersion.change_summary" class="row-meta">修改说明 {{ selectedProfileVersion.change_summary }}</p>
          <h3 class="detail-label">章节结构</h3>
          <ol class="section-outline">
            <li v-for="section in [...selectedProfileVersion.definition.sections].sort((a, b) => a.order - b.order)" :key="section.id" :data-level="section.level">
              <strong>{{ section.title }}</strong> <span v-if="section.required" class="status-tag">必需</span>
              <p v-if="section.purpose">{{ section.purpose }}</p>
              <p v-if="section.writing_guidance">{{ section.writing_guidance }}</p>
              <p v-if="section.required_fields.length">必需字段 {{ section.required_fields.join('、') }}</p>
              <p v-if="section.source_requirements.length">来源要求 {{ section.source_requirements.join('、') }}</p>
            </li>
          </ol>
          <details v-if="selectedProfileVersion.definition.global_rules.length" class="detail-disclosure">
            <summary>全局写作规则</summary>
            <ul><li v-for="rule in selectedProfileVersion.definition.global_rules" :key="rule">{{ rule }}</li></ul>
          </details>
          <details v-if="selectedProfileVersion.definition.required_inputs.length" class="detail-disclosure">
            <summary>补充信息字段</summary>
            <ul><li v-for="field in selectedProfileVersion.definition.required_inputs" :key="field.id">
              {{ field.label }}<span v-if="field.required"> · 必填</span><span v-if="field.description"> · {{ field.description }}</span>
            </li></ul>
          </details>
          <details v-if="selectedProfileVersion.definition.uncertainties?.length" class="detail-disclosure">
            <summary>待核对规则</summary>
            <ul><li v-for="rule in selectedProfileVersion.definition.uncertainties" :key="rule">{{ rule }}</li></ul>
          </details>
          <details class="detail-disclosure" open>
            <summary>来源与提炼记录</summary>
            <p v-if="selectedProfileVersion.source_summary">{{ selectedProfileVersion.source_summary }}</p>
            <p v-if="selectedProfileVersion.extraction_confidence != null">提炼置信度 {{ new Intl.NumberFormat('zh-CN', { style: 'percent' }).format(selectedProfileVersion.extraction_confidence) }}</p>
            <p v-if="!selectedProfileVersion.source_summary && !selectedProfileVersion.source_metadata?.length">暂无来源记录</p>
            <ul v-if="selectedProfileVersion.source_metadata?.length" class="source-list">
              <li v-for="(source, index) in selectedProfileVersion.source_metadata" :key="index">
                <strong>{{ source.label || source.name || '参考资料' }}</strong>
                <span v-if="source.detail"> · {{ source.detail }}</span><span v-if="source.locator"> · {{ source.locator }}</span>
              </li>
            </ul>
          </details>
          <div v-if="historyProfile.can_manage" class="detail-actions">
            <UiButton v-if="historyProfile.status !== 'archived'" size="sm" @click="openEditor(historyProfile)"><FilePenLine />编辑当前版本</UiButton>
            <UiButton v-if="historyProfile.status !== 'published' || historyProfile.published_version !== historyProfile.current_version" size="sm" variant="primary" :disabled="!!mutating" @click="changeProfile(historyProfile, 'publish')">发布 v{{ historyProfile.current_version }}</UiButton>
            <UiButton v-if="historyProfile.status !== 'archived'" size="sm" variant="ghost" :disabled="!!mutating" @click="changeProfile(historyProfile, 'archive')"><Archive />归档</UiButton>
            <UiButton size="sm" variant="danger" :disabled="!!mutating" @click="changeProfile(historyProfile, 'remove')"><Trash2 />删除规范</UiButton>
          </div>
        </template>
        <p v-else class="center-state">暂无版本记录</p>
      </template>
    </el-dialog>
    <el-dialog v-model="previewOpen" :title="previewTitle" width="min(1040px, calc(100vw - 24px))" append-to-body destroy-on-close>
      <div v-if="previewOpen && previewTarget" class="preview-scroll">
        <DocumentPdfPreview :document-id="previewTarget.asset.id" :version="previewTarget.version" :title="previewTitle" />
      </div>
    </el-dialog>
    <el-dialog v-model="editorOpen" :title="editing ? '编辑文档规范' : '新建文档规范'"
      width="min(700px, calc(100vw - 24px))" append-to-body destroy-on-close :close-on-click-modal="!editorBusy" :close-on-press-escape="!editorBusy" :show-close="!editorBusy">
      <div class="profile-options">
        <label>可见范围
          <select v-model="visibility" :disabled="!auth.isSuperuser || editorBusy"><option value="private">私有</option><option value="organization">组织</option></select>
        </label>
        <label>修改说明（选填）<input v-model="changeSummary" maxlength="500" :disabled="editorBusy" /></label>
      </div>
      <p v-if="editorError" class="inline-error" role="alert">{{ editorError }}</p>
      <ProfileForm v-if="editorOpen" :initial="editingDefinition" :busy="editorBusy" :submit-label="editing ? '保存新版本' : '创建草稿'" @submit="saveProfile" @cancel="editorOpen = false" />
    </el-dialog>
  </div>
</template>

<style scoped>
.document-center { max-width: 1280px; }
.center-list { padding: 0 clamp(12px, 2.3vw, 24px); }
.center-row { display: flex; align-items: center; gap: 14px; padding: 15px 0; border-bottom: 1px solid var(--ui-border); }
.center-row:last-child { border-bottom: 0; }
.item-icon {
  display: grid; place-items: center; width: 42px; height: 42px; flex: none;
  color: var(--ui-accent); background: var(--ui-accent-soft); border-radius: var(--ui-radius-md);
}
.profile-icon { color: var(--ui-info); background: var(--ui-subtle); }
.row-copy { min-width: 0; flex: 1; }
.item-title { font: inherit; font-weight: 650; color: var(--ui-text); overflow-wrap: anywhere; }
button.item-title { border: 0; padding: 0; background: none; cursor: pointer; text-align: start; }
button.item-title:hover, .row-meta a:hover { text-decoration: underline; }
.row-description { margin: 3px 0; color: var(--ui-secondary); font-size: 13px; overflow-wrap: anywhere; }
.row-meta { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 10px; margin-top: 5px; color: var(--ui-muted); font-size: 12px; }
.row-meta a { color: var(--ui-accent); text-decoration: none; }
.row-actions, .detail-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.center-state { padding: 20px 0; color: var(--ui-secondary); text-align: center; }
.center-pages { display: flex; justify-content: end; padding: 12px 0; }
.status-tag { padding: 1px 6px; border-radius: var(--ui-radius-sm); background: var(--ui-subtle); color: var(--ui-secondary); font-size: 12px; }
.status-tag[data-status="published"], .status-tag[data-status="completed"] { color: var(--ui-success); background: var(--ui-success-soft); }
.status-tag[data-status="waiting_input"], .status-tag[data-status="waiting_approval"] { color: var(--ui-warning); background: var(--ui-warning-soft); }
.status-tag[data-status="failed"], .inline-error { color: var(--ui-danger); }
.version-header { display: flex; align-items: end; gap: 10px; justify-content: space-between; margin-bottom: 12px; }
.version-header label, .profile-options label { display: grid; gap: 5px; font-size: 13px; color: var(--ui-secondary); }
.version-header select, .profile-options select, .profile-options input {
  min-height: 32px; max-width: 100%; background: var(--ui-bg); border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-sm); color: var(--ui-text); padding: 4px 7px;
}
.detail-actions { margin-top: 16px; padding-top: 11px; border-top: 1px solid var(--ui-border); }
.detail-label { margin: 14px 0 4px; color: var(--ui-text); font-size: 14px; }
.detail-disclosure { padding: 8px 0; overflow-wrap: anywhere; }
.detail-disclosure summary { color: var(--ui-text); cursor: pointer; }
.detail-disclosure ul { padding-inline-start: 20px; list-style: disc; }
.source-list li { margin: 4px 0; }
.section-outline { padding-inline-start: 21px; list-style: decimal; overflow-wrap: anywhere; }
.section-outline li { margin: 7px 0; }
.section-outline li[data-level="2"] { margin-inline-start: 14px; }
.section-outline li[data-level="3"] { margin-inline-start: 28px; }
.section-outline p { margin: 2px 0; font-size: 13px; color: var(--ui-secondary); }
.preview-scroll { height: 72vh; overflow: hidden; background: var(--ui-subtle); }
.profile-options { display: grid; grid-template-columns: minmax(85px, 1fr) 3fr; gap: 12px; margin-bottom: 14px; }
@media (max-width: 720px) {
  .center-row { flex-wrap: wrap; align-items: start; gap: 8px 11px; }
  .row-copy { min-width: calc(100% - 55px); }
  .row-actions { margin-left: 53px; }
}
@media (max-width: 430px) { .row-actions { margin-left: 0; } }
</style>
