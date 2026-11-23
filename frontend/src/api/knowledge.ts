// 知识库 API（/api/v1/knowledge）
import request from '@/utils/request'
import type { AxiosProgressEvent } from 'axios'
import type { KnowledgeBase, KnowledgeDocument, PageResponse } from '@/types'

export const knowledgeApi = {
  listBases: (params?: any) => request.get<any, PageResponse<KnowledgeBase>>('/knowledge/bases', { params }),
  createBase: (data: any) => request.post<any, KnowledgeBase>('/knowledge/bases', data),
  getBase: (id: number) => request.get<any, KnowledgeBase>(`/knowledge/bases/${id}`),
  updateBase: (id: number, data: any) => request.put<any, KnowledgeBase>(`/knowledge/bases/${id}`, data),
  deleteBase: (id: number) => request.delete(`/knowledge/bases/${id}`),

  listDocuments: (kbId: number, params?: any) =>
    request.get<any, PageResponse<KnowledgeDocument>>(`/knowledge/bases/${kbId}/documents`, { params }),
  uploadDocument: (kbId: number, file: File, onUploadProgress?: (event: AxiosProgressEvent) => void) => {
    const form = new FormData()
    form.append('file', file)
    return request.post<any, KnowledgeDocument>(`/knowledge/bases/${kbId}/documents`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress,
    })
  },
  deleteDocument: (kbId: number, docId: number) =>
    request.delete(`/knowledge/bases/${kbId}/documents/${docId}`),

  search: (data: any) => request.post<any, any>('/knowledge/search', data),
  crawlWebpage: (kbId: number, data: { url: string; title?: string }) =>
    request.post<any, KnowledgeDocument>(`/knowledge/bases/${kbId}/crawl`, data),
}
