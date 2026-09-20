/**
 * Inkforge UI 探针 · 互动创作正文预览框（可拉伸）
 *
 * 目的：用真实浏览器验证「互动创作 · 人审」屏里的正文预览框真的能上下拉伸。
 * 做法：挂载**真实的** InteractiveCards.vue，只把 window.inkforge 桥换成内存桩，
 * 状态桩固定为 awaiting_review（带草稿正文）。
 *
 * 断言要点（CSS resize 是个容易"写了不生效"的属性：元素必须 overflow != visible）：
 *   · computed style 的 resize 必须是 vertical；
 *   · 初始高度 ≥ 300px（旧实现是 max-height:260px 的硬顶，太矮）；
 *   · 用鼠标拖右下角 → 高度必须真的变大（证明不是只写了个 CSS 声明）。
 */
import { createApp, defineComponent, h } from 'vue'
import { NConfigProvider, NDialogProvider, NMessageProvider } from 'naive-ui'

import InteractiveCards from '../src/renderer/src/components/InteractiveCards.vue'
import type { InteractiveState } from '../src/renderer/src/types'
import '../src/renderer/src/styles.css'

const DRAFT = Array.from({ length: 24 }, (_, i) =>
  `第 ${i + 1} 段：他把断剑插回石缝，指节冻得发紫。钟声一下、两下，第三下没响。纯占位文本，用于量测预览框高度。`,
).join('\n\n')

/** 状态桩：类型与引擎 `/api/interactive/state` 一致（探针也要过 typecheck）。 */
const STATE: InteractiveState = {
  status: 'awaiting_review',
  chapter: 1,
  error: null,
  error_source: '',
  error_hint: '',
  cards: [],
  draft: {
    draft_text: DRAFT,
    attempt: 1,
    model: 'probe-model',
    review: {
      consistency: 8,
      plot: 8,
      continuity: 8,
      prose: 8,
      length: DRAFT.length,
      comment: '（探针占位评语）',
      issues: [],
    },
  },
}

const calls: string[] = []

;(window as unknown as Record<string, unknown>)['inkforge'] = {
  request: async (method: string, path: string) => {
    calls.push(`${method} ${path}`)
    return { status: 200, data: STATE }
  },
  engineState: async () => ({ state: 'ready', port: 8137, message: '探针就绪' }),
  onEngineState: () => () => undefined,
  restartEngine: async () => ({ state: 'ready', port: 8137, message: 'ok' }),
  engineLogs: async () => [],
  appInfo: async () => ({ versions: {}, engineDir: '', engineDataDir: '', python: '', enginePort: 8137 }),
  pickBookFile: async () => null,
  exportSkillZip: async () => ({ ok: false, message: '' }),
  exportManuscript: async () => ({ ok: false, message: '' }),
}

const Root = defineComponent({
  setup() {
    return () =>
      h(NConfigProvider, null, {
        default: () =>
          h(NMessageProvider, null, {
            default: () =>
              h(NDialogProvider, null, {
                default: () =>
                  h('div', { style: 'padding:16px;max-width:760px' }, [
                    h(InteractiveCards, { novelId: 'probe-book', state: STATE }),
                  ]),
              }),
          }),
      })
  },
})

createApp(Root).mount('#app')

;(window as unknown as Record<string, unknown>)['__probe'] = {
  calls,
  ready: true,
  state: STATE,
}
