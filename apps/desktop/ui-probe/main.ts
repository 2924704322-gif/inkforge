/**
 * Inkforge UI 探针（临时验证脚手架，不参与产品构建）
 *
 * 目的：用一个真实浏览器把「书架 → 新建作品 → 创建（勾选先生成 Demo）」这条
 * 路径跑一遍，并对像素采样，量化验证向导弹窗面板是否真的有背景。
 *
 * 做法：挂载**真实的** BookshelfDialog.vue（连同其内部的 DemoWizardModal.vue），
 * 只把 Electron 的 window.inkforge 桥替换成内存桩，因此渲染路径与线上完全一致。
 */
import { createApp, defineComponent, h, ref } from 'vue'
import { NConfigProvider, NDialogProvider, NMessageProvider } from 'naive-ui'

import BookshelfDialog from '../src/renderer/src/components/BookshelfDialog.vue'
import '../src/renderer/src/styles.css'

/** 与 /api/books 返回结构一致，让书架有内容可渲染 */
const BOOKS = [
  {
    novel_id: 'demo-web',
    title: '源质觉醒',
    chapters: 3,
    approved: 2,
    active: false,
    finished: false,
    interactive: false,
    is_default: true,
  },
]

/** 记录请求，供断言「点创建后确实调了 /api/demo」 */
const calls: string[] = []

function respond(method: string, path: string): { status: number; data: unknown } {
  calls.push(`${method} ${path}`)
  if (path.startsWith('/api/books')) return { status: 200, data: { books: BOOKS } }
  if (path.startsWith('/api/custom-skills')) return { status: 200, data: { skills: [] } }
  if (path.startsWith('/api/demo/confirm')) return { status: 200, data: { ok: true } }
  // 向导进入「Architect 正在生成…」的 running 态：这正是用户看到灰屏的那一屏
  if (path.startsWith('/api/demo')) {
    return { status: 200, data: { status: 'running', error: null, demo: null } }
  }
  return { status: 200, data: { ok: true } }
}

;(window as unknown as Record<string, unknown>)['inkforge'] = {
  request: async (method: string, path: string) => respond(method, path),
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
    const show = ref(true)
    return () =>
      h(NConfigProvider, null, {
        default: () =>
          h(NMessageProvider, null, {
            default: () =>
              h(NDialogProvider, null, {
                default: () =>
                  h(BookshelfDialog, {
                    show: show.value,
                    'onUpdate:show': (v: boolean) => {
                      show.value = v
                    },
                  }),
              }),
          }),
      })
  },
})

createApp(Root).mount('#app')

// 供 Playwright 断言使用
;(window as unknown as Record<string, unknown>)['__probe'] = {
  calls,
  ready: true,
}
