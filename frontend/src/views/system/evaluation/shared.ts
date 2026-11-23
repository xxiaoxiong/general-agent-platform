/**
 * 「Agent 测评 / 自进化」面板公共工具函数（自 Evaluation.vue 拆分平移，行为不变）。
 */
import dayjs from 'dayjs'
import { ElMessageBox } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'

export function pct(v: any) { return v == null ? '-' : `${(v * 100).toFixed(1)}%` }
export function pctNum(v: any) { return v == null ? 0 : Math.round(v * 100) }
export const complexityText: Record<string, string> = {
  L1: 'L1 简单', L2: 'L2 中等', L3: 'L3 复杂', L4: 'L4 困难',
}
export function complexityTagType(c: string) {
  return (({ L1: 'success', L2: 'info', L3: 'warning', L4: 'danger' } as any)[c] || 'info')
}
export const taskTypeText: Record<string, string> = {
  direct_qa: '直接问答', multi_step_reasoning: '多步推理', tool_use: '工具使用',
  instruction_following: '指令遵循', multi_turn: '多轮任务', adversarial: '对抗鲁棒',
  project_build: '工程构建', long_document: '长文档生成修改', complex_math: '复杂数学',
  deep_research: '深度研究',
}
export function runStatusType(s: string) {
  return s === 'completed' ? 'success' : s === 'failed' ? 'danger'
    : s === 'running' ? 'warning' : 'info'
}
export function runStatusText(s: string) {
  return ({ pending: '待执行', running: '执行中', completed: '已完成',
    failed: '失败', cancelled: '已取消', cancel_requested: '取消中' } as any)[s] || s
}
export function itemStatusType(s: string) {
  return s === 'passed' ? 'success' : s === 'failed' ? 'danger'
    : s === 'error' ? 'danger' : s === 'running' ? 'warning' : 'info'
}
export function itemStatusText(s: string) {
  return ({ pending: '待执行', running: '执行中', passed: '通过',
    failed: '未通过', error: '异常', skipped: '跳过' } as any)[s] || s
}
export function formatDate(d: string | null) { return d ? dayjs(d).format('YYYY-MM-DD HH:mm') : '-' }
export async function confirmAction(message: string, title: string, destructive = false) {
  const confirmation = destructive
    ? confirmDestructive(message, title)
    : ElMessageBox.confirm(message, title, { type: 'warning', confirmButtonText: '确认', cancelButtonText: '取消' })
  return confirmation.then(() => true, () => false)
}
