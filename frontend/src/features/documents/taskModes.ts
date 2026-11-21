import type { DocumentMode } from '../../api/documents'

export interface DocumentTaskSelection {
  mode: DocumentMode
  profileId?: number
  documentId?: number
  version?: number
  title?: string
}

export const documentTaskModes: Array<{ value: DocumentMode; label: string }> = [
  { value: 'auto', label: '自动识别' },
  { value: 'chat', label: '普通对话' },
  { value: 'document_analyze', label: '分析文档' },
  { value: 'profile_extract', label: '提炼规范' },
  { value: 'document_generate', label: '生成 Word' },
  { value: 'document_edit', label: '修改文档' },
]

export const documentTaskLabels: Record<string, string> = {
  general_chat: '普通对话',
  document_analyze: '分析文档',
  profile_extract: '提炼规范',
  document_generate: '生成 Word',
  document_edit: '修改文档',
}

export function documentTaskPayload(selection: DocumentTaskSelection) {
  return {
    task_mode: selection.mode === 'auto' ? undefined : selection.mode,
    document_profile_id: selection.mode === 'document_generate' ? selection.profileId : undefined,
    document_id: selection.mode === 'document_edit' ? selection.documentId : undefined,
    task_options: selection.mode === 'document_edit' && selection.documentId && selection.version
      ? { document_version_no: selection.version }
      : undefined,
  }
}
