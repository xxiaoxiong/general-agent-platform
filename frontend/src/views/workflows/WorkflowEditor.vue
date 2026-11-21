<template>
  <div class="studio">
    <header class="studio-toolbar">
      <div class="workflow-heading">
        <UiButton as-child variant="ghost" size="icon">
          <router-link to="/workflows" title="流程列表" aria-label="返回流程列表"><ArrowLeft /></router-link>
        </UiButton>
        <div class="workflow-title">
          <el-input v-if="workflow" v-model="workflow.name" aria-label="工作流名称" maxlength="100"
            placeholder="工作流名称" :disabled="busy" @input="markDirty" />
          <span v-else>工作流编辑器</span>
          <div v-if="workflow" class="workflow-meta">
            <span>v{{ workflow.version }}</span>
            <span>{{ workflow.is_published ? '已发布' : '草稿' }}</span>
            <span class="save-state" :class="{ 'save-error': saveError, 'save-dirty': dirty }" role="status">
              <span class="save-dot" />{{ saveLabel }}
            </span>
          </div>
        </div>
      </div>

      <div v-if="workflow" class="workflow-actions">
        <UiButton size="sm" :loading="saving" :disabled="publishing || testing" @click="saveWorkflow"><Save v-if="!saving" />保存</UiButton>
        <UiButton size="sm" :loading="publishing" :disabled="saving || testing" @click="publishWorkflow">
          <RadioTower v-if="!publishing" />
          {{ workflow.is_published ? '取消发布' : '发布' }}
        </UiButton>
        <UiButton size="sm" :disabled="busy" @click="router.push(`/workflows/${workflowId}/run`)"><Play />运行</UiButton>
        <UiButton size="sm" :disabled="busy" @click="openPublishAsApp"><PackagePlus />发布为应用</UiButton>
        <UiButton size="sm" variant="primary" :disabled="busy" @click="openDebugPanel"><FlaskConical />调试</UiButton>
      </div>
    </header>

    <div v-if="loading" class="studio-status" role="status"><LoaderCircle :size="21" class="studio-spin" /><strong>正在加载工作流</strong></div>
    <div v-else-if="loadError" class="studio-status" role="alert">
      <strong>工作流加载失败</strong>
      <span>请检查访问权限或网络连接后重试。</span>
      <UiButton @click="loadWorkflow">重试</UiButton>
    </div>

    <div v-else-if="workflow" class="studio-body">
      <aside class="node-library" :class="{ 'library-open': mobileLibraryOpen }" aria-label="节点库">
        <div class="panel-heading">
          <div><h2>节点库</h2><p>点击添加，或拖入画布</p></div>
          <button type="button" class="studio-icon-button library-close" aria-label="关闭节点库" @click="mobileLibraryOpen = false"><X :size="16" /></button>
        </div>
        <div class="library-search">
          <el-input v-model="search" clearable :prefix-icon="Search" aria-label="搜索节点" placeholder="搜索节点" />
        </div>
        <div class="library-groups">
          <section v-for="group in nodeGroups" :key="group.name">
            <h3>{{ group.name }}</h3>
            <button v-for="node in group.nodes" :key="node.type" type="button" class="library-node"
              :draggable="!busy" :disabled="busy" @click="addAtCenter(node.type)"
              @dragstart="dragNode($event, node.type)">
              <span class="library-icon"><component :is="node.icon" :size="16" /></span>
              <span class="library-label">{{ node.label }}<small>{{ node.description }}</small></span>
              <Plus :size="14" class="library-add" />
            </button>
          </section>
          <p v-if="!nodeGroups.length" class="panel-hint">找不到匹配的节点</p>
        </div>
      </aside>

      <button type="button" class="library-launch studio-icon-button" aria-label="打开节点库" title="打开节点库"
        :aria-expanded="mobileLibraryOpen" @click="mobileLibraryOpen = !mobileLibraryOpen"><PanelLeftOpen :size="16" /></button>
      <WorkflowCanvas ref="canvas" :nodes="nodes" :edges="edges" :selected-id="selectedId"
        :node-results="debugResult?.node_results" :busy="busy"
        @select="selectNode" @move="moveNodes" @connect="createConnection" @create="createNode" @remove="removeItems" />

      <aside class="inspector" :class="{ 'inspector-empty': !selectedNode }" aria-label="节点配置">
        <header v-if="selectedNode" class="panel-heading inspector-heading">
          <span class="inspector-icon"><component :is="getNodeType(selectedNode.type)?.icon || WorkflowIcon" :size="18" /></span>
          <div>
            <h2>{{ nodeLabels[selectedNode.id] }}</h2>
            <p>{{ getNodeType(selectedNode.type)?.label || selectedNode.type }} · 节点配置</p>
          </div>
          <button type="button" class="studio-icon-button" title="定位到画布" aria-label="定位到画布" @click="canvas?.locateNode(selectedNode.id)">
            <LocateFixed :size="16" />
          </button>
          <button type="button" class="studio-icon-button" aria-label="关闭节点配置" @click="selectNode(null)"><X :size="16" /></button>
        </header>
        <div v-if="selectedNode" class="inspector-body">
          <fieldset :disabled="busy">
            <WorkflowNodeConfig :key="selectedNode.id" :selected-node="selectedNode" :nodes="nodes"
              :chat-models="chatModels" :knowledge-bases="knowledgeBases" :prompt-templates="promptTemplates" @change="markDirty" />
          </fieldset>
          <section class="node-links">
            <h3>节点连接</h3>
            <p v-if="!connections.length" class="panel-hint">还没有连线。拖动节点右侧端口连接到目标节点。</p>
            <div v-for="link in connections" :key="link.key" class="node-link">
              <span>{{ link.label }}</span>
              <button type="button" @click="inspectConnectedNode(link.nodeId)">{{ nodeLabels[link.nodeId] || link.nodeId }}</button>
            </div>
            <code :title="selectedNode.id">{{ selectedNode.id }}</code>
          </section>
        </div>
        <div v-else class="inspector-guide">
          <header><WorkflowIcon :size="17" /><h2>节点配置</h2></header>
          <p>选中画布中的节点，编辑参数和查看连接。</p>
          <dl>
            <div><dt>平移</dt><dd>拖动画布</dd></div>
            <div><dt>框选</dt><dd><kbd>Shift</kbd> + 拖动</dd></div>
            <div><dt>连线</dt><dd>从右侧端口拖向左侧端口</dd></div>
          </dl>
        </div>
      </aside>
    </div>

    <el-drawer v-model="debugOpen" title="调试工作流" :size="'min(520px, 100vw)'" :modal="false" :close-on-click-modal="false">
      <div v-if="testing" class="studio-status compact" role="status">
        <LoaderCircle :size="22" class="studio-spin" /><strong>正在执行工作流</strong>
        <span>节点结果会在执行结束后显示。</span>
      </div>
      <WorkflowResults v-else-if="debugResult" :result="debugResult" :node-labels="nodeLabels">
        <template #actions><el-button size="small" text @click="debugResult = undefined">修改输入</el-button></template>
      </WorkflowResults>
      <div v-else class="debug-input">
        <h3>运行参数</h3>
        <p v-if="dirty" class="panel-hint">运行调试时将先保存当前编辑。</p>
        <p v-if="debugError" class="failure-notice" role="alert">{{ debugError }}</p>
        <WorkflowInputs v-model="debugInput" :fields="fields" />
      </div>
      <template #footer>
        <div class="form-actions">
          <UiButton @click="debugOpen = false">收起</UiButton>
          <UiButton v-if="!debugResult" variant="primary" :disabled="saving || publishing" :loading="testing" @click="runDebug"><Play v-if="!testing" />运行调试</UiButton>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="publishAppOpen" title="发布为应用" width="min(480px, calc(100vw - 32px))" :close-on-click-modal="!publishing">
      <p class="dialog-description">发布后，可在应用管理中运行此工作流。</p>
      <el-form :model="publishAppForm" label-position="top">
        <el-form-item label="应用名称"><el-input v-model="publishAppForm.name" placeholder="应用展示名称" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="publishAppForm.description" type="textarea" :rows="3" placeholder="简要描述应用的用途" /></el-form-item>
        <el-form-item label="公开访问"><el-switch v-model="publishAppForm.is_public" aria-label="公开访问" /></el-form-item>
      </el-form>
      <template #footer>
        <div class="form-actions">
          <UiButton :disabled="publishing" @click="publishAppOpen = false">取消</UiButton>
          <UiButton variant="primary" :loading="publishing" @click="publishAsApp">发布</UiButton>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import type { RouteLocationNormalized } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { confirmDestructive } from '@/lib/confirmation'
import { ArrowLeft, FlaskConical, LoaderCircle, LocateFixed, PackagePlus, PanelLeftOpen, Play, Plus, RadioTower, Save, Search, Workflow as WorkflowIcon, X } from '@lucide/vue'
import type { Connection } from '@vue-flow/core'
import { appApi, knowledgeApi, modelApi, promptApi, workflowApi } from '@/api'
import UiButton from '@/components/ui/UiButton.vue'
import type { KnowledgeBase, ModelConfig, Workflow as WorkflowRecord, WorkflowExecution } from '@/types'
import WorkflowCanvas from './WorkflowCanvas.vue'
import WorkflowInputs from './WorkflowInputs.vue'
import WorkflowNodeConfig from './WorkflowNodeConfig.vue'
import WorkflowResults from './WorkflowResults.vue'
import { edgeEndpoints, inputFields, moveNode, nodePosition, replaceGraph, type Point, type WorkflowEdge, type WorkflowNode } from './workflowGraph'
import { getNodeType, nodeTypes, type WorkflowPromptTemplate } from './workflowNodes'

const route = useRoute()
const router = useRouter()
const workflowId = computed(() => Number(route.params.id))
const canvas = ref<InstanceType<typeof WorkflowCanvas>>()
const workflow = ref<WorkflowRecord>()
const nodes = ref<(WorkflowNode & { config: Record<string, any> })[]>([])
const edges = ref<WorkflowEdge[]>([])
const selectedId = ref<string | null>(null)
const selectedNode = computed(() => nodes.value.find(node => node.id === selectedId.value))
const loading = ref(false)
const loadError = ref(false)
const saving = ref(false)
const dirty = ref(false)
const saveError = ref(false)
const publishing = ref(false)
const testing = ref(false)
const busy = computed(() => saving.value || publishing.value || testing.value)
const search = ref('')
const mobileLibraryOpen = ref(false)
const chatModels = ref<ModelConfig[]>([])
const knowledgeBases = ref<KnowledgeBase[]>([])
const promptTemplates = ref<WorkflowPromptTemplate[]>([])
let nodeCounter = 0

const debugOpen = ref(false)
const debugInput = ref<Record<string, string>>({})
const debugResult = ref<WorkflowExecution>()
const debugError = ref('')
const fields = computed(() => inputFields(nodes.value))
const saveLabel = computed(() => saving.value ? '保存中' : saveError.value ? '保存失败' : dirty.value ? '有未保存的更改' : '已保存')
const nodeLabels = computed(() => Object.fromEntries(nodes.value.map(node => [
  node.id, node.config.label || node.label || getNodeType(node.type)?.label || node.type,
])))
const nodeGroups = computed(() => {
  const filter = search.value.trim().toLowerCase()
  const groups = new Map<string, typeof nodeTypes>()
  for (const node of nodeTypes) {
    if (!`${node.type} ${node.label} ${node.description}`.toLowerCase().includes(filter)) continue
    const group = groups.get(node.group) || []
    group.push(node)
    groups.set(node.group, group)
  }
  return [...groups].map(([name, nodes]) => ({ name, nodes }))
})
const connections = computed(() => {
  const node = selectedNode.value
  if (!node) return []
  let outgoing = 0
  return edges.value.flatMap((edge, index) => {
    const { source, target } = edgeEndpoints(edge)
    if (source === node.id) {
      const branch = outgoing++
      const label = node.type === 'condition' && branch < 2 ? (branch === 0 ? 'True 出口' : 'False 出口') : '输出到'
      return [{ key: index, label, nodeId: target }]
    }
    return target === node.id ? [{ key: index, label: '来自', nodeId: source }] : []
  })
})

function markDirty() {
  dirty.value = true
  saveError.value = false
}

function selectNode(id: string | null) {
  selectedId.value = id
}

function inspectConnectedNode(id: string) {
  if (!nodes.value.some(node => node.id === id)) return
  selectNode(id)
  canvas.value?.locateNode(id)
}

function dragNode(event: DragEvent, type: string) {
  if (!event.dataTransfer) return
  event.dataTransfer.effectAllowed = 'copy'
  event.dataTransfer.setData('application/x-platform-workflow-node', type)
}

function addAtCenter(type: string) {
  mobileLibraryOpen.value = false
  canvas.value?.addAtCenter(type)
}

function createNode(type: string, position: Point) {
  const nodeType = getNodeType(type)
  if (!nodeType || busy.value) return
  const id = `node_${Date.now()}_${++nodeCounter}`
  nodes.value.push({ id, type, config: { label: nodeType.label }, position })
  selectNode(id)
  markDirty()
}

function moveNodes(positions: Map<string, Point>) {
  let changed = false
  nodes.value = nodes.value.map(node => {
    const position = positions.get(node.id)
    const previous = nodePosition(node)
    if (!position || (previous.x === position.x && previous.y === position.y)) return node
    changed = true
    return moveNode(node, position)
  })
  if (changed) markDirty()
}

function createConnection({ source, target }: Connection) {
  if (!source || !target || busy.value) return
  edges.value.push({ id: `edge_${source}_${target}`, source, target })
  markDirty()
}

async function removeItems(nodeIds: string[], edgeIndices: number[]) {
  if (busy.value) return
  const nodesToRemove = new Set(nodeIds)
  const linksToRemove = new Set(edgeIndices.map(index => edges.value[index]))
  try {
    await confirmDestructive(nodeIds.length ? `将删除 ${nodeIds.length} 个节点及其连线。` : `将删除 ${edgeIndices.length} 条连线。`,
      '删除流程元素')
  } catch { return }
  nodes.value = nodes.value.filter(node => !nodesToRemove.has(node.id))
  edges.value = edges.value.filter(edge => {
    const { source, target } = edgeEndpoints(edge)
    return !linksToRemove.has(edge) && !nodesToRemove.has(source) && !nodesToRemove.has(target)
  })
  if (selectedId.value && nodesToRemove.has(selectedId.value)) selectNode(null)
  markDirty()
}

async function saveWorkflow() {
  if (!workflow.value || saving.value) return false
  if (!workflow.value.name.trim()) {
    ElMessage.warning('请输入工作流名称')
    return false
  }
  saving.value = true
  saveError.value = false
  try {
    const definition = replaceGraph(workflow.value.definition || {}, nodes.value, edges.value)
    workflow.value = await workflowApi.update(workflowId.value, { name: workflow.value.name, definition })
    dirty.value = false
    ElMessage.success('工作流已保存')
    return true
  } catch {
    saveError.value = true
    return false
  } finally {
    saving.value = false
  }
}

async function publishWorkflow() {
  if (!workflow.value || busy.value) return
  const publish = !workflow.value.is_published
  try {
    await ElMessageBox.confirm(publish ? '发布后，此工作流可用于应用。' : '确定取消工作流的发布状态？',
      publish ? '发布工作流' : '取消发布', { confirmButtonText: publish ? '发布' : '取消发布', cancelButtonText: '返回' })
  } catch { return }
  publishing.value = true
  try {
    if (publish && dirty.value && !await saveWorkflow()) return
    const updated = await workflowApi.update(workflowId.value, { is_published: publish })
    workflow.value.is_published = updated.is_published
    ElMessage.success(publish ? '已发布' : '已取消发布')
  } catch {
    // 请求层提供失败反馈。
  } finally {
    publishing.value = false
  }
}

const publishAppOpen = ref(false)
const publishAppForm = reactive({ name: '', description: '', is_public: false })

function openPublishAsApp() {
  if (!workflow.value) return
  Object.assign(publishAppForm, {
    name: workflow.value.name, description: workflow.value.description || '',
    is_public: false,
  })
  publishAppOpen.value = true
}

async function publishAsApp() {
  if (!workflow.value || busy.value) return
  if (!publishAppForm.name.trim()) return ElMessage.warning('请输入应用名称')
  publishing.value = true
  try {
    if (dirty.value && !await saveWorkflow()) return
    if (!workflow.value.is_published) {
      const updated = await workflowApi.update(workflowId.value, { is_published: true })
      workflow.value.is_published = updated.is_published
    }
    await appApi.createInstance({
      template_id: 1, name: publishAppForm.name, description: publishAppForm.description,
      workflow_id: workflowId.value, is_public: publishAppForm.is_public,
    })
    ElMessage.success('已发布为应用')
    publishAppOpen.value = false
  } catch {
    // 请求层提供失败反馈。
  } finally {
    publishing.value = false
  }
}

function openDebugPanel() {
  debugInput.value = Object.fromEntries(fields.value.map(field => [field.key, debugInput.value[field.key] ?? '']))
  debugError.value = ''
  debugOpen.value = true
}

async function runDebug() {
  if (busy.value) return
  testing.value = true
  debugError.value = ''
  debugResult.value = undefined
  try {
    if (dirty.value && !await saveWorkflow()) {
      debugError.value = '工作流未能保存，请检查编辑内容后重试。'
      return
    }
    debugResult.value = await workflowApi.execute(workflowId.value, { input_data: debugInput.value })
  } catch {
    debugError.value = '调试请求失败，请重试。'
  } finally {
    testing.value = false
  }
}

async function loadWorkflow() {
  const id = workflowId.value
  loading.value = true
  loadError.value = false
  selectedId.value = null
  dirty.value = false
  saveError.value = false
  debugOpen.value = false
  debugResult.value = undefined
  debugInput.value = {}
  try {
    const data = await workflowApi.get(id)
    if (id !== workflowId.value) return
    workflow.value = data
    nodes.value = (data.definition?.nodes || []).map((node: WorkflowNode) => ({ ...node, config: node.config || {} }))
    edges.value = data.definition?.edges || []
  } catch {
    if (id === workflowId.value) loadError.value = true
  } finally {
    if (id === workflowId.value) loading.value = false
  }
}

async function confirmLeave(to: RouteLocationNormalized) {
  if (to.name === 'Login' && !localStorage.getItem('access_token')) return true
  if (busy.value) return false
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('离开编辑器会丢失未保存的更改。', '离开工作流', {
      type: 'warning', confirmButtonText: '离开', cancelButtonText: '继续编辑',
    })
    return true
  } catch { return false }
}

onBeforeRouteLeave(confirmLeave)
onBeforeRouteUpdate(confirmLeave)
watch(workflowId, loadWorkflow, { immediate: true })
onMounted(async () => {
  const results = await Promise.allSettled([
    modelApi.listConfigs({ model_type: 'chat' }),
    knowledgeApi.listBases({ page_size: 100 }),
    promptApi.list({ page_size: 100, include_public: true }),
  ] as const)
  const [models, knowledge, prompts] = results
  if (models.status === 'fulfilled') chatModels.value = models.value.filter(model => model.model_type === 'chat' && model.is_active)
  if (knowledge.status === 'fulfilled') knowledgeBases.value = knowledge.value.items
  if (prompts.status === 'fulfilled') promptTemplates.value = prompts.value.items
})
</script>

<style scoped>
.studio { display: flex; flex-direction: column; height: calc(100dvh - var(--ui-header-height, 52px)); min-height: 470px; background: var(--ui-bg); }
.studio-toolbar {
  position: relative; z-index: 2; display: flex; flex: none; align-items: center; justify-content: space-between;
  flex-wrap: wrap; gap: 8px 16px; padding: 10px 16px; min-height: 65px;
  border-bottom: 1px solid var(--ui-border); background: var(--ui-surface);
}
.workflow-heading,.workflow-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-width: 0; }
.workflow-heading { flex-wrap: nowrap; gap: 8px; }
.workflow-title { min-width: 0; width: min(235px, 66vw); }
.workflow-title :deep(.el-input__wrapper) { box-shadow: none; padding: 0 5px; background: transparent; }
.workflow-title :deep(.el-input__wrapper:hover),.workflow-title :deep(.el-input__wrapper.is-focus) { box-shadow: inset 0 0 0 1px var(--ui-border-strong); }
.workflow-title :deep(.el-input__inner) { height: 29px; font-size: 16px; font-weight: 650; }
.workflow-meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding-inline: 5px; color: var(--ui-secondary); font-size: 11px; }
.save-state { display: inline-flex; align-items: center; gap: 5px; color: var(--ui-secondary); }
.save-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--ui-success); }
.save-error .save-dot { background: var(--ui-danger); }
.save-dirty .save-dot { background: var(--ui-warning); }
.form-actions { display: flex; justify-content: flex-end; gap: 8px; }
.studio-body { display: flex; flex: 1; min-height: 0; min-width: 0; position: relative; overflow: hidden; }
.node-library,.inspector { display: flex; flex-direction: column; flex: none; min-height: 0; background: var(--ui-surface); }
.node-library { width: 215px; border-right: 1px solid var(--ui-border); }
.inspector { width: 294px; border-left: 1px solid var(--ui-border); }
.panel-heading { display: flex; align-items: center; gap: 8px; min-width: 0; padding: 14px 12px; }
.panel-heading h2 { margin: 0; font-size: 14px; font-weight: 650; overflow-wrap: anywhere; }
.panel-heading p { margin: 3px 0 0; color: var(--ui-secondary); font-size: 12px; overflow-wrap: anywhere; }
.panel-heading > div { flex: 1; min-width: 0; }
.library-search { padding: 0 12px 10px; }
.library-groups,.inspector-body { overflow: auto; }
.library-groups { padding: 0 7px 12px; }
.library-groups h3 { margin: 10px 5px 5px; color: var(--ui-secondary); font-size: 11px; font-weight: 650; }
.library-node {
  display: flex; align-items: center; gap: 8px; width: 100%; min-height: 47px; padding: 5px;
  border: 1px solid transparent; background: transparent; color: var(--ui-text);
  border-radius: var(--ui-radius-md); font: inherit; text-align: left; cursor: grab;
}
.library-node:hover,.library-node:focus-visible { background: var(--ui-hover); border-color: var(--ui-border); }
.library-node:disabled { opacity: 0.6; cursor: default; }
.library-icon,.inspector-icon {
  flex: 0 0 30px; height: 30px; display: grid; place-items: center;
  color: var(--ui-accent); background: var(--ui-accent-soft); border-radius: var(--ui-radius-md);
}
.library-label { display: grid; gap: 2px; flex: 1; min-width: 0; font-size: 12px; font-weight: 600; }
.library-label small { color: var(--ui-secondary); font-weight: 400; font-size: 11px; line-height: 1.35; }
.library-add { flex: none; color: var(--ui-muted); visibility: hidden; }
.library-node:hover .library-add,.library-node:focus-visible .library-add { visibility: visible; }
.inspector-heading { border-bottom: 1px solid var(--ui-border); }
.inspector-body { padding: 14px; }
.inspector-body fieldset { margin: 0; padding: 0; min-width: 0; border: 0; }
.inspector-body fieldset:disabled { pointer-events: none; opacity: .7; }
.inspector-guide { padding: 14px; color: var(--ui-secondary); font-size: 12px; }
.inspector-guide header { display: flex; align-items: center; gap: 8px; color: var(--ui-text); }
.inspector-guide h2 { margin: 0; font-size: 14px; }
.inspector-guide p { margin: 12px 0 16px; }
.inspector-guide dl { display: grid; gap: 9px; margin: 0; padding-top: 12px; border-top: 1px solid var(--ui-border); }
.inspector-guide dl div { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
.inspector-guide dd { margin: 0; text-align: right; }
.inspector-guide kbd { padding-inline: 4px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); font: 11px var(--ui-font-mono); }
.node-links { border-top: 1px solid var(--ui-border); padding-top: 12px; }
.node-links h3,.debug-input h3 { font-size: 13px; margin: 0 0 10px; }
.node-link { display: grid; grid-template-columns: 62px minmax(0, 1fr); align-items: baseline; gap: 4px; margin-bottom: 6px; color: var(--ui-secondary); font-size: 12px; }
.node-link button {
  width: fit-content; max-width: 100%; border: 0; background: transparent; color: var(--ui-accent);
  font: inherit; padding: 2px; text-align: left; cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.node-link button:hover { text-decoration: underline; }
.node-links code { display: block; margin-top: 12px; color: var(--ui-secondary); font: 11px/1.5 var(--ui-font-mono); overflow-wrap: anywhere; }
.panel-hint,.dialog-description { color: var(--ui-secondary); font-size: 13px; line-height: 1.5; margin: 0 0 12px; }
.library-groups .panel-hint { padding: 8px; }
.studio-icon-button {
  display: inline-flex; align-items: center; justify-content: center; flex: none;
  width: 30px; height: 30px; border: 0; border-radius: var(--ui-radius-sm);
  background: transparent; color: var(--ui-secondary); cursor: pointer;
}
.studio-icon-button:hover { background: var(--ui-hover); color: var(--ui-text); }
.library-launch,.library-close { display: none; }
.studio-status {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 11px;
  padding: 24px; text-align: center; color: var(--ui-secondary); font-size: 13px;
}
.studio-status strong { color: var(--ui-text); font-size: 15px; }
.studio-status.compact { min-height: 200px; }
.studio-spin { color: var(--ui-accent); animation: studio-spin 1s linear infinite; }
@keyframes studio-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .studio-spin { animation: none; } }
.failure-notice { color: var(--ui-danger); padding: 10px; background: var(--ui-danger-soft); border-radius: var(--ui-radius-md); }
@media (max-width: 1050px) {
  .node-library { width: 174px; }
  .inspector { width: 260px; }
  .library-label small { display: none; }
  .library-node { min-height: 40px; }
}
@media (max-width: 1100px) {
  .inspector { position: absolute; z-index: 3; inset: 0 0 0 auto; width: min(280px, 76%); box-shadow: var(--ui-shadow-md); }
  .inspector-empty { display: none; }
}
@media (max-width: 680px) {
  .studio-toolbar { padding: 9px; gap: 7px; }
  .workflow-actions { gap: 4px; }
  .workflow-actions :deep(button) { padding-inline: 7px; font-size: 12px; }
  .node-library { display: none; }
  .node-library.library-open { display: flex; z-index: 4; position: absolute; inset: 0 auto 0 0; width: min(220px, 75%); box-shadow: var(--ui-shadow-md); }
  .library-close,.library-launch { display: inline-flex; }
  .library-launch { position: absolute; z-index: 2; top: 12px; right: 12px; background: var(--ui-surface); border: 1px solid var(--ui-border); }
}
</style>
