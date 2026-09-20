<script setup lang="ts">
import { NInput, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'

import { api } from '../api'
import { appStore, openBook as openWorkspaceBook, type SideWindowId } from '../store'
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
  DashboardData,
  LearningConflict,
  LearningItem,
  LearningResolution,
  LearningSource,
  Material,
  MaterialBook,
} from '../types'

/**
 * 左侧功能导航点击后弹出的小窗页面（一次一个）。
 * 窗体为悬浮面板，覆盖在对话栏之上，带标题与关闭按钮。
 */

const show = computed(() => appStore.sideWindow !== null)

const message = useMessage()

const which = computed<SideWindowId>(() => appStore.sideWindow)

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

async function loadAll(): Promise<void> {
  try {
    const jobs: Promise<void>[] = [loadPresets(), loadLearning(), loadMaterials()]
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
    void loadAll()
  },
)
watch(
  () => appStore.bookId,
  () => {
    if (appStore.sideWindow === 'dashboard') void loadDashboard()
  },
)
</script>

<template>
  <transition name="slide">
    <aside v-if="show && which" class="side-window">
      <div class="win-head">
        <span class="win-title">
          {{
            ({
              agents: '智能体设置',
              learning: '学习仿写',
              materials: '素材库',
              dashboard: '数据看板',
              export: '导出中心',
            } as Record<string, string>)[which]
          }}
        </span>
        <button class="close-btn" @click="appStore.sideWindow = null">✕</button>
      </div>

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
.side-window {
  position: fixed;
  left: 184px;
  top: 12px;
  bottom: 12px;
  width: 520px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 14px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.14);
  z-index: 60;
  display: flex;
  flex-direction: column;
  overflow: hidden;
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
  padding: 14px 18px;
  border-bottom: 1px solid #eef0f2;
}
.win-title {
  font-weight: 700;
  font-size: 15px;
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
</style>
