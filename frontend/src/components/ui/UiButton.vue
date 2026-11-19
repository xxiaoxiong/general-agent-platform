<script setup lang="ts">
import { Primitive } from 'reka-ui'
import { LoaderCircle } from '@lucide/vue'
import { cva } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const props = withDefaults(defineProps<{
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md' | 'icon'
  type?: 'button' | 'submit' | 'reset'
  loading?: boolean
  disabled?: boolean
  asChild?: boolean
}>(), { variant: 'secondary', size: 'md', type: 'button' })

const button = cva(
  'inline-flex shrink-0 select-none items-center justify-center gap-2 whitespace-nowrap rounded-md border text-sm font-medium no-underline transition-colors duration-(--ui-duration) disabled:pointer-events-none disabled:opacity-55 [&_svg]:size-4 [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        primary: 'border-transparent bg-accent text-on-accent hover:bg-accent-hover',
        secondary: 'border-line-strong bg-surface text-foreground hover:bg-subtle',
        ghost: 'border-transparent bg-transparent text-secondary hover:bg-hover hover:text-foreground',
        danger: 'border-transparent bg-danger-soft text-danger hover:border-danger',
      },
      size: {
        sm: 'h-8 px-3',
        md: 'h-9 px-3.5',
        icon: 'size-8 p-0',
      },
    },
  },
)
</script>

<template>
  <Primitive
    as="button"
    :as-child="props.asChild"
    :type="props.asChild ? undefined : props.type"
    :class="cn(button({ variant, size }))"
    :aria-busy="loading || undefined"
    :disabled="disabled || loading"
  >
    <LoaderCircle v-if="loading" class="animate-spin motion-reduce:animate-none" aria-hidden="true" />
    <slot />
  </Primitive>
</template>
