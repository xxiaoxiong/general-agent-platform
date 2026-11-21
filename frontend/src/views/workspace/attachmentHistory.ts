export interface MessageAttachment {
  name: string
  path: string
  previewUrl?: string
}

function fromWorkspacePath(path: unknown): MessageAttachment | undefined {
  if (typeof path !== 'string') return undefined
  const match = /^(?:\/?workspace\/)?(uploads\/([^/\\:\x00-\x1f\x7f]+))$/.exec(path)
  if (!match || match[2] === '.' || match[2] === '..') return undefined
  return { name: match[2], path: match[1] }
}

// 新会话消息保存结构化附件。旧消息从后端追加的附件注记恢复原文件路径。
export function messageAttachments(content: string, records?: unknown): { text: string; files: MessageAttachment[] } {
  const marker = '\n\n[附件]'
  const offset = content.lastIndexOf(marker)
  const legacy = offset < 0 ? [] : content.slice(offset + marker.length).split('\n').slice(1)
    .map((line) => /^-\s+(.+?)(?:（文本副本：[^\n]+）)?$/.exec(line)?.[1])
    .map(fromWorkspacePath)
    .filter((file): file is MessageAttachment => !!file)
  const files = Array.isArray(records) ? records
    .map((item) => fromWorkspacePath(item?.path))
    .filter((file): file is MessageAttachment => !!file) : []
  return { text: legacy.length ? content.slice(0, offset) : content, files: files.length ? files : legacy }
}
