import { isAxiosError } from 'axios'
import { createRequest } from '@/utils/request'

export interface AsrCapabilities {
  enabled: boolean
  max_duration_seconds: number
  max_file_bytes: number
}

const request = createRequest('/api/v1/asr', { inlineErrors: true })

const errors: Record<string, string> = {
  invalid_audio: '无法读取这段录音，请重新录制。',
  file_too_large: '录音文件过大，请录一段更短的语音。',
  unsupported_format: '当前录音格式无法转写，请换用新版浏览器。',
  duration_exceeded: '录音时间超过限制，请分段录制。',
  no_speech: '没有识别到可转写的文字，请重新录制。',
  asr_busy: '语音转写服务正忙，请稍后重试。',
  asr_invalid_response: '语音转写服务返回异常，请稍后重试。',
  asr_unavailable: '语音转写服务暂时不可用，可以继续输入文字。',
  asr_timeout: '语音识别超时，请缩短录音后重试。',
}

function describeRequestError(error: unknown) {
  if (!isAxiosError(error)) return '语音转写失败，请重新录制。'
  if (['ECONNABORTED', 'ETIMEDOUT'].includes(error.code || '')) return errors.asr_timeout
  const detail: unknown = error.response?.data?.detail
  if (detail && typeof detail === 'object' && 'code' in detail && typeof detail.code === 'string'
      && Object.prototype.hasOwnProperty.call(errors, detail.code)) return errors[detail.code]
  switch (error.response?.status) {
    case undefined: return '网络连接异常，请检查连接后重新录制。'
    case 401: return '登录已过期，请重新登录。'
    case 413: return errors.file_too_large
    case 429: return errors.asr_busy
    case 503: return errors.asr_unavailable
    case 504: return errors.asr_timeout
    default: return '语音转写失败，请稍后重试。'
  }
}

export const asrApi = {
  async capabilities(signal: AbortSignal) {
    try {
      return await request.get<unknown, AsrCapabilities>('/capabilities', { signal, timeout: 10_000 })
    } catch (error) {
      throw new Error(describeRequestError(error))
    }
  },
  async transcribe(file: File, signal: AbortSignal) {
    const form = new FormData()
    form.append('file', file)
    try {
      const result = await request.post<unknown, { text: string }>('/transcribe', form, { signal, timeout: 175_000 })
      return typeof result.text === 'string' ? result.text : ''
    } catch (error) {
      throw new Error(describeRequestError(error))
    }
  },
}
