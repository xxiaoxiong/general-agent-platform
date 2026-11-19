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
