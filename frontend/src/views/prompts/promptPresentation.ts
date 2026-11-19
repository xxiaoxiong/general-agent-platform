export const categories = [
  { value: 'general', label: '通用' },
  { value: 'analysis', label: '分析' },
  { value: 'writing', label: '写作' },
  { value: 'coding', label: '编程' },
  { value: 'summary', label: '总结' },
  { value: 'translation', label: '翻译' },
  { value: 'qa', label: '问答' },
  { value: 'other', label: '其他' },
]

export interface PromptTemplate {
  id: number
  name: string
  description?: string
  category: string
  content: string
  variables: string[]
  is_public: boolean
  owner_id: number
  version: number
  tags?: string
  usage_count: number
}

export interface PromptVersion {
  id: number
  version: number
  content: string
  change_note?: string
  created_at: string
}

export function getCategoryLabel(value: string) {
  return categories.find(category => category.value === value)?.label || value
}

export function detectVariables(content: string) {
  return [...new Set(Array.from(content.matchAll(/\{\{([\p{L}\p{N}_]+)\}\}/gu), match => match[1]))]
}
