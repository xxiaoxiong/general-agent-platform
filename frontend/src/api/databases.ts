// 数据库连接 API（/api/v1/databases）
import request from '@/utils/request'

export const databaseApi = {
  list: () => request.get<any, any>('/databases'),
  create: (data: any) => request.post<any, any>('/databases', data),
  get: (id: number) => request.get<any, any>(`/databases/${id}`),
  update: (id: number, data: any) => request.put<any, any>(`/databases/${id}`, data),
  delete: (id: number) => request.delete(`/databases/${id}`),
  test: (id: number) => request.post<any, any>(`/databases/${id}/test`),
  getSchema: (id: number, table?: string) =>
    request.get<any, any>(`/databases/${id}/schema`, { params: table ? { table } : {} }),
  query: (id: number, data: { sql: string; limit?: number }) =>
    request.post<any, any>(`/databases/${id}/query`, data),
}
