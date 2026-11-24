/**
 * 会话附件、右侧临时文件预览与工作目录文件面板：
 * - 附件上传即写入会话工作区（uploads/），随消息告知模型读取；
 * - 点文件名 → 右侧预览（对齐 Trae），可关闭 / 下载；
 * - 文件面板列出当前工作目录文件（Codex 式），点击预览，可打包下载。
 */
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { conversationApi, type PreviewContent } from '@/api/conversations'
import { requireZipSignature, saveBlob } from '@/api/artifacts'
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
      if (completeCount) st.noteWorkspaceChange()
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

  // ── 工作目录文件面板（清单刷新 / 开合 / 打包下载）──────────────────────────
  let fileListTimer: number | undefined
  const workspaceZipBusy = ref(false)

  /** 刷新工作区文件清单：immediate=立即拉取；否则 800ms 防抖（面板与预览均未打开时不请求） */
  async function refreshFileList(immediate = false) {
    const conv = st.currentConversation.value
    if (!conv) {
      st.fileList.value = []
      return
    }
    if (!immediate) {
      if (!st.filesPanelOpen.value && !st.preview.value) return
      if (fileListTimer) clearTimeout(fileListTimer)
      fileListTimer = window.setTimeout(() => { void refreshFileList(true) }, 800)
      return
    }
    const previewed = st.preview.value
    const previous = previewed && st.fileList.value.find((item) => item.path === previewed.path)
    st.filesLoading.value = true
    st.filesError.value = ''
    try {
      const res = await conversationApi.fileList(conv.id)
      if (st.inactive.value || st.currentConversation.value?.id !== conv.id) return
      const files = res.files || []
      const next = previewed && files.find((item) => item.path === previewed.path)
      st.fileList.value = files
      st.filesTruncated.value = res.truncated === true
      // 当前预览文件被 Agent 改写时自动重载内容（大小 / 修改时间变化即视为变动）
      if (previewed && next && (!previous || previous.size !== next.size || previous.mtime !== next.mtime)) {
        void openPreview(previewed.path, previewed.name)
      }
    } catch {
      if (st.inactive.value || st.currentConversation.value?.id !== conv.id) return
      st.filesError.value = '无法加载文件列表'
    } finally {
      if (st.currentConversation.value?.id === conv.id) st.filesLoading.value = false
    }
  }

  // 工具写入 / 会话切换等文件变动信号 → 防抖刷新（面板与预览均未打开时不请求）
  watch(() => st.workspaceVersion.value, () => { void refreshFileList() })

  function openFilesPanel() {
    st.closePreview()
    st.filesPanelOpen.value = true
    void refreshFileList(true)
  }

  function closeFilesPanel() {
    st.filesPanelOpen.value = false
  }

  /** thread-head「文件」按钮：列表态再点关闭；预览态点击回列表 */
  function toggleFilesPanel() {
    if (st.filesPanelOpen.value && !st.preview.value && !st.documentPreview.value) {
      closeFilesPanel()
    } else {
      openFilesPanel()
    }
  }

  /** 打包下载当前会话工作区文件（文件面板 ZIP） */
  async function downloadWorkspaceZip() {
    const conv = st.currentConversation.value
    if (!conv || workspaceZipBusy.value) return
    workspaceZipBusy.value = true
    try {
      const blob = await conversationApi.downloadWorkspaceZip(conv.id)
      await requireZipSignature(blob)
      saveBlob(blob, `workspace-${conv.id}.zip`)
    } catch {
      ElMessage.error('打包下载失败，请重试')
    } finally {
      workspaceZipBusy.value = false
    }
  }

  return {
    uploadAttachments, removeAttachment, openPreview, downloadPreview,
    refreshFileList, openFilesPanel, closeFilesPanel, toggleFilesPanel,
    downloadWorkspaceZip, workspaceZipBusy,
  }
}
