// Workspace Sandbox API（项目级持久化开发工作空间：Project / Sandbox / Service / Preview）
import { createRequest } from '@/utils/request'

const request = createRequest('/api/v2')

export interface SandboxInfo {
  id: number
  generation: number
  desired_state: string
  observed_state: string
  failure_code?: string | null
  failure_message?: string | null
  cpu_request_millis: number
  cpu_limit_millis: number
  memory_request_bytes: number
  memory_limit_bytes: number
  disk_quota_bytes: number
  pid_limit: number
  created_at?: string | null
  last_observed_at?: string | null
}

export interface WorkspaceInfo {
  id: number
  project_id: number
  status: string
  volume_driver: string
  storage_quota_bytes: number
  resource_profile_id?: number | null
  last_active_at?: string | null
  sandbox?: SandboxInfo | null
  created_at?: string | null
}

export interface ProjectSummary {
  id: number
  name: string
  description?: string
  owner_id: number
  status: string
  role?: string
  created_at?: string | null
  updated_at?: string | null
  workspace?: WorkspaceInfo | null
  service_count?: number
}

export interface WorkspaceServiceInfo {
  id: number
  workspace_id: number
  sandbox_id?: number | null
  name: string
  command?: string
  port: number
  protocol: string
  working_directory: string
  health_check_type: string
  health_check_path: string
  health_check_timeout: number
  desired_state: string
  observed_state: string
  auto_restart: boolean
  auto_start_on_sandbox_start: boolean
  process_id?: string | null
  exit_code?: number | null
  last_started_at?: string | null
  last_exited_at?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface PreviewGrantInfo {
  id: string
  service_id: number
  sandbox_id: number
  port: number
  expires_at?: string | null
  revoked_at?: string | null
  created_at?: string | null
  /** 首次创建时返回；之后仅返回元数据 */
  token?: string
  url?: string
  fallback_url?: string
  /** PREVIEW_REQUIRE_LOGIN=true 时的短期登录凭证（前端拼到 iframe 地址） */
  access_token?: string
}

export interface ProjectWorkspaceDetail {
  project: { id: number; name: string; owner_id: number; status: string }
  role: string
  workspace: WorkspaceInfo
  services: WorkspaceServiceInfo[]
  preview_grants?: PreviewGrantInfo[]
}

export const sandboxApi = {
  listProjects: () => request.get<any, { items: ProjectSummary[] }>('/projects'),
  getProject: (projectId: number) =>
    request.get<any, ProjectSummary & { role?: string }>(`/projects/${projectId}`),
  createProject: (data: { name: string; description?: string; profile?: string }) =>
    request.post<any, ProjectSummary>('/projects', data),
  deleteProject: (projectId: number, purgeVolume = false) =>
    request.delete(`/projects/${projectId}`, { params: { purge_volume: purgeVolume } }),
  getProjectWorkspace: (projectId: number) =>
    request.get<any, ProjectWorkspaceDetail>(`/projects/${projectId}/workspace`),
  provisionProject: (projectId: number, profile?: string) =>
    request.post<any, { workspace: WorkspaceInfo; sandbox: SandboxInfo }>(
      `/projects/${projectId}/workspace/provision`, profile ? { profile } : {},
    ),
  getWorkspace: (workspaceId: number) =>
    request.get<any, { workspace: WorkspaceInfo; services: WorkspaceServiceInfo[] }>(
      `/workspaces/${workspaceId}`,
    ),
  getSandbox: (workspaceId: number) =>
    request.get<any, { sandbox: SandboxInfo | null }>(`/workspaces/${workspaceId}/sandbox`),
  startSandbox: (workspaceId: number) =>
    request.post<any, { workspace: WorkspaceInfo; sandbox: SandboxInfo }>(
      `/workspaces/${workspaceId}/sandbox/start`,
    ),
  stopSandbox: (workspaceId: number) =>
    request.post<any, { workspace: WorkspaceInfo; sandbox: SandboxInfo }>(
      `/workspaces/${workspaceId}/sandbox/stop`,
    ),
  restartSandbox: (workspaceId: number) =>
    request.post<any, { workspace: WorkspaceInfo; sandbox: SandboxInfo }>(
      `/workspaces/${workspaceId}/sandbox/restart`,
    ),
  listServices: (workspaceId: number) =>
    request.get<any, { items: WorkspaceServiceInfo[] }>(`/workspaces/${workspaceId}/services`),
  createService: (workspaceId: number, data: {
    name: string
    command: string
    port: number
    working_directory?: string
    health_check_path?: string
    health_check_timeout?: number
    auto_restart?: boolean
    auto_start_on_sandbox_start?: boolean
  }) => request.post<any, WorkspaceServiceInfo>(`/workspaces/${workspaceId}/services`, data),
  stopService: (workspaceId: number, serviceId: number) =>
    request.post<any, WorkspaceServiceInfo>(`/workspaces/${workspaceId}/services/${serviceId}/stop`),
  restartService: (workspaceId: number, serviceId: number) =>
    request.post<any, WorkspaceServiceInfo>(`/workspaces/${workspaceId}/services/${serviceId}/restart`),
  serviceLogs: (workspaceId: number, serviceId: number, tail = 200) =>
    request.get<any, { name: string; content: string; truncated: boolean }>(
      `/workspaces/${workspaceId}/services/${serviceId}/logs`, { params: { tail } },
    ),
  createPreviewGrant: (workspaceId: number, serviceId: number, ttlSeconds = 3600) =>
    request.post<any, PreviewGrantInfo>(
      `/workspaces/${workspaceId}/services/${serviceId}/preview-grants`,
      { ttl_seconds: ttlSeconds },
    ),
  revokePreviewGrant: (workspaceId: number, grantId: string) =>
    request.delete(`/workspaces/${workspaceId}/preview-grants/${grantId}`),
}
