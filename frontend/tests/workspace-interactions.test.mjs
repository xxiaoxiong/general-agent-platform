import assert from 'node:assert/strict'
import test from 'node:test'
import { hasScrolledAway, isNearLatest, isSendKey } from '../src/views/workspace/interactions.ts'

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
