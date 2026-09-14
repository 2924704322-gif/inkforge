import { reactive } from 'vue'

import type { EngineState } from '../../shared/bridge'

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
  /** 当前打开的书；空 = 未选择（书架对话框自动弹出）。 */
  bookId: '',
  bookTitle: '',
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

export function openBook(novelId: string, title = ''): void {
  appStore.bookId = novelId
  appStore.bookTitle = title || novelId
  appStore.selection = null
  appStore.chatId = ''
  appStore.treeVersion += 1
}
