<script setup lang="ts">
import { computed } from 'vue'
import type { DocumentDraft } from '@/api/documents'

const props = defineProps<{ draft: DocumentDraft }>()
const sections = computed(() => [...props.draft.sections].sort((a, b) => a.order - b.order))
</script>

<template>
  <article class="document-page" aria-label="文档内容">
    <header>
      <h1>{{ draft.title }}</h1>
      <p v-if="draft.subtitle" class="subtitle">{{ draft.subtitle }}</p>
      <dl v-if="Object.keys(draft.metadata).length" class="metadata">
        <template v-for="(value, key) in draft.metadata" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></template>
      </dl>
      <p v-if="draft.abstract" class="abstract">{{ draft.abstract }}</p>
    </header>
    <section v-for="section in sections" :key="section.id">
      <component :is="section.level === 1 ? 'h2' : section.level === 2 ? 'h3' : 'h4'">{{ section.title }}</component>
      <template v-for="(block, index) in section.blocks" :key="block.id || index">
        <p v-if="block.type === 'paragraph'" :class="block.style">{{ block.text }}</p>
        <component :is="block.ordered ? 'ol' : 'ul'" v-else-if="block.type === 'list'">
          <li v-for="(item, row) in block.items" :key="row">{{ item }}</li>
        </component>
        <div v-else class="table-scroll">
          <table>
            <thead v-if="block.header && block.rows.length"><tr><th v-for="(cell, column) in block.rows[0]" :key="column" scope="col">{{ cell }}</th></tr></thead>
            <tbody>
              <tr v-for="(row, rowIndex) in block.header ? block.rows.slice(1) : block.rows" :key="rowIndex">
                <td v-for="(cell, column) in row" :key="column">{{ cell }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </section>
    <footer v-if="draft.sources.length || draft.missing_fields.length">
      <details v-if="draft.sources.length">
        <summary>来源（{{ draft.sources.length }}）</summary>
        <ul><li v-for="source in draft.sources" :key="source.id">
          <strong>{{ source.label }}</strong><span v-if="source.detail"> · {{ source.detail }}</span><span v-if="source.locator"> · {{ source.locator }}</span>
        </li></ul>
      </details>
      <p v-if="draft.missing_fields.length" class="missing">待补充字段 {{ draft.missing_fields.join('、') }}</p>
    </footer>
  </article>
</template>

<style scoped>
.document-page {
  max-width: 780px; margin: 0 auto; padding: clamp(14px, 4%, 36px); overflow-wrap: anywhere;
  background: var(--ui-bg); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm);
  color: var(--ui-text); font-size: 14px; line-height: 1.65;
}
.document-page h1 { margin: 0; font-size: clamp(20px, 3vw, 27px); line-height: 1.3; letter-spacing: -.015em; }
.document-page h2, .document-page h3, .document-page h4 { margin: 20px 0 7px; line-height: 1.4; }
.document-page h2 { font-size: 18px; }
.document-page h3 { font-size: 16px; }
.document-page h4 { font-size: 14px; }
.document-page p { margin: 7px 0; white-space: pre-wrap; }
.document-page ul, .document-page ol { margin: 7px 0; padding-inline-start: 22px; }
.document-page ul { list-style: disc; }
.document-page ol { list-style: decimal; }
.subtitle, .caption, .metadata { color: var(--ui-muted); }
.metadata { display: grid; grid-template-columns: max-content 1fr; gap: 3px 12px; font-size: 12px; }
.metadata dd { margin: 0; }
.abstract, .quote { padding: 5px 10px; border-inline-start: 2px solid var(--ui-border); color: var(--ui-secondary); }
.table-scroll { margin: 10px 0; overflow-x: auto; }
.document-page table { width: 100%; border-collapse: collapse; font-size: 13px; }
.document-page th, .document-page td { min-width: 64px; padding: 5px 8px; border: 1px solid var(--ui-border); text-align: start; white-space: pre-wrap; }
.document-page th { background: var(--ui-subtle); }
.document-page footer { margin-top: 22px; padding-top: 9px; border-top: 1px solid var(--ui-border); font-size: 12px; color: var(--ui-secondary); }
.document-page summary { cursor: pointer; }
.missing { color: var(--ui-warning); }
</style>
