<template>
  <!-- 文本抽取副本提示：一键切换到原文件富预览 -->
  <div v-if="originalPath" class="fp-extracted-bar">
    <Info :size="15" />
    <span>此为「{{ originalName }}」的文本抽取副本</span>
    <button type="button" class="fp-link-btn" @click="emit('open-original', originalPath)">
      查看原文件
    </button>
  </div>

  <!-- 内容体（预览面板与工作目录文件面板共用） -->
  <div class="fp-body" :class="{ 'no-pad': data?.kind === 'image' || data?.kind === 'pdf' }">
    <!-- 加载态 -->
    <div v-if="loading" class="fp-state" role="status">
      <LoaderCircle class="mini-spin" :size="16" />
      <span>正在加载预览…</span>
    </div>

    <!-- 错误态 -->
    <div v-else-if="error" class="fp-state fp-error" role="alert">
      <CircleAlert :size="16" />
      <span>{{ error }}</span>
    </div>

    <template v-else-if="data">
      <slot v-if="$slots.document" name="document" />
      <!-- 图片：中性底 + contain 适配 + 点击放大（缩放/旋转） -->
      <div v-else-if="data.kind === 'image'" class="fp-image-wrap">
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
      <MessageMarkdown
        v-else-if="data.kind === 'markdown'"
        :text="data.text || ''"
      />

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
        <File :size="16" />
        <span>该文件类型（{{ data.mime || '未知' }}）暂不支持在线预览，可下载查看。</span>
      </div>
    </template>

    <div v-if="data?.truncated" class="fp-truncated">内容过长，仅显示前面部分（可下载完整文件）</div>
  </div>
</template>

<script setup lang="ts">
/**
 * 文件预览内容体（按类型深度优化展示）：表格(数据网格) / 代码(行号+高亮) /
 * 文本 / Markdown / 图片 / PDF / Office(段落) / 二进制兜底。
 * 预览面板与工作目录文件面板共用同一渲染。
 */
import { computed } from 'vue'
import { CircleAlert, File, Info, LoaderCircle } from '@lucide/vue'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import type { PreviewContent } from '@/api/conversations'
import MessageMarkdown from '@/features/run/MessageMarkdown.vue'

const props = defineProps<{
  /** 已解析的预览内容（null 时仅显示占位） */
  data?: PreviewContent | null
  loading?: boolean
  error?: string
}>()

const emit = defineEmits<{ 'open-original': [path: string] }>()

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
</script>

<style scoped>
/* 文本抽取副本提示条 */
.fp-extracted-bar {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 12px; font-size: 11px; color: var(--ui-warning); background: var(--ui-warning-soft);
  border-bottom: 1px solid var(--ui-border); flex-shrink: 0;
}
.fp-extracted-bar span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fp-link-btn {
  display: inline-flex; align-items: center; gap: 2px;
  border: none; background: transparent; color: var(--ui-accent); cursor: pointer;
  font-size: 11px; padding: 2px 6px; border-radius: var(--ui-radius-sm); flex-shrink: 0;
}
.fp-link-btn:hover { background: var(--ui-accent-soft); }

/* 内容体 */
.fp-body { flex: 1; min-height: 0; overflow: auto; padding: 12px; }
.fp-body.no-pad { padding: 0; overflow: hidden; display: flex; }

.fp-state {
  display: flex; align-items: center; gap: 8px; justify-content: center;
  padding: 40px 16px; color: var(--ui-secondary); font-size: 13px; text-align: center;
}
.fp-error { color: var(--ui-danger); }
/* 图片 */
.fp-image-wrap {
  flex: 1; display: flex; align-items: center; justify-content: center;
  padding: 16px; min-height: 240px;
  background:
    repeating-conic-gradient(var(--ui-subtle) 0% 25%, var(--ui-surface) 0% 50%) 50% / 20px 20px;
}
.fp-image { max-width: 100%; max-height: 100%; object-fit: contain; border-radius: var(--ui-radius-sm); box-shadow: var(--ui-shadow-md); }

/* PDF */
.fp-pdf { flex: 1; width: 100%; height: 100%; border: none; display: block; background: var(--ui-code-bg); }

/* 代码：行号槽 + 高亮 */
.fp-code-wrap {
  display: flex; align-items: flex-start;
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); overflow: auto; background: var(--ui-surface);
}
.fp-code-gutter {
  position: sticky; left: 0; z-index: 1; flex-shrink: 0;
  padding: 12px 0; background: var(--ui-subtle); border-right: 1px solid var(--ui-border);
  user-select: none;
}
.fp-gutter-line {
  width: 40px; padding: 0 10px 0 8px; text-align: right;
  font-family: var(--ui-font-mono); font-size: 12px; line-height: 20px; color: var(--ui-muted);
}
.fp-code {
  margin: 0; padding: 12px 16px; font-family: var(--ui-font-mono);
  font-size: 12px; line-height: 20px; color: var(--ui-text);
  white-space: pre; flex: 1; min-width: 0;
}

/* 表格：数据网格 */
.fp-table-wrap {
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); overflow: auto; background: var(--ui-surface);
  max-height: 100%;
}
.fp-table { border-collapse: separate; border-spacing: 0; font-size: 12.5px; min-width: 100%; }
.fp-table th, .fp-table td {
  padding: 7px 12px; white-space: nowrap; max-width: 260px;
  overflow: hidden; text-overflow: ellipsis; border-bottom: 1px solid var(--ui-hover);
}
.fp-table thead th {
  position: sticky; top: 0; z-index: 2;
  background: var(--ui-subtle); color: var(--ui-text); font-weight: 600; text-align: left;
  border-bottom: 1px solid var(--ui-border);
}
.fp-table thead th .th-text { display: block; overflow: hidden; text-overflow: ellipsis; }
.fp-table tbody tr:nth-child(even) { background: var(--ui-bg); }
.fp-table tbody tr:hover { background: var(--ui-accent-soft); }
.fp-table td.num, .fp-table th.num { text-align: right; font-variant-numeric: tabular-nums; font-family: var(--ui-font-mono); }
/* 行号槽 */
.fp-rownum {
  position: sticky; left: 0; z-index: 1;
  width: 40px; min-width: 40px; text-align: center;
  color: var(--ui-muted); background: var(--ui-subtle); font-family: var(--ui-font-mono);
  border-right: 1px solid var(--ui-border); font-size: 11px;
}
.fp-table thead th.fp-corner { z-index: 3; background: var(--ui-border); }
.fp-table tbody tr:hover .fp-rownum { background: var(--ui-accent-soft); color: var(--ui-accent); }

/* Office 段落 */
.fp-office { line-height: 1.8; font-size: 13.5px; color: var(--ui-text); }
.fp-office-p { margin: 0 0 10px; word-break: break-word; }

/* 纯文本 */
.fp-text {
  margin: 0; padding: 14px 16px; font-family: var(--ui-font-mono);
  font-size: 12.5px; line-height: 1.7; color: var(--ui-text);
  white-space: pre-wrap; word-break: break-word;
  background: var(--ui-bg); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md);
}

.fp-truncated {
  padding: 8px 14px; font-size: 11px; color: var(--ui-warning);
  border-top: 1px solid var(--ui-hover); background: var(--ui-warning-soft); flex-shrink: 0;
}
.fp-code :deep(.hljs-comment) { color: var(--ui-muted); }
.fp-code :deep(.hljs-string) { color: var(--ui-success); }
.fp-code :deep(.hljs-keyword), .fp-code :deep(.hljs-built_in), .fp-code :deep(.hljs-literal) { color: var(--ui-accent); }
.fp-code :deep(.hljs-number), .fp-code :deep(.hljs-title), .fp-code :deep(.hljs-attr) { color: var(--ui-info); }
.mini-spin { flex: none; animation: fp-spin 1s linear infinite; }
@keyframes fp-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .mini-spin { animation: none; } }
</style>
