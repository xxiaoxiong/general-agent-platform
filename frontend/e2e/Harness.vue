<template>
  <div class="harness" data-testid="harness">
    <div class="harness-toolbar">
      <button id="toggle-theme" type="button" @click="toggleTheme">切换主题：{{ theme }}</button>
      <button id="finish-streaming" type="button" @click="streaming = false">结束流式</button>
      <button id="stream-fragment" type="button" @click="appendStreamFragment">流式增量</button>
      <button id="toggle-mounted" type="button" @click="mounted = !mounted">切换消息挂载</button>
      <button id="update-message" type="button" @click="updates += 1">更新消息</button>
      <button id="replace-message" type="button" @click="replaced = true">替换消息</button>
      <span data-testid="theme">{{ theme }}</span>
    </div>
    <div class="harness-message">
      <MessageMarkdown v-if="mounted" :text="text" :streaming="streaming" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import MessageMarkdown from '../src/features/run/MessageMarkdown.vue'
import { setTheme, useTheme } from '../src/lib/theme'
import { FIXTURES } from './fixtures'

const params = new URLSearchParams(window.location.search)
const baseText = FIXTURES[params.get('case') || 'mixed'] ?? FIXTURES.mixed
const streaming = ref(params.get('streaming') === '1')
const mounted = ref(true)
const updates = ref(0)
const replaced = ref(false)
const streamed = ref('')
const fragmentIndex = ref(0)
const STREAM_FRAGMENTS = [
  '\n\n```mermaid\n',
  'flowchart TD\n',
  '\n  A[开始] --> B[完成]\n',
  '```\n',
]
const text = computed(() => {
  if (replaced.value) return FIXTURES.multi
  return `${baseText}${streamed.value}${updates.value ? `\n\n更新段落 ${updates.value}` : ''}`
})

function appendStreamFragment() {
  const fragment = STREAM_FRAGMENTS[fragmentIndex.value]
  if (!fragment) return
  streamed.value += fragment
  fragmentIndex.value += 1
}

const { theme, toggleTheme } = useTheme()
setTheme(params.get('theme') === 'dark' ? 'dark' : 'light')
</script>

<style scoped>
.harness { max-width: 980px; margin: 0 auto; padding: 16px; }
.harness-toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.harness-toolbar button { padding: 4px 10px; border: 1px solid var(--ui-border); border-radius: 6px; background: var(--ui-subtle); color: var(--ui-text); cursor: pointer; }
.harness-message { padding: 16px; border: 1px solid var(--ui-border); border-radius: 10px; background: var(--ui-bg); }
</style>
