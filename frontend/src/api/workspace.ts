// 工作区 API（/api/v2/workspace、/api/v2/capabilities）
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')

export interface Assistant {
  id: number
  name: string
  description: string
  icon: string
  system_prompt: string
  capability_ids: string[]
  tools: any[]
  model_config_id: number | null
  knowledge_base_id: number | null
  database_connection_id: number | null
  is_default: boolean
  is_active: boolean
  version?: { id: number; version_no: number; published_at: string | null }
}

export const workspaceApi = {
  bootstrap: () =>
    request.get<any, {
      user: { id: number; username: string; is_superuser: boolean }
      default_assistant: Assistant
      capabilities: any[]
      workspace: Record<string, any>
      sandbox?: { enabled: boolean; preview_domain: string; default_profile: string }
    }>('/workspace/bootstrap'),
  capabilities: () => request.get<any, { items: any[] }>('/capabilities'),
}
