// 提示词模板 API（/api/v1/prompts）
import request from '@/utils/request'

export const promptApi = {
  list: (params?: any) => request.get<any, any>('/prompts', { params }),
  create: (data: any) => request.post<any, any>('/prompts', data),
  get: (id: number) => request.get<any, any>(`/prompts/${id}`),
  update: (id: number, data: any) => request.put<any, any>(`/prompts/${id}`, data),
  delete: (id: number) => request.delete(`/prompts/${id}`),
  listVersions: (id: number) => request.get<any, any[]>(`/prompts/${id}/versions`),
  render: (data: { content: string; variables: Record<string, string> }) =>
    request.post<any, any>('/prompts/render', data),
  debug: (data: any) => request.post<any, any>('/prompts/debug', data),
}
