/**
 * Work Orchestrator 计划视图与事件归约。
 * 实时流与历史回放共用本模块；`work.plan.*` 与 DeepAgents 的 `plan.updated` 互不覆盖。
 */

export const workExecutorLabels: Record<string, string> = {
  general_agent: '通用智能体',
  research: '资料检索',
  data_analysis: '数据分析',
  database: '数据库查询',
  coding: '代码任务',
  business_action: '受控业务操作',
  document_docx: 'Word 文档',
  team: '团队协作',
  software_project: '软件项目',
}

export const workStepStatusLabels: Record<string, string> = {
  pending: '待执行',
  running: '执行中',
  waiting_approval: '等待审批',
  completed: '已完成',
  failed: '失败',
  skipped: '已跳过',
  needs_reconciliation: '待核对',
}

export const workPlanStatusLabels: Record<string, string> = {
  planning: '规划中',
  ready: '已就绪',
  running: '执行中',
  verifying: '验收中',
  paused: '已暂停',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  shadow: '影子计划',
  degraded: '已降级',
}

export interface WorkCheckView {
  id: string
  passed: boolean
  detail: string
}

export interface WorkVerificationView {
  status: string
  checks: WorkCheckView[]
  artifactCount: number
  stopReason: string
}

export interface WorkPlanStepView {
  id: string
  title: string
  executorProfile: string
  executorLabel: string
  dependsOn: string[]
  status: string
  summary?: string
  artifactCount: number
  verification?: WorkVerificationView
}

export interface WorkPlanView {
  planId: string
  revision: number
  status: string
  mode: string
  objective: string
  steps: WorkPlanStepView[]
  completedCount: number
  stepCount: number
  currentStepId?: string
  verification?: WorkVerificationView
  degradedReason?: string
  shadow?: boolean
}

export interface WorkPlanBlockView {
  kind: 'work_plan'
  plan: WorkPlanView
}

function text(value: unknown, limit = 600): string {
  return typeof value === 'string' ? value.slice(0, limit) : ''
}

function stringList(value: unknown, limit = 8): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string').slice(0, limit) : []
}

function knownStatus(value: unknown, dictionary: Record<string, string>, fallback: string): string {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(dictionary, value) ? value : fallback
}

export function readWorkStep(value: unknown): WorkPlanStepView | undefined {
  if (!value || typeof value !== 'object') return undefined
  const item = value as Record<string, unknown>
  const id = text(item.id, 80)
  if (!id) return undefined
  return {
    id,
    title: text(item.title, 160) || id,
    executorProfile: text(item.executor_profile, 40),
    executorLabel: workExecutorLabels[text(item.executor_profile, 40)] ?? text(item.executor_profile, 40),
    dependsOn: stringList(item.depends_on),
    status: knownStatus(item.status, workStepStatusLabels, 'pending'),
    summary: text(item.summary, 600) || undefined,
    artifactCount: Number.isSafeInteger(item.artifact_count) ? Number(item.artifact_count) : 0,
    verification: readVerification(item.verification),
  }
}

export function readVerification(value: unknown): WorkVerificationView | undefined {
  if (!value || typeof value !== 'object') return undefined
  const item = value as Record<string, unknown>
  if (typeof item.status !== 'string') return undefined
  const checks = Array.isArray(item.checks)
    ? item.checks
      .filter((check): check is Record<string, unknown> => !!check && typeof check === 'object')
      .slice(0, 32)
      .filter((check) => typeof check.id === 'string' && typeof check.passed === 'boolean')
      .map((check) => ({ id: String(check.id), passed: Boolean(check.passed), detail: text(check.detail, 600) }))
    : []
  const artifacts = Array.isArray(item.artifact_refs) ? item.artifact_refs.length : 0
  return {
    status: item.status,
    checks,
    artifactCount: artifacts,
    stopReason: text(item.stop_reason, 120),
  }
}

export function readWorkPlan(data: Record<string, unknown>): WorkPlanView | undefined {
  const planId = text(data.plan_id, 60)
  if (!planId) return undefined
  const steps = Array.isArray(data.steps)
    ? data.steps.map(readWorkStep).filter((step): step is WorkPlanStepView => !!step).slice(0, 6)
    : []
  const stepCount = Number.isSafeInteger(data.step_count) ? Number(data.step_count) : steps.length
  const completed = Number.isSafeInteger(data.completed_count)
    ? Number(data.completed_count)
    : steps.filter((step) => step.status === 'completed').length
  return {
    planId,
    revision: Number.isSafeInteger(data.revision) ? Number(data.revision) : 1,
    status: knownStatus(data.status, workPlanStatusLabels, 'running'),
    mode: text(data.mode, 12) || 'auto',
    objective: text(data.objective, 2000),
    steps,
    completedCount: completed,
    stepCount,
    currentStepId: text(data.current_step_id, 80) || undefined,
    verification: readVerification(data.verification),
    shadow: data.mode === 'shadow',
  }
}

const STATUS_RANK: Record<string, number> = {
  pending: 0, running: 1, waiting_approval: 2,
  completed: 3, failed: 3, skipped: 3, needs_reconciliation: 3,
}

export function mergeWorkPlan(previous: WorkPlanView | undefined, next: WorkPlanView): WorkPlanView {
  if (!previous || previous.planId !== next.planId) return next
  const byId = new Map(previous.steps.map((step) => [step.id, step]))
  const steps = next.steps.map((step) => {
    const old = byId.get(step.id)
    if (!old) return step
    // 计划快照可能滞后于步骤事件：状态只能前进，不能被旧的 pending 覆盖
    const keepOld = (STATUS_RANK[old.status] ?? 0) > (STATUS_RANK[step.status] ?? 0)
    return {
      ...old,
      ...step,
      status: keepOld ? old.status : step.status,
      summary: keepOld ? old.summary : (step.summary ?? old.summary),
      artifactCount: Math.max(old.artifactCount, step.artifactCount),
      verification: step.verification ?? old.verification,
    }
  })
  return {
    ...previous,
    ...next,
    steps,
    objective: next.objective || previous.objective,
    verification: next.verification ?? previous.verification,
    degradedReason: next.degradedReason ?? previous.degradedReason,
  }
}

export function applyWorkStepEvent(plan: WorkPlanView, event: string, data: Record<string, unknown>): WorkPlanView {
  const stepId = text(data.step_id, 80)
  if (!stepId) return plan
  const index = plan.steps.findIndex((step) => step.id === stepId)
  const base: WorkPlanStepView = index >= 0 ? plan.steps[index] : {
    id: stepId,
    title: text(data.title, 160) || stepId,
    executorProfile: text(data.executor_profile, 40),
    executorLabel: workExecutorLabels[text(data.executor_profile, 40)] ?? text(data.executor_profile, 40),
    dependsOn: stringList(data.depends_on),
    status: 'pending',
    artifactCount: 0,
  }
  let next: WorkPlanStepView = { ...base }
  if (event === 'work.step.started') {
    next = { ...next, status: 'running', title: text(data.title, 160) || next.title }
    plan.currentStepId = stepId
  } else if (event === 'work.step.completed') {
    next = {
      ...next,
      status: 'completed',
      summary: text(data.summary, 600) || next.summary,
      artifactCount: Array.isArray(data.artifact_refs) ? data.artifact_refs.length : next.artifactCount,
      title: text(data.title, 160) || next.title,
    }
  } else if (event === 'work.step.failed') {
    next = {
      ...next,
      status: 'failed',
      summary: text(data.message, 600) || next.summary,
      title: text(data.title, 160) || next.title,
    }
  } else if (event === 'work.step.verification') {
    next = { ...next, verification: readVerification(data) ?? next.verification }
  }
  const steps = [...plan.steps]
  if (index >= 0) steps[index] = next
  else steps.push(next)
  return {
    ...plan,
    steps,
    stepCount: Math.max(plan.stepCount, steps.length),
    completedCount: steps.filter((step) => step.status === 'completed').length,
  }
}

export function finishWorkPlan(plan: WorkPlanView, runStatus: string | undefined): WorkPlanView {
  if (['completed', 'failed', 'cancelled'].includes(plan.status)) return plan
  if (!['completed', 'failed', 'cancelled', 'limit_exceeded', 'suspended'].includes(runStatus || '')) return plan
  const status = runStatus === 'cancelled' ? 'cancelled'
    : runStatus === 'completed' ? (plan.status === 'verifying' ? 'verifying' : plan.status)
      : 'failed'
  return {
    ...plan,
    status,
    steps: plan.steps.map((step) => {
      if (step.status !== 'running' && step.status !== 'waiting_approval') return step
      return { ...step, status: runStatus === 'cancelled' ? 'skipped' : 'failed', summary: step.summary || '未收到执行结果' }
    }),
  }
}

export function planLastActivity(plan: WorkPlanView): string {
  if (plan.degradedReason) return `计划降级：${plan.degradedReason}`
  if (plan.shadow) return '影子计划已生成（不改变执行路线）'
  const current = plan.steps.find((step) => step.id === plan.currentStepId)
  if (current?.status === 'running') return `正在执行：${current.title}`
  const waiting = plan.steps.find((step) => step.status === 'waiting_approval')
  if (waiting) return `等待审批：${waiting.title}`
  if (plan.status === 'completed') return `工作计划已完成（${plan.completedCount}/${plan.stepCount}）`
  if (plan.status === 'failed') return `工作计划未通过验收（${plan.completedCount}/${plan.stepCount}）`
  return workPlanStatusLabels[plan.status] ?? '工作计划执行中'
}
