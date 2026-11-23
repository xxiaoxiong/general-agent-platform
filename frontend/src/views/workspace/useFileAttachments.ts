/**
 * 会话附件与右侧临时文件预览：
 * - 附件上传即写入会话工作区（uploads/），随消息告知模型读取；
 * - 点文件名 → 右侧预览（对齐 Trae），可关闭 / 下载。
 */
import { ElMessage } from 'element-plus'
import { conversationApi, type PreviewContent } from '@/api/conversations'
import { filePreviewError } from '@/features/preview/previewError'
import type { WorkspaceState } from './chatState'
import type { useChatWorkspace } from './useChatWorkspace'

export function useFileAttachments(
  st: WorkspaceState,
  chat: ReturnType<typeof useChatWorkspace>,
) {
  async function uploadAttachments(files: File[]) {
    st.uploading.value += files.length
    try {
      const conv = await chat.ensureConversation()
      const results = await Promise.allSettled(files.map(async (file) => {
        const res = await conversationApi.uploadFile(conv.id, file)
        if (st.currentConversation.value?.id !== conv.id || st.inactive.value) return
        st.attachments.value.push({ ...res, previewUrl: st.isImageName(res.name) ? st.attachmentUrl(file) : undefined })
      }))
      const completeCount = results.filter((result) => result.status === 'fulfilled').length
      if (completeCount && st.currentConversation.value?.id === conv.id) ElMessage.success(`已上传 ${completeCount} 个附件`)
    } catch { /* 错误提示已由请求层统一弹出 */ }
    finally {
      st.uploading.value -= files.length
    }
  }

  async function removeAttachment(i: number) {
    const a = st.attachments.value[i]
    st.attachments.value.splice(i, 1)
    st.releaseAttachmentUrl(a?.previewUrl)
    if (st.currentConversation.value && a) {
      conversationApi.deleteFile(st.currentConversation.value.id, a.name).catch(() => {})
    }
  }

  /** 打开右侧预览（path 为工作区相对路径；name 为展示名） */
  async function openPreview(path: string, name?: string) {
    const conv = st.currentConversation.value
    if (!conv || !path) return
    st.documentPreview.value = null
    // 工具历史会指向抽取副本。用户点击 Word 时直接查看原文件版式。
    const originalWord = /\.docx\.extracted\.txt$/i.test(path)
    let sourcePath = originalWord ? path.slice(0, -'.extracted.txt'.length) : path
    const display = (originalWord ? name?.replace(/\.extracted\.txt$/i, '') : name)
      || sourcePath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || sourcePath
    st.previewState.controller?.abort()
    const controller = new AbortController()
    st.previewState.controller = controller
    const previousUrl = st.preview.value?.data?.objectUrl
    if (previousUrl) URL.revokeObjectURL(previousUrl)
    const generation = ++st.previewState.generation
    st.preview.value = { name: display, path: sourcePath, loading: true, error: '', data: null }
    let fallback: PreviewContent | null = null
    try {
      const meta = await conversationApi.filePreview(conv.id, sourcePath).catch((cause) => {
        if (!originalWord || cause?.response?.status !== 404) throw cause
        sourcePath = path
        return conversationApi.filePreview(conv.id, sourcePath)
      })
      if (generation !== st.previewState.generation) return
      fallback = meta
      const word = /\.docx$/i.test(meta.name)
      let objectUrl: string | undefined
      if (word || meta.kind === 'image' || meta.kind === 'pdf') {
        const blob = word
          ? await conversationApi.filePreviewPdf(conv.id, sourcePath, controller.signal)
          : await conversationApi.fileBytes(conv.id, sourcePath)
        if (generation !== st.previewState.generation) return
        objectUrl = URL.createObjectURL(blob)
      }
      st.preview.value = {
        name: meta.name || display, path: meta.path || sourcePath,
        loading: false, error: '', data: { ...meta, kind: word ? 'pdf' : meta.kind, objectUrl },
      }
    } catch (cause) {
      const error = await filePreviewError(cause, '无法加载文件预览')
      if (generation !== st.previewState.generation) return
      st.preview.value = {
        name: fallback?.name || display, path: fallback?.path || sourcePath,
        loading: false, error, data: fallback,
      }
    }
  }

  /** 下载当前预览文件 */
  async function downloadPreview() {
    const conv = st.currentConversation.value
    const previewState = st.preview.value
    if (!conv || !previewState) return
    try {
      const blob = await conversationApi.fileBytes(conv.id, previewState.path)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = previewState.name
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch {
      ElMessage.error('下载失败')
    }
  }

  return { uploadAttachments, removeAttachment, openPreview, downloadPreview }
}
