// 认证 API（/api/v1/auth）
import request from '@/utils/request'
import type { User } from '@/types'

export const authApi = {
  login: (data: { username: string; password: string }) =>
    request.post<any, any>('/auth/login', data),
  getMe: () => request.get<any, User>('/auth/me'),
  changePassword: (data: { old_password: string; new_password: string }) =>
    request.post('/auth/change-password', data),
  refresh: (refresh_token: string) =>
    request.post<any, any>('/auth/refresh', null, { params: { refresh_token_str: refresh_token } }),
}
