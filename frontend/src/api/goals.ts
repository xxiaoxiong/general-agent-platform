/** Goal Runtime / Deep Research API（/api/v2/goals）。 */
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')

export interface GoalCriterion {
  id: string
  description: string
  verification_type: 'test' | 'artifact' | 'browser' | 'semantic' | 'evidence' | 'manual' | string
  required: boolean
  status: 'pending' | 'in_progress' | 'passed' | 'failed' | 'blocked' | string
  check_ids?: string[]
  evidence_refs?: string[]
  last_checked_at?: string | null
  failure_reason?: string
  next_step?: string
  source?: string
}

export interface GoalProgress {
  percent: number
  required_total: number
  required_passed: number
  required_failed: number
  optional_total: number
  counts: Record<string, number>
  updated_at?: string
}

export interface GoalIteration {
  iteration_no: number
  run_id: string
  status: string
  executor: string
  plan: Array<Record<string, any>>
  result: Record<string, any>
  evidence_refs: string[]
  started_at?: string | null
  finished_at?: string | null
}

export interface GoalContract {
  goal_id: string
  run_id: string
  origin_run_id?: string | null
  session_id?: number | null
  revision: number
  objective: string
  acceptance_criteria: GoalCriterion[]
  constraints: string[]
  deliverables: string[]
  stop_conditions: string[]
  status: string
  iterations: number
  iterations_detail?: GoalIteration[]
  progress: GoalProgress
  created_at?: string | null
  updated_at?: string | null
  finished_at?: string | null
}

export interface ResearchQuestion {
  id: string
  question: string
  priority: 'high' | 'medium' | 'low' | string
  status: string
  queries?: string[]
  claim_refs?: string[]
  gaps?: string[]
  evidence_count?: number
  claim_count?: number
}

export interface ResearchSourceView {
  source_id: string
  url: string
  title: string
  source_type: string
  author?: string
  published_at?: string
  domain?: string
  http_status?: number
  status: string
  retrieved_at?: string | null
}

export interface ResearchEvidenceView {
  evidence_id: string
  source_id: string
  question_id?: string
  quote: string
  locator?: string
  status: string
}

export interface ResearchClaimView {
  claim_id: string
  question_id?: string
  statement: string
  status: string
  support_evidence: string[]
  contradict_evidence: string[]
  verification: Record<string, any>
}

export interface ResearchSnapshot {
  plan: {
    research_id: string
    goal_id?: string
    run_id: string
    revision: number
    topic: string
    scope: Record<string, any>
    questions: ResearchQuestion[]
    report_outline: any[]
    status: string
  } | null
  sources: ResearchSourceView[]
  evidence: ResearchEvidenceView[]
  claims: ResearchClaimView[]
}

export const goalApi = {
  byRun(runId: string) {
    return request.get<any, GoalContract>(`/goals/by-run/${encodeURIComponent(runId)}`)
  },
  detail(goalId: string) {
    return request.get<any, GoalContract>(`/goals/${encodeURIComponent(goalId)}`)
  },
  listBySession(sessionId: number, limit = 20) {
    return request.get<any, { items: GoalContract[] }>('/goals', { params: { session_id: sessionId, limit } })
  },
  revise(goalId: string, payload: {
    objective?: string
    acceptance_criteria?: Array<string | Record<string, any>>
    note?: string
  }) {
    return request.post<any, GoalContract>(`/goals/${encodeURIComponent(goalId)}/revision`, payload)
  },
  research(goalId: string) {
    return request.get<any, ResearchSnapshot>(`/goals/${encodeURIComponent(goalId)}/research`)
  },
  reviseResearch(goalId: string, payload: {
    topic?: string
    scope?: Record<string, any>
    questions?: Array<string | Record<string, any>>
    report_outline?: any[]
    reason?: string
  }) {
    return request.post<any, Record<string, any>>(
      `/goals/${encodeURIComponent(goalId)}/research/plan`, payload,
    )
  },
}
