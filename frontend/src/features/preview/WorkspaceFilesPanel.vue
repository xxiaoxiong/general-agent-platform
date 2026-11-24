<template>
  <div class="wf-backdrop" :class="{ visible: maximized }" aria-hidden="true" @click="closeOverlay"></div>
  <aside
    ref="panel"
    class="workspace-files"
    aria-label="工作目录文件"
    :class="{ maximized, resizing }"
    :style="{ '--wf-width': visibleWidth + 'px' }"
  >
    <div v-if="!maximized" ref="separator" class="wf-resize" role="separator" tabindex="0"
      aria-label="调整面板宽度" aria-orientation="vertical" title="拖动调整面板宽度"
      :aria-valuemin="minWidth" :aria-valuemax="availableWidth" :aria-valuenow="visibleWidth"
      @pointerdown="startResize" @pointermove="moveResize" @pointerup="releaseResize"
      @pointercancel="releaseResize" @lostpointercapture="releaseResize" @keydown="resizeWithKeyboard"
    ></div>

    <!-- 头部：标题 + 文件数 + 刷新/打包下载/放大/关闭 -->
    <header class="wf-head">
      <FolderOpen :size="16" aria-hidden="true" />
      <strong>工作目录</strong>
      <span v-if="files.length" class="wf-count">{{ files.length }} 个文件</span>
      <div class="wf-actions">
        <button type="button" class="wf-icon-btn" title="刷新" aria-label="刷新文件列表" :disabled="loading" @click="$emit('refresh')">
          <LoaderCircle v-if="loading" class="mini-spin" :size="15" />
          <RefreshCw v-else :size="15" />
        </button>
        <button type="button" class="wf-icon-btn" title="打包下载（ZIP）" aria-label="打包下载全部文件"
          :disabled="downloading || !files.length" @click="$emit('download-zip')">
          <LoaderCircle v-if="downloading" class="mini-spin" :size="15" />
          <Download v-else :size="15" />
        </button>
        <button type="button" class="wf-icon-btn" :title="maximized ? '还原面板' : '放大面板'"
          :aria-label="maximized ? '还原面板' : '放大面板'" @click="maximized = !maximized">
          <component :is="maximized ? Minimize : Maximize" :size="15" />
        </button>
        <button type="button" class="wf-icon-btn" title="关闭（Esc）" aria-label="关闭文件面板" @click="closeOverlay">
          <X :size="15" />
        </button>
      </div>
    </header>

    <div class="wf-body">
      <!-- 左：项目文件树（层级索引） -->
      <ul class="wf-tree" aria-label="工作目录文件树">
        <li v-if="loading && !files.length" class="wf-state" role="status">
          <LoaderCircle class="mini-spin" :size="16" />
          <span>正在加载文件列表…</span>
        </li>
        <li v-else-if="error" class="wf-state wf-error" role="alert">
          <CircleAlert :size="16" />
          <span>{{ error }}</span>
          <button type="button" class="wf-link-btn" @click="$emit('refresh')">重试</button>
        </li>
        <li v-else-if="!files.length" class="wf-state">
          <FolderOpen :size="16" />
          <span>工作目录暂无文件，任务产出的文件会显示在这里</span>
        </li>
        <template v-else>
          <li v-for="row in rows" :key="row.key">
            <button
              v-if="row.kind === 'dir'"
              type="button" class="wf-row wf-dir" :style="{ paddingInlineStart: 6 + row.depth * 14 + 'px' }"
              :aria-expanded="row.expanded" @click="toggleDir(row.path)"
            >
              <ChevronRight class="wf-chevron" :class="{ open: row.expanded }" :size="13" />
              <component :is="row.expanded ? FolderOpen : Folder" class="wf-folder" :size="15" />
              <span class="wf-name">{{ row.name }}</span>
            </button>
            <button
              v-else
              type="button" class="wf-row wf-file" :class="{ selected: preview?.path === row.path }"
              :style="{ paddingInlineStart: 6 + row.depth * 14 + 'px' }" :title="row.path"
              @click="openFile(row)"
            >
              <span class="wf-kind-icon" :style="{ color: rowMeta(row).color, background: rowMeta(row).bg }">
                <component :is="rowMeta(row).icon" :size="14" />
              </span>
              <span class="wf-name">{{ row.name }}</span>
              <span class="wf-size">{{ row.file ? formatSize(row.file.size) : '' }}</span>
            </button>
          </li>
        </template>
        <li v-if="truncated" class="wf-truncated">文件较多，仅显示前 {{ files.length }} 个</li>
      </ul>

      <!-- 右：文件内容（点文件名原位预览，树保持可见） -->
      <section class="wf-content" aria-label="文件内容">
        <div class="wf-content-head">
          <template v-if="preview">
            <span class="wf-content-name" :title="preview.path">{{ preview.name }}</span>
            <span v-if="preview.data?.size != null" class="wf-size">{{ formatSize(preview.data.size) }}</span>
            <button type="button" class="wf-icon-btn" title="下载" aria-label="下载当前文件" @click="$emit('download')">
              <Download :size="15" />
            </button>
            <button type="button" class="wf-icon-btn" title="关闭预览" aria-label="关闭文件预览" @click="$emit('close-preview')">
              <X :size="15" />
            </button>
          </template>
          <span v-else class="wf-content-name">文件内容</span>
        </div>
        <FilePreviewBody
          v-if="preview"
          :data="preview.data" :loading="preview.loading" :error="preview.error"
          @open-original="emit('open-original', $event)"
        />
        <div v-else class="wf-state wf-hint">
          <FileSearch :size="17" />
          <span>点击左侧文件名，在此预览文件内容</span>
        </div>
      </section>
    </div>
  </aside>
</template>

<script setup lang="ts">
/**
 * 工作目录文件面板（VS Code 资源管理器式）：
 * 左侧项目文件树作层级索引（目录在前、可展开折叠），点文件名 → 右侧原位预览内容；
 * 支持手动刷新、打包下载（ZIP）、拖动调宽与放大。
 */
import { computed, reactive, ref } from 'vue'
import {
  ChevronRight, CircleAlert, Download, FileSearch, Folder, FolderOpen,
  LoaderCircle, Maximize, Minimize, RefreshCw, X,
} from '@lucide/vue'
import type { PreviewContent, WorkspaceFile } from '@/api/conversations'
import FilePreviewBody from './FilePreviewBody.vue'
import { fileKindMeta } from './fileKinds'
import { buildFileTree, flattenFileTree, type FileTreeRowView } from './fileTree'
import { useResizablePanel } from './useResizablePanel'

const props = defineProps<{
  files: WorkspaceFile[]
  loading?: boolean
  error?: string
  truncated?: boolean
  /** 打包下载进行中 */
  downloading?: boolean
  /** 当前预览（右侧内容区；null 时显示提示） */
  preview?: {
    name: string
    path: string
    loading: boolean
    error: string
    data: PreviewContent | null
  } | null
}>()

const emit = defineEmits<{
  open: [path: string, name: string]
  refresh: []
  close: []
  'download-zip': []
  download: []
  'open-original': [path: string]
  'close-preview': []
}>()

// ── 文件树（默认全部展开，折叠状态记在 collapsed 集合）──────────────────────
const collapsed = reactive(new Set<string>())
const tree = computed(() => buildFileTree(props.files))
const rows = computed<FileTreeRowView[]>(() =>
  flattenFileTree(tree.value, (path) => !collapsed.has(path)).map(({ node, depth }) => ({
    key: node.path,
    name: node.name,
    path: node.path,
    depth,
    kind: node.dir ? 'dir' : 'file',
    expanded: node.dir && !collapsed.has(node.path),
    file: node.file,
  })),
)

function toggleDir(path: string) {
  if (collapsed.has(path)) collapsed.delete(path)
  else collapsed.add(path)
}

function openFile(row: FileTreeRowView) {
  if (row.file) emit('open', row.file.path, row.file.name)
}

/** 文件行的类型图标 / 配色 */
function rowMeta(row: FileTreeRowView) {
  return fileKindMeta(row.file?.kind || '')
}

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

// ── 面板宽度拖动 / 放大 ─────────────────────────────────────────────────────
const panel = ref<HTMLElement>()
const separator = ref<HTMLElement>()
const {
  visibleWidth, minWidth, availableWidth, maximized, resizing,
  startResize, moveResize, releaseResize, resizeWithKeyboard,
} = useResizablePanel(panel, separator, { width: 620, minWidth: 360 })

function closeOverlay() {
  if (maximized.value) maximized.value = false
  else emit('close')
}
</script>

<style scoped>
.workspace-files {
  position: relative;
  flex: none;
  width: var(--wf-width);
  min-width: min(360px, 100%);
  max-width: 100%;
  border-left: 1px solid var(--ui-border);
  background: var(--ui-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* 左缘拖拽调宽 */
.wf-resize {
  position: absolute; left: 0; top: 0; bottom: 0; width: 12px; z-index: 5;
  cursor: col-resize; touch-action: none; outline-offset: -2px;
}
.wf-resize::after {
  content: ''; position: absolute; inset: 0 auto 0 0; width: 2px; background: transparent;
}
.wf-resize:hover::after, .wf-resize:focus-visible::after, .resizing .wf-resize::after { background: var(--ui-accent); }
.workspace-files.resizing :deep(iframe) { pointer-events: none; }

/* 放大（最大化）：覆盖式大面板 */
.workspace-files.maximized {
  position: fixed; inset: 28px; z-index: var(--ui-z-dialog);
  width: auto; min-width: 0; max-width: none; height: auto;
  border: none; border-radius: var(--ui-radius-lg);
  box-shadow: var(--ui-shadow-md);
}
.wf-backdrop {
  display: none; position: fixed; inset: 0; z-index: calc(var(--ui-z-dialog) - 1); background: var(--ui-overlay);
}
.wf-backdrop.visible { display: block; }

/* 头部 */
.wf-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--ui-border);
  flex-shrink: 0;
  font-size: 13px;
  color: var(--ui-text);
}
.wf-head > svg { flex: none; color: var(--ui-accent); }
.wf-count { color: var(--ui-muted); font-size: 11px; }
.wf-actions { display: flex; align-items: center; gap: 2px; margin-inline-start: auto; }
.wf-icon-btn {
  width: 28px; height: 28px; padding: 0; border: none; border-radius: var(--ui-radius-sm);
  background: transparent; color: var(--ui-secondary); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.wf-icon-btn:hover:not(:disabled) { background: var(--ui-hover); color: var(--ui-accent); }
.wf-icon-btn:disabled { opacity: .5; cursor: default; }

/* 主体：左树右内容 */
.wf-body { flex: 1; min-height: 0; display: flex; }
.wf-tree {
  flex: 0 0 236px; min-width: 0; margin: 0; padding: 4px; list-style: none;
  overflow: auto; border-right: 1px solid var(--ui-border);
}
.wf-row {
  width: 100%; display: flex; align-items: center; gap: 5px; padding: 3px 6px;
  border: 0; border-radius: var(--ui-radius-sm); background: transparent; cursor: pointer;
  text-align: start; color: var(--ui-text); min-height: 24px;
}
.wf-row:hover { background: var(--ui-hover); }
.wf-row .wf-name {
  flex: 1; min-width: 0; font-size: 12.5px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.wf-chevron {
  flex: none; color: var(--ui-muted); transition: transform var(--ui-duration) var(--ui-ease);
}
.wf-chevron.open { transform: rotate(90deg); }
.wf-folder { flex: none; color: var(--ui-warning); }
.wf-dir .wf-name { font-weight: 550; }
.wf-kind-icon {
  width: 22px; height: 22px; flex: none; display: inline-flex; align-items: center; justify-content: center;
  border-radius: var(--ui-radius-sm);
}
.wf-file.selected { background: var(--ui-accent-soft); }
.wf-file.selected .wf-name { color: var(--ui-accent); font-weight: 550; }
.wf-size { flex: none; font-size: 11px; color: var(--ui-muted); font-variant-numeric: tabular-nums; }
.wf-truncated {
  padding: 8px 10px; font-size: 11px; color: var(--ui-warning);
  background: var(--ui-warning-soft); border-top: 1px solid var(--ui-hover);
}
.wf-state {
  display: flex; align-items: center; gap: 8px; justify-content: center; flex-wrap: wrap;
  padding: 32px 12px; color: var(--ui-secondary); font-size: 12.5px; text-align: center;
}
.wf-error { color: var(--ui-danger); }
.wf-link-btn {
  border: none; background: transparent; color: var(--ui-accent); cursor: pointer;
  font-size: 12px; padding: 2px 6px; border-radius: var(--ui-radius-sm);
}
.wf-link-btn:hover { background: var(--ui-accent-soft); }

/* 右侧内容区 */
.wf-content { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.wf-content-head {
  display: flex; align-items: center; gap: 8px; flex: none;
  padding: 7px 10px; border-bottom: 1px solid var(--ui-border);
}
.wf-content-name {
  flex: 1; min-width: 0; font-size: 12.5px; font-weight: 600; color: var(--ui-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.wf-hint { flex: 1; justify-content: center; font-weight: 400; }

.mini-spin { flex: none; animation: wf-spin 1s linear infinite; }
@keyframes wf-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .mini-spin { animation: none; } }

@media (max-width: 849px) {
  .workspace-files, .workspace-files.maximized {
    position: fixed; inset: var(--ui-header-height) 0 0 auto; z-index: var(--ui-z-dialog);
    width: 100vw; max-width: 100vw; min-width: 0; height: auto;
    border-left: 1px solid var(--ui-border); border-radius: 0; box-shadow: var(--ui-shadow-md);
  }
  .wf-backdrop { display: block; }
  .wf-resize, .wf-actions .wf-icon-btn:nth-of-type(3) { display: none; }
  .wf-body { flex-direction: column; }
  .wf-tree { flex: 0 0 45%; border-right: none; border-bottom: 1px solid var(--ui-border); }
}
</style>
