/**
 * 工作台共享状态包（单实例视图内组合使用）：
 * 会话/消息存储、运行执行簿记、附件与预览状态、文档任务状态及其轻量状态操作。
 * 跨模块（会话 / 运行 / 文档 / 附件）共享的可变状态统一放这里，避免 composable 循环依赖。
 */
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { Conversation, WorkspaceFile } from '@/api/conversations'
import { conversationApi, type PreviewContent } from '@/api/conversations'
import type { Assistant } from '@/api/workspace'
import { pendingDocInput, isDocumentReviewInput } from '@/features/run/blocks'
import type { DocumentArtifact } from '@/api/documents'
import type { DocumentTaskSelection } from '@/features/documents/taskModes'
import { useAuthStore } from '@/stores/auth'
import ConversationComposer from './ConversationComposer.vue'
import type { ActiveRun, ChatMessage, PreviewState } from './chatDisplay'

export function useWorkspaceState() {
  const route = useRoute()
  const router = useRouter()

  // ── 会话与消息存储 ─────────────────────────────────────────────────────────
  // 每会话独立消息存储：切换会话不打断后台运行，回来即见最新进度
  const convMessages = reactive(new Map<number, ChatMessage[]>())
  const conversations = ref<Conversation[]>([])
  const currentConversation = ref<Conversation | null>(null)
  const messages = computed<ChatMessage[]>(() =>
    currentConversation.value ? convArr(currentConversation.value.id) : [],
  )

  function convArr(convId: number): ChatMessage[] {
    if (!convMessages.has(convId)) convMessages.set(convId, [])
    return convMessages.get(convId)!
  }

  function pushMsg(convId: number, msg: ChatMessage): ChatMessage {
    const arr = convArr(convId)
    arr.push(msg)
    return arr[arr.length - 1] // 返回响应式代理，后续变更才能触发视图更新
  }

  // ── 运行执行簿记 ───────────────────────────────────────────────────────────
  // 当前会话是否有活跃 Run（各会话互不影响，可并行执行）
  // 注意：entry 含 AbortController 等类实例且需要引用相等比较，必须用普通 Map；
  // 响应式仅体现在 runningConvIds（供模板/计算属性跟踪）
  const activeRuns = new Map<number, ActiveRun>()
  const historyStreams = new Map<string, AbortController>()
  const pendingDecisions = reactive(new Map<string, 'approve' | 'reject'>())
  const runningConvIds = ref<number[]>([])
  /** 组件已卸载标记（后台请求回来后不再触碰视图状态） */
  const inactive = { value: false }

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

  // ── 发送状态 ───────────────────────────────────────────────────────────────
  const inputText = ref('')
  const sending = ref(false)
  const repairingProject = ref('')
  const projectRepairErrors = reactive(new Map<string, string>())
  const projectRepairKeys = new Map<string, string>()
  const uploading = ref(0)
  const approvalMode = ref<'manual' | 'auto'>(
    localStorage.getItem('approval_mode') === 'auto' ? 'auto' : 'manual',
  )
  watch(approvalMode, (v) => localStorage.setItem('approval_mode', v))
  const modelOptions = ref<Array<{ id: number; name: string; display_name?: string | null }>>([])
  const selectedModelId = ref<number>(0)
  /** 当前对话使用的助手：固定为 bootstrap 下发的默认助手（不再提供助手管理入口） */
  const currentAssistant = ref<Assistant | null>(null)
  /** 任务耗时时钟（秒级刷新） */
  const clock = ref(Date.now())

  // ── 团队模式偏好（按用户 + 会话记忆）────────────────────────────────────────
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

  // ── 会话列表（历史面板）状态 ───────────────────────────────────────────────
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
  /** 会话创建中的去重 Promise */
  const creating = { value: undefined as Promise<Conversation> | undefined }
  const historyRevision = { value: 0 }
  const requestedHistoryPages = { value: 1 }

  // ── 文档任务状态 ───────────────────────────────────────────────────────────
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

  // ── 附件与文件预览状态 ─────────────────────────────────────────────────────
  // 待发送附件（上传即写入会话工作区，随消息告知模型读取；切换会话即清空）
  const attachments = ref<Array<{
    name: string; path: string; text_path: string | null; size: number
    /** 图片本地预览 URL（URL.createObjectURL） */
    previewUrl?: string
  }>>([])
  const attachmentUrls = new Set<string>()
  const preview = ref<PreviewState | null>(null)
  const previewState = {
    generation: 0,
    controller: undefined as AbortController | undefined,
  }

  // ── 右侧文件面板状态（当前会话工作目录文件清单）────────────────────────────
  const filesPanelOpen = ref(false)
  const fileList = ref<WorkspaceFile[]>([])
  const filesLoading = ref(false)
  const filesError = ref('')
  const filesTruncated = ref(false)
  /** 工作区文件变动信号（工具写入 / 会话切换等），文件面板据此防抖刷新 */
  const workspaceVersion = ref(0)

  function noteWorkspaceChange() {
    workspaceVersion.value += 1
  }

  /** 图片扩展名（生成缩略图预览用） */
  function isImageName(name: string): boolean {
    return /\.(png|jpe?g|gif|bmp|webp|svg)$/i.test(name)
  }

  function attachmentUrl(blob: Blob) {
    if (inactive.value) return undefined
    const url = URL.createObjectURL(blob)
    attachmentUrls.add(url)
    return url
  }

  function releaseAttachmentUrl(url?: string) {
    if (url && attachmentUrls.delete(url)) URL.revokeObjectURL(url)
  }

  /** 历史图片附件懒加载预览（Blob -> objectURL） */
  async function loadFilePreview(conversationId: number, name: string): Promise<string | undefined> {
    try {
      const blob = await conversationApi.fileBlob(conversationId, name)
      return attachmentUrl(blob)
    } catch { return undefined }
  }

  function clearAttachments() {
    for (const file of attachments.value) releaseAttachmentUrl(file.previewUrl)
    attachments.value = []
  }

  /** 关闭右侧预览（回收 objectURL，防内存泄漏） */
  function closePreview() {
    ++previewState.generation
    previewState.controller?.abort()
    previewState.controller = undefined
    const url = preview.value?.data?.objectUrl
    if (url) URL.revokeObjectURL(url)
    preview.value = null
    documentPreview.value = null
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

  // ── URL 同步辅助 ───────────────────────────────────────────────────────────
  function numericQuery(value: unknown) {
    if (typeof value !== 'string' || !/^\d+$/.test(value)) return undefined
    const id = Number(value)
    return Number.isSafeInteger(id) && id > 0 ? id : undefined
  }

  function syncConversationUrl(id?: number) {
    if (route.name !== 'Workspace' || route.query.conversation === (id ? String(id) : undefined)) return
    const query = { ...route.query }
    if (id) query.conversation = String(id)
    else delete query.conversation
    if (query.run) delete query.run
    void router.replace({ query })
  }

  // ── 历史面板开合 ───────────────────────────────────────────────────────────
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

  return {
    route, router,
    convMessages, conversations, currentConversation, messages, convArr, pushMsg,
    activeRuns, historyStreams, pendingDecisions, runningConvIds, inactive,
    setActiveRun, clearActiveRun, streaming, historyRun, blockedReason,
    inputText, sending, repairingProject, projectRepairErrors, projectRepairKeys, uploading,
    approvalMode, modelOptions, selectedModelId, currentAssistant, clock,
    authStore, draftTeamMode, rememberedTeamModes, teamPreferenceKey, rememberTeamMode, teamMode,
    composer, historyToggle, historyMedia, historyOpen, historyLoading, historyFailed,
    historyLoadingMore, historyTotal, historyLoadedPages, historyPageSize, hasMoreConversations,
    creatingConversation, deletingId, loadingMessagesFor, failedMessagesFor,
    loadingMessages, messagesFailed, showWelcome,
    creating, historyRevision, requestedHistoryPages,
    documentTask, documentContext, documentPreview, downloadingDocuments,
    documentInputsBusy, documentInputErrors,
    attachments, attachmentUrls, preview, previewState,
    filesPanelOpen, fileList, filesLoading, filesError, filesTruncated,
    workspaceVersion, noteWorkspaceChange,
    isImageName, attachmentUrl, releaseAttachmentUrl, loadFilePreview, clearAttachments,
    closePreview, resetDocumentTask,
    numericQuery, syncConversationUrl, openHistory, closeHistory, syncHistoryVisibility,
  }
}

export type WorkspaceState = ReturnType<typeof useWorkspaceState>
