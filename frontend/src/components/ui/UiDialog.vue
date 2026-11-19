<script setup lang="ts">
import { DialogClose, DialogContent, DialogDescription, DialogOverlay, DialogPortal, DialogRoot, DialogTitle } from 'reka-ui'
import { X } from '@lucide/vue'
import UiButton from './UiButton.vue'

defineProps<{ title: string; description?: string; variant?: 'modal' | 'drawer' | 'command' }>()
defineEmits<{ closeAutoFocus: [event: Event] }>()
const open = defineModel<boolean>('open', { required: true })
</script>

<template>
  <DialogRoot v-model:open="open">
    <DialogPortal>
      <DialogOverlay class="ui-dialog-overlay" />
      <DialogContent class="ui-dialog" :class="`ui-dialog--${variant || 'modal'}`" @close-auto-focus="$emit('closeAutoFocus', $event)">
        <header class="ui-dialog-header" :class="{ 'sr-only': variant === 'command' }">
          <div>
            <DialogTitle>{{ title }}</DialogTitle>
            <DialogDescription :class="{ 'sr-only': !description }">{{ description || title }}</DialogDescription>
          </div>
          <DialogClose v-if="variant !== 'command'" as-child>
            <UiButton variant="ghost" size="icon" aria-label="关闭"><X /></UiButton>
          </DialogClose>
        </header>
        <slot />
        <footer v-if="$slots.footer" class="ui-dialog-footer"><slot name="footer" /></footer>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<style scoped>
.ui-dialog-overlay {
  position: fixed; inset: 0; z-index: var(--ui-z-dialog);
  background: var(--ui-overlay);
  animation: enter var(--ui-duration) var(--ui-ease);
}
.ui-dialog {
  position: fixed; z-index: calc(var(--ui-z-dialog) + 1);
  top: 50%; left: 50%; transform: translate(-50%, -50%);
  width: min(460px, calc(100vw - 24px)); max-height: calc(100dvh - 24px);
  overflow: auto; padding: var(--ui-space-6);
  border: var(--ui-line); border-radius: var(--ui-radius-dialog);
  background: var(--ui-surface); box-shadow: var(--ui-shadow-md);
  animation: enter var(--ui-duration) var(--ui-ease);
}
.ui-dialog--command {
  width: min(560px, calc(100vw - 24px));
  top: clamp(12px, 16dvh, 145px); transform: translateX(-50%); padding: 0;
}
.ui-dialog--drawer {
  left: 0; top: 0; bottom: 0; width: min(310px, 88vw); transform: none;
  border-radius: 0; max-height: 100%; padding: 0; display: flex; flex-direction: column;
}
.ui-dialog--drawer > header { padding: 12px; margin: 0; border-bottom: var(--ui-line); }
.ui-dialog-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: var(--ui-space-4); }
.ui-dialog-header h2 { font-size: var(--ui-size-lg); line-height: 1.4; }
.ui-dialog-header p { color: var(--ui-muted); margin-top: var(--ui-space-1); }
.ui-dialog-footer { display: flex; justify-content: flex-end; gap: var(--ui-space-2); margin-top: var(--ui-space-5); }
@keyframes enter { from { opacity: 0; } }
</style>

