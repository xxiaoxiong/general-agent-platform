/**
 * Agent 测评 API（/api/v2/evaluation，仅超管）：
 * 数据集（CRUD/用例/JSONL 导入/社区种子）+ 测评任务（创建执行/结果/取消/恢复/导出/执行轨迹）。
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
  /** 复杂度：L1 简单 / L2 中等 / L3 复杂 / L4 困难 */
  complexity: string
  /** 任务类型：direct_qa / multi_step_reasoning / tool_use / instruction_following / multi_turn / adversarial / project_build / long_document / complex_math / deep_research */
  task_type: string
  /** 任务声明的必需工具（客观对照） */
  required_tools: string[]
  /** 期望最少执行步数（客观对照） */
  expected_min_steps: number | null
  /** 多轮任务的后续用户消息（input 为首轮） */
  turns: string[]
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

/** 一组用例的分层汇总（纯客观） */
export interface AggLayer {
  total: number
  completed: number
  passed: number
  failed: number
  error: number
  pass_rate: number | null
  avg_score: number | null
  avg_steps: number | null
  avg_tool_calls: number | null
  avg_duration_s: number | null
  /** 平均输出字数（长程任务规模） */
  avg_output_chars?: number | null
}

/** 问题用例（纯客观事实，供开发分析） */
export interface ProblemItem {
  item_id: number
  position: number
  status: string
  complexity: string
  task_type: string
  problems: string[]
  score: number | null
  steps?: number
  tool_calls?: number
  output_chars?: number | null
  used_tools?: string[]
  missing_tools?: string[]
  error?: string | null
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
  /** 按复杂度分层（L1-L4） */
  by_complexity?: Record<string, AggLayer>
  /** 按任务类型分层 */
  by_task_type?: Record<string, AggLayer>
  /** 工具使用统计 */
  tool_usage?: Record<string, { calls: number; failed: number }>
  /** Agent 能力事实 */
  capability?: {
    avg_steps?: number | null
    max_steps?: number
    multi_step_count?: number
    zero_tool_count?: number
    tool_error_rate?: number
    missing_required_tools_count?: number
    limit_exceeded_count?: number
    /** 长程任务客观指标 */
    avg_output_chars?: number | null
    max_output_chars?: number
    constraint_items?: number
    constraint_total?: number
    constraint_passed?: number
    constraint_ratio?: number | null
  }
  problem_items?: ProblemItem[]
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
  complexity: string
  task_type: string
  steps?: number | null
  tool_calls?: number | null
  output_chars?: number | null
  used_tools?: string[]
  missing_tools?: string[]
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
  snap_meta: Record<string, any>
  actual_output: string
  score_detail: Record<string, any>
  stats: Record<string, any>
  started_at: string | null
  finished_at: string | null
}

/** 执行轨迹（纯客观：事件时间线 + 工具执行账本） */
export interface EvalRunItemTrace {
  item_id: number
  runs: Array<{
    run_id: string
    events: Array<{
      sequence: number
      event_type: string
      payload: string | null
      created_at: string | null
    }>
    tools: Array<{
      tool_name: string
      status: string
      attempt_count: number
      args: string | null
      result: string | null
      error: string | null
      started_at: string | null
      finished_at: string | null
    }>
  }>
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
    request.post<any, { created: string[]; updated: string[]; skipped: string[]; available: string[] }>(
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
  getRunItemTrace: (itemId: number) =>
    request.get<any, EvalRunItemTrace>(`/evaluation/run-items/${itemId}/trace`),
  cancelRun: (id: number) =>
    request.post<any, { status: string; changed: boolean }>(`/evaluation/runs/${id}/cancel`, {}),
  resumeRun: (id: number) =>
    request.post<any, { status: string }>(`/evaluation/runs/${id}/resume`, {}),
  exportRunMarkdown: (id: number) =>
    request.get<any, string>(`/evaluation/runs/${id}/markdown`, { responseType: 'text' as any }),
}
