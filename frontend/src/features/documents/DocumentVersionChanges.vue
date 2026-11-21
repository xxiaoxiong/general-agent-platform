<script setup lang="ts">
import { computed } from 'vue'
import type { DocumentEditReview, DocumentVersion } from '@/api/documents'
import DocumentEditReviewCard from './DocumentEditReviewCard.vue'

const props = defineProps<{ version: DocumentVersion }>()

const hasChanges = computed(() => Boolean(props.version.version_diff?.sections?.length || props.version.version_diff?.office_changes?.length))
const changedSections = computed(() => {
  const diff = props.version.version_diff
  return [...new Set([
    ...(diff?.sections ?? []).map((section) => section.title_after || section.title_before || section.section_id),
    ...(diff?.office_changes ?? []).map((change) => change.section_title || change.title || ''),
  ].filter(Boolean))]
})
const review = computed<DocumentEditReview | undefined>(() => {
  const version = props.version
  if (!version.created_by_run_id || !hasChanges.value) return undefined
  return {
    ordinal: 1,
    source_name: version.filename,
    base_version: version.version_diff?.base_version,
    context_url: `/api/v2/runs/${version.created_by_run_id}/document-review/context`,
    changes: version.version_diff?.office_changes ?? [],
  }
})
</script>

<template>
  <div class="version-details">
    <p class="version-file">{{ version.filename }}</p>
    <p v-if="version.profile_id">
      <RouterLink :to="{ name: 'Documents', query: { tab: 'profiles', profile: String(version.profile_id), version: String(version.profile_version_no || '') } }">
        规范 #{{ version.profile_id }}<template v-if="version.profile_version_no"> · v{{ version.profile_version_no }}</template>
      </RouterLink>
    </p>
    <p v-if="version.change_summary">{{ version.change_summary }}</p>
    <section v-if="version.validation_report?.passed !== undefined" class="version-validation">
      <strong :data-validation="version.validation_report.passed">文档校验{{ version.validation_report.passed ? '通过' : '发现问题' }}</strong>
      <ul v-if="version.validation_report.issues?.length">
        <li v-for="(issue, index) in version.validation_report.issues" :key="index">{{ issue.message }}</li>
      </ul>
    </section>
    <section v-if="hasChanges">
      <h3>已应用的改动<template v-if="version.version_diff?.base_version"> · 基于 v{{ version.version_diff.base_version }}</template></h3>
      <ul v-if="changedSections.length" class="changed-sections"><li v-for="section in changedSections" :key="section">{{ section }}</li></ul>
      <DocumentEditReviewCard v-if="review" :key="review.context_url" :review="review" state="closed" applied />
    </section>
  </div>
</template>

<style scoped>
.version-details { overflow-wrap: anywhere; }
.version-details > p { margin: 5px 0; }
.version-file, .version-details h3 { color: var(--ui-text); font-weight: 600; }
.version-details a { color: var(--ui-accent); }
.version-validation { padding: 7px 0; }
.version-validation strong[data-validation="true"] { color: var(--ui-success); }
.version-validation strong[data-validation="false"] { color: var(--ui-danger); }
.version-validation ul { padding-inline-start: 20px; list-style: disc; }
.version-details h3 { margin: 9px 0 6px; }
.changed-sections { color: var(--ui-secondary); margin: 4px 0 10px; padding-inline-start: 22px; list-style: disc; }
</style>
