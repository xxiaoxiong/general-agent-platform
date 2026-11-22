/** 项目交付与验收事件的前端呈现契约。 */

export type ProjectPhase = 'planning' | 'building' | 'checking' | 'repairing' | 'packaging'

export interface ProjectProgress {
  phase: ProjectPhase
  detail?: string
  title?: string
  files?: { written: number; total: number }
}

export interface ProjectCheck {
  id: string
  label: string
  status: 'passed' | 'failed' | 'skipped'
  detail?: string
}

export interface ProjectDelivery {
  title: string
  filename: string
  download_url: string
  file_count: number
  size: number
  state: 'passed' | 'failed'
  checks: ProjectCheck[]
  issues?: { path: string; message: string }[]
  verification_scope?: string
}

export const projectPhaseLabels: Record<ProjectPhase, string> = {
  planning: '正在确定交付范围',
  building: '正在实现项目',
  checking: '正在验收项目',
  repairing: '正在修复验收问题',
  packaging: '正在打包项目',
}

export const projectCheckLabels: Record<ProjectCheck['status'], string> = {
  passed: '通过',
  failed: '未通过',
  skipped: '未执行',
}

export const PROJECT_DOWNLOAD_PATH = /^\/api\/v2\/artifacts\/([a-z0-9_-]{1,512})$/i
const ZIP_NAME = /^[^/\\\u0000-\u001f]{1,180}\.zip$/i

export function readProjectProgress(data: Record<string, unknown>): ProjectProgress | undefined {
  const phase = data.phase
  if (typeof phase !== 'string' || !Object.prototype.hasOwnProperty.call(projectPhaseLabels, phase)) return undefined
  const written = data.completed_files
  const total = data.total_files
  const files = typeof written === 'number' && Number.isSafeInteger(written) && written >= 0
    && typeof total === 'number' && Number.isSafeInteger(total) && total > 0 && written <= total
    ? { written, total } : undefined
  return {
    phase: phase as ProjectPhase,
    detail: typeof data.detail === 'string' ? data.detail : undefined,
    title: typeof data.title === 'string' ? data.title : undefined,
    files,
  }
}

function isProjectCheck(value: unknown): value is ProjectCheck {
  if (!value || typeof value !== 'object') return false
  const { id, label, status, detail } = value as Record<string, unknown>
  return typeof id === 'string' && Boolean(id.trim())
    && typeof label === 'string' && Boolean(label.trim())
    && (status === 'passed' || status === 'failed' || status === 'skipped')
    && (detail == null || typeof detail === 'string')
}

export function readProjectDelivery(data: Record<string, unknown>): ProjectDelivery | undefined {
  const { title, filename, download_url, file_count, size, state, checks, issues, verification_scope } = data
  if (typeof title !== 'string' || !title.trim() || typeof filename !== 'string' || !ZIP_NAME.test(filename)
    || typeof download_url !== 'string' || !PROJECT_DOWNLOAD_PATH.test(download_url)
    || typeof file_count !== 'number' || !Number.isSafeInteger(file_count) || file_count <= 0
    || typeof size !== 'number' || !Number.isSafeInteger(size) || size <= 0
    || (state !== 'passed' && state !== 'failed') || !Array.isArray(checks) || !checks.every(isProjectCheck)
    || new Set(checks.map((check) => check.id)).size !== checks.length) return undefined
  const passed = state === 'passed' && checks.some((check) => check.status === 'passed')
    && checks.every((check) => check.status !== 'failed')
  const findings = Array.isArray(issues) ? issues.slice(0, 60).flatMap((item: unknown) => {
    if (!item || typeof item !== 'object') return []
    const { path, message } = item as Record<string, unknown>
    return typeof path === 'string' && path.trim() && typeof message === 'string' && message.trim()
      ? [{ path: path.slice(0, 240), message: message.slice(0, 500) }] : []
  }) : []
  return {
    title, filename, download_url, file_count, size, checks,
    state: passed ? 'passed' : 'failed',
    verification_scope: typeof verification_scope === 'string' ? verification_scope : undefined,
    ...(findings.length ? { issues: findings } : {}),
  }
}

export function describeProjectScope(scope: string | undefined, checks: ProjectCheck[] = []): string {
  switch (scope) {
    case 'static': return '静态检查，未启动项目'
    case 'sandbox': {
      const scenarios = checks.filter(check => check.id.startsWith('runtime.browser.flow.'))
      if (!scenarios.length) return '隔离环境中的运行检查'
      const passed = scenarios.filter(check => check.status === 'passed').length
      return `浏览器操作场景 ${passed} / ${scenarios.length} 通过 · 隔离运行`
    }
    default: return scope || '查看明细了解已执行的检查'
  }
}
