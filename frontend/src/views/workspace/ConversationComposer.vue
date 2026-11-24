<template>
  <div class="composer">
    <div v-if="uploading" class="composer-feedback" role="status">
      <LoaderCircle class="spin" :size="14" />
      正在上传 {{ uploading }} 个附件…
    </div>

    <div v-if="dictationPhase !== 'idle'" class="dictation-feedback">
      <div class="composer-feedback" :class="dictationPhase" :role="dictationPhase === 'error' ? 'alert' : 'status'">
        <template v-if="dictationPhase === 'recording'">
          <span class="recording-dot" aria-hidden="true"></span>
          正在录音 <span class="recording-timer" aria-hidden="true">{{ recordingTime }}</span>
        </template>
        <template v-else-if="dictationPhase === 'done'">
          <CircleCheck :size="14" aria-hidden="true" />
          文字已填入输入框，可编辑后发送。
        </template>
        <template v-else-if="dictationPhase === 'error'">
          <CircleAlert :size="14" aria-hidden="true" />
          {{ dictationError }}
        </template>
        <template v-else>
          <LoaderCircle class="spin" :size="14" aria-hidden="true" />
          {{ dictationPhase === 'requesting' ? '正在准备录音…' : dictationPhase === 'finalizing' ? '正在结束录音…' : '正在识别语音…' }}
        </template>
      </div>
      <button v-if="dictationBusy" type="button" class="dictation-cancel" aria-label="取消语音输入" @click="cancelFromButton">取消</button>
    </div>

    <form class="composer-box" @submit.prevent="submit">
      <slot name="document-context" />
      <ul v-if="attachments.length" class="attachment-list" aria-label="待发送附件">
        <li v-for="(file, index) in attachments" :key="file.path" class="attachment">
          <button type="button" class="attachment-preview" :title="file.name" @click="$emit('preview', file.path, file.name)">
            <img v-if="file.previewUrl" :src="file.previewUrl" alt="" />
            <FileText v-else :size="16" />
            <span>{{ file.name }}</span>
          </button>
          <button type="button" class="button-icon attachment-remove" :aria-label="'移除附件 ' + file.name" title="移除附件" @click="$emit('remove', index)">
            <X :size="14" />
          </button>
        </li>
      </ul>

      <textarea
        ref="textarea"
        v-model="text"
        rows="2"
        :disabled="locked"
        aria-label="消息"
        placeholder="给智能体发送消息…"
        @keydown="onKeydown"
        @paste="onPaste"
      />

      <div class="composer-bar">
        <div class="composer-options">
          <input ref="picker" hidden type="file" multiple @change="filesSelected" />
          <button type="button" class="button-icon" :disabled="locked" aria-label="上传附件" title="上传附件，也可粘贴文件或图片" @click="picker?.click()">
            <Paperclip :size="18" />
          </button>
          <slot name="document-task" />
          <button
            type="button" class="selector team-switch" role="switch" aria-label="智能体团队"
            :aria-checked="teamMode" :disabled="locked || sending || stoppable || Boolean(blockedReason) || uploading > 0"
            :title="teamMode ? '关闭当前会话的团队协作' : '开启当前会话的团队协作，会增加模型调用'"
            @click="teamMode = !teamMode"
          >
            <UsersRound :size="16" aria-hidden="true" /><span>团队</span>
            <span class="team-indicator" aria-hidden="true"></span>
          </button>
          <button
            type="button" class="selector team-switch" role="switch" aria-label="联网检索"
            :aria-checked="webEnabled" :disabled="locked || sending"
            :title="webEnabled ? '关闭后本次对话不再联网检索' : '开启后允许智能体联网搜索与抓取网页'"
            @click="webEnabled = !webEnabled"
          >
            <Globe :size="16" aria-hidden="true" /><span>联网</span>
            <span class="team-indicator" aria-hidden="true"></span>
          </button>
          <DropdownMenuRoot>
            <DropdownMenuTrigger as-child>
              <button type="button" class="selector" aria-label="审批模式">
                <ShieldCheck :size="15" /><span>{{ approvalMode === 'manual' ? '手动审批' : '自动审批' }}</span><ChevronDown :size="13" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuPortal>
              <DropdownMenuContent class="composer-menu" align="start" :side-offset="6" :collision-padding="12">
                <DropdownMenuRadioGroup v-model="approvalMode">
                  <DropdownMenuRadioItem class="composer-option" value="manual" text-value="手动审批">
                    <span class="composer-indicator"><DropdownMenuItemIndicator><Check :size="14" /></DropdownMenuItemIndicator></span>
                    <span>手动审批<small>高风险工具执行前需确认</small></span>
                  </DropdownMenuRadioItem>
                  <DropdownMenuRadioItem class="composer-option" value="auto" text-value="自动审批">
                    <span class="composer-indicator"><DropdownMenuItemIndicator><Check :size="14" /></DropdownMenuItemIndicator></span>
                    <span>自动审批<small>高风险工具自动获得批准</small></span>
                  </DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenuPortal>
          </DropdownMenuRoot>
        </div>

        <div class="composer-options model-options">
          <DropdownMenuRoot>
            <DropdownMenuTrigger as-child>
              <button type="button" class="selector model-selector" aria-label="选择模型" :title="modelLabel">
                <Cpu :size="15" /><span>{{ modelLabel }}</span><ChevronDown :size="13" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuPortal>
              <DropdownMenuContent class="composer-menu" align="end" :side-offset="6" :collision-padding="12">
                <DropdownMenuRadioGroup v-model="modelId">
                  <DropdownMenuRadioItem v-for="option in modelOptions" :key="option.value" class="composer-option" :value="option.value">
                    <span class="composer-indicator"><DropdownMenuItemIndicator><Check :size="14" /></DropdownMenuItemIndicator></span>
                    {{ option.label }}
                  </DropdownMenuRadioItem>
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenuPortal>
          </DropdownMenuRoot>
          <button
            ref="micButton" type="button" class="button-icon dictation-button"
            :class="{ 'dictation-recording': dictationPhase === 'recording' }"
            :disabled="dictationPhase !== 'recording' && (dictationBusy || locked || sending)"
            :aria-label="dictationButtonLabel" :title="dictationButtonLabel" @click="toggleDictation"
          >
            <Square v-if="dictationPhase === 'recording'" :size="14" fill="currentColor" aria-hidden="true" />
            <Mic v-else :size="18" aria-hidden="true" />
          </button>
          <button v-if="stoppable" type="button" class="button-send button-stop" aria-label="停止生成" title="停止生成" @click="$emit('stop')">
            <Square :size="14" fill="currentColor" />
          </button>
          <button v-else type="submit" class="button-send" :disabled="!canSend" aria-label="发送消息" title="发送消息">
            <LoaderCircle v-if="sending" class="spin" :size="16" />
            <ArrowUp v-else :size="18" />
          </button>
        </div>
      </div>
    </form>
    <div class="composer-meta">
      <span v-if="blockedReason" class="blocked">{{ blockedReason }}</span>
      <span v-else-if="teamMode" class="team-enabled">团队模式已开启 · 多角色协作会增加模型调用</span>
      <span v-else><kbd>Enter</kbd> 发送 <span class="shortcut-gap">·</span> <kbd>Shift</kbd> + <kbd>Enter</kbd> 换行</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowUp, Check, ChevronDown, CircleAlert, CircleCheck, Cpu, FileText, Globe, LoaderCircle, Mic, Paperclip, ShieldCheck, Square, UsersRound, X } from '@lucide/vue'
import {
  DropdownMenuContent, DropdownMenuItemIndicator, DropdownMenuPortal, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuRoot, DropdownMenuTrigger,
} from 'reka-ui'
import { clipboardFiles, isSendKey } from './interactions'
import { asrApi } from '@/api/asr'
import { appendTranscript, useDictation } from './dictation'

const props = defineProps<{
  attachments: Array<{ name: string; path: string; previewUrl?: string }>
  options: Array<{ id: number; name: string; display_name?: string | null }>
  uploading: number
  sending: boolean
  stoppable: boolean
  blockedReason?: string
  locked: boolean
}>()

const emit = defineEmits<{
  send: []
  stop: []
  upload: [files: File[]]
  remove: [index: number]
  preview: [path: string, name: string]
}>()

const text = defineModel<string>('text', { required: true })
const teamMode = defineModel<boolean>('teamMode', { required: true })
const approvalMode = defineModel<'manual' | 'auto'>('approvalMode', { required: true })
const webEnabled = defineModel<boolean>('webEnabled', { required: true })
const selectedModel = defineModel<number>('model', { required: true })
const modelId = computed({
  get: () => String(selectedModel.value),
  set: (value: string) => { selectedModel.value = Number(value) },
})
const modelOptions = computed(() => [
  { value: '0', label: '默认模型' },
  ...props.options.map((option) => ({ value: String(option.id), label: option.display_name || option.name })),
])
const modelLabel = computed(() => modelOptions.value.find((option) => option.value === modelId.value)?.label || '默认模型')
const canSend = computed(() => Boolean(text.value.trim()) && !props.locked && !props.sending && !props.stoppable && !props.uploading && !props.blockedReason)

const textarea = ref<HTMLTextAreaElement>()
const picker = ref<HTMLInputElement>()
const micButton = ref<HTMLButtonElement>()
const {
  phase: dictationPhase, error: dictationError, elapsed, busy: dictationBusy,
  start: startDictation, stop: stopDictation, cancel: cancelDictation,
} = useDictation({
  capabilities: asrApi.capabilities,
  transcribe: asrApi.transcribe,
  insert: (transcript) => {
    text.value = appendTranscript(text.value, transcript)
    const focused = document.activeElement
    if (focused === micButton.value || focused === document.body) {
      void nextTick(() => {
        textarea.value?.focus()
        textarea.value?.setSelectionRange(text.value.length, text.value.length)
      })
    }
  },
})

const recordingTime = computed(() => [
  Math.floor(elapsed.value / 60), elapsed.value % 60,
].map((part) => String(part).padStart(2, '0')).join(':'))

const dictationButtonLabel = computed(() => {
  if (dictationPhase.value === 'recording') return '停止录音'
  if (dictationBusy.value) return '语音输入处理中'
  return '开始录音'
})

onMounted(() => window.addEventListener('pagehide', cancelDictation))
onBeforeUnmount(() => {
  window.removeEventListener('pagehide', cancelDictation)
  cancelDictation()
})

function toggleDictation() {
  if (dictationPhase.value === 'recording') stopDictation()
  else if (!dictationBusy.value && !props.locked && !props.sending) void startDictation()
}

function cancelFromButton() {
  cancelDictation()
  void nextTick(() => micButton.value?.focus())
}

function resizeTextarea() {
  if (!textarea.value) return
  textarea.value.style.height = 'auto'
  textarea.value.style.height = textarea.value.scrollHeight + 'px'
}

watch(text, resizeTextarea, { flush: 'post' })

function onKeydown(event: KeyboardEvent) {
  if (!isSendKey(event)) return
  event.preventDefault()
  submit()
}

function submit() {
  if (!canSend.value) return
  cancelDictation()
  emit('send')
}

function filesSelected() {
  if (!picker.value) return
  const files = Array.from(picker.value.files || [])
  picker.value.value = ''
  if (files.length) emit('upload', files)
}

function onPaste(event: ClipboardEvent) {
  if (props.locked || event.defaultPrevented) return
  const files = clipboardFiles(event.clipboardData)
  if (!files.length) return
  event.preventDefault()
  emit('upload', files)
}

defineExpose({ focus: () => textarea.value?.focus(), cancelDictation })
</script>

<style scoped>
.composer { display: grid; gap: 8px; }
.composer-box {
  padding: 8px; min-width: 0; background: var(--ui-surface); border: 1px solid var(--ui-border-strong);
  border-radius: var(--ui-radius-lg); box-shadow: var(--ui-shadow-sm);
  transition: border-color var(--ui-duration) var(--ui-ease);
}
.composer-box:focus-within { border-color: var(--ui-accent); }
.composer textarea {
  display: block; width: 100%; min-height: 58px; max-height: min(224px, 30vh); padding: 7px 8px; resize: none;
  background: transparent; border: 0; outline: none; color: var(--ui-text); line-height: 1.55; font-size: 15px;
}
.composer textarea:focus-visible { outline: none; }
.composer textarea::placeholder { color: var(--ui-muted); }
.composer-bar, .composer-options { min-width: 0; display: flex; align-items: center; gap: 4px; }
.composer-bar { justify-content: space-between; flex-wrap: wrap; gap: 4px 8px; }
.model-options { justify-content: end; margin-inline-start: auto; }
.button-icon, .selector, .button-send {
  display: inline-flex; align-items: center; justify-content: center; height: 34px;
  border: 0; color: var(--ui-secondary); background: transparent; border-radius: var(--ui-radius-sm); cursor: pointer;
}
.button-icon, .button-send { width: 34px; flex: none; }
.button-icon:hover, .selector:hover, .selector[data-state="open"] { background: var(--ui-hover); color: var(--ui-text); }
.button-icon:disabled { opacity: .5; cursor: not-allowed; }
.selector { min-width: 0; padding: 0 7px; gap: 5px; font-size: 12px; }
.selector svg { flex: none; }
.selector span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.team-switch { flex: none; border: 1px solid transparent; }
.team-switch[aria-checked="true"] { color: var(--ui-accent); background: var(--ui-accent-soft); }
.team-switch:focus-visible { outline: var(--ui-focus); outline-offset: 2px; }
.team-switch:disabled { opacity: .55; cursor: not-allowed; }
.team-indicator { width: 16px; height: 9px; border-radius: 8px; border: 1px solid currentColor; position: relative; }
.team-indicator::before {
  content: ''; position: absolute; inset: 1px auto 1px 1px; width: 5px;
  background: currentColor; border-radius: 50%; transition: transform var(--ui-duration) var(--ui-ease);
}
.team-switch[aria-checked="true"] .team-indicator::before { transform: translateX(7px); }
.model-selector { max-width: 208px; }
.button-send { background: var(--ui-accent); color: var(--ui-on-accent); }
.button-send:hover { background: var(--ui-accent-hover); }
.button-send:disabled { background: var(--ui-subtle); color: var(--ui-muted); cursor: not-allowed; }
.button-stop { background: var(--ui-danger-soft); color: var(--ui-danger); }
.button-stop:hover { background: var(--ui-danger); color: var(--ui-surface); }
.composer-meta { color: var(--ui-muted); min-height: 18px; text-align: center; font-size: 12px; }
.composer-meta kbd { font: inherit; }
.composer-meta .blocked { color: var(--ui-secondary); }
.composer-meta .team-enabled { color: var(--ui-accent); }
.shortcut-gap { margin: 0 3px; }
.composer-feedback { display: flex; align-items: center; gap: 6px; padding-inline: 8px; color: var(--ui-info); font-size: 12px; }
.composer-feedback svg { flex: none; }
.dictation-feedback { display: flex; align-items: center; gap: 4px; }
.dictation-feedback .composer-feedback { min-height: 26px; }
.dictation-feedback .recording, .dictation-feedback .error { color: var(--ui-danger); }
.dictation-feedback .done { color: var(--ui-success); }
.dictation-cancel {
  flex: none; padding: 3px 8px; border: 0; background: transparent; color: var(--ui-secondary);
  border-radius: var(--ui-radius-sm); font-size: 12px; cursor: pointer;
}
.dictation-cancel:hover { background: var(--ui-hover); color: var(--ui-text); }
.recording-dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.recording-timer { font-variant-numeric: tabular-nums; }
.dictation-button.dictation-recording { color: var(--ui-danger); background: var(--ui-danger-soft); }
.dictation-button.dictation-recording:hover { color: var(--ui-surface); background: var(--ui-danger); }
.attachment-list { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; padding: 4px 4px 6px; }
.attachment { min-width: 0; max-width: 100%; display: flex; align-items: center; border: 1px solid var(--ui-border); border-radius: var(--ui-radius-sm); }
.attachment-preview { display: flex; align-items: center; gap: 6px; min-width: 0; padding: 3px 6px; border: 0; background: none; color: var(--ui-text); cursor: pointer; }
.attachment-preview:hover { color: var(--ui-accent); }
.attachment-preview img { width: 28px; height: 28px; object-fit: cover; border-radius: var(--ui-radius-sm); }
.attachment-preview span { max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.attachment-preview svg { flex: none; color: var(--ui-muted); }
.attachment-remove { width: 26px; height: 26px; }
.attachment-remove:hover { color: var(--ui-danger); background: var(--ui-danger-soft); }
.spin { animation: rotate 1s linear infinite; }
@keyframes rotate { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
@media (max-width: 560px) {
  .model-selector { max-width: 130px; }
  .selector .lucide-cpu, .selector .lucide-shield-check { display: none; }
  .composer-meta { font-size: 11px; }
  .composer-meta > span:not(.blocked, .team-enabled) { visibility: hidden; }
}
</style>

<style>
.composer-menu {
  z-index: var(--ui-z-popover);
  max-height: min(280px, var(--reka-dropdown-menu-content-available-height));
  overflow: auto;
  padding: 4px;
  background: var(--ui-surface);
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md);
  box-shadow: var(--ui-shadow-md);
  color: var(--ui-text); min-width: 150px;
}
.composer-option { display: flex; align-items: center; gap: 7px; padding: 7px 8px; border-radius: var(--ui-radius-sm); cursor: pointer; font-size: 13px; outline: none; }
.composer-option[data-highlighted] { background: var(--ui-hover); }
.composer-option small { display: block; color: var(--ui-muted); font-size: 12px; }
.composer-indicator { display: inline-flex; align-items: center; width: 14px; color: var(--ui-accent); }
</style>
