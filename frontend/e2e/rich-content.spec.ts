import { expect, test, type Page } from '@playwright/test'

/** 富内容 E2E：真实浏览器验证 SVG / Canvas 实际生成、交互、降级、安全与响应式 */

function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('console', message => {
    if (message.type() === 'error') errors.push(message.text())
  })
  page.on('pageerror', error => errors.push(error.message))
  return errors
}

async function openHarness(page: Page, query: string) {
  await page.goto(`/e2e/harness.html?${query}`)
  await expect(page.getByTestId('harness')).toBeVisible()
}

test('混合消息：Markdown 表格、普通代码块、Mermaid SVG 与 ECharts Canvas 同时正确渲染', async ({ page }) => {
  const errors = collectConsoleErrors(page)
  await openHarness(page, 'case=mixed')

  await expect(page.locator('.table-scroll table tbody tr')).toHaveCount(3)
  await expect(page.locator('.table-scroll td strong')).toHaveText('180 万')
  await expect(page.locator('.table-scroll td code')).toHaveText('150 万')
  await expect(page.locator('.table-scroll td a[href="https://example.com"]')).toHaveText('+20%')
  await expect(page.locator('.code-block')).toHaveCount(2)
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
  await expect(page.locator('.chart-card canvas')).toBeVisible()
  // 普通 JSON 代码块不得被误判为 chart
  await expect(page.locator('.chart-card')).toHaveCount(1)
  await expect(page.locator('.mermaid-card')).toHaveCount(1)

  // 图表位于正文中间：前后文字顺序不被打乱
  const lead = await page.getByText('执行链路如下：').boundingBox()
  const mermaidCard = await page.locator('.mermaid-card').boundingBox()
  const intro = await page.getByText('收入趋势：').boundingBox()
  const chartCard = await page.locator('.chart-card').boundingBox()
  const tail = await page.getByText('以上为本次经营概览。').boundingBox()
  expect(lead!.y).toBeLessThan(mermaidCard!.y)
  expect(mermaidCard!.y).toBeLessThan(intro!.y)
  expect(intro!.y).toBeLessThan(chartCard!.y)
  expect(chartCard!.y).toBeLessThan(tail!.y)

  const svgBox = await page.locator('.mermaid-canvas svg').boundingBox()
  expect(svgBox?.width || 0).toBeGreaterThan(80)
  const canvasBox = await page.locator('.chart-card canvas').boundingBox()
  expect(canvasBox?.width || 0).toBeGreaterThan(200)

  await page.screenshot({ path: 'test-results/mixed-light.png', fullPage: true })
  expect(errors).toEqual([])
})

test('长 URL、长行内代码与正文竖线：不撑破气泡，竖线不误判为表格', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await openHarness(page, 'case=text-edge')
  await expect(page.locator('.response-markdown a[href^="https://example.com/"]')).toHaveCount(1)
  await expect(page.locator('.table-scroll')).toHaveCount(1)
  await expect(page.locator('.table-scroll tbody tr')).toHaveCount(1)
  const width = await page.evaluate(() => document.documentElement.scrollWidth)
  expect(width).toBeLessThanOrEqual(391)
})

test('Mermaid：图表/源码切换、复制源码、放大查看与关闭', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: 'http://localhost:5199' })
  await openHarness(page, 'case=mixed')
  const card = page.locator('.mermaid-card')
  await expect(card.locator('svg')).toBeVisible()

  await card.getByRole('button', { name: '源码', exact: true }).click()
  await expect(card.locator('.rich-source pre')).toContainText('flowchart TD')
  await expect(card.locator('svg')).toHaveCount(0)

  await card.getByRole('button', { name: '图表', exact: true }).click()
  await expect(card.locator('svg')).toBeVisible()

  await card.getByRole('button', { name: '复制源码' }).click()
  const copied = await page.evaluate(() => navigator.clipboard.readText())
  expect(copied).toContain('flowchart TD')

  await card.getByRole('button', { name: '放大' }).click()
  await expect(page.locator('.rich-modal')).toBeVisible()
  await expect(page.locator('.rich-modal .rich-modal-svg svg')).toBeVisible()
  await page.locator('.rich-modal').getByRole('button', { name: '＋' }).click()
  await expect(page.locator('.rich-zoom')).toHaveText('120%')
  await page.screenshot({ path: 'test-results/mermaid-fullscreen.png' })
  await page.locator('.rich-modal').getByRole('button', { name: '关闭' }).click()
  await expect(page.locator('.rich-modal')).toHaveCount(0)
})

test('ECharts：数据表、源码视图与单位展示', async ({ page }) => {
  await openHarness(page, 'case=mixed')
  const card = page.locator('.chart-card')
  await expect(card.locator('canvas')).toBeVisible()

  await card.getByRole('button', { name: '数据', exact: true }).click()
  await expect(card.locator('.chart-data table')).toBeVisible()
  await expect(card.locator('.chart-data tbody tr')).toHaveCount(4)
  await expect(card.locator('.chart-data')).toContainText('120')
  await expect(card.locator('.chart-data thead')).toContainText('收入（万元）')

  await card.getByRole('button', { name: '源码', exact: true }).click()
  await expect(card.locator('.rich-source pre')).toContainText('"type": "bar"')
  await page.screenshot({ path: 'test-results/chart-source.png' })
})

test('多个 Mermaid 与多个图表共存：实例独立、SVG ID 不冲突', async ({ page }) => {
  await openHarness(page, 'case=multi')
  await expect(page.locator('.mermaid-canvas svg')).toHaveCount(2)
  await expect(page.locator('.chart-card canvas')).toHaveCount(2)

  const ids = await page.locator('.mermaid-canvas svg').evaluateAll(nodes => nodes.map(node => node.id))
  expect(new Set(ids).size).toBe(ids.length)
})

test('历史回放：重新加载页面后图表仍自动渲染', async ({ page }) => {
  await openHarness(page, 'case=mixed')
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
  await page.reload()
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
  await expect(page.locator('.chart-card canvas')).toBeVisible()
})

test('流式阶段显示生成中占位，结束后自动渲染且不残留占位', async ({ page }) => {
  await openHarness(page, 'case=streaming&streaming=1')
  await expect(page.locator('.rich-pending')).toHaveCount(2)
  await expect(page.locator('.rich-pending').first()).toContainText('图表生成中')
  await expect(page.locator('.mermaid-card')).toHaveCount(0)
  await expect(page.locator('.chart-card')).toHaveCount(0)
  await page.screenshot({ path: 'test-results/streaming-pending.png' })

  await page.locator('#finish-streaming').click()
  await expect(page.locator('.rich-pending')).toHaveCount(0)
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
  await expect(page.locator('.chart-card canvas')).toBeVisible()
})

test('逐段流式输出：围栏未完成时只显示生成中，结束后一次渲染', async ({ page }) => {
  await openHarness(page, 'case=incremental&streaming=1')
  for (let step = 0; step < 4; step += 1) {
    await page.locator('#stream-fragment').click()
    if (step === 0) await expect(page.locator('.rich-pending')).toHaveCount(1)
    await expect(page.locator('.mermaid-canvas svg')).toHaveCount(0)
  }
  // 输出未结束时切走（卸载）再切回：不残留实例，恢复后仍显示生成中
  await page.locator('#toggle-mounted').click()
  await expect(page.locator('.rich-pending')).toHaveCount(0)
  await page.locator('#toggle-mounted').click()
  await expect(page.locator('.rich-pending')).toHaveCount(1)
  await expect(page.locator('.rich-pending')).toContainText('图表生成中')
  await expect(page.locator('.code-block')).toHaveCount(0)
  await page.screenshot({ path: 'test-results/streaming-incremental.png' })

  await page.locator('#finish-streaming').click()
  await expect(page.locator('.rich-pending')).toHaveCount(0)
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
})

test('非法与空源码降级：显示错误和源码，后续正文不受影响', async ({ page }) => {
  await openHarness(page, 'case=errors')
  await expect(page.locator('.mermaid-card .rich-state.is-error')).toBeVisible()
  await expect(page.locator('.mermaid-card .rich-state.is-error')).toContainText('图表渲染失败')
  await expect(page.locator('.mermaid-card .rich-source pre')).toContainText('flowchart TD')
  await expect(page.locator('.chart-card .rich-state.is-error')).toBeVisible()
  await expect(page.locator('.chart-card .rich-source pre')).toContainText('"type":"radar"')
  await expect(page.getByText('错误之后的正文仍然正常显示。')).toBeVisible()
  await page.screenshot({ path: 'test-results/errors-fallback.png', fullPage: true })

  await openHarness(page, 'case=empty')
  await expect(page.locator('.mermaid-card .rich-state.is-error')).toBeVisible()
  await expect(page.locator('.chart-card .rich-state.is-error')).toBeVisible()
  await expect(page.getByText('空源码之后的正文仍然显示。')).toBeVisible()
})

test('长标题与长标签：标题省略、标签自动避让，窄屏不溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await openHarness(page, 'case=long-labels')
  const card = page.locator('.chart-card')
  await expect(card.locator('canvas')).toBeVisible()
  const labelBox = await card.locator('.rich-label').boundingBox()
  const cardBox = await card.boundingBox()
  expect(labelBox!.width).toBeLessThanOrEqual(cardBox!.width)
  const width = await page.evaluate(() => document.documentElement.scrollWidth)
  expect(width).toBeLessThanOrEqual(391)
  await page.screenshot({ path: 'test-results/chart-long-labels.png', fullPage: true })
})

test('生成中断留下未闭合围栏：保留源码与错误提示，不白屏', async ({ page }) => {
  await openHarness(page, 'case=truncated')
  await expect(page.locator('.mermaid-card .rich-state.is-error')).toBeVisible()
  await expect(page.locator('.mermaid-card .rich-source pre')).toContainText('flowchart TD')
  await expect(page.getByText('生成中断的内容应保留：')).toBeVisible()
})

test('空数据图表显示空状态，不报错也不绘制空图', async ({ page }) => {
  await openHarness(page, 'case=empty-data')
  const card = page.locator('.chart-card')
  await expect(card.locator('.rich-state.is-error')).toHaveCount(0)
  await expect(card.locator('.chart-empty')).toHaveText('暂无数据')
  await expect(card.locator('canvas')).toHaveCount(0)
  await expect(card.locator('.rich-label')).toContainText('空数据')
  await card.getByRole('button', { name: '数据', exact: true }).click()
  await expect(card.locator('.chart-empty')).toHaveText('暂无数据')
  await card.getByRole('button', { name: '源码', exact: true }).click()
  await expect(card.locator('.rich-source pre')).toContainText('"labels": []')
})

test('超长 Mermaid 源码被限制，显示错误与源码而不白屏', async ({ page }) => {
  await openHarness(page, 'case=oversized')
  await expect(page.locator('.mermaid-card .rich-state.is-error')).toContainText('20000')
  await expect(page.locator('.mermaid-card .rich-source code')).toBeVisible()
})

test('相同 Mermaid 源码重复出现时 SVG ID 不冲突', async ({ page }) => {
  await openHarness(page, 'case=duplicate')
  await expect(page.locator('.mermaid-canvas svg')).toHaveCount(2)
  const ids = await page.locator('.mermaid-canvas svg').evaluateAll(nodes => nodes.map(node => node.id))
  expect(new Set(ids).size).toBe(2)
})

test('列表与引用块中的图表同样渲染', async ({ page }) => {
  await openHarness(page, 'case=nested')
  await expect(page.locator('li .mermaid-canvas svg')).toBeVisible()
  await expect(page.locator('blockquote .chart-card canvas')).toBeVisible()
})

test('四种图表变体渲染：折线 / 面积 / 饼图 / 堆叠柱', async ({ page }) => {
  await openHarness(page, 'case=chart-variants')
  await expect(page.locator('.chart-card canvas')).toHaveCount(4)
  await expect(page.locator('.chart-card .rich-state.is-error')).toHaveCount(0)
  await expect(page.locator('.chart-card').nth(3).locator('.rich-label')).toContainText('堆叠柱')
  await page.screenshot({ path: 'test-results/chart-variants.png', fullPage: true })
})

test('图表配置：多系列图例、坐标轴、单位与 richText Tooltip', async ({ page }) => {
  await openHarness(page, 'case=chart-variants')
  await expect(page.locator('.chart-card canvas')).toHaveCount(4)

  const option = await page.locator('.chart-canvas').nth(3).evaluate(async element => {
    const module = await import('/src/features/run/rich-content/echarts.ts')
    return module.echarts.getInstanceByDom(element as HTMLElement)?.getOption()
  })
  expect(option?.tooltip?.[0]?.trigger).toBe('axis')
  expect(option?.tooltip?.[0]?.renderMode).toBe('richText')
  expect(option?.xAxis?.[0]?.type).toBe('category')
  expect(option?.yAxis?.[0]?.type).toBe('value')
  expect(option?.legend?.[0]?.show).toBe(true)
  const card = page.locator('.chart-card').nth(3)
  await expect(card.locator('.rich-label')).toContainText('堆叠柱')
  await expect(card.locator('.rich-unit')).toHaveText('单')

  // 悬停触发 Tooltip：Canvas 像素发生变化，证明 tooltip 真实绘制
  const canvas = card.locator('canvas')
  await page.waitForTimeout(500)
  const before = await canvas.screenshot()
  const box = await canvas.boundingBox()
  await page.mouse.move(box!.x + box!.width * 0.55, box!.y + box!.height * 0.55)
  await page.waitForTimeout(500)
  const after = await canvas.screenshot()
  expect(Buffer.compare(before, after)).not.toBe(0)
})

test('常用 Mermaid 图种全部可渲染：flowchart/graph/sequence/class/state/er/gantt/mindmap/pie/xychart/timeline', async ({ page }) => {
  await openHarness(page, 'case=diagrams')
  await expect(page.locator('.mermaid-canvas svg')).toHaveCount(11)
  await expect(page.locator('.mermaid-card .rich-state.is-error')).toHaveCount(0)
  const ids = await page.locator('.mermaid-canvas svg').evaluateAll(nodes => nodes.map(node => node.id))
  expect(new Set(ids).size).toBe(11)
  await page.screenshot({ path: 'test-results/mermaid-diagrams.png', fullPage: true })
})

test('离线资源：渲染 Mermaid 与图表不请求任何外部 CDN', async ({ page }) => {
  const external: string[] = []
  page.on('request', request => {
    const url = new URL(request.url())
    if (url.protocol.startsWith('http') && !['localhost', '127.0.0.1'].includes(url.hostname)) external.push(request.url())
  })
  await openHarness(page, 'case=mixed')
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
  await expect(page.locator('.chart-card canvas')).toBeVisible()
  expect(external).toEqual([])
})

test('按需加载：无图表消息不请求引擎，出现图表后才加载 Mermaid / ECharts', async ({ page }) => {
  const requested: string[] = []
  page.on('request', request => requested.push(request.url()))
  const engineLoaded = (pattern: RegExp) => requested.some(
    url => pattern.test(url) && /node_modules\/\.vite\/deps|mermaid\.core|echarts-/i.test(url),
  )

  await openHarness(page, 'case=text-edge')
  await expect(page.locator('.response-markdown')).toBeVisible()
  await page.waitForTimeout(800)
  expect(engineLoaded(/mermaid/i)).toBe(false)
  expect(engineLoaded(/echarts/i)).toBe(false)

  requested.length = 0
  await openHarness(page, 'case=mixed')
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
  await expect(page.locator('.chart-card canvas')).toBeVisible()
  expect(engineLoaded(/mermaid/i)).toBe(true)
  expect(engineLoaded(/echarts/i)).toBe(true)
})

test('资源加载失败降级：引擎 chunk 不可用时显示错误与源码', async ({ page }) => {
  await page.route(/\/\.vite\/deps\/mermaid/, route => route.abort())
  await page.route(/\/\.vite\/deps\/echarts/, route => route.abort())
  await openHarness(page, 'case=mixed')
  await expect(page.locator('.mermaid-card .rich-state.is-error')).toBeVisible()
  await expect(page.locator('.chart-card .rich-state.is-error')).toBeVisible()
  await expect(page.locator('.mermaid-card .rich-source pre')).toContainText('flowchart TD')
  await expect(page.locator('.chart-card .rich-source pre')).toContainText('"type": "bar"')
  await expect(page.getByText('以上为本次经营概览。')).toBeVisible()
})

test('生命周期：消息更新不重复创建实例，卸载后 ECharts 实例销毁并可重新挂载', async ({ page }) => {
  await openHarness(page, 'case=mixed')
  await expect(page.locator('.chart-card canvas')).toHaveCount(1)

  await page.locator('#update-message').click()
  await expect(page.getByText('更新段落 1')).toBeVisible()
  await expect(page.locator('.chart-card canvas')).toHaveCount(1)
  await expect(page.locator('.mermaid-canvas svg')).toHaveCount(1)

  await page.evaluate(() => {
    (window as unknown as { __chartHost?: Element }).__chartHost = document.querySelector('.chart-canvas') || undefined
  })
  await page.locator('#toggle-mounted').click()
  await expect(page.locator('.chart-canvas')).toHaveCount(0)

  const disposed = await page.evaluate(async () => {
    const module = await import('/src/features/run/rich-content/echarts.ts')
    const host = (window as unknown as { __chartHost?: HTMLElement }).__chartHost
    return !host || module.echarts.getInstanceByDom(host) == null
  })
  expect(disposed).toBe(true)

  await page.locator('#toggle-mounted').click()
  await expect(page.locator('.chart-card canvas')).toHaveCount(1)
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
})

test('重新生成/替换回答：旧图表实例销毁，新内容按新图表重建', async ({ page }) => {
  await openHarness(page, 'case=mixed')
  await expect(page.locator('.chart-card canvas')).toHaveCount(1)
  await page.evaluate(() => {
    (window as unknown as { __oldChartHost?: Element }).__oldChartHost = document.querySelector('.chart-canvas') || undefined
  })

  await page.locator('#replace-message').click()
  await expect(page.locator('.mermaid-canvas svg')).toHaveCount(2)
  await expect(page.locator('.chart-card canvas')).toHaveCount(2)

  const disposed = await page.evaluate(async () => {
    const module = await import('/src/features/run/rich-content/echarts.ts')
    const host = (window as unknown as { __oldChartHost?: HTMLElement }).__oldChartHost
    return !host || module.echarts.getInstanceByDom(host) == null
  })
  expect(disposed).toBe(true)
})

test('深色主题：图表随主题切换重新渲染，CSS 变量生效', async ({ page }) => {
  await openHarness(page, 'case=mixed&theme=dark')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
  await expect(page.locator('.chart-card canvas')).toBeVisible()
  const darkBg = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--ui-bg').trim())
  await page.screenshot({ path: 'test-results/mixed-dark.png', fullPage: true })

  await page.locator('#toggle-theme').click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
  await expect(page.locator('.chart-card canvas')).toHaveCount(1)
  const lightBg = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--ui-bg').trim())
  expect(lightBg).not.toBe(darkBg)
})

test('手机窄屏 390px：页面不横向溢出，宽表格在自身区域滚动', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await openHarness(page, 'case=mixed')
  await expect(page.locator('.mermaid-canvas svg')).toBeVisible()
  await expect(page.locator('.chart-card canvas')).toBeVisible()

  const layout = await page.evaluate(() => {
    const table = document.querySelector<HTMLElement>('.table-scroll')
    return {
      docWidth: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
      tableOverflow: table ? getComputedStyle(table).overflowX : '',
    }
  })
  expect(layout.docWidth).toBeLessThanOrEqual(layout.viewport + 1)
  expect(layout.tableOverflow).toBe('auto')
  await page.screenshot({ path: 'test-results/mixed-mobile.png', fullPage: true })
})

test('连续调整窗口宽度：1024 / 768 / 390 均不溢出，图表 Canvas 随容器重绘', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await openHarness(page, 'case=mixed')
  await expect(page.locator('.chart-card canvas')).toBeVisible()
  const wideCanvas = await page.locator('.chart-card canvas').evaluate(element => (element as HTMLCanvasElement).width)

  for (const width of [1024, 768, 390]) {
    await page.setViewportSize({ width, height: 900 })
    await expect.poll(async () => page.evaluate(() => document.documentElement.scrollWidth))
      .toBeLessThanOrEqual(width + 1)
    if (width <= 768) {
      await expect.poll(async () => page.locator('.chart-card canvas').evaluate(element => (element as HTMLCanvasElement).width))
        .toBeLessThan(wideCanvas)
    }
  }
})

test('800 行超大表格：全部行保留、限高滚动，渲染后页面仍可交互', async ({ page }) => {
  const started = Date.now()
  await openHarness(page, 'case=huge-table')
  const table = page.locator('.table-scroll--large')
  await expect(table).toBeVisible()
  await expect(table.locator('tbody tr')).toHaveCount(800)
  expect(Date.now() - started).toBeLessThan(15_000)
  await page.locator('#toggle-theme').click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
})

test('120 行大表格：行不丢失、限高滚动、渲染不阻塞', async ({ page }) => {
  await openHarness(page, 'case=large-table')
  const table = page.locator('.table-scroll--large')
  await expect(table).toBeVisible()
  await expect(table.locator('tbody tr')).toHaveCount(120)
  const metrics = await table.evaluate(element => ({
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
  }))
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight)
})

test('安全：模型生成的 HTML / 脚本 / javascript 链接不执行', async ({ page }) => {
  await openHarness(page, 'case=security')
  await expect(page.locator('.chart-card canvas')).toBeVisible()
  await page.waitForTimeout(400)

  const result = await page.evaluate(() => ({
    xss: (window as unknown as { __xss?: number }).__xss,
    scriptCount: document.querySelectorAll('.response-markdown script').length,
    jsHrefCount: document.querySelectorAll('.response-markdown a[href^="javascript"]').length,
    eventAttrCount: document.querySelectorAll('.response-markdown [onerror], .response-markdown [onclick]').length,
    chartImages: document.querySelectorAll('.chart-card img').length,
    foreignObjects: document.querySelectorAll('.response-markdown foreignObject').length,
  }))
  expect(result.xss).toBeUndefined()
  expect(result.scriptCount).toBe(0)
  expect(result.jsHrefCount).toBe(0)
  expect(result.eventAttrCount).toBe(0)
  expect(result.chartImages).toBe(0)
  expect(result.foreignObjects).toBe(0)
})
