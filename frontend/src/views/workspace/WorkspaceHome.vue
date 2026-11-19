<template>
  <section class="workspace-home" :class="{ 'has-preview': preview }" aria-label="智能体工作空间">
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
      :status="conversationStatus"
      @close="closeHistory"
      @retry="loadConversations()"
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
                    @click="openPreview('uploads/' + file.name, file.name)"
                  >
                    <img v-if="file.previewUrl" :src="file.previewUrl" alt="" />
                    <FileText v-else :size="16" />
                    <span>{{ file.name }}</span>
                  </button>
                </div>
                {{ m.content }}
              </div>
            </article>

            <article v-else class="msg-assistant" aria-label="助手消息">
              <div class="assistant-mark"><Bot :size="16" /></div>
              <div class="msg-content">
                <div class="msg-heading">
                  <strong>{{ currentAssistant?.name || '助手' }}</strong>
                  <span v-if="runState(m)" class="run-state" :data-state="runState(m)?.tone">{{ runState(m)?.label }}</span>
                  <span v-if="taskTimeText(m)" class="run-duration">{{ taskTimeText(m) }}</span>
                </div>
                <template v-for="(block, blockIdx) in messageBlocks[idx]" :key="block.kind === 'tool' ? block.key : blockIdx">
                  <MessageMarkdown v-if="block.kind === 'text'" :text="block.text" />
                  <ToolStep v-else-if="block.kind === 'tool'" :tool="block" @preview="openPreview" />

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

                <div v-if="m.streaming" class="live-status" role="status">
                  <LoaderCircle class="spin" :size="14" />
                  {{ m.liveStatus || '正在处理…' }}
                </div>
                <p v-if="staleSeconds(m) >= 8" class="stale-hint">已 {{ staleSeconds(m) }} 秒无新进展，任务仍在执行。</p>

                <div v-if="m.error" class="msg-error" role="alert"><CircleAlert :size="16" />{{ m.error }}</div>
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
          v-model:approval-mode="approvalMode"
          v-model:model="selectedModelId"
          :options="modelOptions"
          :attachments="attachments"
          :uploading="uploading"
          :sending="sending"
          :stoppable="streaming"
          :blocked-reason="blockedReason"
          :locked="loadingMessages || messagesFailed"
          @send="sendMessage"
          @stop="stopGeneration"
          @upload="uploadAttachments"
          @remove="removeAttachment"
          @preview="openPreview"
        />
      </div>
    </section>

    <FilePreviewPanel
      v-if="preview"
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
  ArrowDown, Bot, ChartNoAxesCombined, Circle, CircleAlert, CircleCheck, CircleDot,
  Copy, ExternalLink, FilePenLine, FileText, Layers, ListTodo, LoaderCircle, PanelLeft, RotateCcw, Search,
} from '@lucide/vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { modelApi } from '@/api'
import {
  conversationApi, consumeRunEvents, runApi, workspaceApi,
  type Assistant, type Conversation, type PreviewContent,
} from '@/api/v2'
import {
  applyRunEvent, finalizeRunMessage, hasVisibleText,
  type MsgBlock, type RunStreamMessage, type ToolBlock,
} from '@/features/run/blocks'
import ToolStep from '@/features/run/ToolStep.vue'
import ApprovalPanel from '@/features/run/ApprovalPanel.vue'
import MessageMarkdown from '@/features/run/MessageMarkdown.vue'
import FilePreviewPanel from '@/features/preview/FilePreviewPanel.vue'
import ConversationComposer from './ConversationComposer.vue'
import ConversationHistory from './ConversationHistory.vue'
import { hasScrolledAway, isNearLatest } from './interactions'

interface ChatMessage extends RunStreamMessage {
  role: 'user' | 'assistant'
  content: string
  runId?: string
  /** 用户消息携带的附件（历史回放时从附件注记恢复；图片带本地预览 URL） */
  files?: Array<{ name: string; previewUrl?: string }>
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
const composer = ref<InstanceType<typeof ConversationComposer>>()
const historyToggle = ref<HTMLButtonElement>()
const historyMedia = window.matchMedia('(min-width: 850px)')
const historyOpen = ref(historyMedia.matches)
const historyLoading = ref(false)
const historyFailed = ref(false)
const creatingConversation = ref(false)
const deletingId = ref<number | null>(null)
const loadingMessagesFor = reactive(new Set<number>())
const failedMessagesFor = reactive(new Set<number>())
const loadingMessages = computed(() => loadingMessagesFor.has(currentConversation.value?.id ?? -1))
const messagesFailed = computed(() => failedMessagesFor.has(currentConversation.value?.id ?? -1))
const showWelcome = computed(() => !messages.value.length && !loadingMessages.value && !messagesFailed.value)
const sending = ref(false)
const uploading = ref(0)
let creating: Promise<Conversation> | undefined
let historyRevision = 0
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
  void router.replace({ query })
}

function openLinkedConversation() {
  const id = route.query.conversation
  if (typeof id !== 'string' || historyLoading.value || historyFailed.value || inactive) return
  const item = conversations.value.find((conversation) => String(conversation.id) === id)
  if (item && currentConversation.value?.id !== item.id) void selectConversation(item)
}

watch(() => route.query.conversation, openLinkedConversation)

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
const blockedReason = computed(() => {
  if (streaming.value) return '任务运行中，可以停止生成。'
  if (messages.value.some((m) => m.pendingActions?.length)) return '请先处理消息中的审批请求。'
  if (messages.value.some((m) => m.streaming)) return '正在恢复执行进度…'
  return ''
})

const conversations = ref<Conversation[]>([])
const currentConversation = ref<Conversation | null>(null)
/** 当前对话使用的助手：固定为 bootstrap 下发的默认助手（不再提供助手管理入口） */
const currentAssistant = ref<Assistant | null>(null)

function conversationStatus(c: Conversation) {
  if (runningConvIds.value.includes(c.id)) return '执行中'
  const cached = convMessages.get(c.id)
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
  needs_reconciliation: { label: '等待处理', tone: 'warning' },
  completed: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  cancelled: { label: '已停止', tone: 'neutral' },
  suspended: { label: '已挂起', tone: 'warning' },
  limit_exceeded: { label: '达到执行上限', tone: 'warning' },
}

function runState(message: ChatMessage) {
  if (message.pendingActions?.length) return statuses.waiting_approval
  if (message.streaming && message.runStatus !== 'pending') return statuses.running
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
  if (!refresh) historyLoading.value = true
  historyFailed.value = false
  try {
    const res = await conversationApi.list()
    if (revision === historyRevision) {
      conversations.value = res.items || []
      const refreshed = conversations.value.find((item) => item.id === currentConversation.value?.id)
      if (refreshed) currentConversation.value = refreshed
    }
  } catch {
    if (revision === historyRevision && !conversations.value.length) historyFailed.value = true
  } finally {
    if (revision === historyRevision) historyLoading.value = false
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
  try {
    const c = await createConversation()
    if (currentConversation.value?.id === c.id) focusConversation()
  } catch { /* 请求层提示失败，用户可重试 */ }
}

/** 无会话时（上传附件 / 首条消息）自动创建会话 */
function createConversation(): Promise<Conversation> {
  if (creating) return creating
  const origin = currentConversation.value?.id
  creatingConversation.value = true
  creating = conversationApi.create({ assistant_id: currentAssistant.value?.id }).then((c) => {
    conversations.value.unshift(c)
    convMessages.set(c.id, [])
    if (currentConversation.value?.id === origin) switchConversation(c)
    void loadConversations(true)
    return c
  }).finally(() => {
    creating = undefined
    creatingConversation.value = false
  })
  return creating
}

function ensureConversation() {
  return currentConversation.value ?? createConversation()
}

function disposeAttachments() {
  for (const file of attachments.value) releaseAttachmentUrl(file.previewUrl)
  attachments.value = []
}

function switchConversation(c: Conversation | null) {
  composer.value?.cancelDictation()
  disposeAttachments()
  closePreview()
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

async function selectConversation(c: Conversation) {
  if (currentConversation.value?.id !== c.id) switchConversation(c)
  if (!historyMedia.matches) closeHistory()
  if (loadingMessagesFor.has(c.id)) return
  if (convMessages.has(c.id) && !failedMessagesFor.has(c.id)) {
    await forceScrollToBottom()
    return
  }
  loadingMessagesFor.add(c.id)
  failedMessagesFor.delete(c.id)
  try {
    const res = await conversationApi.messages(c.id)
    if (inactive || !conversations.value.some((item) => item.id === c.id)) return
    const arr = convArr(c.id)
    const raw: any[] = res.items || []
    for (const m of raw) {
      if (m.role === 'user') {
        const { text, files } = splitAttachmentNote(m.content || '')
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
        const msg = pushMsg(c.id, {
          role: 'assistant', content, blocks: [], runId,
          streaming: true, runStatus: '', pendingActions: [],
          lastActivity: 'other', lastEventAt: Date.now(),
          liveStatus: '正在恢复执行进度…',
        })
        const controller = new AbortController()
        historyStreams.set(runId, controller)
        void consumeRunEvents(runId, {
          signal: controller.signal,
          afterSeq: 0,
          onEvent: (envelope) => {
            applyRunEvent(msg, envelope)
            if (currentConversation.value?.id === c.id) void scrollToBottom()
          },
          onDone: (reason) => finalizeRunMessage(msg, reason, msg.content),
        }).catch(() => {
          msg.streaming = false
          msg.liveStatus = ''
        }).finally(() => {
          historyStreams.delete(runId)
        })
      }
    }
  } catch {
    convMessages.delete(c.id)
    failedMessagesFor.add(c.id)
  } finally {
    loadingMessagesFor.delete(c.id)
    if (currentConversation.value?.id === c.id && !failedMessagesFor.has(c.id)) await forceScrollToBottom()
  }
}

async function removeConversation(c: Conversation) {
  if (deletingId.value !== null) return
  deletingId.value = c.id
  try {
    await ElMessageBox.confirm(`确定删除「${c.title || '新对话'}」？`, '删除对话', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '保留',
    })
  } catch {
    deletingId.value = null
    return
  }
  try {
    const entry = activeRuns.get(c.id)
    if (entry && !entry.msg.runId) {
      ElMessage.info('任务正在创建，请稍后再删除对话')
      return
    }
    const runId = entry?.msg.runId
    if (runId) await runApi.cancel(runId, '删除会话前取消任务')
    await conversationApi.delete(c.id)
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

/** 剥离用户消息尾部的附件注记块（后端注入），还原正文与附件列表 */
function splitAttachmentNote(content: string): {
  text: string
  files: Array<{ name: string; previewUrl?: string }>
} {
  const marker = '\n\n[附件]'
  const idx = content.indexOf(marker)
  if (idx < 0) return { text: content, files: [] }
  const files = content
    .slice(idx + marker.length)
    .split('\n')
    .map((l) => l.replace(/^-\s*/, '').replace(/（.*$/, '').replace(/^\/?workspace\//, '').trim())
    .filter(Boolean)
    .map((p) => ({ name: p.replace(/^uploads\//, '') }))
  return { text: content.slice(0, idx), files }
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

/** 打开右侧预览（path 为工作区相对路径；name 为展示名） */
async function openPreview(path: string, name?: string) {
  const conv = currentConversation.value
  if (!conv || !path) return
  const display = name || path.replace(/\\/g, '/').split('/').filter(Boolean).pop() || path
  const previousUrl = preview.value?.data?.objectUrl
  if (previousUrl) URL.revokeObjectURL(previousUrl)
  const generation = ++previewGeneration
  preview.value = { name: display, path, loading: true, error: '', data: null }
  try {
    const meta = await conversationApi.filePreview(conv.id, path)
    if (generation !== previewGeneration) return
    let objectUrl: string | undefined
    if (meta.kind === 'image' || meta.kind === 'pdf') {
      const blob = await conversationApi.fileBytes(conv.id, path)
      if (generation !== previewGeneration) return
      objectUrl = URL.createObjectURL(blob)
    }
    preview.value = {
      name: meta.name || display, path: meta.path || path,
      loading: false, error: '', data: { ...meta, objectUrl },
    }
  } catch (e: any) {
    if (generation !== previewGeneration) return
    const detail = e?.response?.data?.detail
    preview.value = {
      name: display, path, loading: false,
      error: typeof detail === 'string' ? detail : '无法加载文件预览', data: null,
    }
  }
}

/** 关闭预览（回收 objectURL，防内存泄漏） */
function closePreview() {
  ++previewGeneration
  const url = preview.value?.data?.objectUrl
  if (url) URL.revokeObjectURL(url)
  preview.value = null
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
  if (preview.value) closePreview()
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
  if (message.files?.length) ElMessage.info('请为重试重新选择附件')
  composer.value?.focus()
}

async function sendMessage() {
  const text = inputText.value.trim()
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
  const sentFiles = attachments.value.map((a) => ({ ...a }))
  const sentModelId = selectedModelId.value
  const sentMode = approvalMode.value

  inputText.value = ''
  attachments.value = []
  pushMsg(convId, {
    role: 'user', content: text, blocks: [],
    files: sentFiles.map((a) => ({ name: a.name, previewUrl: a.previewUrl })),
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
      attachments: sentFiles,
    })
    if (entry.stopped) {
      if (!inactive) runApi.cancel(created.run_id, '用户主动停止').catch(() => {
        ElMessage.warning('取消请求失败，任务可能继续执行')
      })
      return
    }
    aMsg.runId = created.run_id

    consumeRunEvents(created.run_id, {
      signal: entry.abort.signal,
      afterSeq: 0,
      onEvent: (envelope) => {
        if (entry.stopped) return
        applyRunEvent(aMsg, envelope)
        if (currentConversation.value?.id === convId) scrollToBottom()
      },
      onReconnecting: (attempt) => {
        if (entry.stopped) return
        aMsg.liveStatus = `连接中断，正在重连（第 ${attempt} 次）…`
      },
      onDone: (reason) => {
        if (entry.stopped) return
        finishRun(convId, entry, reason)
      },
    }).catch(() => { /* 网络异常已由 onDone 兜底 */ })
  } catch {
    if (entry.stopped) return
    aMsg.streaming = false
    aMsg.liveStatus = ''
    aMsg.runStatus = 'failed'
    aMsg.error = '创建任务失败，可编辑原消息后重新发送。'
    clearActiveRun(convId, entry)
    if (currentConversation.value?.id === convId && !inputText.value) inputText.value = text
  }
}

function finishRun(convId: number, entry: ActiveRun, reason: string) {
  if (activeRuns.get(convId) !== entry) return
  finalizeRunMessage(entry.msg, reason, entry.msg.content)
  clearActiveRun(convId, entry)
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
  if (!entry) return
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
    if (!inactive && convMessages.get(convId)?.includes(message)) observeResumedRun(convId, message, type)
  } catch { /* 请求层显示错误，保留待审批操作以便重试 */ }
  finally {
    pendingDecisions.delete(runId)
  }
}

function observeResumedRun(convId: number, aMsg: ChatMessage, type: 'approve' | 'reject') {
  if (!aMsg.runId) return
  const previous = activeRuns.get(convId)
  if (previous) {
    previous.stopped = true
    previous.abort.abort()
  }
  historyStreams.get(aMsg.runId)?.abort()
  aMsg.pendingActions = []
  aMsg.streaming = true
  aMsg.lastActivity = 'other'
  aMsg.lastEventAt = Date.now()
  aMsg.liveStatus = type === 'approve' ? '已批准，恢复执行…' : '已拒绝，恢复执行…'

  const entry: ActiveRun = { abort: new AbortController(), stopped: false, msg: aMsg }
  setActiveRun(convId, entry)

  consumeRunEvents(aMsg.runId, {
    signal: entry.abort.signal,
    afterSeq: aMsg.lastSeq || 0,
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
.has-preview .ws-history { flex-basis: 192px; }
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
