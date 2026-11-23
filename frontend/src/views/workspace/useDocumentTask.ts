/**
 * 文档任务域：任务选择（链接同步/继续编辑）、业务输入提交、修改稿审阅、断点重试、文档预览下载。
 */
import { watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  documentError, documentProfilesApi, documentsApi, downloadDocument, type DocumentArtifact,
} from '@/api/documents'
import type { InputRequest, ProfileDefinition } from '@/api/documents'
import { runApi } from '@/api/runs'
import {
  acceptDocInput, documentReviewInput, pendingDocInput, recordDocumentReview,
  type DocumentEditReviewBlock,
} from '@/features/run/blocks'
import { isProfileConfirmation } from '@/features/documents/inputRequest'
import type { ChatMessage } from './chatDisplay'
import type { WorkspaceState } from './chatState'
import type { useChatWorkspace } from './useChatWorkspace'

export function useDocumentTask(
  st: WorkspaceState,
  chat: ReturnType<typeof useChatWorkspace>,
) {
  /** 从 URL 参数恢复文档/规范任务上下文 */
  async function syncLinkedDocument() {
    const id = st.numericQuery(st.route.query.document)
    const profile = st.numericQuery(st.route.query.profile)
    if (!id && !profile) return
    const selection = id
      ? { mode: 'document_edit' as const, documentId: id, version: st.numericQuery(st.route.query.version) }
      : { mode: 'document_generate' as const, profileId: profile }
    st.documentTask.value = selection
    const inserted = st.documentTask.value
    try {
      const title = id ? (await documentsApi.get(id)).title : profile ? (await documentProfilesApi.get(profile)).name : ''
      if (st.documentTask.value === inserted) st.documentTask.value = { ...selection, title }
    } catch { /* 仍展示链接中的文档或规范标识，任务创建会校验可见性。 */ }
  }

  watch(() => [st.route.query.document, st.route.query.version, st.route.query.profile], syncLinkedDocument, { immediate: true })

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
    const convId = st.currentConversation.value?.id
    if (!runId || !convId || st.documentInputsBusy.has(runId) || pendingDocInput(message)?.request.request_id !== requestId) return
    st.documentInputsBusy.add(runId)
    st.documentInputErrors.delete(runId)
    try {
      await runApi.submitInput(runId, requestId, values)
      if (st.inactive.value || !st.convMessages.get(convId)?.includes(message)) return
      const accepted = values.accepted_change_ids
      if (Array.isArray(accepted)) recordDocumentReview(message, requestId, accepted)
      acceptDocInput(message, requestId)
      chat.observeResumedRun(convId, message, Array.isArray(accepted) ? '审阅已提交，正在继续执行…' : '资料已提交，正在继续执行…')
    } catch (error) {
      st.documentInputErrors.set(runId, documentError(error, '提交失败，请重试'))
    } finally {
      st.documentInputsBusy.delete(runId)
    }
  }

  async function cancelDocumentInput(message: ChatMessage) {
    const runId = message.runId
    const convId = st.currentConversation.value?.id
    if (!runId || !convId || st.documentInputsBusy.has(runId) || !pendingDocInput(message)) return
    st.documentInputsBusy.add(runId)
    st.documentInputErrors.delete(runId)
    try {
      await runApi.cancel(runId, '用户取消文档任务')
      if (!st.inactive.value && st.convMessages.get(convId)?.includes(message)) {
        const input = pendingDocInput(message)
        if (input) input.state = 'closed'
        chat.observeResumedRun(convId, message, '正在取消文档任务…')
      }
    } catch (error) {
      st.documentInputErrors.set(runId, documentError(error, '取消失败，请重试'))
    } finally {
      st.documentInputsBusy.delete(runId)
    }
  }

  async function retryFailedDocument(message: ChatMessage) {
    const runId = message.runId
    const convId = st.currentConversation.value?.id
    if (!runId || !convId || st.documentInputsBusy.has(runId)) return
    if (st.activeRuns.has(convId)) {
      ElMessage.warning('此会话正在执行任务，请等它完成后再继续')
      return
    }
    st.documentInputsBusy.add(runId)
    try {
      const resumed = await runApi.retryDocument(runId, message.lastSeq)
      if (st.inactive.value || !st.convMessages.get(convId)?.includes(message)) return
      message.lastSeq = resumed.after_seq
      message.error = undefined
      message.documentRetryable = false
      chat.observeResumedRun(convId, message, '正在从文档断点继续…')
    } catch (error) {
      ElMessage.error(documentError(error, '暂时无法继续，请重试'))
    } finally {
      st.documentInputsBusy.delete(runId)
    }
  }

  function openDocumentPreview(artifact: DocumentArtifact) {
    st.closePreview()
    st.documentPreview.value = artifact
  }

  async function downloadArtifact(artifact: DocumentArtifact) {
    const id = artifact.document_version_id
    if (st.downloadingDocuments.has(id)) return
    st.downloadingDocuments.add(id)
    try {
      await downloadDocument(artifact.document_id, artifact.version_no, artifact.filename)
    } catch (error) {
      ElMessage.error(documentError(error, 'Word 文档下载失败'))
    } finally {
      st.downloadingDocuments.delete(id)
    }
  }

  function continueDocument(artifact: DocumentArtifact) {
    if (st.blockedReason.value) {
      ElMessage.info(st.blockedReason.value)
      return
    }
    st.documentTask.value = {
      mode: 'document_edit', documentId: artifact.document_id, version: artifact.version_no, title: artifact.title,
    }
    st.closePreview()
    chat.focusConversation()
  }

  return {
    syncLinkedDocument,
    isInlineProfileConfirmation, profileConfirmation, confirmProfile,
    submitDocumentReview, submitDocumentInput, cancelDocumentInput,
    retryFailedDocument,
    openDocumentPreview, downloadArtifact, continueDocument,
  }
}
