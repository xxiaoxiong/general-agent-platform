import type { AuditEventType, AuditLog, AuditLogDetail, AuditSnapshot, AuditValue } from '@/types'

export const auditEventLabels: Record<AuditEventType | 'legacy', string> = {
  request: '请求记录', change: '数据变更', legacy: '历史记录',
}

export const auditModuleLabels: Record<string, string> = {
  auth: '认证', user: '用户与权限', agent: '智能体对话', document: '文档', model: '模型',
  knowledge: '知识库', workflow: '工作流', app: '应用', database: '数据库', prompt: '提示词',
  skill: '技能', mcp: 'MCP 服务', evaluation: '测评', evolution: '自进化', audit: '审计', asr: '语音识别',
}

const resourceLabels: Record<string, string> = {
  user: '用户', users: '用户', role: '角色', roles: '角色', permission: '权限', permissions: '权限',
  user_roles: '用户角色', role_permissions: '角色权限',
  conversation: '会话', conversations: '会话', agent_session: '会话', agent_sessions: '会话',
  agent: '智能体', agents: '智能体', agent_versions: '智能体版本', assistant: '助手', assistants: '助手',
  run: '任务', runs: '任务', agent_run: '任务', agent_runs: '任务', agent_approvals: '任务审批',
  agent_tool_executions: '工具执行', run_input_requests: '任务输入',
  document: '文档', documents: '文档', document_asset: '文档', document_assets: '文档',
  document_version: '文档版本', document_versions: '文档版本',
  document_profile: '文档规范', document_profiles: '文档规范', 'document-profiles': '文档规范',
  document_profile_versions: '文档规范版本',
  model: '模型', models: '模型', model_config: '模型配置', model_configs: '模型配置',
  provider: '模型供应商', model_provider: '模型供应商', model_providers: '模型供应商',
  kb: '知识库', knowledge: '知识库', knowledge_base: '知识库', knowledge_bases: '知识库',
  knowledge_document: '知识库文档', knowledge_documents: '知识库文档',
  workflow: '工作流', workflows: '工作流', workflow_execution: '工作流运行', workflow_executions: '工作流运行',
  app: '应用', apps: '应用', app_instance: '应用实例', app_instances: '应用实例', app_template: '应用模板', app_templates: '应用模板',
  database: '数据库连接', databases: '数据库连接', database_connection: '数据库连接', database_connections: '数据库连接',
  prompt: '提示词', prompts: '提示词', prompt_template: '提示词模板', prompt_templates: '提示词模板',
  prompt_template_versions: '提示词版本',
  skill: '技能', skills: '技能', mcp: 'MCP 服务', mcp_server: 'MCP 服务', mcp_servers: 'MCP 服务',
  evaluation: '测评', evaluation_dataset: '测评数据集', eval_datasets: '测评数据集', eval_dataset_items: '测评样本',
  evaluation_run: '测评任务', eval_runs: '测评任务', evolution_reports: '自进化报告', evolution_change_log: '自进化变更',
  file: '文件', files: '文件', workspace_files: '工作区文件', auth: '认证', audit: '审计日志', asr: '语音识别',
}

const actionLabels: Record<string, string> = {
  login: '登录', login_failed: '登录失败', logout: '退出登录', refresh: '刷新会话',
  change_password: '修改密码', get_me: '查看账户信息', rename_conversation: '重命名会话',
  pin_conversation: '置顶会话', unpin_conversation: '取消置顶会话', update_conversation: '更新会话',
  submit_run_input: '提交任务输入', resume_run: '恢复任务', retry_run: '重试任务', cancel_run: '取消任务',
  execute_workflow: '运行工作流', get_conversation_messages: '查看对话消息',
  list_audit_logs: '查询审计日志', get_audit_log: '查看审计详情',
  upload_file: '上传文件', download_file: '下载文件', preview_file: '预览文件',
}

const fieldLabels: Record<string, string> = {
  title: '标题', name: '名称', description: '描述', username: '用户名', email: '邮箱',
  is_active: '启用状态', status: '状态', title_edited: '手动命名', pinned_at: '置顶时间',
  is_pinned: '置顶状态', version: '版本', version_no: '版本号', current_version: '当前版本',
  published_version: '已发布版本', resource_id: '资源 ID', definition: '定义',
  full_name: '姓名', department: '部门', is_superuser: '管理员权限', roles: '角色',
  password: '密码', hashed_password: '密码', api_key: 'API 密钥', access_token: '访问令牌',
}

const verbs: Record<string, string> = {
  create: '创建', update: '更新', delete: '删除', list: '查看列表 · ', get: '查看',
  publish: '发布', upload: '上传', download: '下载', preview: '预览', export: '导出',
  test: '测试', execute: '执行', approve: '批准', reject: '拒绝',
}

function lookup(values: Record<string, string>, key: string) {
  return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : undefined
}

export function auditAction(action: string, type?: string | null) {
  const known = lookup(actionLabels, action)
  if (known) return known
  const separator = action.indexOf('_')
  if (separator < 0) {
    const verb = lookup(verbs, action)
    return verb ? verb + (type ? auditResource(type) : '') : action
  }
  const verb = lookup(verbs, action.slice(0, separator))
  const resource = lookup(resourceLabels, action.slice(separator + 1))
  return verb && resource ? verb + resource : action
}

export function auditModule(module: string) { return lookup(auditModuleLabels, module) || module || '—' }
export function auditResource(type?: string | null) { return type ? lookup(resourceLabels, type) || type : '—' }
export function auditField(field: string) { return lookup(fieldLabels, field) || field }

export function auditActor(row: AuditLog) {
  if (row.username) return row.username
  if (row.user_id != null) return `用户 #${row.user_id}`
  return row.event_type === 'request' ? '未登录' : '未记录'
}

export function auditStatus(status: string) {
  if (status === 'success') return { text: '成功', tone: 'success' }
  if (status === 'denied') return { text: '拒绝', tone: 'warning' }
  if (status === 'failed') return { text: '失败', tone: 'danger' }
  return { text: status || '未知', tone: 'neutral' }
}

function containsKey(snapshot: AuditSnapshot, key: string) {
  return Object.prototype.hasOwnProperty.call(snapshot, key)
}

function equalValue(left?: AuditValue, right?: AuditValue): boolean {
  if (Object.is(left, right)) return true
  if (left === null || right === null || typeof left !== 'object' || typeof right !== 'object') return false
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((item, index) => equalValue(item, right[index]))
  }
  const keys = Object.keys(left)
  return keys.length === Object.keys(right).length && keys.every((key) => containsKey(right, key) && equalValue(left[key], right[key]))
}

export function auditFields(log: AuditLogDetail, onlyChanged: boolean) {
  const before = log.before_data ?? {}
  const after = log.after_data ?? {}
  const reported = new Set(log.changed_fields ?? [])
  const fields = [...new Set([...reported, ...Object.keys(before), ...Object.keys(after)])]
  return fields.map((field) => {
    const from = containsKey(before, field) ? before[field] : undefined
    const to = containsKey(after, field) ? after[field] : undefined
    const changed = reported.has(field) || !equalValue(from, to)
    return { field, from, to, changed }
  }).filter((item) => !onlyChanged || item.changed)
}

export function formatAuditValue(value?: AuditValue): string {
  if (typeof value === 'string') return value || '""'
  return value === undefined ? '未记录' : JSON.stringify(value, null, 2)
}
