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

/** 模型配置桩：一个真实形状的接入点 + 三个角色绑定（含对话输入框徽标用的 master/prose）。
 *
 *  api_key 特意写成 `${PROBE_API_KEY}` 而不是 `****`：引擎对占位符是不掩码的
 *  （它本来就不是秘密），所以「编辑接入点」弹窗拿到的就是这串占位符。探针据此守住
 *  「点获取模型时不得把占位符当字面量密钥回传」（2026-09-21 线上实测的报错来源）。
 */
const MODEL_CONFIG = {
  providers: [
    {
      name: 'probe-provider',
      type: 'openai_compat',
      base_url: 'http://probe.invalid/v1',
      api_key: '${PROBE_API_KEY}',
      headers: { 'x-opencode-session': 'probe-session-1' },
    },
    // 第二个接入点：用来验「行内换接入点」（新增接入点后必须能在角色行里选到它）
    {
      name: 'probe-second',
      type: 'openai_compat',
      base_url: 'http://probe.invalid/v2',
      api_key: '****',
      // 也带头：探针点「编辑」时命中的是最后一个接入点行，两边都带头断言才稳定
      headers: { 'x-opencode-session': 'probe-session-2' } as Record<string, string>,
    },
  ],
  roles: [
    { role: 'master', provider: 'probe-provider', model: 'probe-master-model', temperature: 0.8 },
    { role: 'prose', provider: 'probe-provider', model: 'probe-prose-model', temperature: 0.7 },
    { role: 'writer', provider: 'probe-provider', model: 'probe-old-model', temperature: 0.7 },
  ],
  path: '/tmp/models.yaml',
}

/** 接入点 /models 的返回：故意不含 probe-old-model 之外的其它型号，便于断言「候选来自接口」。
 *  probe-second 给一份完全不同的清单，用来验「换接入点后模型也跟着落到新清单里的值」。 */
const MODEL_LIST = ['probe-large', 'probe-old-model', 'probe-small']
const MODEL_LIST_SECOND = ['probe-alt-1', 'probe-alt-2']

const calls: string[] = []

function route(method: string, path: string, body?: unknown): { status: number; data: unknown } {
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
  // 风格工坊第三页（约束提炼）：智能体名片 + 一次提炼的结果，桩数据形状与引擎一致
  if (p === '/api/style-forge/agent') {
    return {
      status: 200,
      data: {
        key: 'constraint',
        label: '约束提炼智能体',
        role: 'constraint',
        prompt: '（探针桩：约束提炼智能体的系统提示词）',
        max_items: { default: 18, min: 3, max: 60 },
        limits: { source_max: 20000, source_min: 20, requirement_max: 2000 },
        sees: ['你在【正文内容】框里粘贴的文本', '你在【提炼需求】框里写的要求'],
        never_sees: [
          '你作品库里的任何文件（正文 / 章节 / 大纲 / 设定 / 素材 / 摘要）',
          '你没有粘贴进输入框的任何内容',
        ],
      },
    }
  }
  if (p === '/api/style-forge/constraints') {
    return {
      status: 200,
      data: {
        ok: true,
        agent: { key: 'constraint', label: '约束提炼智能体' },
        title: '提炼文风与节奏',
        constraints: '- **叙事视角**：第三人称限知；\n- **节奏**：短句为主，单段不超过 4 行',
        items: ['**叙事视角**：第三人称限知', '**节奏**：短句为主，单段不超过 4 行'],
        count: 2,
        role: 'constraint',
        model: 'probe-model',
        provider: 'probe',
        elapsed: 0.1,
        isolation: '只提炼你在本页粘贴的正文；未读取你作品库里的任何文件。',
      },
    }
  }
  if (p === '/api/learning') return { status: 200, data: { items: [] } }
  if (p === '/api/materials') return { status: 200, data: { materials: [] } }
  if (p === '/api/settings/tree') return { status: 200, data: { items: [] } }
  if (p === '/api/chapters') return { status: 200, data: { chapters: [] } }
  if (p === '/api/outline') return { status: 200, data: { html: null } }
  if (p === '/api/model-config') return { status: 200, data: MODEL_CONFIG }
  // 「获取模型」：GET /api/model-config/providers/{name}/models
  if (p.startsWith('/api/model-config/providers/') && p.endsWith('/models')) {
    const name = p.split('/')[4] ?? 'probe-provider'
    const list = name === 'probe-second' ? MODEL_LIST_SECOND : MODEL_LIST
    return {
      status: 200,
      data: { ok: true, provider: name, models: list, count: list.length },
    }
  }
  // 接入点保存：回带改动 + 记录请求头（「保存时是否真的带上了自定义头」要能被断言）
  if (method === 'PUT' && p.startsWith('/api/model-config/providers/')) {
    const name = decodeURIComponent(p.split('/').pop() ?? '')
    const patch = (body ?? {}) as {
      type?: string
      base_url?: string
      headers?: Record<string, string>
    }
    const cfg = structuredClone(MODEL_CONFIG)
    const row = cfg.providers.find((x) => x.name === name)
    if (row) {
      if (patch.type) row.type = patch.type
      if (patch.base_url) row.base_url = patch.base_url
      if (patch.headers) row.headers = patch.headers
    }
    calls.push(`HEADERS ${JSON.stringify(patch.headers ?? null)}`)
    return { status: 200, data: { ok: true, ...cfg } }
  }
  // 角色绑定保存：把改动回带（前端会把返回值直接当新的 config 视图用），
  // 于是「选完模型后行内下拉要显示新值」这条断言才验得到真东西。
  if (method === 'PUT' && p.startsWith('/api/model-config/roles/')) {
    const role = decodeURIComponent(p.split('/').pop() ?? '')
    const cfg = structuredClone(MODEL_CONFIG)
    const patch = (body ?? {}) as { provider?: string; model?: string; temperature?: number }
    const row = cfg.roles.find((r) => r.role === role)
    if (row) {
      if (patch.provider) row.provider = patch.provider
      if (patch.model) row.model = patch.model
      if (typeof patch.temperature === 'number') row.temperature = patch.temperature
    }
    return { status: 200, data: { ok: true, ...cfg } }
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
  request: async (method: string, path: string, body?: unknown) => {
    // POST /api/chats 要回一个会话对象，前端才会拿到 chatId
    const p = (path.split('?')[0] ?? path).replace(/\/$/, '')
    if (method === 'POST' && p === '/api/chats') {
      calls.push(`${method} ${path}`)
      return {
        status: 200,
        data: { ok: true, chat: { id: 'c-probe', agent: 'master', title: '', created: 0, messages: [] } },
      }
    }
    return route(method, path, body)
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
