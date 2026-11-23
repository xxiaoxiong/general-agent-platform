// 模型管理 API（/api/v1/models）
import request from '@/utils/request'
import type { ModelProvider, ModelConfig } from '@/types'

export interface ModelProbeReport {
  tool_calling: boolean
  tool_calling_latency_ms: number | null
  streaming: boolean
  streaming_latency_ms: number | null
  passed: boolean
}

export const modelApi = {
  listProviders: () => request.get<any, ModelProvider[]>('/models/providers'),
  createProvider: (data: any) => request.post<any, ModelProvider>('/models/providers', data),
  updateProvider: (id: number, data: any) => request.put<any, ModelProvider>(`/models/providers/${id}`, data),
  deleteProvider: (id: number) => request.delete(`/models/providers/${id}`),
  testProvider: (id: number) => request.post<any, any>(`/models/providers/${id}/test`),

  listConfigs: (params?: any) => request.get<any, ModelConfig[]>('/models/configs', { params }),
  createConfig: (data: any) => request.post<any, ModelConfig>('/models/configs', data),
  updateConfig: (id: number, data: any) => request.put<any, ModelConfig>(`/models/configs/${id}`, data),
  testConfig: (id: number) => request.post<any, ModelProbeReport | { detail: { report?: ModelProbeReport } }>(
    `/models/configs/${id}/test`, undefined, { validateStatus: status => status === 200 || status === 422 },
  ),
  deleteConfig: (id: number) => request.delete(`/models/configs/${id}`),
}
