// 扩展治理 API：技能管理 + MCP 管理（/api/v2）
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')
const requestV1 = createRequest('/api/v1')

// ── 技能（Skill） ─────────────────────────────────────────────

export interface SkillItem {
  id: number | null
  name: string
  description: string
  version: string
  body: string
  enabled: boolean
  agent_ids: number[] | null // null=全局注入；[] = 不注入；[1,2]=仅指定 Agent
  scope: string
  source: string
  managed: boolean
}

export const skillApi = {
  list: () => request.get<any, { items: SkillItem[] }>('/skills'),
  create: (data: Partial<SkillItem>) => request.post<any, SkillItem>('/skills', data),
  update: (id: number, data: Partial<SkillItem>) => request.put<any, SkillItem>(`/skills/${id}`, data),
  toggle: (id: number) => request.post<any, SkillItem>(`/skills/${id}/toggle`),
  remove: (id: number) => request.delete<any, { ok: boolean }>(`/skills/${id}`),
}

// ── MCP 服务 ─────────────────────────────────────────────────

export interface McpToolInfo {
  name: string
  description: string
  inputSchema: Record<string, unknown>
}

export interface McpToolSetting {
  enabled?: boolean
  requires_approval?: boolean
  side_effect_class?: 'read_only' | 'non_idempotent'
}

export interface McpServerItem {
  id: number
  name: string
  description: string
  transport: 'stdio' | 'streamable_http'
  command: string | null
  args: string[]
  env: Record<string, string>
  url: string | null
  headers: Record<string, string>
  enabled: boolean
  require_approval: boolean
  side_effect_class: 'read_only' | 'non_idempotent'
  tool_settings: Record<string, McpToolSetting>
  discovered_tools: McpToolInfo[]
  agent_ids: number[]
  last_test_at: string | null
  last_test_error: string | null
  created_at: string | null
  updated_at: string | null
}

export interface McpTestResult {
  ok: boolean
  error: string | null
  server_info: Record<string, unknown> | null
  tools: McpToolInfo[]
  saved_tools: number
}

export const mcpApi = {
  list: () => request.get<any, { items: McpServerItem[] }>('/mcp/servers'),
  create: (data: Partial<McpServerItem>) => request.post<any, McpServerItem>('/mcp/servers', data),
  update: (id: number, data: Partial<McpServerItem>) => request.put<any, McpServerItem>(`/mcp/servers/${id}`, data),
  test: (id: number) => request.post<any, McpTestResult>(`/mcp/servers/${id}/test`),
  remove: (id: number) => request.delete<any, { ok: boolean }>(`/mcp/servers/${id}`),
}

// ── Agent 选项（绑定选择用） ─────────────────────────────────

export interface AgentOption {
  id: number
  name: string
}

export const agentOptionApi = {
  list: async (): Promise<AgentOption[]> => {
    const resp = await requestV1.get<any, { items?: AgentOption[] } | AgentOption[]>('/agents', {
      params: { page: 1, page_size: 100 },
    })
    const items = Array.isArray(resp) ? resp : (resp.items || [])
    return items.map((a) => ({ id: a.id, name: (a as any).name || `Agent ${a.id}` }))
  },
}
