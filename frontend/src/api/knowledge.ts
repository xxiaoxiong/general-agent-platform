// 知识库 API（/api/v1/knowledge）
import request from '@/utils/request'
import type { AxiosProgressEvent } from 'axios'
import type {
  KnowledgeBase, KnowledgeDocument, IngestionJob, ParsedPreview, ChunkPage,
  KnowledgeSearchResponse, KnowledgeEvaluation, PageResponse,
} from '@/types'

export interface KnowledgeDocumentWithJob extends KnowledgeDocument {
  job?: IngestionJob | null
}

export const knowledgeApi = {
  listBases: (params?: any) => request.get<any, PageResponse<KnowledgeBase>>('/knowledge/bases', { params }),
  createBase: (data: any) => request.post<any, KnowledgeBase>('/knowledge/bases', data),
  getBase: (id: number) => request.get<any, KnowledgeBase>(`/knowledge/bases/${id}`),
  updateBase: (id: number, data: any) => request.put<any, KnowledgeBase>(`/knowledge/bases/${id}`, data),
  deleteBase: (id: number) => request.delete(`/knowledge/bases/${id}`),

  listDocuments: (kbId: number, params?: any) =>
    request.get<any, PageResponse<KnowledgeDocument>>(`/knowledge/bases/${kbId}/documents`, { params }),
  getDocument: (kbId: number, docId: number) =>
    request.get<any, KnowledgeDocument>(`/knowledge/bases/${kbId}/documents/${docId}`),
  uploadDocument: (kbId: number, file: File, onUploadProgress?: (event: AxiosProgressEvent) => void) => {
    const form = new FormData()
    form.append('file', file)
    return request.post<any, KnowledgeDocumentWithJob>(`/knowledge/bases/${kbId}/documents`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress,
    })
  },
  deleteDocument: (kbId: number, docId: number) =>
    request.delete(`/knowledge/bases/${kbId}/documents/${docId}`),

  getIngestion: (docId: number) =>
    request.get<any, IngestionJob>(`/knowledge/documents/${docId}/ingestion`),
  reindexDocument: (docId: number, data: { new_generation?: boolean } = {}) =>
    request.post<any, KnowledgeDocumentWithJob>(`/knowledge/documents/${docId}/reindex`, data),
  rebuildBase: (kbId: number) =>
    request.post<any, { message: string; generation_id: number; generation_no: number; jobs: number[] }>(
      `/knowledge/bases/${kbId}/rebuild`,
    ),

  getParsedPreview: (docId: number, params?: { offset?: number; limit?: number }) =>
    request.get<any, ParsedPreview>(`/knowledge/documents/${docId}/parsed-preview`, { params }),
  listChunks: (docId: number, params?: { page?: number; page_size?: number }) =>
    request.get<any, ChunkPage>(`/knowledge/documents/${docId}/chunks`, { params }),

  search: (data: any) => request.post<any, KnowledgeSearchResponse>('/knowledge/search', data),
  crawlWebpage: (kbId: number, data: { url: string; title?: string }) =>
    request.post<any, KnowledgeDocumentWithJob>(`/knowledge/bases/${kbId}/crawl`, data),

  createEvaluation: (kbId: number, data: any) =>
    request.post<any, KnowledgeEvaluation>(`/knowledge/bases/${kbId}/evaluations`, data),
  listEvaluations: (kbId: number, params?: { limit?: number }) =>
    request.get<any, KnowledgeEvaluation[]>(`/knowledge/bases/${kbId}/evaluations`, { params }),
  getEvaluation: (evaluationId: number) =>
    request.get<any, KnowledgeEvaluation>(`/knowledge/evaluations/${evaluationId}`),
}
