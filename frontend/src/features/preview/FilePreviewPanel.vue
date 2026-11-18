<template>
  <div v-if="maximized" class="fp-backdrop" @click="maximized = false"></div>
  <aside
    class="file-preview"
    :class="{ maximized }"
    :style="maximized ? undefined : { width: panelWidth + 'px' }"
  >
    <!-- 左缘拖拽调宽 -->
    <div class="fp-resize" title="拖动调整宽度" @mousedown="startResize"></div>

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
          @click="maximized = !maximized"
        >
          <el-icon><FullScreen /></el-icon>
        </button>
        <button v-if="data" type="button" class="fp-icon-btn" title="下载" @click="$emit('download')">
          <el-icon><Download /></el-icon>
        </button>
        <button type="button" class="fp-icon-btn" title="关闭（Esc）" @click="$emit('close')">
          <el-icon><Close /></el-icon>
        </button>
      </div>
    </header>

    <!-- 完整相对路径（防同名混淆） -->
    <div v-if="data?.path" class="fp-path" :title="data.path">{{ data.path }}</div>

    <!-- 文本抽取副本提示：一键切换到原文件富预览 -->
    <div v-if="originalPath" class="fp-extracted-bar">
      <el-icon><InfoFilled /></el-icon>
      <span>此为「{{ originalName }}」的文本抽取副本</span>
      <button type="button" class="fp-link-btn" @click="$emit('open-original', originalPath)">
        查看原文件 <el-icon size="11"><Top /></el-icon>
      </button>
    </div>

    <!-- 内容体 -->
    <div class="fp-body" :class="{ 'no-pad': data?.kind === 'image' || data?.kind === 'pdf' }">
      <!-- 加载态 -->
      <div v-if="loading" class="fp-state">
        <span class="mini-spin"></span>
        <span>正在加载预览…</span>
      </div>

      <!-- 错误态 -->
      <div v-else-if="error" class="fp-state fp-error">
        <el-icon><WarningFilled /></el-icon>
        <span>{{ error }}</span>
      </div>

      <template v-else-if="data">
        <!-- 图片：中性底 + contain 适配 + 点击放大（缩放/旋转） -->
        <div v-if="data.kind === 'image'" class="fp-image-wrap">
          <el-image
            v-if="data.objectUrl"
            class="fp-image"
            :src="data.objectUrl"
            :preview-src-list="[data.objectUrl]"
            fit="contain"
            preview-teleported
            hide-on-click-modal
          />
        </div>

        <!-- PDF：浏览器原生查看器 -->
        <iframe
          v-else-if="data.kind === 'pdf' && data.objectUrl"
          class="fp-pdf"
          :src="data.objectUrl"
          :title="data.name"
        ></iframe>

        <!-- Markdown -->
        <div
          v-else-if="data.kind === 'markdown'"
          class="fp-md"
          v-html="renderedMarkdown"
          @click="onCopyClick"
        ></div>

        <!-- 代码：行号槽 + 语法高亮 + 语言标签 -->
        <div v-else-if="data.kind === 'code'" class="fp-code-wrap">
          <div class="fp-code-gutter">
            <div v-for="n in lineCount" :key="n" class="fp-gutter-line">{{ n }}</div>
          </div>
          <pre class="fp-code"><code v-html="highlightedCode"></code></pre>
        </div>

        <!-- 表格：表头吸顶 + 斑马纹 + 行号 + 数字右对齐 -->
        <div v-else-if="data.kind === 'table'" class="fp-table-wrap">
          <table v-if="tableRows.length || tableHeader.length" class="fp-table">
            <thead>
              <tr>
                <th class="fp-rownum fp-corner"></th>
                <th v-for="(h, ci) in tableHeader" :key="ci" :class="{ num: isNum(h) }">
                  <span class="th-text" :title="h">{{ h }}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, ri) in tableRows" :key="ri">
                <td class="fp-rownum">{{ ri + 1 }}</td>
                <td
                  v-for="(cell, ci) in row"
                  :key="ci"
                  :class="{ num: isNum(cell) }"
                  :title="cell"
                >{{ cell }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="fp-state">（表格为空或无法解析）</div>
        </div>

        <!-- Office / 音频：按段落渲染（非裸 pre） -->
        <div v-else-if="data.kind === 'office'" class="fp-office">
          <template v-if="officeParagraphs.length">
            <p v-for="(p, i) in officeParagraphs" :key="i" class="fp-office-p">{{ p }}</p>
          </template>
          <div v-else class="fp-state">（未能提取到文本内容，可下载查看原文件）</div>
        </div>

        <!-- 纯文本：干净卡片排版 -->
        <pre v-else-if="data.text != null" class="fp-text">{{ data.text }}</pre>

        <!-- 二进制 / 未知：下载 + 元信息兜底 -->
        <div v-else class="fp-state">
          <el-icon><Document /></el-icon>
          <span>该文件类型（{{ data.mime || '未知' }}）暂不支持在线预览，可下载查看。</span>
        </div>
      </template>

      <div v-if="data?.truncated" class="fp-truncated">内容过长，仅显示前面部分（可下载完整文件）</div>
    </div>
  </aside>
</template>

<script setup lang="ts">
/**
 * 右侧临时文件预览区块（对齐 Trae / VS Code preview tab 语义）：
 * 点对话里的文件名 → 右侧出现临时预览 → 再点别的文件原位替换 → 可关闭。
 * 按类型深度优化展示：表格(数据网格) / 代码(行号+高亮) / 文本 / Markdown /
 * 图片 / PDF / Office(段落) / 二进制兜底。
 */
import { computed, ref } from 'vue'
import {
  Close, Document, Download, Folder, FullScreen, Grid, InfoFilled, Monitor, Notebook,
  Picture, Tickets, Top, WarningFilled,
} from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import 'highlight.js/styles/github.css'
import type { PreviewContent } from '@/api/v2'

const props = defineProps<{
  /** 已解析的预览内容（null 时仅显示文件名占位） */
  data?: PreviewContent | null
  name?: string
  loading?: boolean
  error?: string
}>()

const emit = defineEmits<{ close: []; download: []; 'open-original': [path: string] }>()

// ── 面板交互：拖拽调宽 + 放大（最大化）+ 代码块复制 ─────────────────────────
const panelWidth = ref(460)
const maximized = ref(false)

/** 左缘拖拽调宽（clamp 到 320px ~ 视口 75%） */
function startResize(e: MouseEvent) {
  e.preventDefault()
  const startX = e.clientX
  const startW = panelWidth.value
  const maxW = Math.min(window.innerWidth * 0.75, 1100)
  const onMove = (ev: MouseEvent) => {
    panelWidth.value = Math.min(maxW, Math.max(320, startW + (startX - ev.clientX)))
  }
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
  }
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

/** 代码块复制（事件委托）：内联 onclick 会被 DOMPurify 剥离，故用委托 */
function onCopyClick(e: MouseEvent) {
  const btn = (e.target as HTMLElement | null)?.closest?.('.copy-btn') as HTMLElement | null
  if (!btn) return
  const code = btn.closest('.code-block-wrapper')?.querySelector('code')?.textContent || ''
  const flash = (t: string) => {
    btn.textContent = t
    window.setTimeout(() => { btn.textContent = '复制' }, 1500)
  }
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(code).then(() => flash('已复制')).catch(() => flash('复制失败'))
  } else {
    flash('复制失败')
  }
}

/** 类型 -> 图标 / 标签 / 配色（头部视觉锚点） */
const KIND_META: Record<string, { label: string; icon: any; color: string; bg: string }> = {
  image: { label: '图片', icon: Picture, color: '#2f9e6f', bg: '#e8f6ef' },
  pdf: { label: 'PDF', icon: Document, color: '#d64545', bg: '#fdecec' },
  markdown: { label: 'Markdown', icon: Notebook, color: '#3b82f6', bg: '#e8f0fe' },
  code: { label: '代码', icon: Monitor, color: '#7c4dff', bg: '#f0eaff' },
  text: { label: '文本', icon: Document, color: '#606266', bg: '#f0f2f5' },
  table: { label: '表格', icon: Grid, color: '#1f8fde', bg: '#e7f2fc' },
  office: { label: '文档', icon: Tickets, color: '#c77700', bg: '#fdf2e0' },
  binary: { label: '文件', icon: Folder, color: '#909399', bg: '#f0f2f5' },
}
const meta = computed(() =>
  (props.data && KIND_META[props.data.kind]) || { label: '文件', icon: Document, color: '#909399', bg: '#f0f2f5' },
)

// ── 代码高亮 + 行号 ──────────────────────────────────────────────────────────
const highlightedCode = computed(() => {
  const code = props.data?.text || ''
  if (!code) return ''
  try {
    const lang = props.data?.language
    const html = lang && hljs.getLanguage(lang)
      ? hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
      : hljs.highlightAuto(code).value
    return DOMPurify.sanitize(html)
  } catch {
    return DOMPurify.sanitize(code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'))
  }
})
const lineCount = computed(() => (props.data?.text || '').split('\n').length)

// ── Markdown ─────────────────────────────────────────────────────────────────
const renderedMarkdown = computed(() => {
  const text = props.data?.text || ''
  if (!text) return ''
  try {
    return DOMPurify.sanitize(marked.parse(text, { breaks: true, gfm: true }) as string)
  } catch {
    return ''
  }
})

// ── 表格（数据网格）─────────────────────────────────────────────────────────
const tableCols = computed(() =>
  Math.max(0, ...(props.data?.rows || []).map((r) => r.length)),
)
const tableHeader = computed(() => {
  const r = props.data?.rows?.[0] || []
  return Array.from({ length: tableCols.value }, (_, i) => r[i] ?? '')
})
const tableRows = computed(() =>
  (props.data?.rows || []).slice(1).map((r) =>
    Array.from({ length: tableCols.value }, (_, i) => r[i] ?? ''),
  ),
)
/** 数字（含千分位 / 百分号 / 负号）右对齐 */
function isNum(v: string): boolean {
  return /^-?[\d,]+(\.\d+)?%?$/.test(String(v ?? '').trim())
}

// ── Office / 音频：按段落渲染 ────────────────────────────────────────────────
const officeParagraphs = computed(() =>
  (props.data?.text || '')
    .split(/\n+/)
    .map((s) => s.trim())
    .filter(Boolean),
)

// ── 文本抽取副本 -> 原文件富预览 ────────────────────────────────────────────
const originalPath = computed(() => {
  const p = props.data?.path || ''
  return p.endsWith('.extracted.txt') ? p.slice(0, -'.extracted.txt'.length) : ''
})
const originalName = computed(() => {
  const n = (props.data?.name || '').replace(/\.extracted\.txt$/i, '')
  return n.replace(/\\/g, '/').split('/').pop() || n
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
  min-width: 320px;
  border-left: 1px solid #e4e7ed;
  background: #fff;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
/* 左缘拖拽调宽 */
.fp-resize {
  position: absolute; left: 0; top: 0; bottom: 0; width: 6px; z-index: 5;
  cursor: col-resize; background: transparent;
}
.fp-resize:hover { background: rgba(64, 158, 255, 0.35); }

/* 放大（最大化）：覆盖式大面板 */
.file-preview.maximized {
  position: fixed; inset: 28px; z-index: 2001;
  width: auto; min-width: 0; height: auto;
  border: none; border-radius: 12px;
  box-shadow: 0 16px 56px rgba(0, 0, 0, 0.28);
}
.fp-backdrop {
  position: fixed; inset: 0; z-index: 2000; background: rgba(20, 24, 31, 0.42);
}

/* 头部 */
.fp-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px solid #e4e7ed;
  flex-shrink: 0;
}
.fp-kind-icon {
  width: 30px; height: 30px; border-radius: 8px; flex-shrink: 0;
  display: inline-flex; align-items: center; justify-content: center; font-size: 16px;
}
.fp-title { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.fp-name {
  font-weight: 600; font-size: 13px; color: #303133;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.fp-kind {
  flex-shrink: 0; font-size: 11px; font-weight: 500;
  border-radius: 4px; padding: 1px 7px;
}
.fp-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.fp-size { font-size: 11px; color: #b0b3b8; }
.fp-icon-btn {
  width: 26px; height: 26px; padding: 0; border: none; border-radius: 6px;
  background: transparent; color: #909399; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
}
.fp-icon-btn:hover { background: #f0f2f5; color: #409eff; }

.fp-path {
  padding: 5px 12px; font-size: 11px; color: #b0b3b8;
  border-bottom: 1px solid #f5f7fa;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: 'Consolas', monospace; flex-shrink: 0;
}

/* 文本抽取副本提示条 */
.fp-extracted-bar {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 12px; font-size: 11px; color: #c77700; background: #fdf7ec;
  border-bottom: 1px solid #f5e9d4; flex-shrink: 0;
}
.fp-extracted-bar span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fp-link-btn {
  display: inline-flex; align-items: center; gap: 2px;
  border: none; background: transparent; color: #409eff; cursor: pointer;
  font-size: 11px; padding: 2px 6px; border-radius: 4px; flex-shrink: 0;
}
.fp-link-btn:hover { background: #ecf5ff; }

/* 内容体 */
.fp-body { flex: 1; overflow: auto; padding: 12px; }
.fp-body.no-pad { padding: 0; overflow: hidden; display: flex; }

.fp-state {
  display: flex; align-items: center; gap: 8px; justify-content: center;
  padding: 40px 16px; color: #909399; font-size: 13px; text-align: center;
}
.fp-error { color: #f56c6c; }
.mini-spin {
  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
  background: #409eff; animation: fppulse 1.2s infinite ease-in-out;
}
@keyframes fppulse { 0%, 100% { opacity: 0.35; transform: scale(0.8); } 50% { opacity: 1; transform: scale(1); } }

/* 图片 */
.fp-image-wrap {
  flex: 1; display: flex; align-items: center; justify-content: center;
  padding: 16px; min-height: 240px;
  background:
    repeating-conic-gradient(#f6f7f9 0% 25%, #ffffff 0% 50%) 50% / 20px 20px;
}
.fp-image { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 6px; box-shadow: 0 2px 12px rgba(0,0,0,0.12); }

/* PDF */
.fp-pdf { flex: 1; width: 100%; height: 100%; min-height: 420px; border: none; display: block; background: #525659; }

/* Markdown */
.fp-md { line-height: 1.75; font-size: 13.5px; color: #303133; word-break: break-word; }
.fp-md :deep(h1), .fp-md :deep(h2), .fp-md :deep(h3) { font-weight: 650; color: #1a1a1a; margin: 14px 0 8px; line-height: 1.35; }
.fp-md :deep(h1) { font-size: 20px; } .fp-md :deep(h2) { font-size: 17px; } .fp-md :deep(h3) { font-size: 15px; }
.fp-md :deep(p) { margin: 8px 0; }
.fp-md :deep(ul), .fp-md :deep(ol) { padding-left: 22px; margin: 8px 0; }
.fp-md :deep(li) { margin: 4px 0; }
.fp-md :deep(code) { background: #eef1f5; color: #c7254e; padding: 2px 6px; border-radius: 4px; font-family: 'Consolas', monospace; font-size: 12px; }
/* 代码块 + 复制按钮（marked 全局渲染器注入 .code-block-wrapper/.copy-btn） */
.fp-md :deep(.code-block-wrapper) { position: relative; margin: 12px 0; }
.fp-md :deep(.copy-btn) {
  position: absolute; top: 8px; right: 8px; z-index: 1;
  background: rgba(255, 255, 255, 0.88); color: #606266;
  border: 1px solid #dcdfe6; border-radius: 5px;
  padding: 2px 10px; font-size: 11px; cursor: pointer; line-height: 1.6;
}
.fp-md :deep(.copy-btn:hover) { background: #fff; color: #409eff; border-color: #409eff; }
.fp-md :deep(pre) { background: #f6f8fa; border: 1px solid #eef1f5; border-radius: 8px; padding: 14px 16px; overflow-x: auto; margin: 0; }
.fp-md :deep(pre code) { background: none; color: #24292e; padding: 0; font-size: 12.5px; line-height: 1.65; }
.fp-md :deep(blockquote) { border-left: 3px solid #409eff; margin: 10px 0; padding: 4px 12px; color: #606266; background: #f6f9ff; border-radius: 0 6px 6px 0; }
.fp-md :deep(table) {
  border-collapse: separate; border-spacing: 0; width: 100%; margin: 12px 0; font-size: 12.5px;
  border: 1px solid #e8ebf0; border-radius: 8px; overflow: hidden;
}
.fp-md :deep(th), .fp-md :deep(td) { border-bottom: 1px solid #eef1f5; padding: 8px 12px; text-align: left; }
.fp-md :deep(th) { background: #f5f7fa; font-weight: 600; color: #303133; }
.fp-md :deep(tbody tr:nth-child(even)) { background: #fafbfc; }
.fp-md :deep(tbody tr:last-child td) { border-bottom: none; }
.fp-md :deep(img) { max-width: 100%; border-radius: 8px; margin: 8px 0; }
.fp-md :deep(hr) { border: none; border-top: 1px solid #e4e7ed; margin: 14px 0; }
.fp-md :deep(a) { color: #409eff; text-decoration: none; }
.fp-md :deep(a:hover) { text-decoration: underline; }

/* 代码：行号槽 + 高亮 */
.fp-code-wrap {
  display: flex; align-items: flex-start;
  border: 1px solid #eef1f5; border-radius: 8px; overflow: auto; background: #fff;
}
.fp-code-gutter {
  position: sticky; left: 0; z-index: 1; flex-shrink: 0;
  padding: 12px 0; background: #f7f9fb; border-right: 1px solid #eef1f5;
  user-select: none;
}
.fp-gutter-line {
  width: 40px; padding: 0 10px 0 8px; text-align: right;
  font-family: 'Consolas', monospace; font-size: 12px; line-height: 20px; color: #b0b3b8;
}
.fp-code {
  margin: 0; padding: 12px 16px; font-family: 'Consolas', monospace;
  font-size: 12px; line-height: 20px; color: #24292e;
  white-space: pre; flex: 1; min-width: 0;
}

/* 表格：数据网格 */
.fp-table-wrap {
  border: 1px solid #eef1f5; border-radius: 8px; overflow: auto; background: #fff;
  max-height: 100%;
}
.fp-table { border-collapse: separate; border-spacing: 0; font-size: 12.5px; min-width: 100%; }
.fp-table th, .fp-table td {
  padding: 7px 12px; white-space: nowrap; max-width: 260px;
  overflow: hidden; text-overflow: ellipsis; border-bottom: 1px solid #f0f2f5;
}
.fp-table thead th {
  position: sticky; top: 0; z-index: 2;
  background: #f5f7fa; color: #303133; font-weight: 600; text-align: left;
  border-bottom: 1px solid #e4e7ed;
}
.fp-table thead th .th-text { display: block; overflow: hidden; text-overflow: ellipsis; }
.fp-table tbody tr:nth-child(even) { background: #fafbfc; }
.fp-table tbody tr:hover { background: #ecf5ff; }
.fp-table td.num, .fp-table th.num { text-align: right; font-variant-numeric: tabular-nums; font-family: 'Consolas', monospace; }
/* 行号槽 */
.fp-rownum {
  position: sticky; left: 0; z-index: 1;
  width: 40px; min-width: 40px; text-align: center;
  color: #b0b3b8; background: #f7f9fb; font-family: 'Consolas', monospace;
  border-right: 1px solid #eef1f5; font-size: 11px;
}
.fp-table thead th.fp-corner { z-index: 3; background: #eef1f5; }
.fp-table tbody tr:hover .fp-rownum { background: #dcecff; color: #79a7e0; }

/* Office 段落 */
.fp-office { line-height: 1.8; font-size: 13.5px; color: #303133; }
.fp-office-p { margin: 0 0 10px; word-break: break-word; }

/* 纯文本 */
.fp-text {
  margin: 0; padding: 14px 16px; font-family: 'Consolas', monospace;
  font-size: 12.5px; line-height: 1.7; color: #303133;
  white-space: pre-wrap; word-break: break-word;
  background: #fafbfc; border: 1px solid #eef1f5; border-radius: 8px;
}

.fp-truncated {
  padding: 8px 14px; font-size: 11px; color: #e6a23c;
  border-top: 1px solid #f0f2f5; background: #fdf7ec; flex-shrink: 0;
}
</style>
