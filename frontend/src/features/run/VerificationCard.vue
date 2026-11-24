<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { CircleAlert, CircleCheck, Download, ListChecks, LoaderCircle } from '@lucide/vue'
import { downloadRunZip } from '@/api/artifacts'
import UiButton from '@/components/ui/UiButton.vue'
import type { VerificationBlock } from './blocks'

const props = defineProps<{ report: VerificationBlock; runId?: string }>()
const failed = computed(() => props.report.checks.filter(check => !check.passed))
const status = computed(() => ({ reworking: '正在补齐', passed: '验收通过', incomplete: '有未完成项' })[props.report.state])
const downloading = ref(false)
const error = ref('')
let request: AbortController | undefined

/** 产出文件汇总为一个 ZIP 下载（单文件获取走右侧文件面板，不再逐个列出下载） */
async function downloadZip() {
  if (downloading.value || !props.runId) return
  const controller = new AbortController()
  request = controller
  downloading.value = true
  error.value = ''
  try {
    await downloadRunZip(props.runId, `run-${props.runId}.zip`, controller.signal)
  } catch {
    if (!controller.signal.aborted) error.value = '打包下载失败，请重试。'
  } finally {
    if (!controller.signal.aborted) downloading.value = false
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
    <div v-if="report.artifacts.length" class="verification-deliverables">
      <UiButton v-if="runId" size="sm" variant="primary" aria-label="打包下载全部产出文件"
        :loading="downloading" :disabled="downloading" @click="downloadZip">
        <Download v-if="!downloading" aria-hidden="true" />下载全部（ZIP）
      </UiButton>
      <details class="verification-names">
        <summary>已交付 {{ report.artifacts.length }} 个文件</summary>
        <ul aria-label="产出文件">
          <li v-for="file in report.artifacts" :key="file.url"><code>{{ file.name }}</code></li>
        </ul>
      </details>
    </div>
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
.verification-deliverables {
  display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px;
  padding: 9px 12px; border-top: var(--ui-line);
}
.verification-names { flex: 1 1 160px; min-width: 0; font-size: 12px; color: var(--ui-secondary); }
.verification-names summary { cursor: pointer; list-style: none; }
.verification-names summary::-webkit-details-marker { display: none; }
.verification-names summary:hover { color: var(--ui-text); }
.verification-names ul { margin: 4px 0 0; padding: 0; list-style: none; }
.verification-names li { padding: 2px 0; overflow-wrap: anywhere; }
.verification-names code { font-family: var(--ui-font-mono); font-size: 11px; }
.verification-spinner { animation: verification-spin .9s linear infinite; }
@keyframes verification-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .verification-spinner { animation: none; } }
</style>
