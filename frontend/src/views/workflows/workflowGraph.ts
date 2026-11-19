export interface Point { x: number; y: number }

export interface WorkflowNode {
  id: string
  type: string
  label?: string
  config?: Record<string, any>
  position?: Point
  x?: number
  y?: number
  [key: string]: unknown
}

export interface WorkflowEdge {
  id?: string
  from?: string
  to?: string
  source?: string
  target?: string
  [key: string]: unknown
}

export interface WorkflowInputField {
  key: string
  label?: string
  type?: 'text' | 'textarea' | 'select'
  placeholder?: string
  options?: string
}

export function inputFields(nodes: WorkflowNode[]): WorkflowInputField[] {
  const explicit = nodes.find(node => node.type === 'start')?.config?.input_fields
  if (Array.isArray(explicit) && explicit.length) return explicit

  const keys = new Set<string>()
  for (const node of nodes) {
    for (const value of Object.values(node.config || {})) {
      if (typeof value !== 'string') continue
      for (const match of value.matchAll(/\{\{(\w+)\}\}/g)) keys.add(match[1])
    }
  }
  return [...keys].map(key => ({ key, placeholder: '请输入 ' + key }))
}

export function nodePosition(node: WorkflowNode): Point {
  return { x: node.position?.x ?? node.x ?? 100, y: node.position?.y ?? node.y ?? 100 }
}

export function edgeEndpoints(edge: WorkflowEdge) {
  // 执行引擎优先读取旧字段，画布必须遵循相同顺序。
  return { source: edge.from || edge.source || '', target: edge.to || edge.target || '' }
}

export function replaceGraph(
  definition: Record<string, unknown>,
  nodes: WorkflowNode[],
  edges: WorkflowEdge[],
) {
  return { ...definition, nodes, edges }
}

export function moveNode<T extends WorkflowNode>(node: T, position: Point): T {
  const moved = { ...node, position: { ...node.position, ...position } }
  if ('x' in node) moved.x = position.x
  if ('y' in node) moved.y = position.y
  return moved
}
