// 助手（Agent）选项 API（/api/v2/assistants）：绑定选择用
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')

export interface AgentOption {
  id: number
  name: string
}

export const agentOptionApi = {
  list: async (): Promise<AgentOption[]> => {
    const resp = await request.get<any, { items?: AgentOption[] } | AgentOption[]>('/assistants', {
      params: { page: 1, page_size: 100 },
    })
    const items = Array.isArray(resp) ? resp : (resp.items || [])
    return items.map((a) => ({ id: a.id, name: (a as any).name || `Agent ${a.id}` }))
  },
}
