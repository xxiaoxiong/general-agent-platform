// 应用市场 API（/api/v1/apps）
import request from '@/utils/request'
import type { AppTemplate, AppInstance, PageResponse } from '@/types'

export const appApi = {
  listTemplates: (params?: any) => request.get<any, PageResponse<AppTemplate>>('/apps/templates', { params }),
  getTemplate: (id: number) => request.get<any, AppTemplate>(`/apps/templates/${id}`),
  listInstances: (params?: any) => request.get<any, PageResponse<AppInstance>>('/apps/instances', { params }),
  createInstance: (data: any) => request.post<any, AppInstance>('/apps/instances', data),
  updateInstance: (id: number, data: any) => request.put<any, AppInstance>(`/apps/instances/${id}`, data),
  deleteInstance: (id: number) => request.delete(`/apps/instances/${id}`),
}
