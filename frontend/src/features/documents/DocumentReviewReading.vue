<script setup lang="ts">
import { computed } from 'vue'
import type { DocumentReviewBlock, DocumentReviewGroup } from '@/api/documents'
import DocumentReviewImage from './DocumentReviewImage.vue'

const props = defineProps<{
  group: DocumentReviewGroup
  side: 'before' | 'after'
  locatedChange?: string
}>()

const sections = computed(() => props.group[props.side])
const alignedParagraphs = computed(() => {
  const result = new Map<string, DocumentReviewBlock>()
  if (props.group.mode !== 'edits') return result
  const opposite = props.side === 'after' ? 'before' : 'after'
  for (const section of props.group[opposite]) {
    for (const block of section.blocks) {
      if (block.type === 'paragraph') result.set(`${section.id}/${block.id}`, block)
    }
  }
  return result
})

function paragraphParts(sectionId: string, block: DocumentReviewBlock) {
  const text = block.text || ''
  const opposite = alignedParagraphs.value.get(`${sectionId}/${block.id}`)
  if (!opposite || !block.change_ids?.some((id) => opposite.change_ids?.includes(id))) return { lead: text, mark: '', tail: '' }

  const here = Array.from(text)
  const there = Array.from(opposite.text || '')
  const limit = Math.min(here.length, there.length)
  let prefix = 0
  let suffix = 0
  while (prefix < limit && here[prefix] === there[prefix]) prefix++
  while (suffix < limit - prefix && here[here.length - suffix - 1] === there[there.length - suffix - 1]) suffix++
  const end = here.length - suffix
  return { lead: here.slice(0, prefix).join(''), mark: here.slice(prefix, end).join(''), tail: here.slice(end).join('') }
}

function headingElement(level: number) {
  return level <= 1 ? 'h2' : level === 2 ? 'h3' : 'h4'
}
</script>

<template>
  <article class="review-reading" :data-side="side" :aria-label="side === 'after' ? '修改稿正文' : '原文正文'">
    <p v-if="!sections.length" class="reading-empty">{{ side === 'after' ? '修改稿中已移除此章节。' : '这一章节在修改稿中新增。' }}</p>
    <section v-for="section in sections" :key="section.id" class="reading-section" :data-review-anchor="section.id">
      <component :is="headingElement(section.level)" v-if="section.title" class="reading-heading">{{ section.title }}</component>
      <template v-for="block in section.blocks" :key="block.id">
        <div class="reading-block" :data-review-anchor="`${section.id}/${block.id}`"
          :data-change-ids="group.mode === 'edits' ? block.change_ids?.join(' ') : undefined"
          :data-changed="group.mode === 'edits' && Boolean(block.change_ids?.length)"
          :class="{ 'is-located': locatedChange && block.change_ids?.includes(locatedChange) }">
          <template v-if="block.type === 'paragraph'">
            <p v-for="part in [paragraphParts(section.id, block)]" :key="block.id">{{ part.lead }}<mark v-if="part.mark">{{ part.mark }}</mark>{{ part.tail }}</p>
          </template>
          <component :is="block.ordered ? 'ol' : 'ul'" v-else-if="block.type === 'list'">
            <li v-for="(item, index) in block.items" :key="index">{{ item }}</li>
          </component>
          <div v-else-if="block.type === 'table' && block.rows?.length" class="table-overflow">
            <table>
              <thead v-if="block.header"><tr><th v-for="(cell, index) in block.rows[0]" :key="index" scope="col">{{ cell }}</th></tr></thead>
              <tbody>
                <tr v-for="(row, rowIndex) in (block.header ? block.rows.slice(1) : block.rows)" :key="rowIndex">
                  <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <figure v-else-if="block.type === 'image'">
            <DocumentReviewImage v-if="block.image_url" :path="block.image_url" :alt="block.text || '文档图片'" />
            <figcaption v-if="block.text || !block.image_url">{{ block.text || '文档图片' }}</figcaption>
          </figure>
        </div>
      </template>
    </section>
  </article>
</template>

<style scoped>
.review-reading {
  max-width: 76ch;
  min-height: 24rem;
  margin: 0 auto;
  padding: clamp(12px, 2vw, 28px);
  color: var(--ui-text);
  background: var(--ui-surface);
  border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-md);
  line-height: 1.75;
  font-size: 14px;
  overflow-wrap: anywhere;
}
.reading-section + .reading-section { margin-top: 1.7em; }
.reading-heading { margin: 0 0 0.7em; color: var(--ui-text); line-height: 1.36; font-size: 16px; font-weight: 650; }
h2.reading-heading { font-size: clamp(19px, 2.4vw, 24px); }
h3.reading-heading { font-size: 17px; }
.reading-block { position: relative; margin: 0 0 0.85em; scroll-margin-block: 12px; }
.reading-block:last-child { margin-bottom: 0; }
.reading-block[data-changed="true"]::before {
  content: '';
  position: absolute;
  inset-block: 0;
  inset-inline-start: -12px;
  width: 2px;
  border-radius: 2px;
  background: var(--ui-accent);
}
.reading-block.is-located { outline: 2px solid var(--ui-accent); outline-offset: 3px; border-radius: var(--ui-radius-sm); }
.reading-block p { margin: 0; white-space: pre-wrap; }
.reading-block ol, .reading-block ul { margin: 0; padding-inline-start: 1.65em; }
.reading-block ol { list-style: decimal; }
.reading-block ul { list-style: disc; }
.reading-block li { white-space: pre-wrap; }
.table-overflow { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: start; line-height: 1.55; }
th, td { padding: 7px 9px; border: 1px solid var(--ui-border); vertical-align: top; white-space: pre-wrap; }
th { color: var(--ui-text); background: var(--ui-subtle); font-weight: 650; text-align: start; }
figure { margin: 0; }
figcaption { margin-top: 3px; color: var(--ui-secondary); font-size: 13px; white-space: pre-wrap; }
mark { padding-block: 1px; background: var(--ui-danger-soft); color: var(--ui-text); }
[data-side="after"] mark { background: var(--ui-success-soft); }
.reading-empty { color: var(--ui-secondary); }
</style>
