// 技能（Skill）管理 API（/api/v2/skills）
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')

export interface SkillItem {
  id: number | null
  name: string
  description: string
  version: string
  body: string
  // 附属文件（标准 Skill 形态）：{相对 POSIX 路径: 文本内容}
  files?: Record<string, string>
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
