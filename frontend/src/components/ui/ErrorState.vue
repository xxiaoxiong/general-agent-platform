<script setup lang="ts">
import { CircleAlert } from '@lucide/vue'
import EmptyState from './EmptyState.vue'
import UiButton from './UiButton.vue'

withDefaults(defineProps<{ title?: string; description?: string }>(), {
  title: '加载失败',
  description: '请检查连接后重试。',
})
defineEmits<{ retry: [] }>()
</script>

<template>
  <EmptyState :icon="CircleAlert" :title="title" :description="description">
    <UiButton size="sm" @click="$emit('retry')">重试</UiButton>
  </EmptyState>
</template>
