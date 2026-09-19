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

/**
 * 网络层失败（IPC 桥本身报错：引擎进程不在、socket 被重置、请求超时）。
 *
 * 由来（用户实测）：引擎未启动或进程中途退出时，渲染层只会收到 Electron 抛出的
 * 英文 `Error invoking remote method 'engine:request': Connection error.`，
 * 既不说明是谁失败，也不提示怎么办。这里统一归一。
 */
function describeTransportError(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err)
  const low = text.toLowerCase()
  if (low.includes('connection error') || low.includes('econnrefused') || low.includes('econnreset')) {
    return (
      '引擎进程连接中断（它可能已退出或正在重启）。'
      + '先等 3-5 秒重试；若一直如此，用菜单里的「重启引擎」或重新启动 Inkforge。'
      + `原始信息：${text}`
    )
  }
  if (low.includes('timed out') || low.includes('timeout')) {
    return (
      '请求超时：引擎或模型服务长时间没有回应。先重试；'
      + `长章节生成本身可能耗时数分钟。原始信息：${text}`
    )
  }
  return text
}

export async function api<T>(method: HttpMethod, path: string, body?: unknown): Promise<T> {
  // 关键：Vue 的 reactive/ref Proxy 无法跨 Electron IPC 结构化克隆
  // （"An object could not be cloned"），JSON 往返剥离所有 Proxy 后再过桥。
  const safeBody =
    body === undefined ? undefined : (JSON.parse(JSON.stringify(body)) as unknown)
  let res: { status: number; data: unknown }
  try {
    res = await window.inkforge.request(method, path, safeBody)
  } catch (err) {
    throw new EngineError(0, describeTransportError(err))
  }
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
