<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { CircleAlert, CircleCheck, Download, ListChecks, LoaderCircle } from '@lucide/vue'
import { downloadRunArtifact } from '@/api/v2'
import UiButton from '@/components/ui/UiButton.vue'
import type { VerificationBlock } from './blocks'

const props = defineProps<{ report: VerificationBlock }>()
const failed = computed(() => props.report.checks.filter(check => !check.passed))
const status = computed(() => ({ reworking: '正在补齐', passed: '验收通过', incomplete: '有未完成项' })[props.report.state])
const downloading = ref('')
const error = ref('')
let request: AbortController | undefined

async function download(file: VerificationBlock['artifacts'][number]) {
  if (downloading.value) return
  request = new AbortController()
  const signal = request.signal
  downloading.value = file.url
  error.value = ''
  try {
    await downloadRunArtifact(file.url, file.name, signal)
  } catch {
    if (!signal.aborted) error.value = '下载失败，请重试。'
  } finally {
    downloading.value = ''
    request = undefined
  }
}

onBeforeUnmount(() => request?.abort())
</script>

<template>
  <section class="verification-card" aria-label="交付验收">
    <header class="verification-header">
      <ListChecks :size="17" aria-hidden="true" />
      <strong>交付验收</strong>
      <span class="verification-status" :data-state="report.state" role="status">
        <LoaderCircle v-if="report.state === 'reworking'" class="verification-spinner" :size="14" aria-hidden="true" />
        <CircleCheck v-else-if="report.state === 'passed'" :size="14" aria-hidden="true" />
        <CircleAlert v-else :size="14" aria-hidden="true" />
        {{ status }}<template v-if="report.rounds && report.state === 'reworking'"> · 第 {{ report.rounds }} 轮</template>
      </span>
    </header>
    <ul v-if="failed.length" class="verification-list" aria-label="待处理的验收项">
      <li v-for="check in failed" :key="check.id">{{ check.detail || '这项要求尚未核实' }}</li>
    </ul>
    <p v-if="report.state === 'incomplete' && report.stopReason === 'external_operation'" class="verification-note">
      本次执行已有外部操作回执或待核对记录，请核对后再决定是否继续。
    </p>
    <ul v-if="report.artifacts.length" class="verification-files" aria-label="可下载文件">
      <li v-for="file in report.artifacts" :key="file.url">
        <span>{{ file.name }}</span>
        <UiButton size="sm" variant="secondary" :aria-label="`下载 ${file.name}`" :loading="downloading === file.url"
          :disabled="!!downloading" @click="download(file)">
          <Download v-if="downloading !== file.url" aria-hidden="true" />下载
        </UiButton>
      </li>
    </ul>
    <p v-if="error" class="verification-note" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.verification-card { border: var(--ui-line); border-radius: var(--ui-radius-md); overflow: hidden; color: var(--ui-text); }
.verification-header { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; padding: 9px 12px; background: var(--ui-subtle); font-size: 13px; }
.verification-header > svg { color: var(--ui-accent); }
.verification-status { display: flex; align-items: center; gap: 4px; margin-inline-start: auto; color: var(--ui-info); font-size: 12px; }
.verification-status[data-state='passed'] { color: var(--ui-success); }
.verification-status[data-state='incomplete'] { color: var(--ui-warning); }
.verification-list { margin: 0; padding: 8px 12px 8px 29px; color: var(--ui-secondary); overflow-wrap: anywhere; font-size: 13px; }
.verification-list li + li { margin-top: 3px; }
.verification-note { padding: 6px 12px; font-size: 12px; color: var(--ui-secondary); }
.verification-note[role='alert'] { color: var(--ui-danger); }
.verification-files { margin: 0; padding: 0; list-style: none; }
.verification-files li { border-top: var(--ui-line); display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 12px; font-size: 13px; }
.verification-files span { min-width: 0; overflow-wrap: anywhere; }
.verification-spinner { animation: verification-spin .9s linear infinite; }
@keyframes verification-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .verification-spinner { animation: none; } }
</style>
