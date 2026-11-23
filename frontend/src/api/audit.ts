// 审计日志 API（/api/v1/audit）
import { createRequest } from '@/utils/request'
import type { AuditLog, AuditLogDetail, AuditLogQuery, PageResponse } from '@/types'

const auditRequest = createRequest('/api/v1', { inlineErrors: true })

export const auditApi = {
  list: (params: AuditLogQuery, signal?: AbortSignal) =>
    auditRequest.get<unknown, PageResponse<AuditLog>>('/audit', { params, signal }),
  get: (id: number, signal?: AbortSignal) =>
    auditRequest.get<unknown, AuditLogDetail>(`/audit/${id}`, { signal }),
}
