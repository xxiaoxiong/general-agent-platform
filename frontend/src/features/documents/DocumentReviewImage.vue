<script setup lang="ts">
import { ref, watch } from 'vue'
import { getDocumentReviewImage } from '@/api/documents'

const props = defineProps<{ path: string; alt: string }>()
const objectUrl = ref('')
const failed = ref(false)

watch(() => props.path, async (path, _, onCleanup) => {
  const controller = new AbortController()
  let url = ''
  objectUrl.value = ''
  failed.value = false
  onCleanup(() => {
    controller.abort()
    if (url) URL.revokeObjectURL(url)
  })
  try {
    const image = await getDocumentReviewImage(path, controller.signal)
    if (!controller.signal.aborted) {
      url = URL.createObjectURL(image)
      objectUrl.value = url
    }
  } catch {
    if (!controller.signal.aborted) failed.value = true
  }
}, { immediate: true })
</script>

<template>
  <img v-if="objectUrl && !failed" :src="objectUrl" :alt="alt" @error="failed = true" />
  <div v-else class="image-status">{{ failed ? '图片预览不可用' : '正在加载图片…' }}</div>
</template>

<style scoped>
img {
  display: block;
  width: auto;
  max-width: 100%;
  max-height: 270px;
  object-fit: contain;
  background: var(--ui-surface);
  border: 1px solid var(--ui-border);
  border-radius: var(--ui-radius-sm);
}
.image-status { font-size: 12px; color: var(--ui-secondary); }
</style>
