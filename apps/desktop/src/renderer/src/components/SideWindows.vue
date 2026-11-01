<script setup lang="ts">
import { NInput, NSelect, useMessage } from 'naive-ui'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import { api, withNovel } from '../api'
import { appStore, openBook as openWorkspaceBook, openRevisionFromSelection, type SideWindowId } from '../store'
import {
  catSourceBook,
  defaultSpawnPicks,
  setCatSource,
  spawnPool,
  spawnSelectedIds,
} from './spawnPick'
import type {
  ActionAuditRecord,
  AgentPreset,
  Bindings,
  CustomSkill,
  DashboardData,
  LearningConflict,
  LearningItem,
  LearningResolution,
  LearningSource,
  Material,
  MaterialBook,
  RevisionAgentInfo,
  RevisionResult,
} from '../types'

/**
 * 左侧功能导航点击后弹出的小窗页面（一次一个）。
 * 窗体为悬浮面板，覆盖在对话栏之上，带标题与关闭按钮。
 */

const show = computed(() => appStore.sideWindow !== null)

const message = useMessage()

const which = computed<SideWindowId>(() => appStore.sideWindow)

/* ── 顶部返回路径 ────────────────────────────────────────────────
 * 改造前这层是 position:fixed 悬浮面板，直接盖住对话栏：打开「正文改写」
 * 就没法再跟墨师说话，也没有"我点的事去哪了"的痕迹。
 * 现在它占用正文列那一格，头部留一条「← 回到 X」+ 来源面包屑，对话栏全程可用。
 */
const backLabel = computed(() => {
  const sel = appStore.selection
  if (!sel) return '正文'
  return sel.kind === 'chapter' ? ` 第 ${sel.chapter} 章` : ` ${sel.title || '正文'}`
})
const originHint = computed(() => appStore.revisionPrefill?.origin ?? '')

function closeWindow(): void {
  appStore.sideWindow = null
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Escape') return
  const el = e.target as HTMLElement | null
  // 输入框里的 Esc 留给浏览器/组件自己处理（关下拉、清候选）
  if (el && /^(INPUT|TEXTAREA)$/.test(el.tagName)) return
  if (appStore.sideWindow) closeWindow()
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

// ── 智能体设置 ──
const presets = ref<AgentPreset[]>([])
const editingPreset = ref<AgentPreset | null>(null)
const savingPreset = ref(false)

async function loadPresets(): Promise<void> {
  const res = await api<{ presets: AgentPreset[] }>('GET', '/api/agent-presets')
  presets.value = res.presets
}

function editPreset(p: AgentPreset): void {
  editingPreset.value = { ...p }
}

async function savePreset(): Promise<void> {
  if (!editingPreset.value) return
  savingPreset.value = true
  try {
    await api('PUT', `/api/agent-presets/${editingPreset.value.key}`, {
      prompt: editingPreset.value.prompt,
    })
    message.success(`「${editingPreset.value.label}」提示词已保存，下一轮对话生效`)
    editingPreset.value = null
    await loadPresets()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    savingPreset.value = false
  }
}

async function resetPreset(p: AgentPreset): Promise<void> {
  await api('DELETE', `/api/agent-presets/${p.key}`)
  message.success('已恢复默认提示词')
  await loadPresets()
  editingPreset.value = null
}

// ── 学习仿写 ──
const learningItems = ref<LearningItem[]>([])
const learnTitle = ref('')
const learnSample = ref('')
// full=全量蒸馏（素材+剧情+文风）；style=只学通用写法（文风+技法+负面清单，不含原书内容）
const learnMode = ref<'full' | 'style'>('full')
const learningBusy = ref(false)
const viewLearning = ref<LearningItem | null>(null)

const MODE_HINT: Record<'full' | 'style', string> = {
  full: '提取素材：世界观 / 人物 / 道具地点 / 桥段 → 进素材库。设定类条目可绑定到作品，也可用它「二开建书」；桥段默认不导入，需在素材库手动勾选。',
  style: '提取写法：文风指纹 + 技法模板 + 负面清单，已做专有名词净化，产出里不带原书的人名/地名/门派/功法与剧情。',
}

async function loadLearning(): Promise<void> {
  const res = await api<{ items: LearningItem[] }>('GET', '/api/learning')
  learningItems.value = res.items
}

async function runLearning(): Promise<void> {
  if (!learnTitle.value.trim() || !learnSample.value.trim()) {
    message.warning('标题与样本正文不能为空（样本至少 200 字）')
    return
  }
  learningBusy.value = true
  try {
    const res = await api<{ id: string; mode_label?: string; scrubbed?: number }>(
      'POST',
      '/api/learning',
      {
        title: learnTitle.value.trim(),
        sample: learnSample.value,
        mode: learnMode.value,
      },
    )
    const extra =
      learnMode.value === 'style' && res.scrubbed
        ? `（已净化 ${res.scrubbed} 个专有名词）`
        : ''
    message.success(`${res.mode_label ?? '学习'}完成${extra}`)
    learnTitle.value = ''
    learnSample.value = ''
    await loadLearning()
    await viewDoc(res.id)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    learningBusy.value = false
  }
}

async function viewDoc(id: string): Promise<void> {
  const res = await api<LearningItem>(`GET`, `/api/learning/${id}`)
  viewLearning.value = res
}

// 列表里 sources/items/conflicts 是计数，详情里是数组 —— 用取数辅助函数抹平差异
const listCount = (v: unknown): number => (Array.isArray(v) ? v.length : Number(v ?? 0))
const asArray = <T,>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : [])
const srcCount = (item: LearningItem): number => listCount(item.sources)
const srcDetail = (item: LearningItem): LearningSource[] =>
  asArray<LearningSource>(item.sources)
const conflictDetail = (item: LearningItem): LearningConflict[] =>
  asArray<LearningConflict>(item.conflicts)
const resolvedDetail = (item: LearningItem): LearningResolution[] =>
  asArray<LearningResolution>(item.resolved)

// ── 冲突裁决（确定性落库，不再调模型） ──
const resolvingBusy = ref(false)
const editingConflict = ref<number | null>(null)
const editingText = ref('')

const ACTION_LABEL: Record<string, string> = {
  keep_existing: '保留原有',
  take_incoming: '采纳新增',
  merge_both: '合并两条',
  edit: '手动编辑',
}

function startEditConflict(index: number, seed: string): void {
  editingConflict.value = index
  editingText.value = seed
}

async function resolveConflict(
  action: 'keep_existing' | 'take_incoming' | 'merge_both' | 'edit' | 'undo',
  opts: { index?: number; text?: string; bulk?: boolean; undoIndex?: number } = {},
): Promise<void> {
  const target = viewLearning.value
  if (!target) return
  if (action === 'undo' && opts.undoIndex === undefined) return
  resolvingBusy.value = true
  try {
    const res = await api<{
      resolved?: number
      remaining?: number
      warnings?: string[]
      summary?: string
    }>('POST', `/api/learning/${target.id}/conflicts/resolve`, {
      action,
      index: opts.index,
      text: opts.text ?? '',
      bulk: opts.bulk ?? false,
      undo_index: opts.undoIndex,
    })
    if (res.warnings?.length) {
      message.warning(res.warnings[0])
    } else {
      message.success(res.summary ?? '已处理')
    }
    editingConflict.value = null
    editingText.value = ''
    await viewDoc(target.id)
    await loadLearning()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    resolvingBusy.value = false
  }
}

// ── 追加蒸馏：在既有成果上继续投喂下一章 ──
const appendTarget = ref<LearningItem | null>(null)
const appendSample = ref('')
const appendLabel = ref('')
const appending = ref(false)

function startAppend(item: LearningItem): void {
  appendTarget.value = item
  appendSample.value = ''
  appendLabel.value = `第 ${srcCount(item) + 1} 章`
}

async function doAppend(): Promise<void> {
  const target = appendTarget.value
  if (!target) return
  if (appendSample.value.trim().length < 200) {
    message.warning('追加的样本至少 200 字')
    return
  }
  appending.value = true
  try {
    const res = await api<{
      already?: boolean
      summary?: string
      added?: number
      merged?: number
      conflicts?: number
      scrubbed?: number
    }>('POST', `/api/learning/${target.id}/append`, {
      sample: appendSample.value,
      label: appendLabel.value.trim(),
      mode: target.mode,
    })
    if (res.already) {
      message.info(res.summary ?? '这段内容已经蒸馏过了')
    } else {
      const extra = res.scrubbed ? `，净化 ${res.scrubbed} 个专名` : ''
      message.success(
        `已追加：新增 ${res.added ?? 0} 条，合并 ${res.merged ?? 0} 条` +
          (res.conflicts ? `，冲突 ${res.conflicts} 条（待裁决）` : '') + extra,
      )
    }
    appendTarget.value = null
    appendSample.value = ''
    await loadLearning()
    await viewDoc(target.id)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    appending.value = false
  }
}

async function removeLearning(item: LearningItem): Promise<void> {
  await api('DELETE', `/api/learning/${item.id}`)
  if (viewLearning.value?.id === item.id) viewLearning.value = null
  await loadLearning()
}

// ── 素材库（统一容器；主浏览维度 = 来源书籍 → 书内七维模块） ──
const materials = ref<Material[]>([])
const materialCats = ref<string[]>([])
const materialBooks = ref<MaterialBook[]>([])
const activeBook = ref('')
const activeBookData = ref<{ by_category: Record<string, Material[]>; total: number } | null>(null)
const newMaterial = ref({ title: '', content: '', category: '其他' })
const expandedMaterial = ref('')

async function loadMaterials(): Promise<void> {
  const res = await api<{ materials: Material[]; books: MaterialBook[]; categories: string[] }>(
    'GET',
    '/api/materials',
  )
  materials.value = res.materials
  materialBooks.value = res.books ?? []
  materialCats.value = res.categories ?? []
}

/** 进入某本书的素材详情页（七维模块）。 */
function openBook(book: string): void {
  activeBook.value = book
  expandedMaterial.value = ''
  showMinor.value = false
  void refreshBook()
}

async function refreshBook(): Promise<void> {
  if (!activeBook.value) return
  const res = await api<{
    by_category: Record<string, Material[]>
    total: number
  }>('GET', `/api/materials?book=${encodeURIComponent(activeBook.value)}`)
  activeBookData.value = { by_category: res.by_category, total: res.total }
}

function closeBook(): void {
  activeBook.value = ''
  activeBookData.value = null
  void loadMaterials()
}

/** 书详情页里的分类顺序：按七维固定顺序展示 */
const BOOK_CAT_ORDER = ['世界观', '人物', '道具', '地点', '桥段', '技法', '文风', '其他']
// 道具/地点默认只显示"主级"（反复出现或被强调过的），次级另给开关 —— 用户实测混进来的杂项太多
const showMinor = ref(false)
const IMPORTANCE_ORDER: Record<string, number> = { 主级: 0, 次级: 1 }

function catItems(cat: string): Material[] {
  const all = activeBookData.value?.by_category?.[cat] ?? []
  const filtered = showMinor.value ? all : all.filter((m) => (m.importance ?? '次级') === '主级')
  return [...filtered].sort(
    (a, b) => (IMPORTANCE_ORDER[a.importance ?? '次级'] ?? 1) - (IMPORTANCE_ORDER[b.importance ?? '次级'] ?? 1),
  )
}

function catTotal(cat: string): number {
  return (activeBookData.value?.by_category?.[cat] ?? []).length
}

const activeBookCats = computed(() =>
  BOOK_CAT_ORDER.filter((c) => catTotal(c) > 0),
)

const minorCount = computed(() =>
  BOOK_CAT_ORDER.reduce((n, c) => n + catItems(c).filter(
    (m) => (m.importance ?? '次级') !== '主级').length, 0),
)

async function createMaterial(): Promise<void> {
  if (!newMaterial.value.title.trim() || !newMaterial.value.content.trim()) {
    message.warning('名称与内容不能为空')
    return
  }
  try {
    await api('POST', '/api/materials', {
      title: newMaterial.value.title.trim(),
      content: newMaterial.value.content.trim(),
      category: newMaterial.value.category,
      // 停在某本书详情页时新增，就归到那本书名下
      source_book: activeBook.value || '',
    })
    message.success('素材已保存')
    newMaterial.value = { title: '', content: '', category: newMaterial.value.category }
    if (activeBook.value) await refreshBook()
    else await loadMaterials()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

async function removeMaterial(m: Material): Promise<void> {
  await api('DELETE', `/api/materials/${m.id}`)
  if (activeBook.value) await refreshBook()
  else await loadMaterials()
}

// ── 二开建书 ──
// 结构：**一个来源书** + 七个分类的内容清单（默认全部来自该来源书）。
// 只有"参考其他书"是显式动作，不再让每个分类各自带一个下拉（那是杂乱的主要来源）。
const spawnOpen = ref(false)
const spawning = ref(false)
const SPAWN_CATS = ['世界观', '人物', '道具', '地点', '技法', '文风', '桥段']
/** 桥段是原著最"像"的部分：默认不导入，需显式打开（并说明它变成"续写起点"）。 */
const SPAWN_PLOT = '桥段'
const spawnForm = ref({
  title: '',
  /** 目录名（可留空 → 引擎按书名派生；纯中文书名不会再 400） */
  novel_id: '',
  /** 唯一来源书（'' = 全部来源书，仅在显式切换时出现） */
  sourceBook: '',
  /** 分类 → 勾选的素材 id */
  picks: {} as Record<string, string[]>,
  /** 每个分类单独的来源书覆盖（跨书混搭；默认无） */
  overrides: {} as Record<string, string>,
  /** 高级：是否允许按分类指定来源书 */
  advanced: false,
  /** 展开查看条目的分类（默认全部收起，只看数量） */
  expanded: {} as Record<string, boolean>,
})

/**
 * 打开时的默认来源书 = **当前书**。
 *
 * 由来（用户实测）：默认落到"全部来源书"，于是别的书的素材会被一起勾上、一起导入。
 * 现在两级回落，保证"打开就是当前书"：
 *   ① `appStore.bookId`（工作台当前书）；
 *   ② 素材库正打开的那本书（`activeBook`）—— 素材库书籍列表页点「二开建书」时
 *      工作台可能没有当前书，此前就会静默退化成全库。
 */
const spawnDefaultBook = computed(() => appStore.bookId || activeBook.value || '')

/** 当前生效的来源书（含"全部来源书"态）。 */
const spawnSource = computed(() => spawnForm.value.sourceBook)

/** 某分类实际使用的来源书（考虑按分类覆盖）。 */
function catBook(cat: string): string {
  return catSourceBook(spawnForm.value.overrides, cat, spawnSource.value)
}

/** 某分类的候选素材池。 */
function spawnCatPool(cat: string): Material[] {
  return spawnPool(materials.value, cat, catBook(cat)) as Material[]
}

/** 按当前来源书重新预勾选（桥段默认不勾）。 */
function reselectForSource(): void {
  spawnForm.value.overrides = {}
  spawnForm.value.picks = defaultSpawnPicks(
    materials.value, SPAWN_CATS, spawnSource.value,
  )
}

/** 切换来源书：来源变了就按新来源重勾（用户要的是"一键回到干净状态"）。 */
function setSpawnSource(book: string): void {
  spawnForm.value.sourceBook = book
  reselectForSource()
}

function openSpawn(): void {
  const book = spawnDefaultBook.value
  spawnForm.value = {
    title: '',
    novel_id: '',
    sourceBook: book,
    picks: {},
    overrides: {},
    advanced: false,
    expanded: {},
  }
  reselectForSource()
  spawnOpen.value = true
}

/** 单分类改来源书（跨书混搭）：勾选清空，避免残留上一本书的 id。 */
function setCatBook(cat: string, book: string): void {
  const next = setCatSource(
    spawnForm.value.picks, spawnForm.value.overrides, cat, book, spawnSource.value,
  )
  spawnForm.value.picks = next.picks
  spawnForm.value.overrides = next.overrides
}

function toggleSpawnItem(cat: string, id: string): void {
  const ids = spawnForm.value.picks[cat] ?? (spawnForm.value.picks[cat] = [])
  const i = ids.indexOf(id)
  if (i >= 0) ids.splice(i, 1)
  else ids.push(id)
}

function toggleCatExpand(cat: string): void {
  spawnForm.value.expanded[cat] = !spawnForm.value.expanded[cat]
}

function selectAllInCat(cat: string): void {
  spawnForm.value.picks[cat] = spawnCatPool(cat).map((m) => m.id)
}

function clearCat(cat: string): void {
  spawnForm.value.picks[cat] = []
}

/** 桥段总开关（默认关）：打开 = 导入全部桥段并生成"续写起点"。 */
const spawnWithPlot = computed({
  get: () => (spawnForm.value.picks[SPAWN_PLOT]?.length ?? 0) > 0,
  set: (on: boolean) => {
    spawnForm.value.picks[SPAWN_PLOT] = on
      ? spawnCatPool(SPAWN_PLOT).map((m) => m.id)
      : []
  },
})

/** 只列出**有素材**的分类（空分类不占版面：这是"杂乱"的主要观感来源）。 */
const spawnCatRows = computed(() =>
  SPAWN_CATS.filter((c) => spawnCatPool(c).length > 0),
)

/** 分类 → 该书下该分类的素材总数（用于 "已选 x/y"）。 */
function catPoolSize(cat: string): number {
  return spawnCatPool(cat).length
}

const spawnTotal = computed(() =>
  SPAWN_CATS.reduce((n, c) => n + (spawnForm.value.picks[c]?.length ?? 0), 0),
)

/** 跨书混搭提示：列出实际用到的来源书。 */
const spawnSourceList = computed(() => {
  const books = [...new Set(SPAWN_CATS.map((c) => catBook(c)))]
  return books.map((b) => (b ? `《${b}》` : '全部来源书'))
})
const spawnMixed = computed(() => spawnSourceList.value.length > 1)

/** 落盘去向摘要（技术路径从正文里挪到这里，不再糊在按钮区）。 */
const spawnDestSummary =
  '世界观→设定 / 人物→角色 / 文风→文风指纹 / 道具·地点·技法→创作约束 / 桥段→续写起点'

async function doSpawn(): Promise<void> {
  const title = spawnForm.value.title.trim()
  const novelId = spawnForm.value.novel_id.trim()
  if (!title && !novelId) {
    message.warning('请填写新书书名')
    return
  }
  const ids = spawnSelectedIds(spawnForm.value.picks, SPAWN_CATS)
  if (!ids.length) {
    message.warning('至少选择一个分类里的素材')
    return
  }
  spawning.value = true
  try {
    const res = await api<{
      imported?: number
      summary?: string
      source_summary?: string
      novel_id: string
      title?: string
    }>('POST', '/api/materials/spawn-book', {
      // novel_id 留空 → 引擎按书名派生（纯中文书名不再 400）；填了就按填的用
      novel_id: novelId,
      title: title || novelId,
      material_ids: ids,
      mode: 'interactive',
    })
    message.success(
      `《${res.title ?? res.novel_id}》${res.summary ?? ''}${res.source_summary ? `（${res.source_summary}）` : ''}`,
    )
    spawnOpen.value = false
    appStore.treeVersion += 1
    // 建完直接进新书：否则用户停在素材库，看不到新书、也看不到书名（真实体感：
    // "书没建成 / 名字也没了"）。openWorkspaceBook 同步引擎侧「当前书」，工作台立即切过去。
    openWorkspaceBook(res.novel_id, res.title ?? '')
    appStore.sideWindow = null
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    spawning.value = false
  }
}

// ── 数据看板 ──
const dashboard = ref<DashboardData | null>(null)
const dashBook = computed(() => appStore.bookId)
/** 墨师操作审计（P4 可视化）：与书无关，工程级记录，无书也能看。 */
const auditRecords = ref<ActionAuditRecord[]>([])
const dashTab = ref<'book' | 'audit'>('book')

async function loadDashboard(): Promise<void> {
  auditRecords.value = await api<{ records: ActionAuditRecord[] }>(
    'GET',
    '/api/actions/audit?limit=100',
  )
    .then((r) => r.records)
    .catch(() => [])
  if (!dashBook.value) {
    dashboard.value = null
    return
  }
  dashboard.value = await api<DashboardData>(
    'GET',
    `/api/dashboard?novel=${encodeURIComponent(dashBook.value)}`,
  )
}

const AUDIT_LABEL: Record<string, string> = {
  book_list: '列出书目',
  book_stat: '书目进度',
  doc_list: '设定清单',
  doc_read: '读取设定',
  chapter_list: '章节清单',
  chapter_read: '读取章节',
  outline_read: '读取大纲',
  search_workspace: '跨书检索',
  material_list: '素材清单',
  skill_list: '技能包清单',
  constraint_list: '约束清单',
  model_config: '模型绑定',
  book_create: '新建书',
  book_select: '切换书目',
  book_delete: '删除书',
  book_bind_skills: '绑定约束',
  gen_start: '启动生成',
  gen_resume: '断点续跑',
  gen_pause: '暂停生成',
  gen_decide: '章节裁决',
  demo_run: '生成设定',
  demo_confirm: '设定入库',
  interactive_start: '启动互动创作',
  interactive_choose: '选定剧情卡',
  material_create: '新增素材',
  constraint_create: '新增约束',
}

function auditLabel(op: string): string {
  return AUDIT_LABEL[op] ?? op
}

function fmtAuditTime(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(
    d.getMinutes(),
  ).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}

// ── 导出中心 ──
const exporting = ref('')
const includeDraft = ref(true)
async function doExport(format: 'txt' | 'epub'): Promise<void> {
  if (!appStore.bookId) {
    message.warning('请先在书架选择作品')
    return
  }
  exporting.value = format
  try {
    const res = await window.inkforge.exportManuscript(
      appStore.bookId,
      format,
      includeDraft.value,
    )
    if (res.ok) message.success(`已导出：${res.message}`)
    else if (res.message !== '已取消') message.error(res.message)
  } finally {
    exporting.value = ''
  }
}

/* ══════════════════ 正文改写（创作功能第四项） ══════════════════
 *
 * 形态：粘/送进来【原文】+ 写【要求】→ 整篇改后正文 + 改动对照 → 复制 / 写回 / 继续改。
 * 硬性口径（引擎侧同源）：
 *   · 只有要求点名的地方会动，其余逐字保留（越界会被引擎退回重做，不是"提示词请求"）；
 *   · 产出必须整篇回传；拒答/空产出在引擎内消化，不会把拒绝文本当结果给用户；
 *   · 自定义约束优先级最高（冲突时按约束执行）。
 */

const revAgent = ref<RevisionAgentInfo | null>(null)
const revSource = ref('')
const revRequirement = ref('')
const revMode = ref('auto')
const revConstraints = ref('')
const revStyleSample = ref('')
const revExtraLocks = ref('')
const revAutoLock = ref(true)
const revAnchor = ref(0)
const revBusy = ref(false)
const revResult = ref<RevisionResult | null>(null)
const revView = ref<'full' | 'diff' | 'summary'>('diff')
/** 约束库（全局自定义约束）与本次勾选项 —— 「让我勾选我已有的约束」 */
const revSkillLibrary = ref<CustomSkill[]>([])
const revPickedSkills = ref<string[]>([])
/** 当前书已绑定的约束 id（只在列表里打标提示，不自动勾选 —— 用哪几条由你说了算） */
const revBoundSkillIds = ref<string[]>([])
/** 来源面包屑：从哪打开的这个模块（侧窗会盖住对话栏，入口必须留痕） */
const revOrigin = ref('')
/** 回填目标（人审卡「局部改写」用）：改完回填本章草稿，而不是让用户自己复制 */
const revFillBack = ref<{ chapter: number; novelId: string } | null>(null)

const revSourceLimit = computed(() => revAgent.value?.limits?.source_max ?? 15000)
const revReqLimit = computed(() => revAgent.value?.limits.requirement_max ?? 4000)
const revTooLong = computed(() => revSource.value.length > revSourceLimit.value)
const revCanRun = computed(() =>
  revSource.value.trim().length > 0
  && revRequirement.value.trim().length > 0
  && !revTooLong.value
  && !revBusy.value,
)

/** 段落列表（与引擎同一条切分规则：空行分段）—— 锚点就是这么来的。 */
const revParas = computed(() =>
  revSource.value
    .replace(/\r\n/g, '\n')
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter((p) => p.length > 0),
)

/** 写回/回填的目标章号：人审来源优先，其次当前选中的章节。 */
const revTargetChapter = computed<number>(() => {
  if (revFillBack.value) return revFillBack.value.chapter
  const sel = appStore.selection
  return sel && sel.kind === 'chapter' ? sel.chapter : 0
})

/**
 * 真正发给模型的约束文本 = 手写条目 + 勾选的库条目。
 *
 * 两者**都**按最高优先级执行（引擎侧渲染成"逐条核对的执行清单"）。
 * 库条目带上标题（`【约束名】`），便于产出后在正文里核对是哪一条起了作用。
 */
const revEffectiveConstraints = computed(() => {
  const picked = revSkillLibrary.value.filter((s) => revPickedSkills.value.includes(s.skill_id))
  const blocks = picked.map((s) => `【${s.title}】\n${(s.content || '').trim()}`)
  const manual = revConstraints.value.trim()
  return [manual, ...blocks].filter(Boolean).join('\n\n')
})

/** 载入约束库（全局自定义约束）与"当前书已绑定哪几条"。 */
async function loadRevisionLibraries(): Promise<void> {
  try {
    const res = await api<{ skills: CustomSkill[] }>('GET', '/api/custom-skills')
    revSkillLibrary.value = res.skills ?? []
  } catch {
    revSkillLibrary.value = []
  }
  try {
    if (!appStore.bookId) {
      revBoundSkillIds.value = []
      return
    }
    const b = await api<Bindings>('GET', withNovel('/api/bindings', appStore.bookId))
    revBoundSkillIds.value = b.bound_custom ?? []
  } catch {
    revBoundSkillIds.value = []
  }
}

/** 模式下拉：auto 排第一（默认），其余按"改动幅度从大到小"排，避免误选"整体重写"。 */
const revModeOptions = computed(() => {
  const modes = revAgent.value?.modes ?? [
    { value: 'auto', label: '自动判定' },
    { value: 'expand', label: '扩写' },
    { value: 'rewrite', label: '改写' },
    { value: 'insert', label: '新增段落' },
    { value: 'replace', label: '定向替换' },
    { value: 'rewrite_all', label: '整体重写' },
  ]
  return modes.map((m) => ({ label: m.label, value: m.value }))
})

/** 快捷要求片：降低上手成本（点一下就填进要求框，用户仍可改）。 */
const REV_QUICK = [
  '扩写环境描写',
  '加一段对白',
  '改得更紧凑',
  '只改错别字',
]

/** 改动对照的行首标签。 */
const REV_KIND: Record<string, string> = {
  keep: '未动',
  insert: '新增',
  edit: '改写',
  delete: '删除',
}

const revShowPrompt = ref(false)

/** 从创作空间当前选中的文稿载入原文（走 store 的共用通道，与自由协作入口同一实现）。 */
async function loadRevisionFromSelection(): Promise<void> {
  const res = await openRevisionFromSelection('创作空间')
  if (res.ok) message.success('已载入原文（改完可写回同一处）')
  else message.warning(res.message)
}

async function loadRevisionAgent(): Promise<void> {
  try {
    revAgent.value = await api<RevisionAgentInfo>('GET', '/api/revision/agent')
  } catch {
    // 读不到就说读不到：面板仍可用，只是拿不到上限/视野声明
    revAgent.value = null
  }
}

/** 消费四个入口写进来的预填载荷（消费后清空，避免下次打开又冒出来）。 */
function consumeRevisionPrefill(): void {
  const p = appStore.revisionPrefill
  if (!p) return
  appStore.revisionPrefill = null
  revSource.value = p.source || ''
  revRequirement.value = p.requirement || ''
  revMode.value = p.mode || 'auto'
  revAnchor.value = p.anchorIndex || 0
  revOrigin.value = p.origin || ''
  revFillBack.value = p.fillBack ?? null
  revResult.value = null
  revView.value = 'diff'
}

async function runRevision(): Promise<void> {
  if (!revCanRun.value) return
  revBusy.value = true
  revResult.value = null
  try {
    const res = await api<RevisionResult>('POST', '/api/revision/rewrite', {
      source_text: revSource.value,
      requirement: revRequirement.value,
      mode: revMode.value,
      constraints: revEffectiveConstraints.value,
      style_sample: revStyleSample.value,
      lock_dialogue: revAutoLock.value,
      lock_segments: revExtraLocks.value
        .split('\n')
        .map((s) => s.trim())
        .filter((s) => s.length > 0),
      anchors: revAnchor.value > 0 ? [{ after_index: revAnchor.value }] : [],
    })
    revResult.value = res
    revView.value = 'diff'
    message.success(
      `${res.mode_label}完成：${res.stats.inserted} 新增 / ${res.stats.edited} 改写 / ${res.stats.deleted} 删除（${res.model} · ${res.elapsed}s）`,
    )
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    revBusy.value = false
  }
}

async function copyRevised(): Promise<void> {
  const text = revResult.value?.revised ?? ''
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    message.success('已复制整篇正文')
  } catch {
    message.warning('复制失败：请手动选中后按 Ctrl+C')
  }
}

/**
 * 写回/回填：**一律走既有 `PUT /api/chapters/{n}`**（人工编辑语义）。
 *
 * 它用 `metadata=dict(doc.metadata)` 保存，所以 status/title/chapter 全部继承 ——
 * 草稿仍是草稿，互动创作的状态机不会因为这次回填而认不出这一章。
 * 绝不新造第二条落盘通路。
 */
async function writeBackRevision(): Promise<void> {
  const chapter = revTargetChapter.value
  const text = revResult.value?.revised ?? ''
  if (!chapter || !text) return
  const where = revFillBack.value ? '本章草稿' : `第 ${chapter} 章`
  if (!window.confirm(
    `把改写结果写回${where}？\n\n这会覆盖该章现有正文（Git 写作历史会留痕，可回滚）。\n`
    + '注意：这只做「人工编辑」，不会替你定稿——互动创作/人审仍需你自己点通过。',
  )) return
  try {
    await api('PUT', withNovel(`/api/chapters/${chapter}`, appStore.bookId), { content: text })
    appStore.treeVersion += 1
    message.success(`已写回${where}（草稿，未定稿）`)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

/** 继续改这一版：把结果设为新的原文（多轮迭代不必重新粘贴）。 */
function continueRevision(): void {
  const text = revResult.value?.revised ?? ''
  if (!text) return
  revSource.value = text
  revRequirement.value = ''
  revAnchor.value = 0
  revResult.value = null
  revOrigin.value = revOrigin.value ? `${revOrigin.value} · 第 2 轮` : ''
  message.info('已把这版设为新的原文，可以接着改')
}

const revDiffLines = computed(() => revResult.value?.segments ?? [])

async function loadAll(): Promise<void> {
  try {
    const jobs: Promise<void>[] = [
      loadPresets(), loadLearning(), loadMaterials(), loadRevisionAgent(),
      loadRevisionLibraries(),
    ]
    if (appStore.bookId) jobs.push(loadDashboard())
    await Promise.all(jobs)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

watch(
  () => appStore.sideWindow,
  (w) => {
    if (!w) return
    // 每次打开素材库都回到「书籍列表」第一屏（避免停在上次那本书的详情页）
    if (w === 'materials') {
      activeBook.value = ''
      activeBookData.value = null
      spawnOpen.value = false
    }
    // 正文改写：消费入口写下的预填载荷（原文/要求/锚点/来源/回填目标）
    if (w === 'revision') consumeRevisionPrefill()
    void loadAll()
  },
)
watch(
  () => appStore.bookId,
  () => {
    if (appStore.sideWindow === 'dashboard') void loadDashboard()
  },
)
// 预填载荷是"每个入口现写现用"的一次性输入：侧窗**已经开着**时再送文稿进来，
// sideWindow 不会发生变化，只有这条 watch 能把它消费掉（否则点了像没反应）。
watch(
  () => appStore.revisionPrefill,
  () => consumeRevisionPrefill(),
)
</script>

<template>
  <transition name="slide">
    <aside v-if="show && which" class="side-window">
      <div class="win-head">
        <div class="win-head-left">
          <button class="back-btn" title="关闭并回到正文（Esc）" @click="closeWindow">
            ← 回到{{ backLabel }}
          </button>
          <span class="win-title">{{
            ({
              agents: '智能体设置',
              learning: '学习仿写',
              materials: '素材库',
              revision: '正文改写',
              dashboard: '数据看板',
              export: '导出中心',
            } as Record<string, string>)[which]
          }}</span>
        </div>
        <button class="close-btn" title="关闭（Esc）" @click="closeWindow">✕</button>
      </div>
      <div v-if="originHint" class="win-origin">来自：{{ originHint }}</div>

      <div class="win-body scroll-y">
        <!-- 智能体设置 -->
        <template v-if="which === 'agents'">
          <div class="muted" style="margin-bottom: 10px">
            各智能体的系统提示词（五个阶段子智能体 + 风格工坊的约束提炼智能体）；修改后下一轮对话立即生效。
            绑定素材与约束见「风格工坊」。
          </div>
          <div v-for="p in presets" :key="p.key" class="agent-block">
            <div class="agent-line">
              <b>{{ p.label }}</b>
              <span v-if="p.custom" class="tag">已自定义</span>
              <span class="row-gap">
                <button class="ghost-btn" @click="editPreset(p)">编辑</button>
                <button v-if="p.custom" class="ghost-btn" @click="resetPreset(p)">恢复默认</button>
              </span>
            </div>
            <pre v-if="editingPreset?.key !== p.key" class="prompt-view pre-wrap">{{ p.prompt }}</pre>
            <template v-else>
              <NInput
                v-model:value="editingPreset.prompt"
                type="textarea"
                :rows="6"
                class="prompt-edit"
              />
              <div class="row-gap" style="justify-content: flex-end; margin-top: 6px">
                <button class="ghost-btn" @click="editingPreset = null">取消</button>
                <button class="primary-btn" :disabled="savingPreset" @click="savePreset">保存</button>
              </div>
            </template>
          </div>
        </template>

        <!-- 学习仿写 → 提取素材 / 提取写法（产出统一进素材库） -->
        <template v-else-if="which === 'learning'">
          <div class="muted" style="margin-bottom: 10px">
            粘贴一段喜欢的正文（建议 500-3000 字）。两种意图二选一，产出都会进
            <b>素材库</b>，可在那里筛选、绑定到作品、或直接二开建书。
          </div>

          <div class="mode-row">
            <button
              class="mode-card"
              :class="{ on: learnMode === 'full' }"
              @click="learnMode = 'full'"
            >
              <b>提取素材</b>
              <span class="muted">世界观 / 人物 / 道具地点 / 桥段 → 素材库（可二开建书）</span>
            </button>
            <button
              class="mode-card"
              :class="{ on: learnMode === 'style' }"
              @click="learnMode = 'style'"
            >
              <b>提取写法</b>
              <span class="muted">文风指纹 + 技法模板 + 负面清单 · 不含原书内容</span>
            </button>
          </div>
          <div class="mode-hint muted">{{ MODE_HINT[learnMode] }}</div>

          <NInput v-model:value="learnTitle" size="small" placeholder="学习任务标题，例：天蚕土豆·战斗章" style="margin-bottom: 8px" />
          <NInput
            v-model:value="learnSample"
            type="textarea"
            :rows="7"
            placeholder="粘贴样本文本……"
            style="margin-bottom: 8px"
          />
          <button class="primary-btn" :disabled="learningBusy" @click="runLearning">
            {{
              learningBusy
                ? learnMode === 'style'
                  ? '写法分析中…（约 1 分钟）'
                  : '三阶段分析中…（约 1 分钟）'
                : learnMode === 'style'
                  ? '提取写法'
                  : '提取素材'
            }}
          </button>

          <div class="section-title">任务列表（产出已进素材库）</div>
          <div v-for="item in learningItems" :key="item.id" class="row-card">
            <span class="row-title" @click="viewDoc(item.id)">
              <span class="tag" :class="item.mode === 'style' ? 'style' : 'full'">
                {{ item.mode === 'style' ? '写法' : '素材' }}
              </span>
              {{ item.title }}
              <span class="muted">
                · {{ srcCount(item) }} 章
                <template v-if="listCount(item.materials)"> · 素材 {{ listCount(item.materials) }} 条</template>
                <template v-if="listCount(item.items)"> · 写法 {{ listCount(item.items) }} 条</template>
                <template v-if="item.scrubbed"> · 已净化 {{ item.scrubbed }}</template>
                <template v-if="listCount(item.conflicts)"> · <b class="warn">冲突 {{ listCount(item.conflicts) }}</b></template>
              </span>
            </span>
            <span class="row-gap">
              <button class="ghost-btn" @click="startAppend(item)">追加蒸馏</button>
              <button class="ghost-btn" @click="viewDoc(item.id)">查看</button>
              <button class="del-btn" @click="removeLearning(item)">删除</button>
            </span>
          </div>

          <!-- 追加蒸馏对话框 -->
          <div v-if="appendTarget" class="append-box">
            <div class="row-between">
              <b>往「{{ appendTarget.title }}」追加一章</b>
              <button class="ghost-btn" @click="appendTarget = null">取消</button>
            </div>
            <div class="muted">
              已学 {{ srcCount(appendTarget) }} 章。追加后：重复的写法只累加命中次数，
              新写法并入条目表；同一条目出现不同说法会进「待裁决冲突」，不会覆盖原有内容。
              同一段内容重复投喂会自动跳过。
            </div>
            <NInput v-model:value="appendLabel" size="small" placeholder="章节标签，例：第二章" />
            <NInput
              v-model:value="appendSample"
              type="textarea"
              :rows="6"
              placeholder="粘贴本章样本（至少 200 字）……"
            />
            <button class="primary-btn" :disabled="appending" @click="doAppend">
              {{ appending ? '蒸馏中…（约 1 分钟）' : '追加并蒸馏' }}
            </button>
          </div>

          <div v-if="viewLearning" class="view-box scroll-y">
            <div class="row-between">
              <b>{{ viewLearning.title }}</b>
              <button class="ghost-btn" @click="viewLearning = null">收起</button>
            </div>
            <div class="muted">
              来源：{{ srcDetail(viewLearning).map((s) => `${s.label}(${s.chars}字)`).join(' / ') || '—' }}
              <template v-if="viewLearning.legacy"> · 旧格式（追加一章会自动升级为条目化）</template>
            </div>
            <div v-if="conflictDetail(viewLearning).length" class="conflict-box">
              <div class="row-between">
                <b>待裁决冲突 {{ conflictDetail(viewLearning).length }} 条</b>
                <span class="row-gap">
                  <button
                    class="ghost-btn"
                    :disabled="resolvingBusy"
                    @click="resolveConflict('keep_existing', { bulk: true })"
                  >
                    全部保留原有
                  </button>
                  <button
                    class="ghost-btn"
                    :disabled="resolvingBusy"
                    @click="resolveConflict('take_incoming', { bulk: true })"
                  >
                    全部采纳新增
                  </button>
                </span>
              </div>
              <div class="muted">
                同一维度的两种说法会让 Writer 收到矛盾要求，需要收敛成一条。
                采纳后仍可撤销；裁决只改条目表，不再调用模型。
              </div>
              <div
                v-for="(c, i) in conflictDetail(viewLearning)"
                :key="i"
                class="conflict-row"
              >
                <div class="muted">
                  【{{ c.section }}】重合度 {{ c.overlap ?? '—' }}
                </div>
                <div>原有（第 {{ (c.existing_chapters || []).join('、') }} 章）：{{ c.existing }}</div>
                <div>追加（第 {{ c.incoming_chapter }} 章）：{{ c.incoming }}</div>
                <div v-if="editingConflict === i" class="conflict-edit">
                  <NInput v-model:value="editingText" type="textarea" :rows="2" size="small" />
                  <span class="row-gap">
                    <button
                      class="primary-btn"
                      :disabled="resolvingBusy || !editingText.trim()"
                      @click="resolveConflict('edit', { index: i, text: editingText })"
                    >
                      保存
                    </button>
                    <button class="ghost-btn" @click="editingConflict = null">取消</button>
                  </span>
                </div>
                <div v-else class="row-gap conflict-actions">
                  <button class="ghost-btn" :disabled="resolvingBusy" @click="resolveConflict('keep_existing', { index: i })">
                    保留原有
                  </button>
                  <button class="primary-btn" :disabled="resolvingBusy" @click="resolveConflict('take_incoming', { index: i })">
                    采纳新增
                  </button>
                  <button class="ghost-btn" :disabled="resolvingBusy" @click="resolveConflict('merge_both', { index: i })">
                    合并两条
                  </button>
                  <button class="ghost-btn" :disabled="resolvingBusy" @click="startEditConflict(i, c.incoming)">
                    手动编辑…
                  </button>
                </div>
              </div>
            </div>

            <div v-if="resolvedDetail(viewLearning).length" class="resolved-box">
              <b>已裁决 {{ resolvedDetail(viewLearning).length }} 条</b>
              <div
                v-for="(r, i) in resolvedDetail(viewLearning)"
                :key="i"
                class="resolved-row"
              >
                <span class="muted">
                  ✓ {{ ACTION_LABEL[r.action] ?? r.action }}（{{ r.section }}）
                </span>
                <span>{{ r.final_text || r.incoming || r.existing }}</span>
                <button
                  class="ghost-btn"
                  :disabled="resolvingBusy"
                  @click="resolveConflict('undo', { undoIndex: i })"
                >
                  撤销
                </button>
              </div>
            </div>
            <pre class="pre-wrap view-pre">{{ viewLearning.content }}</pre>
          </div>
        </template>

        <!-- 素材库：① 书籍列表 → ② 书内七维模块 -->
        <template v-else-if="which === 'materials'">
          <!-- ② 书详情：七维模块 -->
          <template v-if="activeBook">
            <div class="row-between shelf-head-gap">
              <span class="row-gap">
                <button class="ghost-btn" @click="closeBook">← 返回书籍列表</button>
                <b>{{ activeBook }}</b>
                <span class="muted">
                  共 {{ activeBookData?.total ?? 0 }} 条
                  <template v-if="minorCount"> · 已折叠 {{ minorCount }} 条次级</template>
                </span>
                <label class="minor-toggle">
                  <input v-model="showMinor" type="checkbox" />
                  显示次级（只出现一次的杂项）
                </label>
              </span>
              <button class="primary-btn" @click="openSpawn">二开建书</button>
            </div>

            <div v-for="cat in activeBookCats" :key="cat" class="dim-block">
              <div class="dim-title">
                <span class="tag cat">{{ cat }}</span>
                {{ catItems(cat).length }} / {{ catTotal(cat) }} 条
                <span v-if="cat === '桥段'" class="muted">· 按章序排列，导入后作为续写线索</span>
                <span v-else-if="cat === '道具' || cat === '地点'" class="muted">
                  · 默认只显示反复出现/被强调过的
                </span>
              </div>
              <div
                v-for="m in catItems(cat)"
                :key="m.id"
                class="row-card"
              >
                <div class="col-gap">
                  <div class="row-between">
                    <span class="row-title">
                      <span v-if="(m.importance ?? '') === '主级'" class="tag major">主级</span>
                      {{ m.title }}
                      <span v-if="(m.hits ?? 1) > 1" class="muted">· 印证 {{ m.hits }} 次</span>
                      <span v-if="m.chapters?.length" class="muted">· 第 {{ m.chapters.join('、') }} 章</span>
                    </span>
                    <span class="row-gap">
                      <button class="ghost-btn" @click="expandedMaterial = expandedMaterial === m.id ? '' : m.id">
                        {{ expandedMaterial === m.id ? '收起' : '查看' }}
                      </button>
                      <button class="del-btn" @click="removeMaterial(m)">删除</button>
                    </span>
                  </div>
                  <pre v-if="expandedMaterial === m.id" class="pre-wrap view-pre">{{ m.content }}</pre>
                </div>
              </div>
              <div v-if="!catItems(cat).length" class="muted" style="padding: 2px 0">
                该分类下没有主级素材（勾选上方「显示次级」可查看全部）。
              </div>
            </div>
            <div v-if="!activeBookCats.length" class="muted" style="padding: 4px 0">
              这本书下暂无素材。
            </div>
          </template>

          <!-- ① 书籍列表：素材的主浏览维度 -->
          <template v-else>
            <div class="muted" style="margin-bottom: 10px">
              按<b>来源书籍</b>浏览：点一本书进入它的七维素材模块。素材来自手写、墨师新增、
              以及「提取素材」蒸馏的产出。
            </div>
            <div class="row-between shelf-head-gap">
              <span class="muted">共 {{ materialBooks.length }} 本书 · {{ materials.length }} 条素材</span>
              <button class="primary-btn" @click="openSpawn">二开建书</button>
            </div>

            <div
              v-for="b in materialBooks"
              :key="b.book"
              class="row-card book-card-click"
              @click="openBook(b.book)"
            >
              <div class="col-gap" style="width: 100%">
                <div class="row-between">
                  <span class="row-title">📖 {{ b.book }}</span>
                  <span class="muted">{{ b.total }} 条 →</span>
                </div>
                <span class="muted">
                  <template v-if="b.chapters?.length">已蒸馏第 {{ b.chapters.join('、') }} 章 · </template>
                  <template v-for="(n, cat) in b.counts" :key="cat">{{ cat }} {{ n }} · </template>
                </span>
              </div>
            </div>
            <div v-if="!materialBooks.length" class="muted" style="padding: 4px 0">
              素材库还是空的。可以用「学习仿写 → 提取素材」从样本里蒸馏，或在下方直接添加。
            </div>

            <div class="new-block">
              <NInput v-model:value="newMaterial.title" size="small" placeholder="素材名称，例：修真宗门体系" style="margin-bottom: 6px" />
              <NInput v-model:value="newMaterial.content" type="textarea" :rows="4" size="small" placeholder="素材内容" style="margin-bottom: 6px" />
              <div class="cat-row" style="margin-bottom: 8px">
                <button
                  v-for="c in (materialCats.length ? materialCats : ['其他'])"
                  :key="c"
                  class="cat-chip"
                  :class="{ on: newMaterial.category === c }"
                  @click="newMaterial.category = c"
                >
                  {{ c }}
                </button>
              </div>
              <button class="primary-btn" @click="createMaterial">添加素材</button>
            </div>
          </template>

          <!-- ══════════ 二开建书（三段式：新书 → 来源 → 内容清单） ══════════
               设计原则：一条主线、默认全对、技术细节收起。
               ① 新书：只填书名（目录名自动派生，可展开手填）
               ② 来源：**默认当前书**（一个来源，不是七个下拉）
               ③ 内容：有素材的分类才出现，默认全选，展开才看条目
               跨书混搭 / 落盘去向 / 目录名 一律收进「高级」，
               主线上没有一个需要用户理解的内部概念（worldview / custom-skills 等）。 -->
          <div v-if="spawnOpen" class="append-box spawn-panel">
            <div class="row-between">
              <b>二开建书</b>
              <span class="row-gap">
                <span class="muted">将导入 {{ spawnTotal }} 条素材</span>
                <button class="ghost-btn" @click="spawnOpen = false">取消</button>
              </span>
            </div>

            <!-- ① 新书 -->
            <div class="spawn-block">
              <div class="spawn-step">① 新书</div>
              <NInput
                v-model:value="spawnForm.title"
                placeholder="书名，例：拆装时代"
                @keyup.enter="doSpawn"
              />
              <div v-if="spawnForm.advanced" class="spawn-sub">
                <span class="muted">目录名（可留空，将按书名自动生成）</span>
                <NInput
                  v-model:value="spawnForm.novel_id"
                  size="small"
                  placeholder="留空即可，例：chai-zhuang-shi-dai"
                />
              </div>
            </div>

            <!-- ② 来源：默认当前书 -->
            <div class="spawn-block">
              <div class="spawn-step">② 素材来源</div>
              <div class="spawn-source" :class="{ mixed: spawnMixed }">
                <span class="spawn-source-main">
                  <b v-if="spawnSource">《{{ spawnSource }}》</b>
                  <b v-else>全部来源书（跨书混选）</b>
                  <span class="muted">
                    {{ spawnSource
                      ? ' · 只取这本书的素材，不会混入别的书'
                      : ' · 会混入所有书的素材' }}
                  </span>
                  <span v-if="spawnMixed" class="warn">
                    · 各分类来源不同：{{ spawnSourceList.join(' + ') }}
                  </span>
                </span>
                <span class="row-gap">
                  <button
                    v-if="spawnDefaultBook && spawnSource !== spawnDefaultBook"
                    class="ghost-btn"
                    @click="setSpawnSource(spawnDefaultBook)"
                  >回到当前书</button>
                  <select
                    :value="spawnSource"
                    class="mini-select"
                    @change="setSpawnSource(($event.target as HTMLSelectElement).value)"
                  >
                    <option v-if="spawnDefaultBook" :value="spawnDefaultBook">
                      当前书《{{ spawnDefaultBook }}》
                    </option>
                    <option value="">全部来源书（跨书混选）</option>
                    <option
                      v-for="b in materialBooks.filter((x) => x.book !== spawnDefaultBook)"
                      :key="b.book"
                      :value="b.book"
                    >《{{ b.book }}》</option>
                  </select>
                </span>
              </div>
            </div>

            <!-- ③ 内容清单：有素材的分类才出现；默认全选；展开才看条目 -->
            <div class="spawn-block">
              <div class="row-between">
                <div class="spawn-step">③ 导入内容</div>
                <span class="row-gap">
                  <button class="link-btn" @click="reselectForSource">全部重选</button>
                  <button
                    class="link-btn"
                    @click="SPAWN_CATS.forEach((c) => clearCat(c))"
                  >全部清空</button>
                </span>
              </div>

              <div
                v-for="cat in spawnCatRows"
                :key="cat"
                class="spawn-row"
                :class="{ off: !(spawnForm.picks[cat]?.length) }"
              >
                <div class="row-between">
                  <span class="row-gap">
                    <span class="tag cat">{{ cat }}</span>
                    <span class="muted">
                      已选 {{ spawnForm.picks[cat]?.length ?? 0 }} / {{ catPoolSize(cat) }}
                    </span>
                    <span v-if="cat === SPAWN_PLOT" class="muted">
                      · 导入后作为续写起点（按原章序）
                    </span>
                  </span>
                  <span class="row-gap">
                    <button class="ghost-btn" @click="selectAllInCat(cat)">全选</button>
                    <button class="ghost-btn" @click="clearCat(cat)">清空</button>
                    <button class="link-btn" @click="toggleCatExpand(cat)">
                      {{ spawnForm.expanded[cat] ? '收起条目' : '选择条目' }}
                    </button>
                  </span>
                </div>
                <div v-if="spawnForm.expanded[cat]" class="cat-row">
                  <button
                    v-for="m in spawnCatPool(cat)"
                    :key="m.id"
                    class="cat-chip"
                    :class="{ on: spawnForm.picks[cat]?.includes(m.id) }"
                    @click="toggleSpawnItem(cat, m.id)"
                    :title="m.source_book || '无来源'"
                  >
                    {{ m.title }}
                  </button>
                </div>
              </div>
              <div v-if="!spawnCatRows.length" class="muted">
                当前来源下没有素材。换一个来源，或先去上方「学习仿写」提取素材。
              </div>

              <label v-if="spawnCatPool(SPAWN_PLOT).length" class="spawn-plot">
                <input v-model="spawnWithPlot" type="checkbox" />
                <span>
                  <b>同时导入「桥段」</b>
                  <small class="muted">
                    桥段是原著最"像"的部分，默认不导入。勾上后新书会带一份
                    <b>续写起点</b>，从原文最后一条之后接着往下写。
                  </small>
                </span>
              </label>
            </div>

            <!-- 高级：跨书混搭 / 落盘去向 / 目录名 -->
            <details class="spawn-advanced">
              <summary>高级（跨书混搭 · 落盘去向 · 目录名）</summary>
              <div class="muted">落盘去向：{{ spawnDestSummary }}</div>
              <div v-for="cat in spawnCatRows" :key="cat" class="advanced-row">
                <span class="tag cat">{{ cat }}</span>
                <select
                  :value="catBook(cat)"
                  class="mini-select"
                  @change="setCatBook(cat, ($event.target as HTMLSelectElement).value)"
                >
                  <option value="">全部来源书</option>
                  <option
                    v-for="b in materialBooks"
                    :key="b.book"
                    :value="b.book"
                  >《{{ b.book }}》</option>
                </select>
                <span class="muted">该分类单独指定来源书（会清空该分类勾选）</span>
              </div>
            </details>

            <div class="spawn-footer">
              <span class="muted">
                来源：{{ spawnSourceList.join(' + ') }} · 共 {{ spawnTotal }} 条
              </span>
              <button class="primary-btn" :disabled="spawning" @click="doSpawn">
                {{ spawning ? '建书中…' : '建书并进入新书' }}
              </button>
            </div>
          </div>
        </template>

        <!-- 正文改写（第四个模块 · 专职智能体：你给原文 + 你写要求 → 它改出整篇） -->
        <template v-else-if="which === 'revision'">
          <div v-if="revOrigin" class="rev-crumb">
            来自：<b>{{ revOrigin }}</b>
            <button class="ghost-btn" @click="revOrigin = ''">知道了</button>
          </div>

          <div class="rev-banner">
            <div class="rev-banner-line">
              <b>{{ revAgent?.label || '正文改写智能体' }}</b>
              <span class="muted">在你给的<b>原文</b>上，按你写的<b>要求</b>改出<b>整篇</b></span>
            </div>
            <div class="rev-scope">
              <span class="scope-yes">✅ 它看得到：{{
                (revAgent?.sees || ['你在【原文】框里粘贴的正文', '你在【改写要求】框里写的要求']).join('、')
              }}</span>
              <span class="scope-no">🚫 它看不到：{{
                (revAgent?.never_sees || ['你作品库里的任何文件', '你没粘贴、也没送进来的任何内容']).join('、')
              }}</span>
            </div>
          </div>

          <div class="rev-field">
            <div class="rev-field-head">
              <span>① 原文<span class="muted">（要改的正文）</span></span>
              <span class="rev-counter" :class="{ over: revTooLong }">
                {{ revSource.length }} / {{ revSourceLimit }}
              </span>
            </div>
            <NInput
              v-model:value="revSource"
              type="textarea"
              :rows="8"
              size="small"
              placeholder="粘贴要改的正文；也可以从右侧创作空间点选一篇文稿后按下面的按钮直接载入"
            />
            <div class="row-gap">
              <button
                class="ghost-btn"
                :disabled="!appStore.selection"
                :title="appStore.selection ? '载入当前选中的文稿' : '先在右侧创作空间点选文稿'"
                @click="loadRevisionFromSelection"
              >
                从创作空间选中载入
              </button>
              <button class="ghost-btn" @click="revSource = ''; revAnchor = 0">清空</button>
              <span class="muted">共 {{ revParas.length }} 段</span>
            </div>
          </div>

          <div v-if="revParas.length" class="rev-field">
            <div class="rev-field-head">
              <span>段落锚点<span class="muted">（点一段 = 在它之后改／插；不选则由模型判断位置）</span></span>
              <span class="rev-counter">{{ revAnchor ? `已选第 ${revAnchor} 段` : '未选' }}</span>
            </div>
            <div class="rev-anchors scroll-y">
              <button
                v-for="(p, i) in revParas"
                :key="i"
                class="rev-anchor"
                :class="{ on: revAnchor === i + 1 }"
                @click="revAnchor = revAnchor === i + 1 ? 0 : i + 1"
              >
                <span class="rev-idx">{{ i + 1 }}</span>
                <span class="rev-quote">{{ p.slice(0, 26) }}{{ p.length > 26 ? '…' : '' }}</span>
              </button>
            </div>
          </div>

          <div class="rev-field">
            <div class="rev-field-head">
              <span>② 修改 / 新增要求<span class="muted">（唯一指令来源）</span></span>
              <span class="rev-counter">{{ revRequirement.length }} / {{ revReqLimit }}</span>
            </div>
            <NSelect
              v-model:value="revMode"
              size="small"
              :options="revModeOptions"
              style="width: 160px"
            />
            <NInput
              v-model:value="revRequirement"
              type="textarea"
              :rows="4"
              size="small"
              placeholder="例：在第 1 段之后补一段院子里的雨与灯的环境描写，约 200 字；前面的句子一句都不要动"
            />
            <div class="row-gap">
              <button
                v-for="q in REV_QUICK"
                :key="q"
                class="ghost-btn"
                @click="revRequirement = q"
              >
                {{ q }}
              </button>
            </div>
          </div>

          <div class="rev-field">
            <div class="rev-field-head">
              <span>③ 自定义约束<span class="muted">（优先级最高）</span></span>
              <span class="rev-counter">
                {{ revPickedSkills.length }} 条来自约束库 / 手写 {{ revConstraints.trim().length }} 字
              </span>
            </div>
            <div v-if="revSkillLibrary.length" class="rev-skills scroll-y">
              <label
                v-for="s in revSkillLibrary"
                :key="s.skill_id"
                class="rev-skill"
                :class="{ on: revPickedSkills.includes(s.skill_id) }"
              >
                <input v-model="revPickedSkills" type="checkbox" :value="s.skill_id" />
                <span class="rev-skill-title">{{ s.title }}</span>
                <span v-if="revBoundSkillIds.includes(s.skill_id)" class="rev-skill-tag" title="这条已经绑给了当前作品">
                  已绑本书
                </span>
              </label>
            </div>
            <div v-else class="muted">
              约束库还是空的 —— 可以在左侧「风格工坊 → 技能包与约束」新建，
              或直接在下面手写（手写的同样按最高优先级执行）。
            </div>
            <NInput
              v-model:value="revConstraints"
              type="textarea"
              :rows="3"
              size="small"
              placeholder="- **句式**：不连续三句用同一主语&#10;- **用词**：不使用「仿佛」「宛如」这类比喻堆砌"
            />
            <div class="muted">
              勾选的库条目与手写条目会<b>一起</b>送进去，都按最高优先级执行；
              约束与要求冲突时<b>按约束执行</b>。写成分行条目会被渲染成执行清单，逐条核对。
            </div>
          </div>

          <details class="rev-adv">
            <summary>④ 高级（文风样本 / 锁定片段）</summary>
            <div class="rev-field">
              <div class="rev-field-head"><span>文风样本<span class="muted">（可选：另给一段语感样本）</span></span></div>
              <NInput
                v-model:value="revStyleSample"
                type="textarea"
                :rows="3"
                size="small"
                placeholder="留空即按原文的客观文风指标（句长 / 短句占比 / 对白比例 / 段长）来改"
              />
            </div>
            <div class="rev-field">
              <div class="rev-field-head"><span>锁定片段<span class="muted">（每行一条，逐字不许动）</span></span></div>
              <NInput
                v-model:value="revExtraLocks"
                type="textarea"
                :rows="2"
                size="small"
                placeholder="例：「你要是回来得早，就先把火生上。」"
              />
              <label class="rev-check">
                <input v-model="revAutoLock" type="checkbox" />
                自动锁定原文里的对白（扩写时最容易被顺手改掉的就是对白）
              </label>
            </div>
          </details>

          <div class="rev-actions">
            <button class="primary-btn" :disabled="!revCanRun" @click="runRevision">
              {{ revBusy ? '改写中…' : '开始改写' }}
            </button>
            <span v-if="revBusy" class="muted">整篇回传是长调用，通常 40~120 秒，期间别关窗</span>
            <span v-else-if="revTooLong" class="muted warn">原文超过上限，请分段（一次一章）</span>
            <span v-else-if="!revSource.trim()" class="muted">① 里放入要改的正文</span>
            <span v-else-if="!revRequirement.trim()" class="muted">② 里写下你要改什么</span>
          </div>

          <template v-if="revResult">
            <div class="section-title">
              结果
              <span class="muted" style="font-weight: 400">
                {{ revResult.mode_label }}（{{ revResult.mode_reason || '自动' }}） ·
                作用范围：{{ revResult.scope_label }}{{
                  revResult.scope_reason ? `（判据「${revResult.scope_reason}」）` : ''
                }} ·
                {{ revResult.model }} · {{ revResult.elapsed }}s
              </span>
            </div>
            <div class="rev-summary">{{ revResult.summary }}</div>
            <div class="rev-stats">
              <span class="rev-stat">{{ revResult.stats.kept }} 未动</span>
              <span class="rev-stat add">{{ revResult.stats.inserted }} 新增</span>
              <span class="rev-stat edit">{{ revResult.stats.edited }} 改写</span>
              <span v-if="revResult.stats.merged" class="rev-stat warn">
                并段 {{ revResult.stats.merged }}
              </span>
              <span class="rev-stat del">{{ revResult.stats.deleted }} 删除</span>
              <span class="rev-stat">{{ revResult.stats.before_chars }} → {{ revResult.stats.after_chars }} 字</span>
            </div>
            <div class="rev-checks">
              <span :class="revResult.checks.minimal_edit.ok ? 'ok' : 'bad'">
                {{ revResult.checks.minimal_edit.ok ? '✅' : '⛔' }} 最小改动闸门
              </span>
              <span :class="revResult.checks.locked.ok ? 'ok' : 'bad'">
                {{ revResult.checks.locked.ok ? '✅' : '⛔' }} 锁定片段{{ revResult.locked ? `（${revResult.locked} 处）` : '' }}
              </span>
              <span :class="revResult.checks.whole_returned.ok ? 'ok' : 'bad'">
                {{ revResult.checks.whole_returned.ok ? '✅' : '⛔' }} 整篇回传
                （相似度 {{ revResult.checks.whole_returned.similarity }}）
              </span>
              <span :class="revResult.style_shift.ok ? 'ok' : 'warn'">
                {{ revResult.style_shift.ok ? '✅' : '⚠' }} 文风偏差
                （句长 {{ revResult.style_shift.before.avg_sentence_len }}
                → {{ revResult.style_shift.after.avg_sentence_len }}）
              </span>
            </div>
            <div v-if="revResult.checks.minimal_edit.soft" class="muted warn">
              ⚠ 有 {{ revResult.checks.minimal_edit.out_of_scope.length }} 处改动在容差内（没被要求点名）：
              <template v-for="(o, i) in revResult.checks.minimal_edit.out_of_scope" :key="i">
                {{ i > 0 ? '；' : '' }}第 {{ o.index }} 段{{ o.kind === 'merged' ? '（被并进其它段，内容还在）' : '（被改写）' }}
              </template>
              —— 建议在「改动对照」里核一眼。
            </div>
            <div v-if="revResult.stats.unchanged" class="muted warn">
              ⚠ 产出与原文完全一致：模型认为无需改动。若这不是你要的，把要求写得再具体些。
            </div>
            <div v-if="revResult.checks.minimal_edit.relaxed?.length" class="muted warn">
              ⚠ 本轮为收敛放宽过：{{ revResult.checks.minimal_edit.relaxed.join('；') }}
              —— 放宽的只有"可选"的约束，内容丢失与整篇回传的底线没有放松。
            </div>

            <div class="mode-row" style="margin-top: 8px">
              <button class="mode-card" :class="{ on: revView === 'diff' }" @click="revView = 'diff'">改动对照</button>
              <button class="mode-card" :class="{ on: revView === 'full' }" @click="revView = 'full'">全文</button>
              <button class="mode-card" :class="{ on: revView === 'summary' }" @click="revView = 'summary'">改动摘要</button>
            </div>

            <div v-if="revView === 'diff'" class="rev-diff">
              <div
                v-for="(s, i) in revDiffLines"
                :key="i"
                class="rev-line"
                :class="`is-${s.kind}`"
              >
                <span class="rev-kind">{{ REV_KIND[s.kind] }}</span>
                <div class="rev-body">
                  <div v-if="s.kind === 'edit'" class="rev-before">改前：{{ s.before }}</div>
                  <div class="rev-text">{{ s.text }}</div>
                </div>
              </div>
              <div v-if="revDiffLines.length === 0" class="muted">（无段落）</div>
            </div>

            <NInput
              v-else-if="revView === 'full'"
              :value="revResult.revised"
              type="textarea"
              :rows="14"
              size="small"
              readonly
            />

            <div v-else class="rev-summary-box">
              <div>模式：{{ revResult.mode_label }}{{ revResult.mode_reason ? `（判据「${revResult.mode_reason}」）` : '' }}</div>
              <div>
                作用范围：{{ revResult.scope_label }}{{
                  revResult.scope_label === '全篇'
                    ? '（作者的要求覆盖整篇，该动的地方都动了）'
                    : '（只准动点名处，其余逐字保留）'
                }}
              </div>
              <div>输入边界：{{ revResult.isolation }}</div>
              <div v-if="revResult.anchors.length">
                锚点：{{ revResult.anchors.map((a) => `第 ${a.index} 段（${a.quote}…）`).join('、') }}
              </div>
              <div>自定义约束：{{ revResult.constraints.count }} 条{{ revResult.constraints.declared_priority ? '（已声明最高优先级）' : '' }}</div>
              <div v-if="revResult.checks.minimal_edit.out_of_scope.length">
                未点名的改动：
                {{ revResult.checks.minimal_edit.out_of_scope.map((o) => `第 ${o.index} 段（${o.head}）`).join('；') }}
              </div>
            </div>

            <div class="row-gap" style="margin-top: 10px">
              <button class="ghost-btn" @click="copyRevised">复制全文</button>
              <button
                class="primary-btn"
                :disabled="!revTargetChapter"
                :title="revTargetChapter ? `写回第 ${revTargetChapter} 章（人工编辑，需确认）` : '未选中章节'"
                @click="writeBackRevision"
              >
                {{ revFillBack ? '回填本章草稿' : `写回第 ${revTargetChapter || '—'} 章` }}
              </button>
              <button class="ghost-btn" @click="continueRevision">继续改这版</button>
            </div>
            <div class="muted">
              写回 = <b>人工编辑</b>语义（走既有章节保存、Git 写作历史留痕、需二次确认）；
              它<b>不会</b>替你点「定稿入库」。
            </div>
          </template>

          <div class="rev-foot">
            <button class="ghost-btn" @click="revShowPrompt = !revShowPrompt">
              {{ revShowPrompt ? '收起智能体设定' : '查看智能体设定（它被要求做什么、不许做什么）' }}
            </button>
            <span class="muted">想改它的行为？在左侧「智能体设置」里改，下一轮立即生效。</span>
          </div>
          <pre v-if="revShowPrompt" class="prompt-view pre-wrap">{{ revAgent?.prompt || '（引擎未就绪，读不到智能体设定）' }}</pre>
        </template>

        <!-- 数据看板（含「墨师操作」审计页：与书无关，工程级记录） -->
        <template v-else-if="which === 'dashboard'">
          <div class="dash-tabs">
            <button class="dash-tab" :class="{ active: dashTab === 'book' }" @click="dashTab = 'book'">
              📈 作品指标
            </button>
            <button class="dash-tab" :class="{ active: dashTab === 'audit' }" @click="dashTab = 'audit'">
              🧭 墨师操作
              <span v-if="auditRecords.length" class="muted">（{{ auditRecords.length }}）</span>
            </button>
          </div>

          <template v-if="dashTab === 'audit'">
            <div class="muted" style="margin: 6px 0 8px">
              墨师通过对话执行过的动作都会留痕（含读取类）。写操作需你确认后才执行，未确认的不入账。
            </div>
            <div v-for="(r, i) in auditRecords" :key="i" class="row-card">
              <div class="col-gap">
                <span class="row-title">
                  <span class="tag" :class="r.ok ? 'ok' : ''">{{ r.ok ? '成功' : '失败' }}</span>
                  {{ auditLabel(r.op) }}
                  <span class="muted">· {{ r.scope === 'write' ? '写' : '读' }}</span>
                </span>
                <span class="muted">
                  {{ fmtAuditTime(r.ts) }} · {{ r.book || r.session || '工作区' }} · {{ r.elapsed_ms }}ms
                </span>
                <span v-if="r.summary" class="muted">{{ r.summary.slice(0, 90) }}</span>
                <span v-else-if="r.error" class="muted">⚠ {{ r.error.slice(0, 90) }}</span>
              </div>
            </div>
            <div v-if="auditRecords.length === 0" class="muted" style="padding: 4px 0">
              暂无墨师操作记录——在对话框里让墨师查资料或建书试试。
            </div>
          </template>

          <template v-else>
          <div v-if="!dashBook" class="muted">尚未打开作品。可切到「墨师操作」查看工作区里的动作记录，或在书架打开/新建一部作品。</div>
          <template v-else-if="dashboard">
            <div class="metric-grid">
              <div class="metric-card">
                <div class="metric-num">{{ dashboard.metrics.approved }}/{{ dashboard.metrics.total_chapters }}</div>
                <div class="muted">定稿 / 总章</div>
              </div>
              <div class="metric-card">
                <div class="metric-num">{{ dashboard.metrics.first_pass_rate ?? '—' }}<i v-if="dashboard.metrics.first_pass_rate !== null">%</i></div>
                <div class="muted">一次通过率</div>
              </div>
              <div class="metric-card">
                <div class="metric-num">{{ dashboard.metrics.avg_score ?? '—' }}</div>
                <div class="muted">Editor 均分</div>
              </div>
              <div class="metric-card">
                <div class="metric-num">{{ dashboard.foreshadow.resolved }}/{{ dashboard.foreshadow.total }}</div>
                <div class="muted">伏笔回收</div>
              </div>
            </div>

            <div class="section-title">章节与评分</div>
            <div v-for="ch in dashboard.chapters" :key="String(ch.chapter)" class="row-card">
              <span>第 {{ ch.chapter }} 章</span>
              <span class="muted">{{ ch.status === 'approved' ? `已定稿 · ${ch.score ?? '—'}` : '草稿' }}</span>
            </div>
            <div v-if="dashboard.chapters.length === 0" class="muted" style="padding: 4px 0">暂无章节</div>

            <div class="section-title">伏笔台账</div>
            <div v-for="f in dashboard.foreshadow.items" :key="f.id" class="row-card">
              <div class="col-gap">
                <span class="row-title">{{ f.desc }}</span>
                <span class="muted">埋于 {{ f.planted_ch ?? '?' }} 章 → 回收于 {{ f.resolve_ch ?? '?' }} 章</span>
              </div>
              <span class="tag" :class="f.status === 'resolved' ? 'ok' : ''">
                {{ f.status === 'resolved' ? '已回收' : '未回收' }}
              </span>
            </div>
          </template>
          </template>
        </template>

        <!-- 导出中心 -->
        <template v-else-if="which === 'export'">
          <div class="muted" style="margin-bottom: 10px">
            导出《{{ appStore.bookTitle || '（未选择作品）' }}》。TXT 含全部定稿章节；EPUB 按卷组装。
          </div>
          <div class="export-row">
            <button class="export-card" :disabled="exporting !== ''" @click="doExport('txt')">
              <span class="export-icon">📄</span>
              <span>导出 TXT</span>
              <small class="muted">纯文本，含分卷分隔</small>
            </button>
            <button class="export-card" :disabled="exporting !== ''" @click="doExport('epub')">
              <span class="export-icon">📚</span>
              <span>导出 EPUB</span>
              <small class="muted">电子书格式</small>
            </button>
          </div>
          <label class="check-line">
            <input v-model="includeDraft" type="checkbox" />
            包含草稿章（对话创作流中尚未走流水线定稿的章节）
          </label>
          <div class="muted" style="margin-top: 8px">
            默认只导出定稿章节；勾选后草稿章一并写入。
          </div>
        </template>
      </div>
    </aside>
  </transition>
</template>

<script lang="ts">
export default { name: 'SideWindows' }
</script>

<style scoped>
.dash-tabs {
  display: flex;
  gap: 6px;
  margin-bottom: 10px;
}
.dash-tab {
  padding: 5px 10px;
  font-size: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 999px;
  background: #fff;
  color: #475569;
  cursor: pointer;
}
.dash-tab.active {
  border-color: #2563eb;
  color: #2563eb;
  background: #eff6ff;
  font-weight: 600;
}
/*
 * 悬浮页形态（作者指定，2026-10-18 复原）。
 *
 * 与改造前那版的差别只有三处，都是纯收益：
 * ① `left` 不再写死 184px —— 原来假设导航恒为 176px，导航一折叠/变宽就错位；
 *    现在跟着栅格的 --rail-w 走，导航 56px 时浮层自动贴到左边。
 * ② 宽度不再无条件 520px：max-width 兜住窄窗口，不会顶到创作空间上。
 * ③ 头部多了「← 回到 X」+ 来源面包屑，Esc 也能关 —— 浮层盖住对话栏时，
 *    "我刚点开的东西去哪了"有据可查。
 */
.side-window {
  position: fixed;
  left: calc(var(--rail-w, 176px) + 8px);
  top: 12px;
  bottom: 12px;
  width: 520px;
  /*
   * 上限 = 对话列宽度（视口 − 导航 − 正文 − 创作空间 − 两条中线）。
   * 浮层盖住对话列是预期的；但再往右就会把正文列左缘切掉一条
   * （原版 1366 下切掉约 104px，正文那行字直接缺一半）。
   * 宽屏（≥1500 左右）对话列本来就比 520 宽，这里不生效，浮层保持 520。
   */
  max-width: calc(
    100vw - var(--rail-w, 176px) - var(--doc-w, 470px) - var(--tree-w, 288px) -
      2 * var(--splitter-w, 8px)
  );
  min-width: 300px;
  background: var(--bg-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-4);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.14);
  z-index: var(--z-drawer, 60);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.win-origin {
  padding: 6px 18px;
  font-size: var(--fs-12);
  color: var(--ink-4);
  background: var(--accent-soft);
  border-bottom: 1px solid var(--line-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.slide-enter-active,
.slide-leave-active {
  transition: all 0.18s ease;
}
.slide-enter-from,
.slide-leave-to {
  opacity: 0;
  transform: translateX(-16px);
}
.win-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 18px;
  border-bottom: 1px solid var(--line-2);
  gap: 10px;
}
.win-head-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.back-btn {
  border: 1px solid var(--line-3);
  background: var(--bg-0);
  color: var(--ink-3);
  border-radius: var(--r-1);
  padding: 3px 10px;
  font-size: var(--fs-12);
  cursor: pointer;
  font-family: inherit;
  white-space: nowrap;
  flex: 0 0 auto;
}
.back-btn:hover {
  background: var(--bg-2);
  color: var(--ink-1);
}
.win-title {
  font-weight: 700;
  font-size: var(--fs-15);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.close-btn {
  background: none;
  border: none;
  font-size: 14px;
  color: #8a8f99;
  cursor: pointer;
  padding: 2px 8px;
  border-radius: 6px;
}
.close-btn:hover {
  background: #f0f1f3;
  color: #26272b;
}
.win-body {
  flex: 1;
  min-height: 0;
  padding: 14px 18px;
}
.section-title {
  font-weight: 600;
  font-size: 13.5px;
  margin: 14px 0 8px;
}
.agent-block {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.agent-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.prompt-view {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: #5c6470;
  background: #f9fafb;
  border-radius: 8px;
  padding: 8px 10px;
  max-height: 110px;
  overflow-y: auto;
}
.row-card {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 9px 12px;
  margin-bottom: 7px;
}
.row-title {
  font-weight: 600;
  font-size: 13px;
  cursor: pointer;
}
.mode-row {
  display: flex;
  gap: 8px;
  margin-bottom: 6px;
}
.mode-card {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-align: left;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 9px 11px;
  cursor: pointer;
  font-size: 13px;
  line-height: 1.5;
}
.mode-card.on {
  border-color: #1d4ed8;
  background: #f0f5ff;
}
.mode-hint {
  font-size: 12px;
  line-height: 1.7;
  margin-bottom: 10px;
}
.tag {
  display: inline-block;
  font-size: 11px;
  border-radius: 999px;
  padding: 1px 8px;
  margin-right: 6px;
  white-space: nowrap;
}
.tag.full {
  background: #f3f4f6;
  color: #5c6470;
}
.tag.style {
  background: #f3e8ff;
  color: #6b21a8;
}
.tag.cat {
  background: #eef4ff;
  color: #1d4ed8;
}
.cat-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.cat-chip {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 999px;
  padding: 3px 12px;
  font-size: 12px;
  cursor: pointer;
  color: #5c6470;
}
.cat-chip.on {
  border-color: #1d4ed8;
  background: #f0f5ff;
  color: #1d4ed8;
  font-weight: 600;
}
.shelf-head-gap {
  margin-bottom: 8px;
}
.book-card-click {
  cursor: pointer;
}
.minor-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #5c6470;
  cursor: pointer;
}
.tag.major {
  background: #fff7ed;
  color: #b45309;
}
.book-card-click:hover {
  border-color: #1d4ed8;
  background: #f8faff;
}
.dim-block {
  margin-bottom: 14px;
}
.dim-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
  color: #3a3d44;
}
.spawn-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
/* 默认素材来源说明条：明确"这批素材来自哪本书"，避免误以为全库都会被导入 */
.spawn-source {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12.5px;
  padding: 7px 10px;
  border-radius: 8px;
  background: #eef5ff;
  border: 1px solid #d6e4ff;
  color: #24405f;
}
.spawn-source.mixed {
  background: #fff7ed;
  border-color: #fed7aa;
  color: #92400e;
}
.spawn-source-main {
  display: block;
  line-height: 1.7;
}
/* ══ 二开建书面板：三段式（新书 → 来源 → 内容清单），高级选项收起 ══ */
.spawn-panel {
  gap: 12px;
}
.spawn-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid #e8ecf3;
  border-radius: 10px;
  background: #fcfdff;
}
.spawn-step {
  font-size: 13px;
  font-weight: 600;
  color: #24405f;
}
.spawn-sub {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  flex-wrap: wrap;
}
/* 一行分类：默认只显示"已选 x/y"与三个操作；展开才显示条目 chips */
.spawn-row.off {
  opacity: 0.62;
}
.spawn-plot {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12.5px;
  padding: 8px 10px;
  border-radius: 8px;
  background: #fffaf0;
  border: 1px solid #f6e3c0;
  color: #7a4b00;
}
.spawn-plot input {
  margin-top: 3px;
}
.spawn-plot small {
  display: block;
  line-height: 1.6;
  margin-top: 2px;
  color: #8a6320;
}
.spawn-advanced {
  font-size: 12.5px;
  color: #5c6470;
}
.spawn-advanced > summary {
  cursor: pointer;
  padding: 4px 0;
}
.spawn-advanced .muted {
  margin: 4px 0 8px;
}
.advanced-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
}
.spawn-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.spawn-row {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 8px 10px;
  background: #fff;
}
.mini-select {
  font-size: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 2px 6px;
  background: #fff;
  color: #3a3d44;
  max-width: 160px;
}
.warn {
  color: #b45309;
}
.append-box,
.conflict-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid #dbe6fe;
  background: #f8faff;
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 10px;
  font-size: 13px;
  line-height: 1.7;
}
.conflict-box {
  border-color: #fcd34d;
  background: #fffbeb;
}
.conflict-row {
  border-top: 1px dashed #f0d9a0;
  padding-top: 6px;
}
.conflict-actions {
  flex-wrap: wrap;
  margin-top: 4px;
}
.conflict-edit {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 6px;
}
.resolved-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border: 1px solid #d7e8d9;
  background: #f6fbf7;
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 10px;
  font-size: 13px;
  line-height: 1.7;
}
.resolved-row {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-content: space-between;
  border-top: 1px dashed #d7e8d9;
  padding-top: 5px;
}
.row-between {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
}
.col-gap {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
  flex: 1;
}
.row-gap {
  display: flex;
  gap: 6px;
  align-items: center;
}
.tag {
  font-size: 11px;
  background: #f3f4f6;
  border-radius: 999px;
  padding: 1px 8px;
  color: #5c6470;
}
.tag.ok {
  background: #e6f6ec;
  color: #116932;
}
.ghost-btn {
  background: none;
  border: 1px solid #e5e7eb;
  border-radius: 7px;
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
  color: #3a3d44;
}
.del-btn {
  background: none;
  border: none;
  color: #dc2626;
  font-size: 12px;
  cursor: pointer;
}
.primary-btn {
  background: #1f2937;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 6px 16px;
  font-size: 12.5px;
  cursor: pointer;
}
.primary-btn:disabled {
  background: #d1d5db;
}
.view-box {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 10px 12px;
  margin-top: 10px;
  max-height: 320px;
}
.view-pre {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.7;
}
.new-block {
  border: 1px dashed #d9dce1;
  border-radius: 10px;
  padding: 10px;
  margin-top: 10px;
  display: flex;
  flex-direction: column;
}
.metric-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 6px;
}
.metric-card {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 12px;
  text-align: center;
}
.metric-num {
  font-size: 20px;
  font-weight: 700;
  color: #26272b;
}
.metric-num i {
  font-style: normal;
  font-size: 13px;
}
.export-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.export-card {
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 16px;
  background: #fff;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: center;
  font-size: 13.5px;
  color: #26272b;
}
.export-card:hover:not(:disabled) {
  border-color: #1d4ed8;
}
.export-icon {
  font-size: 22px;
}
.check-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #3a3d44;
  margin-top: 10px;
}

/* ══════════ 正文改写（第四个模块） ══════════ */
/* 侧窗（520px，fixed left:184）会盖住对话栏，而人审卡/墨师动作恰好长在对话栏里 ——
   所以入口一律带来源，面板顶部用这条面包屑把"我刚点的事去哪了"讲清楚。 */
.rev-crumb {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #1d4ed8;
  background: #eef2ff;
  border: 1px solid #c7d2fe;
  border-radius: 8px;
  padding: 6px 10px;
  margin-bottom: 10px;
}
.rev-banner {
  border: 1px solid #dbeafe;
  background: #f5f9ff;
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.rev-banner.orange {
  border-color: #fde68a;
  background: #fffbeb;
}
.rev-banner-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13.5px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.rev-scope {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
  line-height: 1.6;
}
.scope-yes { color: #116932; }
.scope-no { color: #9a3412; }
.scope-warn { color: #92400e; font-weight: 600; }

.rev-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 12px;
}
.rev-field-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 12.5px;
  font-weight: 600;
  color: #3a3d44;
}
.rev-counter {
  font-size: 11.5px;
  color: #9aa0aa;
  font-variant-numeric: tabular-nums;
}
.rev-counter.over { color: #b42318; font-weight: 700; }

/* 约束库勾选（「让我勾选我已有的约束」）：列表只读展示标题，勾中的整条内容随请求发出 */
.rev-skills {
  display: flex;
  flex-direction: column;
  gap: 4px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 6px;
  max-height: 148px;
  background: #fcfcfd;
}
.rev-skill {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border-radius: 7px;
  font-size: 12.5px;
  color: #3a3d44;
  cursor: pointer;
}
.rev-skill:hover { background: #eff6ff; }
.rev-skill.on { background: #e8f0fe; color: #1d4ed8; font-weight: 600; }
.rev-skill input { flex-shrink: 0; }
.rev-skill-title { flex: 1; min-width: 0; }
.rev-skill-tag {
  flex-shrink: 0;
  font-size: 11px;
  color: #116932;
  background: #e6f6ec;
  border-radius: 5px;
  padding: 0 6px;
}

.rev-anchors {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  max-height: 118px;
  background: #fcfcfd;
}
.rev-anchor {
  width: 100%;
  display: flex;
  gap: 8px;
  align-items: baseline;
  border: none;
  border-bottom: 1px solid #eef0f2;
  background: none;
  padding: 5px 10px;
  font-size: 12px;
  color: #5c6470;
  text-align: left;
  cursor: pointer;
}
.rev-anchor:last-child { border-bottom: none; }
.rev-anchor:hover { background: #eff6ff; }
.rev-anchor.on { background: #e8f0fe; color: #1d4ed8; font-weight: 600; }
.rev-idx {
  color: #9aa0aa;
  font-variant-numeric: tabular-nums;
  width: 18px;
  text-align: right;
  flex-shrink: 0;
}
.rev-anchor.on .rev-idx { color: #1d4ed8; }
.rev-quote { flex: 1; min-width: 0; }

.rev-adv {
  border: 1px solid #e5e7eb;
  border-radius: 9px;
  background: #fcfcfd;
  padding: 2px 12px 10px;
  margin-bottom: 12px;
}
.rev-adv > summary {
  cursor: pointer;
  padding: 7px 0;
  font-size: 12.5px;
  font-weight: 600;
  color: #3a3d44;
}
.rev-check {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  color: #3a3d44;
  margin-top: 8px;
}
.rev-check.dim { opacity: 0.5; }

.rev-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  border-top: 1px solid #eef0f2;
  padding-top: 12px;
}
.rev-summary {
  font-size: 13px;
  line-height: 1.7;
  color: #26272b;
  background: #f6f7f9;
  border: 1px solid #eef0f2;
  border-radius: 8px;
  padding: 8px 11px;
  margin-bottom: 8px;
}
.rev-stats {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 11.5px;
  margin-bottom: 8px;
}
.rev-stat {
  border-radius: 6px;
  padding: 2px 8px;
  background: #f3f4f6;
  color: #5c6470;
}
.rev-stat.add { background: #e6f6ec; color: #116932; }
.rev-stat.edit { background: #fef6e0; color: #8a5a00; }
.rev-stat.del { background: #fdebec; color: #b42318; }
.rev-stat.warn { background: #fffbeb; color: #92400e; }
.rev-checks {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 11.5px;
  margin-bottom: 8px;
}
.rev-checks .ok { color: #116932; }
.rev-checks .bad { color: #b42318; font-weight: 600; }
.rev-checks .warn { color: #92400e; }

.rev-diff {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow: hidden;
  font-size: 12.5px;
  line-height: 1.85;
  margin-top: 8px;
  max-width: 100%;
}
/* 三段式：行首标签（定宽）+ 正文列（自己吃满剩余宽度）。
   坑（用户实测截图：正文被挤成每行一个字）：原先把「改前：…」与「改后：…」当同一行的
   两个 flex 子项，长文本的 min-width:auto 会把另一个子项挤到 ~1 字宽。
   正文必须是**列容器**，并显式 min-width:0 + overflow-wrap。 */
.rev-line {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 5px 10px;
  border-bottom: 1px solid #eef0f2;
}
.rev-line:last-child { border-bottom: none; }
.rev-kind {
  flex: 0 0 26px;
  font-size: 11px;
  color: #9aa0aa;
  text-align: right;
}
.rev-body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.rev-text,
.rev-before {
  min-width: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.rev-before {
  color: #9a3412;
  text-decoration: line-through;
  font-size: 12px;
}
.rev-line.is-keep { background: #fff; }
.rev-line.is-insert { background: #e6f6ec; }
.rev-line.is-insert .rev-kind { color: #116932; }
.rev-line.is-edit { background: #fef6e0; }
.rev-line.is-edit .rev-kind { color: #8a5a00; }
.rev-line.is-delete { background: #fdebec; }
.rev-line.is-delete .rev-text { color: #b42318; text-decoration: line-through; }
.rev-line.is-delete .rev-kind { color: #b42318; }
.rev-summary-box {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 12.5px;
  line-height: 1.7;
  color: #3a3d44;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 10px 12px;
}
.rev-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  border-top: 1px solid #eef0f2;
  margin-top: 14px;
  padding-top: 12px;
}
</style>
