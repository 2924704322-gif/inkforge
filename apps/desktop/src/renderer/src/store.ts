import { reactive } from 'vue'

import type { EngineState } from '../../shared/bridge'
import { api } from './api'

/** 当前右侧编辑器的选中文档。 */
export type DocSelection =
  | { kind: 'chapter'; chapter: number }
  | { kind: 'settings'; rel: string; title: string }
  | null

export type SideWindowId =
  | 'agents'
  | 'learning'
  | 'materials'
  | 'dashboard'
  | 'export'
  | null

/** 全局轻量状态（DeepWrite 式单窗口：功能导航 / 对话 / 创作空间）。 */
export const appStore = reactive({
  /** 当前打开的书；空 = 工作区模式（墨师全域总控，仍可对话与办事）。 */
  bookId: '',
  bookTitle: '',
  /** 工作区哨兵值（与引擎 src/web/scope.py 的 WORKSPACE 对齐）。 */
  engineState: 'stopped' as EngineState,
  engineMessage: '引擎未启动',
  enginePort: 0,
  dialogs: {
    bookshelf: false,
    modelConfig: false,
    styleForge: false,
  },
  /** 当前打开的功能小窗（左侧导航点击弹出）。 */
  sideWindow: null as SideWindowId,
  /** 右侧编辑器选中的文档。 */
  selection: null as DocSelection,
  /** 当前对话会话（空 = 未加载）。 */
  chatId: '',
  chatAgent: 'chat',
  /** 资源树刷新信号（章节/设定变更后自增）。 */
  treeVersion: 0,
})

/** 工作区哨兵值：无书时用它调用引擎，让墨师在工作台上工作而不是报错。 */
export const WORKSPACE_SCOPE = '__workspace__'

/** 当前请求作用域：有书用书，无书用工作区。 */
export function scopeParam(): string {
  return appStore.bookId || WORKSPACE_SCOPE
}

/** 是否处于工作区（无书）模式。 */
export function inWorkspace(): boolean {
  return !appStore.bookId
}

export function openBook(novelId: string, title = ''): void {
  appStore.bookId = novelId
  appStore.bookTitle = novelId ? (title || novelId) : ''
  appStore.selection = null
  appStore.chatId = ''
  appStore.treeVersion += 1
  // 同步引擎侧「工作台当前书目」（工作区会话据此解析"这本书"、并决定关卡放行目标）。
  // 失败静默：只影响"当前书"标记的即时性，不影响本地 UI。
  void api('PUT', '/api/book-select', { novel_id: novelId }).catch(() => undefined)
}

/** 回到工作区（无书）状态：清空当前书与右侧编辑器选中。 */
export function openWorkspace(): void {
  openBook('')
}
