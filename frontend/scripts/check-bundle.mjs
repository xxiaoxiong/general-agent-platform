import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * 构建产物检查（任务书 §9.8）：
 * 1. 入口主包不得包含 Mermaid / ECharts 引擎代码；
 * 2. 两者必须以独立 chunk 形式存在，保证按需加载；
 * 3. 构建产物中不得出现运行时 CDN 依赖。
 */
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const dist = join(root, 'dist')

let html
try {
  html = readFileSync(join(dist, 'index.html'), 'utf8')
} catch {
  console.error('BUNDLE_FAIL 未找到 dist/index.html，请先执行 npm run build')
  process.exit(1)
}

const entry = html.match(/<script[^>]+src="\/assets\/([^"]+\.js)"/)?.[1]
if (!entry) {
  console.error('BUNDLE_FAIL 未在 dist/index.html 中找到入口脚本')
  process.exit(1)
}

const entryCode = readFileSync(join(dist, 'assets', entry), 'utf8')
const leaked = ['mermaid', 'echarts'].filter(name => entryCode.toLowerCase().includes(name))
if (leaked.length) {
  console.error(`BUNDLE_FAIL 入口主包包含引擎代码：${leaked.join(', ')}`)
  process.exit(1)
}

const assets = readdirSync(join(dist, 'assets'))
const lazyChunks = assets.filter(name => name.startsWith('mermaid.core') || name.startsWith('echarts'))
const missing = ['mermaid.core', 'echarts'].filter(prefix => !assets.some(name => name.startsWith(prefix)))
if (missing.length) {
  console.error(`BUNDLE_FAIL 缺少按需加载分块：${missing.join(', ')}`)
  process.exit(1)
}

const allCode = assets.filter(name => name.endsWith('.js'))
  .map(name => readFileSync(join(dist, 'assets', name), 'utf8')).join('\n')
const cdnPatterns = [/cdn\.jsdelivr\.net/, /unpkg\.com/, /cdnjs\.cloudflare\.com/]
const cdn = cdnPatterns.filter(pattern => pattern.test(allCode))
if (cdn.length) {
  console.error(`BUNDLE_FAIL 构建产物包含运行时 CDN 依赖：${cdn.join(', ')}`)
  process.exit(1)
}

console.log(
  `BUNDLE_OK entry=${entry} entrySize=${(Buffer.byteLength(entryCode) / 1024).toFixed(1)}KB `
  + `lazyChunks=${lazyChunks.length} assets=${assets.length}`,
)
