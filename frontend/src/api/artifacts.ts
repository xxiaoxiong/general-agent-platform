// 产物下载 API（/api/v2/artifacts）
import { createRequest } from '@/utils/request'
import { PROJECT_DOWNLOAD_PATH } from '@/features/run/projectDelivery'

const inlineRequest = createRequest('/api/v2', { inlineErrors: true })

export async function downloadRunArtifact(path: string, filename: string, signal?: AbortSignal, requireZip = false) {
  // 交付事件中的地址只用于本平台产物路由，访问令牌不发往动态外部地址。
  const match = PROJECT_DOWNLOAD_PATH.exec(path)
  if (!match) throw new Error('产物下载地址无效')
  const blob = await inlineRequest.get<unknown, Blob>(`/artifacts/${match[1]}`, { responseType: 'blob', signal })
  if (requireZip) requireZipSignature(blob)
  if (signal?.aborted) return
  saveBlob(blob, filename)
}

/** ZIP 签名校验（防止错误页/异常响应被当包保存） */
export async function requireZipSignature(blob: Blob) {
  const signature = new Uint8Array(await blob.slice(0, 4).arrayBuffer())
  if (signature[0] !== 0x50 || signature[1] !== 0x4b || signature[2] !== 3 || signature[3] !== 4) {
    throw new Error('服务器返回的内容无法识别为 ZIP 文件')
  }
}

/** 触发浏览器保存 Blob */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** 打包下载 Run 已发布的交付文件（对话流"下载全部（ZIP）"） */
export async function downloadRunZip(runId: string, filename: string, signal?: AbortSignal) {
  const blob = await inlineRequest.get<unknown, Blob>(
    `/artifacts/runs/${encodeURIComponent(runId)}/zip`, { responseType: 'blob', signal },
  )
  await requireZipSignature(blob)
  if (signal?.aborted) return
  saveBlob(blob, filename)
}

export const downloadProjectZip = (path: string, filename: string, signal: AbortSignal) =>
  downloadRunArtifact(path, filename, signal, true)

export function artifactDownloadUrl(artifactId: string): string {
  return `/api/v2/artifacts/${artifactId}`
}
