import {
  Activity, Boxes, Braces, Cpu, Database, FileClock, FlaskConical, LibraryBig, MessageSquare,
  PanelsTopLeft, Plug, SquarePen, Store, UsersRound, Workflow,
} from '@lucide/vue'
import type { Component } from 'vue'
import type { Router } from 'vue-router'

interface Entry {
  name: string
  icon: Component
  keywords: string
}

export interface NavigationEntry extends Entry {
  title: string
  path: string
}

export interface NavigationGroup {
  title: string
  entries: NavigationEntry[]
}

const groups: { title: string; entries: Entry[] }[] = [
  { title: '工作空间', entries: [
    { name: 'Workspace', icon: MessageSquare, keywords: '聊天 对话 会话 agent chat' },
    { name: 'Dashboard', icon: PanelsTopLeft, keywords: '首页 工作台 dashboard' },
    { name: 'AppMarket', icon: Store, keywords: '应用市场 商店 marketplace' },
    { name: 'AppInstances', icon: Boxes, keywords: '部署 实例 apps' },
  ] },
  { title: '构建与资源', entries: [
    { name: 'Workflows', icon: Workflow, keywords: '流程 工作流 编排 workflow studio' },
    { name: 'Knowledge', icon: LibraryBig, keywords: 'RAG 文档 文件 knowledge' },
    { name: 'PromptList', icon: SquarePen, keywords: '模板 prompts' },
    { name: 'PromptDebugger', icon: FlaskConical, keywords: '模板 调试 prompt debugger' },
    { name: 'Models', icon: Cpu, keywords: '供应商 模型 LLM model provider' },
    { name: 'DatabaseList', icon: Database, keywords: 'SQL 数据源 database' },
    { name: 'SystemSkills', icon: Braces, keywords: '技能 扩展 skill' },
    { name: 'SystemMcp', icon: Plug, keywords: '扩展 工具 MCP' },
  ] },
  { title: '运行与评测', entries: [
    { name: 'SystemEvaluation', icon: Activity, keywords: '运行 评估 发展 自进化 轨迹 trace evaluation evolution' },
  ] },
  { title: '系统管理', entries: [
    { name: 'SystemUsers', icon: UsersRound, keywords: '账号 权限 RBAC users' },
    { name: 'SystemAudit', icon: FileClock, keywords: '审计 记录 日志 audit' },
  ] },
]

export function getNavigation(router: Router, isSuperuser: boolean): NavigationGroup[] {
  return groups.map(group => ({
    title: group.title,
    entries: group.entries.flatMap(entry => {
      const route = router.resolve({ name: entry.name })
      if (route.meta.requiresSuperuser && !isSuperuser) return []
      return [{ ...entry, title: String(route.meta.title), path: route.path }]
    }),
  })).filter(group => group.entries.length)
}
