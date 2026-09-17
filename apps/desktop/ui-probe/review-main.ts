/**
 * Inkforge UI 探针 · 人审关卡布局
 *
 * 目的：把「流水线产出章节 → 人审环节」这一屏的真实渲染路径（App.vue 整个外壳）
 * 挂进真实 Chromium，用几何量测把「审阅意见框把正文挤扁」这类观感变成数字。
 *
 * 做法：挂载**真实的** App.vue；只把 Electron 的 window.inkforge 桥换成内存桩，
 * 让 /api/status 返回一个 chapter_review 待审快照。渲染路径与线上完全一致。
 * 使用的正文是**探针自造的填充文本**，不读取任何真实作品数据。
 */
import { createApp } from 'vue'

import App from '../src/renderer/src/App.vue'
import { appStore, rememberBook } from '../src/renderer/src/store'
import '../src/renderer/src/styles.css'

/** 自造填充段落（探针用，与任何真实作品无关） */
const PARA =
  '　　风从垛口的缝里钻进来，带着细沙和铁锈的味道。他数着自己的呼吸，一，二，三，' +
  '直到远处的钟声把夜色敲出一道裂缝。守夜的人换了岗，火光在墙根挪了一小步，' +
  '影子跟着挪了一小步，谁也没有说话。'

/** 一整章的体量（约 30 段，足以把审阅卡撑高） */
const DRAFT_TEXT = Array.from({ length: 30 }, () => PARA).join('\n\n')

const PREVIOUS_DRAFT = DRAFT_TEXT.slice(0, Math.floor(DRAFT_TEXT.length * 0.6))

/** 审阅类型由 URL hash 切换（默认章节审阅）：
 *  #chapter 章节审阅 | #outline 大纲审阅
 *  #proposal 章节审阅 + 待审改稿提案 | #outline-proposal 大纲审阅 + 待审改稿提案
 */
const MODE = window.location.hash.replace('#', '') || 'chapter'
const WITH_PROPOSAL = MODE.includes('proposal')
const IS_INTERACTIVE = MODE.includes('interactive')
const IS_OUTLINE = MODE.includes('outline')

const OUTLINE_PENDING = {
  type: 'outline_review',
  outline: {
    book_title: '探针样板书',
    theme: '一个关于守夜与失约的故事',
    volumes: [
      { volume: 1, title: '第一夜', chapters: [{ chapter: 1, title: '城墙之下', outline: '开场', characters: [] }] },
      { volume: 2, title: '第二夜', chapters: [{ chapter: 2, title: '钟声裂缝', outline: '转折', characters: [] }] },
    ],
    foreshadowing: [{ id: 'f1', desc: '钟声的次数', planted_ch: 1, resolve_ch: 20 }],
  },
}

const STATUS = {
  novel_id: 'probe-review',
  started: true,
  done: false,
  error: null,
  parallel: false,
  last_wave: null,
  review_history: [{ chapter: 1, attempt: 1, overall: 7.5 }],
  metrics: { total_chapters: 12, approved: 1, first_pass_rate: 50, avg_score: 7.8 },
  pending: IS_OUTLINE ? OUTLINE_PENDING : {
    type: 'chapter_review',
    chapter: 2,
    volume: 1,
    attempt: 2,
    model: 'deepseek-chat',
    used_fallback: false,
    retry_exceeded: false,
    draft_text: DRAFT_TEXT,
    previous_draft: PREVIOUS_DRAFT,
    previous_attempt: 1,
    review: {
      consistency: 7,
      plot: 8,
      continuity: 6,
      prose: 7.5,
      length: 2600,
      comment: '整体节奏尚可，但结尾的钩子偏弱，且第三段与人物状态略有冲突。',
      issues: [
        {
          dimension: 'continuity',
          severity: 'major',
          description: '主角在上一章末已离开城南，本章却出现在城南城墙上。',
          quote: '他数着自己的呼吸',
          suggestion: '把场景移到城北驿道，或补一句回城的过渡。',
        },
        {
          dimension: 'prose',
          severity: 'minor',
          description: '连续三段用「挪了一小步」的句式，显得重复。',
          quote: '影子跟着挪了一小步',
          suggestion: '换用不同的动作描写。',
        },
        {
          dimension: 'plot',
          severity: 'minor',
          description: '本章没有推进主线目标，读者可能失去耐心。',
          quote: '直到远处的钟声把夜色敲出一道裂缝',
          suggestion: '让钟声带来一条新的外部压力。',
        },
      ],
    },
  },
}

const BOOKS = {
  books: [
    {
      novel_id: 'probe-review',
      title: '探针样板书',
      chapters: 2,
      approved: 1,
      active: true,
      finished: false,
      interactive: IS_INTERACTIVE,
      is_default: true,
    },
  ],
}

const CHAPTERS = {
  chapters: [
    {
      chapter: 1,
      volume: 1,
      title: '城墙之下',
      status: 'approved',
      score: 8,
      first_review_passed: true,
      model: 'deepseek-chat',
      used_fallback: false,
    },
    {
      chapter: 2,
      volume: 1,
      title: '钟声裂缝',
      status: 'draft',
      score: 7.5,
      first_review_passed: false,
      model: 'deepseek-chat',
      used_fallback: false,
    },
  ],
}

const calls: string[] = []
/** 正文 raw 被取了几次：用于验证「外部改写后编辑器会重取正文」 */
let rawHits = 0

const NOW = Math.floor(Date.now() / 1000)

const CHAT_SUMMARY = {
  chats: [
    { id: 'probe-chat', agent: 'master', agent_label: '主智能体 · 墨师', title: '大纲改稿', updated: NOW, scope: 'book', novel: 'probe-review' },
  ],
}

/** 「回到主对话」会 POST /api/chats 建一个空白工作区会话——桩要能记下来。 */
const workspaceChats: Array<Record<string, unknown>> = []
let chatSeq = 0

const CHAT_DATA = {
  id: 'probe-chat',
  agent: 'master',
  title: '大纲改稿',
  created: NOW - 600,
  // 关键：提案是在第 2 条消息之后创建的，之后用户又继续聊了两轮。
  // 提案卡必须插在第 2 条之后，而不是永远堆在最后。
  messages: [
    { role: 'user', content: '把第二卷的冲突提前到第一卷末，并补一条伏笔。', ts: NOW - 300 },
    { role: 'assistant', content: '收到。我按你的要求产出了一份改稿提案，请在下方卡片审阅。', ts: NOW - 290 },
    { role: 'user', content: '另外把主角的名字统一一下。', ts: NOW - 120 },
    { role: 'assistant', content: '好的，已记下，等你先处理完上面那张提案卡。', ts: NOW - 110 },
  ],
}

const PROPOSAL_CREATED = NOW - 250

const PROPOSALS = {
  proposals: [
    {
      id: 'p1',
      title: 'outline · 改稿提案',
      summary: '把第二卷的核心冲突前移，并在第一卷末埋入钟声伏笔。',
      target: { kind: 'settings', key: 'settings/outline.md' },
      // 引擎侧新增的"目标可核对证据"（2026-09-17）：卡片必须显示改的是哪份文稿。
      // 回归依据：曾出现"选的是大纲、动的是第一章"且界面无从发现。
      targetPath: 'settings/outline.md',
      targetTitle: '大纲',
      targetChars: 1180,
      status: 'pending',
      statusMessage: '',
      additions: 6,
      deletions: 2,
      created: PROPOSAL_CREATED,
      hunks: [
        {
          oldStart: 4,
          oldLines: 3,
          newStart: 4,
          newLines: 5,
          lines: [
            { type: 'context', old: 4, new: 4, text: '## 第一卷 第一夜' },
            { type: 'deletion', old: 5, new: '', text: '- 主线冲突在第二卷展开' },
            { type: 'addition', old: '', new: 5, text: '+ 主线冲突在第一卷末点燃' },
            { type: 'addition', old: '', new: 6, text: '+ 埋入「钟声次数」伏笔' },
          ],
        },
      ],
      truncated: false,
      proposed: '## 第一卷 第一夜\n- 主线冲突在第一卷末点燃\n- 埋入「钟声次数」伏笔\n\n## 第二卷 第二夜\n- 次日清晨，冲突余波未平\n',
      // 审校主编评审（引擎在生成提案时一并产出）
      review: {
        consistency: 7.5,
        plot: 8,
        continuity: 6.5,
        prose: 7,
        length: 2600,
        comment: '冲突前移后的衔接略生硬，钟声伏笔的埋法可以更自然。',
        issues: [
          {
            dimension: 'continuity',
            severity: 'major',
            description: '第一卷末的冲突与第二卷开篇的时间线重叠。',
            quote: '第一卷末点燃',
            suggestion: '把第二卷开篇改为次日清晨。',
          },
          {
            dimension: 'prose',
            severity: 'minor',
            description: '伏笔的提示过于直白。',
            quote: '埋入「钟声次数」伏笔',
            suggestion: '改为由人物对话自然带出。',
          },
        ],
      },
    },
  ],
}

function route(method: string, path: string): { status: number; data: unknown } {
  calls.push(`${method} ${path}`)
  const p = path.split('?')[0] ?? path
  const query = path.includes('?') ? path.slice(path.indexOf('?')) : ''
  if (p === '/api/status') return { status: 200, data: STATUS }
  if (p === '/api/books') return { status: 200, data: BOOKS }
  if (p === '/api/chapters') return { status: 200, data: CHAPTERS }
  if (p === '/api/chapters/1/raw') {
    // 每次取都换文案：用于验证 treeVersion 变化后编辑器确实重取了正文
    rawHits += 1
    return {
      status: 200,
      data: {
        chapter: 1,
        title: '城墙之下',
        status: rawHits > 1 ? 'draft' : 'approved',
        content: rawHits > 1 ? `${PARA}\n\n（本节已按改稿提案更新）` : PARA,
      },
    }
  }
  if (p === '/api/settings/tree') return { status: 200, data: { items: [] } }
  // 设定文档一律 404：既模拟"文件不存在"，也用来触发 EditorPane 的
  // 「选中态与已加载正文不同步」告警（加载失败 → loadedFrom 归空 → 不一致）。
  if (p === '/api/settings/doc') {
    return { status: 404, data: { detail: '文档不存在：探针未提供该设定文档' } }
  }
  if (p === '/api/interactive/state') {
    return {
      status: 200,
      data: {
        status: 'awaiting_choice',
        chapter: 1,
        approved_count: 0,
        error: null,
        cards: [
          { card_id: 'c1', title: '方向一 · 旧账现形', tag: '线索', outline: '寡妇在旧账里发现一笔不该存在的支出，牵出二十年前的旧案。', hook: '账本上多出的名字是谁？', characters: ['林素娘'] },
          { card_id: 'c2', title: '方向二 · 茶客登门', tag: '对峙', outline: '一个自称故人的茶客上门，带来与旧案相反的证词。', hook: '他为什么要替死人说话？', characters: ['林素娘'] },
          { card_id: 'c3', title: '方向三 · 账册失窃', tag: '危机', outline: '茶铺夜半失窃，账册不翼而飞，唯一的证物没了。', hook: '偷账本的人就在镇上。', characters: ['林素娘'] },
        ],
      },
    }
  }
  if (p === '/api/outline') return { status: 200, data: { html: null } }
  if (p === '/api/chats' && method === 'POST') {
    chatSeq += 1
    const chat = {
      id: `probe-ws-${chatSeq}`, agent: 'master', title: '', created: NOW,
      messages: [], scope: 'workspace', novel: '',
    }
    workspaceChats.push(chat)
    return { status: 200, data: { ok: true, chat } }
  }
  if (p === '/api/chats') {
    // 作用域隔离：工作区请求（novel=__workspace__）只返回工作区会话；
    // 书内请求只返回该书的会话。这与引擎 /api/chats 的行为一致。
    const inWorkspace = query.includes('__workspace__')
    const chats = inWorkspace ? workspaceChats : (WITH_PROPOSAL ? CHAT_SUMMARY.chats : [])
    return { status: 200, data: { chats } }
  }
  if (p === '/api/chats/probe-chat') return { status: 200, data: CHAT_DATA }
  if (p === '/api/chats/probe-chat/proposals') {
    return { status: 200, data: WITH_PROPOSAL ? PROPOSALS : { proposals: [] } }
  }
  if (p === '/api/book-select') return { status: 200, data: { ok: true, novel_id: '', exists: false } }
  return { status: 200, data: { ok: true } }
}

;(window as unknown as Record<string, unknown>)['inkforge'] = {
  request: async (method: string, path: string) => route(method, path),
  engineState: async () => ({ state: 'ready', port: 8137, message: '探针就绪' }),
  onEngineState: () => () => undefined,
  restartEngine: async () => ({ state: 'ready', port: 8137, message: 'ok' }),
  engineLogs: async () => [],
  appInfo: async () => ({
    versions: {},
    engineDir: '',
    engineDataDir: '',
    python: '',
    enginePort: 8137,
  }),
  pickBookFile: async () => null,
  exportSkillZip: async () => ({ ok: false, message: '' }),
  exportManuscript: async () => ({ ok: false, message: '' }),
}

// 预置作品，避免书架对话框自动弹出遮挡布局。
// **必须走 rememberBook**：生产的启动路径（App.vue::syncActiveBook）会调用它来记住
// "最近打开过的书"，工作区顶栏的「↩ 回到《X》」靠它渲染。探针直接赋值 bookId 会绕过它，
// 于是按钮永远不出现——而线上若也绕过（这正是 2026-09-17 修掉的缺陷）同样不出现。
rememberBook('probe-review', '探针样板书')
appStore.bookId = 'probe-review'
appStore.bookTitle = '探针样板书'

createApp(App).mount('#app')

// 挂载后再选中文档：EditorPane 用 watch（非 immediate）加载正文，
// 挂载前赋值不会触发 watcher。
appStore.selection = { kind: 'chapter', chapter: 1 }

// 供 Playwright 断言直接改全局状态（模拟「接受改稿提案」的 treeVersion += 1）
;(window as unknown as Record<string, unknown>)['__store'] = appStore
;(window as unknown as Record<string, unknown>)['__probe'] = {
  calls,
  ready: true,
  rawHits: () => rawHits,
  // 2026-09-17 新增断言所需的观测点
  docAligned: () => appStore.docAligned,
  loadedDocKey: () => appStore.loadedDocKey,
  chatResetToken: () => appStore.chatResetToken,
  workspaceChatCount: () => workspaceChats.length,
}
