/** Goal Runtime 视图模型：目标契约、验收条件与 Deep Research 计划归约。 */
import type { GoalCriterion, GoalProgress } from '@/api/goals'

export interface GoalResearchView {
  researchId: string
  topic: string
  status: string
  scope: Record<string, any>
  questions: Array<{
    id: string
    question: string
    priority: string
    status: string
    evidenceCount: number
    claimCount: number
  }>
  gaps: string[]
  sourceCount: number
  evidenceCount: number
  claimCount: number
  sources: Array<{ sourceId: string; title: string; url: string; status: string }>
  claims: Array<{ claimId: string; statement: string; status: string }>
}

export interface GoalIterationView {
  iterationNo: number
  status: string
  executor: string
  reason: string
  startedAt?: string | null
  finishedAt?: string | null
}

export interface GoalView {
  goalId: string
  revision: number
  objective: string
  status: string
  iteration: number
  progress: GoalProgress
  criteria: GoalCriterion[]
  blocking: Array<{ id: string; description: string; failure_reason?: string }>
  research?: GoalResearchView
  iterationsDetail?: GoalIterationView[]
}

const STATUS_LABELS: Record<string, string> = {
  active: '执行中', completed: '已达成', paused: '已暂停', blocked: '受阻',
  failed: '失败', cancelled: '已取消', superseded: '已被新目标取代',
}

const CRITERIA_LABELS: Record<string, string> = {
  pending: '待验证', in_progress: '验证中', passed: '已通过', failed: '未通过', blocked: '受阻',
}

export function goalStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

export function criterionStatusLabel(status: string): string {
  return CRITERIA_LABELS[status] ?? status
}

function progressOf(raw: any): GoalProgress {
  return {
    percent: Number(raw?.percent) || 0,
    required_total: Number(raw?.required_total) || 0,
    required_passed: Number(raw?.required_passed) || 0,
    required_failed: Number(raw?.required_failed) || 0,
    optional_total: Number(raw?.optional_total) || 0,
    counts: (raw?.counts && typeof raw.counts === 'object') ? raw.counts : {},
    updated_at: typeof raw?.updated_at === 'string' ? raw.updated_at : undefined,
  }
}

function criteriaOf(raw: any): GoalCriterion[] {
  if (!Array.isArray(raw)) return []
  return raw.filter((item: any) => item && typeof item.description === 'string').slice(0, 40)
    .map((item: any) => ({
      id: String(item.id || ''),
      description: String(item.description || ''),
      verification_type: String(item.verification_type || 'semantic'),
      required: item.required !== false,
      status: String(item.status || 'pending'),
      failure_reason: String(item.failure_reason || ''),
      evidence_refs: Array.isArray(item.evidence_refs) ? item.evidence_refs : [],
    }))
}

export function readGoalContract(data: Record<string, any>): GoalView {
  return {
    goalId: String(data.goal_id || ''),
    revision: Number(data.revision) || 1,
    objective: String(data.objective || ''),
    status: 'active',
    iteration: 0,
    progress: progressOf(data.progress),
    criteria: criteriaOf(data.acceptance_criteria),
    blocking: [],
  }
}

export function applyGoalEvaluation(goal: GoalView, data: Record<string, any>): GoalView {
  return {
    ...goal,
    revision: Number(data.revision) || goal.revision,
    status: String(data.status || goal.status),
    iteration: Number(data.iteration) || goal.iteration,
    progress: progressOf(data.progress ?? goal.progress),
    criteria: Array.isArray(data.criteria) ? criteriaOf(data.criteria) : goal.criteria,
    blocking: Array.isArray(data.blocking)
      ? data.blocking.filter((item: any) => item && item.description).slice(0, 12).map((item: any) => ({
        id: String(item.id || ''),
        description: String(item.description || ''),
        failure_reason: String(item.failure_reason || ''),
      }))
      : goal.blocking,
  }
}

export function mergeResearch(goal: GoalView, data: Record<string, any>, snapshot?: Record<string, any>): GoalView {
  const questions = Array.isArray(data.questions) ? data.questions : []
  const counts = snapshot?.counts || {}
  const gaps = Array.isArray(snapshot?.gaps)
    ? snapshot.gaps.map((item: any) => String(item.detail || item.type || '')).filter(Boolean)
    : (goal.research?.gaps ?? [])
  const sources = Array.isArray(snapshot?.sources)
    ? snapshot.sources.slice(0, 40).map((item: any) => ({
      sourceId: String(item.source_id || ''),
      title: String(item.title || item.url || ''),
      url: String(item.url || ''),
      status: String(item.status || 'discovered'),
    }))
    : (goal.research?.sources ?? [])
  const claims = Array.isArray(snapshot?.claims)
    ? snapshot.claims.slice(0, 40).map((item: any) => ({
      claimId: String(item.claim_id || ''),
      statement: String(item.statement || ''),
      status: String(item.status || 'pending'),
    }))
    : (goal.research?.claims ?? [])
  return {
    ...goal,
    research: {
      researchId: String(data.research_id || goal.research?.researchId || ''),
      topic: String(data.topic || snapshot?.topic || goal.research?.topic || ''),
      status: String(data.status || snapshot?.status || goal.research?.status || 'planning'),
      scope: (data.scope && typeof data.scope === 'object')
        ? data.scope : (goal.research?.scope ?? snapshot?.scope ?? {}),
      questions: questions.length
        ? questions.slice(0, 40).map((item: any) => ({
          id: String(item.id || ''),
          question: String(item.question || ''),
          priority: String(item.priority || 'medium'),
          status: String(item.status || 'pending'),
          evidenceCount: Number(item.evidence_count ?? 0) || 0,
          claimCount: Number(item.claim_count ?? 0) || 0,
        }))
        : (goal.research?.questions ?? []),
      gaps: gaps.slice(0, 12),
      sourceCount: Number(counts.sources ?? goal.research?.sourceCount ?? sources.length) || 0,
      evidenceCount: Number(counts.evidence ?? goal.research?.evidenceCount ?? 0) || 0,
      claimCount: Number(counts.claims ?? goal.research?.claimCount ?? claims.length) || 0,
      sources,
      claims,
    },
  }
}
