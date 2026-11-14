<template>
  <div class="workspace-home">
    <!-- 左侧：最近对话 + 我的智能助手 -->
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
          :class="{ active: currentConversation?.id === c.id }"
          @click="selectConversation(c)"
        >
          <span class="conv-title">{{ c.title || '新对话' }}</span>
          <el-icon class="conv-delete" @click.stop="removeConversation(c)"><Delete /></el-icon>
        </div>
        <div v-if="!conversations.length" class="sidebar-empty">暂无对话</div>
      </div>

      <div class="sidebar-section">
        <div class="sidebar-title">我的智能助手</div>
        <div
          v-for="a in assistants"
          :key="a.id"
          class="assistant-item"
          :class="{ active: currentAssistant?.id === a.id }"
          @click="selectAssistant(a)"
        >
          <span>{{ a.icon }} {{ a.name }}</span>
          <el-tag v-if="a.is_default" size="small" type="info">默认</el-tag>
        </div>
        <el-button text type="primary" size="small" @click="showBuilder = true">
          + 创建助手
        </el-button>
      </div>
    </aside>

    <!-- 右侧：对话区 -->
    <main class="ws-main">
      <div class="ws-header">
        <span class="ws-assistant-name">{{ currentAssistant?.name || '通用助手' }}</span>
        <span v-if="currentRunId" class="ws-run-id">run: {{ currentRunId.slice(0, 8) }}</span>
      </div>

      <div ref="messagesRef" class="ws-messages">
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
          <!-- 用户消息 -->
          <div v-if="m.role === 'user'" class="msg-row msg-user">
            <div class="msg-bubble">{{ m.content }}</div>
          </div>

          <!-- 助手消息 -->
          <div v-else class="msg-row msg-assistant">
            <div class="msg-content">
              <RunTimeline
                v-if="m.runId && m.timelineEvents?.length"
                :events="m.timelineEvents"
                :status="m.runStatus || 'running'"
              />
              <div v-if="m.content" class="msg-text" v-html="renderMarkdown(m.content)"></div>
              <div v-else-if="m.streaming" class="msg-text msg-thinking">{{ m.statusText || '思考中…' }}</div>
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

      <!-- 输入区 -->
      <div class="ws-composer">
        <div v-if="streaming" class="composer-running">
          <span>{{ streamingStatus }}</span>
          <el-button size="small" type="danger" plain @click="stopGeneration">
            <el-icon><VideoPause /></el-icon> 停止
          </el-button>
        </div>
        <div class="composer-input">
          <el-input
            v-model="inputText"
            type="textarea"
            :rows="2"
            :disabled="streaming"
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

    <!-- 创建助手（自然语言 Builder，UX-02/03） -->
    <el-dialog v-model="showBuilder" title="创建智能助手" width="560px">
      <div class="builder-desc">
        描述你想要的助手，系统会自动生成配置草稿（名称、能力、指令），确认后立即可用。
      </div>
      <el-input
        v-model="builderText"
        type="textarea"
        :rows="4"
        placeholder="例如：帮我创建一个行业研究助手，能够搜索最新资料、分析数据、核实信息来源，并生成结构化研究报告。"
      />
      <div v-if="draft" class="builder-draft">
        <el-divider content-position="left">草稿预览</el-divider>
        <p><b>名称：</b>{{ draft.name }}</p>
        <p><b>能力包：</b>{{ (draft.capability_ids || []).join('、') || 'general' }}</p>
        <p><b>角色指令：</b></p>
        <pre class="draft-prompt">{{ draft.system_prompt }}</pre>
        <div v-if="draft.risk_notes?.length" class="draft-risk">
          <div v-for="(note, i) in draft.risk_notes" :key="i">⚠ {{ note }}</div>
        </div>
      </div>
      <template #footer>
        <el-button @click="showBuilder = false">取消</el-button>
        <el-button :loading="drafting" @click="generateDraft">生成草稿</el-button>
        <el-button type="primary" :disabled="!draft" :loading="creating" @click="createFromDraft">
          确认创建并使用
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
/**
 * 统一智能体工作台（任务书 §2.2）：
 * - 登录后直接对话（默认助手，UX-01）；
 * - 自然语言创建助手（UX-02/03）；
 * - Run Timeline 展示任务进度（UX-05）；
 * - 取消走后端 /runs/{id}/cancel（UX-07）；
 * - 断线重连 + Last-Event-ID 续传（UX-08）；
 * - 审批按 action_id 决策（UX-09）；
 * - 会话切换不串状态（UX-10）。
 */
import { nextTick, onMounted, ref } from 'vue'
import { Delete, Plus, VideoPause } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  assistantApi, conversationApi, consumeRunEvents, runApi, workspaceApi,
  type Assistant, type Conversation, type RunEventEnvelope,
} from '@/api/v2'
import RunTimeline from '@/features/run/RunTimeline.vue'
import ApprovalPanel from '@/features/run/ApprovalPanel.vue'

interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
  statusText?: string
  error?: string
  runId?: string
  timelineEvents?: RunEventEnvelope[]
  runStatus?: string
  pendingActions?: any[]
}

const messages = ref<ChatMessage[]>([])
const messagesRef = ref<HTMLElement>()
const inputText = ref('')
const streaming = ref(false)
const streamingStatus = ref('')

const conversations = ref<Conversation[]>([])
const currentConversation = ref<Conversation | null>(null)
const assistants = ref<Assistant[]>([])
const currentAssistant = ref<Assistant | null>(null)

// Run 生命周期（§8.4）：保存 X-Run-Id / 最后事件序号，断线后恢复
const currentRunId = ref<string | null>(null)
let abortController: AbortController | null = null
let activeRunToken = 0 // 会话切换防串扰

const showBuilder = ref(false)
const builderText = ref('')
const draft = ref<any>(null)
const drafting = ref(false)
const creating = ref(false)

function renderMarkdown(text: string): string {
  // 轻量渲染：转义 + 换行（避免 v-html 注入，先转义）
  const escaped = text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return escaped.replace(/\n/g, '<br/>')
}

async function scrollToBottom() {
  await nextTick()
  messagesRef.value?.scrollTo({ top: messagesRef.value.scrollHeight })
}

// ── 初始化 ────────────────────────────────────────────────────────────────────

onMounted(async () => {
  const boot = await workspaceApi.bootstrap()
  currentAssistant.value = boot.default_assistant
  assistants.value = [boot.default_assistant]
  try {
    const res = await assistantApi.list()
    assistants.value = res.items || []
    const def = assistants.value.find((a) => a.is_default)
    if (def) currentAssistant.value = def
  } catch { /* bootstrap 的默认助手兜底 */ }
  await loadConversations()
})

async function loadConversations() {
  const res = await conversationApi.list()
  conversations.value = res.items || []
}

// ── 会话管理（UX-10：切换不串状态）────────────────────────────────────────────

async function newConversation() {
  if (streaming.value) {
    ElMessage.warning('请等待当前回答完成后再新建对话')
    return
  }
  const c = await conversationApi.create({ assistant_id: currentAssistant.value?.id })
  conversations.value.unshift(c)
  currentConversation.value = c
  messages.value = []
  currentRunId.value = null
}

async function selectConversation(c: Conversation) {
  if (streaming.value) {
    ElMessage.warning('请等待当前回答完成后再切换会话')
    return
  }
  currentConversation.value = c
  currentRunId.value = null
  try {
    const res = await conversationApi.messages(c.id)
    messages.value = (res.items || []).map((m: any) => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content || '',
    }))
    await scrollToBottom()
  } catch { /* ignore */ }
}

async function removeConversation(c: Conversation) {
  await ElMessageBox.confirm('确定删除此对话？', '确认', { type: 'warning' })
  await conversationApi.delete(c.id)
  conversations.value = conversations.value.filter((x) => x.id !== c.id)
  if (currentConversation.value?.id === c.id) {
    currentConversation.value = null
    messages.value = []
  }
}

function selectAssistant(a: Assistant) {
  if (streaming.value) {
    ElMessage.warning('请等待当前回答完成后再切换助手')
    return
  }
  currentAssistant.value = a
}

// ── 发送与事件流消费（§7.1 v2 协议 + §8.4 重连）──────────────────────────────

function sendExample(text: string) {
  inputText.value = text
  sendMessage()
}

async function sendMessage() {
  const text = inputText.value.trim()
  if (!text || streaming.value) return

  if (!currentConversation.value) {
    const c = await conversationApi.create({ assistant_id: currentAssistant.value?.id })
    conversations.value.unshift(c)
    currentConversation.value = c
  }

  inputText.value = ''
  messages.value.push({ role: 'user', content: text })

  const token = ++activeRunToken
  const conversationIdForThisRun = currentConversation.value.id

  const assistantMsg: ChatMessage = {
    role: 'assistant', content: '', streaming: true, statusText: '正在创建任务…',
    timelineEvents: [], runStatus: 'pending', pendingActions: [],
  }
  messages.value.push(assistantMsg)
  const msgIdx = messages.value.length - 1
  streaming.value = true
  streamingStatus.value = '正在创建任务…'
  await scrollToBottom()

  abortController = new AbortController()

  try {
    const created = await runApi.create({
      message: text,
      conversation_id: conversationIdForThisRun,
      assistant_id: currentAssistant.value?.id,
    })
    if (token !== activeRunToken) return // 会话已切换，丢弃
    currentRunId.value = created.run_id
    messages.value[msgIdx].runId = created.run_id

    await consumeRunEvents(created.run_id, {
      signal: abortController.signal,
      afterSeq: 0,
      onEvent: (envelope) => {
        if (token !== activeRunToken) return
        applyEvent(msgIdx, envelope)
      },
      onReconnecting: (attempt) => {
        if (token !== activeRunToken) return
        streamingStatus.value = `连接中断，正在重连（第 ${attempt} 次）…`
      },
      onDone: (reason) => {
        if (token !== activeRunToken) return
        finalizeMessage(msgIdx, reason)
      },
    })
    await loadConversations()
  } catch (e: any) {
    if (token !== activeRunToken) return
    messages.value[msgIdx] = {
      ...messages.value[msgIdx],
      streaming: false,
      error: `执行出错: ${e.message}`,
    }
    ElMessage.error('请求失败: ' + e.message)
  } finally {
    if (token === activeRunToken) {
      streaming.value = false
      abortController = null
      await scrollToBottom()
    }
  }
}

function applyEvent(msgIdx: number, envelope: RunEventEnvelope) {
  const msg = messages.value[msgIdx]
  if (!msg) return
  msg.timelineEvents = [...(msg.timelineEvents || []), envelope]

  switch (envelope.event) {
    case 'run.started':
      msg.statusText = '任务已开始'
      streamingStatus.value = '任务执行中…'
      break
    case 'plan.updated':
      streamingStatus.value = '正在规划任务…'
      break
    case 'tool.started':
      streamingStatus.value = `调用工具: ${envelope.data?.tool_name || ''}`
      break
    case 'tool.completed':
      streamingStatus.value = '获取工具结果…'
      break
    case 'assistant.delta':
      // v2 主协议：模型增量直接进最终回答区（不展示原始推理）
      msg.content = (msg.content || '') + (envelope.data?.text || '')
      msg.statusText = '正在生成回答…'
      streamingStatus.value = '正在生成回答…'
      break
    case 'approval.required':
      msg.pendingActions = envelope.data?.actions || []
      msg.statusText = '等待人工审批'
      streamingStatus.value = '等待人工审批…'
      break
    case 'run.status_changed':
      msg.runStatus = envelope.data?.status || msg.runStatus
      break
    case 'run.failed':
      msg.error = envelope.data?.error || '运行失败'
      break
  }
  scrollToBottom()
}

function finalizeMessage(msgIdx: number, reason: string) {
  const msg = messages.value[msgIdx]
  if (!msg) return
  msg.streaming = false
  msg.runStatus = reason.startsWith('reconnect_failed') ? (msg.runStatus || 'failed') : reason
  if (reason === 'run.completed' && !msg.content) {
    msg.content = '（任务已完成）'
  }
  if (reason.startsWith('reconnect_failed')) {
    msg.error = `连接中断且重连失败：${reason.replace('reconnect_failed: ', '')}。可刷新页面恢复任务进度。`
  }
  streaming.value = false
  scrollToBottom()
}

/** UX-07：停止 = 后端真实取消（不停止订阅而已）+ 断开 SSE */
async function stopGeneration() {
  const runId = currentRunId.value
  if (abortController) {
    abortController.abort()
    abortController = null
  }
  if (runId) {
    try {
      const res = await runApi.cancel(runId, '用户主动停止')
      ElMessage.info(res.message || '取消请求已发送')
    } catch {
      ElMessage.warning('取消请求失败，任务可能继续执行')
    }
  }
  streaming.value = false
}

function onApprovalDecided(type: 'approve' | 'reject') {
  // 审批后恢复执行：继续消费事件流（run 重新进入 running）
  const msgIdx = messages.value.findIndex((m) => m.pendingActions?.length)
  if (msgIdx < 0) return
  const msg = messages.value[msgIdx]
  msg.pendingActions = []
  msg.streaming = true
  streaming.value = true
  streamingStatus.value = type === 'approve' ? '已批准，恢复执行…' : '已拒绝，恢复执行…'
  abortController = new AbortController()
  const token = activeRunToken

  consumeRunEvents(msg.runId!, {
    signal: abortController.signal,
    afterSeq: msg.timelineEvents?.length
      ? msg.timelineEvents[msg.timelineEvents.length - 1].sequence
      : 0,
    onEvent: (envelope) => {
      if (token !== activeRunToken) return
      applyEvent(msgIdx, envelope)
    },
    onDone: (reason) => {
      if (token !== activeRunToken) return
      finalizeMessage(msgIdx, reason)
    },
  }).finally(() => {
    if (token === activeRunToken) {
      streaming.value = false
    }
  })
}

// ── 自然语言 Builder（UX-02/03）──────────────────────────────────────────────

async function generateDraft() {
  if (!builderText.value.trim()) {
    ElMessage.warning('请先描述你想要的助手')
    return
  }
  drafting.value = true
  try {
    draft.value = await assistantApi.draft(builderText.value.trim())
  } finally {
    drafting.value = false
  }
}

async function createFromDraft() {
  if (!draft.value) return
  creating.value = true
  try {
    const created = await assistantApi.create({
      name: draft.value.name,
      description: draft.value.description,
      system_prompt: draft.value.system_prompt,
      capability_ids: draft.value.capability_ids,
      tools: draft.value.tools || [],
    })
    assistants.value.unshift(created)
    currentAssistant.value = created
    showBuilder.value = false
    draft.value = null
    builderText.value = ''
    ElMessage.success('助手已创建并可立即使用')
  } finally {
    creating.value = false
  }
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
.sidebar-empty { color: #c0c4cc; font-size: 12px; }
.assistant-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: #606266;
}
.assistant-item:hover { background: #f5f7fa; }
.assistant-item.active { background: #ecf5ff; color: #409eff; }

/* 主区 */
.ws-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.ws-header {
  padding: 10px 20px;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  gap: 12px;
}
.ws-assistant-name { font-weight: 600; }
.ws-run-id { color: #c0c4cc; font-size: 12px; font-family: monospace; }

.ws-messages { flex: 1; overflow-y: auto; padding: 20px; }
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
}
.msg-assistant { justify-content: flex-start; }
.msg-content { max-width: 80%; width: 100%; }
.msg-text {
  background: #f5f7fa;
  border-radius: 12px;
  padding: 10px 14px;
  white-space: pre-wrap;
  line-height: 1.6;
}
.msg-thinking { color: #909399; font-style: italic; }
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
.composer-running {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  color: #409eff;
  font-size: 13px;
}
.composer-input { display: flex; gap: 10px; align-items: flex-end; }
.composer-input :deep(.el-textarea__inner) { resize: none; }

/* Builder */
.builder-desc { color: #909399; font-size: 13px; margin-bottom: 12px; }
.builder-draft pre.draft-prompt {
  background: #f5f7fa;
  padding: 10px;
  border-radius: 6px;
  white-space: pre-wrap;
  font-size: 12px;
  max-height: 160px;
  overflow-y: auto;
}
.draft-risk { color: #e6a23c; font-size: 12px; margin-top: 8px; }
</style>
