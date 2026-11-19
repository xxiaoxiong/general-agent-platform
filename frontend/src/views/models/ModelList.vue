<template>
  <div class="page-container">
    <PageHeader title="模型管理" description="配置供应商连接及平台可用的模型。">
      <template v-if="auth.isSuperuser" #actions>
        <el-button :icon="Plus" @click="openProviderDialog()">添加供应商</el-button>
        <el-button type="primary" :icon="Plus" :disabled="!providers.length" @click="openConfigDialog()">添加模型</el-button>
      </template>
    </PageHeader>

    <el-skeleton v-if="loading && !configs.length && !providers.length" animated :rows="5" />
    <EmptyState v-else-if="loadFailed" title="模型配置加载失败" description="检查连接后重试。" :icon="CircleAlert">
      <el-button @click="loadData">重新加载</el-button>
    </EmptyState>
    <el-tabs v-else v-model="activeTab">
      <el-tab-pane label="模型配置" name="models">
        <section class="data-surface">
          <div class="page-toolbar">
            <el-input class="search-model" v-model="query" :prefix-icon="Search" placeholder="搜索模型名称或标识" aria-label="搜索模型" clearable />
            <el-select class="filter-select" v-model="filterProviderId" placeholder="所有供应商" aria-label="按供应商筛选" clearable filterable>
              <el-option v-for="p in providers" :key="p.id" :label="p.display_name" :value="p.id" />
            </el-select>
            <el-select class="filter-select" v-model="filterModelType" placeholder="所有类型" aria-label="按类型筛选" clearable>
              <el-option v-for="(label, value) in modelTypes" :key="value" :label="label" :value="value" />
            </el-select>
          </div>
          <el-table :data="filteredConfigs" v-loading="loading" row-key="id">
            <el-table-column label="模型" min-width="215" show-overflow-tooltip>
              <template #default="{ row }">
                <div class="resource-name">{{ row.display_name }}</div>
                <div class="resource-meta mono">{{ row.name }}</div>
              </template>
            </el-table-column>
            <el-table-column label="供应商" min-width="130" show-overflow-tooltip>
              <template #default="{ row }">{{ providerName(row.provider_id) }}</template>
            </el-table-column>
            <el-table-column label="类型与能力" min-width="180">
              <template #default="{ row }">
                <div class="capabilities">
                  <el-tag size="small" type="info">{{ modelTypes[row.model_type] || row.model_type }}</el-tag>
                  <el-tag v-if="row.supports_streaming" size="small" type="info" effect="plain">流式</el-tag>
                  <el-tag v-if="row.supports_function_call" size="small" type="info" effect="plain">工具调用</el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="上下文" min-width="100" align="right" header-align="right">
              <template #default="{ row }"><span class="mono">{{ row.context_length.toLocaleString() }}</span></template>
            </el-table-column>
            <el-table-column label="状态" width="94">
              <template #default="{ row }"><el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '启用' : '停用' }}</el-tag></template>
            </el-table-column>
            <el-table-column v-if="auth.isSuperuser" label="操作" width="188" align="right">
              <template #default="{ row }">
                <div class="model-actions">
                  <el-button v-if="row.model_type === 'chat'" size="small" text @click="testConfig(row)">能力测试</el-button>
                  <el-button size="small" text @click="openConfigDialog(row)">编辑</el-button>
                  <el-button size="small" text type="danger" @click="deleteConfig(row)">删除</el-button>
                </div>
              </template>
            </el-table-column>
            <template #empty>
              <EmptyState :title="configs.length ? '没有匹配的模型' : '暂无模型配置'" :icon="Cpu"
                :description="configs.length ? '请调整搜索或筛选条件。' : '添加供应商后，即可注册该供应商提供的模型。'" />
            </template>
          </el-table>
        </section>
      </el-tab-pane>
      <el-tab-pane label="模型供应商" name="providers">
        <section class="data-surface">
          <el-table :data="providers" v-loading="loading" row-key="id">
            <el-table-column label="供应商" min-width="190" show-overflow-tooltip>
              <template #default="{ row }">
                <div class="resource-name">{{ row.display_name }}</div>
                <div class="resource-meta">{{ providerTypeLabel(row.provider_type) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="API 地址" min-width="220" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono resource-meta">{{ row.base_url }}</span></template>
            </el-table-column>
            <el-table-column label="状态" width="92">
              <template #default="{ row }"><el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '启用' : '停用' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="230" align="right">
              <template #default="{ row }">
                <div class="model-actions">
                  <el-button size="small" :icon="PlugZap" :loading="testingIds.has(row.id)" @click="testProvider(row)">连接测试</el-button>
                  <template v-if="auth.isSuperuser">
                    <el-button size="small" text @click="openProviderDialog(row)">编辑</el-button>
                    <el-button size="small" text type="danger" @click="deleteProvider(row)">删除</el-button>
                  </template>
                </div>
              </template>
            </el-table-column>
            <template #empty>
              <EmptyState title="暂无模型供应商" description="添加 OpenAI 兼容、本地 Ollama 或自定义供应商。" :icon="Server" />
            </template>
          </el-table>
        </section>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="probeVisible" :title="`能力测试 · ${probingModel?.display_name ?? ''}`" width="min(440px, calc(100vw - 32px))">
      <el-skeleton v-if="probing" animated :rows="2" />
      <EmptyState v-else-if="probeFailed" title="能力测试请求失败" :icon="CircleAlert">
        <el-button v-if="probingModel" @click="testConfig(probingModel)">重试</el-button>
      </EmptyState>
      <template v-else-if="probeResult">
        <dl class="probe-summary">
          <div>
            <dt>原生工具调用</dt>
            <dd><el-tag :type="probeResult.tool_calling ? 'success' : 'danger'">{{ probeResult.tool_calling ? '通过' : '未通过' }}</el-tag></dd>
          </div>
          <div>
            <dt>流式输出</dt>
            <dd><el-tag :type="probeResult.streaming ? 'success' : 'danger'">{{ probeResult.streaming ? '通过' : '未通过' }}</el-tag></dd>
          </div>
        </dl>
        <p class="resource-meta">测试会向当前模型发送探测请求并更新能力验证状态。</p>
      </template>
    </el-dialog>

    <!-- 供应商表单 -->
    <el-dialog v-model="providerDialogVisible" :title="editingProvider ? '编辑供应商' : '添加供应商'"
      width="min(520px, calc(100vw - 32px))" @closed="providerForm.api_key = ''">
      <el-form :model="providerForm" label-position="top">
        <el-form-item label="名称标识"><el-input v-model="providerForm.name" :disabled="!!editingProvider" /></el-form-item>
        <el-form-item label="显示名称"><el-input v-model="providerForm.display_name" /></el-form-item>
        <el-form-item label="类型">
          <el-select v-model="providerForm.provider_type" :disabled="!!editingProvider" class="w-full">
            <el-option label="OpenAI 兼容" value="openai_compatible" />
            <el-option label="Ollama 本地" value="ollama" />
            <el-option label="自定义" value="custom" />
          </el-select>
        </el-form-item>
        <el-form-item label="API 地址"><el-input v-model="providerForm.base_url" placeholder="https://api.example.com/v1" /></el-form-item>
        <el-form-item label="API Key"><el-input v-model="providerForm.api_key" type="password" autocomplete="new-password" :placeholder="editingProvider ? '留空保留现有密钥' : '可选'" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="providerForm.description" type="textarea" :rows="2" /></el-form-item>
        <el-form-item v-if="editingProvider" label="状态">
          <el-switch v-model="providerForm.is_active" active-text="启用" inactive-text="停用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="providerDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveProvider">保存</el-button>
      </template>
    </el-dialog>

    <!-- 模型表单 -->
    <el-dialog v-model="configDialogVisible" :title="editingConfig ? '编辑模型' : '添加模型'" width="min(520px, calc(100vw - 32px))">
      <el-form :model="configForm" label-position="top">
        <el-form-item label="供应商">
          <el-select v-model="configForm.provider_id" class="w-full">
            <el-option v-for="p in providers" :key="p.id" :label="p.display_name" :value="p.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="模型ID"><el-input v-model="configForm.name" :disabled="!!editingConfig" placeholder="如 gpt-4o / qwen-max" /></el-form-item>
        <el-form-item label="显示名称"><el-input v-model="configForm.display_name" /></el-form-item>
        <el-form-item label="类型">
          <el-select v-model="configForm.model_type" class="w-full">
            <el-option label="对话 (chat)" value="chat" />
            <el-option label="嵌入 (embedding)" value="embedding" />
            <el-option label="图像 (image)" value="image" />
            <el-option label="音频 (audio)" value="audio" />
          </el-select>
        </el-form-item>
        <el-form-item label="上下文长度"><el-input-number v-model="configForm.context_length" :min="512" :max="1000000" /></el-form-item>
        <el-form-item label="最大输出"><el-input-number v-model="configForm.max_tokens" :min="256" :max="32768" /></el-form-item>
        <el-form-item label="流式输出"><el-switch v-model="configForm.supports_streaming" /></el-form-item>
        <el-form-item v-if="editingConfig" label="状态"><el-switch v-model="configForm.is_active" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="configDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleAlert, Cpu, PlugZap, Plus, Search, Server } from '@lucide/vue'
import { modelApi, type ModelProbeReport } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import { useAuthStore } from '@/stores/auth'
import type { ModelProvider, ModelConfig } from '@/types'

const providers = ref<ModelProvider[]>([])
const auth = useAuthStore()
const configs = ref<ModelConfig[]>([])
const activeTab = ref('models')
const loading = ref(true)
const loadFailed = ref(false)
const query = ref('')
const filterProviderId = ref<number | null>(null)
const filterModelType = ref('')
const saving = ref(false)
const testingIds = reactive(new Set<number>())
const modelTypes: Record<string, string> = { chat: '对话', embedding: '嵌入', image: '图像', audio: '音频' }
const probeVisible = ref(false)
const probing = ref(false)
const probeFailed = ref(false)
const probingModel = ref<ModelConfig | null>(null)
const probeResult = ref<ModelProbeReport | null>(null)

const providerDialogVisible = ref(false)
const editingProvider = ref<ModelProvider | null>(null)
const providerForm = reactive({
  name: '', display_name: '', provider_type: 'openai_compatible', base_url: '', api_key: '', description: '', is_active: true,
})

const configDialogVisible = ref(false)
const editingConfig = ref<ModelConfig | null>(null)
const configForm = reactive({
  provider_id: null as number | null, name: '', display_name: '', model_type: 'chat',
  context_length: 4096, max_tokens: 2048, supports_streaming: true, is_active: true,
})

const filteredConfigs = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return configs.value.filter(c =>
    (!filterProviderId.value || c.provider_id === filterProviderId.value) &&
    (!filterModelType.value || c.model_type === filterModelType.value) &&
    (!keyword || [c.display_name, c.name].some(value => value.toLowerCase().includes(keyword))),
  )
})

function providerName(id: number) {
  return providers.value.find(p => p.id === id)?.display_name || '—'
}

function providerTypeLabel(type: string) {
  const map: Record<string, string> = { openai_compatible: 'OpenAI 兼容', ollama: 'Ollama 本地', custom: '自定义' }
  return map[type] || type
}

async function loadData() {
  loading.value = true
  loadFailed.value = false
  try {
    const [ps, cs] = await Promise.all([modelApi.listProviders(), modelApi.listConfigs()])
    providers.value = ps
    configs.value = cs
  } catch {
    loadFailed.value = true
  } finally {
    loading.value = false
  }
}

function openProviderDialog(p?: ModelProvider) {
  editingProvider.value = p ?? null
  Object.assign(providerForm, {
    name: p?.name ?? '',
    display_name: p?.display_name ?? '',
    provider_type: p?.provider_type ?? 'openai_compatible',
    base_url: p?.base_url ?? '',
    api_key: '',
    description: p?.description ?? '',
    is_active: p?.is_active ?? true,
  })
  providerDialogVisible.value = true
}

async function saveProvider() {
  if (saving.value) return
  const { api_key, ...fields } = providerForm
  if (![fields.name, fields.display_name, fields.base_url].every(value => value.trim())) {
    ElMessage.warning('请填写名称标识、显示名称和 API 地址')
    return
  }
  const payload = { ...fields, ...(api_key.trim() ? { api_key } : {}) }
  saving.value = true
  try {
    if (editingProvider.value) {
      const updated = await modelApi.updateProvider(editingProvider.value.id, payload)
      providers.value = providers.value.map(provider => provider.id === updated.id ? updated : provider)
      ElMessage.success('供应商已更新')
    } else {
      const created = await modelApi.createProvider(payload)
      providers.value = [...providers.value, created]
      ElMessage.success('供应商已添加')
      activeTab.value = 'providers'
    }
    providerForm.api_key = ''
    providerDialogVisible.value = false
  } catch {
    // 请求层已提示失败原因，保留表单方便重试。
  } finally {
    saving.value = false
  }
}

async function deleteProvider(p: ModelProvider) {
  if (configs.value.some(config => config.provider_id === p.id)) {
    ElMessage.warning('请先删除或迁移该供应商下的模型配置')
    return
  }
  try {
    await ElMessageBox.confirm(`删除供应商「${p.display_name}」？`, '删除供应商', { type: 'warning', confirmButtonText: '删除' })
    await modelApi.deleteProvider(p.id)
    providers.value = providers.value.filter(provider => provider.id !== p.id)
    if (filterProviderId.value === p.id) filterProviderId.value = null
    ElMessage.success('供应商已删除')
  } catch {
    // 取消确认或请求层已提示失败原因。
  }
}

async function testProvider(p: ModelProvider) {
  if (testingIds.has(p.id)) return
  testingIds.add(p.id)
  try {
    const res = await modelApi.testProvider(p.id)
    if (res.success) ElMessage.success(res.message)
    else ElMessage.error('供应商连接失败，请检查 API 地址和认证信息。')
  } catch {
    // 请求层已提示失败原因。
  } finally {
    testingIds.delete(p.id)
  }
}

async function testConfig(model: ModelConfig) {
  if (probing.value) return
  probingModel.value = model
  probeVisible.value = true
  probing.value = true
  probeFailed.value = false
  probeResult.value = null
  try {
    const result = await modelApi.testConfig(model.id)
    probeResult.value = 'detail' in result ? result.detail.report ?? null : result
    probeFailed.value = !probeResult.value
  } catch {
    probeFailed.value = true
  } finally {
    probing.value = false
  }
}

function openConfigDialog(c?: ModelConfig) {
  editingConfig.value = c ?? null
  Object.assign(configForm, {
    provider_id: c?.provider_id ?? providers.value[0]?.id ?? null,
    name: c?.name ?? '',
    display_name: c?.display_name ?? '',
    model_type: c?.model_type ?? 'chat',
    context_length: c?.context_length ?? 4096,
    max_tokens: c?.max_tokens ?? 2048,
    supports_streaming: c?.supports_streaming ?? true,
    is_active: c?.is_active ?? true,
  })
  configDialogVisible.value = true
}

async function saveConfig() {
  if (saving.value) return
  if (!configForm.provider_id || !configForm.name.trim() || !configForm.display_name.trim()) {
    ElMessage.warning('请选择供应商，并填写模型标识和显示名称')
    return
  }
  saving.value = true
  try {
    if (editingConfig.value) {
      const updated = await modelApi.updateConfig(editingConfig.value.id, configForm)
      configs.value = configs.value.map(config => config.id === updated.id ? updated : config)
      ElMessage.success('模型配置已更新')
    } else {
      const created = await modelApi.createConfig(configForm)
      configs.value = [...configs.value, created]
      ElMessage.success('模型已添加')
      activeTab.value = 'models'
    }
    configDialogVisible.value = false
  } catch {
    // 请求层已提示失败原因，保留表单方便重试。
  } finally {
    saving.value = false
  }
}

async function deleteConfig(c: ModelConfig) {
  try {
    await ElMessageBox.confirm(`删除模型「${c.display_name}」？`, '删除模型', { type: 'warning', confirmButtonText: '删除' })
    await modelApi.deleteConfig(c.id)
    configs.value = configs.value.filter(config => config.id !== c.id)
    ElMessage.success('模型已删除')
  } catch {
    // 取消确认或请求层已提示失败原因。
  }
}

onMounted(loadData)
</script>

<style scoped>
.search-model { width: min(100%, 256px); }
.filter-select { width: 148px; }
.capabilities { display: flex; flex-wrap: wrap; gap: 4px; }
.model-actions { display: flex; justify-content: flex-end; align-items: center; gap: 4px; }
.model-actions :deep(.el-button + .el-button) { margin-inline-start: 0; }
.model-actions :deep(.el-button.is-text) { padding-inline: 6px; }
.probe-summary { display: grid; gap: 10px; margin: 0 0 16px; }
.probe-summary div { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.probe-summary dd { margin: 0; }
</style>
