<template>
  <div class="page-container">
    <RouterLink to="/prompts" class="debug-back"><ArrowLeft :size="15" /> 提示词模板库</RouterLink>
    <PageHeader title="提示词调试器" description="预览变量替换效果，并比较不同模型的输出。">
      <template #actions>
        <el-button :icon="Save" :disabled="!promptContent.trim()" @click="saveAsTemplate">保存为模板</el-button>
        <el-button type="primary" :icon="Play" :loading="running"
          :disabled="!renderedPrompt || !selectedModelIds.length || previewing || templateLoading" @click="runDebug">
          {{ selectedModelIds.length > 1 ? `对比 ${selectedModelIds.length} 个模型` : '运行调试' }}
        </el-button>
      </template>
    </PageHeader>

    <div class="debug-layout">
      <section class="data-surface debug-panel" aria-labelledby="editor-heading">
        <header>
          <h2 id="editor-heading">编辑与参数</h2>
          <el-select v-model="selectedTemplateId" class="template-select" filterable clearable
            aria-label="从模板库加载" placeholder="从模板库加载" :loading="optionsLoading || templateLoading" @change="loadTemplate">
            <el-option v-for="template in templateOptions" :key="template.id" :label="template.name" :value="template.id" />
          </el-select>
        </header>
        <div class="debug-content">
          <el-form label-position="top" :disabled="templateLoading">
            <el-form-item label="系统提示（可选）">
              <el-input v-model="systemPrompt" type="textarea" :rows="2" placeholder="设置模型角色或全局指令" />
            </el-form-item>
            <el-form-item label="提示词" required>
              <el-input v-model="promptContent" type="textarea" :rows="7" class="code-editor"
                placeholder="输入提示词，使用 {{变量名}} 定义变量" />
            </el-form-item>
          </el-form>

          <section v-if="detectedVars.length" class="debug-section">
            <h3>变量</h3>
            <el-form label-position="top">
              <el-form-item v-for="variable in detectedVars" :key="variable" :label="variable">
                <el-input v-model="varValues[variable]" :placeholder="`填写 ${variable} 的值`" />
              </el-form-item>
            </el-form>
          </section>

          <section class="debug-section">
            <h3>对比模型</h3>
            <el-skeleton v-if="optionsLoading" animated :rows="1" />
            <div v-else-if="optionsFailed" class="retry-options">
              <span class="resource-meta">模型或模板列表加载失败。</span>
              <el-button text :icon="RefreshCw" @click="loadOptions">重试</el-button>
            </div>
            <p v-else-if="!chatModels.length" class="resource-meta">
              暂无可用对话模型。<RouterLink to="/models">前往模型管理</RouterLink>
            </p>
            <el-checkbox-group v-if="chatModels.length" v-model="selectedModelIds" class="model-list">
              <el-checkbox v-for="model in chatModels" :key="model.id" :value="model.id">{{ model.display_name }}</el-checkbox>
            </el-checkbox-group>
          </section>
        </div>
      </section>

      <div class="debug-output">
        <section class="data-surface debug-panel" aria-labelledby="preview-heading">
          <header>
            <h2 id="preview-heading">渲染预览</h2>
            <el-tag v-if="missingVars.length" size="small" type="warning">缺少 {{ missingVars.length }} 个变量</el-tag>
          </header>
          <div class="debug-content">
            <pre class="render-preview" :aria-busy="previewing">{{ renderedPrompt || '输入提示词后，可在此查看变量替换后的内容。' }}</pre>
            <p v-if="missingVars.length" class="resource-meta missing-variables">{{ missingVars.join('、') }}</p>
          </div>
        </section>

      <section class="data-surface debug-panel" aria-labelledby="output-heading">
        <header>
          <h2 id="output-heading">调试结果</h2>
          <el-button v-if="results.length" text size="small" @click="clearResults">清空</el-button>
        </header>
        <div class="debug-content" aria-live="polite" :aria-busy="running">
          <el-skeleton v-if="running" animated :rows="5" />
          <EmptyState v-else-if="debugFailed" title="运行失败" description="检查模型配置后，可重新运行调试。" :icon="CircleAlert" />
          <EmptyState v-else-if="!results.length" :title="ran ? '未返回调试结果' : '等待运行'"
            :description="ran ? '确认模型可用后再试。' : '输入提示词并选择模型，这里将显示响应、耗时和 Token 用量。'" :icon="FlaskConical" />
          <article v-for="result in results" :key="result.model_config_id" class="model-result">
            <div class="result-heading">
              <h3>{{ result.model_name || `模型 ${result.model_config_id}` }}</h3>
              <el-tag :type="result.error ? 'danger' : 'success'" size="small">{{ result.error ? '失败' : '完成' }}</el-tag>
            </div>
            <div v-if="result.duration_ms != null || result.usage" class="result-metadata resource-meta mono">
              <span v-if="result.duration_ms != null">{{ result.duration_ms.toLocaleString() }} ms</span>
              <span v-if="result.usage">输入 {{ result.usage.prompt_tokens || 0 }} · 输出 {{ result.usage.completion_tokens || 0 }} Token</span>
            </div>
            <p v-if="result.error" class="result-error">{{ result.error }}</p>
            <div v-else class="markdown-body result-content" v-html="renderMarkdown(result.content)" />
            <div v-if="result.content" class="result-actions">
              <el-button size="small" text :icon="Copy" @click="copyResult(result.content)">复制</el-button>
              <el-button size="small" text @click="useAsPrompt(result.content)">用作提示词</el-button>
            </div>
          </article>
        </div>
      </section>
      </div>
    </div>

    <!-- Save as Template Dialog -->
    <el-dialog v-model="saveDialogVisible" title="保存为模板" width="min(480px, calc(100vw - 32px))">
      <el-form :model="saveForm" label-position="top">
        <el-form-item label="模板名称" required>
          <el-input v-model="saveForm.name" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select v-model="saveForm.category" class="w-full">
            <el-option v-for="category in categories" :key="category.value" :label="category.label" :value="category.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="saveForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="公开">
          <el-switch v-model="saveForm.is_public" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="saveDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="confirmSaveTemplate">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, CircleAlert, Copy, FlaskConical, Play, RefreshCw, Save } from '@lucide/vue'
import { useDebounceFn } from '@vueuse/core'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { promptApi, modelApi } from '@/api'
import PageHeader from '@/components/layout/PageHeader.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import type { ModelConfig } from '@/types'
import { categories, detectVariables, type PromptTemplate } from './promptPresentation'

interface DebugResult {
  model_config_id: number
  model_name?: string
  content?: string
  error?: string
  duration_ms?: number
  usage?: { prompt_tokens?: number; completion_tokens?: number }
}

const route = useRoute()
const promptContent = ref('')
const systemPrompt = ref('')
const varValues = reactive<Record<string, string>>(Object.create(null))
const detectedVars = computed(() => detectVariables(promptContent.value))
const renderedPrompt = ref('')
const missingVars = ref<string[]>([])
const selectedModelIds = ref<number[]>([])
const chatModels = ref<ModelConfig[]>([])
const running = ref(false)
const ran = ref(false)
const debugFailed = ref(false)
const previewing = ref(false)
const results = ref<DebugResult[]>([])
const optionsLoading = ref(true)
const optionsFailed = ref(false)
const templateLoading = ref(false)
const templateOptions = ref<Pick<PromptTemplate, 'id' | 'name'>[]>([])
const selectedTemplateId = ref<number | null>(null)
let previewVersion = 0
let templateVersion = 0

function renderMarkdown(value?: string) {
  return DOMPurify.sanitize(marked.parse(value ?? '', { async: false }) as string)
}

watch(detectedVars, variables => {
  for (const key of Object.keys(varValues)) {
    if (!variables.includes(key)) delete varValues[key]
  }
  for (const key of variables) {
    if (!(key in varValues)) varValues[key] = ''
  }
}, { flush: 'sync' })

const renderPreview = useDebounceFn(async (id: number, content: string, variables: Record<string, string>) => {
  try {
    const response = await promptApi.render({ content, variables })
    if (id !== previewVersion) return
    renderedPrompt.value = response.rendered
    missingVars.value = response.missing_vars || []
  } catch {
    if (id !== previewVersion) return
    renderedPrompt.value = content.replace(/\{\{([\p{L}\p{N}_]+)\}\}/gu, (match, key: string) => variables[key] || match)
    missingVars.value = detectVariables(content).filter(variable => !variables[variable])
  } finally {
    if (id === previewVersion) previewing.value = false
  }
}, 220)

watch([promptContent, varValues], () => {
  const id = ++previewVersion
  const content = promptContent.value
  if (!content.trim()) {
    previewing.value = false
    renderedPrompt.value = ''
    missingVars.value = []
    return
  }
  previewing.value = true
  void renderPreview(id, content, { ...varValues })
}, { deep: true })

async function runDebug() {
  if (running.value || previewing.value || !renderedPrompt.value || !selectedModelIds.value.length) return
  running.value = true
  debugFailed.value = false
  results.value = []
  try {
    const res = await promptApi.debug({
      content: promptContent.value,
      variables: { ...varValues },
      model_config_ids: [...selectedModelIds.value],
      system_prompt: systemPrompt.value || undefined,
    })
    results.value = res.results || []
    ran.value = true
  } catch {
    debugFailed.value = true
  } finally {
    running.value = false
  }
}

function clearResults() {
  results.value = []
  ran.value = false
}

async function copyResult(content: string) {
  try {
    await navigator.clipboard.writeText(content)
    ElMessage.success('已复制')
  } catch {
    ElMessage.error('无法访问剪贴板，请选中文本后手动复制')
  }
}

function useAsPrompt(content: string) {
  promptContent.value = content
  ElMessage.success('已填入提示词编辑框')
}

async function loadTemplate(value?: number | string | null) {
  const id = ++templateVersion
  const templateId = Number(value)
  if (!Number.isSafeInteger(templateId) || templateId <= 0) {
    templateLoading.value = false
    return
  }
  templateLoading.value = true
  try {
    const template: PromptTemplate = await promptApi.get(templateId)
    if (id !== templateVersion) return
    promptContent.value = template.content
    systemPrompt.value = ''
  } catch {
    // 请求层已提示失败原因。
  } finally {
    if (id === templateVersion) templateLoading.value = false
  }
}

const saveDialogVisible = ref(false)
const saving = ref(false)
const saveForm = reactive({ name: '', category: 'general', description: '', is_public: false })

function saveAsTemplate() {
  if (!promptContent.value.trim()) return
  Object.assign(saveForm, { name: '', category: 'general', description: '', is_public: false })
  saveDialogVisible.value = true
}

async function confirmSaveTemplate() {
  if (saving.value) return
  if (!saveForm.name.trim()) return ElMessage.warning('请输入模板名称')
  saving.value = true
  try {
    const template: PromptTemplate = await promptApi.create({ ...saveForm, content: promptContent.value })
    templateOptions.value.unshift({ id: template.id, name: template.name })
    ElMessage.success('已保存为模板')
    saveDialogVisible.value = false
  } catch {
    // 请求层已提示失败原因，保留表单方便重试。
  } finally {
    saving.value = false
  }
}

async function loadOptions() {
  optionsLoading.value = true
  optionsFailed.value = false
  try {
    const [models, templates] = await Promise.allSettled([
      modelApi.listConfigs({ model_type: 'chat' }),
      promptApi.list({ page_size: 100, include_public: true }),
    ])
    if (models.status === 'fulfilled') chatModels.value = models.value.filter(model => model.is_active)
    if (templates.status === 'fulfilled') templateOptions.value = templates.value.items || []
    optionsFailed.value = models.status === 'rejected' || templates.status === 'rejected'
  } finally {
    optionsLoading.value = false
  }
}

watch(() => route.query.template_id, value => {
  const id = Number(value)
  if (Number.isSafeInteger(id) && id > 0) {
    selectedTemplateId.value = id
    void loadTemplate(id)
  }
}, { immediate: true })

onMounted(loadOptions)
</script>

<style scoped>
.debug-back { display: inline-flex; align-items: center; gap: 6px; color: var(--ui-secondary); text-decoration: none; margin-bottom: 12px; }
.debug-back:hover { color: var(--ui-accent); }
.debug-layout { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }
.debug-output { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; }
.debug-panel { min-width: 0; }
.debug-panel > header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 16px; border-bottom: 1px solid var(--ui-border); min-height: 54px; }
.debug-panel h2 { font-size: 15px; }
.template-select { width: min(100%, 210px); }
.debug-content { padding: 16px; }
.code-editor :deep(textarea) { font-family: var(--ui-font-mono); line-height: 1.65; }
.debug-section { border-top: 1px solid var(--ui-border); padding-top: 12px; margin-top: 12px; }
.debug-section h3, .result-heading h3 { font-size: 14px; font-weight: 600; }
.debug-section h3 { margin-bottom: 8px; }
.render-preview { background: var(--ui-subtle); color: var(--ui-secondary); border-radius: var(--ui-radius-sm); padding: 12px; max-height: 180px; overflow: auto; font: 12px/1.65 var(--ui-font-mono); white-space: pre-wrap; overflow-wrap: anywhere; }
.missing-variables { margin-top: 6px; overflow-wrap: anywhere; }
.retry-options, .model-list { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; }
.model-list :deep(.el-checkbox) { margin-right: 0; max-width: 100%; }
.model-list :deep(.el-checkbox__label) { white-space: normal; overflow-wrap: anywhere; }
.model-result + .model-result { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--ui-border); }
.result-heading { display: flex; justify-content: space-between; align-items: start; gap: 8px; }
.result-heading h3 { overflow-wrap: anywhere; }
.result-metadata { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 6px; }
.result-content { margin-top: 12px; max-height: 540px; overflow: auto; overflow-wrap: anywhere; }
.result-error { color: var(--ui-danger); background: var(--ui-danger-soft); border-radius: var(--ui-radius-sm); margin-top: 12px; padding: 8px 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
.result-actions { margin-top: 8px; }
@media (max-width: 950px) { .debug-layout { grid-template-columns: minmax(0, 1fr); } }
</style>
