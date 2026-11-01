import { reactive } from 'vue'

import type { EngineState } from '../../shared/bridge'
import { api, withNovel } from './api'
import type { RevisionPrefill } from './types'

/** 当前右侧编辑器的选中文档。 */
export type DocSelection =
  | { kind: 'chapter'; chapter: number }
  | { kind: 'settings'; rel: string; title: string }
  | null

export type SideWindowId =
  | 'agents'
  | 'learning'
  | 'materials'
  | 'revision'
  | 'dashboard'
  | 'export'
  | null

/** 读布局偏好（localStorage 不可用时返回兜底，不抛）。 */
function readFlag(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return fallback
    return raw === '1' || raw === 'true'
  } catch {
    return fallback
  }
}

/** 写布局偏好（隐私模式 / 存储被禁用时静默，不影响界面）。 */
function writeFlag(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? '1' : '0')
  } catch {
    /* 忽略：偏好丢了只是下次恢复默认 */
  }
}

/** 全局轻量状态（单窗口：功能导航 / 对话 / 创作空间）。 */
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
  /**
   * 主导航是否折叠成图标条（176 → 56px）。
   *
   * 由来：改造前导航恒定 176px，而 10 个入口里高频只有 3 个；1366 笔记本上
   * 这 176px 相当于正文列的 37%。折叠态省下的宽度直接给对话列。
   */
  navCollapsed: readFlag('inkforge.w.navCollapsed', false),
  /** 创作空间是否折叠成 56px 竖条（窄屏下让位给对话/正文）。 */
  treeCollapsed: readFlag('inkforge.w.treeCollapsed', false),
  /**
   * 「正文改写」侧窗的预填载荷：由创作空间 / 人审卡 / 墨师动作写入，侧窗消费后清空。
   *
   * 为什么要走 store 而不是 props：入口分散在四个组件里（编辑器工具栏、章节树行内、
   * 两个平台的审阅卡、墨师动作回执），而侧窗只挂在 SideWindows 一处。
   * 统一用「打开侧窗 + 填载荷」两个动作，避免每个入口各写一套打开逻辑而漂移。
   */
  revisionPrefill: null as RevisionPrefill | null,
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

/**
 * 记住"最近打开过的书"（供工作区顶栏「↩ 回到《X》」使用）。
 *
 * 为什么要单独抽出来：`openBook()` 会重置选中/会话/资源树，而**启动时的书目同步**
 * （`App.vue::syncActiveBook`）只需要记录、不能触发重置，于是它原先直接赋值
 * `appStore.bookId` —— 结果 `lastBookId` 始终为空，工作区顶栏永远不显示"回到某本书"
 * （UI 探针断言直接抓到：按钮在代码里存在，实际从不渲染）。
 */
/** 折叠/展开主导航（176 ⇄ 56px）。 */
export function setNavCollapsed(v: boolean): void {
  appStore.navCollapsed = v
  writeFlag('inkforge.w.navCollapsed', v)
}

/** 折叠/展开创作空间（竖条 56px）。 */
export function setTreeCollapsed(v: boolean): void {
  appStore.treeCollapsed = v
  writeFlag('inkforge.w.treeCollapsed', v)
}

export function rememberBook(novelId: string, title = ''): void {
  if (!novelId) return
  appStore.lastBookId = novelId
  appStore.lastBookTitle = title || novelId
}

export function openBook(novelId: string, title = ''): void {
  rememberBook(novelId, title)
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

/**
 * 打开「正文改写」侧窗并预填 —— 四个入口（创作空间工具栏 / 章节树 / 人审卡 / 墨师动作）共用的唯一通道。
 *
 * 为什么要抽出来：侧窗（520px，`fixed left:184`）会**盖住对话栏**，
 * 而人审卡与墨师动作恰好长在对话栏里 —— 点完那一下必须留下痕迹。
 * 因此约定：入口一律带 `origin`，面板顶部据此显示一条来源面包屑（"来自：第 3 章人审"），
 * 让用户一眼看出"我刚点的事去哪了"，而不是以为点空了。
 */
export function openRevision(prefill: RevisionPrefill): void {
  appStore.revisionPrefill = prefill
  appStore.sideWindow = 'revision'
}

/**
 * 把「创作空间里当前选中的文稿」送进正文改写（正文改写面板 / 自由协作两个入口共用）。
 *
 * 与编辑器工具栏那个入口的区别：那个直接用编辑器里**当前加载的正文**（可能还没保存），
 * 这里按选中态去引擎取一次最新正文。两条路都不落盘，改完去哪仍由用户决定。
 *
 * 对齐闸门照旧：`docAligned` 为假时拒绝（否则会把改动落到用户没在看的文稿上）。
 */
export async function openRevisionFromSelection(
  originPrefix = '创作空间',
): Promise<{ ok: boolean; message: string }> {
  const sel = appStore.selection
  if (!sel || !appStore.bookId) return { ok: false, message: '先在右侧创作空间里点选一篇文稿' }
  if (!appStore.docAligned) {
    return { ok: false, message: '正文还没跟上你选中的文档：请在创作空间里重新点一次目标文稿' }
  }
  try {
    if (sel.kind === 'chapter') {
      const res = await api<{ content: string }>(
        'GET',
        withNovel(`/api/chapters/${sel.chapter}/raw`, appStore.bookId),
      )
      openRevision({
        source: res.content || '',
        origin: `${originPrefix} · 第 ${sel.chapter} 章`,
        fillBack: { chapter: sel.chapter, novelId: appStore.bookId },
      })
    } else {
      const res = await api<{ content?: string }>(
        'GET',
        withNovel(`/api/settings/doc?rel=${encodeURIComponent(sel.rel)}`, appStore.bookId),
      )
      openRevision({
        source: res.content || '',
        origin: `${originPrefix} · ${sel.title}`,
        fillBack: null,
      })
    }
    return { ok: true, message: '已把选中的文稿送进「正文改写」' }
  } catch (err) {
    return { ok: false, message: err instanceof Error ? err.message : String(err) }
  }
}
