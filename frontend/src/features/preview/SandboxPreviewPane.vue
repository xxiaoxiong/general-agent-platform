<template>
  <section class="sandbox-pane" aria-label="项目页面预览">
    <div v-if="!projectId" class="sp-empty">
      <Globe :size="20" />
      <p>当前会话未绑定项目</p>
      <span>在顶部选择一个项目后，Agent 启动的服务会在这里以真实页面展示。</span>
    </div>

    <template v-else>
      <div v-if="sandboxStateText" class="sp-sandbox-bar" :data-tone="sandboxTone">
        <span class="sp-dot" :data-tone="sandboxTone"></span>
        <span class="sp-sandbox-text">{{ sandboxStateText }}</span>
        <button v-if="canStartSandbox" type="button" class="sp-btn" :disabled="starting" @click="startEnvironment(false)">
          <LoaderCircle v-if="starting" class="sp-spin" :size="13" />
          <Play v-else :size="13" />
          {{ sandbox?.observed_state === 'stopped' ? '启动环境' : '初始化环境' }}
        </button>
        <button v-if="sandbox?.observed_state === 'ready'" type="button" class="sp-btn" :disabled="starting" @click="restartEnvironment">
          <RotateCcw :size="13" /> 重启环境
        </button>
      </div>

      <div v-if="services.length" class="sp-services" role="tablist" aria-label="预览服务">
        <button
          v-for="service in services" :key="service.id" type="button" role="tab"
          class="sp-service" :class="{ active: service.id === selectedId }"
          :aria-selected="service.id === selectedId" @click="selectService(service.id)"
        >
          <span class="sp-dot" :data-tone="serviceTone(service)"></span>
          <span class="sp-service-name">{{ service.name }}</span>
          <span class="sp-service-port">:{{ service.port }}</span>
          <span class="sp-service-state">{{ serviceStateLabel(service) }}</span>
        </button>
      </div>

      <div v-if="error" class="sp-empty sp-error" role="alert">
        <CircleAlert :size="18" />
        <p>{{ error }}</p>
        <button type="button" class="sp-btn" @click="load(false)">重试</button>
      </div>

      <div v-else-if="!services.length" class="sp-empty">
        <LoaderCircle v-if="loading" class="sp-spin" :size="18" />
        <Server v-else :size="20" />
        <p v-if="loading">正在加载项目服务…</p>
        <template v-else>
          <p>暂无服务</p>
          <span>让 Agent 启动服务（如 <code>npm run dev</code> / <code>uvicorn</code>），完成后这里会出现可预览页面。</span>
        </template>
      </div>

      <template v-else>
        <div class="sp-toolbar">
          <span class="sp-current">
            <span class="sp-dot" :data-tone="selectedService ? serviceTone(selectedService) : 'muted'"></span>
            {{ selectedService?.name || '未选择服务' }}
            <span v-if="selectedService" class="sp-muted">· {{ serviceStateLabel(selectedService) }} · {{ selectedService.port }}</span>
          </span>
          <button type="button" class="sp-icon-btn" title="刷新页面" aria-label="刷新页面" @click="reloadFrame">
            <RefreshCw :size="14" />
          </button>
          <button type="button" class="sp-icon-btn" title="重新获取预览授权" aria-label="重新获取预览授权" @click="refreshGrant">
            <KeyRound :size="14" />
          </button>
          <button type="button" class="sp-icon-btn" title="新窗口打开" aria-label="新窗口打开" :disabled="!previewUrl" @click="openExternal">
            <ExternalLink :size="14" />
          </button>
          <button type="button" class="sp-icon-btn" title="复制预览地址" aria-label="复制预览地址" :disabled="!previewUrl" @click="copyUrl">
            <Copy :size="14" />
          </button>
        </div>

        <div class="sp-frame">
          <iframe
            v-if="previewUrl" :key="iframeKey" :src="previewUrl" class="sp-iframe"
            title="项目预览" referrerpolicy="no-referrer"
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads"
            allow="clipboard-read; clipboard-write"
            @load="onFrameLoad"
          ></iframe>
          <div v-else class="sp-empty">
            <LoaderCircle v-if="grantLoading" class="sp-spin" :size="18" />
            <Globe v-else :size="20" />
            <p>{{ grantLoading ? '正在获取预览授权…' : '选择服务以加载预览' }}</p>
          </div>
          <div v-if="previewUrl && iframeLoading" class="sp-overlay" role="status">
            <LoaderCircle class="sp-spin" :size="16" /> 页面加载中…
          </div>
          <div v-else-if="previewUrl && iframeTimedOut" class="sp-overlay sp-overlay-error" role="alert">
            <CircleAlert :size="16" />
            <span>页面加载超时，服务可能仍在启动</span>
            <button type="button" class="sp-btn" @click="reloadFrame">重试</button>
          </div>
        </div>
        <div v-if="previewUrl" class="sp-foot">
          <span class="sp-muted">{{ fallbackUrl ? '预览地址' : '' }}</span>
          <code :title="previewDisplayUrl">{{ previewDisplayUrl }}</code>
          <span v-if="expiryText" class="sp-muted">{{ expiryText }}</span>
        </div>
      </template>
    </template>
  </section>
</template>

<script setup lang="ts">
/**
 * Sandbox Preview 面板：真实 iframe 预览项目 Service 页面。
 *
 * - 打开时自动初始化/启动项目 Sandbox（任务书 §8.6「重新打开项目时排队启动」）；
 * - 预览地址由后端 Preview Grant 签发（绑定 Sandbox Generation + Port，可过期/撤销）；
 * - 页面刷新时重新获取 Grant，不要求重启 Service；
 * - 前端不拼接容器 IP / 端口，只使用后端返回的地址。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  CircleAlert, Copy, ExternalLink, Globe, KeyRound, LoaderCircle, Play,
  RefreshCw, RotateCcw, Server,
} from '@lucide/vue'
import { ElMessage } from 'element-plus'
import {
  consumeSandboxEvents, sandboxApi,
  type PreviewGrantInfo, type SandboxInfo, type WorkspaceInfo, type WorkspaceServiceInfo,
} from '@/api/sandbox'

const props = defineProps<{
  projectId: number | null
  active?: boolean
}>()

const loading = ref(false)
const grantLoading = ref(false)
const starting = ref(false)
const error = ref('')
const workspace = ref<WorkspaceInfo | null>(null)
const sandbox = ref<SandboxInfo | null>(null)
const services = ref<WorkspaceServiceInfo[]>([])
const selectedId = ref<number | null>(null)
const previewUrl = ref('')
const fallbackUrl = ref('')
const grantExpiry = ref(0)
const iframeKey = ref(0)
const iframeLoading = ref(false)
const iframeTimedOut = ref(false)

const grants = new Map<number, { url: string; fallback: string; expires: number; id: string }>()
let pollTimer: number | undefined
let frameTimer: number | undefined
let eventAbort: AbortController | undefined
let eventRefreshTimer: number | undefined
let eventConnected = false
let mounted = false
let autoStartAttempted = false
let usingFallback = false

const selectedService = computed(() =>
  services.value.find((item) => item.id === selectedId.value) || null)

const canStartSandbox = computed(() =>
  !sandbox.value || ['stopped', 'failed'].includes(sandbox.value.observed_state)
  || ['stopped', 'failed'].includes(sandbox.value.desired_state))

const sandboxStateText = computed(() => {
  if (!sandbox.value) return '项目环境尚未初始化'
  const observed = sandbox.value.observed_state
  if (observed === 'ready') {
    return `环境就绪（Generation ${sandbox.value.generation}）`
  }
  if (observed === 'queued') return '环境排队创建中…'
  if (observed === 'provisioning') return '环境创建中…'
  if (observed === 'starting') return '环境启动中…'
  if (observed === 'stopping') return '环境停止中…'
  if (observed === 'stopped') return '环境已停止（项目文件保留）'
  if (observed === 'failed') {
    return `环境启动失败${sandbox.value.failure_message ? '：' + sandbox.value.failure_message : ''}`
  }
  return `环境状态：${observed}`
})

const sandboxTone = computed(() => {
  const observed = sandbox.value?.observed_state
  if (observed === 'ready') return 'success'
  if (observed === 'failed') return 'danger'
  if (observed === 'stopped') return 'muted'
  return 'info'
})

const expiryText = computed(() => {
  if (!grantExpiry.value) return ''
  const seconds = Math.max(0, Math.round(grantExpiry.value - Date.now() / 1000))
  if (seconds <= 0) return '预览授权已过期，点击钥匙重新获取'
  if (seconds < 120) return `${seconds}s 后过期`
  return `${Math.round(seconds / 60)} 分钟后过期`
})

const previewDisplayUrl = computed(() =>
  previewUrl.value.replace(/([?&])access_token=[^&]*/, '$1access_token=***'))

function withAccess(url: string, accessToken?: string): string {
  if (!accessToken) return url
  return `${url}${url.includes('?') ? '&' : '?'}access_token=${encodeURIComponent(accessToken)}`
}

function serviceTone(service: WorkspaceServiceInfo): string {
  const state = service.observed_state
  if (state === 'healthy' || (state === 'starting' && service.desired_state === 'running')) {
    return state === 'healthy' ? 'success' : 'info'
  }
  if (state === 'unhealthy') return 'warning'
  if (state === 'failed') return 'danger'
  if (['stopped', 'exited'].includes(state)) return 'muted'
  return 'info'
}

function serviceStateLabel(service: WorkspaceServiceInfo): string {
  const map: Record<string, string> = {
    created: '已创建', starting: '启动中', healthy: 'Ready', unhealthy: '异常',
    exited: '已退出', failed: '失败', stopping: '停止中', stopped: '已停止',
  }
  return map[service.observed_state] || service.observed_state
}

function stopFrameTimer() {
  if (frameTimer) {
    clearTimeout(frameTimer)
    frameTimer = undefined
  }
}

function onFrameLoad() {
  iframeLoading.value = false
  iframeTimedOut.value = false
  stopFrameTimer()
}

function reloadFrame() {
  if (!previewUrl.value) return
  iframeLoading.value = true
  iframeTimedOut.value = false
  iframeKey.value += 1
  stopFrameTimer()
  frameTimer = window.setTimeout(() => {
    if (!iframeLoading.value) return
    if (!usingFallback && fallbackUrl.value && fallbackUrl.value !== previewUrl.value) {
      // 浏览器无法解析 {token}.localhost 时自动切换路径式兜底地址
      usingFallback = true
      previewUrl.value = fallbackUrl.value
      iframeKey.value += 1
      return
    }
    iframeLoading.value = false
    iframeTimedOut.value = true
  }, 12_000)
}

async function load(silent = false) {
  if (!props.projectId) {
    reset()
    return
  }
  if (!silent) loading.value = true
  try {
    const detail = await sandboxApi.getProjectWorkspace(props.projectId)
    workspace.value = detail.workspace
    sandbox.value = detail.workspace?.sandbox || null
    services.value = detail.services || []
    error.value = ''
    const current = services.value.find((item) => item.id === selectedId.value)
    if (!current) {
      const preferred = services.value.find((item) => item.observed_state === 'healthy')
        || services.value.find((item) => item.desired_state === 'running')
        || services.value[0]
      selectedId.value = preferred?.id ?? null
    }
    if (props.active && canStartSandbox.value && !starting.value && !autoStartAttempted) {
      autoStartAttempted = true
      await startEnvironment(true)
      return
    }
    if (sandbox.value?.observed_state === 'ready' && selectedService.value) {
      await ensurePreview(selectedService.value)
    }
  } catch (exc: any) {
    if (!silent) error.value = exc?.response?.data?.message || exc?.message || '无法加载项目工作空间'
  } finally {
    loading.value = false
  }
}

function reset() {
  workspace.value = null
  sandbox.value = null
  services.value = []
  selectedId.value = null
  previewUrl.value = ''
  fallbackUrl.value = ''
  grantExpiry.value = 0
  grants.clear()
  autoStartAttempted = false
  usingFallback = false
  iframeLoading.value = false
  iframeTimedOut.value = false
  stopFrameTimer()
}

async function startEnvironment(auto = false) {
  if (!props.projectId || starting.value) return
  starting.value = true
  autoStartAttempted = true
  if (!auto) error.value = ''
  try {
    if (!sandbox.value) {
      await sandboxApi.provisionProject(props.projectId)
    } else {
      await sandboxApi.startSandbox(workspace.value?.id ?? 0)
    }
    await waitUntilReady()
  } catch (exc: any) {
    error.value = exc?.response?.data?.message || exc?.message || '环境启动失败'
  } finally {
    starting.value = false
  }
}

async function waitUntilReady(timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const detail = await sandboxApi.getProjectWorkspace(props.projectId as number)
    workspace.value = detail.workspace
    sandbox.value = detail.workspace?.sandbox || null
    services.value = detail.services || []
    if (sandbox.value?.observed_state === 'ready') {
      const preferred = services.value.find((item) => item.id === selectedId.value)
        || services.value[0]
      selectedId.value = preferred?.id ?? null
      if (preferred) await ensurePreview(preferred)
      return
    }
    if (sandbox.value?.observed_state === 'failed') {
      throw new Error(sandbox.value.failure_message || '环境启动失败')
    }
    await new Promise((resolve) => setTimeout(resolve, 2000))
  }
  throw new Error('环境启动超时，请稍后重试')
}

async function restartEnvironment() {
  if (!workspace.value) return
  starting.value = true
  try {
    grants.clear()
    await sandboxApi.restartSandbox(workspace.value.id)
    await waitUntilReady()
  } catch (exc: any) {
    error.value = exc?.response?.data?.message || exc?.message || '环境重启失败'
  } finally {
    starting.value = false
  }
}

async function ensurePreview(service: WorkspaceServiceInfo, force = false) {
  if (!workspace.value || sandbox.value?.observed_state !== 'ready') return
  const cached = grants.get(service.id)
  if (!force && cached && cached.expires > Date.now() / 1000 + 30) {
    if (previewUrl.value !== cached.url) {
      previewUrl.value = cached.url
      fallbackUrl.value = cached.fallback
      grantExpiry.value = cached.expires
      reloadFrame()
    }
    return
  }
  grantLoading.value = true
  try {
    const ttlSeconds = 3600
    const grant: PreviewGrantInfo = await sandboxApi.createPreviewGrant(
      workspace.value.id, service.id, ttlSeconds,
    )
    if (!grant.url) throw new Error('预览授权缺少地址')
    // 后端 expires_at 为无时区 UTC 字符串，前端直接按签发 TTL 计算，
    // 避免被浏览器按本地时区解析导致误判过期。
    const expires = Math.floor(Date.now() / 1000) + ttlSeconds
    const primaryUrl = withAccess(grant.url, grant.access_token)
    const fallback = grant.fallback_url
      ? withAccess(grant.fallback_url, grant.access_token)
      : ''
    grants.set(service.id, {
      url: primaryUrl, fallback, expires, id: grant.id,
    })
    previewUrl.value = primaryUrl
    fallbackUrl.value = fallback
    grantExpiry.value = expires
    usingFallback = false
    reloadFrame()
  } catch (exc: any) {
    previewUrl.value = ''
    error.value = exc?.response?.data?.message || exc?.message || '无法获取预览授权'
  } finally {
    grantLoading.value = false
  }
}

async function selectService(serviceId: number) {
  selectedId.value = serviceId
  const service = services.value.find((item) => item.id === serviceId)
  if (service) await ensurePreview(service)
}

async function refreshGrant() {
  const service = selectedService.value
  if (service) await ensurePreview(service, true)
}

function openExternal() {
  if (previewUrl.value) window.open(previewUrl.value, '_blank', 'noopener,noreferrer')
}

async function copyUrl() {
  if (!previewUrl.value) return
  try {
    await navigator.clipboard.writeText(previewUrl.value)
    ElMessage.success('预览地址已复制')
  } catch {
    ElMessage.error('无法复制预览地址')
  }
}

function scheduleReload() {
  if (eventRefreshTimer) return
  eventRefreshTimer = window.setTimeout(() => {
    eventRefreshTimer = undefined
    if (props.active) void load(true)
  }, 400)
}

function startEvents() {
  stopEvents()
  const workspaceId = workspace.value?.id
  if (!props.active || !workspaceId) return
  eventAbort = new AbortController()
  void consumeSandboxEvents(workspaceId, {
    signal: eventAbort.signal,
    onStateChange: (state) => {
      const connected = state === 'connected'
      if (connected === eventConnected) return
      eventConnected = connected
      startPolling()
    },
    onEvent: (event) => {
      if (!event.type.startsWith('service.') && !event.type.startsWith('sandbox.')
        && !event.type.startsWith('preview.')) return
      scheduleReload()
    },
  })
}

function stopEvents() {
  eventAbort?.abort()
  eventAbort = undefined
  if (eventRefreshTimer) {
    clearTimeout(eventRefreshTimer)
    eventRefreshTimer = undefined
  }
}

function startPolling() {
  stopPolling()
  if (!props.projectId) return
  // SSE 正常时低频兜底；SSE 不可用时恢复 4s 轮询
  const interval = eventConnected ? 30_000 : 4000
  pollTimer = window.setInterval(() => {
    if (document.visibilityState === 'hidden') return
    void load(true)
  }, interval)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = undefined
  }
}

onMounted(() => {
  mounted = true
  if (props.active) {
    void load(false)
    startPolling()
  }
})

onUnmounted(() => {
  mounted = false
  stopPolling()
  stopEvents()
  stopFrameTimer()
})

watch(() => props.projectId, () => {
  reset()
  stopEvents()
  if (props.active) void load(false)
})

watch(() => workspace.value?.id, () => startEvents())

watch(() => props.active, (active) => {
  if (!mounted) return
  if (active) {
    void load(true)
    startPolling()
    startEvents()
  } else {
    stopPolling()
    stopEvents()
  }
})
</script>

<style scoped>
.sandbox-pane { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.sp-sandbox-bar {
  flex: none; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 7px 10px; border-bottom: 1px solid var(--ui-border);
  font-size: 12px; color: var(--ui-secondary); background: var(--ui-bg);
}
.sp-sandbox-bar[data-tone="danger"] { color: var(--ui-danger); background: var(--ui-danger-soft); }
.sp-sandbox-bar[data-tone="success"] { color: var(--ui-success); }
.sp-sandbox-text { flex: 1 1 12rem; overflow-wrap: anywhere; }
.sp-dot { width: 7px; height: 7px; flex: none; border-radius: 50%; background: var(--ui-muted); }
.sp-dot[data-tone="success"] { background: var(--ui-success); }
.sp-dot[data-tone="info"] { background: var(--ui-info); }
.sp-dot[data-tone="warning"] { background: var(--ui-warning); }
.sp-dot[data-tone="danger"] { background: var(--ui-danger); }
.sp-dot[data-tone="muted"] { background: var(--ui-border-strong); }
.sp-btn {
  display: inline-flex; align-items: center; gap: 5px; flex: none;
  padding: 4px 9px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm);
  background: var(--ui-surface); color: var(--ui-secondary); font-size: 12px; cursor: pointer;
}
.sp-btn:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); }
.sp-btn:disabled { opacity: .55; cursor: default; }
.sp-services {
  flex: none; display: flex; gap: 4px; overflow-x: auto; padding: 6px 8px;
  border-bottom: 1px solid var(--ui-border); background: var(--ui-surface); scrollbar-width: thin;
}
.sp-service {
  display: inline-flex; align-items: center; gap: 6px; flex: none;
  padding: 4px 8px; border: 1px solid var(--ui-border); border-radius: 999px;
  background: var(--ui-bg); color: var(--ui-secondary); font-size: 12px; cursor: pointer;
}
.sp-service.active { border-color: var(--ui-accent); color: var(--ui-accent); background: var(--ui-accent-soft); }
.sp-service-name { font-weight: 550; max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sp-service-port { color: var(--ui-muted); font-variant-numeric: tabular-nums; }
.sp-service-state { color: var(--ui-muted); font-size: 11px; }
.sp-toolbar {
  flex: none; display: flex; align-items: center; gap: 2px; padding: 5px 8px;
  border-bottom: 1px solid var(--ui-border); background: var(--ui-surface);
}
.sp-current { flex: 1; min-width: 0; display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--ui-text); }
.sp-icon-btn {
  width: 28px; height: 28px; flex: none; display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: var(--ui-radius-sm); background: transparent;
  color: var(--ui-secondary); cursor: pointer;
}
.sp-icon-btn:hover:not(:disabled) { background: var(--ui-hover); color: var(--ui-accent); }
.sp-icon-btn:disabled { opacity: .45; cursor: default; }
.sp-frame { position: relative; flex: 1; min-height: 0; background: var(--ui-bg); }
.sp-iframe { width: 100%; height: 100%; border: 0; background: #fff; }
.sp-overlay {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  gap: 8px; flex-wrap: wrap; padding: 16px; text-align: center;
  color: var(--ui-secondary); background: color-mix(in srgb, var(--ui-surface) 86%, transparent);
  font-size: 13px;
}
.sp-overlay-error { color: var(--ui-danger); }
.sp-empty {
  flex: 1; min-height: 0; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 7px; padding: 24px 18px; text-align: center; color: var(--ui-secondary); font-size: 12.5px;
}
.sp-empty p { font-size: 13px; color: var(--ui-text); }
.sp-empty span { max-width: 42ch; line-height: 1.6; }
.sp-empty code { padding: 1px 4px; border-radius: 4px; background: var(--ui-subtle); font-size: 11.5px; }
.sp-error { color: var(--ui-danger); }
.sp-foot {
  flex: none; display: flex; align-items: center; gap: 6px; padding: 5px 9px;
  border-top: 1px solid var(--ui-border); background: var(--ui-surface); font-size: 11px;
  color: var(--ui-muted); overflow: hidden;
}
.sp-foot code {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 11px; color: var(--ui-secondary);
}
.sp-muted { color: var(--ui-muted); }
.sp-spin { animation: sp-spin 1s linear infinite; }
@keyframes sp-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .sp-spin { animation: none; } }
</style>
