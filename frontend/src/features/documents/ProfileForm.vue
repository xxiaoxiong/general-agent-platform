<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowDown, ArrowUp, Plus, Trash2 } from '@lucide/vue'
import type { ProfileDefinition } from '@/api/documents'
import UiButton from '@/components/ui/UiButton.vue'
import { defaultFormatting, nextProfileInput, nextProfileSection, normalizeProfileSections } from './profileDefinition'

const props = defineProps<{ initial: ProfileDefinition; busy?: boolean; submitLabel?: string }>()
const emit = defineEmits<{ submit: [definition: ProfileDefinition]; cancel: [] }>()
const form = ref<ProfileDefinition>(JSON.parse(JSON.stringify(props.initial)))
form.value.sections.sort((a, b) => a.order - b.order)
const formatDefaults = defaultFormatting()
form.value.formatting = {
  ...formatDefaults, ...form.value.formatting,
  margins_cm: { ...formatDefaults.margins_cm, ...form.value.formatting?.margins_cm },
}
const marginFields = [{ key: 'top', label: '上' }, { key: 'right', label: '右' }, { key: 'bottom', label: '下' }, { key: 'left', label: '左' }] as const
const error = ref('')
const globalRules = computed({
  get: () => form.value.global_rules.join('\n'),
  set: (value: string) => { form.value.global_rules = lines(value) },
})

function lines(text: string) {
  return text.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
}

function moveSection(index: number, direction: number) {
  const next = index + direction
  const sections = form.value.sections
  if (next < 0 || next >= sections.length) return
  const moved = sections.splice(index, 1)[0]
  sections.splice(next, 0, moved)
}

function addRule() {
  const rules = form.value.quality_rules
  let index = rules.length + 1
  while (rules.some(rule => rule.id === `quality_${index}`)) index++
  rules.push({ id: `quality_${index}`, severity: 'warning', description: '' })
}

function revealInvalid(event: Event) {
  const details = (event.target as HTMLElement).closest('details')
  if (details) details.open = true
}

function submit() {
  error.value = ''
  const definition = JSON.parse(JSON.stringify(form.value)) as ProfileDefinition
  definition.name = definition.name.trim()
  definition.document_type = definition.document_type.trim()
  definition.sections = normalizeProfileSections(definition.sections)
  if (!definition.name || !definition.document_type.trim() || !definition.sections.length
      || definition.sections.some(section => !section.title)) {
    error.value = '请填写规范名称、文档类型和所有章节标题。'
    return
  }
  if (definition.quality_rules.some(rule => !rule.description.trim() || !rule.id.trim())) {
    error.value = '请填写每条质量规则的标识和说明。'
    return
  }
  if (definition.sections.some((item, index) => (item.level ?? 1) > (index ? definition.sections[index - 1].level ?? 1 : 0) + 1)) {
    error.value = '首节使用一级标题，后续章节的标题层级不能跳级。'
    return
  }
  for (const field of definition.required_inputs) {
    field.id = field.id.trim()
    field.label = field.label.trim()
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/.test(field.id) || !field.label) {
      error.value = '补充字段需要有效的标识和显示名称。'
      return
    }
    if (!['single_choice', 'multiple_choice'].includes(field.type)) field.options = []
    else if (!field.options.length || new Set(field.options).size !== field.options.length) {
      error.value = '单选和多选字段需要填写且不能重复的选项。'
      return
    }
  }
  if (new Set(definition.required_inputs.map(field => field.id)).size !== definition.required_inputs.length) {
    error.value = '补充字段标识不能重复。'
    return
  }
  emit('submit', definition)
}
</script>

<template>
  <form class="profile-form" @submit.prevent="submit" @invalid.capture="revealInvalid">
    <div class="profile-fields">
      <label>规范名称 <input v-model="form.name" maxlength="180" required /></label>
      <label>文档类型 <input v-model.trim="form.document_type" maxlength="32" pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*" required /></label>
      <label>目标读者 <input v-model="form.audience" maxlength="200" /></label>
      <label>语言 <input v-model="form.language" maxlength="20" /></label>
      <label class="wide">写作语气 <input v-model="form.tone" maxlength="200" /></label>
      <label class="wide">规范说明 <textarea v-model="form.description" rows="2" /></label>
    </div>

    <section class="profile-sections">
      <div class="section-row">
        <h3>章节结构</h3>
        <UiButton size="sm" @click="form.sections.push(nextProfileSection(form.sections))"><Plus />添加章节</UiButton>
      </div>
      <fieldset v-for="(section, index) in form.sections" :key="section.id">
        <legend>第 {{ index + 1 }} 节</legend>
        <div class="section-row">
          <label class="section-title">章节标题 <input v-model="section.title" maxlength="180" required /></label>
          <div class="section-buttons" :aria-label="`第 ${index + 1} 节的操作`">
            <UiButton variant="ghost" size="icon" aria-label="上移章节" title="上移章节" :disabled="index === 0" @click="moveSection(index, -1)"><ArrowUp /></UiButton>
            <UiButton variant="ghost" size="icon" aria-label="下移章节" title="下移章节" :disabled="index === form.sections.length - 1" @click="moveSection(index, 1)"><ArrowDown /></UiButton>
            <UiButton variant="ghost" size="icon" aria-label="删除章节" title="删除章节" :disabled="form.sections.length === 1" @click="form.sections.splice(index, 1)"><Trash2 /></UiButton>
          </div>
        </div>
        <label class="checkbox"><input v-model="section.required" type="checkbox" />必需章节</label>
        <div class="profile-fields">
          <label>标题级别 <select v-model.number="section.level"><option :value="1">一级</option><option :value="2">二级</option><option :value="3">三级</option></select></label>
          <label>章节标识 <input :value="section.id" readonly /></label>
          <label>编写目的 <textarea v-model="section.purpose" rows="2" /></label>
          <label>写作要求 <textarea v-model="section.writing_guidance" rows="2" /></label>
          <label>必需字段（每行一项）
            <textarea :value="section.required_fields.join('\n')" rows="2" @change="section.required_fields = lines(($event.target as HTMLTextAreaElement).value)" />
          </label>
          <label>来源要求（每行一项）
            <textarea :value="section.source_requirements.join('\n')" rows="2" @change="section.source_requirements = lines(($event.target as HTMLTextAreaElement).value)" />
          </label>
        </div>
      </fieldset>
    </section>

    <label>全局写作规则（每行一项）<textarea v-model.lazy="globalRules" rows="3" /></label>
    <section class="profile-sections">
      <div class="section-row">
        <h3>质量检查</h3>
        <UiButton size="sm" @click="addRule"><Plus />添加规则</UiButton>
      </div>
      <fieldset v-for="(rule, index) in form.quality_rules" :key="index" class="rule">
        <legend>规则 {{ index + 1 }}</legend>
        <div class="profile-fields">
          <label>规则标识 <input v-model="rule.id" required /></label>
          <label>严重程度
            <select v-model="rule.severity"><option value="info">信息</option><option value="warning">提醒</option><option value="error">错误</option></select>
          </label>
          <label class="wide">检查说明 <textarea v-model="rule.description" required rows="2" /></label>
        </div>
        <UiButton variant="ghost" size="sm" @click="form.quality_rules.splice(index, 1)">删除规则</UiButton>
      </fieldset>
    </section>
    <details class="advanced">
      <summary>补充信息字段<template v-if="form.required_inputs.length">（{{ form.required_inputs.length }}）</template></summary>
      <div class="settings-body">
        <fieldset v-for="(field, index) in form.required_inputs" :key="index">
          <legend>字段 {{ index + 1 }}</legend>
          <div class="profile-fields">
            <label>显示名称 <input v-model.trim="field.label" maxlength="180" required /></label>
            <label>字段标识 <input v-model.trim="field.id" maxlength="32" pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*" required /></label>
            <label>字段类型
              <select v-model="field.type">
                <option value="text">文本</option><option value="date">日期</option><option value="number">数字</option>
                <option value="single_choice">单选</option><option value="multiple_choice">多选</option>
              </select>
            </label>
            <label class="checkbox"><input v-model="field.required" type="checkbox" />必填</label>
            <label class="wide">填写提示 <textarea v-model="field.description" maxlength="500" rows="2" /></label>
            <label v-if="field.type === 'single_choice' || field.type === 'multiple_choice'" class="wide">选项（每行一项）
              <textarea :value="field.options.join('\n')" rows="3" @change="field.options = lines(($event.target as HTMLTextAreaElement).value)" />
            </label>
          </div>
          <UiButton class="remove-field" variant="ghost" size="sm" @click="form.required_inputs.splice(index, 1)">删除字段</UiButton>
        </fieldset>
        <UiButton class="add-field" size="sm" @click="form.required_inputs.push(nextProfileInput(form.required_inputs))"><Plus />添加字段</UiButton>
      </div>
    </details>
    <details class="advanced">
      <summary>Word 排版设置</summary>
      <div class="profile-fields settings-body">
        <label>纸张规格 <select v-model="form.formatting.page_size"><option>A4</option><option>Letter</option></select></label>
        <label>正文字体 <input v-model="form.formatting.font_family" maxlength="80" /></label>
        <label>字号（磅） <input v-model.number="form.formatting.body_font_size" type="number" min="9" max="16" /></label>
        <label>行距 <input v-model.number="form.formatting.line_spacing" type="number" min="1" max="2.2" step=".01" /></label>
        <label v-for="margin in marginFields" :key="margin.key">{{ margin.label }}页边距（厘米）
          <input v-model.number="form.formatting.margins_cm[margin.key]" type="number" min=".75" max="5" step=".01" />
        </label>
        <label>页眉 <input v-model="form.formatting.header" maxlength="500" /></label>
        <label>页脚 <input v-model="form.formatting.footer" maxlength="500" /></label>
        <label class="checkbox"><input v-model="form.formatting.heading_hierarchy" type="checkbox" />校验标题层级</label>
        <label class="checkbox"><input v-model="form.formatting.tables_allowed" type="checkbox" />允许表格</label>
        <label class="checkbox"><input v-model="form.formatting.page_numbers" type="checkbox" />显示页码</label>
        <label class="checkbox"><input v-model="form.formatting.section_page_break" type="checkbox" />一级章节另起一页</label>
      </div>
    </details>
    <p v-if="error" class="profile-error" role="alert">{{ error }}</p>
    <div class="actions">
      <UiButton :disabled="busy" @click="emit('cancel')">取消</UiButton>
      <UiButton type="submit" variant="primary" :loading="busy">{{ submitLabel || '保存草稿' }}</UiButton>
    </div>
  </form>
</template>

<style scoped>
.profile-form, .profile-sections { display: grid; gap: 14px; }
.profile-form label { min-width: 0; display: grid; gap: 4px; font-size: 13px; color: var(--ui-secondary); }
.profile-fields { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.wide { grid-column: 1 / -1; }
.profile-form input:not([type=checkbox]), .profile-form textarea, .profile-form select {
  width: 100%; min-width: 0; padding: 7px 9px; border-radius: var(--ui-radius-sm);
  border: 1px solid var(--ui-border-strong); background: var(--ui-surface); color: var(--ui-text); font: inherit;
}
.profile-form textarea { resize: vertical; }
.profile-form fieldset { min-width: 0; padding: 8px 12px 12px; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); }
.profile-form legend { padding: 0 5px; color: var(--ui-muted); font-size: 12px; }
.section-row, .section-buttons, .actions { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
.section-row h3 { margin: 0; font-size: 14px; font-weight: 600; }
.section-title { flex: 1; }
.profile-form .checkbox { display: flex; align-items: center; gap: 6px; margin: 8px 0; }
.rule > button { margin-top: 6px; }
.advanced { border-top: 1px solid var(--ui-border); padding-top: 9px; }
.advanced summary { cursor: pointer; font-size: 13px; color: var(--ui-secondary); }
.settings-body { display: grid; gap: 10px; margin-top: 9px; }
.add-field { justify-self: start; }
.remove-field { margin-top: 5px; }
.actions { justify-content: flex-end; }
.profile-error { margin: 0; font-size: 13px; color: var(--ui-danger); }
@media (max-width: 510px) { .profile-fields { grid-template-columns: minmax(0, 1fr); } }
</style>
