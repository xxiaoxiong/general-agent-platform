import DOMPurify from 'dompurify'

/** Mermaid 只在首次出现图表时下载，不进入初始主包（任务书 §7.3） */
let loader: Promise<(typeof import('mermaid'))['default']> | null = null
let renderSeq = 0

const MAX_SOURCE_LENGTH = 20_000
const RENDER_TIMEOUT_MS = 10_000

async function loadMermaid() {
  if (!loader) loader = import('mermaid').then(module => module.default)
  return loader
}

/**
 * 渲染 Mermaid 源码为经过净化的 SVG 字符串。
 * 安全：securityLevel=strict（禁用 click / 内嵌 HTML 交互），产物再过 DOMPurify。
 */
export async function renderMermaidSvg(code: string, theme: 'light' | 'dark'): Promise<string> {
  const source = code.trim()
  if (!source) throw new Error('图表源码为空')
  if (source.length > MAX_SOURCE_LENGTH) throw new Error(`图表源码超过 ${MAX_SOURCE_LENGTH} 字符上限`)

  const mermaid = await loadMermaid()
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    suppressErrorRendering: true,
    theme: theme === 'dark' ? 'dark' : 'default',
    fontFamily: 'inherit',
    maxTextSize: MAX_SOURCE_LENGTH,
    maxEdges: 200,
    // 顶层 htmlLabels=false：节点文字使用 SVG text（foreignObject 会被安全清理移除）
    htmlLabels: false,
  })
  const id = `mermaid-render-${++renderSeq}`
  let timer: ReturnType<typeof setTimeout> | undefined
  let output: { svg: string }
  try {
    output = await Promise.race([
      mermaid.render(id, source),
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`图表渲染超时（>${RENDER_TIMEOUT_MS / 1000} 秒）`)),
          RENDER_TIMEOUT_MS,
        )
      }),
    ])
  } catch (error) {
    document.getElementById(id)?.remove()
    throw error
  } finally {
    if (timer) clearTimeout(timer)
  }
  return DOMPurify.sanitize(output.svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ['script', 'foreignObject'],
  })
}
