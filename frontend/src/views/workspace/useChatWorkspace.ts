/**
 * 会话管理 + 运行执行（对话域）：
 * - 会话列表 / 切换 / 创建 / 删除 / 消息加载（执行中自由切换，后台运行互不干扰 UX-10）；
 * - 发送与事件流消费（§7.1 v2 协议 + §8.4 重连）、停止（UX-07）、审批（UX-09）、项目返工；
 * - 历史消息经事件回放重建块流。
 */
import { nextTick, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import {
  conversationApi, type Conversation, type ConversationMetadata,
} from '@/api/conversations'
import { consumeRunEvents, runApi } from '@/api/runs'
import { documentError } from '@/api/documents'
import {
  addDocInput, applyRunEvent, executionPause, finalizeRunMessage,
  pendingDocInput,
} from '@/features/run/blocks'
import { documentTaskPayload } from '@/features/documents/taskModes'
import { messageAttachments } from './attachmentHistory'
import type { ChatMessage } from './chatDisplay'
import type { WorkspaceState } from './chatState'
import type { useThreadScroll } from './useThreadScroll'

export function useChatWorkspace(st: WorkspaceState, scroll: ReturnType<typeof useThreadScroll>) {
  let confirmingDelete = false

  // ── 会话列表 ───────────────────────────────────────────────────────────────

  async function loadConversations(refresh = false) {
    const revision = ++st.historyRevision.value
    const pageCount = st.requestedHistoryPages.value
    if (!refresh) st.historyLoading.value = true
    st.historyFailed.value = false
    try {
      const pages = await Promise.all(Array.from({ length: pageCount }, (_, index) => conversationApi.list({
        page: index + 1, page_size: st.historyPageSize,
      })))
      if (revision === st.historyRevision.value) {
        const previous = st.currentConversation.value
        const rows = pages.flatMap((page) => page.items || [])
        st.conversations.value = [...new Map(rows.map((row) => [row.id, row])).values()]
        st.historyTotal.value = pages[0].total
        st.historyLoadedPages.value = pageCount
        const refreshed = st.conversations.value.find((item) => item.id === st.currentConversation.value?.id)
        if (refreshed) st.currentConversation.value = refreshed
        else if (previous) st.conversations.value.push(previous)
      }
    } catch {
      if (revision === st.historyRevision.value && !st.conversations.value.length) st.historyFailed.value = true
    } finally {
      if (revision === st.historyRevision.value) st.historyLoading.value = false
    }
  }

  async function loadMoreConversations() {
    if (!st.hasMoreConversations.value || st.historyLoadingMore.value) return
    st.historyLoadingMore.value = true
    st.requestedHistoryPages.value = st.historyLoadedPages.value + 1
    try {
      await loadConversations(true)
    } finally {
      st.requestedHistoryPages.value = st.historyLoadedPages.value
      st.historyLoadingMore.value = false
    }
  }

  async function updateConversationMetadata(item: Conversation, changes: ConversationMetadata) {
    try {
      const updated = await conversationApi.update(item.id, changes)
      if (st.inactive.value) return
      st.conversations.value = st.conversations.value.map((row) => row.id === item.id ? updated : row)
      if (st.currentConversation.value?.id === item.id) st.currentConversation.value = updated
      void loadConversations(true)
    } catch (error) {
      throw new Error(documentError(error, changes.title !== undefined ? '重命名失败，请重试' : '更新置顶状态失败，请重试'))
    }
  }

  // ── 会话创建 / 切换（执行中自由切换；后台运行互不干扰）──────────────────────

  async function newConversation() {
    st.composer.value?.cancelDictation()
    st.resetDocumentTask()
    try {
      const c = await createConversation()
      if (st.currentConversation.value?.id === c.id) focusConversation()
    } catch { /* 请求层提示失败，用户可重试 */ }
  }

  /** 无会话时（上传附件 / 首条消息）自动创建会话 */
  function createConversation(transferDraftTeam = false): Promise<Conversation> {
    if (st.creating.value) return st.creating.value
    const origin = st.currentConversation.value?.id
    const inheritTeam = transferDraftTeam && origin === undefined && st.draftTeamMode.value
    st.creatingConversation.value = true
    st.creating.value = conversationApi.create({ assistant_id: st.currentAssistant.value?.id }).then((c) => {
      st.conversations.value.unshift(c)
      st.convMessages.set(c.id, [])
      if (st.currentConversation.value?.id === origin) {
        switchConversation(c)
        if (inheritTeam) st.rememberTeamMode(c.id, true)
      }
      void loadConversations(true)
      return c
    }).finally(() => {
      st.creating.value = undefined
      st.creatingConversation.value = false
    })
    return st.creating.value
  }

  function ensureConversation() {
    return st.currentConversation.value ?? createConversation(true)
  }

  function switchConversation(c: Conversation | null) {
    st.composer.value?.cancelDictation()
    st.clearAttachments()
    st.closePreview()
    st.noteWorkspaceChange()
    if (st.currentConversation.value && st.currentConversation.value.id !== c?.id) {
      st.resetDocumentTask()
    }
    st.draftTeamMode.value = false
    st.currentConversation.value = c
    st.syncConversationUrl(c?.id)
    scroll.resetFollow()
  }

  function focusConversation() {
    if (!st.historyMedia.matches) st.closeHistory(false)
    nextTick(() => st.composer.value?.focus())
  }

  function reloadMessages() {
    if (st.currentConversation.value) void selectConversation(st.currentConversation.value)
  }

  // ── 消息加载与历史回放 ─────────────────────────────────────────────────────

  function replayConversationRun(convId: number, runId: string, content = '') {
    const msg = st.pushMsg(convId, {
      role: 'assistant', content, blocks: [], runId, streaming: true, pendingActions: [],
      lastActivity: 'other', lastEventAt: Date.now(), liveStatus: '正在恢复执行进度…',
    })
    const controller = new AbortController()
    st.historyStreams.set(runId, controller)
    void consumeRunEvents(runId, {
      signal: controller.signal,
      afterSeq: 0,
      replayPastPauses: true,
      onEvent: (envelope) => {
        applyRunEvent(msg, envelope)
        if (envelope.event === 'tool.completed') st.noteWorkspaceChange()
        if (st.currentConversation.value?.id === convId) void scroll.scrollToBottom()
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
      if (st.historyStreams.get(runId) === controller) st.historyStreams.delete(runId)
    })
  }

  async function selectConversation(c: Conversation) {
    if (st.currentConversation.value?.id !== c.id) switchConversation(c)
    if (!st.historyMedia.matches) st.closeHistory()
    if (st.loadingMessagesFor.has(c.id)) return
    if (st.convMessages.has(c.id) && !st.failedMessagesFor.has(c.id)) {
      if (!await scroll.focusLinkedRun()) await scroll.forceScrollToBottom()
      return
    }
    st.loadingMessagesFor.add(c.id)
    st.failedMessagesFor.delete(c.id)
    try {
      const res = await conversationApi.messages(c.id)
      if (st.inactive.value || !st.conversations.value.some((item) => item.id === c.id)) return
      const arr = st.convArr(c.id)
      const raw: any[] = res.items || []
      const assistantRuns = new Set(raw.filter((item) => item.role === 'assistant').map((item) => item.source_run_id))
      for (const m of raw) {
        if (m.role === 'user') {
          const { text, files } = messageAttachments(m.content || '', m.attachments)
          const msg = st.pushMsg(c.id, { role: 'user', content: text, blocks: [], files })
          // 图片附件懒加载预览（不阻塞历史渲染；经响应式代理赋值触发视图刷新）
          files.forEach((f, fi) => {
            if (st.isImageName(f.name)) {
              st.loadFilePreview(c.id, f.name).then((url) => {
                if (url && msg.files && st.convMessages.has(c.id)) msg.files[fi].previewUrl = url
                else st.releaseAttachmentUrl(url)
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
      st.convMessages.delete(c.id)
      st.failedMessagesFor.add(c.id)
    } finally {
      st.loadingMessagesFor.delete(c.id)
      if (st.currentConversation.value?.id === c.id && !st.failedMessagesFor.has(c.id) && !await scroll.focusLinkedRun()) await scroll.forceScrollToBottom()
    }
  }

  async function openLinkedConversation() {
    const id = st.numericQuery(st.route.query.conversation)
    if (!id || st.historyLoading.value || st.inactive.value) return
    if (st.currentConversation.value?.id === id) {
      if (st.route.query.run) void scroll.focusLinkedRun()
      return
    }
    const known = st.conversations.value.find((conversation) => conversation.id === id)
    if (known) {
      await selectConversation(known)
      return
    }
    try {
      const item = await conversationApi.get(id)
      if (st.inactive.value || st.numericQuery(st.route.query.conversation) !== id) return
      if (!st.conversations.value.some((conversation) => conversation.id === id)) st.conversations.value.push(item)
      await selectConversation(item)
    } catch {
      if (!st.inactive.value && st.numericQuery(st.route.query.conversation) === id) ElMessage.error('无法打开关联会话')
    }
  }

  watch(() => [st.route.query.conversation, st.route.query.run], openLinkedConversation)

  async function removeConversation(c: Conversation) {
    if (confirmingDelete || st.deletingId.value !== null) return
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
    st.deletingId.value = c.id
    try {
      const entry = st.activeRuns.get(c.id)
      if (entry && !entry.msg.runId) {
        ElMessage.info('任务正在创建，请稍后再删除对话')
        return
      }
      const runId = entry?.msg.runId || st.convMessages.get(c.id)?.find((message) => message.streaming || pendingDocInput(message))?.runId
        || c.active_run_id
      if (runId) await runApi.cancel(runId, '删除会话前取消任务')
      await conversationApi.delete(c.id)
      st.rememberTeamMode(c.id, false)
      if (entry) {
        entry.stopped = true
        entry.abort.abort()
        st.clearActiveRun(c.id, entry)
      }
      st.conversations.value = st.conversations.value.filter((x) => x.id !== c.id)
      for (const message of st.convMessages.get(c.id) || []) {
        for (const file of message.files || []) st.releaseAttachmentUrl(file.previewUrl)
        if (message.runId) st.historyStreams.get(message.runId)?.abort()
      }
      st.convMessages.delete(c.id)
      if (st.currentConversation.value?.id === c.id) switchConversation(null)
      ElMessage.success('对话已删除')
      void loadConversations(true)
    } catch { /* 请求层提示失败，原对话仍可查看 */ }
    finally {
      st.deletingId.value = null
    }
  }

  // ── 发送与事件流消费（§7.1 v2 协议 + §8.4 重连）──────────────────────────────

  async function sendMessage(continueTask = false) {
    const latest = st.messages.value[st.messages.value.length - 1]
    if (continueTask && (!latest?.runId || latest.streaming || !executionPause(latest)?.canContinue)) return
    const text = continueTask ? '继续' : st.inputText.value.trim()
    if (!text || st.blockedReason.value || st.sending.value || st.uploading.value
      || st.loadingMessages.value || st.messagesFailed.value) return

    st.sending.value = true
    let conv: Conversation
    try {
      conv = await ensureConversation()
    } catch {
      return
    } finally {
      st.sending.value = false
    }
    const convId = conv.id
    if (st.inactive.value || st.currentConversation.value?.id !== convId) return
    const sentFiles = continueTask ? [] : st.attachments.value.map((a) => ({ ...a }))
    const sentModelId = st.selectedModelId.value
    const sentMode = st.approvalMode.value
    const sentWebEnabled = st.webSearchOn.value
    const sentTeamMode = !continueTask && st.teamMode.value
    const sentTask = continueTask ? { mode: 'chat' as const } : { ...st.documentTask.value }
    // 右侧正在预览的文档视为本轮编辑目标（所见即所改）；手动选文档或带附件时不覆盖
    const sentPreview = !continueTask && !sentFiles.length && sentTask.mode === 'auto'
      ? st.documentPreview.value : null

    if (!continueTask) {
      st.inputText.value = ''
      st.attachments.value = []
      st.resetDocumentTask()
    }
    st.pushMsg(convId, {
      role: 'user', content: text, blocks: [], task: sentTask,
      files: sentFiles.map(({ name, path, previewUrl }) => ({ name, path, previewUrl })),
    })
    const aMsg = st.pushMsg(convId, {
      role: 'assistant', content: '', blocks: [], streaming: true,
      lastActivity: 'other', lastEventAt: Date.now(),
      liveStatus: '正在创建任务…', runStatus: 'pending', pendingActions: [],
    })
    const entry = { abort: new AbortController(), stopped: false, msg: aMsg }
    st.setActiveRun(convId, entry)
    void scroll.forceScrollToBottom()

    try {
      const created = await runApi.create({
        message: text,
        conversation_id: convId,
        assistant_id: st.currentAssistant.value?.id,
        approval_mode: sentMode,
        web_enabled: sentWebEnabled,
        model_config_id: sentModelId || undefined,
        ...(sentTeamMode ? { team_mode: true } : {}),
        attachments: sentFiles,
        ...documentTaskPayload(sentTask),
        ...(sentPreview ? {
          document_id: sentPreview.document_id,
          task_options: { document_version_no: sentPreview.version_no },
        } : {}),
      })
      if (entry.stopped) {
        if (!st.inactive.value) runApi.cancel(created.run_id, '用户主动停止').catch(() => {
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
      st.clearActiveRun(convId, entry)
      if (!continueTask && st.currentConversation.value?.id === convId && !st.inputText.value) {
        st.inputText.value = text
        st.documentTask.value = sentTask
      }
    }
  }

  function subscribeActiveRun(convId: number, entry: { abort: AbortController; stopped: boolean; msg: ChatMessage }, runId: string) {
    consumeRunEvents(runId, {
      signal: entry.abort.signal,
      afterSeq: 0,
      onEvent: (envelope) => {
        if (entry.stopped) return
        applyRunEvent(entry.msg, envelope)
        if (envelope.event === 'tool.completed') st.noteWorkspaceChange()
        if (st.currentConversation.value?.id === convId) void scroll.scrollToBottom()
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

  function finishRun(convId: number, entry: { abort: AbortController; stopped: boolean; msg: ChatMessage }, reason: string) {
    if (st.activeRuns.get(convId) !== entry) return
    finalizeRunMessage(entry.msg, reason, entry.msg.content)
    st.clearActiveRun(convId, entry)
    st.noteWorkspaceChange()
    if (entry.msg.runStatus === 'failed') void refreshDocumentRetryability(entry.msg)
    void loadConversations(true) // 标题已规范化生成，刷新会话列表
    if (st.currentConversation.value?.id === convId) void scroll.scrollToBottom()
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
    const convId = st.currentConversation.value?.id
    if (!convId) return
    const entry = st.activeRuns.get(convId)
    if (!entry) {
      const message = st.historyRun.value
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
    st.clearActiveRun(convId, entry)
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
    if (!runId || !message.pendingActions?.length || st.pendingDecisions.has(runId)) return
    st.pendingDecisions.set(runId, type)
    try {
      await runApi.resume(runId, message.pendingActions.map((action) => ({ type, action_id: action.action_id || null })))
      if (!st.inactive.value && st.convMessages.get(convId)?.includes(message)) {
        observeResumedRun(convId, message, type === 'approve' ? '已批准，恢复执行…' : '已拒绝，恢复执行…')
      }
    } catch { /* 请求层显示错误，保留待审批操作以便重试 */ }
    finally {
      st.pendingDecisions.delete(runId)
    }
  }

  /** 项目草稿返工（team_mode 失败运行的 repair） */
  async function repairProjectDraft(message: ChatMessage) {
    const sourceId = message.runId
    const convId = st.currentConversation.value?.id
    if (!sourceId || !convId || message.runStatus !== 'failed' || st.blockedReason.value
      || st.sending.value || st.loadingMessages.value || st.messagesFailed.value) return
    const modelId = st.selectedModelId.value || undefined
    const keyId = `${sourceId}:${modelId || 'default'}`
    const key = st.projectRepairKeys.get(keyId) || globalThis.crypto?.randomUUID?.()
    if (key) st.projectRepairKeys.set(keyId, key)
    st.sending.value = true
    st.repairingProject.value = sourceId
    st.projectRepairErrors.delete(sourceId)
    try {
      const created = await runApi.repairProject(sourceId, modelId, key)
      st.projectRepairKeys.delete(keyId)
      if (st.inactive.value || created.conversation_id !== convId) return
      st.pushMsg(convId, { role: 'user', content: created.user_message, blocks: [] })
      const assistant = st.pushMsg(convId, {
        role: 'assistant', content: '', blocks: [], runId: created.run_id, streaming: true,
        lastActivity: 'other', lastEventAt: Date.now(), runStatus: 'pending', pendingActions: [],
        liveStatus: '正在载入项目草稿…',
      })
      const entry = { abort: new AbortController(), stopped: false, msg: assistant }
      st.setActiveRun(convId, entry)
      if (st.currentConversation.value?.id === convId) void scroll.forceScrollToBottom()
      subscribeActiveRun(convId, entry, created.run_id)
    } catch (error) {
      st.projectRepairErrors.set(sourceId, documentError(error, '暂时无法继续修复草稿，请重试'))
    } finally {
      st.sending.value = false
      st.repairingProject.value = ''
    }
  }

  /** 审批 / 文档输入提交后重新订阅事件流（从断点继续） */
  function observeResumedRun(convId: number, aMsg: ChatMessage, liveStatus: string) {
    if (!aMsg.runId) return
    const previous = st.activeRuns.get(convId)
    if (previous) {
      previous.stopped = true
      previous.abort.abort()
    }
    st.historyStreams.get(aMsg.runId)?.abort()
    aMsg.pendingActions = []
    aMsg.streaming = true
    aMsg.runStatus = 'running'
    aMsg.lastActivity = 'other'
    aMsg.lastEventAt = Date.now()
    aMsg.liveStatus = liveStatus

    const entry = { abort: new AbortController(), stopped: false, msg: aMsg }
    st.setActiveRun(convId, entry)

    consumeRunEvents(aMsg.runId, {
      signal: entry.abort.signal,
      afterSeq: aMsg.lastSeq || 0,
      replayPastPauses: true,
      onEvent: (envelope) => {
        if (entry.stopped) return
        applyRunEvent(aMsg, envelope)
        if (envelope.event === 'tool.completed') st.noteWorkspaceChange()
        if (st.currentConversation.value?.id === convId) void scroll.scrollToBottom()
      },
      onDone: (reason) => {
        if (entry.stopped) return
        finishRun(convId, entry, reason)
      },
    }).catch(() => { /* 网络异常已由 onDone 兜底 */ })
  }

  async function refreshDocumentRetryability(message: ChatMessage) {
    if (!message.runId || !message.blocks.some((block) => block.kind === 'document_stage')) return
    try {
      const run = await runApi.get(message.runId)
      if (message.runStatus === 'failed') message.documentRetryable = run.document_retryable === true
    } catch { /* 保留事件中已知的恢复状态 */ }
  }

  return {
    loadConversations, loadMoreConversations, updateConversationMetadata,
    newConversation, createConversation, ensureConversation,
    switchConversation, focusConversation, reloadMessages, selectConversation,
    openLinkedConversation, removeConversation,
    sendMessage, subscribeActiveRun, finishRun, openTrace, stopGeneration,
    decideApproval, repairProjectDraft, observeResumedRun, refreshDocumentRetryability,
    replayConversationRun,
  }
}
