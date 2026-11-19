import {
  Bot, Braces, CirclePlay, CircleStop, Clock3, Database, DatabaseZap, FileText, GitBranch,
  GitFork, Globe, Mail, Repeat2, Search, Terminal, Waypoints, Cable,
} from '@lucide/vue'

export interface WorkflowPromptTemplate {
  id: number
  name: string
  category: string
  content: string
}

export const nodeTypes = [
  { type: 'start', label: '开始', icon: CirclePlay, group: '流程控制', description: '定义流程入口与输入' },
  { type: 'end', label: '结束', icon: CircleStop, group: '流程控制', description: '结束流程执行' },
  { type: 'condition', label: '条件判断', icon: GitBranch, group: '流程控制', description: '按条件选择分支' },
  { type: 'loop', label: '循环', icon: Repeat2, group: '流程控制', description: '遍历列表执行' },
  { type: 'parallel', label: '并行', icon: GitFork, group: '流程控制', description: '并行执行子节点' },
  { type: 'delay', label: '延时等待', icon: Clock3, group: '流程控制', description: '等待指定时间' },
  { type: 'ai_chat', label: 'AI 对话', icon: Bot, group: 'AI 与知识', description: '调用大语言模型' },
  { type: 'agent_invoke', label: 'Agent 调用', icon: Waypoints, group: 'AI 与知识', description: '调用平台智能体' },
  { type: 'knowledge_search', label: '知识检索', icon: Search, group: 'AI 与知识', description: '检索知识库内容' },
  { type: 'doc_output', label: '文档生成', icon: FileText, group: 'AI 与知识', description: '输出 Word 文档' },
  { type: 'set_variable', label: '设置变量', icon: Braces, group: '数据与集成', description: '写入上下文变量' },
  { type: 'data_source', label: '数据读取', icon: Database, group: '数据与集成', description: '读取 MySQL、CSV、Excel' },
  { type: 'db_write', label: '写入数据库', icon: DatabaseZap, group: '数据与集成', description: '将结果写回 MySQL' },
  { type: 'http', label: 'HTTP 请求', icon: Cable, group: '数据与集成', description: '调用外部接口' },
  { type: 'web_crawl', label: '网页爬取', icon: Globe, group: '数据与集成', description: '提取网页正文' },
  { type: 'send_email', label: '发送邮件', icon: Mail, group: '数据与集成', description: '发送 SMTP 通知' },
  { type: 'code', label: '代码执行', icon: Terminal, group: '数据与集成', description: '执行 Python 沙箱代码' },
]

export function getNodeType(type: string) {
  return nodeTypes.find(node => node.type === type)
}
