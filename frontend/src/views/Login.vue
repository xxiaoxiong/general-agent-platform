<script setup lang="ts">
import { ref, reactive } from 'vue'
import { AxiosError } from 'axios'
import { Eye, EyeOff } from '@lucide/vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import BrandMark from '@/components/layout/BrandMark.vue'
import UiButton from '@/components/ui/UiButton.vue'

const router = useRouter()
const auth = useAuthStore()
const loading = ref(false)
const showPassword = ref(false)
const error = ref('')
const form = reactive({ username: '', password: '' })

async function handleLogin() {
  if (loading.value) return
  error.value = ''
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    await router.push('/')
  } catch (cause) {
    const status = cause instanceof AxiosError ? cause.response?.status : undefined
    if (status === 401) error.value = '用户名或密码错误，请重新输入。'
    else if (status === 403) error.value = '此账号已停用，请联系管理员。'
    else error.value = '暂时无法登录，请稍后重试。'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <div class="login-brand"><BrandMark :size="32" /><span>General Agent Platform</span></div>
    <section class="login-box" aria-labelledby="login-title">
      <header class="login-heading">
        <h1 id="login-title">登录工作空间</h1>
        <p>使用您的平台账号继续。</p>
      </header>
      <form class="login-form" @submit.prevent="handleLogin">
        <div class="login-field">
          <label for="login-username">用户名或邮箱</label>
          <input id="login-username" v-model="form.username" autocomplete="username" required />
        </div>
        <div class="login-field">
          <label for="login-password">密码</label>
          <div class="password-field">
            <input id="login-password" v-model="form.password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" required />
            <UiButton variant="ghost" size="icon" :aria-label="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword">
              <EyeOff v-if="showPassword" /><Eye v-else />
            </UiButton>
          </div>
        </div>
        <p v-if="error" class="login-error" role="alert">{{ error }}</p>
        <UiButton variant="primary" type="submit" class="login-submit" :loading="loading">登录</UiButton>
      </form>
    </section>
  </main>
</template>

<style scoped>
.login-page {
  min-height: 100%; display: grid; align-content: center; justify-items: center; gap: 24px;
  background: var(--ui-bg); padding: 32px 16px 12vh;
}
.login-brand { display: flex; align-items: center; gap: 9px; font-weight: var(--ui-weight-semibold); color: var(--ui-secondary); }
.login-box {
  width: min(100%, 404px); padding: clamp(22px, 5vw, 36px);
  border: var(--ui-line); background: var(--ui-surface); border-radius: var(--ui-radius-dialog); box-shadow: var(--ui-shadow-sm);
}
.login-heading { margin-bottom: 24px; }
.login-heading h1 { font-size: var(--ui-size-xl); line-height: 1.35; letter-spacing: var(--ui-tracking-tight); }
.login-heading p { color: var(--ui-muted); margin-top: 6px; }
.login-form, .login-field { display: grid; gap: 14px; }
.login-field { gap: 6px; }
.login-field label { font-weight: var(--ui-weight-medium); }
.login-field input {
  width: 100%; height: 40px; padding: 0 11px; color: var(--ui-text); background: var(--ui-bg);
  border: var(--ui-line-strong); border-radius: var(--ui-radius-md);
}
.password-field { position: relative; }
.password-field input { padding-right: 44px; }
.password-field button { position: absolute; top: 4px; right: 4px; }
.login-submit { width: 100%; margin-top: 6px; }
.login-error { border-radius: var(--ui-radius-md); background: var(--ui-danger-soft); color: var(--ui-danger); padding: 8px 10px; }
</style>
