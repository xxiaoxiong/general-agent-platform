/** Blob 请求的失败响应仍使用服务端 JSON 错误结构。 */
export async function filePreviewError(error: unknown, fallback: string): Promise<string> {
  let data = (error as { response?: { data?: unknown } })?.response?.data
  if (data instanceof Blob && data.type.includes('json') && data.size <= 64 * 1024) {
    try {
      data = JSON.parse(await data.text())
    } catch {
      return fallback
    }
  }
  const detail = (data as { detail?: unknown } | null)?.detail
  return typeof detail === 'string' && detail ? detail : fallback
}
