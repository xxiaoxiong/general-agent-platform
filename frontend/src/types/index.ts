export interface User {
  id: number
  username: string
  email: string
  full_name?: string
  avatar?: string
  is_active: boolean
  is_superuser: boolean
  department?: string
  phone?: string
  created_at: string
}

export interface Role {
  id: number
  name: string
  display_name: string
  description?: string
  is_system: boolean
}

export interface ModelProvider {
  id: number
  name: string
  display_name: string
  provider_type: string
  base_url: string
  is_active: boolean
  is_default: boolean
  description?: string
  logo_url?: string
  created_at: string
}

export interface ModelConfig {
  id: number
  provider_id: number
  name: string
  display_name: string
  model_type: string
  context_length: number
  max_tokens: number
  input_price: number
  output_price: number
  is_active: boolean
  supports_streaming: boolean
  supports_function_call: boolean
  supports_vision: boolean
  description?: string
  created_at: string
}

export interface KnowledgeBase {
  id: number
  name: string
  description?: string
  embedding_model?: string
  chunk_size: number
  chunk_overlap: number
  chunk_size_tokens?: number
  chunk_overlap_tokens?: number
  is_active: boolean
  owner_id: number
  doc_count: number
  vector_count: number
  indexed_chunk_count?: number
  failed_doc_count?: number
  project_id?: number | null
  visibility?: 'private' | 'project' | 'organization'
  status?: string
  active_index_generation_id?: number | null
  embedding_dimension?: number
  parser_profile?: 'auto' | 'docling' | 'native'
  last_ingested_at?: string | null
  created_at: string
}

export interface KnowledgeDocument {
  id: number
  knowledge_base_id: number
  filename: string
  file_type: string
  file_size: number
  status: 'pending' | 'queued' | 'processing' | 'ready' | 'partial' | 'failed'
    | 'deleting' | 'deleted' | 'migration_required' | 'done'
  chunk_count: number
  error_code?: string | null
  error_msg?: string
  mime_type?: string
  page_count?: number
  sheet_count?: number
  slide_count?: number
  element_count?: number
  parse_stats?: Record<string, any> | null
  warnings?: string[]
  active_version_id?: number | null
  created_at: string
  updated_at?: string
}

export interface IngestionJob {
  id: number
  knowledge_base_id: number
  document_id?: number | null
  document_version_id?: number | null
  job_type: string
  stage: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  progress: number
  attempt: number
  max_attempts: number
  error_code?: string | null
  error_message?: string | null
  created_at?: string
  started_at?: string | null
  finished_at?: string | null
}

export interface ParsedElement {
  element_id: string
  type: string
  text: string
  heading_path: string[]
  provenance: {
    page?: number
    page_end?: number
    sheet_name?: string
    row_start?: number
    row_end?: number
    slide_number?: number
    table_index?: number
  }
  level?: number
  table?: { headers: string[]; rows: string[][]; start_row?: number; merged_cells?: string[] } | null
  ocr_score?: number | null
}

export interface ParsedPreview {
  document_id: number
  filename: string
  title: string
  source_type: string
  parser_name: string
  parser_version: string
  parse_stats: Record<string, any>
  warnings: string[]
  total_elements: number
  elements: ParsedElement[]
}

export interface KnowledgeChunk {
  chunk_id: string
  document_id: number
  document_version_id: number
  chunk_index: number
  content: string
  token_count: number
  is_parent: boolean
  parent_chunk_id?: string | null
  heading_path: string[]
  element_type?: string | null
  page_start?: number | null
  page_end?: number | null
  sheet_name?: string | null
  row_start?: number | null
  row_end?: number | null
  slide_number?: number | null
  table_index?: number | null
  content_hash: string
}

export interface ChunkPage {
  total: number
  items: KnowledgeChunk[]
}

export interface KnowledgeCitation {
  knowledge_base_id: number
  knowledge_base_name?: string
  document_id: number
  document_version_id?: number
  filename: string
  title?: string
  source_type?: string
  page_start?: number | null
  page_end?: number | null
  heading_path?: string[]
  sheet_name?: string | null
  row_start?: number | null
  row_end?: number | null
  slide_number?: number | null
  table_index?: number | null
  chunk_id: string
  legacy?: boolean
}

export interface KnowledgeSearchResult {
  content: string
  context?: string
  score: number
  document_id: number
  filename: string
  chunk_index: number
  chunk_id?: string
  signals?: Record<string, number | null>
  citation?: KnowledgeCitation
  metadata?: Record<string, any>
}

export interface KnowledgeSearchResponse {
  results: KnowledgeSearchResult[]
  total: number
  query: string
  mode: string
  retrieval_id?: string
  timings: Record<string, number>
  candidates: Record<string, number>
  notes: string[]
  legacy_used: boolean
}

export interface KnowledgeEvaluation {
  id: number
  knowledge_base_id: number
  name: string
  status: string
  retrieval_mode: string
  embedding_model?: string
  metrics?: Record<string, any> | null
  details?: { cases?: any[] } | null
  error_msg?: string | null
  created_at: string
  finished_at?: string | null
}

export interface Workflow {
  id: number
  name: string
  description?: string
  definition: Record<string, any>
  is_active: boolean
  is_published: boolean
  version: number
  owner_id: number
  trigger_type: string
  category?: string
  icon?: string
  created_at: string
  updated_at: string
}

export interface WorkflowExecution {
  id: number
  workflow_id: number
  status: 'pending' | 'running' | 'success' | 'failed'
  input_data?: Record<string, any>
  output_data?: Record<string, any>
  node_results?: Record<string, any>
  error_msg?: string
  duration_ms?: number
  created_at: string
}

export interface AppTemplate {
  id: number
  name: string
  display_name: string
  description?: string
  category: string
  icon?: string
  cover_image?: string
  app_type: string
  is_builtin: boolean
  is_active: boolean
  sort_order: number
  tags?: string
  usage_count: number
  created_at: string
}

export interface AppInstance {
  id: number
  template_id: number
  name: string
  description?: string
  config?: Record<string, any>
  knowledge_base_id?: number
  model_config_id?: number
  workflow_id?: number
  owner_id: number
  is_active: boolean
  is_public: boolean
  access_token?: string
  created_at: string
}

export type AuditEventType = 'request' | 'change'
export type AuditValue = string | number | boolean | null | AuditValue[] | { [key: string]: AuditValue }
export type AuditSnapshot = Record<string, AuditValue>

export interface AuditLog {
  id: number
  user_id?: number | null
  username?: string | null
  action: string
  module: string
  resource_type?: string | null
  resource_id?: string | null
  ip_address?: string | null
  status: string
  duration_ms?: number | null
  created_at: string
  event_type?: AuditEventType | null
  request_id?: string | null
  http_method?: string | null
  request_path?: string | null
  status_code?: number | null
  changed_fields?: string[] | null
}

export interface AuditLogDetail extends AuditLog {
  before_data?: AuditSnapshot | null
  after_data?: AuditSnapshot | null
  detail?: AuditSnapshot | null
  error_msg?: string | null
  user_agent?: string | null
}

export interface AuditLogQuery {
  page: number
  page_size: number
  username?: string
  action?: string
  module?: string
  status?: string
  event_type?: AuditEventType | 'legacy'
  resource_type?: string
  resource_id?: string
  request_id?: string
  start_time?: string
  end_time?: string
}

export interface PageResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp?: number
}
