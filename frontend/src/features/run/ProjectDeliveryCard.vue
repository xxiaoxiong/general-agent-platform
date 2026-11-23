<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { ChevronRight, CircleAlert, CircleCheck, CircleMinus, Download, FolderArchive, LoaderCircle, Wrench } from '@lucide/vue'
import { downloadProjectZip } from '@/api/artifacts'
import UiButton from '@/components/ui/UiButton.vue'
import {
  describeProjectScope, projectCheckLabels, projectPhaseLabels,
  type ProjectDelivery, type ProjectProgress,
} from './projectDelivery'

const props = defineProps<{
  delivery?: ProjectDelivery
  progress?: ProjectProgress
  active: boolean
  repairable?: boolean
  repairDisabled?: boolean
  repairing?: boolean
  repairError?: string
}>()
const emit = defineEmits<{ repair: [] }>()

const downloading = ref(false)
const downloadError = ref('')
let request: AbortController | undefined
const checks = computed(() => {
  const all = props.delivery?.checks || []
  return {
    passed: all.filter((check) => check.status === 'passed').length,
    failed: all.filter((check) => check.status === 'failed').length,
    skipped: all.filter((check) => check.status === 'skipped').length,
  }
})
const phaseLabel = computed(() => props.progress ? projectPhaseLabels[props.progress.phase] : '')
const badgeText = computed(() => {
  if (props.delivery?.state !== 'passed') return '项目草稿'
  if (props.delivery.verification_scope === 'static') return '静态检查通过'
  return props.delivery.checks.some(check => check.id.startsWith('runtime.') && check.status === 'skipped')
    ? '已运行部分检查' : '隔离运行通过'
})
const checkIcons = { passed: CircleCheck, failed: CircleAlert, skipped: CircleMinus }
const issueFiles = computed(() => {
  const files = new Map<string, string[]>()
  for (const issue of props.delivery?.issues || []) {
    const messages = files.get(issue.path) || []
    if (!messages.includes(issue.message)) messages.push(issue.message)
    files.set(issue.path, messages)
  }
  return [...files.entries()]
})

function formatBytes(size: number) {
  if (size >= 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MB`
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${size} B`
}

async function download() {
  if (downloading.value || !props.delivery) return
  const { download_url, filename } = props.delivery
  const controller = new AbortController()
  request = controller
  downloading.value = true
  downloadError.value = ''
  try {
    await downloadProjectZip(download_url, filename, controller.signal)
  } catch (error) {
    if (controller.signal.aborted) return
    const status = (error as { response?: { status?: number } })?.response?.status
    if (status === 404) downloadError.value = '项目文件已不存在。'
    else if (status === 403) downloadError.value = '当前账号无法下载此项目。'
    else downloadError.value = '项目下载失败，请重试。'
  } finally {
    if (!controller.signal.aborted) downloading.value = false
    request = undefined
  }
}

onBeforeUnmount(() => request?.abort())
</script>

<template>
  <article class="project-card" aria-label="项目交付" :data-state="delivery?.state">
    <header class="project-head">
      <span class="archive-icon"><FolderArchive :size="21" aria-hidden="true" /></span>
      <div class="project-name">
        <h3>{{ delivery?.title || progress?.title || '项目交付' }}</h3>
        <p v-if="delivery" class="project-file">
          <span>{{ delivery.filename }}</span><span>{{ delivery.file_count }} 个文件 · {{ formatBytes(delivery.size) }}</span>
        </p>
        <p v-else-if="active" class="project-phase" role="status">
          <LoaderCircle class="project-spinner" :size="14" aria-hidden="true" />{{ phaseLabel }}
        </p>
        <p v-else class="project-phase">交付未完成</p>
      </div>
      <span v-if="delivery" class="delivery-badge" :data-state="delivery.state">
        <component :is="delivery.state === 'passed' ? CircleCheck : CircleAlert" :size="14" aria-hidden="true" />
        {{ badgeText }}
      </span>
      <div v-else-if="progress?.files" class="file-progress">
        <span>已写入 {{ progress.files.written }} / {{ progress.files.total }} 个文件</span>
        <progress :value="progress.files.written" :max="progress.files.total" aria-label="已写入项目文件"></progress>
      </div>
    </header>

    <p v-if="!delivery && progress?.detail" class="project-note">{{ progress.detail }}</p>

    <template v-if="delivery">
      <div class="project-actions">
        <UiButton v-if="repairable" size="sm" variant="primary" :loading="repairing" :disabled="repairDisabled"
          @click="emit('repair')">
          <Wrench v-if="!repairing" aria-hidden="true" />继续修复草稿
        </UiButton>
        <UiButton size="sm" :variant="delivery.state === 'passed' ? 'primary' : 'secondary'"
          :loading="downloading" @click="download">
          <Download v-if="!downloading" aria-hidden="true" />
          {{ delivery.state === 'passed' ? '下载项目 ZIP' : '下载草稿 ZIP' }}
        </UiButton>
        <p class="project-scope">{{ describeProjectScope(delivery.verification_scope, delivery.checks) }}</p>
      </div>
      <p v-if="downloadError" role="alert" class="download-error">{{ downloadError }}</p>
      <p v-if="repairError" role="alert" class="download-error">{{ repairError }}</p>
      <details v-if="issueFiles.length" class="project-checks" open>
        <summary>
          <ChevronRight class="check-chevron" :size="15" aria-hidden="true" />
          <span>需要修复的文件</span>
          <span class="check-summary">{{ issueFiles.length }} 个</span>
        </summary>
        <ul aria-label="需要修复的文件">
          <li v-for="[path, findings] in issueFiles" :key="path" data-state="failed">
            <CircleAlert class="check-icon" :size="16" aria-hidden="true" />
            <div class="check-copy">
              <strong><code>{{ path }}</code></strong>
              <p v-for="finding in findings" :key="finding">{{ finding }}</p>
            </div>
          </li>
        </ul>
      </details>
      <details class="project-checks" :open="delivery.state === 'failed' && !issueFiles.length">
        <summary>
          <ChevronRight class="check-chevron" :size="15" aria-hidden="true" />
          <span>验收明细</span>
          <span class="check-summary">
            {{ checks.passed }} 通过<template v-if="checks.failed"> · {{ checks.failed }} 未通过</template>
            <template v-if="checks.skipped"> · {{ checks.skipped }} 未执行</template>
          </span>
        </summary>
        <ul v-if="delivery.checks.length" aria-label="验收检查">
          <li v-for="check in delivery.checks" :key="check.id" :data-state="check.status">
            <component :is="checkIcons[check.status]" class="check-icon" :size="16" aria-hidden="true" />
            <div class="check-copy">
              <strong>{{ check.label }}</strong>
              <p v-if="check.detail">{{ check.detail }}</p>
            </div>
            <span class="check-state">{{ projectCheckLabels[check.status] }}</span>
          </li>
        </ul>
        <p v-else class="project-note">本次运行未留下可查的验收结果。</p>
      </details>
    </template>
  </article>
</template>

<style scoped>
.project-card {
  min-width: 0; border: var(--ui-line); border-radius: var(--ui-radius-lg);
  background: var(--ui-surface); overflow: hidden;
}
.project-head, .project-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; padding: 12px 13px; }
.project-head { background: var(--ui-subtle); }
.archive-icon {
  width: 39px; height: 39px; flex: none; display: grid; place-items: center;
  color: var(--ui-accent); background: var(--ui-accent-soft); border-radius: var(--ui-radius-md);
}
.project-name { flex: 1 1 130px; min-width: 0; overflow-wrap: anywhere; }
.project-name h3 { font-size: 14px; line-height: 1.4; font-weight: 700; }
.project-file, .project-phase, .project-scope, .project-note { color: var(--ui-secondary); font-size: 12px; }
.project-file, .project-phase { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 8px; margin-top: 2px; }
.project-phase { align-items: center; }
.file-progress { display: grid; gap: 4px; color: var(--ui-secondary); font-size: 11px; }
.file-progress progress { width: 100%; height: 4px; accent-color: var(--ui-accent); }
.project-spinner { animation: zip-spin 1s linear infinite; }
@keyframes zip-spin { to { transform: rotate(360deg); } }
.project-actions { border-top: var(--ui-line); }
.project-scope { flex: 1 1 170px; min-width: 0; overflow-wrap: anywhere; }
.delivery-badge { flex: none; display: inline-flex; align-items: center; gap: 4px; font-size: 12px; padding: 3px 6px; border-radius: 5px; }
.delivery-badge[data-state='passed'] { background: var(--ui-success-soft); color: var(--ui-success); }
.delivery-badge[data-state='failed'] { background: var(--ui-warning-soft); color: var(--ui-warning); }
.download-error, .project-note { padding: 8px 13px; white-space: pre-wrap; overflow-wrap: anywhere; }
.download-error { color: var(--ui-danger); font-size: 12px; padding-top: 0; }
.project-checks { border-top: var(--ui-line); font-size: 13px; }
.project-checks summary { display: flex; align-items: center; gap: 7px; padding: 9px 13px; list-style: none; cursor: pointer; }
.project-checks summary::-webkit-details-marker { display: none; }
.project-checks summary:hover { background: var(--ui-hover); }
.project-checks summary:focus-visible { outline: var(--ui-focus); outline-offset: -2px; }
.check-chevron { flex: none; color: var(--ui-muted); transition: transform var(--ui-duration) var(--ui-ease); }
.project-checks[open] .check-chevron { transform: rotate(90deg); }
.check-summary { margin-inline-start: auto; color: var(--ui-secondary); font-size: 11px; text-align: end; }
.project-checks ul { padding: 0 13px; margin: 0; list-style: none; }
.project-checks li { display: flex; align-items: flex-start; gap: 7px; padding: 8px 0; border-top: var(--ui-line); }
.check-icon { flex: none; margin-top: 2px; }
.check-copy { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.check-copy strong { font-weight: 600; }
.check-copy p { white-space: pre-wrap; color: var(--ui-secondary); font-size: 12px; }
.check-state { flex: none; font-size: 11px; }
.project-checks li[data-state='passed'] :is(.check-icon, .check-state) { color: var(--ui-success); }
.project-checks li[data-state='failed'] :is(.check-icon, .check-state) { color: var(--ui-danger); }
.project-checks li[data-state='skipped'] :is(.check-icon, .check-state) { color: var(--ui-secondary); }
@media (prefers-reduced-motion: reduce) { .project-spinner { animation: none; } }
</style>
