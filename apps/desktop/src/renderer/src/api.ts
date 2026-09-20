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

/**
 * 字数口径（非对称）：**下浮 500 字是硬线，上浮 2000 字以内算合格**。
 *
 * 与引擎 `configs/base.yaml → generation.word_count_floor_offset / ceiling_offset` 同源；
 * 只要引擎在 payload 里带上 `length_floor` / `length_ceiling`（章节审阅与关卡都会带），
 * 就以引擎给的数为准，这里的常量只是断线兜底，避免两边各显示一套"合格线"。
 */
export const LENGTH_FLOOR_OFFSET = 500
export const LENGTH_CEILING_OFFSET = 2000

export interface LengthBounds {
  /** 目标字数（原样回带，便于直接展示） */
  target: number
  /** 最低可接受字数（低于它 = 不合格，会被门禁打回） */
  floor: number
  /** 最高可接受字数（内容完整性优先，超出才算灌水） */
  ceiling: number
  /** 实际字数 */
  actual: number
  /** 与目标字数的差值（正数=超出） */
  deviation: number
  /** 是否在可接受区间内 */
  ok: boolean
}

/**
 * 由「目标字数 + 可选实际字数」算出可接受区间。
 *
 * `floor`/`ceiling` 来自引擎 payload 时优先使用（单一事实源）；缺省时按上面的偏移量折算。
 */
export function lengthBounds(
  target: number | null | undefined,
  actual?: number | null,
  floor?: number | null,
  ceiling?: number | null,
): LengthBounds | null {
  if (typeof target !== 'number' || target <= 0) return null
  const lo = typeof floor === 'number' && floor > 0 ? floor : Math.max(1, target - LENGTH_FLOOR_OFFSET)
  const hi = typeof ceiling === 'number' && ceiling > 0 ? ceiling : target + LENGTH_CEILING_OFFSET
  const act = typeof actual === 'number' ? actual : 0
  return {
    target,
    floor: lo,
    ceiling: hi,
    actual: act,
    deviation: act - target,
    ok: act >= lo && act <= hi,
  }
}
