import { ref } from 'vue'

type Theme = 'light' | 'dark'
const key = 'agent-platform-theme'
const theme = ref<Theme>('light')

export function setTheme(value: Theme) {
  theme.value = value
  document.documentElement.dataset.theme = value
  document.documentElement.classList.toggle('dark', value === 'dark')
  localStorage.setItem(key, value)
}

export function useTheme() {
  return { theme, toggleTheme: () => setTheme(theme.value === 'light' ? 'dark' : 'light') }
}

export function initializeTheme() {
  setTheme(localStorage.getItem(key) === 'dark' ? 'dark' : 'light')
}
