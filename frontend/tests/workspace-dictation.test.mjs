import assert from 'node:assert/strict'
import test from 'node:test'
import { appendTranscript, useDictation } from '../src/views/workspace/dictation.ts'

const limits = { enabled: true, max_duration_seconds: 60, max_file_bytes: 1024 }
const settle = () => new Promise((resolve) => setImmediate(resolve))

function installRecorder(t, getUserMedia, output = 'audio/mp4;codecs=mp4a.40.2') {
  let recorder
  class Recorder {
    state = 'inactive'
    static isTypeSupported(type) { return type === 'audio/ogg;codecs=opus' }
    constructor(_stream, options) {
      this.mimeType = options?.mimeType || ''
      recorder = this
    }
    start() { this.state = 'recording' }
    stop() {
      this.state = 'inactive'
      queueMicrotask(() => {
        this.ondataavailable?.({ data: new Blob(['语音'], { type: output }) })
        this.onstop?.()
      })
    }
  }

  const overrides = {
    isSecureContext: true,
    navigator: { mediaDevices: { getUserMedia } },
    MediaRecorder: Recorder,
  }
  for (const [key, value] of Object.entries(overrides)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, key)
    Object.defineProperty(globalThis, key, { value, configurable: true })
    t.after(() => {
      if (previous) Object.defineProperty(globalThis, key, previous)
      else delete globalThis[key]
    })
  }
  return () => recorder
}

function scenario(t, settings = {}) {
  let released = 0
  const stream = { getTracks: () => [{ stop: () => { released += 1 } }] }
  const recorder = installRecorder(t, settings.getUserMedia || (async () => stream), settings.output)
  const results = []
  const uploads = []
  const dictation = useDictation({
    capabilities: async () => settings.limits || limits,
    transcribe: async (file, signal) => {
      uploads.push({ file, signal })
      assert.equal(released, 1)
      return settings.transcribe ? settings.transcribe(signal) : ' 新文字 '
    },
    insert: (transcript) => results.push(transcript),
  })
  t.after(dictation.cancel)
  return { dictation, stream, recorder, results, uploads, released: () => released }
}

test('转写追加到当前草稿并保留原始文字', () => {
  assert.equal(appendTranscript('已有草稿', ' 你好 '), '已有草稿\n你好')
  assert.equal(appendTranscript('已有草稿\n', '你好'), '已有草稿\n你好')
  assert.equal(appendTranscript('', '你好'), '你好')
  assert.equal(appendTranscript('已有草稿', '  '), '已有草稿')
})

test('停止时释放麦克风并以真实输出的格式上传', async (t) => {
  const { dictation, recorder, results, uploads, released } = scenario(t)
  await dictation.start()
  assert.equal(dictation.phase.value, 'recording')
  assert.equal(recorder().mimeType, 'audio/ogg;codecs=opus')
  dictation.stop()
  assert.equal(released(), 1)
  await settle()
  assert.equal(uploads[0].file.type, 'audio/mp4;codecs=mp4a.40.2')
  assert.equal(uploads[0].file.name, 'dictation.m4a')
  assert.deepEqual(results, ['新文字'])
  assert.equal(dictation.phase.value, 'done')
})

test('申请权限期间取消会立即释放迟到的麦克风轨道', async (t) => {
  let allow
  const { dictation, stream, uploads, released } = scenario(t, {
    getUserMedia: () => new Promise((resolve) => { allow = resolve }),
  })
  const pending = dictation.start()
  await settle()
  assert.equal(dictation.phase.value, 'requesting')
  dictation.cancel()
  allow(stream)
  await pending
  assert.equal(released(), 1)
  assert.equal(uploads.length, 0)
  assert.equal(dictation.phase.value, 'idle')
})

test('取消录音会释放轨道并丢弃音频', async (t) => {
  const { dictation, uploads, results, released } = scenario(t)
  await dictation.start()
  dictation.cancel()
  await settle()
  assert.equal(released(), 1)
  assert.deepEqual(uploads, [])
  assert.deepEqual(results, [])
})

test('识别期间取消会中止请求并忽略迟到的结果', async (t) => {
  let complete
  const { dictation, results, uploads } = scenario(t, {
    transcribe: () => new Promise((resolve) => { complete = resolve }),
  })
  await dictation.start()
  dictation.stop()
  await settle()
  assert.equal(dictation.phase.value, 'transcribing')
  dictation.cancel()
  assert.equal(uploads[0].signal.aborted, true)
  complete('不得插入的结果')
  await settle()
  assert.deepEqual(results, [])
  assert.equal(dictation.phase.value, 'idle')
})

test('录音超过文件大小限制时释放麦克风且不上传', async (t) => {
  const { dictation, recorder, uploads, released } = scenario(t, { limits: { ...limits, max_file_bytes: 2 } })
  await dictation.start()
  recorder().ondataavailable({ data: new Blob(['太大'], { type: 'audio/webm' }) })
  await settle()
  assert.equal(released(), 1)
  assert.equal(uploads.length, 0)
  assert.equal(dictation.phase.value, 'error')
  assert.match(dictation.error.value, /录音文件过大/)
})

test('达到服务器录音时长上限前自动结束采集', async (t) => {
  let complete
  const uploaded = new Promise((resolve) => { complete = resolve })
  const { dictation, results, released } = scenario(t, {
    limits: { ...limits, max_duration_seconds: 0.2 },
    transcribe: () => { complete(); return '自动结束' },
  })
  await dictation.start()
  await uploaded
  await settle()
  assert.equal(released(), 1)
  assert.deepEqual(results, ['自动结束'])
})

test('麦克风拒绝和空识别结果提供可恢复状态', async (t) => {
  let requests = 0
  const stream = { getTracks: () => [] }
  installRecorder(t, async () => {
    if (++requests === 1) throw new DOMException('拒绝授权', 'NotAllowedError')
    return stream
  })
  const dictation = useDictation({
    capabilities: async () => limits,
    transcribe: async () => '\n',
    insert: () => assert.fail('空识别结果不应写入草稿'),
  })
  t.after(dictation.cancel)
  await dictation.start()
  assert.match(dictation.error.value, /麦克风权限被拒绝/)
  await dictation.start()
  dictation.stop()
  await settle()
  assert.equal(dictation.phase.value, 'error')
  assert.match(dictation.error.value, /没有识别到/)
})

test('服务停用时提示普通文本输入且不打开麦克风', async (t) => {
  let opened = false
  const { dictation } = scenario(t, {
    limits: { ...limits, enabled: false },
    getUserMedia: async () => { opened = true },
  })
  await dictation.start()
  assert.equal(opened, false)
  assert.equal(dictation.phase.value, 'error')
  assert.match(dictation.error.value, /可以继续输入文字/)
})
