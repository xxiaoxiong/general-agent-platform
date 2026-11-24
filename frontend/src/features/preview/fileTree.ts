/**
 * 工作区文件清单 -> 项目文件树（VS Code 资源管理器式层级索引）：
 * 目录在前、文件在后，各自按名称排序；flattenFileTree 按展开集生成可见渲染行。
 */
import type { WorkspaceFile } from '@/api/conversations'

export interface FileTreeNode {
  /** 工作区相对路径（目录不带尾斜杠） */
  path: string
  name: string
  dir: boolean
  /** 仅文件节点携带的清单条目 */
  file?: WorkspaceFile
  children: FileTreeNode[]
}

export interface FileTreeRow {
  node: FileTreeNode
  /** 缩进层级（根为 0） */
  depth: number
}

/** 渲染行（模板直接消费的拍平视图模型） */
export interface FileTreeRowView {
  key: string
  name: string
  path: string
  depth: number
  kind: 'dir' | 'file'
  /** 目录行：是否展开 */
  expanded: boolean
  /** 文件行：清单条目 */
  file?: WorkspaceFile
}

export function buildFileTree(files: WorkspaceFile[]): FileTreeNode[] {
  const dirs = new Map<string, FileTreeNode>()
  const roots: FileTreeNode[] = []
  for (const file of files) {
    const parts = file.path.split('/').filter(Boolean)
    if (!parts.length) continue
    let level = roots
    let prefix = ''
    for (const part of parts.slice(0, -1)) {
      prefix = prefix ? `${prefix}/${part}` : part
      let node = dirs.get(prefix)
      if (!node) {
        node = { path: prefix, name: part, dir: true, children: [] }
        dirs.set(prefix, node)
        level.push(node)
      }
      level = node.children
    }
    level.push({ path: file.path, name: parts[parts.length - 1], dir: false, file, children: [] })
  }
  // 目录在前、文件在后，各自按名称排序（与 VS Code 资源管理器一致）
  const sortLevel = (nodes: FileTreeNode[]) => {
    nodes.sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name) : a.dir ? -1 : 1))
    for (const node of nodes) if (node.dir) sortLevel(node.children)
  }
  sortLevel(roots)
  return roots
}

/** 按展开集把树拍平为可见行（折叠目录的子树不出现） */
export function flattenFileTree(roots: FileTreeNode[], isExpanded: (path: string) => boolean): FileTreeRow[] {
  const rows: FileTreeRow[] = []
  const walk = (nodes: FileTreeNode[], depth: number) => {
    for (const node of nodes) {
      rows.push({ node, depth })
      if (node.dir && isExpanded(node.path)) walk(node.children, depth + 1)
    }
  }
  walk(roots, 0)
  return rows
}
