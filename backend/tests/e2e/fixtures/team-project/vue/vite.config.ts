import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/inspection/',
  plugins: [vue()],
  build: { outDir: 'web-dist' },
  server: { proxy: { '/api': 'http://127.0.0.1:8765' } },
})
