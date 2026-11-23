<template>
  <aside id="conversation-history" ref="history" class="history" aria-label="会话历史">
    <div class="history-head">
      <div class="history-heading">
        <h2>会话历史</h2>
        <button type="button" class="square-button" aria-label="收起会话历史" title="收起会话历史" @click="$emit('close')">
          <PanelLeftClose :size="16" />
        </button>
      </div>
      <button ref="newChat" type="button" class="new-chat" :disabled="creating" @click="$emit('create')">
        <LoaderCircle v-if="creating" class="spin" :size="16" />
        <SquarePen v-else :size="16" />
        新建对话
      </button>
    </div>
    <div v-if="items.length" class="history-search">
      <Search :size="14" aria-hidden="true" />
      <input ref="search" v-model="query" type="search" aria-label="搜索会话" placeholder="搜索会话" />
    </div>
    <div class="history-list" :aria-busy="loading">
      <div v-if="loading" role="status" class="history-state">
        <LoaderCircle class="spin" :size="16" /> 正在加载会话…
      </div>
      <div v-else-if="failed" class="history-state">
        无法加载会话
        <button type="button" class="retry" @click="$emit('retry')">重试</button>
      </div>
      <template v-else>
        <p v-if="query.trim() && !groups.length" class="history-state">没有匹配的会话</p>
        <section v-for="group in groups" :key="group.id" class="history-group" :class="group.id" :aria-label="group.label">
          <header class="group-heading">
            <component :is="group.icon" :size="13" aria-hidden="true" />
            <h3>{{ group.label }}</h3><span v-if="group.items.length" class="group-count" :aria-label="`${group.items.length} 个已加载会话`">{{ group.items.length }}</span>
          </header>
          <p v-if="!group.items.length" class="group-empty">{{ group.empty }}</p>
          <ul v-else>
            <li v-for="c in group.items" :key="c.id" class="conv-item" :class="{ active: activeId === c.id }">
              <button type="button" class="conv-target" :aria-current="activeId === c.id ? 'true' : undefined"
                :aria-label="c.title || '新对话'" :title="c.title || '新对话'" @click="$emit('select', c)">
                <span class="conv-icon"><MessageSquare :size="15" aria-hidden="true" /></span>
                <span class="conv-copy">
                  <span class="conv-title">{{ c.title || '新对话' }}</span>
                  <span v-if="status(c)" class="conv-status" :class="{ waiting: ['等待审批', '等待补充', '等待审阅'].includes(status(c)) }">
                    {{ status(c) }}
                  </span>
                  <time v-else-if="c.updated_at || c.created_at" class="conv-date" :datetime="c.updated_at || c.created_at || ''">
                    {{ formatDate(c.updated_at || c.created_at) }}
                  </time>
                </span>
              </button>
              <DropdownMenuRoot :modal="false">
                <DropdownMenuTrigger as-child>
                  <button type="button" class="square-button conv-actions" :data-conversation-action="c.id"
                    :aria-label="'会话操作 ' + (c.title || '新对话')" title="会话操作" :disabled="isBusy(c.id)">
                    <LoaderCircle v-if="isBusy(c.id)" class="spin" :size="15" aria-hidden="true" />
                    <Ellipsis v-else :size="17" aria-hidden="true" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuPortal>
                  <DropdownMenuContent class="conversation-menu" align="start" :side-offset="5" :collision-padding="12">
                    <DropdownMenuItem class="conversation-menu-item" @select="setPinned(c)">
                      <PinOff v-if="c.is_pinned" :size="15" aria-hidden="true" /><Pin v-else :size="15" aria-hidden="true" />
                      {{ c.is_pinned ? '取消置顶' : '置顶会话' }}
                    </DropdownMenuItem>
                    <DropdownMenuItem class="conversation-menu-item" @select="openRename(c)">
                      <PencilLine :size="15" aria-hidden="true" /> 重命名
                    </DropdownMenuItem>
                    <DropdownMenuSeparator class="menu-separator" />
                    <DropdownMenuItem class="conversation-menu-item danger" @select="$emit('remove', c)">
                      <Trash2 :size="15" aria-hidden="true" /> 删除对话
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenuPortal>
              </DropdownMenuRoot>
            </li>
          </ul>
        </section>
        <button v-if="hasMore" type="button" class="load-more" :disabled="loadingMore" @click="$emit('load-more')">
          <LoaderCircle v-if="loadingMore" class="spin" :size="14" aria-hidden="true" />
          {{ loadingMore ? '正在加载…' : '加载更多会话' }}
        </button>
      </template>
    </div>
    <UiDialog v-model:open="renameOpen" title="重命名会话" @close-auto-focus="restoreRenameFocus">
      <form id="rename-conversation-form" class="rename-form" @submit.prevent="saveRename">
        <label for="rename-conversation-title">会话名称</label>
        <input id="rename-conversation-title" ref="renameInput" v-model="renameTitle" type="text" maxlength="200"
          :aria-invalid="!!renameError" :aria-describedby="renameError ? 'rename-conversation-error' : undefined" />
        <p v-if="renameError" id="rename-conversation-error" role="alert">{{ renameError }}</p>
      </form>
      <template #footer>
        <UiButton type="button" variant="ghost" :disabled="renameSaving" @click="renameOpen = false">取消</UiButton>
        <UiButton type="submit" variant="primary" form="rename-conversation-form" :loading="renameSaving" :disabled="!renameTitle.trim()">保存名称</UiButton>
      </template>
    </UiDialog>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, reactive, ref } from 'vue'
import { Clock, Ellipsis, LoaderCircle, MessageSquare, PanelLeftClose, PencilLine, Pin, PinOff, Search, SquarePen, Trash2 } from '@lucide/vue'
import { DropdownMenuContent, DropdownMenuItem, DropdownMenuPortal, DropdownMenuRoot, DropdownMenuSeparator, DropdownMenuTrigger } from 'reka-ui'
import { ElMessage } from 'element-plus'
import type { Conversation, ConversationMetadata } from '@/api/conversations'
import UiButton from '@/components/ui/UiButton.vue'
import UiDialog from '@/components/ui/UiDialog.vue'
import { splitConversationHistory } from './interactions'

const props = defineProps<{
  items: Conversation[]
  activeId?: number
  deletingId: number | null
  loading: boolean
  failed: boolean
  creating: boolean
  hasMore: boolean
  loadingMore: boolean
  status: (item: Conversation) => string
  updateMetadata: (item: Conversation, changes: ConversationMetadata) => Promise<void>
}>()

defineEmits<{
  close: []
  retry: []
  'load-more': []
  create: []
  select: [item: Conversation]
  remove: [item: Conversation]
}>()

const query = ref('')
const history = ref<HTMLElement>()
const newChat = ref<HTMLButtonElement>()
const search = ref<HTMLInputElement>()
const metadataBusy = reactive(new Set<number>())
const renameInput = ref<HTMLInputElement>()
const renameItem = ref<Conversation>()
const renameTitle = ref('')
const renameError = ref('')
const renameSaving = computed(() => !!renameItem.value && metadataBusy.has(renameItem.value.id))
const renaming = ref(false)
const renameOpen = computed({
  get: () => renaming.value,
  set: (open) => { if (!renameSaving.value) renaming.value = open },
})

const groups = computed(() => {
  const { pinned, recent } = splitConversationHistory(props.items, query.value)
  return [
    { id: 'pinned', label: '已置顶', icon: Pin, items: pinned, empty: '用会话菜单置顶常用对话' },
    { id: 'recent', label: '最近对话', icon: Clock, items: recent, empty: '新建对话后，可在这里继续。' },
  ].filter((group) => !query.value.trim() || group.items.length)
})

function isBusy(id: number) {
  return props.deletingId === id || metadataBusy.has(id)
}

function focusAction(id?: number) {
  const trigger = id !== undefined && history.value?.querySelector<HTMLButtonElement>(`[data-conversation-action="${id}"]`)
  if (trigger) trigger.focus()
  else (search.value || newChat.value)?.focus()
}

async function setPinned(item: Conversation) {
  if (isBusy(item.id)) return
  metadataBusy.add(item.id)
  try {
    await props.updateMetadata(item, { is_pinned: !item.is_pinned })
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '更新置顶状态失败，请重试')
  } finally {
    metadataBusy.delete(item.id)
    const focused = document.activeElement
    if (focused === document.body || (focused instanceof HTMLElement && focused.dataset.conversationAction === String(item.id))) {
      await nextTick()
      focusAction(item.id)
    }
  }
}

async function openRename(item: Conversation) {
  renameItem.value = item
  renameTitle.value = item.title || '新对话'
  renameError.value = ''
  renaming.value = true
  await nextTick()
  renameInput.value?.focus()
  renameInput.value?.select()
}

function restoreRenameFocus(event: Event) {
  event.preventDefault()
  const id = renameItem.value?.id
  renameItem.value = undefined
  void nextTick(() => focusAction(id))
}

async function saveRename() {
  const item = renameItem.value
  const title = renameTitle.value.trim()
  if (!item || !title || isBusy(item.id)) return
  renameError.value = ''
  metadataBusy.add(item.id)
  try {
    await props.updateMetadata(item, { title })
    renaming.value = false
  } catch (error) {
    renameError.value = error instanceof Error ? error.message : '重命名失败，请重试'
  } finally {
    metadataBusy.delete(item.id)
  }
}

const dateFormat = new Intl.DateTimeFormat('zh-CN', { month: 'short', day: 'numeric' })

function formatDate(value: string | null) {
  if (!value) return ''
  // 会话接口将数据库 UTC 时间序列化为 ISO 字符串，部分数据没有时区后缀。
  const timestamp = value.includes('T') && !/(Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value + 'Z' : value
  const date = new Date(timestamp)
  return Number.isNaN(date.valueOf()) ? '' : dateFormat.format(date)
}
</script>

<style scoped>
.history { display: flex; flex-direction: column; gap: 12px; background: var(--ui-bg); min-height: 0; }
.history-head { padding: 14px 12px 0; }
.history-heading { display: flex; align-items: center; justify-content: space-between; margin: 0 0 8px 4px; }
.history-heading h2 { font-size: 13px; font-weight: 650; color: var(--ui-text); }
.square-button {
  display: inline-flex; justify-content: center; align-items: center; width: 30px; height: 30px;
  border: 0; background: transparent; color: var(--ui-muted); border-radius: var(--ui-radius-sm); cursor: pointer;
}
.square-button:hover { background: var(--ui-hover); color: var(--ui-text); }
.new-chat {
  width: 100%; display: flex; align-items: center; justify-content: center; gap: 8px; padding: 8px 10px;
  color: var(--ui-text); background: var(--ui-surface); border: 1px solid var(--ui-border-strong);
  border-radius: var(--ui-radius-md); cursor: pointer; font-weight: 600; box-shadow: var(--ui-shadow-sm);
}
.new-chat:hover { border-color: var(--ui-accent); color: var(--ui-accent); }
.new-chat:disabled { cursor: wait; opacity: .65; }
.history-search {
  margin: 0 12px; padding: 0 9px; min-height: 32px; display: flex; align-items: center; gap: 8px;
  color: var(--ui-muted); background: var(--ui-surface); border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md);
}
.history-search svg { flex: none; }
.history-search input { width: 100%; min-width: 0; padding: 6px 0; border: 0; background: transparent; color: var(--ui-text); font-size: 13px; }
.history-search input:focus { outline: none; }
.history-search:focus-within { border-color: var(--ui-accent); }
.history-list { flex: 1; overflow: auto; padding: 0 8px 16px; scrollbar-gutter: stable; }
.history-list ul { list-style: none; margin: 0; padding: 0; }
.history-state { display: flex; align-items: center; gap: 6px; padding: 8px; color: var(--ui-muted); font-size: 12px; }
.retry { color: var(--ui-accent); border: none; background: transparent; cursor: pointer; }
.history-group { padding: 3px 0; }
.history-group + .history-group { margin-top: 9px; border-top: 1px solid var(--ui-border); padding-top: 9px; }
.group-heading { display: flex; align-items: center; gap: 7px; margin: 2px 7px 5px; color: var(--ui-secondary); }
.group-heading h3 { font-size: 12px; font-weight: 650; line-height: 22px; }
.group-heading svg { flex: none; }
.pinned .group-heading { color: var(--ui-accent); }
.group-count { margin-inline-start: auto; min-width: 20px; border-radius: 20px; padding: 1px 5px; background: var(--ui-surface); color: var(--ui-muted); font-size: 11px; text-align: center; }
.group-empty { padding: 7px 8px; margin: 0; color: var(--ui-muted); font-size: 12px; overflow-wrap: anywhere; }
.conv-item { display: flex; align-items: center; gap: 2px; margin: 2px 0; border: 1px solid transparent; border-radius: var(--ui-radius-md); }
.conv-item:hover { background: var(--ui-hover); }
.conv-item.active { background: var(--ui-accent-soft); border-color: color-mix(in srgb, var(--ui-accent) 17%, transparent); }
.conv-target { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; text-align: start; border: 0; border-radius: var(--ui-radius-md); background: transparent; padding: 6px 5px 6px 7px; color: var(--ui-text); cursor: pointer; }
.conv-icon { display: grid; place-items: center; flex: none; width: 23px; height: 23px; color: var(--ui-muted); background: var(--ui-surface); border-radius: var(--ui-radius-sm); }
.pinned .conv-icon, .conv-item.active .conv-icon { color: var(--ui-accent); }
.conv-copy { display: grid; gap: 2px; flex: 1; min-width: 0; }
.conv-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.conv-item.active .conv-title { color: var(--ui-accent); font-weight: 650; }
.conv-date, .conv-status { font-size: 12px; color: var(--ui-muted); }
.conv-status { color: var(--ui-info); }
.conv-status.waiting { color: var(--ui-warning); }
.conv-actions { flex: none; margin-inline-end: 3px; width: 28px; }
.conversation-menu {
  z-index: var(--ui-z-popover); min-width: 145px; padding: 4px; color: var(--ui-text);
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-surface); box-shadow: var(--ui-shadow-sm);
}
.conversation-menu-item { display: flex; align-items: center; gap: 8px; padding: 7px 8px; outline: none; border-radius: var(--ui-radius-sm); cursor: pointer; font-size: 13px; user-select: none; }
.conversation-menu-item[data-highlighted] { background: var(--ui-hover); }
.conversation-menu-item.danger { color: var(--ui-danger); }
.conversation-menu-item.danger[data-highlighted] { background: var(--ui-danger-soft); }
.menu-separator { height: 1px; background: var(--ui-border); margin: 3px 4px; }
.load-more {
  display: flex; align-items: center; justify-content: center; gap: 6px; width: 100%; margin-top: 9px; min-height: 32px;
  border: 1px solid var(--ui-border); border-radius: var(--ui-radius-md); background: var(--ui-surface); color: var(--ui-secondary); font-size: 12px; cursor: pointer;
}
.load-more:hover { color: var(--ui-accent); border-color: var(--ui-accent); }
.load-more:disabled { opacity: .75; cursor: wait; }
.rename-form { display: grid; gap: 8px; }
.rename-form label { font-weight: 650; color: var(--ui-text); font-size: 13px; }
.rename-form input { padding: 8px 10px; min-height: 36px; border: 1px solid var(--ui-border-strong); border-radius: var(--ui-radius-md); }
.rename-form input:focus { outline: 1px solid var(--ui-accent); border-color: var(--ui-accent); }
.rename-form [role="alert"] { margin: 0; color: var(--ui-danger); font-size: 12px; }
.spin { animation: rotate 1s linear infinite; }
@keyframes rotate { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
