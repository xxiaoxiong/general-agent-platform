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

export const conversationApi = {
  list: (params?: any) =>
    request.get<any, { items: Conversation[]; total: number }>('/conversations', { params }),
  get: (id: number) => inlineRequest.get<unknown, Conversation>(`/conversations/${id}`),
  create: (data: { assistant_id?: number; title?: string } = {}) =>
    request.post<any, Conversation>('/conversations', data),
  update: (id: number, data: ConversationMetadata) =>
    inlineRequest.patch<unknown, Conversation>(`/conversations/${id}`, data),
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
}
