<script setup lang="ts">
import { ChevronDown, KeyRound, LogOut, UserRound } from '@lucide/vue'
import { DropdownMenuContent, DropdownMenuItem, DropdownMenuPortal, DropdownMenuRoot, DropdownMenuSeparator, DropdownMenuTrigger } from 'reka-ui'
import { useAuthStore } from '@/stores/auth'

defineProps<{ collapsed?: boolean; mobile?: boolean; triggerId?: string }>()
defineEmits<{ command: [command: 'profile' | 'password' | 'logout'] }>()
const auth = useAuthStore()
</script>

<template>
  <DropdownMenuRoot>
    <DropdownMenuTrigger :id="triggerId" class="account-trigger" :class="{ 'account-trigger--rail': collapsed }" aria-label="账号菜单">
      <span class="account-avatar">
        <img v-if="auth.user?.avatar" :src="auth.user.avatar" alt="" />
        <template v-else>{{ (auth.user?.full_name || auth.user?.username || 'U').charAt(0).toUpperCase() }}</template>
      </span>
      <span v-if="!collapsed" class="account-name">{{ auth.user?.full_name || auth.user?.username }}</span>
      <ChevronDown v-if="!collapsed" :size="14" aria-hidden="true" />
    </DropdownMenuTrigger>
    <DropdownMenuPortal :disabled="mobile">
      <DropdownMenuContent side="top" align="start" :side-offset="8" class="account-menu">
        <DropdownMenuItem class="account-menu-item" @select="$emit('command', 'profile')"><UserRound /> 个人信息</DropdownMenuItem>
        <DropdownMenuItem class="account-menu-item" @select="$emit('command', 'password')"><KeyRound /> 修改密码</DropdownMenuItem>
        <DropdownMenuSeparator class="account-menu-divider" />
        <DropdownMenuItem class="account-menu-item account-menu-danger" @select="$emit('command', 'logout')"><LogOut /> 退出登录</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenuPortal>
  </DropdownMenuRoot>
</template>

<style scoped>
.account-trigger {
  display: flex; align-items: center; gap: 8px; width: 100%; min-height: 39px; padding: 4px 6px;
  background: transparent; border: 0; border-radius: var(--ui-radius-md); cursor: pointer; color: var(--ui-secondary); text-align: start;
}
.account-trigger:hover, .account-trigger[data-state="open"] { background: var(--ui-hover); }
.account-trigger--rail { justify-content: center; padding-inline: 0; }
.account-avatar {
  display: grid; place-items: center; flex: none; width: 27px; height: 27px; overflow: hidden;
  background: var(--ui-accent-soft); color: var(--ui-accent); border-radius: var(--ui-radius-md); font-weight: var(--ui-weight-semibold);
}
.account-avatar img { width: 100%; height: 100%; object-fit: cover; }
.account-name { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.account-trigger svg { flex: none; }
.account-menu {
  z-index: var(--ui-z-popover); min-width: 184px; padding: 4px;
  background: var(--ui-surface); border: var(--ui-line); border-radius: var(--ui-radius-md); box-shadow: var(--ui-shadow-md);
}
.account-menu-item {
  display: flex; align-items: center; gap: 9px; padding: 7px 9px;
  border-radius: var(--ui-radius-sm); color: var(--ui-secondary); cursor: pointer; outline: none;
}
.account-menu-item[data-highlighted] { background: var(--ui-subtle); color: var(--ui-text); }
.account-menu-item svg { width: 15px; height: 15px; }
.account-menu-item.account-menu-danger { color: var(--ui-danger); }
.account-menu-divider { border-top: var(--ui-line); margin: 4px; }
</style>
