<script setup lang="ts">
import { TooltipContent, TooltipPortal, TooltipProvider, TooltipRoot, TooltipTrigger } from 'reka-ui'
import BrandMark from './BrandMark.vue'
import type { NavigationGroup } from './navigation'

defineProps<{ groups: NavigationGroup[]; active: string; collapsed?: boolean; mobile?: boolean }>()
defineEmits<{ navigate: [] }>()
</script>

<template>
  <aside class="app-sidebar" :class="{ 'sidebar-rail': collapsed }" :aria-label="mobile ? '移动导航' : '应用导航'">
    <RouterLink v-if="!mobile" to="/workspace" class="sidebar-brand" aria-label="General Agent Platform">
      <BrandMark />
      <span v-if="!collapsed">Agent Platform</span>
    </RouterLink>
    <nav class="sidebar-nav" aria-label="主要导航">
      <TooltipProvider :delay-duration="160">
        <section v-for="group in groups" :key="group.title" class="nav-group" :aria-label="group.title">
          <h2 v-if="!collapsed">{{ group.title }}</h2>
          <TooltipRoot v-for="entry in group.entries" :key="entry.name" :disabled="!collapsed">
            <TooltipTrigger as-child>
              <RouterLink
                :to="entry.path"
                class="nav-item"
                :class="{ 'nav-current': active === entry.path }"
                :aria-current="active === entry.path ? 'location' : undefined"
                :aria-label="entry.title"
                @click="$emit('navigate')"
              >
                <component :is="entry.icon" :size="17" :stroke-width="1.7" aria-hidden="true" />
                <span v-if="!collapsed">{{ entry.title }}</span>
              </RouterLink>
            </TooltipTrigger>
            <TooltipPortal>
              <TooltipContent class="sidebar-tooltip" side="right" :side-offset="8">{{ entry.title }}</TooltipContent>
            </TooltipPortal>
          </TooltipRoot>
        </section>
      </TooltipProvider>
    </nav>
    <div v-if="$slots.footer" class="sidebar-footer"><slot name="footer" /></div>
  </aside>
</template>

<style scoped>
.app-sidebar { min-height: 0; height: 100%; display: flex; flex-direction: column; background: var(--ui-bg); }
.sidebar-brand {
  min-height: var(--ui-header-height); display: flex; align-items: center; gap: var(--ui-space-3);
  padding-inline: var(--ui-space-4); color: var(--ui-text); font-size: .9375rem; font-weight: var(--ui-weight-semibold);
  text-decoration: none; letter-spacing: -.015em;
}
.sidebar-rail .sidebar-brand { justify-content: center; padding: 0; }
.sidebar-nav { flex: 1; min-height: 0; overflow: auto; padding: 8px 10px 14px; }
.nav-group + .nav-group { margin-top: 20px; }
.sidebar-rail .nav-group + .nav-group { margin-top: 9px; padding-top: 9px; border-top: var(--ui-line); }
.nav-group h2 { font-size: var(--ui-size-xs); font-weight: var(--ui-weight-semibold); color: var(--ui-muted); padding: 0 9px 5px; }
.nav-item {
  position: relative; display: flex; align-items: center; gap: 10px; min-height: 34px;
  margin: 2px 0; padding: 0 9px; border-radius: var(--ui-radius-md); color: var(--ui-secondary);
  text-decoration: none; white-space: nowrap; font-size: var(--ui-size-sm);
  transition: background var(--ui-duration) var(--ui-ease), color var(--ui-duration) var(--ui-ease);
}
.nav-item svg { flex: none; color: var(--ui-muted); }
.nav-item:hover { color: var(--ui-text); background: var(--ui-hover); }
.nav-item.nav-current { color: var(--ui-accent); background: var(--ui-accent-soft); font-weight: var(--ui-weight-semibold); }
.nav-current svg { color: inherit; }
.sidebar-rail .nav-item { justify-content: center; padding-inline: 0; }
.sidebar-footer { border-top: var(--ui-line); padding: 8px 10px; }
.sidebar-tooltip {
  z-index: var(--ui-z-popover); background: var(--ui-text); color: var(--ui-surface);
  padding: 5px 9px; border-radius: var(--ui-radius-sm); font-size: var(--ui-size-xs);
}
</style>
