<template>
  <div class="page-container">
    <PageHeader title="MCP 服务" description="查看外部工具服务、Agent 绑定和调用策略。">
      <template #actions><UiButton variant="primary" @click="openCreate"><Plus />接入 MCP 服务</UiButton></template>
    </PageHeader>
    <details class="extension-help">
      <summary>MCP 工具审批与重试规则</summary>
      <p>接入外部 MCP（Model Context Protocol）服务，将其工具以
          <code>mcp__服务__工具</code> 命名注入指定 Agent。MCP 工具默认需人工审批且禁止自动重试，
          只读工具可显式降级；未启用 / 未绑定 / 未发现的工具不进工具面。</p>
    </details>

    <div class="data-surface">
      <ErrorState v-if="loadError" title="无法加载MCP 服务列表" @retry="load" />
    <el-table v-else :data="servers" v-loading="loading" empty-text="暂无 MCP 服务">
      <el-table-column prop="name" label="服务" min-width="130" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="mono">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column label="传输/端点" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <el-tag size="small" type="info">{{ row.transport }}</el-tag>
          <span class="mono ml-6">{{ row.transport === 'stdio' ? [row.command, ...(row.args || [])].join(' ') : row.url }}</span>
        </template>
      </el-table-column>
      <el-table-column label="工具" width="80">
        <template #default="{ row }">
          <el-link type="primary" @click="openTools(row)">{{ (row.discovered_tools || []).length }} 个</el-link>
        </template>
      </el-table-column>
      <el-table-column label="安全策略" width="170">
        <template #default="{ row }">
          <el-tag size="small" :type="row.require_approval ? 'danger' : 'success'">
            {{ row.require_approval ? '需人工审批' : '免审批' }}
          </el-tag>
          <el-tag size="small" :type="row.side_effect_class === 'read_only' ? 'success' : 'warning'" class="ml-6">
            {{ row.side_effect_class === 'read_only' ? '只读可重试' : '非幂等禁重试' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="绑定 Agent" width="110">
        <template #default="{ row }">
          <el-tag v-if="(row.agent_ids || []).length" size="small" type="success">{{ row.agent_ids.length }} 个</el-tag>
          <el-tag v-else size="small" type="info">未绑定</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近测试" width="150">
        <template #default="{ row }">
          <template v-if="row.last_test_at">
            <el-tag v-if="!row.last_test_error" size="small" type="success">连通</el-tag>
            <el-tooltip v-else :content="row.last_test_error" placement="top">
              <el-tag size="small" type="danger">失败</el-tag>
            </el-tooltip>
            <div class="sub-text">{{ (row.last_test_at || '').replace('T', ' ').slice(0, 19) }}</div>
          </template>
          <el-text v-else type="info" size="small">未测试</el-text>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
            {{ row.enabled ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="240">
        <template #default="{ row }">
          <el-button size="small" link :loading="testingId === row.id" @click="handleTest(row)">测试</el-button>
          <el-button size="small" link @click="openEdit(row)">编辑</el-button>
          <el-button size="small" link type="danger" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    </div>

    <!-- 接入/编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑 MCP 服务' : '接入 MCP 服务'" width="720px" destroy-on-close>
      <el-form :model="form" label-width="100px">
        <el-form-item label="服务名" required>
          <el-input v-model="form.name" :disabled="!!editingId" placeholder="kebab-case，如 fetch-tools" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" />
        </el-form-item>
        <el-form-item label="传输方式">
          <el-radio-group v-model="form.transport">
            <el-radio value="stdio">stdio（本地子进程）</el-radio>
            <el-radio value="streamable_http">streamable_http（远程端点）</el-radio>
          </el-radio-group>
        </el-form-item>

        <template v-if="form.transport === 'stdio'">
          <el-form-item label="启动命令" required>
            <el-input v-model="form.command" placeholder="如 npx / uvx / python" />
          </el-form-item>
          <el-form-item label="命令参数">
            <el-input v-model="argsText" type="textarea" :rows="3" placeholder="每行一个参数，如&#10;-y&#10;@modelcontextprotocol/server-everything" />
          </el-form-item>
          <el-form-item label="环境变量">
            <el-input v-model="envText" type="textarea" :rows="3" placeholder="每行一个 KEY=VALUE" />
          </el-form-item>
        </template>
        <template v-else>
          <el-form-item label="端点 URL" required>
            <el-input v-model="form.url" placeholder="https://example.com/mcp" />
          </el-form-item>
          <el-form-item label="请求头">
            <el-input v-model="headersText" type="textarea" :rows="3" placeholder="每行一个 KEY=VALUE（如 Authorization=Bearer xxx）" />
          </el-form-item>
        </template>

        <el-form-item label="安全策略">
          <el-switch v-model="form.require_approval" active-text="工具调用需人工审批" />
          <el-select v-model="form.side_effect_class" class="ml-12" style="width: 180px">
            <el-option label="只读（可重试）" value="read_only" />
            <el-option label="非幂等（禁重试）" value="non_idempotent" />
          </el-select>
        </el-form-item>
        <el-form-item label="绑定 Agent">
          <el-select v-model="form.agent_ids" multiple filterable placeholder="不绑定任何 Agent（工具面为空）">
            <el-option v-for="a in agentOptions" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>

    <!-- 工具管理对话框 -->
    <el-dialog v-model="toolsVisible" :title="`工具管理 — ${current?.name || ''}`" width="720px">
      <el-alert v-if="!(current?.discovered_tools || []).length" type="info" :closable="false"
        title="尚未发现工具" description="请先点击「测试」连接服务并执行工具发现。" class="mb-12" />
      <el-table v-else :data="current!.discovered_tools" border size="small">
        <el-table-column prop="name" label="工具名" width="160" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">mcp__{{ current!.name }}__{{ row.name }}</span></template>
        </el-table-column>
        <el-table-column prop="description" label="说明" min-width="180" show-overflow-tooltip />
        <el-table-column label="启用" width="80">
          <template #default="{ row }">
            <el-switch :model-value="toolEnabled(row.name)" @change="(v: any) => setToolField(row.name, 'enabled', v)" />
          </template>
        </el-table-column>
        <el-table-column label="需审批" width="80">
          <template #default="{ row }">
            <el-switch :model-value="toolApproval(row.name)" @change="(v: any) => setToolField(row.name, 'requires_approval', v)" />
          </template>
        </el-table-column>
        <el-table-column label="副作用等级" width="140">
          <template #default="{ row }">
            <el-select :model-value="toolSideEffect(row.name)" size="small"
              @change="(v: string) => setToolField(row.name, 'side_effect_class', v)">
              <el-option label="只读（可重试）" value="read_only" />
              <el-option label="非幂等（禁重试）" value="non_idempotent" />
            </el-select>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="toolsVisible = false">关闭</el-button>
        <el-button v-if="current" type="primary" :loading="saving" @click="saveToolSettings">保存工具设置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '@/components/layout/PageHeader.vue'
import UiButton from '@/components/ui/UiButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { Plus } from '@lucide/vue'
import {
  agentOptionApi,
  mcpApi,
  type AgentOption,
  type McpServerItem,
  type McpToolSetting,
} from '@/api/extensions'

const loading = ref(true)
const loadError = ref(false)
const saving = ref(false)
const testingId = ref<number | null>(null)
const servers = ref<McpServerItem[]>([])
const agentOptions = ref<AgentOption[]>([])

const dialogVisible = ref(false)
const toolsVisible = ref(false)
const editingId = ref<number | null>(null)
const current = ref<McpServerItem | null>(null)
const form = ref<Partial<McpServerItem>>({ transport: 'stdio' })
const argsText = ref('')
const envText = ref('')
const headersText = ref('')
const toolSettings = ref<Record<string, McpToolSetting>>({})

function parseLines(text: string): string[] {
  return text.split('\n').map((s) => s.trim()).filter(Boolean)
}
function parseKV(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of parseLines(text)) {
    const idx = line.indexOf('=')
    if (idx > 0) out[line.slice(0, idx).trim()] = line.slice(idx + 1).trim()
  }
  return out
}
function kvToText(kv: Record<string, string>): string {
  return Object.entries(kv || {}).map(([k, v]) => `${k}=${v}`).join('\n')
}

async function load() {
  loading.value = true
  loadError.value = false
  try {
    servers.value = (await mcpApi.list()).items
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  form.value = { name: '', description: '', transport: 'stdio', command: '', args: [], env: {}, url: '', headers: {}, enabled: true, require_approval: true, side_effect_class: 'non_idempotent', tool_settings: {}, agent_ids: [] }
  argsText.value = ''
  envText.value = ''
  headersText.value = ''
  dialogVisible.value = true
}

function openEdit(row: any) {
  editingId.value = row.id
  form.value = { ...row, args: [...(row.args || [])], env: { ...(row.env || {}) }, headers: { ...(row.headers || {}) }, agent_ids: [...(row.agent_ids || [])] }
  argsText.value = (row.args || []).join('\n')
  envText.value = kvToText(row.env)
  headersText.value = kvToText(row.headers)
  dialogVisible.value = true
}

async function handleSave() {
  if (!form.value.name) return ElMessage.warning('请填写服务名')
  const payload: Partial<McpServerItem> = {
    ...form.value,
    args: parseLines(argsText.value),
    env: parseKV(envText.value),
    headers: parseKV(headersText.value),
  }
  saving.value = true
  try {
    if (editingId.value) {
      await mcpApi.update(editingId.value, payload)
    } else {
      await mcpApi.create(payload)
    }
    ElMessage.success('已保存，建议立即点击「测试」完成工具发现')
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function handleTest(row: any) {
  testingId.value = row.id
  try {
    const result = await mcpApi.test(row.id)
    if (result.ok) {
      ElMessage.success(`连通成功：发现 ${result.tools.length} 个工具（server: ${(result.server_info as any)?.name || 'unknown'}）`)
    } else {
      ElMessage.error(`连通失败：${result.error}`)
    }
    await load()
  } finally {
    testingId.value = null
  }
}

function openTools(row: any) {
  current.value = row
  toolSettings.value = JSON.parse(JSON.stringify(row.tool_settings || {}))
  toolsVisible.value = true
}

function toolEnabled(name: string): boolean {
  return toolSettings.value[name]?.enabled ?? true
}
function toolApproval(name: string): boolean {
  return toolSettings.value[name]?.requires_approval ?? current.value?.require_approval ?? true
}
function toolSideEffect(name: string): string {
  return toolSettings.value[name]?.side_effect_class ?? current.value?.side_effect_class ?? 'non_idempotent'
}
function setToolField(name: string, field: keyof McpToolSetting, value: unknown) {
  toolSettings.value = { ...toolSettings.value, [name]: { ...toolSettings.value[name], [field]: value } }
}

async function saveToolSettings() {
  if (!current.value) return
  saving.value = true
  try {
    await mcpApi.update(current.value.id, { tool_settings: toolSettings.value })
    ElMessage.success('工具设置已保存')
    toolsVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function handleDelete(row: any) {
  await confirmDestructive(`确认删除 MCP 服务「${row.name}」？绑定的 Agent 将失去其工具。`, '删除 MCP 服务')
  await mcpApi.remove(row.id)
  ElMessage.success('已删除')
  await load()
}

onMounted(async () => {
  await load()
  try {
    agentOptions.value = await agentOptionApi.list()
  } catch {
    agentOptions.value = []
  }
})
</script>

<style scoped>
.extension-help { width: fit-content; max-width: 760px; margin-bottom: var(--ui-space-4); color: var(--ui-muted); }
.extension-help summary { cursor: pointer; color: var(--ui-secondary); }
.extension-help p { padding: var(--ui-space-2) 0; }
.ml-6 {
  margin-left: 6px;
}
.ml-12 {
  margin-left: 12px;
}
.mb-12 {
  margin-bottom: 12px;
}
.sub-text {
  font-size: 12px;
  color: var(--ui-muted);
}
</style>
