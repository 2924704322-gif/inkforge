/**
 * 「预期字数」编辑态校验（无需浏览器 / 无需 bundler）。
 *
 * 做法：把**真实源码** `src/renderer/src/composables/useWordTarget.ts`
 * 用 esbuild 去类型（项目已有该依赖），再用真实 Vue 反应式系统复现
 * 「服务端每 2-5 秒轮询刷新」与「组件换稿重挂载」两个场景。
 *
 * 由来（用户实测）：手改「预期字数」后点一下输入框外面，数字就弹回默认的 3000。
 * 根因是编辑态由服务端快照驱动（轮询每次返回新对象 + `:key` 换稿强制重挂载），
 * 而不是由「用户是否改过」驱动。本脚本把该判定固化成可重复执行的回归。
 *
 * 用法：node apps/desktop/scripts/check-word-target.mjs   （退出码 0 = 全通过）
 */

import { readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { transformSync } from 'esbuild'
import { computed, nextTick, ref, watch } from 'vue'

const here = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(here, '../src/renderer/src/composables/useWordTarget.ts')

/**
 * 用 esbuild 做 TS → JS 去类型（老实说不自己写正则：类型标注与三元表达式、
 * 对象字面量混在一起，手写剥离很容易把代码切坏）。
 *
 * 产物写到源码同目录的临时 .mjs 再 import：这样 `from 'vue'` 能按真实 node_modules
 * 解析（data: URL 里解析不到裸模块名，实测 ERR_UNSUPPORTED_RESOLVE_REQUEST）。
 */
async function loadUseWordTarget() {
  const raw = readFileSync(SRC, 'utf-8')
  const js = transformSync(raw, { loader: 'ts', format: 'esm' }).code
  const tmp = resolve(here, '../src/renderer/src/composables/.useWordTarget.check.mjs')
  writeFileSync(tmp, js, 'utf-8')
  try {
    const mod = await import(pathToFileURL(tmp).href)
    return mod.useWordTarget
  } finally {
    rmSync(tmp, { force: true })
  }
}

const useWordTarget = await loadUseWordTarget()

let pass = 0
let fail = 0
const ok = (name, cond, ev = '') => {
  if (cond) pass += 1
  else fail += 1
  console.log(`${cond ? '[PASS]' : '[FAIL]'} ${name}${ev ? `  -- ${ev}` : ''}`)
}

async function main() {
  // ── 场景 A：互动创作 —— 每 2-5 秒轮询返回**新对象** ──
  const state = ref({
    chapter: 1, status: 'awaiting_choice', target_words: 3000, draft: null,
  })
  const t = useWordTarget({
    suggested: computed(() => state.value.target_words ?? null),
    effective: computed(() => state.value.target_words ?? null),
    resetWhen: computed(() => state.value.chapter),
  })
  ok('A1 初始跟随引擎默认 3000', t.display.value === 3000, `display=${t.display.value}`)

  t.display.value = 5000 // 用户手改
  await nextTick()
  ok('A2 手改后 display=5000', t.display.value === 5000)
  ok('A3 标记为「已改」', t.dirty.value === true)

  for (let i = 0; i < 3; i += 1) {
    state.value = JSON.parse(JSON.stringify({ ...state.value, target_words: 3000 }))
    await nextTick()
  }
  ok('A4 ★ 3 次服务端快照刷新后仍是 5000（不再弹回 3000）',
    t.display.value === 5000, `display=${t.display.value}`)
  ok('A5 提交值就是 5000', t.payload.value === 5000)

  t.reset()
  await nextTick()
  ok('A6 「恢复默认」回到 3000', t.display.value === 3000 && t.dirty.value === false)

  // ── 场景 B：换章 → 清空手改 ──
  t.display.value = 8000
  await nextTick()
  state.value = { chapter: 2, status: 'awaiting_choice', target_words: 3000, draft: null }
  await nextTick()
  ok('B1 换章后重新跟随服务端（不把 8000 带进第 2 章）',
    t.display.value === 3000 && t.dirty.value === false, `display=${t.display.value}`)

  // ── 场景 C：生成后引擎回带实际生效值（用户没改过 → 跟随） ──
  state.value = {
    chapter: 2, status: 'awaiting_review', target_words: 6200,
    draft: { attempt: 1, target_words: 6200 },
  }
  await nextTick()
  ok('C1 引擎回带生效值 6200 → 显示 6200', t.display.value === 6200, `display=${t.display.value}`)

  // ── 场景 D：用户改过后，引擎回带的生效值不得覆盖 ──
  t.display.value = 4500
  await nextTick()
  state.value = {
    chapter: 2, status: 'awaiting_review', target_words: 6200,
    draft: { attempt: 2, target_words: 6200 },
  }
  await nextTick()
  ok('D1 ★ 已改状态下引擎回带 6200 不覆盖用户输入 4500',
    t.display.value === 4500, `display=${t.display.value}`)

  // ── 场景 E：组件重挂载（审阅卡 :key 换稿重挂载） ──
  const t2 = useWordTarget({
    suggested: computed(() => 6200),
    effective: computed(() => 6200),
    resetWhen: computed(() => 'ch-1-2'),
  })
  ok('E1 重挂载后回到引擎生效值 6200', t2.display.value === 6200, `display=${t2.display.value}`)
  t2.display.value = null
  ok('E2 清空输入框不被服务端值填回（尊重用户的清空动作）', t2.display.value === null)

  // ── 场景 F：边界钳制 ──
  const t3 = useWordTarget({ suggested: computed(() => 3000), min: 500, max: 20000 })
  t3.display.value = 100
  await nextTick()
  ok('F1 低于下限被钳到 500', t3.display.value === 500, `display=${t3.display.value}`)
  t3.display.value = 999999
  await nextTick()
  ok('F2 高于上限被钳到 20000', t3.display.value === 20000, `display=${t3.display.value}`)

  console.log(`\n总计：${pass} 通过 / ${fail} 失败`)
  process.exit(fail ? 1 : 0)
}

await main()
