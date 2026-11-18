/**
 * 自进化闭环 API（/api/v2/evolution，仅超管）：
 * 评估概览/列表/触发、进化报告生成与查看、建议一键应用、变更回滚/审计。
 */
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')

export interface EvolutionOverview {
  window_hours: number
  overall: Record<string, any>
  by_version: Array<Record<string, any>>
}

export interface EvaluationItem {
  id: number
  run_id: string
  agent_id: number
  agent_version_id: number | null
  status: string
  success: boolean | null
  overall: number | null
  deterministic: Record<string, any>
  judge_scores: Record<string, any>
  error: string | null
  evaluated_at: string | null
  langfuse_trace_id: string | null
  langfuse_trace_url: string | null
}

export interface EvolutionSuggestion {
  type: string
  target: string
  current: any
  proposed: any
  diff?: string
  rationale?: string
  evidence_run_ids?: string[]
  expected_impact?: Record<string, any>
  priority?: number
  auto_applicable?: boolean
}

export interface EvolutionReport {
  id: number
  agent_id: number
  window_start: string | null
  window_end: string | null
  baseline_version_no: number | null
  status: string
  summary_text: string
  suggestions: EvolutionSuggestion[]
  metrics_before: Record<string, any>
  metrics_after: Record<string, any>
  created_by: string | null
  validated_at: string | null
  created_at: string | null
}

export interface EvolutionChange {
  id: number
  report_id: number
  agent_id: number
  change_type: string
  target: string
  before: any
  after: any
  applied_version_no: number | null
  status: string
  validation: Record<string, any> | null
  revert_of_change_id: number | null
  applied_by: string | null
  applied_at: string | null
  reverted_at: string | null
}

export interface DevBriefing {
  meta: Record<string, any>
  runs: Record<string, any>
  failed_runs: Array<Record<string, any>>
  tools: Record<string, any>
  hitl: Record<string, any>
  by_version: Array<Record<string, any>>
  evaluations: Record<string, any>
  changes: Array<Record<string, any>>
}

export const evolutionApi = {
  overview: (params?: { agent_id?: number; hours?: number }) =>
    request.get<any, EvolutionOverview>('/evolution/overview', { params }),
  listEvaluations: (params?: { agent_id?: number; version_no?: number; page?: number; page_size?: number }) =>
    request.get<any, { items: EvaluationItem[]; total: number; page: number; page_size: number }>(
      '/evolution/evaluations', { params }),
  triggerEvaluations: (data: { run_ids?: string[]; agent_id?: number }) =>
    request.post<any, { queued: number }>('/evolution/evaluations/trigger', data),
  generateReport: (data: { agent_id: number; window_hours?: number }) =>
    request.post<any, EvolutionReport>('/evolution/reports/generate', data),
  listReports: (params?: { agent_id?: number; page?: number; page_size?: number }) =>
    request.get<any, { items: EvolutionReport[]; total: number; page: number; page_size: number }>(
      '/evolution/reports', { params }),
  getReport: (id: number) => request.get<any, EvolutionReport>(`/evolution/reports/${id}`),
  exportMarkdown: (id: number) =>
    request.get<any, { report_id: number; markdown: string }>(`/evolution/reports/${id}/markdown`),
  applySuggestion: (reportId: number, index: number) =>
    request.post<any, { change_id: number; new_version_no: number }>(
      `/evolution/reports/${reportId}/suggestions/${index}/apply`),
  listChanges: (params?: { agent_id?: number }) =>
    request.get<any, { items: EvolutionChange[] }>('/evolution/changes', { params }),
  rollbackChange: (changeId: number) =>
    request.post<any, { restored_version_no: number }>(`/evolution/changes/${changeId}/rollback`),
  getBriefing: (params?: { agent_id?: number; hours?: number }) =>
    request.get<any, DevBriefing>('/evolution/briefing', { params }),
  getBriefingMarkdown: (params?: { agent_id?: number; hours?: number }) =>
    request.get<any, { markdown: string }>('/evolution/briefing/markdown', { params }),
}
