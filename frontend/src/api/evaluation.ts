/**
 * Agent 测评 API（/api/v2/evaluation，仅超管）：
 * 数据集（CRUD/用例/JSONL 导入/社区种子）+ 测评任务（创建执行/结果/取消/恢复/导出）。
 */
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')

export interface EvalDatasetItem {
  id: number
  dataset_id: number
  position: number
  input: string
  expected: string
  criteria: string
  scoring_method: string | null
  scoring_config: Record<string, any>
  metadata: Record<string, any>
}

export interface EvalDataset {
  id: number
  name: string
  description: string
  source: string
  category: string
  scoring_method: string
  reference: string | null
  item_count: number | null
  created_at: string | null
  updated_at: string | null
  items?: EvalDatasetItem[]
}

export interface EvalRunAggregate {
  total?: number
  completed?: number
  passed?: number
  failed?: number
  error?: number
  skipped?: number
  pending?: number
  pass_rate?: number | null
  avg_score?: number | null
  avg_duration_s?: number | null
  total_tokens?: number
  total_tool_calls?: number
}

export interface EvalRun {
  id: number
  name: string
  dataset_id: number
  agent_id: number
  agent_version_id: number | null
  status: string
  config: Record<string, any>
  aggregate: EvalRunAggregate
  error: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string | null
  dataset_name?: string | null
  agent_name?: string | null
}

export interface EvalRunItemBrief {
  id: number
  dataset_item_id: number | null
  position: number
  status: string
  score: number | null
  passed: boolean | null
  run_id: string | null
  error: string | null
  input_preview: string
  output_preview: string
}

export interface EvalRunItemDetail extends EvalRunItemBrief {
  eval_run_id: number
  snap_input: string
  snap_expected: string
  snap_criteria: string
  snap_scoring_method: string
  snap_scoring_config: Record<string, any>
  actual_output: string
  score_detail: Record<string, any>
  stats: Record<string, any>
  started_at: string | null
  finished_at: string | null
}

export interface RunCreatePayload {
  name?: string
  dataset_id: number
  agent_id: number
  approval_mode?: string
  use_memory?: boolean
  timeout_s?: number
  limit?: number
}

export const evaluationApi = {
  // ── 数据集 ──
  listDatasets: () =>
    request.get<any, { items: EvalDataset[] }>('/evaluation/datasets'),
  createDataset: (data: Partial<EvalDataset> & { name: string; items?: any[] }) =>
    request.post<any, EvalDataset>('/evaluation/datasets', data),
  getDataset: (id: number) =>
    request.get<any, EvalDataset>(`/evaluation/datasets/${id}`),
  updateDataset: (id: number, data: Partial<EvalDataset>) =>
    request.put<any, EvalDataset>(`/evaluation/datasets/${id}`, data),
  deleteDataset: (id: number) =>
    request.delete<any, { deleted: boolean }>(`/evaluation/datasets/${id}`),
  addItem: (datasetId: number, data: Partial<EvalDatasetItem>) =>
    request.post<any, EvalDatasetItem>(`/evaluation/datasets/${datasetId}/items`, data),
  updateItem: (datasetId: number, itemId: number, data: Partial<EvalDatasetItem>) =>
    request.put<any, EvalDatasetItem>(`/evaluation/datasets/${datasetId}/items/${itemId}`, data),
  deleteItem: (datasetId: number, itemId: number) =>
    request.delete<any, { deleted: boolean }>(`/evaluation/datasets/${datasetId}/items/${itemId}`),
  importItems: (data: {
    name?: string
    description?: string
    category?: string
    scoring_method?: string
    reference?: string
    dataset_id?: number
    content?: string
    items?: any[]
  }) => request.post<any, { dataset_id: number; imported: number }>('/evaluation/datasets/import', data),
  seedCommunity: () =>
    request.post<any, { created: string[]; skipped: string[]; available: string[] }>(
      '/evaluation/datasets/seed-community', {}),

  // ── 测评任务 ──
  createRun: (data: RunCreatePayload) =>
    request.post<any, EvalRun>('/evaluation/runs', data),
  listRuns: (params?: { dataset_id?: number; agent_id?: number }) =>
    request.get<any, { items: EvalRun[] }>('/evaluation/runs', { params }),
  getRun: (id: number) => request.get<any, EvalRun>(`/evaluation/runs/${id}`),
  listRunItems: (runId: number, params?: { status?: string }) =>
    request.get<any, { items: EvalRunItemBrief[] }>(`/evaluation/runs/${runId}/items`, { params }),
  getRunItem: (itemId: number) =>
    request.get<any, EvalRunItemDetail>(`/evaluation/run-items/${itemId}`),
  cancelRun: (id: number) =>
    request.post<any, { status: string; changed: boolean }>(`/evaluation/runs/${id}/cancel`, {}),
  resumeRun: (id: number) =>
    request.post<any, { status: string }>(`/evaluation/runs/${id}/resume`, {}),
  exportRunMarkdown: (id: number) =>
    request.get<any, string>(`/evaluation/runs/${id}/markdown`, { responseType: 'text' as any }),
}
