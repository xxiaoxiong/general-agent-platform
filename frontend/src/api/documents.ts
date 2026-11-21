import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2', { inlineErrors: true })

export type DocumentMode = 'auto' | 'chat' | 'document_analyze' | 'profile_extract' | 'document_generate' | 'document_edit'
export type ProfileVisibility = 'private' | 'organization'
export type ProfileStatus = 'draft' | 'published' | 'archived'

export interface ProfileSection {
  id: string
  title: string
  order: number
  level?: number
  required: boolean
  purpose: string
  required_fields: string[]
  writing_guidance: string
  source_requirements: string[]
}

export interface ProfileQualityRule {
  id: string
  severity: 'error' | 'warning' | 'info'
  description: string
}

export interface ProfileInput {
  id: string
  label: string
  type: 'text' | 'date' | 'number' | 'single_choice' | 'multiple_choice'
  required: boolean
  description: string
  options: string[]
}

export interface ProfileFormatting {
  page_size: 'A4' | 'Letter'
  margins_cm: { top: number; right: number; bottom: number; left: number }
  heading_hierarchy: boolean
  tables_allowed: boolean
  font_family: string
  body_font_size: number
  line_spacing: number
  header: string
  footer: string
  page_numbers: boolean
  section_page_break: boolean
}

export interface ProfileDefinition {
  schema_version: number
  document_type: string
  name: string
  description: string
  language: string
  audience: string
  tone: string
  output_formats: string[]
  sections: ProfileSection[]
  required_inputs: ProfileInput[]
  global_rules: string[]
  formatting: ProfileFormatting
  quality_rules: ProfileQualityRule[]
  uncertainties?: string[]
}

export interface ProfileVersion {
  id: number
  version_no: number
  definition: ProfileDefinition
  source_summary?: string | null
  source_metadata?: Array<{ label?: string; name?: string; kind?: string; detail?: string; locator?: string }> | null
  extraction_confidence?: number | null
  change_summary?: string | null
  created_at: string | null
  created_by_run_id?: string | null
}

export interface DocumentProfile extends Omit<ProfileVersion, 'version_no'> {
  name: string
  description: string
  document_type: string
  owner_id: number
  visibility: ProfileVisibility
  status: ProfileStatus
  can_manage: boolean
  current_version: number
  published_version: number | null
  updated_at: string | null
}

export interface DocumentAsset {
  id: number
  title: string
  document_type: string
  current_version: number
  status: string
  profile_id: number | null
  session_id: number | null
  created_by_run_id: string | null
  created_at: string | null
  updated_at: string | null
}

export interface DocumentReviewChange {
  id: string
  kind: 'text' | 'image' | 'table' | 'other'
  title?: string
  section_id?: string
  section_title?: string
  block_id?: string
  scope?: 'section' | 'block'
  action?: string
  before?: string | null
  after?: string | null
  before_image_url?: string | null
  after_image_url?: string | null
}

export interface DocumentEditReview {
  ordinal: number
  source_name?: string
  base_version?: number | null
  summary?: string
  warnings?: string[]
  changes: DocumentReviewChange[]
  context_url?: string
}

export interface DocumentReviewBlock {
  id: string
  type: 'paragraph' | 'list' | 'table' | 'image'
  text?: string
  style?: string
  items?: string[]
  ordered?: boolean
  rows?: string[][]
  header?: boolean
  image_url?: string | null
  change_ids?: string[]
}

export interface DocumentReviewSection {
  id: string
  title: string
  level: number
  blocks: DocumentReviewBlock[]
}

export interface DocumentReviewGroup {
  id: string
  title: string
  path: string[]
  mode: 'edits' | 'rewrite'
  change_ids: string[]
  before: DocumentReviewSection[]
  after: DocumentReviewSection[]
}

export interface DocumentReviewContext {
  request: string
  groups: DocumentReviewGroup[]
  changes: DocumentReviewChange[]
  decision?: DocumentReviewDecision
}

export interface DocumentReviewDecision {
  accepted_change_ids: string[]
  rejected_change_ids: string[]
}

export interface DocumentVersion {
  id: number
  document_id: number
  version_no: number
  filename: string
  mime_type: string
  size?: number
  content_hash: string
  profile_id: number | null
  profile_version_no: number | null
  created_by_run_id: string | null
  change_summary?: string | null
  validation_report: {
    passed?: boolean
    issues?: Array<{ code: string; severity: 'warning' | 'error' | 'info'; message: string; section_id?: string }>
  }
  version_diff?: {
    base_version?: number
    office_changes?: DocumentReviewChange[]
    sections?: Array<{
      section_id: string
      title_before?: string | null
      title_after?: string | null
      blocks: Array<{ block_id: string; before?: DocumentContentBlock | null; after?: DocumentContentBlock | null }>
    }>
  } | null
  created_at: string | null
}

export interface DocumentArtifact {
  document_id: number
  document_version_id: number
  version_no: number
  title: string
  filename: string
  mime_type: string
  size: number
  profile_id?: number | null
  profile_version?: number | null
  profile_name?: string | null
}

export type DocumentContentBlock = { id?: string | null } & (
  | { type: 'paragraph'; text: string; style?: 'normal' | 'quote' | 'caption' }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'table'; rows: string[][]; header: boolean }
)

export interface DocumentDraft {
  title: string
  subtitle?: string | null
  abstract?: string | null
  metadata: Record<string, unknown>
  sections: Array<{ id: string; title: string; order: number; level: number; blocks: DocumentContentBlock[] }>
  sources: Array<{ id: string; label: string; kind: string; detail?: string | null; locator?: string | null }>
  field_values: Record<string, unknown>
  missing_fields: string[]
}

export interface DocumentTask {
  id: string
  session_id: number | null
  task_type: string
  status: string
  created_at: string | null
  finished_at?: string | null
}

export interface InputFieldSchema {
  type?: string
  title?: string
  description?: string
  format?: string
  enum?: Array<string | number | boolean>
  items?: { enum?: Array<string | number | boolean> }
  optionLabels?: Record<string, string>
  maxLength?: number
  minLength?: number
  minimum?: number
  maximum?: number
  default?: unknown
}

export interface InputRequest {
  request_id: string
  prompt: string
  input_schema: { properties?: Record<string, InputFieldSchema>; required?: string[] }
  required_fields?: string[]
}

interface Page<T> { items: T[]; total: number }
interface PageParams { page?: number; page_size?: number }
interface ProfilePayload { definition: ProfileDefinition; visibility?: ProfileVisibility; change_summary?: string; expected_version?: number }

const profilesPath = '/document-profiles'
const assetsPath = '/documents'

export const documentProfilesApi = {
  list: (params?: PageParams & { status?: ProfileStatus }) => request.get<unknown, Page<DocumentProfile>>(profilesPath, { params }),
  get: (id: number) => request.get<unknown, DocumentProfile>(`${profilesPath}/${id}`),
  create: (payload: ProfilePayload) => request.post<unknown, DocumentProfile>(profilesPath, payload),
  update: (id: number, payload: ProfilePayload) => request.put<unknown, DocumentProfile>(`${profilesPath}/${id}`, payload),
  versions: (id: number) => request.get<unknown, Page<ProfileVersion>>(`${profilesPath}/${id}/versions`),
  publish: (id: number, expectedVersion: number) =>
    request.post<unknown, DocumentProfile>(`${profilesPath}/${id}/publish`, { expected_version: expectedVersion }),
  archive: (id: number) => request.post<unknown, DocumentProfile>(`${profilesPath}/${id}/archive`),
  remove: (id: number) => request.delete(`${profilesPath}/${id}`),
}

export const documentsApi = {
  list: (params?: PageParams) => request.get<unknown, Page<DocumentAsset>>(assetsPath, { params }),
  get: (id: number) => request.get<unknown, DocumentAsset>(`${assetsPath}/${id}`),
  versions: (id: number) => request.get<unknown, Page<DocumentVersion>>(`${assetsPath}/${id}/versions`),
  preview: (id: number, version: number) => request.get<unknown, DocumentDraft>(`${assetsPath}/${id}/versions/${version}/preview`),
  previewPdf: (id: number, version: number, signal?: AbortSignal) =>
    request.get<unknown, Blob>(`${assetsPath}/${id}/versions/${version}/preview.pdf`, { responseType: 'blob', signal }),
  download: (id: number, version: number) => request.get<unknown, Blob>(`${assetsPath}/${id}/versions/${version}/download`, { responseType: 'blob' }),
  tasks: (params?: PageParams) => request.get<unknown, Page<DocumentTask>>(`${assetsPath}/tasks`, { params }),
  remove: (id: number) => request.delete(`${assetsPath}/${id}`),
}

export async function getDocumentReviewImage(path: string, signal: AbortSignal): Promise<Blob> {
  // 只向本平台的 Run 路由发送访问令牌。
  if (!/^\/api\/v2\/runs\/[a-z0-9_-]+\/[a-z0-9_/-]+$/i.test(path)) {
    throw new Error('图片预览地址无效')
  }
  const blob = await request.get<unknown, Blob>(path.slice('/api/v2'.length), { signal, responseType: 'blob' })
  if (!/^image\/(png|jpe?g|gif|webp|bmp)$/i.test(blob.type)) {
    throw new Error('图片格式无法预览')
  }
  return blob
}

export async function getDocumentReviewContext(path: string, signal: AbortSignal): Promise<DocumentReviewContext> {
  if (!/^\/api\/v2\/runs\/[a-z0-9_-]+\/document-review\/context$/i.test(path)) {
    throw new Error('文档审阅地址无效')
  }
  return request.get<unknown, DocumentReviewContext>(path.slice('/api/v2'.length), { signal })
}

export async function downloadDocument(id: number, version: number, filename: string) {
  const blob = await documentsApi.download(id, version)
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function documentError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    const messages = detail.map((issue) => issue?.msg).filter((message): message is string => typeof message === 'string')
    if (messages.length) return messages.join('，')
  }
  return fallback
}
