<template>
  <section class="workspace-home" :class="{ 'has-preview': preview || documentPreview }" aria-label="智能体工作空间">
    <button v-if="historyOpen" type="button" class="history-overlay" aria-label="关闭会话历史" @click="closeHistory()"></button>
    <ConversationHistory
      v-show="historyOpen"
      class="ws-history"
      :items="conversations"
      :active-id="currentConversation?.id"
      :loading="historyLoading"
      :failed="historyFailed"
      :creating="creatingConversation"
      :deleting-id="deletingId"
      :has-more="hasMoreConversations"
      :loading-more="historyLoadingMore"
      :status="conversationStatus"
      :update-metadata="updateConversationMetadata"
      @close="closeHistory"
      @retry="loadConversations()"
      @load-more="loadMoreConversations"
      @create="newConversation"
      @select="selectConversation"
      @remove="removeConversation"
    />

    <section class="ws-main" :class="{ 'is-empty': showWelcome }" aria-label="当前会话">
      <header class="thread-head">
        <button
          v-if="!historyOpen"
          ref="historyToggle"
          type="button"
          class="icon-button"
          aria-label="打开会话历史"
          aria-controls="conversation-history"
          title="打开会话历史"
          @click="openHistory"
        >
          <PanelLeft :size="17" />
        </button>
        <div class="thread-heading">
          <span>{{ currentConversation?.title || '新对话' }}</span>
        </div>
        <span class="assistant-name"><Bot :size="15" />{{ currentAssistant?.name || '通用助手' }}</span>
      </header>

      <div
        ref="messagesRef"
        class="ws-messages"
        :aria-busy="loadingMessages"
        aria-label="对话消息"
        role="region"
        tabindex="0"
        @scroll.passive="onMessagesScroll"
        @wheel.passive="onWheelScroll"
      >
        <div v-if="loadingMessages" class="thread-state" role="status">
          <LoaderCircle class="spin" :size="17" /> 正在加载消息…
        </div>
        <div v-else-if="messagesFailed" class="thread-state" role="alert">
          <CircleAlert :size="17" /> 无法加载此对话
          <button type="button" class="text-button" @click="reloadMessages">重试</button>
        </div>
        <div v-else-if="showWelcome" class="ws-welcome">
          <div class="welcome-mark"><Bot :size="26" :stroke-width="1.65" /></div>
          <h1>今天想完成什么？</h1>
          <p>向{{ currentAssistant?.name || '智能体' }}提问，或上传文件开始协作。</p>
          <div class="task-examples" aria-label="示例任务">
            <button v-for="example in examples" :key="example.label" type="button" class="task-example" @click="sendExample(example.prompt)">
              <component :is="example.icon" :size="16" />
              {{ example.label }}
            </button>
          </div>
        </div>

        <div v-else ref="threadRef" class="thread">
          <template v-for="(m, idx) in messages" :key="idx">
            <article v-if="m.role === 'user'" :id="'q-' + idx" class="msg-user" aria-label="你的消息">
              <div class="msg-bubble">
                <div v-if="m.files?.length" class="msg-files">
                  <button
                    v-for="(file, fileIdx) in m.files"
                    :key="fileIdx"
                    type="button"
                    class="msg-file"
                    :title="file.name"
                    @click="openPreview(file.path, file.name)"
                  >
                    <img v-if="file.previewUrl" :src="file.previewUrl" alt="" />
                    <FileText v-else :size="16" />
                    <span>{{ file.name }}</span>
                  </button>
                </div>
                {{ m.content }}
              </div>
            </article>

            <article v-else class="msg-assistant" aria-label="助手消息" :data-run-id="m.runId">
              <div class="assistant-mark"><Bot :size="16" /></div>
              <div class="msg-content">
                <div class="msg-heading">
                  <strong>{{ currentAssistant?.name || '助手' }}</strong>
                  <span v-if="runState(m)" class="run-state" :data-state="runState(m)?.tone">{{ runState(m)?.label }}</span>
                  <span v-if="taskTimeText(m)" class="run-duration">{{ taskTimeText(m) }}</span>
                </div>
                <template v-for="(block, blockIdx) in messageBlocks[idx]"
                  :key="block.kind === 'tool' ? block.key : block.kind === 'input_request' ? block.request.request_id
                    : block.kind === 'document_edit_review' ? `review-${block.review.ordinal}` : blockIdx">
                  <MessageMarkdown v-if="block.kind === 'text'" :text="block.text" />
                  <ToolStep v-else-if="block.kind === 'tool'" :tool="block" @preview="openPreview" />
                  <TeamRunCard v-else-if="block.kind === 'team'" :progress="block.progress" :active="Boolean(m.streaming)" />
                  <VerificationCard v-else-if="block.kind === 'verification'" :report="block" />
                  <ProjectDeliveryCard v-else-if="block.kind === 'project'"
                    :delivery="block.delivery" :progress="block.progress" :active="Boolean(m.streaming)"
                    :repairable="!!m.runId && m.runStatus === 'failed' && block.delivery?.state === 'failed'"
                    :repair-disabled="!!blockedReason || sending || loadingMessages || messagesFailed"
                    :repairing="repairingProject === m.runId && !!m.runId" :repair-error="projectRepairErrors.get(m.runId || '')"
                    @repair="repairProjectDraft(m)" />
                  <DocumentArtifactCard
                    v-else-if="block.kind === 'document_artifact'" :artifact="block.artifact"
                    :downloading="downloadingDocuments.has(block.artifact.document_version_id)"
                    @preview="openDocumentPreview(block.artifact)" @download="downloadArtifact(block.artifact)"
                    @edit="continueDocument(block.artifact)"
                  />
                  <ProfileDraftCard v-else-if="block.kind === 'document_profile_draft'"
                    :definition="block.definition" :uncertainties="block.uncertainties" :warnings="block.warnings"
                    :saved="block.saved" :input="profileConfirmation(m)" :busy="documentInputsBusy.has(m.runId || '')"
                    :error="documentInputErrors.get(m.runId || '')"
                    @confirm="confirmProfile(m, $event)" @cancel="cancelDocumentInput(m)"
                  />
                  <DocumentEditReviewCard v-else-if="block.kind === 'document_edit_review'"
                    :review="block.review" :decision="block.decision"
                    :applied="m.blocks.some((item) => item.kind === 'document_artifact')"
                    :state="['failed', 'cancelled', 'suspended', 'limit_exceeded'].includes(m.runStatus || '')
                      ? 'closed' : (documentReviewInput(m, block)?.state ?? (m.streaming ? undefined : 'closed'))"
                    :busy="documentInputsBusy.has(m.runId || '')" :error="documentInputErrors.get(m.runId || '')"
                    @submit="submitDocumentReview(m, block, $event)"
                  />
                  <InputRequestCard
                    v-else-if="block.kind === 'input_request' && !isInlineProfileConfirmation(m, block.request)
                      && !isDocumentReviewInput(block.request)"
                    :request="block.request" :state="block.state" :busy="documentInputsBusy.has(m.runId || '')"
                    :error="documentInputErrors.get(m.runId || '')"
                    @submit="submitDocumentInput(m, block.request.request_id, $event)" @cancel="cancelDocumentInput(m)"
                  />
                  <div v-else-if="block.kind === 'document_stage'" class="document-stage" :data-tone="block.tone">
                    <FileText :size="14" /><span>{{ block.label }}<template v-if="block.detail"> · {{ block.detail }}</template></span>
                  </div>

                  <details v-else-if="block.kind === 'plan'" class="process-disclosure" open>
                    <summary><ListTodo :size="15" />任务计划 <span>{{ planItems(block.todos).length }} 项</span></summary>
                    <ul>
                      <li v-for="(todo, todoIdx) in planItems(block.todos)" :key="todoIdx">
                        <component :is="planIcon(todo.status)" :size="14" :class="'plan-' + todo.status" />
                        <span :class="{ 'plan-done': todo.status === 'completed' }">{{ todo.content }}</span>
                      </li>
                    </ul>
                  </details>

                  <details v-else-if="block.kind === 'group'" class="process-disclosure">
                    <summary>
                      <Layers :size="14" /> {{ groupSummary(block) }}
                      <span v-if="groupFailCount(block)" class="group-failure">{{ groupFailCount(block) }} 项失败</span>
                    </summary>
                    <div class="tool-group-items">
                      <ToolStep v-for="tool in block.items" :key="tool.key" :tool="tool" @preview="openPreview" />
                    </div>
                  </details>
                </template>

                <div v-if="m.streaming && !m.blocks.some((block) => (block.kind === 'team' && teamPhaseLabels[block.progress.phase] === m.liveStatus)
                    || (block.kind === 'project' && block.progress && !block.delivery && projectPhaseLabels[block.progress.phase] === m.liveStatus))"
                  class="live-status" role="status">
                  <LoaderCircle class="spin" :size="14" />
                  {{ m.liveStatus || '正在处理…' }}
                </div>
                <p v-if="staleSeconds(m) >= 8" class="stale-hint">已 {{ staleSeconds(m) }} 秒无新进展，任务仍在执行。</p>

                <div v-if="executionPause(m)" class="msg-pause" role="status">
                  <CirclePause :size="17" aria-hidden="true" />
                  <span>{{ executionPause(m)?.text }}</span>
                  <button v-if="m.runId && idx === messages.length - 1 && !m.streaming && executionPause(m)?.canContinue"
                    type="button" class="continue-button" :disabled="!!blockedReason || sending || !!uploading || loadingMessages || messagesFailed"
                    @click="sendMessage(true)">继续任务</button>
                </div>
                <div v-else-if="m.error" class="msg-error" role="alert"><CircleAlert :size="16" />{{ m.error }}</div>
                <ApprovalPanel
                  v-if="m.runId && m.pendingActions?.length && currentConversation"
                  :key="m.runId"
                  :actions="m.pendingActions"
                  :deciding="pendingDecisions.get(m.runId)"
                  @decide="decideApproval(currentConversation.id, m, $event)"
                />
                <div v-if="!m.streaming && (m.runId || m.error || answerText(m))" class="message-actions">
                  <button v-if="answerText(m)" type="button" class="icon-button" aria-label="复制回复" title="复制回复" @click="copyAnswer(m)">
                    <Copy :size="15" />
                  </button>
                  <button v-if="m.runId" type="button" class="trace-link" @click="openTrace(m.runId)">
                    <ExternalLink :size="14" /> 执行 Trace
                  </button>
                  <button v-if="m.documentRetryable && m.runStatus === 'failed'" type="button" class="trace-link"
                    :disabled="documentInputsBusy.has(m.runId || '')" @click="retryFailedDocument(m)">
                    <RotateCcw :size="14" /> 继续文档任务
                  </button>
                  <button v-if="m.error && messages[idx - 1]?.role === 'user'" type="button" class="trace-link" @click="editRetry(idx - 1)">
                    <RotateCcw :size="14" /> 编辑并重试
                  </button>
                </div>
              </div>
            </article>
          </template>
        </div>
      </div>

      <nav v-if="questionAnchors.length > 1" class="question-nav" aria-label="提问导航">
        <el-tooltip v-for="(question, index) in questionAnchors" :key="question.idx" :content="question.preview" placement="left" popper-class="workspace-question-tooltip">
          <button type="button" :aria-label="`跳到第 ${index + 1} 个提问：${question.preview}`" @click="jumpToQuestion(question.idx)"></button>
        </el-tooltip>
      </nav>

      <div class="composer-dock">
        <button v-if="showJumpToLatest" type="button" class="latest-button" @click="forceScrollToBottom">
          <ArrowDown :size="15" /> 跳到最新消息
        </button>
        <ConversationComposer
          ref="composer"
          v-model:text="inputText"
          v-model:team-mode="teamMode"
          v-model:approval-mode="approvalMode"
          v-model:model="selectedModelId"
          :options="modelOptions"
          :attachments="attachments"
          :uploading="uploading"
          :sending="sending"
          :stoppable="streaming || !!historyRun"
          :blocked-reason="blockedReason"
          :locked="loadingMessages || messagesFailed"
          @send="sendMessage"
          @stop="stopGeneration"
          @upload="uploadAttachments"
          @remove="removeAttachment"
          @preview="openPreview"
        >
          <template v-if="documentContext" #document-context>
            <div class="document-context">
              <FileText :size="15" /><span>{{ documentContext }}</span>
              <button type="button" @click="resetDocumentTask" aria-label="清除文档任务"><X :size="14" /></button>
            </div>
          </template>
          <template #document-task>
            <DocumentTaskPicker v-model="documentTask" :disabled="!!blockedReason || loadingMessages || messagesFailed" />
          </template>
        </ConversationComposer>
      </div>
    </section>

    <DocumentPreviewPanel v-if="documentPreview" :artifact="documentPreview"
      @close="closePreview" @download="downloadArtifact(documentPreview)" />
    <FilePreviewPanel
      v-else-if="preview"
      :data="preview.data"
      :name="preview.name"
      :loading="preview.loading"
      :error="preview.error"
      @close="closePreview"
      @download="downloadPreview"
      @open-original="openPreview"
    />
  </section>
</template>

<script setup lang="ts">
/**
 * 统一智能体对话（任务书 §2.2）：
 * - 登录后直接对话（默认助手，UX-01）；
 * - 流式执行体验：工具小步骤实时穿插追加在对话流尾部，每步可点开详情；
 * - 执行中可自由切换会话/页面：任务后台继续跑，会话独立状态互不干扰（UX-10）；
 * - 取消走后端 /runs/{id}/cancel（UX-07）；
 * - 断线重连 + Last-Event-ID 续传（UX-08）；
 * - 审批按 action_id 决策（UX-09）。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowDown, Bot, ChartNoAxesCombined, Circle, CircleAlert, CircleCheck, CircleDot, CirclePause,
  Copy, ExternalLink, FilePenLine, FileText, Layers, ListTodo, LoaderCircle, PanelLeft, RotateCcw, Search, X,
} from '@lucide/vue'
import { ElMessage } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { modelApi } from '@/api'
import {
  conversationApi, consumeRunEvents, runApi, workspaceApi,
  type Assistant, type Conversation, type ConversationMetadata, type PreviewContent,
} from '@/api/v2'
import {
  acceptDocInput, addDocInput, applyRunEvent, documentReviewInput, executionPause, finalizeRunMessage, hasVisibleText,
  isDocumentReviewInput, pendingDocInput, recordDocumentReview,
  type DocumentEditReviewBlock, type MsgBlock, type RunStreamMessage, type ToolBlock,
} from '@/features/run/blocks'
import ToolStep from '@/features/run/ToolStep.vue'
import ApprovalPanel from '@/features/run/ApprovalPanel.vue'
import MessageMarkdown from '@/features/run/MessageMarkdown.vue'
import TeamRunCard from '@/features/run/TeamRunCard.vue'
import { teamPhaseLabels } from '@/features/run/teamProgress'
import ProjectDeliveryCard from '@/features/run/ProjectDeliveryCard.vue'
import VerificationCard from '@/features/run/VerificationCard.vue'
import { projectPhaseLabels } from '@/features/run/projectDelivery'
import FilePreviewPanel from '@/features/preview/FilePreviewPanel.vue'
import { filePreviewError } from '@/features/preview/previewError'
import DocumentArtifactCard from '@/features/documents/DocumentArtifactCard.vue'
import DocumentEditReviewCard from '@/features/documents/DocumentEditReviewCard.vue'
import DocumentPreviewPanel from '@/features/documents/DocumentPreviewPanel.vue'
import DocumentTaskPicker from '@/features/documents/DocumentTaskPicker.vue'
import InputRequestCard from '@/features/documents/InputRequestCard.vue'
import ProfileDraftCard from '@/features/documents/ProfileDraftCard.vue'
import { isProfileConfirmation } from '@/features/documents/inputRequest'
import { documentTaskPayload, type DocumentTaskSelection } from '@/features/documents/taskModes'
import { documentError, documentProfilesApi, documentsApi, downloadDocument, type DocumentArtifact, type InputRequest, type ProfileDefinition } from '@/api/documents'
import { useAuthStore } from '@/stores/auth'
import ConversationComposer from './ConversationComposer.vue'
import ConversationHistory from './ConversationHistory.vue'
import { messageAttachments, type MessageAttachment } from './attachmentHistory'
import { hasScrolledAway, isNearLatest } from './interactions'

interface ChatMessage extends RunStreamMessage {
  role: 'user' | 'assistant'
  content: string
  runId?: string
  task?: DocumentTaskSelection
  files?: MessageAttachment[]
}

/** 折叠后的连续工具步骤组（完成后折叠防刷屏） */
interface ToolGroupItem {
  kind: 'group'
  items: ToolBlock[]
}
type DisplayItem = MsgBlock | ToolGroupItem

interface ActiveRun {
  abort: AbortController
  stopped: boolean
  msg: ChatMessage
}

// 每会话独立消息存储：切换会话不打断后台运行，回来即见最新进度
const convMessages = reactive(new Map<number, ChatMessage[]>())
const messages = computed<ChatMessage[]>(() =>
  currentConversation.value ? convArr(currentConversation.value.id) : [],
)
const messagesRef = ref<HTMLElement>()
const threadRef = ref<HTMLElement>()
const inputText = ref('')
const route = useRoute()
const router = useRouter()
const documentTask = ref<DocumentTaskSelection>({ mode: 'auto' })
const documentContext = computed(() => {
  const { documentId, profileId, title, version } = documentTask.value
  if (documentId) return `修改「${title || `文档 #${documentId}`}」${version ? ` · v${version}` : ''}`
  if (profileId) return `按「${title || `规范 #${profileId}`}」生成`
  return ''
})
const documentPreview = ref<DocumentArtifact | null>(null)
const downloadingDocuments = reactive(new Set<number>())
const documentInputsBusy = reactive(new Set<string>())
const documentInputErrors = reactive(new Map<string, string>())
const composer = ref<InstanceType<typeof ConversationComposer>>()
const historyToggle = ref<HTMLButtonElement>()
const historyMedia = window.matchMedia('(min-width: 850px)')
const historyOpen = ref(historyMedia.matches)
const historyLoading = ref(false)
const historyFailed = ref(false)
const historyLoadingMore = ref(false)
const historyTotal = ref(0)
const historyLoadedPages = ref(1)
const historyPageSize = 50
const hasMoreConversations = computed(() => historyLoadedPages.value * historyPageSize < historyTotal.value)
const creatingConversation = ref(false)
const deletingId = ref<number | null>(null)
const loadingMessagesFor = reactive(new Set<number>())
const failedMessagesFor = reactive(new Set<number>())
const loadingMessages = computed(() => loadingMessagesFor.has(currentConversation.value?.id ?? -1))
const messagesFailed = computed(() => failedMessagesFor.has(currentConversation.value?.id ?? -1))
const showWelcome = computed(() => !messages.value.length && !loadingMessages.value && !messagesFailed.value)
const sending = ref(false)
const repairingProject = ref('')
const projectRepairErrors = reactive(new Map<string, string>())
const projectRepairKeys = new Map<string, string>()
const uploading = ref(0)
let creating: Promise<Conversation> | undefined
let historyRevision = 0
let requestedHistoryPages = 1
let inactive = false

const examples = [
  { label: '分析数据', icon: ChartNoAxesCombined, prompt: '帮我分析这份数据的趋势并给出结论' },
  { label: '研究主题', icon: Search, prompt: '搜索行业最新动态并生成结构化研究摘要' },
  { label: '整理报告', icon: FilePenLine, prompt: '帮我把这段内容整理成一份正式报告' },
]

function syncConversationUrl(id?: number) {
  if (route.name !== 'Workspace' || route.query.conversation === (id ? String(id) : undefined)) return
  const query = { ...route.query }
  if (id) query.conversation = String(id)
  else delete query.conversation
  if (query.run) delete query.run
  void router.replace({ query })
}

async function openLinkedConversation() {
  const id = numericQuery(route.query.conversation)
  if (!id || historyLoading.value || inactive) return
  if (currentConversation.value?.id === id) {
    if (route.query.run) void focusLinkedRun()
    return
  }
  const known = conversations.value.find((conversation) => conversation.id === id)
  if (known) {
    await selectConversation(known)
    return
  }
  try {
    const item = await conversationApi.get(id)
    if (inactive || numericQuery(route.query.conversation) !== id) return
    if (!conversations.value.some((conversation) => conversation.id === id)) conversations.value.push(item)
    await selectConversation(item)
  } catch {
    if (!inactive && numericQuery(route.query.conversation) === id) ElMessage.error('无法打开关联会话')
  }
}

watch(() => [route.query.conversation, route.query.run], openLinkedConversation)

function numericQuery(value: unknown) {
  if (typeof value !== 'string' || !/^\d+$/.test(value)) return undefined
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : undefined
}

async function syncLinkedDocument() {
  const id = numericQuery(route.query.document)
  const profile = numericQuery(route.query.profile)
  if (!id && !profile) return
  const selection: DocumentTaskSelection = id
    ? { mode: 'document_edit', documentId: id, version: numericQuery(route.query.version) }
    : { mode: 'document_generate', profileId: profile }
  documentTask.value = selection
  const inserted = documentTask.value
  try {
    const title = id ? (await documentsApi.get(id)).title : profile ? (await documentProfilesApi.get(profile)).name : ''
    if (documentTask.value === inserted) documentTask.value = { ...selection, title }
  } catch { /* 仍展示链接中的文档或规范标识，任务创建会校验可见性。 */ }
}

watch(() => [route.query.document, route.query.version, route.query.profile], syncLinkedDocument, { immediate: true })

function openHistory() {
  historyOpen.value = true
  nextTick(() => document.querySelector<HTMLElement>('#conversation-history button')?.focus())
}

function closeHistory(restoreFocus = true) {
  historyOpen.value = false
  if (restoreFocus) nextTick(() => historyToggle.value?.focus())
}

function syncHistoryVisibility(event: MediaQueryListEvent) {
  historyOpen.value = event.matches
}

// 当前会话是否有活跃 Run（各会话互不影响，可并行执行）
// 注意：entry 含 AbortController 等类实例且需要引用相等比较，必须用普通 Map；
// 响应式仅体现在 runningConvIds（供模板/计算属性跟踪）
const activeRuns = new Map<number, ActiveRun>()
const pendingDecisions = reactive(new Map<string, 'approve' | 'reject'>())
const runningConvIds = ref<number[]>([])

function setActiveRun(convId: number, entry: ActiveRun) {
  activeRuns.set(convId, entry)
  if (!runningConvIds.value.includes(convId)) {
    runningConvIds.value = [...runningConvIds.value, convId]
  }
}

function clearActiveRun(convId: number, entry?: ActiveRun) {
  const cur = activeRuns.get(convId)
  if (entry && cur !== entry) return
  activeRuns.delete(convId)
  runningConvIds.value = runningConvIds.value.filter((x) => x !== convId)
}

const streaming = computed(() =>
  !!currentConversation.value && runningConvIds.value.includes(currentConversation.value.id))
const historyRun = computed(() => {
  const runId = currentConversation.value?.active_run_id
  if (!runId) return
  return messages.value.find((m) => m.runId === runId && m.streaming && !m.pendingActions?.length && !pendingDocInput(m))
})
const blockedReason = computed(() => {
  const request = messages.value.map(pendingDocInput).find(Boolean)?.request
  if (request) return isDocumentReviewInput(request) ? '当前有修改稿待处理，可在消息中查看、保存或放弃。' : '请先处理消息中的文档补充请求。'
  if (streaming.value) return '任务运行中，可以停止生成。'
  if (messages.value.some((m) => m.pendingActions?.length)) return '请先处理消息中的审批请求。'
  if (messages.value.some((m) => m.streaming)) return '正在恢复执行进度…'
  return ''
})

const conversations = ref<Conversation[]>([])
const currentConversation = ref<Conversation | null>(null)
const authStore = useAuthStore()
const draftTeamMode = ref(false)
const rememberedTeamModes = reactive(new Map<string, boolean>())

function teamPreferenceKey(conversationId: number): string | undefined {
  const userId = authStore.user?.id
  if (userId !== undefined) return `workspace:team:${userId}:${conversationId}`
}

function rememberTeamMode(conversationId: number, enabled: boolean) {
  const key = teamPreferenceKey(conversationId)
  if (!key) return
  rememberedTeamModes.set(key, enabled)
  try {
    if (enabled) sessionStorage.setItem(key, '1')
    else sessionStorage.removeItem(key)
  } catch { /* 浏览器禁用会话存储时仍使用当前页面状态 */ }
}

const teamMode = computed({
  get() {
    const conversation = currentConversation.value
    if (!conversation) return draftTeamMode.value
    const key = teamPreferenceKey(conversation.id)
    if (!key) return false
    const remembered = rememberedTeamModes.get(key)
    if (remembered !== undefined) return remembered
    try { return sessionStorage.getItem(key) === '1' } catch { return false }
  },
  set(enabled: boolean) {
    if (currentConversation.value) rememberTeamMode(currentConversation.value.id, enabled)
    else draftTeamMode.value = enabled
  },
})
/** 当前对话使用的助手：固定为 bootstrap 下发的默认助手（不再提供助手管理入口） */
const currentAssistant = ref<Assistant | null>(null)

function conversationStatus(c: Conversation) {
  if (runningConvIds.value.includes(c.id)) return '执行中'
  const cached = convMessages.get(c.id)
  const request = cached?.map(pendingDocInput).find(Boolean)?.request
  if (request) return isDocumentReviewInput(request) ? '等待审阅' : '等待补充'
  if (cached?.some((message) => message.pendingActions?.length)) return '等待审批'
  if (cached?.some((message) => message.streaming)) return '恢复进度中'
  return c.active_run_id && !cached ? '有活跃任务' : ''
}

// 权限模块：审批模式（manual=高危工具人工审批；auto=自动放行不询问），随 Run 创建下发
const approvalMode = ref<'manual' | 'auto'>(
  localStorage.getItem('approval_mode') === 'auto' ? 'auto' : 'manual',
)
watch(approvalMode, (v) => localStorage.setItem('approval_mode', v))

// 模型选择（手动选模型仅本次 Run 生效；0 = 助手默认模型）
const modelOptions = ref<Array<{ id: number; name: string; display_name?: string | null }>>([])
const selectedModelId = ref<number>(0)

// 待发送附件（上传即写入会话工作区，随消息告知模型读取；切换会话即清空）
const attachments = ref<Array<{
  name: string; path: string; text_path: string | null; size: number
  /** 图片本地预览 URL（URL.createObjectURL） */
  previewUrl?: string
}>>([])

// 停滞检测：长时间无新事件时明确提示"仍在执行"，避免误以为卡死
const clock = ref(Date.now())
let clockTimer: number | undefined

function staleSeconds(m: ChatMessage): number {
  if (!m.streaming || !m.lastEventAt) return 0
  return Math.max(0, Math.floor((clock.value - m.lastEventAt) / 1000))
}

// ── 任务耗时（对齐 Trae「任务耗时 7m 4s」）────────────────────────────────────

function formatDuration(ms: number): string {
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rs = s % 60
  return rs ? `${m}m ${rs}s` : `${m}m`
}

/** 任务耗时文本：运行中实时累加，终态固化；无 run 起点则空 */
function taskTimeText(m: ChatMessage): string {
  if (m.runDurationMs && m.runDurationMs > 0) return formatDuration(m.runDurationMs)
  if (m.streaming && m.runStartedAt && m.runStartedAt > 0) {
    return formatDuration(Math.max(0, clock.value - m.runStartedAt))
  }
  return ''
}

const statuses: Record<string, { label: string; tone: string }> = {
  pending: { label: '排队中', tone: 'info' },
  running: { label: '执行中', tone: 'info' },
  waiting_approval: { label: '等待审批', tone: 'warning' },
  waiting_input: { label: '等待补充', tone: 'warning' },
  waiting_document_review: { label: '等待审阅', tone: 'warning' },
  needs_reconciliation: { label: '等待处理', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已停止', tone: 'neutral' },
  suspended: { label: '已挂起', tone: 'warning' },
  limit_exceeded: { label: '已暂停', tone: 'warning' },
}

function runState(message: ChatMessage) {
  const request = pendingDocInput(message)?.request
  if (request) return isDocumentReviewInput(request) ? statuses.waiting_document_review : statuses.waiting_input
  if (message.pendingActions?.length) return statuses.waiting_approval
  if (message.streaming && message.runStatus !== 'pending') return statuses.running
  if (message.runStatus === 'limit_exceeded') return statuses.limit_exceeded
  if (message.error && message.blocks.some(block => block.kind === 'verification' && block.state === 'incomplete')) {
    return { label: '验收未通过', tone: 'warning' }
  }
  if (message.error) return statuses.failed
  return message.runId ? statuses[message.runStatus || ''] : undefined
}

function answerText(message: ChatMessage) {
  return message.blocks.flatMap((block) => block.kind === 'text' ? [block.text] : []).join('\n\n')
}

async function copyAnswer(message: ChatMessage) {
  try {
    await navigator.clipboard.writeText(answerText(message))
    ElMessage.success('回复已复制')
  } catch {
    ElMessage.error('无法复制回复')
  }
}

/**
 * 粘底跟随（滚动条体验）：
 * - 任务开始 / 发送 / 切换会话：回到对话末端并持续跟随最新内容（无需手动下拉）；
 * - 用户上翻查看历史：停止跟随（绝不强行跳底）；
 * - 用户滚回底部：自动恢复跟随。
 * 旧实现按"距底 <160px"判定跟随——一次大段渲染（批量增量/工具块插入）就会
 * 超出阈值导致从此不再自动滚动。粘底标志表达的是"用户意图"，与内容涨幅无关。
 */
const stickToBottom = ref(true)
const showJumpToLatest = ref(false)
let messageResize: ResizeObserver | undefined
/** 最近一次钉底时的 scrollTop：内容撑开不改变 scrollTop，用户上翻才会 */
let lastPinnedTop = -1

function onMessagesScroll() {
  const el = messagesRef.value
  if (!el) return
  stickToBottom.value = isNearLatest(el)
  showJumpToLatest.value = !stickToBottom.value
}

/** 滚轮上翻 = 明确的"离开底部"意图，同步解除粘底（scroll 事件派发晚于微任务，存在竞态） */
function onWheelScroll(e: WheelEvent) {
  if (e.deltaY < 0) stickToBottom.value = false
}

/** 跟随最新内容（仅粘底状态下生效，不打断阅读历史） */
async function scrollToBottom() {
  await nextTick()
  const el = messagesRef.value
  if (!el || !stickToBottom.value) return
  // 粘底判定必须放在滚动前一刻：若在 nextTick 之前检查，上翻瞬间在途的
  // 跟随调用会把用户强行拽回底部（并经 scroll 事件把粘底标志重新武装）
  // 双保险：scrollTop 被拽离最近钉底位置 = 用户上翻（内容撑开不会改 scrollTop）
  if (hasScrolledAway(el, lastPinnedTop)) {
    stickToBottom.value = false
    showJumpToLatest.value = true
    return
  }
  el.scrollTop = el.scrollHeight
  lastPinnedTop = el.scrollTop
}

/** 强制回到底部并恢复跟随（切换会话/发送消息/任务开始等明确意图） */
async function forceScrollToBottom() {
  stickToBottom.value = true
  showJumpToLatest.value = false
  await nextTick()
  const el = messagesRef.value
  if (el) {
    el.scrollTop = el.scrollHeight
    lastPinnedTop = el.scrollTop
  }
}

async function focusLinkedRun() {
  const id = route.query.run
  if (typeof id !== 'string') return false
  await nextTick()
  const target = Array.from(threadRef.value?.querySelectorAll<HTMLElement>('[data-run-id]') || [])
    .find((message) => message.dataset.runId === id)
  const viewport = messagesRef.value
  if (!target || !viewport) return false
  stickToBottom.value = false
  viewport.scrollTop += target.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 12
  return true
}

// ── 提问锚点导航（右侧纵向圆点：点击跳转对应提问，悬浮气泡预览内容）────────

const questionAnchors = computed(() =>
  messages.value
    .map((m, idx) => ({ m, idx }))
    .filter(({ m }) => m.role === 'user')
    .map(({ m, idx }) => ({ idx, preview: questionPreview(m.content) || '附件消息' })),
)

function questionPreview(text: string): string {
  const t = (text || '').replace(/\s+/g, ' ').trim()
  return t.length > 80 ? `${t.slice(0, 80)}…` : t
}

function jumpToQuestion(idx: number) {
  const el = messagesRef.value
  const target = el?.querySelector<HTMLElement>(`#q-${idx}`)
  if (!el || !target) return
  // 跳到历史提问 = 阅读意图：解除粘底，避免流式更新把视图拽回底部
  stickToBottom.value = false
  // 直接赋值定位：长会话（数万 px）下元素级 scrollTo({behavior:'smooth'})
  // 不产生滚动（实测无效），瞬时跳转对长距离导航也更利落
  el.scrollTop =
    el.scrollTop + target.getBoundingClientRect().top - el.getBoundingClientRect().top - 12
}

// ── 块流展示（工具步骤实时穿插；完成后连续步骤折叠防刷屏）──────────────────

function groupFailCount(g: ToolGroupItem): number {
  return g.items.filter((i) => i.state === 'failed').length
}

/**
 * 工具分组摘要（对齐 Trae）：按类别计数，如「已读取 3 个文件，执行 1 条命令」，
 * 比抽象的「N 个工具调用」更直观反映执行过程。
 */
function groupSummary(g: ToolGroupItem): string {
  const read = ['read_file']
  const search = ['ls', 'glob', 'grep']
  const exec = ['execute', 'python_exec']
  const write = ['write_file', 'edit_file']
  const net = ['web_search', 'http_request']
  const del = ['delete_file', 'delete']
  const count = (names: string[]) => g.items.filter((i) => names.includes(i.name)).length
  const parts: string[] = []
  const nRead = count(read)
  const nSearch = count(search)
  const nExec = count(exec)
  const nWrite = count(write)
  const nNet = count(net)
  const nDel = count(del)
  if (nRead) parts.push(`已读取 ${nRead} 个文件`)
  if (nSearch) parts.push(`搜索 ${nSearch} 次`)
  if (nExec) parts.push(`执行 ${nExec} 条命令`)
  if (nWrite) parts.push(`已编辑 ${nWrite} 个文件`)
  if (nNet) parts.push(`网络请求 ${nNet} 次`)
  if (nDel) parts.push(`删除 ${nDel} 个文件`)
  const known = nRead + nSearch + nExec + nWrite + nNet + nDel
  if (known < g.items.length) parts.push(`${g.items.length - known} 个其他操作`)
  return parts.join('，') || `${g.items.length} 个工具调用`
}

/** 计划清单可见条目（无内容条目不渲染，防白板） */
function planItems(todos: any[]): any[] {
  return (todos || []).filter((t) => hasVisibleText(String(t?.content ?? '')))
}

function displayBlocks(m: ChatMessage): DisplayItem[] {
  // 空白文本块（模型偶发的纯空白/零宽增量）与空计划块不渲染，避免"白板区块"
  const blocks = m.blocks.filter((b) => {
    if (b.kind === 'text') return hasVisibleText(b.text.replace(/<!--.*?-->/gs, ''))
    if (b.kind === 'plan') return planItems(b.todos).length > 0
    return true
  })
  if (m.streaming) return blocks // 执行中逐步实时展示，不折叠
  const out: DisplayItem[] = []
  let i = 0
  while (i < blocks.length) {
    const b = blocks[i]
    if (b.kind === 'tool') {
      let j = i
      while (j < blocks.length && blocks[j].kind === 'tool') j++
      const group = blocks.slice(i, j) as ToolBlock[]
      if (group.length > 3) {
        out.push({ kind: 'group', items: group })
      } else {
        out.push(...group)
      }
      i = j
    } else {
      out.push(b)
      i++
    }
  }
  return out
}

const messageBlocks = computed(() => messages.value.map(displayBlocks))

function planIcon(status: string) {
  if (status === 'completed') return CircleCheck
  if (status === 'in_progress') return CircleDot
  return Circle
}

// ── 初始化 ────────────────────────────────────────────────────────────────────

onMounted(async () => {
  clockTimer = window.setInterval(() => { clock.value = Date.now() }, 1000)
  window.addEventListener('keydown', onGlobalKeydown)
  historyMedia.addEventListener('change', syncHistoryVisibility)
  messageResize = new ResizeObserver(() => { void scrollToBottom() })
  if (messagesRef.value) messageResize.observe(messagesRef.value)
  workspaceApi.bootstrap().then((boot) => {
    currentAssistant.value = boot.default_assistant
  }).catch(() => { /* 默认助手仍可由服务端选择 */ })
  modelApi.listConfigs({ model_type: 'chat' }).then((list) => {
    modelOptions.value = (list || []).filter((m: any) => m.is_active)
  }).catch(() => { /* 模型列表加载失败不阻断对话 */ })
  await loadConversations()
  openLinkedConversation()
})

watch(threadRef, (thread, previous) => {
  if (previous) messageResize?.unobserve(previous)
  if (thread) messageResize?.observe(thread)
})

onUnmounted(() => {
  inactive = true
  if (clockTimer) clearInterval(clockTimer)
  window.removeEventListener('keydown', onGlobalKeydown)
  historyMedia.removeEventListener('change', syncHistoryVisibility)
  messageResize?.disconnect()
  for (const entry of activeRuns.values()) {
    entry.stopped = true
    entry.abort.abort()
  }
  runningConvIds.value = []
  closePreview()
  for (const url of attachmentUrls) URL.revokeObjectURL(url)
  for (const controller of historyStreams.values()) controller.abort()
})

async function loadConversations(refresh = false) {
  const revision = ++historyRevision
  const pageCount = requestedHistoryPages
  if (!refresh) historyLoading.value = true
  historyFailed.value = false
  try {
    const pages = await Promise.all(Array.from({ length: pageCount }, (_, index) => conversationApi.list({
      page: index + 1, page_size: historyPageSize,
    })))
    if (revision === historyRevision) {
      const previous = currentConversation.value
      const rows = pages.flatMap((page) => page.items || [])
      conversations.value = [...new Map(rows.map((row) => [row.id, row])).values()]
      historyTotal.value = pages[0].total
      historyLoadedPages.value = pageCount
      const refreshed = conversations.value.find((item) => item.id === currentConversation.value?.id)
      if (refreshed) currentConversation.value = refreshed
      else if (previous) conversations.value.push(previous)
    }
  } catch {
    if (revision === historyRevision && !conversations.value.length) historyFailed.value = true
  } finally {
    if (revision === historyRevision) historyLoading.value = false
  }
}

async function loadMoreConversations() {
  if (!hasMoreConversations.value || historyLoadingMore.value) return
  historyLoadingMore.value = true
  requestedHistoryPages = historyLoadedPages.value + 1
  try {
    await loadConversations(true)
  } finally {
    requestedHistoryPages = historyLoadedPages.value
    historyLoadingMore.value = false
  }
}

async function updateConversationMetadata(item: Conversation, changes: ConversationMetadata) {
  try {
    const updated = await conversationApi.update(item.id, changes)
    if (inactive) return
    conversations.value = conversations.value.map((row) => row.id === item.id ? updated : row)
    if (currentConversation.value?.id === item.id) currentConversation.value = updated
    void loadConversations(true)
  } catch (error) {
    throw new Error(documentError(error, changes.title !== undefined ? '重命名失败，请重试' : '更新置顶状态失败，请重试'))
  }
}

// ── 会话管理（执行中自由切换；后台运行互不干扰）──────────────────────────────

function convArr(convId: number): ChatMessage[] {
  if (!convMessages.has(convId)) convMessages.set(convId, [])
  return convMessages.get(convId)!
}

function pushMsg(convId: number, msg: ChatMessage): ChatMessage {
  const arr = convArr(convId)
  arr.push(msg)
  return arr[arr.length - 1] // 返回响应式代理，后续变更才能触发视图更新
}

async function newConversation() {
  composer.value?.cancelDictation()
  resetDocumentTask()
  try {
    const c = await createConversation()
    if (currentConversation.value?.id === c.id) focusConversation()
  } catch { /* 请求层提示失败，用户可重试 */ }
}

/** 无会话时（上传附件 / 首条消息）自动创建会话 */
function createConversation(transferDraftTeam = false): Promise<Conversation> {
  if (creating) return creating
  const origin = currentConversation.value?.id
  const inheritTeam = transferDraftTeam && origin === undefined && draftTeamMode.value
  creatingConversation.value = true
  creating = conversationApi.create({ assistant_id: currentAssistant.value?.id }).then((c) => {
    conversations.value.unshift(c)
    convMessages.set(c.id, [])
    if (currentConversation.value?.id === origin) {
      switchConversation(c)
      if (inheritTeam) rememberTeamMode(c.id, true)
    }
    void loadConversations(true)
    return c
  }).finally(() => {
    creating = undefined
    creatingConversation.value = false
  })
  return creating
}

function ensureConversation() {
  return currentConversation.value ?? createConversation(true)
}

function disposeAttachments() {
  for (const file of attachments.value) releaseAttachmentUrl(file.previewUrl)
  attachments.value = []
}

function switchConversation(c: Conversation | null) {
  composer.value?.cancelDictation()
  disposeAttachments()
  closePreview()
  if (currentConversation.value && currentConversation.value.id !== c?.id) {
    resetDocumentTask()
  }
  draftTeamMode.value = false
  currentConversation.value = c
  syncConversationUrl(c?.id)
  showJumpToLatest.value = false
}

function focusConversation() {
  if (!historyMedia.matches) closeHistory(false)
  nextTick(() => composer.value?.focus())
}

function reloadMessages() {
  if (currentConversation.value) void selectConversation(currentConversation.value)
}

const historyStreams = new Map<string, AbortController>()

function replayConversationRun(convId: number, runId: string, content = '') {
  const msg = pushMsg(convId, {
    role: 'assistant', content, blocks: [], runId, streaming: true, pendingActions: [],
    lastActivity: 'other', lastEventAt: Date.now(), liveStatus: '正在恢复执行进度…',
  })
  const controller = new AbortController()
  historyStreams.set(runId, controller)
  void consumeRunEvents(runId, {
    signal: controller.signal,
    afterSeq: 0,
    replayPastPauses: true,
    onEvent: (envelope) => {
      applyRunEvent(msg, envelope)
      if (currentConversation.value?.id === convId) void scrollToBottom()
    },
    onDone: (reason) => {
      finalizeRunMessage(msg, reason, msg.content)
      if (msg.runStatus === 'failed') void refreshDocumentRetryability(msg)
      if (msg.runStatus === 'waiting_input' && !pendingDocInput(msg)) {
        void runApi.get(runId).then((run) => {
          if (!controller.signal.aborted && run.pending_input) addDocInput(msg, run.pending_input)
        }).catch(() => {})
      }
    },
  }).catch(() => {
    msg.streaming = false
    msg.liveStatus = ''
  }).finally(() => {
    if (historyStreams.get(runId) === controller) historyStreams.delete(runId)
  })
}

async function selectConversation(c: Conversation) {
  if (currentConversation.value?.id !== c.id) switchConversation(c)
  if (!historyMedia.matches) closeHistory()
  if (loadingMessagesFor.has(c.id)) return
  if (convMessages.has(c.id) && !failedMessagesFor.has(c.id)) {
    if (!await focusLinkedRun()) await forceScrollToBottom()
    return
  }
  loadingMessagesFor.add(c.id)
  failedMessagesFor.delete(c.id)
  try {
    const res = await conversationApi.messages(c.id)
    if (inactive || !conversations.value.some((item) => item.id === c.id)) return
    const arr = convArr(c.id)
    const raw: any[] = res.items || []
    const assistantRuns = new Set(raw.filter((item) => item.role === 'assistant').map((item) => item.source_run_id))
    for (const m of raw) {
      if (m.role === 'user') {
        const { text, files } = messageAttachments(m.content || '', m.attachments)
        const msg = pushMsg(c.id, { role: 'user', content: text, blocks: [], files })
        // 图片附件懒加载预览（不阻塞历史渲染；经响应式代理赋值触发视图刷新）
        files.forEach((f, fi) => {
          if (isImageName(f.name)) {
            loadFilePreview(c.id, f.name).then((url) => {
              if (url && msg.files && convMessages.has(c.id)) msg.files[fi].previewUrl = url
              else releaseAttachmentUrl(url)
            })
          }
        })
        const documentRunId = m.document_run_id
        if (typeof documentRunId === 'string' && !assistantRuns.has(documentRunId)) {
          replayConversationRun(c.id, documentRunId)
          assistantRuns.add(documentRunId)
        }
        continue
      }
      const content = m.content || ''
      const runId = m.source_run_id || undefined
      // 历史助手消息：块流由事件回放重建（文本/工具按发生顺序穿插）；
      // 无事件流时直接以内容作为文本块
      if (!runId) {
        arr.push({
          role: 'assistant', content,
          blocks: content.trim() ? [{ kind: 'text', text: content }] : [],
          runStatus: 'completed', pendingActions: [],
        })
      } else {
        replayConversationRun(c.id, runId, content)
      }
    }
    if (c.active_run_id && !assistantRuns.has(c.active_run_id)) replayConversationRun(c.id, c.active_run_id)
  } catch {
    convMessages.delete(c.id)
    failedMessagesFor.add(c.id)
  } finally {
    loadingMessagesFor.delete(c.id)
    if (currentConversation.value?.id === c.id && !failedMessagesFor.has(c.id) && !await focusLinkedRun()) await forceScrollToBottom()
  }
}

let confirmingDelete = false

async function removeConversation(c: Conversation) {
  if (confirmingDelete || deletingId.value !== null) return
  confirmingDelete = true
  try {
    await confirmDestructive(`将从历史会话中删除「${c.title || '新对话'}」及其消息。`, '删除对话', {
      cancelButtonText: '保留',
    })
  } catch {
    return
  } finally {
    confirmingDelete = false
  }
  deletingId.value = c.id
  try {
    const entry = activeRuns.get(c.id)
    if (entry && !entry.msg.runId) {
      ElMessage.info('任务正在创建，请稍后再删除对话')
      return
    }
    const runId = entry?.msg.runId || convMessages.get(c.id)?.find((message) => message.streaming || pendingDocInput(message))?.runId
      || c.active_run_id
    if (runId) await runApi.cancel(runId, '删除会话前取消任务')
    await conversationApi.delete(c.id)
    rememberTeamMode(c.id, false)
    if (entry) {
      entry.stopped = true
      entry.abort.abort()
      clearActiveRun(c.id, entry)
    }
    conversations.value = conversations.value.filter((x) => x.id !== c.id)
    for (const message of convMessages.get(c.id) || []) {
      for (const file of message.files || []) releaseAttachmentUrl(file.previewUrl)
      if (message.runId) historyStreams.get(message.runId)?.abort()
    }
    convMessages.delete(c.id)
    if (currentConversation.value?.id === c.id) switchConversation(null)
    ElMessage.success('对话已删除')
    void loadConversations(true)
  } catch { /* 请求层提示失败，原对话仍可查看 */ }
  finally {
    deletingId.value = null
  }
}

// ── 会话附件（上传即入会话工作区；发送时随消息告知模型读取）────────────────

/** 图片扩展名（生成缩略图预览用） */
function isImageName(name: string): boolean {
  return /\.(png|jpe?g|gif|bmp|webp|svg)$/i.test(name)
}

/** 历史图片附件懒加载预览（Blob -> objectURL） */
const attachmentUrls = new Set<string>()

function attachmentUrl(blob: Blob) {
  if (inactive) return undefined
  const url = URL.createObjectURL(blob)
  attachmentUrls.add(url)
  return url
}

function releaseAttachmentUrl(url?: string) {
  if (url && attachmentUrls.delete(url)) URL.revokeObjectURL(url)
}

async function loadFilePreview(conversationId: number, name: string): Promise<string | undefined> {
  try {
    const blob = await conversationApi.fileBlob(conversationId, name)
    return attachmentUrl(blob)
  } catch { return undefined }
}

async function uploadAttachments(files: File[]) {
  uploading.value += files.length
  try {
    const conv = await ensureConversation()
    const results = await Promise.allSettled(files.map(async (file) => {
      const res = await conversationApi.uploadFile(conv.id, file)
      if (currentConversation.value?.id !== conv.id || inactive) return
      attachments.value.push({ ...res, previewUrl: isImageName(res.name) ? attachmentUrl(file) : undefined })
    }))
    const completeCount = results.filter((result) => result.status === 'fulfilled').length
    if (completeCount && currentConversation.value?.id === conv.id) ElMessage.success(`已上传 ${completeCount} 个附件`)
  } catch { /* 错误提示已由请求层统一弹出 */ }
  finally {
    uploading.value -= files.length
  }
}

async function removeAttachment(i: number) {
  const a = attachments.value[i]
  attachments.value.splice(i, 1)
  releaseAttachmentUrl(a?.previewUrl)
  if (currentConversation.value && a) {
    conversationApi.deleteFile(currentConversation.value.id, a.name).catch(() => {})
  }
}

// ── 右侧临时文件预览区块（对齐 Trae：点文件名 → 右侧预览，可关闭）──────────

interface PreviewState {
  name: string
  path: string
  loading: boolean
  error: string
  data: PreviewContent | null
}
const preview = ref<PreviewState | null>(null)
let previewGeneration = 0
let previewController: AbortController | undefined

/** 打开右侧预览（path 为工作区相对路径；name 为展示名） */
async function openPreview(path: string, name?: string) {
  const conv = currentConversation.value
  if (!conv || !path) return
  documentPreview.value = null
  // 工具历史会指向抽取副本。用户点击 Word 时直接查看原文件版式。
  const originalWord = /\.docx\.extracted\.txt$/i.test(path)
  let sourcePath = originalWord ? path.slice(0, -'.extracted.txt'.length) : path
  const display = (originalWord ? name?.replace(/\.extracted\.txt$/i, '') : name)
    || sourcePath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || sourcePath
  previewController?.abort()
  const controller = new AbortController()
  previewController = controller
  const previousUrl = preview.value?.data?.objectUrl
  if (previousUrl) URL.revokeObjectURL(previousUrl)
  const generation = ++previewGeneration
  preview.value = { name: display, path: sourcePath, loading: true, error: '', data: null }
  let fallback: PreviewContent | null = null
  try {
    const meta = await conversationApi.filePreview(conv.id, sourcePath).catch((cause) => {
      if (!originalWord || cause?.response?.status !== 404) throw cause
      sourcePath = path
      return conversationApi.filePreview(conv.id, sourcePath)
    })
    if (generation !== previewGeneration) return
    fallback = meta
    const word = /\.docx$/i.test(meta.name)
    let objectUrl: string | undefined
    if (word || meta.kind === 'image' || meta.kind === 'pdf') {
      const blob = word
        ? await conversationApi.filePreviewPdf(conv.id, sourcePath, controller.signal)
        : await conversationApi.fileBytes(conv.id, sourcePath)
      if (generation !== previewGeneration) return
      objectUrl = URL.createObjectURL(blob)
    }
    preview.value = {
      name: meta.name || display, path: meta.path || sourcePath,
      loading: false, error: '', data: { ...meta, kind: word ? 'pdf' : meta.kind, objectUrl },
    }
  } catch (cause) {
    const error = await filePreviewError(cause, '无法加载文件预览')
    if (generation !== previewGeneration) return
    preview.value = {
      name: fallback?.name || display, path: fallback?.path || sourcePath,
      loading: false, error, data: fallback,
    }
  }
}

/** 关闭预览（回收 objectURL，防内存泄漏） */
function closePreview() {
  ++previewGeneration
  previewController?.abort()
  previewController = undefined
  const url = preview.value?.data?.objectUrl
  if (url) URL.revokeObjectURL(url)
  preview.value = null
  documentPreview.value = null
}

function openDocumentPreview(artifact: DocumentArtifact) {
  closePreview()
  documentPreview.value = artifact
}

async function downloadArtifact(artifact: DocumentArtifact) {
  const id = artifact.document_version_id
  if (downloadingDocuments.has(id)) return
  downloadingDocuments.add(id)
  try {
    await downloadDocument(artifact.document_id, artifact.version_no, artifact.filename)
  } catch (error) {
    ElMessage.error(documentError(error, 'Word 文档下载失败'))
  } finally {
    downloadingDocuments.delete(id)
  }
}

function continueDocument(artifact: DocumentArtifact) {
  if (blockedReason.value) {
    ElMessage.info(blockedReason.value)
    return
  }
  documentTask.value = {
    mode: 'document_edit', documentId: artifact.document_id, version: artifact.version_no, title: artifact.title,
  }
  closePreview()
  focusConversation()
}

function resetDocumentTask() {
  documentTask.value = { mode: 'auto' }
  if (route.name !== 'Workspace' || (!route.query.document && !route.query.profile)) return
  const query = { ...route.query }
  delete query.document
  delete query.version
  delete query.profile
  void router.replace({ query })
}

/** 下载当前预览文件 */
async function downloadPreview() {
  const conv = currentConversation.value
  const st = preview.value
  if (!conv || !st) return
  try {
    const blob = await conversationApi.fileBytes(conv.id, st.path)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = st.name
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch {
    ElMessage.error('下载失败')
  }
}

/** Esc 关闭预览（仅预览打开时拦截） */
function onGlobalKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape' || e.defaultPrevented) return
  if (preview.value || documentPreview.value) closePreview()
  else if (historyOpen.value && !historyMedia.matches) closeHistory()
}

// ── 发送与事件流消费（§7.1 v2 协议 + §8.4 重连）──────────────────────────────

function sendExample(text: string) {
  inputText.value = text
  composer.value?.focus()
}

function editRetry(userMessageIndex: number) {
  const message = messages.value[userMessageIndex]
  if (message?.role !== 'user') return
  inputText.value = message.content
  if (message.task) documentTask.value = message.task
  if (message.files?.length) ElMessage.info('请为重试重新选择附件')
  composer.value?.focus()
}

async function repairProjectDraft(message: ChatMessage) {
  const sourceId = message.runId
  const convId = currentConversation.value?.id
  if (!sourceId || !convId || message.runStatus !== 'failed' || blockedReason.value
    || sending.value || loadingMessages.value || messagesFailed.value) return
  const modelId = selectedModelId.value || undefined
  const keyId = `${sourceId}:${modelId || 'default'}`
  const key = projectRepairKeys.get(keyId) || globalThis.crypto?.randomUUID?.()
  if (key) projectRepairKeys.set(keyId, key)
  sending.value = true
  repairingProject.value = sourceId
  projectRepairErrors.delete(sourceId)
  try {
    const created = await runApi.repairProject(sourceId, modelId, key)
    projectRepairKeys.delete(keyId)
    if (inactive || created.conversation_id !== convId) return
    pushMsg(convId, { role: 'user', content: created.user_message, blocks: [] })
    const assistant = pushMsg(convId, {
      role: 'assistant', content: '', blocks: [], runId: created.run_id, streaming: true,
      lastActivity: 'other', lastEventAt: Date.now(), runStatus: 'pending', pendingActions: [],
      liveStatus: '正在载入项目草稿…',
    })
    const entry: ActiveRun = { abort: new AbortController(), stopped: false, msg: assistant }
    setActiveRun(convId, entry)
    if (currentConversation.value?.id === convId) void forceScrollToBottom()
    subscribeActiveRun(convId, entry, created.run_id)
  } catch (error) {
    projectRepairErrors.set(sourceId, documentError(error, '暂时无法继续修复草稿，请重试'))
  } finally {
    sending.value = false
    repairingProject.value = ''
  }
}

async function sendMessage(continueTask = false) {
  const latest = messages.value[messages.value.length - 1]
  if (continueTask && (!latest?.runId || latest.streaming || !executionPause(latest)?.canContinue)) return
  const text = continueTask ? '继续' : inputText.value.trim()
  if (!text || blockedReason.value || sending.value || uploading.value || loadingMessages.value || messagesFailed.value) return

  sending.value = true
  let conv: Conversation
  try {
    conv = await ensureConversation()
  } catch {
    return
  } finally {
    sending.value = false
  }
  const convId = conv.id
  if (inactive || currentConversation.value?.id !== convId) return
  const sentFiles = continueTask ? [] : attachments.value.map((a) => ({ ...a }))
  const sentModelId = selectedModelId.value
  const sentMode = approvalMode.value
  const sentTeamMode = !continueTask && teamMode.value
  const sentTask: DocumentTaskSelection = continueTask ? { mode: 'chat' } : { ...documentTask.value }

  if (!continueTask) {
    inputText.value = ''
    attachments.value = []
    resetDocumentTask()
  }
  pushMsg(convId, {
    role: 'user', content: text, blocks: [], task: sentTask,
    files: sentFiles.map(({ name, path, previewUrl }) => ({ name, path, previewUrl })),
  })
  const aMsg = pushMsg(convId, {
    role: 'assistant', content: '', blocks: [], streaming: true,
    lastActivity: 'other', lastEventAt: Date.now(),
    liveStatus: '正在创建任务…', runStatus: 'pending', pendingActions: [],
  })
  const entry: ActiveRun = { abort: new AbortController(), stopped: false, msg: aMsg }
  setActiveRun(convId, entry)
  void forceScrollToBottom()

  try {
    const created = await runApi.create({
      message: text,
      conversation_id: convId,
      assistant_id: currentAssistant.value?.id,
      approval_mode: sentMode,
      model_config_id: sentModelId || undefined,
      ...(sentTeamMode ? { team_mode: true } : {}),
      attachments: sentFiles,
      ...documentTaskPayload(sentTask),
    })
    if (entry.stopped) {
      if (!inactive) runApi.cancel(created.run_id, '用户主动停止').catch(() => {
        ElMessage.warning('取消请求失败，任务可能继续执行')
      })
      return
    }
    aMsg.runId = created.run_id
    subscribeActiveRun(convId, entry, created.run_id)
  } catch {
    if (entry.stopped) return
    aMsg.streaming = false
    aMsg.liveStatus = ''
    aMsg.runStatus = 'failed'
    aMsg.error = '创建任务失败，可编辑原消息后重新发送。'
    clearActiveRun(convId, entry)
    if (!continueTask && currentConversation.value?.id === convId && !inputText.value) {
      inputText.value = text
      documentTask.value = sentTask
    }
  }
}

function subscribeActiveRun(convId: number, entry: ActiveRun, runId: string) {
  consumeRunEvents(runId, {
    signal: entry.abort.signal,
    afterSeq: 0,
    onEvent: (envelope) => {
      if (entry.stopped) return
      applyRunEvent(entry.msg, envelope)
      if (currentConversation.value?.id === convId) scrollToBottom()
    },
    onReconnecting: (attempt) => {
      if (entry.stopped) return
      entry.msg.liveStatus = `连接中断，正在重连（第 ${attempt} 次）…`
    },
    onDone: (reason) => {
      if (!entry.stopped) finishRun(convId, entry, reason)
    },
  }).catch(() => { /* 网络异常已由 onDone 兜底 */ })
}

function finishRun(convId: number, entry: ActiveRun, reason: string) {
  if (activeRuns.get(convId) !== entry) return
  finalizeRunMessage(entry.msg, reason, entry.msg.content)
  clearActiveRun(convId, entry)
  if (entry.msg.runStatus === 'failed') void refreshDocumentRetryability(entry.msg)
  void loadConversations(true) // 标题已规范化生成，刷新会话列表
  if (currentConversation.value?.id === convId) scrollToBottom()
}

/** Langfuse Trace 入口：run_id → trace 跳转链接（后端确定性派生） */
async function openTrace(runId: string) {
  try {
    const info = await runApi.get(runId)
    if (info.langfuse_trace_url) {
      window.open(info.langfuse_trace_url, '_blank', 'noopener,noreferrer')
    } else {
      ElMessage.warning('未配置 Langfuse 或 Trace 暂不可用')
    }
  } catch {
    ElMessage.error('获取 Trace 链接失败')
  }
}

/** UX-07：停止 = 后端真实取消（不停止订阅而已）+ 立即本地终结 */
async function stopGeneration() {
  const convId = currentConversation.value?.id
  if (!convId) return
  const entry = activeRuns.get(convId)
  if (!entry) {
    const message = historyRun.value
    if (!message?.runId) return
    try {
      const result = await runApi.cancel(message.runId, '用户主动停止')
      ElMessage.info(result.message || '取消请求已发送')
    } catch {
      ElMessage.warning('取消请求失败，任务可能继续执行')
    }
    return
  }
  entry.stopped = true
  entry.abort.abort()
  clearActiveRun(convId, entry)
  finalizeRunMessage(entry.msg, 'run.cancelled', entry.msg.content)
  const runId = entry.msg.runId
  if (runId) {
    try {
      const res = await runApi.cancel(runId, '用户主动停止')
      ElMessage.info(res.message || '取消请求已发送')
    } catch {
      ElMessage.warning('取消请求失败，任务可能继续执行')
    }
  }
}

async function decideApproval(convId: number, message: ChatMessage, type: 'approve' | 'reject') {
  const runId = message.runId
  if (!runId || !message.pendingActions?.length || pendingDecisions.has(runId)) return
  pendingDecisions.set(runId, type)
  try {
    await runApi.resume(runId, message.pendingActions.map((action) => ({ type, action_id: action.action_id || null })))
    if (!inactive && convMessages.get(convId)?.includes(message)) {
      observeResumedRun(convId, message, type === 'approve' ? '已批准，恢复执行…' : '已拒绝，恢复执行…')
    }
  } catch { /* 请求层显示错误，保留待审批操作以便重试 */ }
  finally {
    pendingDecisions.delete(runId)
  }
}

function isInlineProfileConfirmation(message: ChatMessage, request: InputRequest) {
  return isProfileConfirmation(request) && message.blocks.some((block) => block.kind === 'document_profile_draft')
}

function profileConfirmation(message: ChatMessage) {
  const request = pendingDocInput(message)?.request
  return request && isInlineProfileConfirmation(message, request) ? request : undefined
}

function confirmProfile(message: ChatMessage, definition: ProfileDefinition) {
  const request = profileConfirmation(message)
  if (request) void submitDocumentInput(message, request.request_id, { definition, confirm: true })
}

function submitDocumentReview(message: ChatMessage, block: DocumentEditReviewBlock, acceptedIds: string[]) {
  if (documentReviewInput(message, block)?.state === 'pending' && block.requestId) {
    void submitDocumentInput(message, block.requestId, { accepted_change_ids: acceptedIds })
  }
}

async function submitDocumentInput(message: ChatMessage, requestId: string, values: Record<string, unknown>) {
  const runId = message.runId
  const convId = currentConversation.value?.id
  if (!runId || !convId || documentInputsBusy.has(runId) || pendingDocInput(message)?.request.request_id !== requestId) return
  documentInputsBusy.add(runId)
  documentInputErrors.delete(runId)
  try {
    await runApi.submitInput(runId, requestId, values)
    if (inactive || !convMessages.get(convId)?.includes(message)) return
    const accepted = values.accepted_change_ids
    if (Array.isArray(accepted)) recordDocumentReview(message, requestId, accepted)
    acceptDocInput(message, requestId)
    observeResumedRun(convId, message, Array.isArray(accepted) ? '审阅已提交，正在继续执行…' : '资料已提交，正在继续执行…')
  } catch (error) {
    documentInputErrors.set(runId, documentError(error, '提交失败，请重试'))
  } finally {
    documentInputsBusy.delete(runId)
  }
}

async function cancelDocumentInput(message: ChatMessage) {
  const runId = message.runId
  const convId = currentConversation.value?.id
  if (!runId || !convId || documentInputsBusy.has(runId) || !pendingDocInput(message)) return
  documentInputsBusy.add(runId)
  documentInputErrors.delete(runId)
  try {
    await runApi.cancel(runId, '用户取消文档任务')
    if (!inactive && convMessages.get(convId)?.includes(message)) {
      const input = pendingDocInput(message)
      if (input) input.state = 'closed'
      observeResumedRun(convId, message, '正在取消文档任务…')
    }
  } catch (error) {
    documentInputErrors.set(runId, documentError(error, '取消失败，请重试'))
  } finally {
    documentInputsBusy.delete(runId)
  }
}

async function refreshDocumentRetryability(message: ChatMessage) {
  if (!message.runId || !message.blocks.some((block) => block.kind === 'document_stage')) return
  try {
    const run = await runApi.get(message.runId)
    if (message.runStatus === 'failed') message.documentRetryable = run.document_retryable === true
  } catch { /* 保留事件中已知的恢复状态 */ }
}

async function retryFailedDocument(message: ChatMessage) {
  const runId = message.runId
  const convId = currentConversation.value?.id
  if (!runId || !convId || documentInputsBusy.has(runId)) return
  if (activeRuns.has(convId)) {
    ElMessage.warning('此会话正在执行任务，请等它完成后再继续')
    return
  }
  documentInputsBusy.add(runId)
  try {
    const resumed = await runApi.retryDocument(runId, message.lastSeq)
    if (inactive || !convMessages.get(convId)?.includes(message)) return
    message.lastSeq = resumed.after_seq
    message.error = undefined
    message.documentRetryable = false
    observeResumedRun(convId, message, '正在从文档断点继续…')
  } catch (error) {
    ElMessage.error(documentError(error, '暂时无法继续，请重试'))
  } finally {
    documentInputsBusy.delete(runId)
  }
}

function observeResumedRun(convId: number, aMsg: ChatMessage, liveStatus: string) {
  if (!aMsg.runId) return
  const previous = activeRuns.get(convId)
  if (previous) {
    previous.stopped = true
    previous.abort.abort()
  }
  historyStreams.get(aMsg.runId)?.abort()
  aMsg.pendingActions = []
  aMsg.streaming = true
  aMsg.runStatus = 'running'
  aMsg.lastActivity = 'other'
  aMsg.lastEventAt = Date.now()
  aMsg.liveStatus = liveStatus

  const entry: ActiveRun = { abort: new AbortController(), stopped: false, msg: aMsg }
  setActiveRun(convId, entry)

  consumeRunEvents(aMsg.runId, {
    signal: entry.abort.signal,
    afterSeq: aMsg.lastSeq || 0,
    replayPastPauses: true,
    onEvent: (envelope) => {
      if (entry.stopped) return
      applyRunEvent(aMsg, envelope)
      if (currentConversation.value?.id === convId) scrollToBottom()
    },
    onDone: (reason) => {
      if (entry.stopped) return
      finishRun(convId, entry, reason)
    },
  }).catch(() => { /* 网络异常已由 onDone 兜底 */ })
}
</script>

<style scoped>
.document-stage { display: flex; gap: 6px; align-items: center; color: var(--ui-muted); font-size: 12px; }
.document-stage svg { flex: none; }
.document-stage[data-tone="warning"] { color: var(--ui-warning); }
.document-stage[data-tone="success"] { color: var(--ui-success); }
.document-context {
  display: flex; align-items: center; gap: 6px; margin: 3px 5px 2px; font-size: 12px;
  color: var(--ui-accent); overflow-wrap: anywhere;
}
.document-context > svg { flex: none; }
.document-context > button { display: grid; place-items: center; min-width: 28px; height: 28px; border: 0; border-radius: var(--ui-radius-sm); background: none; color: inherit; cursor: pointer; }
.document-context > button:hover { background: var(--ui-accent-soft); }
.workspace-home {
  --thread-gutter: clamp(16px, 4%, 48px);
  --thread-line: 720px;
  position: relative;
  display: flex;
  height: 100%;
  min-height: 0;
  width: 100%;
  overflow: hidden;
  background: var(--ui-surface);
}
.ws-history { flex: 0 0 224px; border-inline-end: 1px solid var(--ui-border); }
.history-overlay { display: none; }
.ws-main { position: relative; display: flex; flex: 1 1 0; flex-direction: column; min-width: 0; min-height: 0; }
.thread-head {
  flex: none; min-height: 48px; padding: 0 16px; border-bottom: 1px solid var(--ui-border);
  display: flex; align-items: center; gap: 8px; color: var(--ui-secondary);
}
.thread-heading { flex: 1; min-width: 0; padding-inline: 4px; }
.thread-heading span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 600; color: var(--ui-text); }
.assistant-name { display: inline-flex; align-items: center; flex: none; gap: 6px; font-size: 12px; color: var(--ui-secondary); }
.icon-button {
  display: inline-flex; flex: none; align-items: center; justify-content: center; width: 30px; height: 30px;
  border: 0; background: transparent; color: var(--ui-muted); border-radius: var(--ui-radius-sm); cursor: pointer;
}
.icon-button:hover, .trace-link:hover { color: var(--ui-accent); background: var(--ui-hover); }

.ws-messages { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; overflow-anchor: none; }
.ws-messages:focus-visible { outline-offset: -2px; }
.thread {
  display: flex; flex-direction: column; gap: 28px; width: min(calc(100% - var(--thread-gutter) * 2), var(--thread-line));
  margin-inline: auto; padding: 30px 0 24px; min-width: 0;
}
.ws-welcome {
  display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px;
  min-height: 100%; padding: 32px var(--thread-gutter) 24px; text-align: center; color: var(--ui-secondary);
}
.welcome-mark {
  display: grid; place-items: center; width: 52px; height: 52px; margin-bottom: 4px;
  color: var(--ui-accent); background: var(--ui-accent-soft); border-radius: var(--ui-radius-lg);
}
.ws-welcome h1 { font-weight: 660; font-size: clamp(24px, 3.5vw, 38px); line-height: 1.21; letter-spacing: -.045em; color: var(--ui-text); }
.ws-welcome p { font-size: 14px; line-height: 1.5; }
.task-examples { display: flex; justify-content: center; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.task-example {
  display: flex; align-items: center; gap: 7px; padding: 7px 10px;
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-surface);
  color: var(--ui-secondary); font-size: 13px; cursor: pointer;
}
.task-example svg { color: var(--ui-muted); }
.task-example:hover { background: var(--ui-hover); border-color: var(--ui-border-strong); color: var(--ui-text); }
.task-example:hover svg { color: var(--ui-accent); }
.thread-state { display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 60%; color: var(--ui-secondary); font-size: 14px; }
.text-button { color: var(--ui-accent); background: none; border: 0; cursor: pointer; }

.msg-user { display: flex; justify-content: end; min-width: 0; scroll-margin-top: 16px; }
.msg-bubble {
  max-width: min(85%, 580px); padding: 10px 14px; white-space: pre-wrap; overflow-wrap: anywhere;
  font-size: 14px; line-height: 1.7; color: var(--ui-text); background: var(--ui-subtle); border-radius: var(--ui-radius-lg);
}
.msg-files { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
.msg-file {
  min-width: 0; max-width: 100%; display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px;
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); background: var(--ui-surface); color: var(--ui-secondary); cursor: pointer;
}
.msg-file:hover { color: var(--ui-accent); border-color: var(--ui-accent); }
.msg-file img { width: 40px; height: 40px; object-fit: cover; border-radius: var(--ui-radius-sm); }
.msg-file span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.msg-file svg { flex: none; }
.msg-assistant { display: grid; grid-template-columns: 30px minmax(0, 1fr); gap: 10px; align-items: start; }
.assistant-mark {
  display: grid; place-items: center; width: 30px; height: 30px;
  border: 1px solid var(--ui-border); color: var(--ui-accent); border-radius: var(--ui-radius-md); background: var(--ui-surface);
}
.msg-content { display: grid; gap: 8px; min-width: 0; }
.msg-heading { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-height: 30px; font-size: 13px; color: var(--ui-text); }
.run-state { padding: 2px 6px; border-radius: var(--ui-radius-sm); color: var(--ui-secondary); background: var(--ui-subtle); font-size: 12px; }
.run-state[data-state="success"] { color: var(--ui-success); background: var(--ui-success-soft); }
.run-state[data-state="info"] { color: var(--ui-info); background: var(--ui-info-soft); }
.run-state[data-state="warning"] { color: var(--ui-warning); background: var(--ui-warning-soft); }
.run-state[data-state="danger"] { color: var(--ui-danger); background: var(--ui-danger-soft); }
.run-duration, .stale-hint { color: var(--ui-muted); font-size: 12px; }
.run-duration { font-variant-numeric: tabular-nums; }
.live-status, .msg-error { display: flex; align-items: center; gap: 7px; font-size: 13px; }
.live-status { color: var(--ui-info); }
.msg-error {
  align-items: start; padding: 8px 10px; border-radius: var(--ui-radius-md);
  color: var(--ui-danger); background: var(--ui-danger-soft); white-space: pre-wrap; overflow-wrap: anywhere;
}
.msg-error svg { flex: none; margin-top: 2px; }
.msg-pause {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
  padding: 8px 10px; border-radius: var(--ui-radius-md); background: var(--ui-subtle);
  color: var(--ui-secondary); font-size: 13px;
}
.msg-pause > svg { flex: none; color: var(--ui-warning); }
.msg-pause > span { flex: 1 1 12rem; }
.continue-button {
  padding: 4px 10px; border: var(--ui-line); border-radius: var(--ui-radius-sm);
  color: var(--ui-accent); background: var(--ui-bg); cursor: pointer;
}
.continue-button:hover:not(:disabled) { background: var(--ui-accent-soft); }
.continue-button:disabled { cursor: default; opacity: .55; }
.message-actions { display: flex; align-items: center; gap: 4px; }
.trace-link {
  display: inline-flex; align-items: center; min-height: 30px; gap: 6px; padding: 0 8px;
  border: 0; border-radius: var(--ui-radius-sm); color: var(--ui-secondary); background: transparent; font-size: 12px; cursor: pointer;
}
.process-disclosure { min-width: 0; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-bg); }
.process-disclosure summary {
  display: flex; align-items: center; gap: 7px; min-height: 35px; padding: 5px 9px;
  color: var(--ui-secondary); font-size: 13px; font-weight: 550; list-style: none; cursor: pointer;
}
.process-disclosure summary::-webkit-details-marker { display: none; }
.process-disclosure summary:hover { color: var(--ui-text); }
.process-disclosure summary::after {
  content: ''; flex: none; width: 6px; height: 6px; margin-inline: auto 4px;
  border: solid var(--ui-muted); border-width: 0 1.5px 1.5px 0; transform: rotate(-45deg);
}
.process-disclosure[open] summary::after { transform: rotate(45deg); }
.process-disclosure summary span { color: var(--ui-muted); font-weight: 450; font-size: 12px; }
.process-disclosure summary .group-failure { color: var(--ui-danger); }
.process-disclosure ul, .tool-group-items { padding: 3px 7px 7px; list-style: none; }
.process-disclosure li { display: flex; align-items: start; gap: 7px; padding: 4px; font-size: 13px; }
.process-disclosure li svg { flex: none; margin-top: 2px; color: var(--ui-muted); }
.process-disclosure li .plan-completed { color: var(--ui-success); }
.process-disclosure li .plan-in_progress { color: var(--ui-info); }
.plan-done { color: var(--ui-muted); }

.question-nav {
  position: absolute; inset-inline-end: 2px; top: 50%; transform: translateY(-50%);
  display: flex; flex-direction: column; max-height: 45%; overflow-y: auto; padding: 3px; scrollbar-width: thin;
}
.question-nav button { position: relative; width: 20px; height: 18px; flex: none; border: 0; background: transparent; cursor: pointer; }
.question-nav button::before { content: ''; position: absolute; inset: 6px 5px; border-radius: 10px; background: var(--ui-border-strong); }
.question-nav button:hover::before, .question-nav button:focus-visible::before { background: var(--ui-accent); }

.composer-dock {
  width: min(calc(100% - var(--thread-gutter) * 2), var(--thread-line)); margin: 0 auto;
  flex: none; padding: 6px 0 max(10px, env(safe-area-inset-bottom)); position: relative;
}
.latest-button {
  display: inline-flex; gap: 5px; align-items: center; position: absolute; z-index: 1; top: -40px; left: 50%;
  transform: translateX(-50%); width: max-content; padding: 6px 9px; border-radius: var(--ui-radius-md); cursor: pointer;
  color: var(--ui-secondary); background: var(--ui-surface); border: 1px solid var(--ui-border); box-shadow: var(--ui-shadow-sm); font-size: 12px;
}
.latest-button:hover { color: var(--ui-accent); }
.is-empty .composer-dock { padding-bottom: clamp(20px, 23vh, 218px); }
.is-empty .ws-welcome { justify-content: end; }
.has-preview .ws-history { flex: 0 1 192px; min-width: 0; overflow: hidden; }
.spin { animation: rotate 1s linear infinite; }
@keyframes rotate { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }

@media (max-width: 849px) {
  .ws-history { position: absolute; z-index: 6; inset: 0 auto 0 0; width: min(250px, 83%); box-shadow: var(--ui-shadow-md); }
  .history-overlay { display: block; position: absolute; z-index: 5; inset: 0; border: none; background: color-mix(in srgb, var(--ui-text) 25%, transparent); }
  .is-empty .composer-dock { padding-bottom: clamp(16px, 20vh, 168px); }
}
@media (max-width: 480px) {
  .msg-assistant { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .assistant-mark { display: none; }
  .thread { padding-top: 18px; gap: 18px; }
  .ws-main:has(.question-nav) .thread { margin-inline: 12px 26px; width: auto; }
  .question-nav { inset-inline-end: 0; }
  .thread-head { padding: 0 10px; }
  .composer-dock { margin-bottom: 0; padding-bottom: 0; }
}
@media (max-height: 630px) {
  .is-empty .composer-dock { padding-bottom: 12px; }
}
</style>

<style>
.workspace-question-tooltip { max-width: 280px; overflow-wrap: anywhere; }
</style>
