// 会话 API（/api/v2/conversations，含会话附件与工作区文件预览）
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')
const inlineRequest = createRequest('/api/v2', { inlineErrors: true })

export interface Conversation {
  id: number
  assistant_id: number
  title: string
  is_pinned: boolean
  pinned_at: string | null
  message_count: number
  active_run_id: string | null
  /** 项目归属（Workspace Sandbox）；旧会话为 null */
  project_id?: number | null
  created_at: string | null
  updated_at: string | null
}

export type ConversationMetadata = Partial<Pick<Conversation, 'title' | 'is_pinned'>>

/** 通用工作区文件预览元数据（右侧临时预览区块） */
export interface FilePreview {
  name: string
  path: string
  /** 渲染分支：image / pdf / markdown / code / text / table / office / binary */
  kind: string
  size: number
  mime: string
  /** 代码高亮语言提示 */
  language?: string
  /** 文本 / 代码 / Markdown / Office 抽取文本 */
  text?: string
  /** 表格二维数组（csv / xlsx） */
  rows?: string[][]
  truncated?: boolean
}

/** 预览内容 = 元数据 + 图片/PDF 的本地 objectURL（前端解析后附加） */
export interface PreviewContent extends FilePreview {
  objectUrl?: string
}

/** 工作区文件清单条目（右侧文件面板） */
export interface WorkspaceFile {
  /** 工作区内相对路径 */
  path: string
  name: string
  size: number
  /** 最后修改时间（秒级时间戳） */
  mtime: number
  /** 渲染分支（同 FilePreview.kind） */
  kind: string
}

/** 懒加载文件树条目（目录 / 文件） */
export interface WorkspaceTreeEntry {
  path: string
  name: string
  is_dir: boolean
  size: number
  mtime: number
  /** 文件渲染分支 */
  kind?: string
}

export const conversationApi = {
  list: (params?: any) =>
    request.get<any, { items: Conversation[]; total: number }>('/conversations', { params }),
  get: (id: number) => inlineRequest.get<unknown, Conversation>(`/conversations/${id}`),
  create: (data: { assistant_id?: number; title?: string; project_id?: number } = {}) =>
    request.post<any, Conversation>('/conversations', data),
  update: (id: number, data: ConversationMetadata) =>
    inlineRequest.patch<unknown, Conversation>(`/conversations/${id}`, data),
  /** 切换会话所属项目（活动 Run 期间后端返回 409） */
  setProject: (id: number, projectId: number | null) =>
    inlineRequest.patch<unknown, Conversation>(`/conversations/${id}/project`, {
      project_id: projectId,
    }),
  messages: (id: number) => request.get<any, { items: any[] }>(`/conversations/${id}/messages`),
  delete: (id: number) => request.delete(`/conversations/${id}`),
  /** 上传会话附件（写入会话工作区 uploads/，二进制文档同步抽取文本副本） */
  uploadFile: (id: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request.post<any, { name: string; path: string; text_path: string | null; size: number }>(
      `/conversations/${id}/files`, form,
      { headers: { 'Content-Type': 'multipart/form-data' } },
    )
  },
  deleteFile: (id: number, filename: string) =>
    request.delete(`/conversations/${id}/files/${encodeURIComponent(filename)}`),
  /** 下载已上传附件（图片预览用；返回 Blob） */
  fileBlob: (id: number, filename: string) =>
    request.get<any, Blob>(
      `/conversations/${id}/files/${encodeURIComponent(filename)}`, { responseType: 'blob' },
    ),
  /** 通用工作区文件预览元数据（path 可为 uploads/xx 或 Agent 产物相对路径） */
  filePreview: (id: number, path: string) =>
    inlineRequest.get<unknown, FilePreview>(`/conversations/${id}/file-preview`, { params: { path, layout_only: true } }),
  filePreviewPdf: (id: number, path: string, signal?: AbortSignal) =>
    inlineRequest.get<unknown, Blob>(`/conversations/${id}/file-preview-pdf`, {
      params: { path }, responseType: 'blob', signal,
    }),
  /** 通用工作区文件字节（图片 / PDF 预览、二进制下载） */
  fileBytes: (id: number, path: string) =>
    request.get<any, Blob>(`/conversations/${id}/file-bytes`, {
      params: { path }, responseType: 'blob',
    }),
  /** 列出会话工作区文件（右侧文件面板；排除平台内部目录） */
  fileList: (id: number) =>
    request.get<any, { files: WorkspaceFile[]; truncated: boolean }>(`/conversations/${id}/file-list`),
  /** 按目录懒加载文件树（path 为空=工作区根目录；不按总数截断） */
  fileTree: (id: number, path = '') =>
    request.get<any, { path: string; entries: WorkspaceTreeEntry[] }>(
      `/conversations/${id}/file-tree`, { params: { path } },
    ),
  /** 打包下载会话工作区文件（与 fileList 同一范围） */
  downloadWorkspaceZip: (id: number) =>
    request.get<any, Blob>(`/conversations/${id}/files.zip`, { responseType: 'blob' }),
}

/** 历史会话记忆（Conversation Memory）：用户开关 / 索引状态 / 排除 / 项目共享 */
export const memoryApi = {
  settings: () => request.get<any, {
    settings: {
      history_enabled: boolean
      auto_retrieval_enabled: boolean
      project_memory_enabled: boolean
      global_enabled: boolean
      planner_mode: string
      index_version: string
    }
    index: {
      indexed_entries: number
      pending_jobs: number
      failed_jobs: number
      last_job: Record<string, any> | null
    }
  }>('/memory/settings'),
  updateSettings: (data: {
    history_enabled?: boolean
    auto_retrieval_enabled?: boolean
    project_memory_enabled?: boolean
  }) => inlineRequest.patch<unknown, any>('/memory/settings', data),
  status: () => request.get<any, any>('/memory/status'),
  /** 异步重建当前用户的历史索引（Job 入队，立即返回） */
  reindex: () => request.post<any, any>('/memory/reindex'),
  /** 排除/恢复某次会话参与后续检索 */
  excludeConversation: (id: number, excluded: boolean, reason?: string) =>
    inlineRequest.put<unknown, any>(`/memory/conversations/${id}/exclusion`, { excluded, reason }),
  /** 显式共享会话到项目（缺省=会话当前项目） */
  shareConversation: (id: number, projectId?: number) =>
    inlineRequest.post<unknown, any>(`/memory/conversations/${id}/share`, { project_id: projectId }),
  revokeShare: (id: number) =>
    inlineRequest.delete<unknown, any>(`/memory/conversations/${id}/share`),
}
