<template>
  <aside class="workspace-files" aria-label="工作目录文件">
    <!-- 头部：标题 + 文件数 + 刷新/打包下载/关闭 -->
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
        <button type="button" class="wf-icon-btn" title="关闭（Esc）" aria-label="关闭文件面板" @click="$emit('close')">
          <X :size="15" />
        </button>
      </div>
    </header>

    <!-- 内容体：文件列表（点击文件名 → 右侧预览内容） -->
    <div class="wf-body">
      <div v-if="loading && !files.length" class="wf-state" role="status">
        <LoaderCircle class="mini-spin" :size="16" />
        <span>正在加载文件列表…</span>
      </div>
      <div v-else-if="error" class="wf-state wf-error" role="alert">
        <CircleAlert :size="16" />
        <span>{{ error }}</span>
        <button type="button" class="wf-link-btn" @click="$emit('refresh')">重试</button>
      </div>
      <div v-else-if="!files.length" class="wf-state">
        <FolderOpen :size="16" />
        <span>工作目录暂无文件，任务产出的文件会显示在这里</span>
      </div>
      <ul v-else class="wf-list">
        <li v-for="file in files" :key="file.path">
          <button type="button" class="wf-file" :title="file.path" @click="$emit('open', file.path, file.name)">
            <span class="wf-kind-icon" :style="{ color: fileKindMeta(file.kind).color, background: fileKindMeta(file.kind).bg }">
              <component :is="fileKindMeta(file.kind).icon" :size="15" />
            </span>
            <span class="wf-file-main">
              <span class="wf-name">{{ file.name }}</span>
              <span class="wf-path">{{ file.path }}</span>
            </span>
            <span class="wf-size">{{ formatSize(file.size) }}</span>
          </button>
        </li>
      </ul>
      <p v-if="truncated" class="wf-truncated">文件较多，仅显示前 {{ files.length }} 个</p>
    </div>
  </aside>
</template>

<script setup lang="ts">
/**
 * 工作目录文件面板（Codex 式）：列出当前会话工作区文件，
 * 点击文件名 → 右侧原位预览内容；支持手动刷新与打包下载（ZIP）。
 */
import { CircleAlert, Download, FolderOpen, LoaderCircle, RefreshCw, X } from '@lucide/vue'
import type { WorkspaceFile } from '@/api/conversations'
import { fileKindMeta } from './fileKinds'

defineProps<{
  files: WorkspaceFile[]
  loading?: boolean
  error?: string
  truncated?: boolean
  /** 打包下载进行中 */
  downloading?: boolean
}>()

defineEmits<{
  open: [path: string, name: string]
  refresh: []
  close: []
  'download-zip': []
}>()

function formatSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}
</script>

<style scoped>
.workspace-files {
  position: relative;
  flex: none;
  width: min(380px, 100%);
  min-width: min(320px, 100%);
  max-width: 100%;
  border-left: 1px solid var(--ui-border);
  background: var(--ui-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

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

/* 内容体 */
.wf-body { flex: 1; min-height: 0; overflow: auto; }
.wf-state {
  display: flex; align-items: center; gap: 8px; justify-content: center; flex-wrap: wrap;
  padding: 40px 16px; color: var(--ui-secondary); font-size: 13px; text-align: center;
}
.wf-error { color: var(--ui-danger); }
.wf-link-btn {
  border: none; background: transparent; color: var(--ui-accent); cursor: pointer;
  font-size: 12px; padding: 2px 6px; border-radius: var(--ui-radius-sm);
}
.wf-link-btn:hover { background: var(--ui-accent-soft); }

.wf-list { margin: 0; padding: 4px; list-style: none; }
.wf-file {
  width: 100%; display: flex; align-items: center; gap: 9px; padding: 7px 8px;
  border: 0; border-radius: var(--ui-radius-sm); background: transparent; cursor: pointer;
  text-align: start; color: var(--ui-text);
}
.wf-file:hover { background: var(--ui-hover); }
.wf-kind-icon {
  width: 27px; height: 27px; flex: none; display: inline-flex; align-items: center; justify-content: center;
  border-radius: var(--ui-radius-sm);
}
.wf-file-main { flex: 1; min-width: 0; display: grid; }
.wf-name {
  font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.wf-path {
  font-size: 11px; color: var(--ui-muted); font-family: var(--ui-font-mono);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left;
}
.wf-size { flex: none; font-size: 11px; color: var(--ui-muted); font-variant-numeric: tabular-nums; }
.wf-truncated {
  padding: 8px 14px; font-size: 11px; color: var(--ui-warning);
  background: var(--ui-warning-soft); border-top: 1px solid var(--ui-hover);
}

.mini-spin { flex: none; animation: wf-spin 1s linear infinite; }
@keyframes wf-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .mini-spin { animation: none; } }

@media (max-width: 849px) {
  .workspace-files {
    position: fixed; inset: var(--ui-header-height) 0 0 auto; z-index: var(--ui-z-dialog);
    width: 100vw; max-width: 100vw; min-width: 0; height: auto;
    border-left: 1px solid var(--ui-border); box-shadow: var(--ui-shadow-md);
  }
}
</style>
