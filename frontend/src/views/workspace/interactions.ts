import type { Conversation } from '@/api/v2'

const LAST_MESSAGE_THRESHOLD = 40

type Viewport = Pick<HTMLElement, 'scrollTop' | 'scrollHeight' | 'clientHeight'>

export function isNearLatest(viewport: Viewport): boolean {
  return viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop < LAST_MESSAGE_THRESHOLD
}

export function hasScrolledAway(viewport: Viewport, lastPinnedTop: number): boolean {
  // 工具组折叠或视口变高时，浏览器会将 scrollTop 限制到当前最大值。
  const currentMaximum = Math.max(0, viewport.scrollHeight - viewport.clientHeight)
  return lastPinnedTop >= 0 && viewport.scrollTop < Math.min(lastPinnedTop, currentMaximum) - LAST_MESSAGE_THRESHOLD
}

type ComposerKey = Pick<KeyboardEvent, 'key' | 'shiftKey' | 'altKey' | 'ctrlKey' | 'metaKey' | 'isComposing' | 'keyCode'>

export function isSendKey(event: ComposerKey): boolean {
  return event.key === 'Enter' && !event.shiftKey && !event.altKey && !event.ctrlKey && !event.metaKey
    && !event.isComposing && event.keyCode !== 229
}

export function splitConversationHistory(items: Conversation[], query: string) {
  const term = query.trim().toLocaleLowerCase()
  const pinned: Conversation[] = []
  const recent: Conversation[] = []
  for (const item of items) {
    if (!(item.title || '新对话').toLocaleLowerCase().includes(term)) continue
    if (item.is_pinned) pinned.push(item)
    else recent.push(item)
  }
  pinned.sort((left, right) => (right.pinned_at || '').localeCompare(left.pinned_at || '') || right.id - left.id)
  return { pinned, recent }
}

const CLIPBOARD_IMAGE_EXTENSIONS: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/webp': 'webp',
  'image/gif': 'gif',
  'image/bmp': 'bmp',
  'image/svg+xml': 'svg',
}

export function clipboardFiles(data: Pick<DataTransfer, 'files' | 'items'> | null): File[] {
  if (!data) return []
  // 浏览器可能同时通过 files 和 items 暴露相同的剪贴板文件。
  const files = data.files.length ? Array.from(data.files) : Array.from(data.items)
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter((file): file is File => file !== null)

  const stamp = Date.now()
  return files.map((file, index) => {
    const extension = CLIPBOARD_IMAGE_EXTENSIONS[file.type]
    const generic = !file.name.trim() || /^(?:blob|image)(?:\.[a-z0-9]+)?$/i.test(file.name)
    if (!extension || !generic) return file
    return new File([file], `粘贴图片-${stamp}-${index + 1}.${extension}`, {
      type: file.type, lastModified: file.lastModified,
    })
  })
}
