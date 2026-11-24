<template>
  <div class="fp-backdrop" :class="{ visible: maximized }" aria-hidden="true" @click="closeOverlay"></div>
  <aside
    ref="panel"
    class="file-preview"
    aria-label="文件预览"
    :class="{ maximized, resizing }"
    :style="{ '--fp-width': visibleWidth + 'px' }"
  >
    <div v-if="!maximized" ref="separator" class="fp-resize" role="separator" tabindex="0"
      aria-label="调整预览宽度" aria-orientation="vertical" title="拖动调整预览宽度"
      :aria-valuemin="minWidth" :aria-valuemax="availableWidth" :aria-valuenow="visibleWidth"
      @pointerdown="startResize" @pointermove="moveResize" @pointerup="releaseResize"
      @pointercancel="releaseResize" @lostpointercapture="releaseResize" @keydown="resizeWithKeyboard"
    ></div>

    <!-- 头部：类型图标 + 文件名 + 徽标 + 大小 + 放大/还原 + 下载 + 关闭 -->
    <header class="fp-head">
      <span class="fp-kind-icon" :style="{ color: meta.color, background: meta.bg }">
        <component :is="meta.icon" />
      </span>
      <div class="fp-title">
        <span class="fp-name" :title="data?.path || name">{{ data?.name || name }}</span>
        <span class="fp-kind" :style="{ color: meta.color, background: meta.bg }">{{ meta.label }}</span>
      </div>
      <div class="fp-actions">
        <span v-if="data?.size != null" class="fp-size">{{ formatSize(data.size) }}</span>
        <button
          type="button"
          class="fp-icon-btn"
          :title="maximized ? '还原预览' : '放大预览'"
          :aria-label="maximized ? '还原预览' : '放大预览'"
          @click="maximized = !maximized"
        >
          <component :is="maximized ? Minimize : Maximize" :size="16" />
        </button>
        <button v-if="data" type="button" class="fp-icon-btn" title="下载" aria-label="下载" @click="$emit('download')">
          <Download :size="16" />
        </button>
        <button type="button" class="fp-icon-btn" title="关闭（Esc）" aria-label="关闭文件预览" @click="$emit('close')">
          <X :size="16" />
        </button>
      </div>
    </header>

    <!-- 完整相对路径（防同名混淆） -->
    <div v-if="data?.path" class="fp-path" :title="data.path">{{ data.path }}</div>

    <!-- 内容体（表格 / 代码 / 文本 / Markdown / 图片 / PDF / Office / 二进制兜底） -->
    <FilePreviewBody :data="data" :loading="loading" :error="error"
      @open-original="emit('open-original', $event)">
      <template v-if="$slots.document" #document>
        <slot name="document" />
      </template>
    </FilePreviewBody>
  </aside>
</template>

<script setup lang="ts">
/**
 * 右侧临时文件预览区块（对齐 Trae / VS Code preview tab 语义）：
 * 点对话里的文件名 → 右侧出现临时预览 → 再点别的文件原位替换 → 可关闭。
 * 内容渲染委托 FilePreviewBody；宽度拖动 / 放大委托 useResizablePanel。
 */
import { computed, ref } from 'vue'
import { Download, Maximize, Minimize, X } from '@lucide/vue'
import type { PreviewContent } from '@/api/conversations'
import FilePreviewBody from './FilePreviewBody.vue'
import { KIND_META, type FileKindMeta } from './fileKinds'
import { useResizablePanel } from './useResizablePanel'

const props = defineProps<{
  /** 已解析的预览内容（null 时仅显示文件名占位） */
  data?: PreviewContent | null
  name?: string
  loading?: boolean
  error?: string
}>()

const emit = defineEmits<{ close: []; download: []; 'open-original': [path: string] }>()

const panel = ref<HTMLElement>()
const separator = ref<HTMLElement>()
const {
  visibleWidth, minWidth, availableWidth, maximized, resizing,
  startResize, moveResize, releaseResize, resizeWithKeyboard,
} = useResizablePanel(panel, separator, { width: 460, minWidth: 320 })

function closeOverlay() {
  if (maximized.value) maximized.value = false
  else emit('close')
}

/** 类型 -> 图标 / 标签 / 配色（头部视觉锚点，映射表与文件面板共用） */
const meta = computed<FileKindMeta>(() => {
  const data = props.data
  if (data?.kind === 'pdf' && /\.docx$/i.test(data.name)) return { ...KIND_META.office, label: 'Word 版式' }
  return (data && KIND_META[data.kind]) || KIND_META.binary
})

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}
</script>

<style scoped>
.file-preview {
  position: relative;
  flex: none;
  width: var(--fp-width);
  min-width: min(320px, 100%);
  max-width: 100%;
  border-left: 1px solid var(--ui-border);
  background: var(--ui-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* 左缘拖拽调宽 */
.fp-resize {
  position: absolute; left: 0; top: 0; bottom: 0; width: 12px; z-index: 5;
  cursor: col-resize; touch-action: none; outline-offset: -2px;
}
.fp-resize::after {
  content: ''; position: absolute; inset: 0 auto 0 0; width: 2px; background: transparent;
}
.fp-resize:hover::after, .fp-resize:focus-visible::after, .resizing .fp-resize::after { background: var(--ui-accent); }
/* 浏览器内置 PDF 查看器在拖动期间让出指针。 */
.file-preview.resizing :deep(iframe) { pointer-events: none; }

/* 放大（最大化）：覆盖式大面板 */
.file-preview.maximized {
  position: fixed; inset: 28px; z-index: var(--ui-z-dialog);
  width: auto; min-width: 0; max-width: none; height: auto;
  border: none; border-radius: var(--ui-radius-lg);
  box-shadow: var(--ui-shadow-md);
}
.fp-backdrop {
  display: none; position: fixed; inset: 0; z-index: calc(var(--ui-z-dialog) - 1); background: var(--ui-overlay);
}
.fp-backdrop.visible { display: block; }

/* 头部 */
.fp-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--ui-border);
  flex-shrink: 0;
}
.fp-kind-icon {
  width: 30px; height: 30px; border-radius: var(--ui-radius-md); flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center; font-size: 16px;
}
.fp-kind-icon svg { width: 16px; height: 16px; }
.fp-title { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.fp-name {
  font-weight: 600; font-size: 13px; color: var(--ui-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.fp-kind {
  flex-shrink: 0; font-size: 11px; font-weight: 500;
  border-radius: var(--ui-radius-sm); padding: 1px 7px;
}
.fp-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.fp-size { font-size: 11px; color: var(--ui-muted); }
.fp-icon-btn {
  width: 30px; height: 30px; padding: 0; border: none; border-radius: var(--ui-radius-sm);
  background: transparent; color: var(--ui-secondary); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.fp-icon-btn:hover { background: var(--ui-hover); color: var(--ui-accent); }

.fp-path {
  padding: 5px 12px; font-size: 11px; color: var(--ui-muted);
  border-bottom: 1px solid var(--ui-subtle);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: var(--ui-font-mono); flex-shrink: 0;
}

@media (max-width: 849px) {
  .file-preview, .file-preview.maximized {
    position: fixed; inset: var(--ui-header-height) 0 0 auto; z-index: var(--ui-z-dialog);
    width: 100vw; max-width: 100vw; min-width: 0; height: auto;
    border-left: 1px solid var(--ui-border); border-radius: 0; box-shadow: var(--ui-shadow-md);
  }
  .fp-backdrop { display: block; }
  .fp-resize, .fp-actions .fp-icon-btn:first-of-type, .fp-size { display: none; }
}
</style>
