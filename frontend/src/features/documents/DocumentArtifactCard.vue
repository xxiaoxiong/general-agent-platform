<script setup lang="ts">
import { Download, Eye, FilePenLine, FileText } from '@lucide/vue'
import type { DocumentArtifact } from '@/api/documents'
import UiButton from '@/components/ui/UiButton.vue'

defineProps<{ artifact: DocumentArtifact; downloading?: boolean }>()
defineEmits<{ preview: []; download: []; edit: [] }>()

function fileSize(bytes: number) {
  if (!Number.isFinite(bytes) || bytes <= 0) return ''
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}
</script>

<template>
  <article class="artifact-card" aria-label="已生成的 Word 文档">
    <div class="artifact-top">
      <div class="artifact-icon"><FileText :size="21" :stroke-width="1.7" /></div>
      <div class="artifact-copy">
        <strong>{{ artifact.title }}</strong>
        <span>{{ artifact.filename }} <template v-if="fileSize(artifact.size)">· {{ fileSize(artifact.size) }}</template></span>
      </div>
      <span class="version-badge">v{{ artifact.version_no }}</span>
    </div>
    <div class="artifact-detail">
      <span>DOCX · 已生成</span>
      <RouterLink v-if="artifact.profile_id" :to="{ name: 'Documents', query: { tab: 'profiles', profile: String(artifact.profile_id) } }">
        {{ artifact.profile_name || `规范 #${artifact.profile_id}` }}<template v-if="artifact.profile_version"> · v{{ artifact.profile_version }}</template>
      </RouterLink>
      <RouterLink :to="{ name: 'Documents', query: { document: String(artifact.document_id), version: String(artifact.version_no) } }">版本历史</RouterLink>
    </div>
    <div class="artifact-actions">
      <UiButton size="sm" @click="$emit('preview')"><Eye />预览</UiButton>
      <UiButton size="sm" variant="ghost" :loading="downloading" @click="$emit('download')"><Download />下载</UiButton>
      <UiButton size="sm" variant="ghost" @click="$emit('edit')"><FilePenLine />继续修改</UiButton>
    </div>
  </article>
</template>

<style scoped>
.artifact-card {
  padding: 13px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-bg);
}
.artifact-top, .artifact-detail, .artifact-actions { display: flex; align-items: center; gap: 9px; }
.artifact-icon {
  flex: none; display: grid; place-items: center; width: 39px; height: 39px; border-radius: var(--ui-radius-sm);
  color: var(--ui-accent); background: var(--ui-accent-soft);
}
.artifact-copy { flex: 1; min-width: 0; display: grid; gap: 2px; }
.artifact-copy strong, .artifact-copy span { overflow-wrap: anywhere; }
.artifact-copy strong { font-size: 14px; color: var(--ui-text); }
.artifact-copy span, .artifact-detail { font-size: 12px; color: var(--ui-muted); }
.version-badge {
  flex: none; padding: 1px 6px; border-radius: var(--ui-radius-sm); background: var(--ui-subtle);
  color: var(--ui-secondary); font-size: 12px;
}
.artifact-detail, .artifact-actions { flex-wrap: wrap; margin-top: 9px; }
.artifact-detail a { color: var(--ui-accent); text-decoration: none; }
.artifact-detail a:hover { text-decoration: underline; }
</style>
