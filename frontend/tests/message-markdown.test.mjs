import assert from 'node:assert/strict'
import test from 'node:test'
import { renderMessageMarkdown } from '../src/features/run/messageMarkdown.ts'

test('历史消息里的不可见占位行不生成空段落或换行', () => {
  const history = '让我先修复 TypeScript 错误，然后启动服务：\n\u2063\n\n\u2063\n\n\u2063\n\n\u2063\n\n\n\n\n\u2063\n\n\u2063\n\n'
    + '现在重写干净的 Vue 前端文件：\n\n现在修复后端部分：\n\n\u2063\n\n\u2063\n\n现在尝试构建和运行项目：\n\n\u2063\n\n'
  assert.equal(renderMessageMarkdown(history), [
    '<p>让我先修复 TypeScript 错误，然后启动服务：</p>',
    '<p>现在重写干净的 Vue 前端文件：</p>',
    '<p>现在修复后端部分：</p>',
    '<p>现在尝试构建和运行项目：</p>',
    '',
  ].join('\n'))
  assert.equal(renderMessageMarkdown('\u2063\u200b\n\n\u2063'), '')
})

test('空行清理保留正常分行、内联标记和代码原文', () => {
  const rendered = renderMessageMarkdown('上一行\n\u2063\n下一行含有 `\u2063` 和 👩‍💻\n\n```txt\n\u2063\n原文\n```')
  assert.match(rendered, /<p>上一行<br>下一行含有 <code>\u2063<\/code> 和 👩‍💻<\/p>/u)
  assert.match(rendered, /<pre><code>\u2063\n原文<\/code><\/pre>/u)
})
