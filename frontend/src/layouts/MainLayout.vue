<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Menu, Moon, PanelLeftClose, PanelLeftOpen, Search, Sun } from '@lucide/vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { authApi } from '@/api'
import AppSidebar from '@/components/layout/AppSidebar.vue'
import AccountMenu from '@/components/layout/AccountMenu.vue'
import CommandDialog from '@/components/layout/CommandDialog.vue'
import { getNavigation } from '@/components/layout/navigation'
import UiButton from '@/components/ui/UiButton.vue'
import UiDialog from '@/components/ui/UiDialog.vue'
import { useTheme } from '@/lib/theme'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const { theme, toggleTheme } = useTheme()
const collapsed = ref(localStorage.getItem('agent-platform-sidebar') === 'collapsed')
const mobileOpen = ref(false)
const commandOpen = ref(false)
const showProfile = ref(false)
const showPassword = ref(false)
const savingPassword = ref(false)
const pwdForm = ref({ old_password: '', new_password: '' })
const main = ref<HTMLElement>()
const previousFocus = ref<HTMLElement | null>(null)
let accountFromMobile = false
const hotkey = /Mac|iPhone|iPad/i.test(navigator.platform) ? '⌘ K' : 'Ctrl K'

const navigation = computed(() => getNavigation(router, authStore.isSuperuser))
const currentTitle = computed(() => String(route.meta.title || '工作空间'))
const activePath = computed(() => {
  const paths = navigation.value.flatMap(group => group.entries.map(entry => entry.path))
  return paths.filter(path => route.path === path || route.path.startsWith(path + '/'))
    .sort((a, b) => b.length - a.length)[0] || ''
})

watch(collapsed, value => localStorage.setItem('agent-platform-sidebar', value ? 'collapsed' : 'expanded'))
watch(() => route.fullPath, () => {
  mobileOpen.value = false
  main.value?.scrollTo({ top: 0, left: 0 })
})
watch(commandOpen, async open => {
  if (open) {
    previousFocus.value = document.activeElement instanceof HTMLElement ? document.activeElement : null
  } else {
    await nextTick()
    previousFocus.value?.focus()
  }
})
watch(showPassword, open => {
  if (!open) pwdForm.value = { old_password: '', new_password: '' }
})

function keyboard(event: KeyboardEvent) {
  if (event.isComposing || event.repeat || event.altKey || event.shiftKey || !(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'k') return
  const target = event.target
  if (!commandOpen.value && target instanceof HTMLElement && target.closest('input, textarea, [contenteditable="true"], [role="dialog"]')) return
  event.preventDefault()
  if (!mobileOpen.value && !showPassword.value && !showProfile.value) commandOpen.value = !commandOpen.value
}

onMounted(() => window.addEventListener('keydown', keyboard))
onUnmounted(() => window.removeEventListener('keydown', keyboard))

async function accountCommand(command: 'profile' | 'password' | 'logout') {
  accountFromMobile = mobileOpen.value
  mobileOpen.value = false
  if (command === 'profile') {
    showProfile.value = true
  } else if (command === 'password') {
    showPassword.value = true
  } else {
    const confirmed = await ElMessageBox.confirm('确认退出当前账号？', '退出登录', {
      type: 'warning', confirmButtonText: '退出登录', cancelButtonText: '取消',
    }).then(() => true, () => false)
    if (!confirmed) return
    authStore.logout()
    await router.push('/login')
  }
}

function restoreAccountFocus(event: Event) {
  event.preventDefault()
  const source = document.getElementById(accountFromMobile ? 'mobile-navigation-trigger' : 'desktop-account-trigger')
  if (source?.getClientRects().length) source.focus()
}

async function submitPassword() {
  if (savingPassword.value) return
  if (!pwdForm.value.old_password || !pwdForm.value.new_password) {
    ElMessage.warning('请填写原密码和新密码')
    return
  }
  savingPassword.value = true
  try {
    await authApi.changePassword({ ...pwdForm.value })
    ElMessage.success('密码已修改，请重新登录')
    showPassword.value = false
    authStore.logout()
    await router.push('/login')
  } catch {
    // 请求拦截器统一显示接口错误。
  } finally {
    savingPassword.value = false
  }
}
</script>

<template>
  <div class="app-shell" :class="{ 'app-shell--rail': collapsed }">
    <a class="skip-link" href="#main-content" @click.prevent="main?.focus()">跳到主要内容</a>
    <div class="desktop-sidebar">
      <AppSidebar :groups="navigation" :active="activePath" :collapsed="collapsed">
        <template #footer><AccountMenu trigger-id="desktop-account-trigger" :collapsed="collapsed" @command="accountCommand" /></template>
      </AppSidebar>
    </div>
    <div class="app-frame">
      <header class="app-bar">
        <div class="app-bar-context">
          <UiButton
            class="desktop-toggle" size="icon" variant="ghost" :aria-label="collapsed ? '展开侧栏' : '收起侧栏'"
            :title="collapsed ? '展开侧栏' : '收起侧栏'" :aria-expanded="!collapsed" @click="collapsed = !collapsed"
          >
            <PanelLeftOpen v-if="collapsed" /><PanelLeftClose v-else />
          </UiButton>
          <UiButton id="mobile-navigation-trigger" class="mobile-toggle" size="icon" variant="ghost" aria-label="打开导航" @click="mobileOpen = true"><Menu /></UiButton>
          <span class="app-bar-divider" aria-hidden="true" />
          <span class="app-bar-title">{{ currentTitle }}</span>
        </div>
        <div class="app-bar-actions">
          <button class="search-trigger" type="button" aria-label="搜索页面" @click="commandOpen = true">
            <Search :size="15" aria-hidden="true" />
            <span>搜索页面</span>
            <kbd>{{ hotkey }}</kbd>
          </button>
          <UiButton
            size="icon" variant="ghost" :aria-label="theme === 'dark' ? '切换浅色主题' : '切换暗色主题'"
            :title="theme === 'dark' ? '浅色主题' : '暗色主题'" @click="toggleTheme"
          ><Sun v-if="theme === 'dark'" /><Moon v-else /></UiButton>
        </div>
      </header>
      <main id="main-content" ref="main" class="main-content" tabindex="-1"><RouterView /></main>
    </div>
  </div>

  <UiDialog v-model:open="mobileOpen" title="Agent Platform" variant="drawer">
    <AppSidebar :groups="navigation" :active="activePath" mobile @navigate="mobileOpen = false">
      <template #footer><AccountMenu mobile @command="accountCommand" /></template>
    </AppSidebar>
  </UiDialog>
  <CommandDialog v-model:open="commandOpen" :groups="navigation" @navigate="router.push($event)" />
  <UiDialog v-model:open="showProfile" title="个人信息" @close-auto-focus="restoreAccountFocus">
    <dl v-if="authStore.user" class="account-details">
      <dt>用户名</dt><dd>{{ authStore.user.username }}</dd>
      <dt>邮箱</dt><dd>{{ authStore.user.email }}</dd>
      <dt>姓名</dt><dd>{{ authStore.user.full_name || '—' }}</dd>
      <dt>账号角色</dt><dd>{{ authStore.isSuperuser ? '管理员' : '成员' }}</dd>
    </dl>
  </UiDialog>
  <UiDialog v-model:open="showPassword" title="修改密码" description="修改成功后需要重新登录。" @close-auto-focus="restoreAccountFocus">
    <form id="password-form" class="shell-form" @submit.prevent="submitPassword">
      <label for="old-password">原密码</label>
      <el-input id="old-password" v-model="pwdForm.old_password" type="password" autocomplete="current-password" show-password />
      <label for="new-password">新密码</label>
      <el-input id="new-password" v-model="pwdForm.new_password" type="password" autocomplete="new-password" show-password />
    </form>
    <template #footer>
      <UiButton @click="showPassword = false">取消</UiButton>
      <UiButton variant="primary" type="submit" form="password-form" :loading="savingPassword">确认修改</UiButton>
    </template>
  </UiDialog>
</template>

<style scoped>
.app-shell {
  --sidebar-layout-width: var(--ui-sidebar-width);
  display: grid; grid-template-columns: var(--sidebar-layout-width) minmax(0, 1fr);
  height: 100dvh; overflow: hidden;
}
.app-shell--rail { --sidebar-layout-width: var(--ui-sidebar-rail); }
.desktop-sidebar { min-width: 0; min-height: 0; border-right: var(--ui-line); }
.app-frame { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.app-bar {
  display: flex; flex: none; justify-content: space-between; align-items: center; gap: 8px;
  height: var(--ui-header-height); padding-inline: 14px; background: var(--ui-surface); border-bottom: var(--ui-line);
}
.app-bar-context, .app-bar-actions { display: flex; align-items: center; gap: 10px; min-width: 0; }
.app-bar-title { font-size: var(--ui-size-sm); font-weight: var(--ui-weight-medium); overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.app-bar-divider { flex: none; width: 1px; height: 16px; background: var(--ui-border); }
.search-trigger {
  display: flex; align-items: center; gap: 8px; height: 30px; padding: 0 7px 0 9px; white-space: nowrap;
  background: var(--ui-bg); border: var(--ui-line); border-radius: var(--ui-radius-md); color: var(--ui-muted); cursor: pointer;
  font-size: var(--ui-size-sm);
}
.search-trigger:hover { background: var(--ui-subtle); color: var(--ui-text); border-color: var(--ui-border-strong); }
.search-trigger kbd { margin-left: 16px; padding: 1px 4px; border: var(--ui-line); border-radius: var(--ui-radius-sm); line-height: 1.2; font-size: var(--ui-size-xs); }
.main-content { flex: 1; min-width: 0; min-height: 0; overflow: auto; background: var(--ui-bg); }
.mobile-toggle { display: none; }
.skip-link { position: absolute; z-index: var(--ui-z-navigation); transform: translateY(-120%); padding: 8px 12px; background: var(--ui-accent); color: var(--ui-on-accent); }
.skip-link:focus { transform: none; }
.shell-form { display: grid; gap: 8px; }
.shell-form label:not(:first-child) { margin-top: 8px; }
.account-details { margin: 0; display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 12px 20px; }
.account-details dt { color: var(--ui-muted); }
.account-details dd { margin: 0; overflow-wrap: anywhere; }

@media (max-width: 850px) {
  .app-shell { grid-template-columns: minmax(0, 1fr); }
  .desktop-sidebar, .desktop-toggle { display: none; }
  .mobile-toggle { display: inline-flex; }
  .app-bar { padding-inline: 9px; }
}
@media (max-width: 480px) {
  .app-bar-actions { gap: 5px; }
  .search-trigger { padding: 0; justify-content: center; width: 32px; }
  .search-trigger span, .search-trigger kbd { display: none; }
}
</style>
