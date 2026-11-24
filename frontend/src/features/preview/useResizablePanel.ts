/**
 * 右侧面板宽度拖动 / 键盘调整 + 放大（FilePreviewPanel 与 WorkspaceFilesPanel 共用）。
 * 左缘分隔条拖动改宽；方向键微调；放大为覆盖式大面板。
 */
import { computed, onMounted, onUnmounted, ref, type Ref } from 'vue'

export function useResizablePanel(
  panel: Ref<HTMLElement | undefined>,
  separator: Ref<HTMLElement | undefined>,
  options: { width?: number; minWidth?: number } = {},
) {
  const panelWidth = ref(options.width ?? 460)
  const availableWidth = ref(panelWidth.value)
  const minWidth = computed(() => Math.min(options.minWidth ?? 320, availableWidth.value))
  const visibleWidth = computed(() => clampWidth(panelWidth.value))
  const maximized = ref(false)
  const resizing = ref(false)
  let sizeObserver: ResizeObserver | undefined
  let drag: { pointerId: number; startX: number; startWidth: number; cursor: string; userSelect: string } | undefined

  function clampWidth(width: number) {
    return Math.round(Math.min(availableWidth.value, Math.max(width, minWidth.value)))
  }

  function startResize(event: PointerEvent) {
    if (!event.isPrimary || event.button !== 0 || drag || !panel.value || !separator.value) return
    event.preventDefault()
    separator.value.focus({ preventScroll: true })
    separator.value.setPointerCapture(event.pointerId)
    drag = {
      pointerId: event.pointerId, startX: event.clientX, startWidth: panel.value.getBoundingClientRect().width,
      cursor: document.body.style.cursor, userSelect: document.body.style.userSelect,
    }
    resizing.value = true
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('blur', stopResize)
  }

  function moveResize(event: PointerEvent) {
    if (drag?.pointerId === event.pointerId) {
      panelWidth.value = clampWidth(drag.startWidth + drag.startX - event.clientX)
    }
  }

  function releaseResize(event: PointerEvent) {
    if (drag?.pointerId === event.pointerId) stopResize()
  }

  function stopResize() {
    if (!drag) return
    const { pointerId, cursor, userSelect } = drag
    drag = undefined
    resizing.value = false
    document.body.style.cursor = cursor
    document.body.style.userSelect = userSelect
    window.removeEventListener('blur', stopResize)
    if (separator.value?.hasPointerCapture(pointerId)) separator.value.releasePointerCapture(pointerId)
  }

  function resizeWithKeyboard(event: KeyboardEvent) {
    const step = event.shiftKey ? 120 : 32
    const sizes: Record<string, number> = {
      ArrowLeft: visibleWidth.value + step, ArrowRight: visibleWidth.value - step,
      Home: minWidth.value, End: availableWidth.value,
    }
    const size = sizes[event.key]
    if (typeof size !== 'number') return
    event.preventDefault()
    panelWidth.value = clampWidth(size)
  }

  onMounted(() => {
    const workspace = panel.value?.parentElement
    if (!workspace) return
    const updateWidth = () => { if (workspace.clientWidth) availableWidth.value = workspace.clientWidth }
    updateWidth()
    sizeObserver = new ResizeObserver(updateWidth)
    sizeObserver.observe(workspace)
  })
  onUnmounted(() => {
    sizeObserver?.disconnect()
    stopResize()
  })

  return {
    visibleWidth, minWidth, availableWidth, maximized, resizing,
    startResize, moveResize, releaseResize, resizeWithKeyboard,
  }
}
