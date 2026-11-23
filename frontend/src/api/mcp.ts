// MCP 服务管理 API（/api/v2/mcp）
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')

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
