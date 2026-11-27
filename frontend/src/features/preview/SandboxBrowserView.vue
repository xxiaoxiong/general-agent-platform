<template>
  <div
    ref="stage" class="sbv-stage" :class="{ 'is-focused': focused }"
    @pointerdown="onPointerDown" @pointermove="onPointerMove"
    @pointerup="onPointerUp" @pointercancel="onPointerUp"
    @wheel="onWheel" @contextmenu.prevent
  >
    <canvas ref="canvas" class="sbv-canvas" aria-label="服务端浏览器画面"></canvas>
    <!-- 隐藏输入框：承接键盘与中文输入法（composition）事件 -->
    <textarea
      ref="imeInput" class="sbv-ime" aria-hidden="true" tabindex="-1"
      autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
      @keydown="onImeKeyDown" @keyup="onKeyUp" @input="onImeInput"
      @focus="focused = true" @blur="focused = false"
    ></textarea>
    <div v-if="connecting && !hasFrame" class="sbv-overlay" role="status">
      <span class="sbv-spinner"></span>
      <span>正在连接服务端浏览器…</span>
    </div>
    <div v-else-if="error" class="sbv-overlay sbv-error" role="alert">
      <span>{{ error }}</span>
    </div>
    <div v-if="hasFrame && !focused" class="sbv-hint">点击画面后可使用键盘输入</div>
  </div>
</template>

<script setup lang="ts">
/**
 * 内置浏览器画面：消费 Backend 转发的 JPEG 帧并回传键鼠/文本输入。
 *
 * - 画面来自平台侧 Chromium（客户机无需访问 Preview 域名/端口）；
 * - 坐标按画布实际显示尺寸映射到服务端视口；
 * - 隐藏 textarea 承接键盘事件，兼容中文输入法与粘贴；
 * - 面板尺寸变化时向服务端发送 resize（服务端同步视口后重启投屏）。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps<{
  /** Backend WebSocket 地址（相对或绝对，如 /api/v2/workspaces/browser-sessions/...） */
  wsUrl: string
  viewportWidth?: number
  viewportHeight?: number
}>()

const emit = defineEmits<{
  error: [message: string]
  meta: [payload: { url: string; title: string }]
}>()

const stage = ref<HTMLElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const imeInput = ref<HTMLTextAreaElement | null>(null)
const connecting = ref(false)
const hasFrame = ref(false)
const error = ref('')
const focused = ref(false)

const SPECIAL_KEYS = new Set([
  'Enter', 'Tab', 'Backspace', 'Escape', 'Delete', 'Insert',
  'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
  'Home', 'End', 'PageUp', 'PageDown', 'F5',
])

let socket: WebSocket | null = null
let closedByUs = false
let reconnectAttempts = 0
let reconnectTimer: number | undefined
let resizeObserver: ResizeObserver | undefined
let resizeTimer: number | undefined
let moveRaf = 0
let pendingMove: { x: number; y: number } | null = null
let lastResizeKey = ''
let drawing = false
let pendingFrame: ArrayBuffer | null = null
let serverViewport = {
  width: props.viewportWidth || 1280,
  height: props.viewportHeight || 800,
}

function absoluteWsUrl(raw: string): string {
  if (/^wss?:/i.test(raw)) return raw
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${window.location.host}${raw}`
}

function send(message: Record<string, unknown>) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(message))
  }
}

function resetSession() {
  hasFrame.value = false
  error.value = ''
  pendingFrame = null
  lastResizeKey = ''
}

function connect() {
  cleanupSocket()
  resetSession()
  closedByUs = false
  if (!props.wsUrl) return
  connecting.value = true
  try {
    socket = new WebSocket(absoluteWsUrl(props.wsUrl))
  } catch (exc: any) {
    connecting.value = false
    error.value = exc?.message || '无法建立浏览器连接'
    emit('error', error.value)
    return
  }
  socket.binaryType = 'arraybuffer'
  socket.onopen = () => {
    connecting.value = false
    reconnectAttempts = 0
    fitViewport()
  }
  socket.onmessage = (event: MessageEvent) => {
    if (typeof event.data === 'string') {
      handleControl(event.data)
    } else if (event.data instanceof ArrayBuffer) {
      queueFrame(event.data)
    }
  }
  socket.onerror = () => {
    if (!hasFrame.value) error.value = '浏览器连接异常，正在重试…'
  }
  socket.onclose = () => {
    connecting.value = false
    if (closedByUs) return
    if (reconnectAttempts < 4) {
      reconnectAttempts += 1
      reconnectTimer = window.setTimeout(connect, 1200 * reconnectAttempts)
      return
    }
    error.value = '浏览器会话已结束，请点击刷新重新建立会话'
    emit('error', error.value)
  }
}

function cleanupSocket() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = undefined
  }
  if (socket) {
    try {
      socket.close(1000)
    } catch {
      // 已关闭
    }
    socket = null
  }
  connecting.value = false
}

function handleControl(raw: string) {
  let message: any
  try {
    message = JSON.parse(raw)
  } catch {
    return
  }
  if (message?.type === 'viewport') {
    const width = Number(message.width) || serverViewport.width
    const height = Number(message.height) || serverViewport.height
    serverViewport = { width, height }
  } else if (message?.type === 'meta') {
    emit('meta', { url: String(message.url || ''), title: String(message.title || '') })
  } else if (message?.type === 'error') {
    error.value = String(message.message || '页面异常')
  }
}

async function queueFrame(frame: ArrayBuffer) {
  pendingFrame = frame
  if (drawing) return
  drawing = true
  while (pendingFrame) {
    const current = pendingFrame
    pendingFrame = null
    try {
      const bitmap = await createImageBitmap(new Blob([current], { type: 'image/jpeg' }))
      const target = canvas.value
      if (target) {
        if (target.width !== bitmap.width) target.width = bitmap.width
        if (target.height !== bitmap.height) target.height = bitmap.height
        target.getContext('2d')?.drawImage(bitmap, 0, 0)
        hasFrame.value = true
        if (error.value.startsWith('浏览器连接异常')) error.value = ''
      }
      bitmap.close()
    } catch {
      // 单帧解码失败直接丢弃，等待下一帧
    }
  }
  drawing = false
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

function fitViewport() {
  const stageEl = stage.value
  if (!stageEl) return
  const rect = stageEl.getBoundingClientRect()
  if (rect.width < 48 || rect.height < 48) return
  const width = clamp(Math.round(rect.width), 320, 1920)
  const height = clamp(Math.round(rect.height), 240, 1200)
  const key = `${width}x${height}`
  if (key === lastResizeKey) return
  lastResizeKey = key
  send({ type: 'resize', width, height })
}

function pointerPoint(event: MouseEvent) {
  const target = canvas.value
  if (!target) return null
  const rect = target.getBoundingClientRect()
  if (!rect.width || !rect.height) return null
  return {
    x: clamp((event.clientX - rect.left) * serverViewport.width / rect.width, 0, serverViewport.width),
    y: clamp((event.clientY - rect.top) * serverViewport.height / rect.height, 0, serverViewport.height),
  }
}

function focusInput() {
  imeInput.value?.focus({ preventScroll: true })
}

function onPointerDown(event: PointerEvent) {
  if (event.button !== 0 && event.button !== 1 && event.button !== 2) return
  // 阻止浏览器默认把焦点移到 body（否则隐藏输入框立即失焦，键盘无法输入）
  event.preventDefault()
  focusInput()
  try {
    stage.value?.setPointerCapture(event.pointerId)
  } catch {
    // 部分浏览器不支持指针捕获
  }
  const point = pointerPoint(event)
  if (!point) return
  send({
    type: 'mouse', action: 'down', x: point.x, y: point.y,
    button: event.button === 2 ? 'right' : event.button === 1 ? 'middle' : 'left',
  })
  if (event.button === 2) event.preventDefault()
}

function onPointerMove(event: PointerEvent) {
  const point = pointerPoint(event)
  if (!point) return
  pendingMove = point
  if (moveRaf) return
  moveRaf = requestAnimationFrame(() => {
    moveRaf = 0
    if (!pendingMove) return
    send({ type: 'mouse', action: 'move', x: pendingMove.x, y: pendingMove.y })
    pendingMove = null
  })
}

function onPointerUp(event: PointerEvent) {
  const point = pointerPoint(event)
  if (!point) return
  send({
    type: 'mouse', action: 'up', x: point.x, y: point.y,
    button: event.button === 2 ? 'right' : event.button === 1 ? 'middle' : 'left',
  })
}

function onWheel(event: WheelEvent) {
  const point = pointerPoint(event)
  if (!point) return
  event.preventDefault()
  send({
    type: 'mouse', action: 'wheel', x: point.x, y: point.y,
    deltaX: event.deltaX, deltaY: event.deltaY,
  })
}

function onImeKeyDown(event: KeyboardEvent) {
  if (!hasFrame.value || event.isComposing) return
  // Ctrl/Cmd+V 交给浏览器原生粘贴（input 事件再发送文本）
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v') return
  if (event.ctrlKey || event.metaKey) {
    event.preventDefault()
    const parts: string[] = []
    if (event.ctrlKey) parts.push('Control')
    if (event.altKey) parts.push('Alt')
    if (event.shiftKey) parts.push('Shift')
    if (event.metaKey) parts.push('Meta')
    let key = event.key
    if (key.length === 1) key = key.toUpperCase()
    else if (key === ' ') key = 'Space'
    parts.push(key)
    send({ type: 'shortcut', keys: parts.join('+') })
    return
  }
  if (event.altKey) {
    event.preventDefault()
    send({ type: 'key', action: 'down', key: event.key })
    return
  }
  if (event.key.length === 1) return
  if (SPECIAL_KEYS.has(event.key)) {
    event.preventDefault()
    send({ type: 'key', action: 'down', key: event.key })
  }
}

function onImeInput(event: Event) {
  if (event instanceof InputEvent && event.isComposing) return
  const element = imeInput.value
  if (!element) return
  const value = element.value
  if (!value) return
  element.value = ''
  send({ type: 'text', text: value })
}

function onKeyUp(event: KeyboardEvent) {
  if (!hasFrame.value || !SPECIAL_KEYS.has(event.key)) return
  event.preventDefault()
  send({ type: 'key', action: 'up', key: event.key })
}

function reload() {
  send({ type: 'navigate', action: 'reload' })
}

function goBack() {
  send({ type: 'navigate', action: 'back' })
}

function goForward() {
  send({ type: 'navigate', action: 'forward' })
}

defineExpose({ reload, goBack, goForward, focusInput })

onMounted(() => {
  connect()
  const stageEl = stage.value
  if (stageEl && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => {
      if (resizeTimer) clearTimeout(resizeTimer)
      resizeTimer = window.setTimeout(fitViewport, 220)
    })
    resizeObserver.observe(stageEl)
  }
})

onBeforeUnmount(() => {
  closedByUs = true
  cleanupSocket()
  resizeObserver?.disconnect()
  resizeObserver = undefined
  if (resizeTimer) clearTimeout(resizeTimer)
  if (moveRaf) cancelAnimationFrame(moveRaf)
  moveRaf = 0
})

watch(() => props.wsUrl, () => connect())
</script>

<style scoped>
.sbv-stage {
  position: relative; width: 100%; height: 100%; min-height: 0;
  display: flex; align-items: center; justify-content: center;
  overflow: hidden; background: #1f2430; cursor: default; outline: none;
  user-select: none;
}
.sbv-stage.is-focused { box-shadow: inset 0 0 0 1px var(--ui-accent); }
.sbv-canvas {
  display: block; max-width: 100%; max-height: 100%; width: auto; height: auto;
  background: #fff;
}
.sbv-ime {
  position: absolute; left: -8px; bottom: 0; width: 1px; height: 1px;
  padding: 0; border: 0; opacity: 0; resize: none; pointer-events: none;
  font-size: 16px; line-height: 1;
}
.sbv-overlay {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  gap: 8px; flex-wrap: wrap; padding: 16px; text-align: center;
  color: #dfe4ee; background: rgba(20, 24, 33, .82); font-size: 13px;
}
.sbv-error { color: #ffb4b4; }
.sbv-hint {
  position: absolute; left: 50%; bottom: 10px; transform: translateX(-50%);
  padding: 3px 10px; border-radius: 999px; font-size: 11px; color: #e8ecf5;
  background: rgba(20, 24, 33, .68); pointer-events: none; white-space: nowrap;
}
.sbv-spinner {
  width: 16px; height: 16px; border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, .28); border-top-color: #fff;
  animation: sbv-spin .9s linear infinite;
}
@keyframes sbv-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .sbv-spinner { animation: none; } }
</style>
