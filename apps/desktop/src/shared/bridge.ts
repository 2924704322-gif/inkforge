/**
 * Inkforge 桥接契约：Preload 暴露给 Renderer 的最小语义 API。
 *
 * 设计原则（蒸馏自 DeepWrite）：
 * - Renderer 不持有 Node / 文件系统 / 引擎进程；一切经 window.inkforge 语义 API。
 * - Main 是信任汇聚点：校验路径前缀、方法白名单、系统文件选择器授权。
 * - 引擎（Python sidecar）只通过本地 HTTP 访问，Renderer 永不直连。
 */

export type EngineState = 'stopped' | 'starting' | 'ready' | 'crashed'

export interface EngineStateEvent {
  state: EngineState
  port: number
  message: string
}

export interface EngineResponse {
  status: number
  data: unknown
}

export interface AppInfo {
  versions: Record<string, string>
  engineDir: string
  engineDataDir: string
  python: string
  /** 引擎实际监听端口（由内核分配，S1-4 起不再是固定 8137）。 */
  enginePort: number
}

export interface BridgeApi {
  /** 引擎 API 代理：仅允许 /api/ 前缀，方法白名单。 */
  request(method: string, path: string, body?: unknown): Promise<EngineResponse>
  engineState(): Promise<EngineStateEvent>
  restartEngine(): Promise<EngineStateEvent>
  engineLogs(): Promise<string[]>
  appInfo(): Promise<AppInfo>
  /** 系统文件选择器（Main 授权）：选择用于蒸馏的书籍文件（TXT/EPUB），返回绝对路径。 */
  pickBookFile(): Promise<string | null>
  /** 选择保存位置并导出技能包 ZIP。 */
  exportSkillZip(skillId: string): Promise<{ ok: boolean; message: string }>
  /** 选择保存位置并导出当前书目的文稿（txt/epub）；includeDraft 时包含草稿章。 */
  exportManuscript(
    novelId: string,
    format: 'txt' | 'epub',
    includeDraft?: boolean,
  ): Promise<{ ok: boolean; message: string }>
  /** 订阅引擎状态事件，返回取消订阅函数。 */
  onEngineState(cb: (event: EngineStateEvent) => void): () => void
}
