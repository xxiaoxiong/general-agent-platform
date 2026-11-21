<script setup lang="ts">
import { computed, nextTick, onScopeDispose, ref, watch } from 'vue'
import { Check, ChevronRight, FileDiff, LocateFixed } from '@lucide/vue'
import {
  getDocumentReviewContext,
  type DocumentEditReview, type DocumentReviewChange, type DocumentReviewContext, type DocumentReviewDecision, type DocumentReviewGroup,
} from '@/api/documents'
import UiButton from '@/components/ui/UiButton.vue'
import DocumentReviewReading from './DocumentReviewReading.vue'

const props = defineProps<{
  review: DocumentEditReview
  state?: 'pending' | 'submitted' | 'closed'
  decision?: DocumentReviewDecision
  applied?: boolean
  busy?: boolean
  error?: string
}>()
const emit = defineEmits<{ submit: [acceptedIds: string[]] }>()

const reviewOpen = ref(false)
const context = ref<DocumentReviewContext>()
const loading = ref(false)
const loadError = ref('')
const groupId = ref('')
const proposalReading = ref<HTMLElement>()
const originalReading = ref<HTMLElement>()
const locatedChange = ref('')
const selections = ref(new Set<string>())
let loadController: AbortController | undefined
onScopeDispose(() => loadController?.abort())

const decision = computed(() => context.value?.decision ?? props.decision)
const canDecide = computed(() => props.state === 'pending' && !decision.value)
const groups = computed(() => context.value?.groups ?? [])
const activeGroup = computed(() => groups.value.find((group) => group.id === groupId.value))
const changes = computed(() => context.value?.changes ?? props.review.changes)
const knownChanges = computed(() => new Set(changes.value.map((change) => change.id)))
const contextualChanges = computed(() => new Set(groups.value.flatMap((group) => group.change_ids)))
const accepted = computed(() => new Set(decision.value?.accepted_change_ids ?? selections.value))
const selectedChanges = computed(() => [...accepted.value].filter((id) => knownChanges.value.has(id) && contextualChanges.value.has(id)))
const selectedChapters = computed(() => groups.value.filter((group) => group.change_ids.some((id) => accepted.value.has(id))).length)
const allChangesVisible = computed(() => knownChanges.value.size > 0 && [...knownChanges.value].every((id) => contextualChanges.value.has(id)))
const request = computed(() => context.value?.request || '')
const activeDetails = computed(() => {
  const ids = activeGroup.value?.change_ids ?? []
  return ids.map((id) => changes.value.find((change) => change.id === id)).filter((change) => change !== undefined)
})
const parentPath = computed(() => activeGroup.value ? chapterPath(activeGroup.value) : '')

const headingStatus = computed(() => {
  if (decision.value) {
    if (!accepted.value.size) return '已保留原文'
    if (props.applied) return decision.value.rejected_change_ids.length ? '已保存所选修改' : '已应用修改'
    return props.state === 'closed' ? '保存未完成' : '正在保存修改'
  }
  if (props.applied) return '已应用修改'
  if (props.state === 'pending') return '等待审阅'
  if (props.state === 'submitted') return '正在应用处理结果'
  return props.state === 'closed' ? '审阅已结束' : '正在准备审阅'
})

watch(() => props.decision, (decision) => {
  if (decision) reviewOpen.value = false
})

async function loadContext() {
  loadController?.abort()
  loadError.value = ''
  const path = props.review.context_url
  if (!path) {
    loadError.value = '这条审阅记录缺少可读取的章节上下文。'
    return
  }
  const controller = new AbortController()
  loadController = controller
  loading.value = true
  try {
    const response = await getDocumentReviewContext(path, controller.signal)
    if (controller.signal.aborted) return
    if (!response.groups.length) {
      loadError.value = '这条审阅记录缺少可读取的章节上下文。'
      return
    }
    context.value = response
    groupId.value = response.groups[0].id
  } catch {
    if (!controller.signal.aborted) {
      loadError.value = canDecide.value ? '暂时无法读取修改上下文。可以重新加载，或保留原文并结束这次修改。'
        : '暂时无法读取修改上下文。可以通过版本预览查看已保存的文档。'
    }
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}

function openReview() {
  reviewOpen.value = true
  if (!context.value && !loading.value) void loadContext()
}

function navigate(id: string) {
  groupId.value = id
  locatedChange.value = ''
  for (const viewport of [proposalReading.value, originalReading.value]) {
    if (viewport) viewport.scrollTop = 0
  }
}

function whereSelected(ids: string[]) {
  const count = ids.filter((id) => accepted.value.has(id)).length
  if (!count) return decision.value ? '保留原文' : ''
  return count === ids.length ? (props.applied ? '已应用' : '已选') : (props.applied ? '部分应用' : '部分已选')
}

function chapterPath(group: DocumentReviewGroup) {
  const path = group.path[group.path.length - 1] === group.title ? group.path.slice(0, -1) : group.path
  return path.join(' › ')
}

function toggleSelection(ids: string[]) {
  const include = !ids.every((id) => selections.value.has(id))
  for (const id of ids) {
    if (include) selections.value.add(id)
    else selections.value.delete(id)
  }
}

async function locate(id: string) {
  locatedChange.value = id
  await nextTick()

  const panes = [proposalReading.value, originalReading.value].filter((pane): pane is HTMLElement => !!pane)
    .map((viewport) => {
      const anchors = Array.from(viewport.querySelectorAll<HTMLElement>('[data-review-anchor]'))
      const index = anchors.findIndex((element) => element.dataset.changeIds?.split(' ').includes(id))
      return { viewport, anchors, index }
    })
  const source = panes.find((pane) => pane.index >= 0)
  if (!source) return

  for (const { viewport, anchors, index } of panes) {
    let target = anchors[index]
    if (!target) {
      // 单侧新增或删除时，另一侧定位到最近的共享锚点，便于对照上下文。
      const stable = new Map(anchors.map((element) => [element.dataset.reviewAnchor, element]))
      const neighbors = [...source.anchors.slice(0, source.index).reverse(), ...source.anchors.slice(source.index + 1)]
      target = neighbors.map((element) => stable.get(element.dataset.reviewAnchor)).find(Boolean) ?? anchors[0]
    }
    if (target) viewport.scrollTop += target.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 12
  }
}

const kinds: Record<DocumentReviewChange['kind'], string> = { text: '文字', image: '图片', table: '表格', other: '内容' }
const actions: Record<string, string> = {
  insert: '新增', add: '新增', remove: '删除', resize: '调整尺寸', replace: '替换', update: '调整',
  replace_text: '改写', replace_image: '替换', remove_image: '删除', resize_image: '调整尺寸',
  set_image_alt_text: '调整替代文字', set_image_caption: '调整图注',
}

function changeLabel(change: DocumentReviewChange) {
  const action = change.action || ''
  if (/[\u3400-\u9fff]/u.test(action)) return action
  return change.title || (actions[action] ? actions[action] + (kinds[change.kind] || '内容') : '调整' + (kinds[change.kind] || '内容'))
}

function changeExcerpt(change: DocumentReviewChange) {
  const text = (change.after || change.before || '').replace(/\s+/g, ' ').trim()
  return text.length > 48 ? text.slice(0, 48) + '…' : text
}
</script>

<template>
  <section class="review-card" aria-label="文档改动审阅">
    <div class="review-icon"><FileDiff :size="20" aria-hidden="true" /></div>
    <div class="review-heading">
      <div class="review-card-title">
        <strong>文档修改稿</strong>
        <span class="review-status" :data-active="canDecide">{{ headingStatus }}</span>
      </div>
      <p v-if="review.source_name" class="review-filename">{{ review.source_name }}<template v-if="review.base_version"> · 基于 v{{ review.base_version }}</template></p>
      <p v-if="review.summary" class="review-summary">{{ review.summary }}</p>
      <p v-if="error && canDecide && !reviewOpen" class="review-error" role="alert">{{ error }}</p>
    </div>
    <UiButton size="sm" :variant="canDecide ? 'primary' : 'secondary'" @click="openReview">
      {{ canDecide ? '查看修改稿' : '查看审阅记录' }}<ChevronRight aria-hidden="true" />
    </UiButton>
  </section>

  <el-dialog v-model="reviewOpen" title="审阅文档修改稿" width="min(1480px, calc(100vw - 24px))" top="min(5vh, 40px)"
    class="document-review-dialog" append-to-body :close-on-click-modal="false">
    <div class="dialog-overview">
      <p v-if="review.source_name">{{ review.source_name }}<template v-if="review.base_version"> · 原文 v{{ review.base_version }}</template></p>
      <details v-if="request" class="review-notice">
        <summary>查看修改要求</summary>
        <p>{{ request }}</p>
      </details>
      <details v-if="review.warnings?.length" class="review-notice review-warning">
        <summary>文档提示</summary>
        <ul><li v-for="(warning, index) in review.warnings" :key="index">{{ warning }}</li></ul>
      </details>
      <p v-if="applied && decision?.rejected_change_ids.length" class="partial-review-note">
        这里展示审阅时的修改稿。章节标记说明应用范围，已保存的内容可在版本预览中查看。
      </p>
    </div>

    <div v-if="loading" class="review-empty" role="status">正在读取原文和修改稿…</div>
    <div v-else-if="loadError" class="review-empty">
      <p role="alert">{{ loadError }}</p>
      <UiButton v-if="review.context_url" size="sm" @click="loadContext">重新加载</UiButton>
    </div>

    <div v-else-if="activeGroup" class="review-workspace">
      <nav class="review-navigation" aria-label="受影响的章节">
        <h3>修改章节</h3>
        <button v-for="group in groups" :key="group.id" type="button" class="chapter-link"
          :aria-current="group.id === groupId ? 'true' : undefined" @click="navigate(group.id)">
          <span>
            <strong>{{ group.title }}</strong>
            <small v-if="chapterPath(group)" class="chapter-parent">{{ chapterPath(group) }}</small>
            <small>{{ group.mode === 'rewrite' ? '章节重写' : group.change_ids.length + ' 处改动' }}</small>
          </span>
          <span v-if="whereSelected(group.change_ids)" class="chapter-selection"
            :data-retained="!group.change_ids.some((id) => accepted.has(id))">{{ whereSelected(group.change_ids) }}</span>
        </button>
        <details v-if="activeGroup.mode === 'edits' && activeDetails.length > 1" class="anchored-changes">
          <summary>逐处查看</summary>
          <div v-for="(change, index) in activeDetails" :key="change.id" class="anchored-change">
            <label v-if="canDecide" :title="changeLabel(change)">
              <input type="checkbox" :checked="accepted.has(change.id)" :disabled="busy"
                :aria-label="'选中第 ' + (index + 1) + ' 处改动'" @change="toggleSelection([change.id])" />
            </label>
            <button type="button" :class="{ located: locatedChange === change.id }" :title="changeLabel(change)"
              :aria-label="'定位第 ' + (index + 1) + ' 处改动'" @click="locate(change.id)">
              <LocateFixed :size="14" aria-hidden="true" />
              <span>{{ changeLabel(change) }}<small v-if="changeExcerpt(change)" class="anchored-excerpt">{{ changeExcerpt(change) }}</small></span>
            </button>
          </div>
        </details>
      </nav>

      <div class="review-document">
        <header class="reader-header">
          <div class="reader-context">
            <p v-if="parentPath" class="reader-path">{{ parentPath }}</p>
            <h3>{{ activeGroup.title }}</h3>
            <p v-if="activeGroup.mode === 'rewrite'" class="rewrite-note">
              {{ canDecide ? '此章节整体重写，按整节选择是否应用。' : '此章节整体重写。' }}
            </p>
          </div>
          <UiButton v-if="canDecide" size="sm" :variant="activeGroup.change_ids.every((id) => accepted.has(id)) ? 'primary' : 'secondary'"
            :disabled="busy || !activeGroup.change_ids.length" :aria-pressed="activeGroup.change_ids.every((id) => accepted.has(id))"
            @click="toggleSelection(activeGroup.change_ids)">
            <Check v-if="activeGroup.change_ids.every((id) => accepted.has(id))" aria-hidden="true" />
            {{ activeGroup.change_ids.every((id) => accepted.has(id)) ? '已选本章节' : '选中本章节' }}
          </UiButton>
        </header>
        <div class="reader-comparison">
          <div class="reader-pane" data-side="after">
            <h4>修改稿</h4>
            <div ref="proposalReading" class="reader-scroll" role="region" tabindex="0" aria-label="修改稿章节">
              <DocumentReviewReading :group="activeGroup" side="after" :located-change="locatedChange" />
            </div>
          </div>
          <div class="reader-pane" data-side="before">
            <h4>原文</h4>
            <div ref="originalReading" class="reader-scroll" role="region" tabindex="0" aria-label="原文章节">
              <DocumentReviewReading :group="activeGroup" side="before" :located-change="locatedChange" />
            </div>
          </div>
        </div>
      </div>
    </div>

    <template #footer>
      <p v-if="error && canDecide" class="review-error" role="alert">{{ error }}</p>
      <div v-if="canDecide" class="review-footer">
        <UiButton variant="ghost" :disabled="busy" @click="emit('submit', [])">保留原文</UiButton>
        <span v-if="groups.length" aria-live="polite">
          {{ selectedChapters ? '已选 ' + selectedChapters + ' 个章节的改动' : '可保存全部，或选中需要的章节' }}
        </span>
        <div class="save-actions">
          <UiButton :disabled="busy || !selectedChanges.length" @click="emit('submit', selectedChanges)">保存所选</UiButton>
          <UiButton variant="primary" :loading="busy" :disabled="!allChangesVisible" @click="emit('submit', [...knownChanges])">保存全部修改</UiButton>
        </div>
      </div>
      <UiButton v-else @click="reviewOpen = false">关闭</UiButton>
    </template>
  </el-dialog>
</template>

<style scoped>
.review-card {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 13px;
  background: var(--ui-bg);
  border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-md);
  color: var(--ui-text);
  min-width: 0;
}
.review-icon {
  flex: none;
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
  background: var(--ui-accent-soft);
  color: var(--ui-accent);
  border-radius: var(--ui-radius-sm);
}
.review-heading { flex: 1; min-width: 0; }
.review-card-title { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.review-card-title strong { font-size: 14px; }
.review-status {
  border-radius: var(--ui-radius-sm);
  padding: 2px 6px;
  background: var(--ui-subtle);
  color: var(--ui-secondary);
  font-size: 11px;
}
.review-status[data-active="true"] { background: var(--ui-accent-soft); color: var(--ui-accent); }
.review-filename, .review-summary { color: var(--ui-secondary); overflow-wrap: anywhere; font-size: 12px; margin: 4px 0 0; }
.review-summary { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; }
.dialog-overview { color: var(--ui-secondary); display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 18px; margin-bottom: 9px; font-size: 13px; }
.dialog-overview > p { margin: 0; overflow-wrap: anywhere; }
.partial-review-note { flex-basis: 100%; }
.review-notice { max-width: 100%; }
.review-notice[open] { flex-basis: 100%; }
.review-notice summary, .anchored-changes summary { cursor: pointer; }
.review-notice p { white-space: pre-wrap; max-height: 7rem; overflow: auto; margin: 4px 0 0; }
.review-notice ul { padding-inline-start: 20px; list-style: disc; margin: 4px 0; }
.review-warning { color: var(--ui-warning); }
.review-empty { display: grid; min-height: 14rem; align-content: center; justify-items: center; gap: 12px; text-align: center; background: var(--ui-bg); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); padding: 12px; }
.review-empty p { margin: 0; }
.review-workspace {
  display: grid;
  grid-template-columns: clamp(152px, 17vw, 220px) minmax(0, 1fr);
  height: clamp(250px, 67dvh, 760px);
  border-radius: var(--ui-radius-md);
  border: 1px solid var(--ui-border);
  overflow: hidden;
}
.review-navigation { min-width: 0; min-height: 0; overflow: auto; background: var(--ui-bg); border-inline-end: 1px solid var(--ui-border); padding: 14px 9px; }
.review-navigation h3 { margin: 0 7px 8px; font-size: 12px; font-weight: 650; color: var(--ui-secondary); }
.chapter-link {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 7px;
  appearance: none;
  background: transparent;
  border: 1px solid transparent;
  color: var(--ui-text);
  text-align: start;
  border-radius: var(--ui-radius-sm);
  font-size: 13px;
  font-family: inherit;
  line-height: 1.45;
  cursor: pointer;
}
.chapter-link strong { display: block; overflow-wrap: anywhere; font-weight: 650; }
.chapter-link small { color: var(--ui-secondary); }
.chapter-parent { display: block; overflow-wrap: anywhere; }
.chapter-link:hover { background: var(--ui-hover); }
.chapter-link[aria-current="true"] { color: var(--ui-accent); background: var(--ui-accent-soft); }
.chapter-link:focus-visible, .anchored-change button:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: -1px; }
.chapter-selection { flex-shrink: 0; font-size: 11px; color: var(--ui-success); }
.chapter-selection[data-retained="true"] { color: var(--ui-secondary); }
.anchored-changes { margin: 14px 7px; border-top: 1px solid var(--ui-border); padding: 10px 0; font-size: 12px; color: var(--ui-secondary); }
.anchored-change { display: flex; align-items: center; gap: 4px; margin-top: 5px; }
.anchored-change input { accent-color: var(--ui-accent); vertical-align: middle; }
.anchored-change button { display: flex; align-items: flex-start; gap: 5px; min-width: 0; padding: 3px; appearance: none; background: transparent; border: 0; color: var(--ui-secondary); text-align: start; font: inherit; cursor: pointer; }
.anchored-change button svg { flex-shrink: 0; margin-top: 2px; }
.anchored-change button:hover, .anchored-change button.located { color: var(--ui-accent); }
.anchored-excerpt { display: block; overflow-wrap: anywhere; color: var(--ui-secondary); }
.review-document { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.reader-header { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; padding: 10px 14px; border-bottom: 1px solid var(--ui-border); }
.reader-context { flex: 1 1 215px; min-width: 0; overflow-wrap: anywhere; }
.reader-context h3 { margin: 0; font-size: 15px; font-weight: 650; line-height: 1.3; color: var(--ui-text); }
.reader-path, .rewrite-note { margin: 3px 0; color: var(--ui-secondary); font-size: 12px; }
.reader-comparison { display: grid; flex: 1; grid-template-columns: repeat(2, minmax(0, 1fr)); min-height: 0; }
.reader-pane { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.reader-pane + .reader-pane { border-inline-start: 1px solid var(--ui-border); }
.reader-pane h4 { margin: 0; padding: 8px 14px; border-bottom: 1px solid var(--ui-border); background: var(--ui-bg); color: var(--ui-secondary); font-size: 13px; font-weight: 650; }
.reader-pane[data-side="after"] h4 { background: var(--ui-accent-soft); color: var(--ui-accent); }
.reader-scroll { flex: 1; overflow: auto; min-height: 0; overscroll-behavior: contain; padding: clamp(5px, 1.2vw, 16px); background: var(--ui-subtle); }
.reader-scroll:focus-visible { outline-offset: -2px; }
.review-footer { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; text-align: start; }
.review-footer > span { flex: 1; color: var(--ui-secondary); font-size: 12px; }
.save-actions { display: flex; align-items: center; gap: 8px; margin-inline-start: auto; }
.review-error { color: var(--ui-danger); font-size: 13px; margin: 4px 0; }
@media (max-width: 720px) {
  .review-card { flex-wrap: wrap; }
  .review-card > button { margin-inline-start: auto; }
  .review-workspace { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); height: clamp(230px, calc(100dvh - 238px), 62dvh); }
  .review-navigation { display: flex; align-items: flex-start; gap: 4px; overflow: auto; max-height: 28dvh; padding: 5px; border-inline-end: none; border-bottom: 1px solid var(--ui-border); }
  .review-navigation h3 { flex-shrink: 0; margin: 0 4px; }
  .chapter-link { flex: 0 0 auto; width: auto; max-width: 205px; padding: 5px; }
  .anchored-changes { flex-shrink: 0; margin: 0; padding: 5px; border: 0; }
  .reader-header { padding: 6px 9px; }
  .reader-pane h4 { padding: 6px 9px; }
  .reader-context { flex-basis: 100%; }
  .review-footer > span { min-width: 9rem; }
}
</style>

<style>
.document-review-dialog { display: flex; flex-direction: column; max-height: calc(100dvh - min(10dvh, 80px)); }
.document-review-dialog .el-dialog__header { margin-bottom: 0; padding-bottom: 14px; }
.document-review-dialog .el-dialog__body { padding-block: 7px 0; min-height: 0; overflow: auto; }
.document-review-dialog .el-dialog__footer { margin-top: 0; padding-top: 12px; }
@media (max-width: 720px) { .document-review-dialog { --el-dialog-padding-primary: 14px; } }
</style>
