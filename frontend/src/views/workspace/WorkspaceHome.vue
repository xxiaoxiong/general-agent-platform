<template>
  <section class="workspace-home" :class="{ 'has-preview': preview || documentPreview || filesPanelOpen }" aria-label="智能体工作空间">
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
        <div v-if="sandboxEnabled && currentConversation" class="project-switch">
          <FolderKanban :size="14" aria-hidden="true" />
          <select
            :value="currentConversation?.project_id ?? ''" :disabled="projectBusy || !!currentConversation?.active_run_id"
            aria-label="切换项目" title="当前会话所属项目（工作空间）"
            @change="changeProject"
          >
            <option value="">未绑定项目</option>
            <option v-for="project in projects" :key="project.id" :value="project.id">{{ project.name }}</option>
          </select>
          <button type="button" class="icon-button project-add" title="新建项目" aria-label="新建项目"
            :disabled="projectBusy" @click="createProjectPrompt">
            <Plus :size="14" />
          </button>
        </div>
        <span class="assistant-name"><Bot :size="15" />{{ currentAssistant?.name || '通用助手' }}</span>
        <button
          type="button" class="icon-button" :aria-pressed="filesPanelOpen"
          aria-label="工作目录文件" title="工作目录文件" @click="toggleFilesPanel"
        >
          <FolderOpen :size="17" />
        </button>
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
                  <MessageMarkdown v-if="block.kind === 'text'" :text="block.text"
                    :streaming="Boolean(m.streaming) && blockIdx === messageBlocks[idx].length - 1" />
                  <div v-else-if="block.kind === 'steer'" class="steer-block">
                    <span class="steer-label">执行中补充<template v-if="block.applied"> · 已纳入</template><template v-else-if="block.pending"> · 提交中…</template></span>
                    <MessageMarkdown :text="block.text" />
                  </div>
                  <ToolStep v-else-if="block.kind === 'tool'" :tool="block" @preview="openPreview" />
                  <TeamRunCard v-else-if="block.kind === 'team'" :progress="block.progress" :active="Boolean(m.streaming)" />
                  <WorkPlanCard v-else-if="block.kind === 'work_plan'" :plan="block.plan" />
                  <VerificationCard v-else-if="block.kind === 'verification'" :report="block" :run-id="m.runId" />
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

                  <div v-else-if="block.kind === 'subagent'" class="subagent-line" :data-state="block.state">
                    <Bot :size="14" />
                    <span>子代理 {{ block.name }} · {{ block.state === 'running' ? '执行中' : block.state === 'failed' ? '执行失败' : '执行完成' }}</span>
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
                <div v-if="messageSources(m).length" class="msg-sources" aria-label="来源">
                  <div class="msg-sources-label"><BookOpen :size="13" /> 来源</div>
                  <ol class="msg-sources-list">
                    <li v-for="s in messageSources(m)" :key="s.url">
                      <a :href="s.url" target="_blank" rel="noopener noreferrer" :title="s.url">{{ s.title || s.url }}</a>
                    </li>
                  </ol>
                </div>
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
          v-model:goal-mode="goalMode"
          v-model:approval-mode="approvalMode"
          v-model:web-enabled="webSearchOn"
          v-model:model="selectedModelId"
          :options="modelOptions"
          :attachments="attachments"
          :uploading="uploading"
          :sending="sending"
          :stoppable="streaming || !!historyRun"
          :steerable="!!steerTarget"
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
    <!-- 文件面板打开时由面板承载预览（左侧文件树保持可见）；关闭后点文件名走独立预览 -->
    <WorkspaceFilesPanel
      v-else-if="filesPanelOpen"
      :files="fileList" :conversation-id="currentConversation?.id ?? null"
      :loading="filesLoading" :error="filesError" :truncated="filesTruncated"
      :downloading="workspaceZipBusy" :preview="preview"
      :sandbox-enabled="sandboxEnabled" :project-id="currentConversation?.project_id ?? null"
      @open="openPreview" @refresh="refreshFileList(true)" @close="closeFilesPanel"
      @download-zip="downloadWorkspaceZip" @download="downloadPreview"
      @open-original="openPreview" @close-preview="closePreview"
    />
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
 *
 * 模块划分：chatState（共享状态）/ useChatWorkspace（会话+运行）/
 * useDocumentTask（文档任务）/ useFileAttachments（附件+预览）/
 * useThreadScroll（粘底滚动）/ chatDisplay（展示辅助）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  ArrowDown, Bot, BookOpen, ChartNoAxesCombined, CircleAlert, CirclePause,
  Copy, ExternalLink, FilePenLine, FileText, FolderKanban, FolderOpen, Layers,
  ListTodo, LoaderCircle, PanelLeft, Plus, RotateCcw, Search, X,
} from '@lucide/vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { modelApi } from '@/api'
import { workspaceApi } from '@/api/workspace'
import { conversationApi, type Conversation } from '@/api/conversations'
import { sandboxApi, type ProjectSummary } from '@/api/sandbox'
import {
  documentReviewInput, executionPause, isDocumentReviewInput,
} from '@/features/run/blocks'
import ToolStep from '@/features/run/ToolStep.vue'
import ApprovalPanel from '@/features/run/ApprovalPanel.vue'
import MessageMarkdown from '@/features/run/MessageMarkdown.vue'
import TeamRunCard from '@/features/run/TeamRunCard.vue'
import WorkPlanCard from '@/features/run/WorkPlanCard.vue'
import { teamPhaseLabels } from '@/features/run/teamProgress'
import ProjectDeliveryCard from '@/features/run/ProjectDeliveryCard.vue'
import VerificationCard from '@/features/run/VerificationCard.vue'
import { projectPhaseLabels } from '@/features/run/projectDelivery'
import FilePreviewPanel from '@/features/preview/FilePreviewPanel.vue'
import WorkspaceFilesPanel from '@/features/preview/WorkspaceFilesPanel.vue'
import DocumentArtifactCard from '@/features/documents/DocumentArtifactCard.vue'
import DocumentEditReviewCard from '@/features/documents/DocumentEditReviewCard.vue'
import DocumentPreviewPanel from '@/features/documents/DocumentPreviewPanel.vue'
import DocumentTaskPicker from '@/features/documents/DocumentTaskPicker.vue'
import InputRequestCard from '@/features/documents/InputRequestCard.vue'
import ProfileDraftCard from '@/features/documents/ProfileDraftCard.vue'
import ConversationComposer from './ConversationComposer.vue'
import ConversationHistory from './ConversationHistory.vue'
import {
  answerText, conversationStatus as conversationStatusOf, displayBlocks, groupFailCount, groupSummary, messageSources,
  planIcon, planItems, runState, staleSeconds as staleSecondsOf,
  taskTimeText as taskTimeTextOf, type ChatMessage,
} from './chatDisplay'
import { useWorkspaceState } from './chatState'
import { useThreadScroll } from './useThreadScroll'
import { useChatWorkspace } from './useChatWorkspace'
import { useDocumentTask } from './useDocumentTask'
import { useFileAttachments } from './useFileAttachments'

// ── 模块组装：共享状态 → 滚动 → 会话/运行 → 文档 → 附件 ─────────────────────
const st = useWorkspaceState()
const scroll = useThreadScroll(st)
const chat = useChatWorkspace(st, scroll)
const doc = useDocumentTask(st, chat)
const files = useFileAttachments(st, chat)

// ── 共享状态（模板绑定名保持不变）────────────────────────────────────────────
const {
  preview, documentPreview, historyOpen, conversations, currentConversation,
  historyLoading, historyFailed, creatingConversation, deletingId,
  hasMoreConversations, historyLoadingMore, showWelcome, historyToggle,
  currentAssistant, loadingMessages, messagesFailed, messages,
  inputText, teamMode, goalMode, approvalMode, webSearchOn, selectedModelId, modelOptions,
  attachments, uploading, sending, streaming, historyRun, blockedReason, steerTarget,
  pendingDecisions, documentContext, documentTask, documentInputsBusy,
  documentInputErrors, repairingProject, projectRepairErrors, downloadingDocuments,
  filesPanelOpen, fileList, filesLoading, filesError, filesTruncated,
  composer,
  openHistory, closeHistory, resetDocumentTask, closePreview,
} = st

const { messagesRef, threadRef, showJumpToLatest, questionAnchors, onMessagesScroll, onWheelScroll, forceScrollToBottom, jumpToQuestion } = scroll

const {
  loadConversations, loadMoreConversations, updateConversationMetadata, newConversation,
  selectConversation, removeConversation, reloadMessages,
  sendMessage, stopGeneration, decideApproval, repairProjectDraft, openTrace,
} = chat

const {
  profileConfirmation, confirmProfile, cancelDocumentInput, submitDocumentReview,
  submitDocumentInput, retryFailedDocument, openDocumentPreview, downloadArtifact,
  continueDocument, isInlineProfileConfirmation,
} = doc

const {
  uploadAttachments, removeAttachment, openPreview, downloadPreview,
  refreshFileList, closeFilesPanel, toggleFilesPanel, downloadWorkspaceZip, workspaceZipBusy,
} = files

// ── 项目（Workspace Sandbox）─────────────────────────────────────────────────
const sandboxEnabled = ref(false)
const projects = ref<ProjectSummary[]>([])
const projectBusy = ref(false)

async function loadProjects() {
  try {
    projects.value = (await sandboxApi.listProjects()).items
  } catch { /* Sandbox 未启用或暂时不可用时静默 */ }
}

async function changeProject(event: Event) {
  const target = event.target as HTMLSelectElement
  const projectId = target.value ? Number(target.value) : null
  const conversation = currentConversation.value
  if (!conversation || projectBusy.value) return
  if ((conversation.project_id ?? null) === projectId) return
  projectBusy.value = true
  try {
    const updated = await conversationApi.setProject(conversation.id, projectId)
    currentConversation.value = updated
    await refreshFileList(true)
    ElMessage.success(projectId ? '已切换项目' : '已解除项目绑定')
    if (projectId) void loadProjects()
  } catch (exc: any) {
    ElMessage.error(exc?.response?.data?.detail || exc?.response?.data?.message || '切换项目失败')
    await loadConversations(true)
  } finally {
    projectBusy.value = false
  }
}

async function createProjectPrompt() {
  if (projectBusy.value) return
  try {
    const result = await ElMessageBox.prompt('输入项目名称', '新建项目', {
      confirmButtonText: '创建',
      cancelButtonText: '取消',
      inputPattern: /\S+/,
      inputErrorMessage: '项目名称不能为空',
      inputPlaceholder: '例如：官网重构',
    }) as any
    const name = String(result?.value || '').trim()
    if (!name) return
    projectBusy.value = true
    const project = await sandboxApi.createProject({ name })
    await loadProjects()
    if (currentConversation.value) {
      currentConversation.value = await conversationApi.setProject(
        currentConversation.value.id, project.id,
      )
      await refreshFileList(true)
    }
    ElMessage.success(`项目「${project.name}」已创建`)
  } catch (exc: any) {
    if (exc === 'cancel' || exc === 'close') return
    ElMessage.error(exc?.response?.data?.detail || exc?.response?.data?.message || '创建项目失败')
  } finally {
    projectBusy.value = false
  }
}

// ── 展示辅助 ────────────────────────────────────────────────────────────────
const messageBlocks = computed(() => messages.value.map(displayBlocks))
const conversationStatus = (c: Conversation) =>
  conversationStatusOf(c, { runningConvIds: st.runningConvIds.value, convMessages: st.convMessages })
const taskTimeText = (m: ChatMessage) => taskTimeTextOf(m, st.clock.value)
const staleSeconds = (m: ChatMessage) => staleSecondsOf(m, st.clock.value)

const examples = [
  { label: '分析数据', icon: ChartNoAxesCombined, prompt: '帮我分析这份数据的趋势并给出结论' },
  { label: '研究主题', icon: Search, prompt: '搜索行业最新动态并生成结构化研究摘要' },
  { label: '整理报告', icon: FilePenLine, prompt: '帮我把这段内容整理成一份正式报告' },
]

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

async function copyAnswer(message: ChatMessage) {
  try {
    await navigator.clipboard.writeText(answerText(message))
    ElMessage.success('回复已复制')
  } catch {
    ElMessage.error('无法复制回复')
  }
}

/** Esc 关闭预览 / 文件面板（仅打开时拦截） */
function onGlobalKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape' || e.defaultPrevented) return
  if (preview.value || documentPreview.value) closePreview()
  else if (filesPanelOpen.value) closeFilesPanel()
  else if (historyOpen.value && !st.historyMedia.matches) closeHistory()
}

// ── 初始化 / 清理 ───────────────────────────────────────────────────────────
let clockTimer: number | undefined

onMounted(async () => {
  clockTimer = window.setInterval(() => { st.clock.value = Date.now() }, 1000)
  window.addEventListener('keydown', onGlobalKeydown)
  st.historyMedia.addEventListener('change', st.syncHistoryVisibility)
  scroll.startResizeObserver()
  workspaceApi.bootstrap().then((boot) => {
    currentAssistant.value = boot.default_assistant
    sandboxEnabled.value = Boolean(boot.sandbox?.enabled)
    if (sandboxEnabled.value) void loadProjects()
  }).catch(() => { /* 默认助手仍可由服务端选择 */ })
  modelApi.listConfigs({ model_type: 'chat' }).then((list) => {
    modelOptions.value = (list || []).filter((m: any) => m.is_active)
  }).catch(() => { /* 模型列表加载失败不阻断对话 */ })
  await loadConversations()
  chat.openLinkedConversation()
})

onUnmounted(() => {
  st.inactive.value = true
  if (clockTimer) clearInterval(clockTimer)
  window.removeEventListener('keydown', onGlobalKeydown)
  st.historyMedia.removeEventListener('change', st.syncHistoryVisibility)
  scroll.stopResizeObserver()
  for (const entry of st.activeRuns.values()) {
    entry.stopped = true
    entry.abort.abort()
  }
  st.runningConvIds.value = []
  closePreview()
  for (const url of st.attachmentUrls) URL.revokeObjectURL(url)
  for (const controller of st.historyStreams.values()) controller.abort()
})
</script>

<style scoped>
.document-stage { display: flex; gap: 6px; align-items: center; color: var(--ui-muted); font-size: 12px; }
.document-stage svg { flex: none; }
.document-stage[data-tone="warning"] { color: var(--ui-warning); }
.document-stage[data-tone="success"] { color: var(--ui-success); }
.subagent-line {
  display: flex; gap: 6px; align-items: center; padding: 3px 7px; font-size: 12px;
  border: 1px dashed var(--ui-border); border-radius: var(--ui-radius-md); color: var(--ui-secondary);
}
.subagent-line svg { flex: none; color: var(--ui-accent); }
.subagent-line[data-state="running"] { color: var(--ui-info); border-color: var(--ui-info); }
.subagent-line[data-state="failed"] { color: var(--ui-danger); border-color: var(--ui-danger); }
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
.project-switch {
  display: inline-flex; align-items: center; flex: none; gap: 4px;
  padding: 2px 4px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm);
  color: var(--ui-secondary); background: var(--ui-surface);
}
.project-switch > svg { flex: none; color: var(--ui-accent); }
.project-switch select {
  max-width: 150px; border: 0; background: transparent; color: var(--ui-text);
  font-size: 12px; cursor: pointer; outline: none;
}
.project-switch:focus-within { border-color: var(--ui-accent); }
.project-add { width: 22px; height: 22px; }
@media (max-width: 620px) {
  .project-switch { display: none; }
}
.icon-button {
  display: inline-flex; flex: none; align-items: center; justify-content: center; width: 30px; height: 30px;
  border: 0; background: transparent; color: var(--ui-muted); border-radius: var(--ui-radius-sm); cursor: pointer;
}
.icon-button:hover, .trace-link:hover { color: var(--ui-accent); background: var(--ui-hover); }
.icon-button[aria-pressed='true'] { color: var(--ui-accent); background: var(--ui-hover); }

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
.steer-block {
  display: grid; gap: 4px; padding: 8px 10px; border-inline-start: 3px solid var(--ui-warning, #b26a00);
  background: var(--ui-warning-soft, rgba(255, 170, 0, .08)); border-radius: var(--ui-radius-sm);
}
.steer-label { color: var(--ui-warning, #b26a00); font-size: 11px; font-weight: 600; }
.steer-block :deep(.response-markdown) { font-size: 13px; }
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
/* 消息尾部来源引用区（正文 [标题](URL) 引用的去重清单） */
.msg-sources {
  margin-top: 10px; padding: 8px 10px;
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-surface);
}
.msg-sources-label {
  display: flex; align-items: center; gap: 5px;
  font-size: 12px; color: var(--ui-muted); margin-bottom: 4px;
}
.msg-sources-list { margin: 0; padding-inline-start: 18px; display: flex; flex-direction: column; gap: 2px; }
.msg-sources-list li { font-size: 12px; line-height: 1.6; }
.msg-sources-list a { color: var(--ui-secondary); text-decoration: none; overflow-wrap: anywhere; }
.msg-sources-list a:hover { color: var(--ui-accent); text-decoration: underline; }
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
