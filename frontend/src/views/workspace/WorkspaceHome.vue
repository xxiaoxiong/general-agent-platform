<template>
  <div class="workspace-home">
    <!-- 左侧：最近对话 -->
    <aside class="ws-sidebar">
      <el-button type="primary" class="new-chat-btn" @click="newConversation">
        <el-icon><Plus /></el-icon> 新建对话
      </el-button>

      <div class="sidebar-section">
        <div class="sidebar-title">最近对话</div>
        <div
          v-for="c in conversations"
          :key="c.id"
          class="conv-item"
          :class="{ active: currentConversation?.id === c.id, 'is-running': runningConvIds.includes(c.id) }"
          @click="selectConversation(c)"
        >
          <span v-if="runningConvIds.includes(c.id)" class="conv-run-dot"></span>
          <span class="conv-title">{{ c.title || '新对话' }}</span>
          <el-icon class="conv-delete" @click.stop="removeConversation(c)"><Delete /></el-icon>
        </div>
        <div v-if="!conversations.length" class="sidebar-empty">暂无对话</div>
      </div>
    </aside>

    <!-- 右侧：对话区 -->
    <main class="ws-main">
      <div class="ws-header">
        <span class="ws-assistant-name">{{ currentAssistant?.name || '通用助手' }}</span>
      </div>

      <div
        ref="messagesRef"
        class="ws-messages"
        @scroll.passive="onMessagesScroll"
        @wheel.passive="onWheelScroll"
      >
        <div v-if="!messages.length" class="ws-empty">
          <h2>有什么可以帮你？</h2>
          <p>直接输入问题开始对话，或试试：</p>
          <div class="ws-examples">
            <div class="ws-example" @click="sendExample('帮我分析这份数据的趋势并给出结论')">数据分析</div>
            <div class="ws-example" @click="sendExample('搜索行业最新动态并生成结构化研究摘要')">行业研究</div>
            <div class="ws-example" @click="sendExample('帮我把这段内容整理成一份正式报告')">生成报告</div>
          </div>
        </div>

        <template v-for="(m, idx) in messages" :key="idx">
          <!-- 用户消息（id 供右侧提问圆点导航锚点跳转） -->
          <div v-if="m.role === 'user'" :id="`q-${idx}`" class="msg-row msg-user">
            <div class="msg-bubble">
              <div v-if="m.files?.length" class="msg-files">
                <span v-for="f in m.files" :key="f" class="msg-file-tag">
                  <el-icon><Paperclip /></el-icon> {{ f }}
                </span>
              </div>
              {{ m.content }}
            </div>
          </div>

          <!-- 助手消息：文本段 / 工具小步骤按事件顺序实时穿插在对话流尾部 -->
          <div v-else class="msg-row msg-assistant">
            <div class="msg-content">
              <template v-for="(b, bi) in displayBlocks(m, idx)" :key="bi">
                <div v-if="b.kind === 'text'" class="msg-text" v-html="renderMarkdown(b.text)"></div>

                <ToolStep v-else-if="b.kind === 'tool'" :tool="b" />

                <div v-else-if="b.kind === 'plan'" class="plan-inline">
                  <div class="plan-inline-title">任务计划</div>
                  <div v-for="(t, ti) in planItems(b.todos)" :key="ti" class="plan-inline-item">
                    <span class="plan-mark" :class="'st-' + (t.status || '')">{{ planMark(t.status) }}</span>
                    <span :class="{ 'plan-done': t.status === 'completed' }">{{ t.content }}</span>
                  </div>
                </div>

                <!-- 完成后的连续工具步骤折叠为摘要（不刷屏），可展开回看 -->
                <div v-else-if="b.kind === 'group'" class="tool-group">
                  <div class="tool-group-head" @click="toggleGroup(b.key)">
                    <span class="tool-state-mark">✓</span>
                    <span>{{ b.items.length }} 个工具调用<span v-if="groupFailCount(b)">（{{ groupFailCount(b) }} 个失败）</span></span>
                    <el-icon class="row-chevron"><component :is="b.open ? 'ArrowDown' : 'ArrowRight'" /></el-icon>
                  </div>
                  <template v-if="b.open">
                    <ToolStep v-for="it in b.items" :key="it.key" :tool="it" />
                  </template>
                </div>
              </template>

              <!-- 打字光标：仅"正在生成文本"时出现；工具执行期间用状态行表示 -->
              <div v-if="m.streaming && m.lastActivity === 'text'" class="typing-cursor-line">
                <span class="typing-cursor"></span>
              </div>

              <!-- 实时状态行：明确告知当前在做什么；久无进展时给出等待提示（不是卡死） -->
              <div v-if="m.streaming" class="live-status">
                <span class="mini-spin"></span>
                <span>{{ m.liveStatus || '正在处理…' }}</span>
                <span v-if="staleSeconds(m) >= 8" class="stale-hint">
                  · 已 {{ staleSeconds(m) }}s 无新进展，仍在执行
                </span>
              </div>

              <div v-if="m.error" class="msg-error">{{ m.error }}</div>
              <ApprovalPanel
                v-if="m.runId && m.pendingActions?.length"
                :run-id="m.runId"
                :actions="m.pendingActions"
                @decided="onApprovalDecided"
              />
            </div>
          </div>
        </template>
      </div>

      <!-- 提问锚点导航：右侧纵向圆点，点击跳转对应提问，悬浮气泡预览提问内容 -->
      <div v-if="questionAnchors.length" class="q-nav">
        <el-tooltip
          v-for="q in questionAnchors"
          :key="q.idx"
          :content="q.preview"
          placement="left"
          :show-after="80"
          popper-class="q-nav-popper"
        >
          <button type="button" class="q-nav-dot" @click="jumpToQuestion(q.idx)"></button>
        </el-tooltip>
      </div>

      <!-- 输入区（执行中可继续浏览/切换；当前会话运行时可停止） -->
      <div class="ws-composer">
        <div class="composer-perm">
          <span class="perm-label">模型</span>
          <el-select v-model="selectedModelId" class="model-select" size="small">
            <el-option label="默认模型" :value="0" />
            <el-option
              v-for="m in modelOptions"
              :key="m.id"
              :label="m.display_name || m.name"
              :value="m.id"
            />
          </el-select>
          <span class="perm-label">审批模式</span>
          <el-radio-group v-model="approvalMode" size="small">
            <el-radio-button value="manual">手动审批</el-radio-button>
            <el-radio-button value="auto">自动审批</el-radio-button>
          </el-radio-group>
          <span class="perm-hint">
            {{ approvalMode === 'manual'
              ? '高危工具（执行代码 / 外发消息 / 删除等）执行前需人工批准'
              : '高危工具自动放行，执行过程不再逐次询问' }}
          </span>
        </div>
        <!-- 待发送附件（文件已上传到会话工作区，发送时告知模型读取） -->
        <div v-if="attachments.length" class="attach-strip">
          <span v-for="(a, i) in attachments" :key="a.path" class="attach-chip">
            <el-icon><Paperclip /></el-icon>
            <span class="attach-name">{{ a.name }}</span>
            <el-icon class="attach-remove" @click="removeAttachment(i)"><Close /></el-icon>
          </span>
        </div>
        <div v-if="streaming" class="composer-running">
          <span class="mini-spin"></span>
          <span>{{ streamingStatus }}</span>
          <el-button size="small" type="danger" plain @click="stopGeneration">
            <el-icon><VideoPause /></el-icon> 停止
          </el-button>
        </div>
        <div class="composer-input">
          <el-upload
            class="attach-upload"
            :show-file-list="false"
            :http-request="handleUpload"
            multiple
          >
            <el-button class="attach-btn" text title="上传文件">
              <el-icon size="18"><Paperclip /></el-icon>
            </el-button>
          </el-upload>
          <el-input
            v-model="inputText"
            type="textarea"
            :rows="2"
            placeholder="输入消息，Enter 发送（Shift+Enter 换行）"
            @keydown.enter.exact.prevent="sendMessage"
          />
          <el-button
            type="primary"
            :disabled="streaming || !inputText.trim()"
            @click="sendMessage"
          >
            发送
          </el-button>
        </div>
      </div>
    </main>
  </div>
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
import { Delete, Paperclip, Plus, VideoPause } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { modelApi } from '@/api'
import {
  conversationApi, consumeRunEvents, runApi, workspaceApi,
  type Assistant, type Conversation,
} from '@/api/v2'
import {
  applyRunEvent, finalizeRunMessage, hasVisibleText,
  type MsgBlock, type RunStreamMessage, type ToolBlock,
} from '@/features/run/blocks'
import ToolStep from '@/features/run/ToolStep.vue'
import ApprovalPanel from '@/features/run/ApprovalPanel.vue'

interface ChatMessage extends RunStreamMessage {
  role: 'user' | 'assistant'
  content: string
  runId?: string
  /** 用户消息携带的附件名（历史回放时从附件注记恢复） */
  files?: string[]
}

/** 折叠后的连续工具步骤组（完成后折叠防刷屏） */
interface ToolGroupItem {
  kind: 'group'
  key: string
  items: ToolBlock[]
  open: boolean
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
const inputText = ref('')

// 当前会话是否有活跃 Run（各会话互不影响，可并行执行）
// 注意：entry 含 AbortController 等类实例且需要引用相等比较，必须用普通 Map；
// 响应式仅体现在 runningConvIds（供模板/计算属性跟踪）
const activeRuns = new Map<number, ActiveRun>()
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
const streamingStatus = computed(() => {
  const entry = currentConversation.value && activeRuns.get(currentConversation.value.id)
  return entry?.msg.liveStatus || '任务执行中…'
})

const conversations = ref<Conversation[]>([])
const currentConversation = ref<Conversation | null>(null)
/** 当前对话使用的助手：固定为 bootstrap 下发的默认助手（不再提供助手管理入口） */
const currentAssistant = ref<Assistant | null>(null)

// 权限模块：审批模式（manual=高危工具人工审批；auto=自动放行不询问），随 Run 创建下发
const approvalMode = ref<'manual' | 'auto'>(
  localStorage.getItem('approval_mode') === 'auto' ? 'auto' : 'manual',
)
watch(approvalMode, (v) => localStorage.setItem('approval_mode', v))

// 模型选择（手动选模型仅本次 Run 生效；0 = 助手默认模型）
const modelOptions = ref<any[]>([])
const selectedModelId = ref<number>(0)

// 待发送附件（上传即写入会话工作区，随消息告知模型读取；切换会话即清空）
const attachments = ref<Array<{ name: string; path: string; text_path: string | null; size: number }>>([])

// 停滞检测：长时间无新事件时明确提示"仍在执行"，避免误以为卡死
const clock = ref(Date.now())
let clockTimer: number | undefined

function staleSeconds(m: ChatMessage): number {
  if (!m.streaming || !m.lastEventAt) return 0
  return Math.max(0, Math.floor((clock.value - m.lastEventAt) / 1000))
}

// Markdown 渲染（marked + DOMPurify）：回答主体是用户的核心产物，必须完整渲染
// marked v12 renderer 收 token 对象；兼容旧式 (code, lang) 双签名
function renderCodeBlock(code: string, lang?: string) {
  const langClass = lang ? ` class="language-${lang}"` : ''
  const escaped = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return `<div class="code-block-wrapper"><button class="copy-btn" onclick="(function(btn){var code=btn.nextElementSibling.querySelector('code');navigator.clipboard.writeText(code.innerText).then(function(){btn.textContent='已复制';setTimeout(function(){btn.textContent='复制'},1500)}).catch(function(){btn.textContent='失败'});})(this)">复制</button><pre><code${langClass}>${escaped}</code></pre></div>`
}

marked.use({
  breaks: true,
  gfm: true,
  renderer: {
    code(arg1: any, arg2?: string) {
      if (arg1 && typeof arg1 === 'object') {
        return renderCodeBlock(String(arg1.text || ''), arg1.lang || undefined)
      }
      return renderCodeBlock(String(arg1 || ''), arg2)
    },
    link(href: string, title: string | null, text: string) {
      const titleAttr = title ? ` title="${title}"` : ''
      return `<a href="${href}" target="_blank" rel="noopener noreferrer"${titleAttr}>${text}</a>`
    },
  } as any,
})

function renderMarkdown(text: string): string {
  if (!text) return ''
  const raw = marked.parse(text) as string
  return DOMPurify.sanitize(raw)
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
/** 最近一次钉底时的 scrollTop：内容撑开不改变 scrollTop，用户上翻才会 */
let lastPinnedTop = -1

function onMessagesScroll() {
  const el = messagesRef.value
  if (!el) return
  stickToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
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
  if (lastPinnedTop >= 0 && el.scrollTop < lastPinnedTop - 40) {
    stickToBottom.value = false
    return
  }
  el.scrollTop = el.scrollHeight
  lastPinnedTop = el.scrollTop
}

/** 强制回到底部并恢复跟随（切换会话/发送消息/任务开始等明确意图） */
async function forceScrollToBottom() {
  stickToBottom.value = true
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
    .map(({ m, idx }) => ({ idx, preview: questionPreview(m.content) })),
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

const groupOpen = reactive<Record<string, boolean>>({})

function toggleGroup(key: string) {
  groupOpen[key] = !groupOpen[key]
}

function groupFailCount(g: ToolGroupItem): number {
  return g.items.filter((i) => i.state === 'failed').length
}

/** markdown 渲染后无可见内容（只剩注释/标记/不可见字符）的文本块视为空块 */
function rendersEmpty(md: string): boolean {
  const tmp = document.createElement('div')
  tmp.innerHTML = renderMarkdown(md)
  const text = (tmp.textContent || '').replace(new RegExp('[\\s\\u00ad\\u200b-\\u200f\\u2060\\ufeff]', 'g'), '')
  return !text && !tmp.querySelector('img, pre, table, hr')
}

/** 计划清单可见条目（无内容条目不渲染，防白板） */
function planItems(todos: any[]): any[] {
  return (todos || []).filter((t) => hasVisibleText(String(t?.content ?? '')))
}

function displayBlocks(m: ChatMessage, msgIdx: number): DisplayItem[] {
  // 空白文本块（模型偶发的纯空白/零宽增量）与空计划块不渲染，避免"白板区块"
  const blocks = m.blocks.filter((b) => {
    if (b.kind === 'text') return hasVisibleText(b.text) && !rendersEmpty(b.text)
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
        const key = `${msgIdx}-${i}`
        out.push({ kind: 'group', key, items: group, open: !!groupOpen[key] })
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

function planMark(status: string) {
  return { completed: '[x]', in_progress: '[>]' }[status] || '[ ]'
}

// ── 初始化 ────────────────────────────────────────────────────────────────────

onMounted(async () => {
  clockTimer = window.setInterval(() => { clock.value = Date.now() }, 1000)
  const boot = await workspaceApi.bootstrap()
  currentAssistant.value = boot.default_assistant
  modelApi.listConfigs({ model_type: 'chat' }).then((list) => {
    modelOptions.value = (list || []).filter((m: any) => m.is_active)
  }).catch(() => { /* 模型列表加载失败不阻断对话 */ })
  await loadConversations()
})

onUnmounted(() => {
  if (clockTimer) clearInterval(clockTimer)
  for (const entry of activeRuns.values()) {
    entry.stopped = true
    entry.abort.abort()
  }
  runningConvIds.value = []
})

async function loadConversations() {
  const res = await conversationApi.list()
  conversations.value = res.items || []
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
  const c = await conversationApi.create({ assistant_id: currentAssistant.value?.id })
  conversations.value.unshift(c)
  currentConversation.value = c
  convMessages.set(c.id, [])
  attachments.value = []
}

/** 无会话时（上传附件 / 首条消息）自动创建会话 */
async function ensureConversation(): Promise<Conversation> {
  if (currentConversation.value) return currentConversation.value
  const c = await conversationApi.create({ assistant_id: currentAssistant.value?.id })
  conversations.value.unshift(c)
  currentConversation.value = c
  convMessages.set(c.id, [])
  return c
}

async function selectConversation(c: Conversation) {
  currentConversation.value = c
  attachments.value = []
  if (convMessages.has(c.id)) {
    await forceScrollToBottom()
    return
  }
  const arr = convArr(c.id)
  try {
    const res = await conversationApi.messages(c.id)
    const raw: any[] = res.items || []
    for (const m of raw) {
      if (m.role === 'user') {
        const { text, files } = splitAttachmentNote(m.content || '')
        arr.push({ role: 'user', content: text, blocks: [], files })
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
        consumeRunEvents(runId, {
          afterSeq: 0,
          onEvent: (envelope) => applyRunEvent(msg, envelope),
          onDone: (reason) => finalizeRunMessage(msg, reason, msg.content),
        }).catch(() => {
          msg.streaming = false
          msg.liveStatus = ''
        })
      }
    }
    await forceScrollToBottom()
  } catch { /* ignore */ }
}

async function removeConversation(c: Conversation) {
  await ElMessageBox.confirm('确定删除此对话？', '确认', { type: 'warning' })
  const entry = activeRuns.get(c.id)
  if (entry) {
    entry.stopped = true
    entry.abort.abort()
    clearActiveRun(c.id)
    if (entry.msg.runId) runApi.cancel(entry.msg.runId, '会话已删除').catch(() => {})
  }
  await conversationApi.delete(c.id)
  conversations.value = conversations.value.filter((x) => x.id !== c.id)
  convMessages.delete(c.id)
  if (currentConversation.value?.id === c.id) {
    currentConversation.value = null
  }
}

// ── 会话附件（上传即入会话工作区；发送时随消息告知模型读取）────────────────

/** 剥离用户消息尾部的附件注记块（后端注入），还原正文与附件名列表 */
function splitAttachmentNote(content: string): { text: string; files: string[] } {
  const marker = '\n\n[附件]'
  const idx = content.indexOf(marker)
  if (idx < 0) return { text: content, files: [] }
  const files = content
    .slice(idx + marker.length)
    .split('\n')
    .map((l) => l.replace(/^-\s*/, '').replace(/（.*$/, '').replace(/^\/?workspace\//, '').trim())
    .filter(Boolean)
    .map((p) => p.replace(/^uploads\//, ''))
  return { text: content.slice(0, idx), files }
}

async function handleUpload(req: { file: File }) {
  try {
    const conv = await ensureConversation()
    const res = await conversationApi.uploadFile(conv.id, req.file)
    attachments.value.push(res)
  } catch { /* 错误提示已由请求层统一弹出 */ }
}

async function removeAttachment(i: number) {
  const a = attachments.value[i]
  attachments.value.splice(i, 1)
  if (currentConversation.value && a) {
    conversationApi.deleteFile(currentConversation.value.id, a.name).catch(() => {})
  }
}

// ── 发送与事件流消费（§7.1 v2 协议 + §8.4 重连）──────────────────────────────

function sendExample(text: string) {
  inputText.value = text
  sendMessage()
}

async function sendMessage() {
  const text = inputText.value.trim()
  if (!text || streaming.value) return

  const conv = await ensureConversation()
  const convId = conv.id
  const sentFiles = attachments.value.map((a) => ({ ...a }))
  const sentModelId = selectedModelId.value

  inputText.value = ''
  attachments.value = []
  pushMsg(convId, {
    role: 'user', content: text, blocks: [],
    files: sentFiles.map((a) => a.name),
  })
  const aMsg = pushMsg(convId, {
    role: 'assistant', content: '', blocks: [], streaming: true,
    lastActivity: 'other', lastEventAt: Date.now(),
    liveStatus: '正在创建任务…', runStatus: 'pending', pendingActions: [],
  })
  await forceScrollToBottom()

  const entry: ActiveRun = { abort: new AbortController(), stopped: false, msg: aMsg }
  setActiveRun(convId, entry)

  try {
    const created = await runApi.create({
      message: text,
      conversation_id: convId,
      assistant_id: currentAssistant.value?.id,
      approval_mode: approvalMode.value,
      model_config_id: sentModelId || undefined,
      attachments: sentFiles,
    })
    if (entry.stopped) return
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
  } catch (e: any) {
    if (entry.stopped) return
    aMsg.streaming = false
    aMsg.liveStatus = ''
    aMsg.error = `执行出错: ${e.message}`
    clearActiveRun(convId, entry)
    ElMessage.error('请求失败: ' + e.message)
  }
}

function finishRun(convId: number, entry: ActiveRun, reason: string) {
  finalizeRunMessage(entry.msg, reason, entry.msg.content)
  clearActiveRun(convId, entry)
  loadConversations().catch(() => {}) // 标题已规范化生成，刷新会话列表
  if (currentConversation.value?.id === convId) scrollToBottom()
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

function onApprovalDecided(type: 'approve' | 'reject') {
  const convId = currentConversation.value?.id
  if (!convId) return
  const arr = convArr(convId)
  const aMsg = arr.find((m) => m.role === 'assistant' && m.pendingActions?.length)
  if (!aMsg?.runId) return
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
  display: flex;
  height: calc(100vh - 56px);
  background: #fff;
}

/* 左栏 */
.ws-sidebar {
  width: 240px;
  border-right: 1px solid #e4e7ed;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow-y: auto;
}
.new-chat-btn { width: 100%; }
.sidebar-title { font-size: 12px; color: #909399; margin-bottom: 6px; }
.conv-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: #606266;
}
.conv-item:hover { background: #f5f7fa; }
.conv-item.active { background: #ecf5ff; color: #409eff; }
.conv-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-delete { opacity: 0; color: #c0c4cc; }
.conv-item:hover .conv-delete { opacity: 1; }
.conv-delete:hover { color: #f56c6c; }
/* 后台运行中的会话指示（切换不打断执行） */
.conv-run-dot {
  width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
  background: #409eff; animation: pulse 1.2s infinite ease-in-out;
}
@keyframes pulse { 0%, 100% { opacity: 0.35; transform: scale(0.8); } 50% { opacity: 1; transform: scale(1); } }
.sidebar-empty { color: #c0c4cc; font-size: 12px; }

/* 主区 */
.ws-main { flex: 1; display: flex; flex-direction: column; min-width: 0; position: relative; }
.ws-header {
  padding: 10px 20px;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  gap: 12px;
}
.ws-assistant-name { font-weight: 600; }

/* 右侧预留提问圆点导航宽度，避免内容压到圆点下 */
.ws-messages { flex: 1; overflow-y: auto; padding: 20px 36px 20px 20px; }

/* 提问锚点导航：右侧纵向小圆点，点击快速跳转对应提问 */
.q-nav {
  position: absolute;
  right: 12px;
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  max-height: 68%;
  overflow-y: auto;
  scrollbar-width: none;
  z-index: 2;
}
.q-nav::-webkit-scrollbar { display: none; }
.q-nav-dot {
  width: 8px;
  height: 8px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: #dcdfe6;
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.15s ease, transform 0.15s ease;
}
.q-nav-dot:hover { background: #409eff; transform: scale(1.35); }
.ws-empty { text-align: center; color: #909399; margin-top: 10vh; }
.ws-examples {
  display: flex;
  gap: 12px;
  justify-content: center;
  margin-top: 16px;
  flex-wrap: wrap;
}
.ws-example {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  padding: 10px 16px;
  cursor: pointer;
  font-size: 13px;
}
.ws-example:hover { border-color: #409eff; color: #409eff; }

.msg-row { display: flex; margin-bottom: 16px; }
.msg-user { justify-content: flex-end; }
.msg-bubble {
  max-width: 70%;
  background: #409eff;
  color: #fff;
  border-radius: 12px;
  padding: 10px 14px;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg-assistant { justify-content: flex-start; }
.msg-content { max-width: 86%; width: 100%; }
.msg-text {
  background: #f5f7fa;
  border-radius: 12px;
  padding: 12px 16px;
  line-height: 1.7;
  word-break: break-word;
  font-size: 14px;
  color: #303133;
}
/* Markdown 排版（回答是核心产物，完整渲染） */
.msg-text :deep(h1), .msg-text :deep(h2), .msg-text :deep(h3) {
  font-weight: 600; color: #0d0d0d; margin: 12px 0 6px;
}
.msg-text :deep(h1) { font-size: 19px; }
.msg-text :deep(h2) { font-size: 17px; }
.msg-text :deep(h3) { font-size: 15px; }
.msg-text :deep(p) { margin: 6px 0; }
.msg-text :deep(ul), .msg-text :deep(ol) { padding-left: 22px; margin: 6px 0; }
.msg-text :deep(li) { margin: 3px 0; }
.msg-text :deep(code) {
  background: #e8eaef; color: #c7254e;
  padding: 2px 6px; border-radius: 4px; font-family: 'Consolas', monospace; font-size: 13px;
}
.msg-text :deep(.code-block-wrapper) { position: relative; margin: 10px 0; }
.msg-text :deep(.copy-btn) {
  position: absolute; top: 6px; right: 8px;
  background: rgba(255,255,255,0.12); color: #d4d4d4;
  border: 1px solid rgba(255,255,255,0.2); border-radius: 4px;
  padding: 2px 10px; font-size: 12px; cursor: pointer; z-index: 1; line-height: 1.6;
}
.msg-text :deep(.copy-btn:hover) { background: rgba(255,255,255,0.22); color: #fff; }
.msg-text :deep(pre) {
  background: #1e1e1e; border-radius: 8px;
  padding: 12px 14px; overflow-x: auto; margin: 0; font-size: 13px;
}
.msg-text :deep(pre code) { background: none; padding: 0; color: #d4d4d4; font-family: 'Consolas', 'Monaco', monospace; }
.msg-text :deep(blockquote) {
  border-left: 3px solid #409eff; margin: 8px 0; padding: 4px 12px;
  color: #606266; background: #ecf5ff33; border-radius: 0 6px 6px 0;
}
.msg-text :deep(table) {
  border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 13px;
  border: 1px solid #e4e7ed; border-radius: 8px; overflow: hidden;
}
.msg-text :deep(th), .msg-text :deep(td) { border: 1px solid #e4e7ed; padding: 7px 12px; text-align: left; }
.msg-text :deep(th) { background: #eef1f6; font-weight: 600; }
.msg-text :deep(a) { color: #409eff; text-decoration: none; word-break: break-all; }
.msg-text :deep(a:hover) { text-decoration: underline; }
.msg-text :deep(img) { max-width: 100%; border-radius: 8px; margin: 8px 0; }
.msg-text :deep(hr) { border: none; border-top: 1px solid #e4e7ed; margin: 12px 0; }

/* 计划块（穿插在流中的紧凑清单） */
.plan-inline {
  border-left: 3px solid #409eff;
  background: #f5f7fa;
  border-radius: 0 8px 8px 0;
  padding: 8px 12px;
  margin: 6px 0;
}
.plan-inline-title { font-size: 12px; color: #909399; margin-bottom: 2px; }
.plan-inline-item { padding: 2px 0; color: #606266; font-size: 13px; }
.plan-mark { font-family: monospace; margin-right: 6px; color: #909399; }
.plan-mark.st-completed { color: #67c23a; }
.plan-mark.st-in_progress { color: #409eff; }
.plan-done { color: #a0a4ab; text-decoration: line-through; }

/* 工具步骤组（完成后折叠摘要，防刷屏） */
.tool-group {
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  background: #fafbfc;
  padding: 4px 8px;
  margin: 6px 0;
}
.tool-group-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 6px;
  cursor: pointer;
  user-select: none;
  font-size: 12px;
  color: #606266;
}
.tool-group-head:hover { background: #eef1f5; border-radius: 6px; }
.tool-state-mark { color: #67c23a; width: 14px; text-align: center; }
.row-chevron { color: #c0c4cc; font-size: 11px; }

/* 流式打字光标：仅文本生成中出现 */
.typing-cursor-line { margin-top: -6px; padding-left: 4px; }
.typing-cursor {
  display: inline-block; width: 2px; height: 1.1em;
  background: #409eff; vertical-align: text-bottom;
  animation: cursorBlink 0.8s infinite;
}
@keyframes cursorBlink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }

/* 实时状态行：明确"正在做什么/仍在执行"，替代孤立光标 */
.live-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  padding: 4px 2px;
  color: #909399;
  font-size: 12px;
}
.mini-spin {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  background: #409eff; animation: pulse 1.2s infinite ease-in-out;
}
.stale-hint { color: #e6a23c; }

.msg-error {
  color: #f56c6c;
  background: #fef0f0;
  border-radius: 8px;
  padding: 8px 12px;
  margin-top: 6px;
  font-size: 13px;
}

/* 输入区 */
.ws-composer { border-top: 1px solid #e4e7ed; padding: 12px 20px; }
/* 权限模块：审批模式选择（手动审批 / 自动审批） */
.composer-perm {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.perm-label { font-size: 12px; color: #909399; flex-shrink: 0; }
.perm-hint { font-size: 12px; color: #c0c4cc; }
.composer-running {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  color: #409eff;
  font-size: 13px;
}
.composer-input { display: flex; gap: 10px; align-items: flex-end; }
.composer-input :deep(.el-textarea__inner) { resize: none; }

/* 模型选择 / 附件上传 */
.model-select { width: 160px; }
.attach-upload { display: flex; align-items: flex-end; }
.attach-btn { padding: 6px; margin-bottom: 1px; }
.attach-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.attach-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border: 1px solid #dcdfe6;
  border-radius: 12px;
  font-size: 12px;
  color: #606266;
  background: #f5f7fa;
}
.attach-chip .attach-name {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attach-remove { cursor: pointer; color: #c0c4cc; }
.attach-remove:hover { color: #f56c6c; }
/* 消息气泡内附件标签 */
.msg-files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}
.msg-file-tag {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 8px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.18);
  font-size: 12px;
}
</style>

<style>
/* 提问预览气泡（el-tooltip popper 默认挂到 body，scoped 样式够不到，需全局块） */
.q-nav-popper { max-width: 320px; word-break: break-word; }
</style>
