import assert from 'node:assert/strict'
import test from 'node:test'
import { clipboardFiles, hasScrolledAway, isNearLatest, isSendKey, splitConversationHistory } from '../src/views/workspace/interactions.ts'

test('流式内容增长时继续跟随之前的底部位置', () => {
  const viewport = { scrollHeight: 1600, scrollTop: 800, clientHeight: 400 }
  assert.equal(isNearLatest(viewport), false)
  assert.equal(hasScrolledAway(viewport, 800), false)
})

test('用户在流式渲染前上翻时保留阅读位置', () => {
  const viewport = { scrollHeight: 1600, scrollTop: 600, clientHeight: 400 }
  assert.equal(hasScrolledAway(viewport, 800), true)
})

test('消息折叠使滚动空间缩小时继续跟随底部', () => {
  const viewport = { scrollHeight: 1000, scrollTop: 600, clientHeight: 400 }
  assert.equal(isNearLatest(viewport), true)
  assert.equal(hasScrolledAway(viewport, 800), false)
})

test('Enter 发送，换行和输入法选词按键留给浏览器', () => {
  const enter = { key: 'Enter', shiftKey: false, altKey: false, ctrlKey: false, metaKey: false, isComposing: false, keyCode: 13 }
  assert.equal(isSendKey(enter), true)
  assert.equal(isSendKey({ ...enter, shiftKey: true }), false)
  assert.equal(isSendKey({ ...enter, isComposing: true }), false)
  assert.equal(isSendKey({ ...enter, keyCode: 229 }), false)
})

test('会话历史分组优先展示最新置顶，搜索同时匹配置顶与最近会话', () => {
  const rows = [
    { id: 1, title: 'PHM 每周复盘', is_pinned: false },
    { id: 2, title: 'PHM 交付文档', is_pinned: true, pinned_at: '2026-11-19T11:00:00' },
    { id: 3, title: '研发记录', is_pinned: true, pinned_at: '2026-11-20T11:00:00' },
    { id: 4, title: '', is_pinned: false },
  ]
  let groups = splitConversationHistory(rows, '')
  assert.deepEqual(groups.pinned.map((row) => row.id), [3, 2])
  assert.deepEqual(groups.recent.map((row) => row.id), [1, 4])
  groups = splitConversationHistory(rows, ' phm ')
  assert.deepEqual(groups.pinned.map((row) => row.id), [2])
  assert.deepEqual(groups.recent.map((row) => row.id), [1])
  assert.deepEqual(splitConversationHistory(rows, '新对话').recent.map((row) => row.id), [4])
})

test('从剪贴板保留多份独立文件，并避免从两个入口重复上传', () => {
  const first = new File(['first'], '同名报告.docx')
  const second = new File(['other'], '同名报告.docx')
  const item = { kind: 'file', getAsFile: () => first }
  assert.deepEqual(clipboardFiles({ files: [first, second], items: [item] }), [first, second])
})

test('从剪贴板项目读取截图、补齐图片名称，并将纯文字粘贴留给浏览器', async () => {
  const text = { kind: 'string', getAsFile: () => assert.fail('文字不应被上传') }
  assert.deepEqual(clipboardFiles(null), [])
  assert.deepEqual(clipboardFiles({ files: [], items: [text] }), [])
  const screenshot = new File(['png-bytes'], 'image.png', { type: 'image/png', lastModified: 42 })
  const files = clipboardFiles({
    files: [],
    items: [text, { kind: 'file', getAsFile: () => screenshot }, { kind: 'file', getAsFile: () => null }],
  })
  assert.equal(files.length, 1)
  assert.match(files[0].name, /^粘贴图片-\d+-1\.png$/)
  assert.equal(files[0].type, 'image/png')
  assert.equal(files[0].lastModified, 42)
  assert.equal(await files[0].text(), 'png-bytes')
})
