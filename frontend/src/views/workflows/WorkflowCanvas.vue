<template>
  <section ref="canvas" class="canvas-zone" tabindex="-1" aria-label="工作流画布" @dragover.prevent @drop.prevent="dropNode">
    <VueFlow
      :nodes="graphNodes"
      :edges="graphEdges"
      :min-zoom="0.2"
      :max-zoom="2"
      :fit-view-on-init="true"
      :select-nodes-on-drag="false"
      :delete-key-code="null"
      :edges-updatable="false"
      :connect-on-click="true"
      :connection-mode="ConnectionMode.Strict"
      :apply-default="false"
      @nodes-change="syncNodes"
      @edges-change="syncEdges"
      @connect="connect"
      @node-click="selectNode"
      @edge-click="selectEdge"
      @node-context-menu="openNodeMenu"
      @edge-context-menu="openEdgeMenu"
      @pane-context-menu="openCanvasMenu"
      @pane-click="clearSelection"
      @selection-end="focusCanvas"
    >
      <Background :gap="20" :size="1.8" pattern-color="var(--ui-border-strong)" />
      <template #node-step="node">
        <div class="flow-node" :data-status="node.data.status" :class="{ inspecting: selectedId === node.id }">
          <Handle type="target" :position="Position.Left" :is-valid-connection="canConnect" title="输入连接点" />
          <span class="flow-node-icon"><component :is="node.data.icon" :size="17" /></span>
          <div class="flow-node-content">
            <div class="flow-node-name" :title="node.data.label">{{ node.data.label }}</div>
            <div class="flow-node-type">{{ node.data.type }}</div>
          </div>
          <component
            :is="node.data.status === 'ok' ? CircleCheck : node.data.status === 'error' ? CircleAlert : CircleMinus"
            v-if="node.data.status" class="flow-node-status" :size="15"
            :aria-label="node.data.status === 'ok' ? '执行成功' : node.data.status === 'error' ? '执行失败' : '已跳过'"
          />
          <Handle type="source" :position="Position.Right" :is-valid-connection="canConnect" title="输出连接点" />
        </div>
      </template>
      <MiniMap v-if="minimap && nodes.length" class="flow-overview" position="bottom-right"
        :width="160" :height="102" node-color="var(--ui-accent-soft)" node-stroke-color="var(--ui-accent)"
        mask-color="color-mix(in srgb, var(--ui-bg) 70%, transparent)" pannable zoomable />
    </VueFlow>

    <div v-if="!nodes.length" class="canvas-empty">
      <Workflow :size="26" />
      <strong>画布暂无节点</strong>
      <span>从节点库添加节点，开始搭建流程。</span>
    </div>
    <div v-if="connectFrom" class="connect-hint" role="status">
      选择目标节点完成连线
      <button class="studio-icon-button" type="button" aria-label="取消连线" @click="connectFrom = ''"><X :size="15" /></button>
    </div>

    <div class="canvas-meta">{{ nodes.length }} 个节点 · {{ edges.length }} 条连接</div>
    <div class="canvas-controls" role="toolbar" aria-label="画布视图">
      <button class="studio-icon-button" type="button" title="缩小" aria-label="缩小" @click="zoomOut()"><Minus :size="16" /></button>
      <button class="canvas-zoom" type="button" title="重置缩放" @click="zoomTo(1)">{{ Math.round(viewport.zoom * 100) }}%</button>
      <button class="studio-icon-button" type="button" title="放大" aria-label="放大" @click="zoomIn()"><Plus :size="16" /></button>
      <span class="controls-divider" />
      <button class="studio-icon-button" type="button" title="适配视图" aria-label="适配视图" @click="fitGraph"><Maximize :size="15" /></button>
      <button class="studio-icon-button" type="button" title="切换缩略图" aria-label="切换缩略图" :aria-pressed="minimap" @click="minimap = !minimap"><MapIcon :size="16" /></button>
    </div>

    <Teleport to="body">
      <div v-if="menu" ref="menuElement" class="graph-menu" :style="{ left: menu.x + 'px', top: menu.y + 'px' }" @keydown.esc="closeMenu">
        <template v-if="menu.type === 'node'">
          <button type="button" @click="menuAction(() => locateNode(menu?.id ?? ''))">定位节点</button>
          <button type="button" @click="menuAction(startMenuConnection)">从此节点连线</button>
          <button type="button" @click="menuAction(() => removeConnections('target'))">删除所有入连线</button>
          <button type="button" @click="menuAction(() => removeConnections('source'))">删除所有出连线</button>
          <button type="button" class="graph-danger" @click="menuAction(() => deleteItems([menu?.id ?? ''], []))">删除节点</button>
        </template>
        <button v-else-if="menu.type === 'edge'" type="button" class="graph-danger" @click="menuAction(() => deleteItems([], [menu?.id ?? '']))">删除此连线</button>
        <template v-else>
          <button v-for="type in canvasMenuTypes" :key="type" type="button" @click="menuAction(() => addFromMenu(type))">
            添加{{ getNodeType(type)?.label }}节点
          </button>
        </template>
      </div>
    </Teleport>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { onClickOutside, useEventListener } from '@vueuse/core'
import { Background } from '@vue-flow/background'
import { MiniMap } from '@vue-flow/minimap'
import {
  ConnectionMode, Handle, MarkerType, Position, VueFlow, useVueFlow,
  type Connection, type Edge, type EdgeChange, type EdgeMouseEvent, type Node, type NodeChange, type NodeMouseEvent,
} from '@vue-flow/core'
import { CircleAlert, CircleCheck, CircleMinus, Map as MapIcon, Maximize, Minus, Plus, Workflow, X } from '@lucide/vue'
import { edgeEndpoints, nodePosition, type Point, type WorkflowEdge, type WorkflowNode } from './workflowGraph'
import { getNodeType } from './workflowNodes'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'

const props = defineProps<{
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  selectedId: string | null
  nodeResults?: Record<string, any>
  busy: boolean
}>()
const emit = defineEmits<{
  select: [id: string | null]
  move: [positions: Map<string, Point>]
  connect: [connection: Connection]
  create: [type: string, position: Point]
  remove: [nodeIds: string[], edgeIndices: number[]]
}>()
const {
  applyNodeChanges, applyEdgeChanges, fitView, zoomIn, zoomOut, zoomTo, viewport, findNode,
  screenToFlowCoordinate, getSelectedNodes, getSelectedEdges, removeSelectedElements, addSelectedNodes,
} = useVueFlow()
const canvas = ref<HTMLElement>()
const menuElement = ref<HTMLElement>()
const minimap = ref(true)
const connectFrom = ref('')
const menu = ref<{ type: 'node' | 'edge' | 'canvas'; x: number; y: number; point: Point; id: string }>()
const canvasMenuTypes = ['ai_chat', 'condition', 'knowledge_search', 'http']
const graphEdgeIds = new WeakMap<WorkflowEdge, string>()
let edgeSequence = 0

function graphEdgeId(edge: WorkflowEdge) {
  const existing = graphEdgeIds.get(edge)
  if (existing) return existing
  const id = `workflow-edge-${++edgeSequence}`
  graphEdgeIds.set(edge, id)
  return id
}

const graphNodes = computed<Node[]>(() => props.nodes.map(node => {
  const meta = getNodeType(node.type)
  const label = node.config?.label || node.label || meta?.label || node.type
  return {
    id: node.id,
    type: 'step',
    position: nodePosition(node),
    data: {
      type: meta && meta.label === label ? meta.description : meta?.label || node.type,
      icon: meta?.icon || Workflow,
      label,
      status: props.nodeResults?.[node.id]?.status,
    },
    draggable: !props.busy,
  }
}))
const graphEdges = computed<Edge[]>(() => {
  const ids = new Set(props.nodes.map(node => node.id))
  return props.edges.flatMap(edge => {
    const { source, target } = edgeEndpoints(edge)
    if (!ids.has(source) || !ids.has(target)) return []
    return [{
      id: graphEdgeId(edge), source, target, type: 'smoothstep',
      markerEnd: { type: MarkerType.ArrowClosed, color: 'var(--ui-muted)', width: 16, height: 16 },
    }]
  })
})

function syncNodes(changes: NodeChange[]) {
  applyNodeChanges(changes)
  const positions = new Map<string, Point>()
  for (const change of changes) {
    if (change.type !== 'position' || change.dragging) continue
    const position = findNode(change.id)?.position
    if (position) positions.set(change.id, position)
  }
  if (positions.size) emit('move', positions)
}

function syncEdges(changes: EdgeChange[]) {
  applyEdgeChanges(changes)
}

function canvasPoint(x: number, y: number) {
  const position = screenToFlowCoordinate({ x, y })
  return { x: Math.round(position.x - 86), y: Math.round(position.y - 28) }
}

function addAtCenter(type: string) {
  const rect = canvas.value?.getBoundingClientRect()
  if (!rect) return
  const selected = props.nodes.find(node => node.id === props.selectedId)
  if (selected) {
    const { x, y } = nodePosition(selected)
    emit('create', type, { x: x + 224, y })
  } else {
    emit('create', type, canvasPoint(rect.x + rect.width / 2, rect.y + rect.height / 2))
  }
}

function dropNode(event: DragEvent) {
  const type = event.dataTransfer?.getData('application/x-platform-workflow-node')
  if (type && getNodeType(type) && !props.busy) emit('create', type, canvasPoint(event.clientX, event.clientY))
}

function canConnect(connection: Connection) {
  return !props.busy && connection.source !== connection.target && !props.edges.some(edge => {
    const { source, target } = edgeEndpoints(edge)
    return source === connection.source && target === connection.target
  })
}

function connect(connection: Connection) {
  if (canConnect(connection)) emit('connect', connection)
}

function selectNode({ node }: NodeMouseEvent) {
  if (connectFrom.value) {
    connect({ source: connectFrom.value, target: node.id, sourceHandle: null, targetHandle: null })
    connectFrom.value = ''
  } else {
    emit('select', node.id)
  }
  closeMenu()
}

function selectEdge() {
  emit('select', null)
  closeMenu()
}

function clearSelection() {
  connectFrom.value = ''
  emit('select', null)
  closeMenu()
}

async function openMenu(event: MouseEvent, type: 'node' | 'edge' | 'canvas', id = '') {
  event.preventDefault()
  if (props.busy) return
  menu.value = {
    type, id, point: canvasPoint(event.clientX, event.clientY),
    x: Math.max(8, Math.min(event.clientX, window.innerWidth - 200)),
    y: Math.max(8, Math.min(event.clientY, window.innerHeight - (type === 'node' ? 188 : type === 'canvas' ? 150 : 48))),
  }
  await nextTick()
  menuElement.value?.querySelector('button')?.focus()
}

function openNodeMenu({ event, node }: NodeMouseEvent) {
  if (!(event instanceof MouseEvent)) return
  emit('select', node.id)
  void openMenu(event, 'node', node.id)
}

function openEdgeMenu({ event, edge }: EdgeMouseEvent) {
  if (event instanceof MouseEvent) void openMenu(event, 'edge', edge.id)
}

function openCanvasMenu(event: MouseEvent) {
  connectFrom.value = ''
  void openMenu(event, 'canvas')
}

function focusCanvas() {
  canvas.value?.focus({ preventScroll: true })
}

function menuAction(action: () => void) {
  action()
  closeMenu()
}

function closeMenu() {
  menu.value = undefined
}

function addFromMenu(type: string) {
  if (menu.value) emit('create', type, menu.value.point)
}

function startMenuConnection() {
  connectFrom.value = menu.value?.id ?? ''
  emit('select', null)
  focusCanvas()
}

function removeConnections(endpoint: 'source' | 'target') {
  if (!menu.value) return
  const edgeIds = props.edges.flatMap(edge => edgeEndpoints(edge)[endpoint] === menu.value?.id ? [graphEdgeId(edge)] : [])
  deleteItems([], edgeIds)
}

function deleteItems(nodeIds: string[], edgeIds: string[]) {
  const edgeIndices = props.edges.flatMap((edge, index) => edgeIds.includes(graphEdgeId(edge)) ? [index] : [])
  if (nodeIds.length || edgeIndices.length) emit('remove', nodeIds, edgeIndices)
}

function onKeyboard(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    connectFrom.value = ''
    closeMenu()
  }
  if (
    props.busy || !canvas.value?.contains(document.activeElement)
    || !['Delete', 'Backspace'].includes(event.key)
    || (event.target instanceof HTMLElement && event.target.closest('input, textarea, [contenteditable], button'))
  ) return

  const nodes = getSelectedNodes.value.map(node => node.id)
  const edges = getSelectedEdges.value.map(edge => edge.id)
  if (!nodes.length && !edges.length) return
  event.preventDefault()
  deleteItems(nodes, edges)
}

function locateNode(id: string) {
  const node = findNode(id)
  if (node) {
    removeSelectedElements()
    addSelectedNodes([node])
    void fitView({ nodes: [id], padding: 0.45, maxZoom: 1.18 })
  }
}

function fitGraph() {
  void fitView({ padding: 0.24, maxZoom: 1.18 })
}

useEventListener(window, 'keydown', onKeyboard)
onClickOutside(menuElement, closeMenu)
defineExpose({ addAtCenter, locateNode, fitGraph })
</script>

<style scoped>
.canvas-zone { position: relative; min-width: 0; min-height: 0; flex: 1; overflow: hidden; background: var(--ui-bg); }
.canvas-zone :deep(.vue-flow) {
  --vf-node-bg: var(--ui-surface);
  --vf-node-color: var(--ui-text);
  --vf-handle: var(--ui-muted);
  --vf-box-shadow: none;
  --vf-connection-path: var(--ui-accent);
  --vf-edge: var(--ui-muted);
  --vf-edge-selected: var(--ui-accent);
}
.canvas-zone :deep(.vue-flow__node:focus-visible) { outline: 2px solid var(--ui-accent); outline-offset: 4px; border-radius: var(--ui-radius-lg); }
.canvas-zone :deep(.vue-flow__edge-path) { stroke-width: 1.6; }
.canvas-zone :deep(.vue-flow__edge:hover .vue-flow__edge-path) { stroke: var(--ui-accent); stroke-width: 2.4; }
.canvas-zone :deep(.vue-flow__minimap) {
  background: var(--ui-surface); border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-lg); box-shadow: var(--ui-shadow-sm); overflow: hidden;
}
.flow-node {
  position: relative; display: flex; align-items: center; gap: 9px; width: 172px; min-height: 56px;
  padding: 8px 12px; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-md);
  color: var(--ui-text); background: var(--ui-surface); box-shadow: var(--ui-shadow-sm);
}
.flow-node:hover { border-color: var(--ui-muted); }
.flow-node.inspecting, .canvas-zone :deep(.selected .flow-node) { border-color: var(--ui-accent); box-shadow: 0 0 0 3px var(--ui-accent-soft); }
.flow-node-icon {
  display: grid; place-items: center; flex: 0 0 30px; height: 30px;
  border-radius: var(--ui-radius-sm); color: var(--ui-accent); background: var(--ui-accent-soft);
}
.flow-node-content { min-width: 0; flex: 1; }
.flow-node-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; font-weight: 650; line-height: 18px; }
.flow-node-type { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; color: var(--ui-secondary); }
.flow-node-status { flex-shrink: 0; color: var(--ui-muted); }
.flow-node[data-status='ok'] .flow-node-status { color: var(--ui-success); }
.flow-node[data-status='error'] .flow-node-status { color: var(--ui-danger); }
.flow-node :deep(.vue-flow__handle) {
  width: 10px; height: 10px; min-width: 10px; min-height: 10px; background: var(--ui-surface);
  border: 1.5px solid var(--ui-muted);
}
.flow-node :deep(.vue-flow__handle:hover), .flow-node :deep(.vue-flow__handle.valid) { border-color: var(--ui-accent); background: var(--ui-accent); }
.canvas-empty {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  display: grid; justify-items: center; gap: 8px; text-align: center; color: var(--ui-secondary); pointer-events: none;
  width: min(100% - 32px, 300px); font-size: 13px;
}
.canvas-empty strong { color: var(--ui-text); font-size: 15px; }
.canvas-meta {
  position: absolute; top: 12px; left: 12px; padding: 5px 8px; border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-sm); background: var(--ui-surface); color: var(--ui-secondary); font-size: 12px; pointer-events: none;
}
.canvas-controls, .connect-hint {
  display: flex; align-items: center; gap: 2px; padding: 3px;
  background: var(--ui-surface); border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-md); box-shadow: var(--ui-shadow-sm);
}
.canvas-controls { position: absolute; left: 12px; bottom: 12px; }
.connect-hint { position: absolute; top: 12px; left: 50%; transform: translateX(-50%); padding-left: 10px; white-space: nowrap; font-size: 12px; }
.controls-divider { width: 1px; height: 17px; margin: 0 3px; background: var(--ui-border); }
.studio-icon-button, .canvas-zoom {
  display: inline-flex; align-items: center; justify-content: center; min-width: 30px; height: 30px;
  border: 0; border-radius: var(--ui-radius-sm); background: transparent; color: var(--ui-secondary); cursor: pointer;
}
.canvas-zoom { width: 46px; font-size: 12px; font-variant-numeric: tabular-nums; }
.studio-icon-button:hover, .studio-icon-button[aria-pressed='true'], .canvas-zoom:hover { color: var(--ui-text); background: var(--ui-hover); }
.graph-menu {
  position: fixed; z-index: var(--ui-z-popover, 2100); display: grid; min-width: 186px; padding: 4px;
  background: var(--ui-surface); color: var(--ui-text); border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-md); box-shadow: var(--ui-shadow-md);
}
.graph-menu button { text-align: left; background: transparent; border: 0; border-radius: var(--ui-radius-sm); padding: 7px 10px; color: inherit; font: inherit; font-size: 13px; cursor: pointer; }
.graph-menu button:hover { background: var(--ui-hover); }
.graph-menu .graph-danger { color: var(--ui-danger); }
.graph-menu .graph-danger:hover { background: var(--ui-danger-soft); }
@media (max-width: 780px) { .flow-overview { display: none; } }
</style>
