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
  /**
   * 右侧正文是否已与选中态对齐（由 EditorPane 维护）。
   *
   * 用途：改稿目标取自 `selection`，而正文是异步拉取的。两者不同步时发改稿指令，
   * 会把改动落到用户没在看的文稿上（曾出现"选的是大纲、动的是第一章"）。
   * 因此发送改稿前必须先确认对齐；未对齐则拦下并提示重新点选。
   */
  docAligned: false,
  /** 已对齐正文的标识，形如 `<bookId>|<ch-3 / settings/outline.md>`。 */
  loadedDocKey: '',
  /** 当前对话会话（空 = 未加载）。 */
  chatId: '',
  chatAgent: 'chat',
  /**
   * 「回到主对话」信号：自增即表示用户主动要一个**全新的空白工作区会话**。
   *
   * 为什么不用 chatId='' 代替：`loadChats()` 会自动恢复最近一个会话，
   * 于是切回工作区时旧上下文又被拉回来（实测体感："初始对话结束后就进了书的会话里"）。
   * 自增信号让 ChatPanel 明确区分"普通切书"与"我要开一个新的主对话"。
   */
  chatResetToken: 0,
  /** 上一次主对话的会话 id：仅用于在历史面板里提示"旧会话还在，可点开"。 */
  lastMainChatId: '',
  /** 最近打开过的书：工作区顶部「回到《X》」用它，避免切走后回不去。 */
  lastBookId: '',
  lastBookTitle: '',
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
  if (novelId) {
    appStore.lastBookId = novelId
    appStore.lastBookTitle = title || novelId
  }
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

/**
 * 回到**主对话**（工作区作用域）并开一个全新空白会话。
 *
 * 语义（用户确认）：
 *   · 切到工作区作用域（不隶属任何书）→ 后续指令不再被某本书的上下文污染；
 *   · 开新会话，不自动恢复旧会话；
 *   · **旧会话不删**：仍在「历史对话」面板里，随时可点开继续。
 */
export function resetToMainChat(): void {
  if (!appStore.bookId && appStore.chatId) {
    appStore.lastMainChatId = appStore.chatId
  }
  openWorkspace()
  appStore.chatResetToken += 1
}
