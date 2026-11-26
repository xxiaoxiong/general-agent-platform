import { defineConfig } from '@playwright/test'

/**
 * Workspace Sandbox 真实浏览器 E2E：直接访问已部署栈（默认 http://localhost），
 * 不启动开发服务器。需要 E2E_USERNAME / E2E_PASSWORD（以及可选的 E2E_BASE_URL）。
 *
 * 运行：E2E_USERNAME=... E2E_PASSWORD=... npm run test:e2e:sandbox
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: 'sandbox-preview.spec.ts',
  timeout: 300_000,
  expect: { timeout: 25_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost',
    viewport: { width: 1440, height: 900 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    ignoreHTTPSErrors: true,
    channel: (process.env.PLAYWRIGHT_CHANNEL as 'msedge' | 'chrome' | undefined) || 'msedge',
  },
})
