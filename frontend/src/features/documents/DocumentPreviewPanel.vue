<script setup lang="ts">
import { computed } from 'vue'
import type { DocumentArtifact } from '@/api/documents'
import type { PreviewContent } from '@/api/conversations'
import FilePreviewPanel from '@/features/preview/FilePreviewPanel.vue'
import DocumentPdfPreview from './DocumentPdfPreview.vue'

const props = defineProps<{ artifact: DocumentArtifact }>()
defineEmits<{ close: []; download: [] }>()
const metadata = computed<PreviewContent>(() => ({
  kind: 'pdf', name: props.artifact.filename, path: '', size: props.artifact.size,
  mime: props.artifact.mime_type,
}))
</script>

<template>
  <FilePreviewPanel :data="metadata" @close="$emit('close')" @download="$emit('download')">
    <template #document>
      <DocumentPdfPreview :document-id="artifact.document_id" :version="artifact.version_no" :title="artifact.title" />
    </template>
  </FilePreviewPanel>
</template>
