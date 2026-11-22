export const teamPhaseLabels = {
  planning: '正在拆解任务',
  working: '团队协作中',
  synthesizing: '负责人正在汇总',
  verifying: '团队正在复核成果',
  repairing: '团队正在修订成果',
  completed: '团队协作完成',
  failed: '团队协作失败',
  cancelled: '团队协作已停止',
} as const

export const teamTaskLabels = {
  pending: '待执行',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  skipped: '已跳过',
  cancelled: '已停止',
} as const

export type TeamPhase = keyof typeof teamPhaseLabels
export type TeamTaskStatus = keyof typeof teamTaskLabels

export interface TeamTask {
  id: string
  role: string
  title: string
  brief: string
  depends_on: string[]
  status: TeamTaskStatus
  result?: string
  error?: string
}

export interface TeamProgress {
  phase: TeamPhase
  lead: string
  tasks: TeamTask[]
  summary?: string
}

function isKnownKey<T extends object>(value: unknown, dictionary: T): value is keyof T {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(dictionary, value)
}

function isTeamTask(value: unknown): value is TeamTask {
  if (!value || typeof value !== 'object') return false
  const task = value as Partial<TeamTask>
  return typeof task.id === 'string' && task.id.length > 0 && typeof task.title === 'string'
    && typeof task.role === 'string' && typeof task.brief === 'string' && isKnownKey(task.status, teamTaskLabels)
    && Array.isArray(task.depends_on) && task.depends_on.every((id) => typeof id === 'string')
    && (task.result == null || typeof task.result === 'string') && (task.error == null || typeof task.error === 'string')
}

export function readTeamProgress(data: Record<string, unknown>): TeamProgress | undefined {
  if (!isKnownKey(data.phase, teamPhaseLabels) || !Array.isArray(data.tasks) || !data.tasks.every(isTeamTask)) return
  if (new Set(data.tasks.map((task) => task.id)).size !== data.tasks.length) return
  return {
    phase: data.phase,
    lead: typeof data.lead === 'string' && data.lead.trim() ? data.lead : '团队负责人',
    tasks: data.tasks,
    summary: typeof data.summary === 'string' ? data.summary : undefined,
  }
}

export function finishTeamProgress(progress: TeamProgress, runStatus: string | undefined): TeamProgress {
  if (progress.phase === 'completed' || progress.phase === 'failed' || progress.phase === 'cancelled') return progress
  if (runStatus !== 'cancelled' && runStatus !== 'failed' && runStatus !== 'limit_exceeded') return progress
  const phase = runStatus === 'cancelled' ? 'cancelled' : 'failed'
  return {
    ...progress,
    phase,
    tasks: progress.tasks.map((task) => {
      if (task.status === 'running') return { ...task, status: phase }
      if (task.status === 'pending') return { ...task, status: phase === 'cancelled' ? 'cancelled' : 'skipped' }
      return task
    }),
  }
}
