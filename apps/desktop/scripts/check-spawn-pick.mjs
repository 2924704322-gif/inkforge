/**
 * 二开建书「来源与勾选」回归（无浏览器，直接跑真实源码 spawnPick.ts）。
 *
 * 锁定三次用户实测暴露的缺陷：
 *   ① 默认落到"全部来源书"（应为**当前书**）；
 *   ② "只看当前书"点了没反应（按钮渲染条件写错）；
 *   ③ 结构杂乱 → 改为"一个来源 + 分类清单"，跨书混搭收进高级。
 *
 * 用法：node apps/desktop/scripts/check-spawn-pick.mjs   （退出码 0 = 全通过）
 */

import { readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { transformSync } from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(here, '../src/renderer/src/components/spawnPick.ts')

async function load() {
  const raw = readFileSync(SRC, 'utf-8')
  const js = transformSync(raw, { loader: 'ts', format: 'esm' }).code
  const tmp = resolve(here, '../src/renderer/src/components/.spawnPick.check.mjs')
  writeFileSync(tmp, js, 'utf-8')
  try {
    return await import(pathToFileURL(tmp).href)
  } finally {
    rmSync(tmp, { force: true })
  }
}

const {
  spawnPool, defaultSpawnPicks, setCatSource, spawnSelectedIds,
  allFromBook, spawnSourceBooks, catSourceBook,
} = await load()

const CATS = ['世界观', '人物', '道具', '地点', '技法', '文风', '桥段']

/** 两本书的素材（source_book 标识归属），外加一条无来源的手工素材 */
const MATERIALS = [
  { id: 'a1', category: '世界观', source_book: '书A' },
  { id: 'a2', category: '人物', source_book: '书A' },
  { id: 'a3', category: '技法', source_book: '书A' },
  { id: 'a4', category: '桥段', source_book: '书A' },
  { id: 'b1', category: '世界观', source_book: '书B' },
  { id: 'b2', category: '人物', source_book: '书B' },
  { id: 'b3', category: '桥段', source_book: '书B' },
  { id: 'm1', category: '世界观', source_book: '' },
]

let pass = 0
let fail = 0
const ok = (name, cond, ev = '') => {
  if (cond) pass += 1
  else fail += 1
  console.log(`${cond ? '[PASS]' : '[FAIL]'} ${name}${ev ? `  -- ${ev}` : ''}`)
}

// ── 1. 池子按来源书过滤 ──
ok('1 池子只含该书素材',
  spawnPool(MATERIALS, '世界观', '书A').map((m) => m.id).join(',') === 'a1')
ok('2 空来源 = 全库', spawnPool(MATERIALS, '世界观', '').length === 3)
ok('3 无来源素材不算进某本书',
  spawnPool(MATERIALS, '世界观', '书A').every((m) => m.id !== 'm1'))

// ── 2. 默认：只取当前书（用户需求 ①）──
const picksA = defaultSpawnPicks(MATERIALS, CATS, '书A')
ok('4 ★ 默认来源 = 当前书（不是全部来源书）',
  catSourceBook({}, '世界观', '书A') === '书A')
ok('5 ★ 默认勾选只含当前书素材',
  picksA['世界观'].join(',') === 'a1' && !picksA['世界观'].includes('b1'),
  `ids=${picksA['世界观'].join(',')}`)
ok('6 桥段默认不勾', picksA['桥段'].length === 0)
ok('7 ★ 默认导入集合里没有任何别的书的素材',
  !spawnSelectedIds(picksA, CATS).some((id) => id.startsWith('b')),
  spawnSelectedIds(picksA, CATS).join(','))
ok('8 无该分类素材时为空数组（不是全库兜底）', picksA['地点'].length === 0)
ok('9 工作区无当前书时才退化为全库',
  catSourceBook({}, '世界观', '') === '' &&
  defaultSpawnPicks(MATERIALS, CATS, '')['世界观'].length === 3)

// ── 3. 单分类改来源：清空该分类，不影响其它 ──
const after = setCatSource(picksA, {}, '世界观', '书B', '书A')
ok('10 ★ 单分类改来源后该分类勾选被清空',
  after.picks['世界观'].length === 0 && after.overrides['世界观'] === '书B')
ok('11 改回主来源会移除覆盖项',
  Object.keys(setCatSource(picksA, { 世界观: '书B' }, '世界观', '书A', '书A').overrides)
    .length === 0)
ok('12 其它分类不受影响', after.picks['人物'].join(',') === 'a2')
ok('13 分类实际来源 = 覆盖优先生效',
  catSourceBook(after.overrides, '世界观', '书A') === '书B' &&
  catSourceBook(after.overrides, '人物', '书A') === '书A')

// ── 4. 生效态与来源清单（UI 高亮 / 摘要依据）──
ok('14 ★ 未覆盖时 allFromBook(当前书) 为真', allFromBook(picksA, {}, CATS, '书A') === true)
ok('15 覆盖后为假（来源条会提示跨书）',
  allFromBook(after.picks, after.overrides, CATS, '书A') === false)
ok('16 无当前书时恒为假（不误判）', allFromBook(picksA, {}, CATS, '') === false)
ok('17 来源清单可枚举跨书混搭',
  spawnSourceBooks(after.overrides, CATS, '书A').join(',').includes('书B'))
ok('18 纯当前书来源清单只有一项',
  spawnSourceBooks({}, CATS, '书A').join(',') === '书A')

// ── 5. 提交集合 ──
ok('19 提交集合按分类顺序去重',
  spawnSelectedIds({ 世界观: ['x1'], 人物: ['x1', 'x2'] }, ['世界观', '人物']).join(',')
  === 'x1,x2')
ok('20 提交集合忽略缺失分类（旧数据不炸）',
  spawnSelectedIds({ 世界观: ['x1'] }, ['世界观', '人物']).join(',') === 'x1')

console.log(`\n总计：${pass} 通过 / ${fail} 失败`)
process.exit(fail ? 1 : 0)
