import { defineConfig } from '@playwright/test'

/**
 * 富内容渲染 E2E：使用独立测试页（e2e/harness.html）在真实浏览器中验证
 * Mermaid SVG / ECharts Canvas 的实际生成、交互、降级与响应式布局。
 *
 * `npm test` 仍只跑 Node 单元测试；本套件通过 `npm run test:e2e` 单独执行，
 * 不要求普通测试环境提前下载浏览器（默认复用系统 Edge / Chrome）。
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:5199',
    viewport: { width: 1440, height: 900 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [{
    name: 'system-browser',
    use: { channel: (process.env.PLAYWRIGHT_CHANNEL as 'msedge' | 'chrome' | undefined) || 'msedge' },
  }],
  webServer: {
    command: 'npm run dev -- --port 5199 --strictPort',
    url: 'http://localhost:5199/e2e/harness.html',
    reuseExistingServer: true,
    timeout: 120_000,
  },
})
