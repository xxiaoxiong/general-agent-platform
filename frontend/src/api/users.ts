// 用户管理 API（/api/v1/users）
import request from '@/utils/request'
import type { User, PageResponse } from '@/types'

export const userApi = {
  list: (params?: any) => request.get<any, PageResponse<User>>('/users', { params }),
  create: (data: any) => request.post<any, User>('/users', data),
  update: (id: number, data: any) => request.put<any, User>(`/users/${id}`, data),
  delete: (id: number) => request.delete(`/users/${id}`),
  assignRole: (userId: number, roleId: number) =>
    request.post(`/users/${userId}/roles/${roleId}`),
  removeRole: (userId: number, roleId: number) =>
    request.delete(`/users/${userId}/roles/${roleId}`),
}
