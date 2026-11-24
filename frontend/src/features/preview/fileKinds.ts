/**
 * 预览类型 -> 图标 / 标签 / 配色（文件面板列表与预览头部共用的视觉锚点）。
 */
import type { Component } from 'vue'
import { CodeXml, FileText, Folder, Image as ImageIcon, Notebook, Table } from '@lucide/vue'
import type { PreviewContent } from '@/api/conversations'

export interface FileKindMeta {
  label: string
  icon: Component
  color: string
  bg: string
}

export const KIND_META: Record<PreviewContent['kind'], FileKindMeta> = {
  image: { label: '图片', icon: ImageIcon, color: 'var(--ui-success)', bg: 'var(--ui-success-soft)' },
  pdf: { label: 'PDF', icon: FileText, color: 'var(--ui-danger)', bg: 'var(--ui-danger-soft)' },
  markdown: { label: 'Markdown', icon: Notebook, color: 'var(--ui-info)', bg: 'var(--ui-info-soft)' },
  code: { label: '代码', icon: CodeXml, color: 'var(--ui-accent)', bg: 'var(--ui-accent-soft)' },
  text: { label: '文本', icon: FileText, color: 'var(--ui-secondary)', bg: 'var(--ui-subtle)' },
  table: { label: '表格', icon: Table, color: 'var(--ui-info)', bg: 'var(--ui-info-soft)' },
  office: { label: '文档', icon: FileText, color: 'var(--ui-warning)', bg: 'var(--ui-warning-soft)' },
  binary: { label: '文件', icon: Folder, color: 'var(--ui-secondary)', bg: 'var(--ui-subtle)' },
}

export function fileKindMeta(kind: string): FileKindMeta {
  return KIND_META[kind as PreviewContent['kind']] || KIND_META.binary
}
