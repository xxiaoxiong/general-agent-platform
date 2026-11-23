// 工作流 API（/api/v1/workflows）
import request from '@/utils/request'
import type { Workflow, WorkflowExecution, PageResponse } from '@/types'

export const workflowApi = {
  list: (params?: any) => request.get<any, PageResponse<Workflow>>('/workflows', { params }),
  create: (data: any) => request.post<any, Workflow>('/workflows', data),
  get: (id: number) => request.get<any, Workflow>(`/workflows/${id}`),
  update: (id: number, data: any) => request.put<any, Workflow>(`/workflows/${id}`, data),
  delete: (id: number) => request.delete(`/workflows/${id}`),
  execute: (id: number, data?: any) => request.post<any, WorkflowExecution>(`/workflows/${id}/execute`, data || {}),
  listExecutions: (id: number, params?: any) =>
    request.get<any, PageResponse<WorkflowExecution>>(`/workflows/${id}/executions`, { params }),
  exportDoc: (id: number, data: { content: string; title?: string; filename?: string }) =>
    request.post(`/workflows/${id}/export-doc`, data, { responseType: 'blob' }),
}
