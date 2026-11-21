<script setup lang="ts">
import { ref, watch } from 'vue'
import { CircleAlert, LoaderCircle } from '@lucide/vue'
import { documentsApi } from '@/api/documents'
import { filePreviewError } from '@/features/preview/previewError'

const props = defineProps<{ documentId: number; version: number; title: string }>()
const url = ref('')
const error = ref('')
const loading = ref(true)

watch(() => [props.documentId, props.version] as const, async ([id, version], _, onCleanup) => {
  const controller = new AbortController()
  let objectUrl = ''
  onCleanup(() => {
    controller.abort()
    if (objectUrl) URL.revokeObjectURL(objectUrl)
  })
  url.value = ''
  error.value = ''
  loading.value = true
  try {
    const blob = await documentsApi.previewPdf(id, version, controller.signal)
    if (controller.signal.aborted) return
    objectUrl = URL.createObjectURL(blob)
    url.value = objectUrl
  } catch (cause) {
    const message = await filePreviewError(cause, '无法加载 Word 版式预览，请尝试下载原件')
    if (!controller.signal.aborted) error.value = message
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}, { immediate: true })
</script>

<template>
  <div class="document-pdf-preview">
    <div v-if="loading" class="pdf-state" role="status">
      <LoaderCircle class="mini-spin" :size="17" /><span>正在渲染 Word 版式…</span>
    </div>
    <div v-else-if="error" class="pdf-state pdf-error" role="alert">
      <CircleAlert :size="17" /><span>{{ error }}</span>
    </div>
    <iframe v-else-if="url" :src="url" :title="`${title}（Word 版式）`" />
  </div>
</template>

<style scoped>
.document-pdf-preview { display: flex; flex: 1; width: 100%; height: 100%; min-height: 0; }
.document-pdf-preview iframe { flex: 1; width: 100%; height: 100%; border: 0; background: var(--ui-code-bg); }
.pdf-state { display: flex; align-items: center; align-self: start; gap: 8px; padding: 18px; color: var(--ui-muted); font-size: 13px; }
.pdf-error { color: var(--ui-danger); }
</style>
