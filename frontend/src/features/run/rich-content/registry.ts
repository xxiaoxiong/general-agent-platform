import type { RichKind } from './types.ts'

/**
 * 代码围栏语言 → 富内容渲染器注册表。
 * 只识别明确的别名，其余语言保持普通代码块，避免误判模型输出。
 */
const RICH_LANGUAGE_ALIASES: Record<string, RichKind> = {
  mermaid: 'mermaid',
  mmd: 'mermaid',
  mermaidjs: 'mermaid',
  chart: 'chart',
}

/** 从围栏 info 中提取语言（`mermaid title=xx` 只取第一个词） */
export function fenceLanguage(info?: string | null): string {
  return (info || '').trim().split(/\s+/, 1)[0].toLowerCase()
}

export function detectRichKind(info?: string | null): RichKind | null {
  return RICH_LANGUAGE_ALIASES[fenceLanguage(info)] ?? null
}

export function richKindLabel(kind: RichKind): string {
  return kind === 'mermaid' ? 'Mermaid 图表' : '数据图表'
}

/** 语言别名全集，供测试与提示词文档引用 */
export function richLanguageAliases(): string[] {
  return Object.keys(RICH_LANGUAGE_ALIASES)
}
