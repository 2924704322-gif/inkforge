/** 引擎 API 调用封装：统一走 window.inkforge 桥，统一错误抛出。 */

export class EngineError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'EngineError'
  }
}

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'DELETE'

export async function api<T>(method: HttpMethod, path: string, body?: unknown): Promise<T> {
  // 关键：Vue 的 reactive/ref Proxy 无法跨 Electron IPC 结构化克隆
  // （"An object could not be cloned"），JSON 往返剥离所有 Proxy 后再过桥。
  const safeBody =
    body === undefined ? undefined : (JSON.parse(JSON.stringify(body)) as unknown)
  const res = await window.inkforge.request(method, path, safeBody)
  if (res.status >= 400) {
    const detail = (res.data as { detail?: unknown } | null)?.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail !== undefined && detail !== null
          ? JSON.stringify(detail)
          : JSON.stringify(res.data)
    throw new EngineError(res.status, message)
  }
  return res.data as T
}

export function withNovel(path: string, novelId: string): string {
  const sep = path.includes('?') ? '&' : '?'
  return `${path}${sep}novel=${encodeURIComponent(novelId)}`
}

export function scoreColor(
  score: number | null | undefined,
): 'success' | 'warning' | 'error' | 'default' {
  if (score === null || score === undefined) return 'default'
  if (score >= 8) return 'success'
  if (score >= 6) return 'warning'
  return 'error'
}
