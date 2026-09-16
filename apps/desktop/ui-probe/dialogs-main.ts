/**
 * Inkforge UI 探针 · 对话框可用性
 *
 * 目的：把真实 App.vue 外壳挂进真实 Chromium，用内存桥桩把每一个对话框
 * 从头走到尾，找出「打不开 / 打开后点不动 / 按钮没反应 / 被遮罩锁死」的那一个。
 *
 * 覆盖：书架 → 新建作品 → 设定 Demo 审核向导 → 同意设定并开始生成；
 *       与智能体的对话输入框（发送）；向导打开时点背景不误关。
 * 只用探针自造数据，不读任何真实作品。
 */
import { createApp } from 'vue'

import App from '../src/renderer/src/App.vue'
import { appStore } from '../src/renderer/src/store'
import '../src/renderer/src/styles.css'

const BOOKS = {
  books: [
    {
      novel_id: 'probe-book',
      title: '探针样板书',
      chapters: 12,
      approved: 0,
      active: false,
      finished: false,
      interactive: false,
      is_default: true,
    },
  ],
}

const DEMO = {
  book_title: '探针样板书',
  synopsis: '一个守夜人与一次失约的故事。',
  overview: '开端：守夜人接下最后一班岗。发展：钟声次数对不上。高潮：失约被揭开。结局：代价落定。',
  theme: '守夜与失约',
  worldview: [
    { filename: 'w1', title: '城墙与钟楼', content: '城墙十二里，钟楼在正中，钟声按更次敲响。' },
  ],
  characters: [
    { name: '陆守', role: '主角', appearance: '瘦高，左眉有旧疤', personality: '沉默，认死理', background: '三代守夜' },
  ],
}

const STATUS_IDLE = {
  novel_id: 'probe-book',
  started: false,
  done: false,
  error: null,
  pending: null,
  parallel: false,
  last_wave: null,
  review_history: [],
  metrics: { total_chapters: 12, approved: 0, first_pass_rate: null, avg_score: null },
}

const calls: string[] = []

function route(method: string, path: string): { status: number; data: unknown } {
  calls.push(`${method} ${path}`)
  const p = (path.split('?')[0] ?? path).replace(/\/$/, '')
  if (p === '/api/books') return { status: 200, data: BOOKS }
  if (p === '/api/status') return { status: 200, data: STATUS_IDLE }
  if (p === '/api/custom-skills') return { status: 200, data: { skills: [] } }
  if (p === '/api/skills/list') return { status: 200, data: { skills: [] } }
  if (p === '/api/bindings') return { status: 200, data: { bound_custom: [], bound_packs: [] } }
  if (p === '/api/demo') {
    // 向导读快照（GET）时直接给 done，便于走完「确认入库 → 开始生成」
    return { status: 200, data: { status: 'done', error: null, demo: DEMO } }
  }
  if (p === '/api/demo/confirm') return { status: 200, data: { ok: true } }
  if (p === '/api/start') return { status: 200, data: { ok: true } }
  if (p === '/api/chats') return { status: 200, data: { chats: [] } }
  if (p === '/api/chats/c-probe/send') return { status: 200, data: { reply: '探针回复' } }
  if (p === '/api/agent-presets') return { status: 200, data: { presets: [] } }
  if (p === '/api/learning') return { status: 200, data: { items: [] } }
  if (p === '/api/materials') return { status: 200, data: { materials: [] } }
  if (p === '/api/settings/tree') return { status: 200, data: { items: [] } }
  if (p === '/api/chapters') return { status: 200, data: { chapters: [] } }
  if (p === '/api/outline') return { status: 200, data: { html: null } }
  if (p === '/api/model-config') {
    return { status: 200, data: { providers: [], roles: [], path: '/tmp/models.yaml' } }
  }
  if (p === '/api/dashboard') {
    return {
      status: 200,
      data: {
        metrics: { total_chapters: 0, approved: 0, first_pass_rate: null, avg_score: null },
        chapters: [],
        foreshadow: { total: 0, resolved: 0, items: [] },
      },
    }
  }
  return { status: 200, data: { ok: true } }
}

;(window as unknown as Record<string, unknown>)['inkforge'] = {
  request: async (method: string, path: string) => {
    // POST /api/chats 要回一个会话对象，前端才会拿到 chatId
    const p = (path.split('?')[0] ?? path).replace(/\/$/, '')
    if (method === 'POST' && p === '/api/chats') {
      calls.push(`${method} ${path}`)
      return {
        status: 200,
        data: { ok: true, chat: { id: 'c-probe', agent: 'master', title: '', created: 0, messages: [] } },
      }
    }
    return route(method, path)
  },
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

// 预置作品，避免书架对话框自动弹出干扰
appStore.bookId = 'probe-book'
appStore.bookTitle = '探针样板书'

createApp(App).mount('#app')

;(window as unknown as Record<string, unknown>)['__probe'] = { calls, ready: true }
