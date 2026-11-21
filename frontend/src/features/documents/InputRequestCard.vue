<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { Check, ClipboardList } from '@lucide/vue'
import type { InputRequest } from '@/api/documents'
import UiButton from '@/components/ui/UiButton.vue'
import { collectRunInput, visibleInputFields } from './inputRequest'

const props = defineProps<{
  request: InputRequest
  state: 'pending' | 'submitted' | 'closed'
  busy?: boolean
  error?: string
}>()
const emit = defineEmits<{ submit: [values: Record<string, unknown>]; cancel: [] }>()
const fields = computed(() => visibleInputFields(props.request))
const raw = reactive<Record<string, any>>(Object.create(null))
const clientError = ref('')
for (const field of fields.value) {
  const { schema, key } = field
  if (schema.type === 'array') {
    raw[key] = Array.isArray(schema.default)
      ? schema.default.map(value => schema.items?.enum?.indexOf(value) ?? -1).filter(value => value >= 0)
      : []
  } else if (schema.enum && schema.default !== undefined) {
    const index = schema.enum.indexOf(schema.default as string | number | boolean)
    raw[key] = index >= 0 ? String(index) : ''
  } else if (schema.default !== undefined) {
    raw[key] = schema.type === 'object' ? JSON.stringify(schema.default, null, 2) : String(schema.default)
  }
}

function submit() {
  if (props.busy || props.state !== 'pending') return
  try {
    clientError.value = ''
    emit('submit', collectRunInput(props.request, raw))
  } catch (cause) {
    clientError.value = (cause as Error).message
  }
}
</script>

<template>
  <section class="input-request-card" :data-state="state" aria-label="文档资料补充">
    <header>
      <ClipboardList v-if="state === 'pending'" :size="17" />
      <Check v-else :size="17" />
      <strong>{{ state === 'pending' ? '需要补充资料' : state === 'submitted' ? '资料已提交' : '补充请求已结束' }}</strong>
    </header>
    <p class="request-text">{{ request.prompt }}</p>
    <form v-if="state === 'pending'" class="input-form" @submit.prevent="submit">
      <template v-for="{ key, schema, required } in fields" :key="key">
        <fieldset v-if="schema.type === 'array'">
          <legend>{{ schema.title || key }}{{ required ? '（必填）' : '' }}</legend>
          <p v-if="schema.description" class="secondary">{{ schema.description }}</p>
          <label v-for="(option, index) in schema.items?.enum || []" :key="index" class="choice">
            <input v-model="raw[key]" type="checkbox" :value="index" />{{ schema.optionLabels?.[String(option)] || option }}
          </label>
        </fieldset>
        <label v-else class="field">
          <span>{{ schema.title || key }} <small v-if="required">必填</small></span>
          <span v-if="schema.description" class="secondary">{{ schema.description }}</span>
          <select v-if="schema.enum" v-model="raw[key]" :required="required">
            <option value="">请选择</option>
            <option v-for="(option, index) in schema.enum" :key="index" :value="String(index)">{{ option }}</option>
          </select>
          <select v-else-if="schema.type === 'boolean'" v-model="raw[key]" :required="required">
            <option value="">请选择</option><option value="true">是</option><option value="false">否</option>
          </select>
          <textarea v-else-if="schema.type === 'object' || schema.format === 'textarea' || ['notes', 'remarks', 'comment'].includes(key)"
            v-model="raw[key]" :required="required" :maxlength="schema.maxLength" rows="3" />
          <input v-else-if="schema.type === 'number' || schema.type === 'integer'" v-model="raw[key]"
            type="number" :step="schema.type === 'integer' ? 1 : 'any'" :required="required" :min="schema.minimum" :max="schema.maximum" />
          <input v-else v-model="raw[key]" :type="schema.format === 'date' ? 'date' : 'text'"
            :required="required" :minlength="schema.minLength" :maxlength="schema.maxLength" />
        </label>
      </template>
      <p v-if="clientError || error" class="request-error" role="alert">{{ clientError || error }}</p>
      <div class="request-actions">
        <UiButton variant="ghost" size="sm" :disabled="busy" @click="emit('cancel')">取消任务</UiButton>
        <UiButton type="submit" variant="primary" size="sm" :loading="busy">提交并继续</UiButton>
      </div>
    </form>
  </section>
</template>

<style scoped>
.input-request-card {
  border: 1px solid var(--ui-border); border-inline-start: 3px solid var(--ui-warning);
  background: var(--ui-bg); border-radius: var(--ui-radius-md); padding: 12px;
}
.input-request-card[data-state=submitted] { border-inline-start-color: var(--ui-success); }
.input-request-card[data-state=closed] { border-inline-start-color: var(--ui-muted); }
header, .request-actions { display: flex; align-items: center; gap: 8px; }
header { color: var(--ui-text); font-size: 14px; }
header svg { color: var(--ui-muted); }
.request-text { color: var(--ui-secondary); font-size: 13px; white-space: pre-wrap; margin: 6px 0 0; }
.input-form { display: grid; gap: 10px; margin-top: 12px; }
.field { display: grid; gap: 4px; font-size: 13px; color: var(--ui-text); }
.field small, .secondary { color: var(--ui-muted); font-size: 12px; }
.field input, .field textarea, .field select {
  box-sizing: border-box; width: 100%; padding: 7px 9px; border-radius: var(--ui-radius-sm);
  border: 1px solid var(--ui-border-strong); background: var(--ui-surface); color: var(--ui-text); font: inherit;
}
.input-form fieldset { border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); padding: 8px; font-size: 13px; }
.choice { display: inline-flex; gap: 5px; align-items: center; margin-inline-end: 12px; }
.request-actions { justify-content: flex-end; }
.request-error { margin: 0; color: var(--ui-danger); font-size: 13px; }
</style>
