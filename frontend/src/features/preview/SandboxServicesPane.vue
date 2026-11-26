<template>
  <section class="service-pane" aria-label="项目服务">
    <div v-if="!projectId" class="sv-empty">
      <Server :size="20" />
      <p>当前会话未绑定项目</p>
      <span>选择项目后可查看和操作 Sandbox 内运行的前后端服务。</span>
    </div>

    <template v-else>
      <div class="sv-env-bar">
        <span class="sv-dot" :data-tone="envTone"></span>
        <span class="sv-env-text">{{ envText }}</span>
        <button v-if="canStart" type="button" class="sv-btn" :disabled="busy" @click="startEnv">
          <LoaderCircle v-if="busy" class="sv-spin" :size="13" />
          <Play v-else :size="13" />启动环境
        </button>
        <button v-if="sandbox?.observed_state === 'ready'" type="button" class="sv-btn" :disabled="busy" @click="stopEnv">
          <Square :size="13" />停止环境
        </button>
        <button type="button" class="sv-btn" :disabled="busy" @click="load(false)">
          <RefreshCw :size="13" />刷新
        </button>
      </div>

      <div v-if="error" class="sv-empty sv-error" role="alert">
        <CircleAlert :size="18" />
        <p>{{ error }}</p>
        <button type="button" class="sv-btn" @click="load(false)">重试</button>
      </div>

      <ul v-else class="sv-list" aria-label="服务列表">
        <li v-if="loading && !services.length" class="sv-empty">
          <LoaderCircle class="sv-spin" :size="17" /> 正在加载服务…
        </li>
        <li v-else-if="!services.length" class="sv-empty">
          <Server :size="18" />
          <p>暂无服务</p>
          <span>Agent 启动的服务会显示在这里；也可以手动启动。</span>
        </li>
        <li v-for="service in services" :key="service.id" class="sv-item">
          <div class="sv-item-main">
            <span class="sv-dot" :data-tone="serviceTone(service)"></span>
            <div class="sv-item-text">
              <div class="sv-name">
                {{ service.name }}
                <span class="sv-port">:{{ service.port }}</span>
                <span class="sv-state" :data-tone="serviceTone(service)">{{ serviceStateLabel(service) }}</span>
              </div>
              <div class="sv-meta">
                <span>{{ service.observed_state }} / desired {{ service.desired_state }}</span>
                <span v-if="service.process_id">PID {{ service.process_id }}</span>
                <span v-if="service.exit_code !== null && service.exit_code !== undefined">exit {{ service.exit_code }}</span>
                <span>{{ service.working_directory }}</span>
              </div>
              <code class="sv-command" :title="service.command">{{ service.command }}</code>
            </div>
          </div>
          <div class="sv-actions">
            <button type="button" class="sv-icon-btn" title="查看日志" aria-label="查看日志" @click="openLogs(service)">
              <ScrollText :size="14" />
            </button>
            <button v-if="service.desired_state === 'running'" type="button" class="sv-icon-btn" title="停止服务" aria-label="停止服务" @click="stopService(service)">
              <Square :size="14" />
            </button>
            <button type="button" class="sv-icon-btn" title="重启服务" aria-label="重启服务" @click="restartService(service)">
              <RotateCcw :size="14" />
            </button>
          </div>
        </li>
      </ul>

      <details class="sv-manual">
        <summary><Plus :size="14" />手动启动服务</summary>
        <div class="sv-form">
          <input v-model="form.name" placeholder="服务名（如 frontend）" aria-label="服务名" />
          <input v-model="form.port" placeholder="端口（如 5173）" inputmode="numeric" aria-label="端口" />
          <input v-model="form.command" placeholder="启动命令（如 npm run dev -- --host 0.0.0.0）" aria-label="启动命令" />
          <button type="button" class="sv-btn sv-primary" :disabled="creating" @click="createService">
            <LoaderCircle v-if="creating" class="sv-spin" :size="13" />
            <Play v-else :size="13" />启动
          </button>
        </div>
      </details>
    </template>

    <el-dialog v-model="logsOpen" :title="`服务日志 · ${logsName}`" width="min(720px, 92vw)" append-to-body>
      <div class="sv-logs-head">
        <button type="button" class="sv-btn" :disabled="logsLoading" @click="reloadLogs">
          <LoaderCircle v-if="logsLoading" class="sv-spin" :size="13" />
          <RefreshCw v-else :size="13" />刷新
        </button>
        <span class="sv-muted">尾部 {{ logsTail }} 行（已脱敏）</span>
      </div>
      <pre class="sv-logs">{{ logs || '（无输出）' }}</pre>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
/** 项目 Services 面板：状态、启停/重启、日志与手动启动。 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  CircleAlert, LoaderCircle, Play, Plus, RefreshCw, RotateCcw, ScrollText,
  Server, Square,
} from '@lucide/vue'
import { ElMessage } from 'element-plus'
import { sandboxApi, type SandboxInfo, type WorkspaceInfo, type WorkspaceServiceInfo } from '@/api/sandbox'

const props = defineProps<{
  projectId: number | null
  active?: boolean
}>()

const loading = ref(false)
const busy = ref(false)
const creating = ref(false)
const error = ref('')
const sandbox = ref<SandboxInfo | null>(null)
const workspace = ref<WorkspaceInfo | null>(null)
const services = ref<WorkspaceServiceInfo[]>([])
const logsOpen = ref(false)
const logsLoading = ref(false)
const logs = ref('')
const logsName = ref('')
const logsTail = 300
let logsServiceId: number | null = null
let pollTimer: number | undefined

const form = reactive({ name: '', command: '', port: '' })

const canStart = computed(() => !sandbox.value
  || ['stopped', 'failed'].includes(sandbox.value.observed_state))

const envText = computed(() => {
  if (!sandbox.value) return '项目环境尚未初始化'
  const map: Record<string, string> = {
    queued: '环境排队中', provisioning: '环境创建中', starting: '环境启动中',
    ready: `环境就绪（Generation ${sandbox.value.generation}）`,
    stopping: '环境停止中', stopped: '环境已停止（文件保留）',
    failed: '环境失败',
  }
  return map[sandbox.value.observed_state] || `环境状态：${sandbox.value.observed_state}`
})

const envTone = computed(() => {
  const state = sandbox.value?.observed_state
  if (state === 'ready') return 'success'
  if (state === 'failed') return 'danger'
  if (state === 'stopped') return 'muted'
  return 'info'
})

function serviceTone(service: WorkspaceServiceInfo): string {
  if (service.observed_state === 'healthy') return 'success'
  if (service.observed_state === 'unhealthy') return 'warning'
  if (service.observed_state === 'failed') return 'danger'
  if (['stopped', 'exited'].includes(service.observed_state)) return 'muted'
  return 'info'
}

function serviceStateLabel(service: WorkspaceServiceInfo): string {
  const map: Record<string, string> = {
    created: '已创建', starting: '启动中', healthy: 'Ready', unhealthy: '异常',
    exited: '已退出', failed: '失败', stopping: '停止中', stopped: '已停止',
  }
  return map[service.observed_state] || service.observed_state
}

async function load(silent = false) {
  if (!props.projectId) {
    sandbox.value = null
    services.value = []
    return
  }
  if (!silent) loading.value = true
  try {
    const detail = await sandboxApi.getProjectWorkspace(props.projectId)
    workspace.value = detail.workspace
    sandbox.value = detail.workspace?.sandbox || null
    services.value = detail.services || []
    error.value = ''
  } catch (exc: any) {
    if (!silent) error.value = exc?.response?.data?.message || exc?.message || '无法加载服务'
  } finally {
    loading.value = false
  }
}

async function startEnv() {
  if (!props.projectId || busy.value) return
  busy.value = true
  try {
    if (!sandbox.value) await sandboxApi.provisionProject(props.projectId)
    else if (workspace.value) await sandboxApi.startSandbox(workspace.value.id)
    await load(true)
  } catch (exc: any) {
    error.value = exc?.response?.data?.message || exc?.message || '环境启动失败'
  } finally {
    busy.value = false
  }
}

async function stopEnv() {
  if (!workspace.value || busy.value) return
  busy.value = true
  try {
    await sandboxApi.stopSandbox(workspace.value.id)
    await load(true)
  } catch (exc: any) {
    ElMessage.error(exc?.response?.data?.message || exc?.message || '停止失败')
  } finally {
    busy.value = false
  }
}

async function stopService(service: WorkspaceServiceInfo) {
  if (!workspace.value) return
  try {
    await sandboxApi.stopService(workspace.value.id, service.id)
    await load(true)
    ElMessage.success(`服务 ${service.name} 已停止`)
  } catch (exc: any) {
    ElMessage.error(exc?.response?.data?.message || exc?.message || '停止服务失败')
  }
}

async function restartService(service: WorkspaceServiceInfo) {
  if (!workspace.value) return
  try {
    await sandboxApi.restartService(workspace.value.id, service.id)
    await load(true)
    ElMessage.success(`服务 ${service.name} 已重启`)
  } catch (exc: any) {
    ElMessage.error(exc?.response?.data?.message || exc?.message || '重启服务失败')
  }
}

async function createService() {
  if (!workspace.value) return
  const port = Number(form.port)
  if (!form.name.trim() || !form.command.trim() || !Number.isFinite(port)) {
    ElMessage.warning('请填写服务名、端口与启动命令')
    return
  }
  creating.value = true
  try {
    await sandboxApi.createService(workspace.value.id, {
      name: form.name.trim(), command: form.command.trim(), port,
    })
    const started = form.name.trim()
    form.name = ''
    form.command = ''
    form.port = ''
    await load(true)
    ElMessage.success(`服务 ${started} 已提交启动`)
  } catch (exc: any) {
    ElMessage.error(exc?.response?.data?.message || exc?.message || '启动服务失败')
  } finally {
    creating.value = false
  }
}

async function openLogs(service: WorkspaceServiceInfo) {
  logsServiceId = service.id
  logsName.value = service.name
  logs.value = ''
  logsOpen.value = true
  await reloadLogs()
}

async function reloadLogs() {
  if (!workspace.value || !logsServiceId) return
  logsLoading.value = true
  try {
    const data = await sandboxApi.serviceLogs(workspace.value.id, logsServiceId, logsTail)
    logs.value = data.content || ''
  } catch (exc: any) {
    logs.value = exc?.response?.data?.message || exc?.message || '读取日志失败'
  } finally {
    logsLoading.value = false
  }
}

function startPolling() {
  stopPolling()
  if (!props.projectId) return
  pollTimer = window.setInterval(() => {
    if (document.visibilityState === 'hidden') return
    void load(true)
  }, 4000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = undefined
  }
}

onMounted(() => {
  if (props.active) {
    void load(false)
    startPolling()
  }
})

onUnmounted(stopPolling)

watch(() => props.projectId, () => {
  sandbox.value = null
  workspace.value = null
  services.value = []
  if (props.active) void load(false)
})

watch(() => props.active, (active) => {
  if (active) {
    void load(true)
    startPolling()
  } else {
    stopPolling()
  }
})
</script>

<style scoped>
.service-pane { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; overflow: auto; }
.sv-env-bar {
  flex: none; display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
  padding: 7px 10px; border-bottom: 1px solid var(--ui-border);
  background: var(--ui-bg); font-size: 12px; color: var(--ui-secondary);
}
.sv-env-text { flex: 1 1 10rem; overflow-wrap: anywhere; }
.sv-dot { width: 7px; height: 7px; flex: none; border-radius: 50%; background: var(--ui-muted); }
.sv-dot[data-tone="success"] { background: var(--ui-success); }
.sv-dot[data-tone="info"] { background: var(--ui-info); }
.sv-dot[data-tone="warning"] { background: var(--ui-warning); }
.sv-dot[data-tone="danger"] { background: var(--ui-danger); }
.sv-dot[data-tone="muted"] { background: var(--ui-border-strong); }
.sv-btn {
  display: inline-flex; align-items: center; gap: 5px; flex: none;
  padding: 4px 9px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm);
  background: var(--ui-surface); color: var(--ui-secondary); font-size: 12px; cursor: pointer;
}
.sv-btn:hover:not(:disabled) { border-color: var(--ui-accent); color: var(--ui-accent); }
.sv-btn:disabled { opacity: .55; cursor: default; }
.sv-primary { color: var(--ui-accent); border-color: var(--ui-accent); }
.sv-list { list-style: none; margin: 0; padding: 6px; display: flex; flex-direction: column; gap: 6px; }
.sv-item {
  display: flex; align-items: flex-start; gap: 8px; padding: 8px 10px;
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-surface);
}
.sv-item-main { flex: 1; min-width: 0; display: flex; align-items: flex-start; gap: 8px; }
.sv-item-main > .sv-dot { margin-top: 6px; }
.sv-item-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.sv-name { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; font-size: 13px; font-weight: 600; color: var(--ui-text); }
.sv-port { color: var(--ui-muted); font-weight: 450; font-variant-numeric: tabular-nums; }
.sv-state { font-size: 11px; font-weight: 500; padding: 1px 6px; border-radius: 999px; background: var(--ui-subtle); color: var(--ui-secondary); }
.sv-state[data-tone="success"] { color: var(--ui-success); background: var(--ui-success-soft); }
.sv-state[data-tone="warning"] { color: var(--ui-warning); background: var(--ui-warning-soft); }
.sv-state[data-tone="danger"] { color: var(--ui-danger); background: var(--ui-danger-soft); }
.sv-state[data-tone="info"] { color: var(--ui-info); background: var(--ui-info-soft); }
.sv-meta { display: flex; gap: 10px; flex-wrap: wrap; font-size: 11px; color: var(--ui-muted); }
.sv-command {
  display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: 11.5px; color: var(--ui-secondary); background: var(--ui-bg);
  padding: 3px 6px; border-radius: var(--ui-radius-sm);
}
.sv-actions { display: flex; gap: 2px; flex: none; }
.sv-icon-btn {
  width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: var(--ui-radius-sm); background: transparent;
  color: var(--ui-secondary); cursor: pointer;
}
.sv-icon-btn:hover { background: var(--ui-hover); color: var(--ui-accent); }
.sv-empty {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 7px; padding: 28px 16px; text-align: center; color: var(--ui-secondary); font-size: 12.5px;
}
.sv-empty p { font-size: 13px; color: var(--ui-text); }
.sv-empty span { max-width: 42ch; line-height: 1.6; }
.sv-error { color: var(--ui-danger); }
.sv-manual {
  margin: 4px 8px 10px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md);
  background: var(--ui-bg);
}
.sv-manual summary {
  display: flex; align-items: center; gap: 6px; padding: 8px 10px; cursor: pointer;
  font-size: 12.5px; color: var(--ui-secondary); list-style: none;
}
.sv-manual summary::-webkit-details-marker { display: none; }
.sv-form { display: flex; flex-direction: column; gap: 6px; padding: 0 10px 10px; }
.sv-form input {
  width: 100%; padding: 6px 8px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm);
  background: var(--ui-surface); color: var(--ui-text); font-size: 12.5px;
}
.sv-form input:focus { outline: 2px solid var(--ui-accent-soft); border-color: var(--ui-accent); }
.sv-form .sv-btn { align-self: flex-start; }
.sv-logs-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.sv-logs {
  margin: 0; padding: 10px; max-height: 56vh; overflow: auto;
  background: #0f172a; color: #dbeafe; border-radius: var(--ui-radius-md);
  font-size: 12px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere;
}
.sv-muted { color: var(--ui-muted); font-size: 11.5px; }
.sv-spin { animation: sv-spin 1s linear infinite; }
@keyframes sv-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .sv-spin { animation: none; } }
</style>
