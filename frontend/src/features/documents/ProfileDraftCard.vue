<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { BookmarkCheck, FilePenLine, ListChecks } from '@lucide/vue'
import type { DocumentProfile, InputRequest, ProfileDefinition } from '@/api/documents'
import UiButton from '@/components/ui/UiButton.vue'
import ProfileForm from './ProfileForm.vue'

const props = defineProps<{
  definition: ProfileDefinition
  uncertainties: string[]
  warnings: string[]
  input?: InputRequest
  saved?: { id: DocumentProfile['id']; version?: number }
  busy?: boolean
  error?: string
}>()
const emit = defineEmits<{ confirm: [definition: ProfileDefinition]; cancel: [] }>()
const editorOpen = ref(false)
const adjusted = ref<ProfileDefinition>()
const visibleDefinition = computed(() => adjusted.value ?? props.definition)
const notices = computed(() => [...new Set([...(visibleDefinition.value.uncertainties ?? []), ...props.uncertainties, ...props.warnings])])
watch(() => props.input, (input) => { if (!input) editorOpen.value = false })

function confirm(definition: ProfileDefinition) {
  adjusted.value = definition
  emit('confirm', definition)
}
</script>

<template>
  <section class="profile-draft" aria-label="文档规范草稿">
    <header>
      <span class="profile-icon"><ListChecks :size="19" /></span>
      <div class="header-copy"><strong>{{ visibleDefinition.name }}</strong><span>{{ saved ? '规范已保存' : '文档规范草稿' }}</span></div>
      <RouterLink v-if="saved" :to="{ name: 'Documents', query: { tab: 'profiles', profile: String(saved.id), version: String(saved.version || '') } }">
        查看规范 <template v-if="saved.version">v{{ saved.version }}</template>
      </RouterLink>
    </header>
    <p v-if="visibleDefinition.description" class="secondary">{{ visibleDefinition.description }}</p>
    <ol class="draft-outline">
      <li v-for="section in visibleDefinition.sections" :key="section.id">
        <span>{{ section.title }}</span>
        <small v-if="section.required">必需</small>
      </li>
    </ol>
    <details v-if="visibleDefinition.global_rules.length" class="draft-details">
      <summary>写作规则</summary>
      <ul><li v-for="rule in visibleDefinition.global_rules" :key="rule">{{ rule }}</li></ul>
    </details>
    <details v-if="notices.length" class="draft-details notice" open>
      <summary>待核对的规则</summary>
      <ul><li v-for="notice in notices" :key="notice">{{ notice }}</li></ul>
    </details>
    <div v-if="input" class="review">
      <p class="secondary">{{ input.prompt }}</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <div class="review-actions">
        <UiButton size="sm" variant="primary" :loading="busy" @click="confirm(visibleDefinition)"><BookmarkCheck />确认保存</UiButton>
        <UiButton size="sm" :disabled="busy" @click="editorOpen = true"><FilePenLine />编辑规范</UiButton>
        <UiButton size="sm" variant="ghost" :disabled="busy" @click="emit('cancel')">取消任务</UiButton>
      </div>
    </div>
    <el-dialog v-if="editorOpen" v-model="editorOpen" title="编辑文档规范草稿" width="min(680px, calc(100vw - 24px))"
      append-to-body destroy-on-close :close-on-click-modal="!busy" :close-on-press-escape="!busy" :show-close="!busy">
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <ProfileForm :initial="visibleDefinition" submit-label="确认并保存" :busy="busy" @submit="confirm" @cancel="editorOpen = false" />
    </el-dialog>
  </section>
</template>

<style scoped>
.profile-draft { padding: 13px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-bg); }
header { display: flex; align-items: center; gap: 9px; }
.profile-icon {
  display: grid; place-items: center; flex: none; width: 35px; height: 35px; border-radius: var(--ui-radius-sm);
  color: var(--ui-accent); background: var(--ui-accent-soft);
}
.header-copy { display: grid; gap: 2px; min-width: 0; flex: 1; }
.header-copy strong { color: var(--ui-text); font-size: 14px; overflow-wrap: anywhere; }
.header-copy span, .secondary { color: var(--ui-muted); font-size: 12px; }
header a { color: var(--ui-accent); font-size: 12px; text-decoration: none; }
header a:hover { text-decoration: underline; }
.draft-outline { margin: 10px 0; padding-inline-start: 22px; color: var(--ui-secondary); font-size: 13px; list-style: decimal; }
.draft-outline small {
  display: inline-block; margin-inline-start: 6px; padding: 0 5px; color: var(--ui-accent);
  background: var(--ui-accent-soft); border-radius: var(--ui-radius-sm); font-size: 11px;
}
.draft-details { font-size: 13px; color: var(--ui-secondary); }
.draft-details > summary { cursor: pointer; }
.draft-details > ul { margin: 4px 0; padding-inline-start: 22px; list-style: disc; }
.notice { margin-top: 6px; color: var(--ui-warning); }
.review { margin-top: 10px; border-top: 1px solid var(--ui-border); padding-top: 8px; }
.review-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; }
.error { margin: 4px 0; font-size: 13px; color: var(--ui-danger); }
</style>
