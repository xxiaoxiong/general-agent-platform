import { computed, ref } from 'vue'
import type { AsrCapabilities } from '../../api/asr'

const preferredTypes = [
  'audio/webm;codecs=opus',
  'audio/ogg;codecs=opus',
  'audio/mp4;codecs=mp4a.40.2',
  'audio/mp4',
  'audio/webm',
  'audio/ogg',
]

const extensions: Record<string, string> = {
  'audio/webm': 'webm',
  'audio/ogg': 'ogg',
  'application/ogg': 'ogg',
  'audio/mp4': 'm4a',
  'audio/x-m4a': 'm4a',
  'audio/wav': 'wav',
  'audio/wave': 'wav',
  'audio/x-wav': 'wav',
}

type Phase = 'idle' | 'requesting' | 'recording' | 'finalizing' | 'transcribing' | 'done' | 'error'

interface Take {
  abort: AbortController
  stream?: MediaStream
  recorder?: MediaRecorder
  chunks: Blob[]
  tick?: ReturnType<typeof setInterval>
  stopTimer?: ReturnType<typeof setTimeout>
  readyTimer?: ReturnType<typeof setTimeout>
}

interface DictationOptions {
  capabilities: (signal: AbortSignal) => Promise<AsrCapabilities>
  transcribe: (file: File, signal: AbortSignal) => Promise<string>
  insert: (text: string) => void
}

export function appendTranscript(draft: string, transcript: string): string {
  const text = transcript.trim()
  if (!text) return draft
  return draft && !draft.endsWith('\n') ? `${draft}\n${text}` : draft + text
}

function browserError(error: unknown): string {
  switch (error instanceof Error ? error.name : '') {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
    case 'SecurityError':
      return '麦克风权限被拒绝，请在浏览器设置中允许访问后重试。'
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return '未找到麦克风，请检查设备连接。'
    case 'NotReadableError':
    case 'TrackStartError':
      return '暂时无法访问麦克风，请检查设备是否被占用。'
    default:
      return '无法开始录音，请检查麦克风后重试。'
  }
}

function releaseStream(stream?: MediaStream) {
  stream?.getTracks().forEach((track) => track.stop())
}

export function useDictation(options: DictationOptions) {
  const phase = ref<Phase>('idle')
  const error = ref('')
  const elapsed = ref(0)
  const busy = computed(() => ['requesting', 'recording', 'finalizing', 'transcribing'].includes(phase.value))
  let active: Take | undefined

  function dispose(take: Take) {
    active = undefined
    take.abort.abort()
    clearInterval(take.tick)
    clearTimeout(take.stopTimer)
    clearTimeout(take.readyTimer)
    if (take.recorder) {
      take.recorder.ondataavailable = null
      take.recorder.onerror = null
      take.recorder.onstop = null
      try {
        if (take.recorder.state !== 'inactive') take.recorder.stop()
      } catch {
        // 设备意外脱离后仍须释放轨道与音频片段。
      }
      take.recorder = undefined
    }
    releaseStream(take.stream)
    take.stream = undefined
    take.chunks.length = 0
  }

  function cancel() {
    if (active) dispose(active)
    phase.value = 'idle'
    error.value = ''
    elapsed.value = 0
  }

  function fail(message: string) {
    if (active) dispose(active)
    phase.value = 'error'
    error.value = message
  }

  async function recognize(take: Take, recorderType: string) {
    if (active !== take) return
    clearTimeout(take.readyTimer)
    if (!take.chunks.length) {
      fail('没有录到音频，请重新录制。')
      return
    }
    const mime = take.chunks.find((chunk) => chunk.type)?.type || recorderType
    const extension = extensions[mime.split(';', 1)[0].trim().toLowerCase()]
    if (!extension) {
      fail('当前浏览器的录音格式无法转写，请换用新版浏览器。')
      return
    }

    const file = new File(take.chunks, `dictation.${extension}`, { type: mime })
    take.chunks.length = 0
    phase.value = 'transcribing'
    try {
      const transcript = (await options.transcribe(file, take.abort.signal)).trim()
      if (active !== take) return
      if (!transcript) {
        fail('没有识别到可转写的文字，请重新录制。')
        return
      }
      options.insert(transcript)
      dispose(take)
      phase.value = 'done'
    } catch (reason) {
      if (active === take) fail(reason instanceof Error ? reason.message : '语音转写失败，请重新录制。')
    }
  }

  function stop() {
    const take = active
    if (phase.value !== 'recording' || !take?.recorder) return
    phase.value = 'finalizing'
    clearInterval(take.tick)
    clearTimeout(take.stopTimer)
    take.readyTimer = setTimeout(() => {
      if (active === take) fail('无法结束录音，请重新录制。')
    }, 5000)
    try {
      take.recorder.stop()
    } catch {
      fail('无法结束录音，请重新录制。')
    } finally {
      releaseStream(take.stream)
      take.stream = undefined
    }
  }

  async function start() {
    if (active) return
    if (!globalThis.isSecureContext) {
      fail('语音输入需要 HTTPS 或本机 localhost 地址。')
      return
    }
    if (typeof globalThis.MediaRecorder === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      fail('当前浏览器不支持录音，请换用新版浏览器。')
      return
    }

    const take: Take = { abort: new AbortController(), chunks: [] }
    active = take
    phase.value = 'requesting'
    error.value = ''
    elapsed.value = 0
    let limits: AsrCapabilities
    try {
      limits = await options.capabilities(take.abort.signal)
    } catch (reason) {
      if (active === take) fail(reason instanceof Error ? reason.message : '无法连接语音转写服务。')
      return
    }
    if (active !== take) return
    if (!limits?.enabled || !Number.isFinite(limits.max_duration_seconds) || limits.max_duration_seconds <= 0
        || !Number.isSafeInteger(limits.max_file_bytes) || limits.max_file_bytes <= 0) {
      fail('语音转写服务暂时不可用，可以继续输入文字。')
      return
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (active !== take) {
        releaseStream(stream)
        return
      }

      take.stream = stream
      const mimeType = typeof MediaRecorder.isTypeSupported === 'function'
        ? preferredTypes.find((type) => MediaRecorder.isTypeSupported(type))
        : undefined
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      take.recorder = recorder
      let bytes = 0
      recorder.ondataavailable = ({ data }) => {
        if (active !== take || !data.size) return
        bytes += data.size
        if (bytes > limits.max_file_bytes) fail('录音文件过大，请录一段更短的语音。')
        else take.chunks.push(data)
      }
      recorder.onerror = () => { if (active === take) fail('录音已中断，请检查麦克风后重试。') }
      recorder.onstop = () => {
        if (active !== take) return
        if (phase.value === 'finalizing') void recognize(take, recorder.mimeType || mimeType || '')
        else fail('麦克风已断开，请检查设备后重试。')
      }

      recorder.start(250)
      phase.value = 'recording'
      const started = performance.now()
      take.tick = setInterval(() => { elapsed.value = Math.floor((performance.now() - started) / 1000) }, 250)
      // 为浏览器停止事件和容器的编码延迟预留余量。
      take.stopTimer = setTimeout(() => {
        if (active === take) stop()
      }, Math.max(1, limits.max_duration_seconds * 1000 - 500))
    } catch (reason) {
      if (active === take) fail(browserError(reason))
    }
  }

  return { phase, elapsed, error, busy, start, stop, cancel }
}
