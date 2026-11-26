/**
 * Workspace Sandbox 真实浏览器 E2E（已部署栈 + 真实 Docker Sandbox）。
 *
 * 前置：E2E_USERNAME / E2E_PASSWORD（可登录账号）；后端已启用 SANDBOX_ENABLED、
 * Runner 在线、workspace-sandbox 镜像可用。
 *
 * 覆盖：项目创建 → Sandbox 就绪 → Service 启动 → 会话绑定项目 → Preview iframe
 * 真实页面 → 页面刷新后重新获取 Grant → 切换项目不串显 → Services 面板。
 */
import fs from 'node:fs'
import path from 'node:path'
import { expect, test, type APIRequestContext } from '@playwright/test'

const USERNAME = process.env.E2E_USERNAME || ''
const PASSWORD = process.env.E2E_PASSWORD || ''
const API_BASE = process.env.E2E_API_BASE || process.env.E2E_BASE_URL || 'http://localhost'

async function login(request: APIRequestContext): Promise<string> {
  const response = await request.post(`${API_BASE}/api/v1/auth/login`, {
    data: { username: USERNAME, password: PASSWORD },
  })
  expect(response.ok(), `登录失败: ${response.status()} ${await response.text()}`).toBeTruthy()
  const data = await response.json()
  return data.access_token as string
}

async function waitForSandbox(request: APIRequestContext, token: string, projectId: number) {
  await expect.poll(async () => {
    const response = await request.get(
      `${API_BASE}/api/v2/projects/${projectId}/workspace`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    if (!response.ok()) return `http_${response.status()}`
    const data = await response.json()
    const sandbox = data.workspace?.sandbox
    if (sandbox?.observed_state === 'failed') return `failed:${sandbox.failure_message || ''}`
    return sandbox?.observed_state || 'none'
  }, { timeout: 180_000, intervals: [2000] }).toBe('ready')
}

async function waitForService(
  request: APIRequestContext, token: string, workspaceId: number, name: string,
) {
  await expect.poll(async () => {
    const response = await request.get(
      `${API_BASE}/api/v2/workspaces/${workspaceId}/services`,
      { headers: { Authorization: `Bearer ${token}` } },
    )
    if (!response.ok()) return `http_${response.status()}`
    const data = await response.json()
    return data.items?.find((item: any) => item.name === name)?.observed_state || 'none'
  }, { timeout: 120_000, intervals: [2000] }).toBe('healthy')
}

test('项目 Sandbox Preview 全链路（真实 iframe / 刷新 / 切换项目）', async ({ page, request }) => {
  test.skip(!USERNAME || !PASSWORD, '需要 E2E_USERNAME / E2E_PASSWORD')
  test.setTimeout(300_000)

  const token = await login(request)
  const headers = { Authorization: `Bearer ${token}` }
  const label = `E2E Sandbox ${Date.now()}`
  let projectId: number | null = null
  let conversationId: number | null = null

  try {
    // ── 1. 项目 + Workspace + Sandbox ────────────────────────────────
    const created = await request.post(`${API_BASE}/api/v2/projects`, {
      headers, data: { name: label },
    })
    expect(created.ok(), await created.text()).toBeTruthy()
    const project = await created.json()
    projectId = project.id
    const workspaceId = project.workspace.id as number

    const provisioned = await request.post(
      `${API_BASE}/api/v2/projects/${projectId}/workspace/provision`,
      { headers, data: {} },
    )
    expect(provisioned.ok(), await provisioned.text()).toBeTruthy()
    await waitForSandbox(request, token, projectId)

    // ── 2. Service：写一个真实页面并启动 HTTP 服务 ───────────────────
    const service = await request.post(
      `${API_BASE}/api/v2/workspaces/${workspaceId}/services`,
      {
        headers,
        data: {
          name: 'web',
          command:
            "printf '%s' '<!doctype html><html><body><h1>E2E Preview OK</h1></body></html>' > index.html && python3 -m http.server 8123 --bind 0.0.0.0",
          port: 8123,
          health_check_type: 'http',
          health_check_path: '/',
          health_check_timeout: 60,
        },
      },
    )
    expect(service.ok(), await service.text()).toBeTruthy()
    await waitForService(request, token, workspaceId, 'web')

    // ── 3. 会话绑定项目 ──────────────────────────────────────────────
    const conversation = await request.post(`${API_BASE}/api/v2/conversations`, {
      headers, data: { title: label, project_id: projectId },
    })
    expect(conversation.ok(), await conversation.text()).toBeTruthy()
    conversationId = (await conversation.json()).id

    // ── 4. 浏览器：登录态注入 → 打开会话 → Preview iframe ─────────────
    await page.addInitScript((accessToken: string) => {
      localStorage.setItem('access_token', accessToken)
      localStorage.setItem('refresh_token', '')
    }, token)
    await page.goto('/')
    await page.getByRole('button', { name: label, exact: true }).first().click()
    await expect(page.getByRole('button', { name: '工作目录文件' })).toBeVisible()

    await page.getByRole('button', { name: '工作目录文件' }).click()
    await page.getByRole('tab', { name: '预览' }).click()

    const iframe = page.locator('iframe[title="项目预览"]')
    await expect(iframe).toBeVisible({ timeout: 90_000 })
    const frame = page.frameLocator('iframe[title="项目预览"]')
    await expect(frame.locator('h1')).toContainText('E2E Preview OK', { timeout: 90_000 })

    const shotDir = path.resolve(process.cwd(), '..', 'docs', 'assets')
    fs.mkdirSync(shotDir, { recursive: true })
    await page.screenshot({ path: path.join(shotDir, 'workspace-sandbox-preview.png') })

    // ── 5. 刷新页面：重新获取 Grant，预览仍可用 ──────────────────────
    await page.reload()
    await page.getByRole('button', { name: label, exact: true }).first().click()
    await page.getByRole('button', { name: '工作目录文件' }).click()
    await expect(page.locator('iframe[title="项目预览"]')).toBeVisible({ timeout: 90_000 })
    await expect(
      page.frameLocator('iframe[title="项目预览"]').locator('h1'),
    ).toContainText('E2E Preview OK', { timeout: 90_000 })

    // ── 6. 切换项目：不串显上一个项目的预览 ──────────────────────────
    await page.locator('.project-switch select').selectOption('')
    await expect(page.getByText('当前会话未绑定项目')).toBeVisible()
    await expect(page.locator('iframe[title="项目预览"]')).toHaveCount(0)
    await page.locator('.project-switch select').selectOption(String(projectId))
    await expect(page.locator('iframe[title="项目预览"]')).toBeVisible({ timeout: 90_000 })

    // ── 7. Services 面板：状态与操作入口 ─────────────────────────────
    await page.getByRole('tab', { name: '服务' }).click()
    await expect(page.locator('.sv-name').filter({ hasText: 'web' }).first()).toBeVisible()
    await expect(page.getByText('Ready').first()).toBeVisible()
    await page.screenshot({ path: path.join(shotDir, 'workspace-sandbox-services.png') })
  } finally {
    if (conversationId) {
      await request.delete(`${API_BASE}/api/v2/conversations/${conversationId}`, { headers })
    }
    if (projectId) {
      await request.delete(
        `${API_BASE}/api/v2/projects/${projectId}?purge_volume=true`, { headers },
      )
    }
  }
})
