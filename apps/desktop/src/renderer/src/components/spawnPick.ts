/**
 * 二开建书的「来源与勾选」纯逻辑（与 UI 解耦，便于无浏览器回归）。
 *
 * 两次用户实测驱动的演进：
 *   · 第一版：每个分类各自带一个「来源书」下拉，默认全是"全部来源书"，
 *     预勾选又拿全库素材铺满 → "只选了一本书"却导入多本书的素材；
 *   · 第二版（当前）：界面只保留**一个来源书**，默认 = 当前书，
 *     分类清单只负责"选哪些内容"，跨书混搭收进"高级"。
 *
 * 规则（不可回退）：
 * 1. 默认来源 = 当前书；没有当前书时明确是"全部来源书"，不冒充当前书；
 * 2. 预勾选只从来源书里取（桥段默认不勾）；
 * 3. 换来源必须**按新来源重新预勾选**（用户要的是一键回到干净状态），
 *    单分类改来源则清空该分类（避免残留上一本书的 id 被静默导入）。
 */

export interface SpawnMaterial {
  id: string
  /** 素材分类（旧数据可能缺省 → 不参与任何分类池） */
  category?: string | null
  /** 来源书；空串 = 无来源（手工素材） */
  source_book?: string | null
}

/** 分类 → 已勾选的素材 id。 */
export type SpawnPicks = Record<string, string[]>

/** 分类 → 单独指定的来源书（仅"高级"里逐行改过时才有键）。 */
export type SpawnOverrides = Record<string, string>

/** 桥段默认不勾：它是原著最"像"的部分，导入后容易贴着原著情节走。 */
export const SPAWN_DEFAULT_UNCHECKED_CATEGORY = '桥段'

/** 某来源书的素材池（book='' = 全库）。 */
export function spawnPool(
  materials: SpawnMaterial[],
  category: string,
  book: string,
): SpawnMaterial[] {
  const items = materials.filter((m) => m.category === category)
  return book ? items.filter((m) => (m.source_book ?? '') === book) : items
}

/** 某分类的**实际**来源书（考虑逐行覆盖）。 */
export function catSourceBook(
  overrides: SpawnOverrides,
  category: string,
  sourceBook: string,
): string {
  return overrides[category] ?? sourceBook
}

/** 打开弹窗/切换来源时的默认勾选：来源书内该分类全部素材（桥段除外）。 */
export function defaultSpawnPicks(
  materials: SpawnMaterial[],
  categories: string[],
  book: string,
): SpawnPicks {
  const picks: SpawnPicks = {}
  for (const cat of categories) {
    picks[cat] = cat === SPAWN_DEFAULT_UNCHECKED_CATEGORY
      ? []
      : spawnPool(materials, cat, book).map((m) => m.id)
  }
  return picks
}

/** 单分类改来源：清空该分类勾选（避免残留上一本书的 id 被静默导入）。 */
export function setCatSource(
  picks: SpawnPicks,
  overrides: SpawnOverrides,
  category: string,
  book: string,
  sourceBook: string,
): { picks: SpawnPicks; overrides: SpawnOverrides } {
  const nextOverrides = { ...overrides }
  if (book === sourceBook) delete nextOverrides[category]
  else nextOverrides[category] = book
  return { picks: { ...picks, [category]: [] }, overrides: nextOverrides }
}

/** 提交给引擎的素材 id 列表（按分类顺序去重）。 */
export function spawnSelectedIds(picks: SpawnPicks, categories: string[]): string[] {
  const out: string[] = []
  for (const cat of categories) {
    for (const id of picks[cat] ?? []) if (!out.includes(id)) out.push(id)
  }
  return out
}

/** 当前是否"所有分类都取自同一本书"（来源条状态显示用）。 */
export function allFromBook(
  picks: SpawnPicks,
  overrides: SpawnOverrides,
  categories: string[],
  book: string,
): boolean {
  if (!book) return false
  return categories.every((cat) => catSourceBook(overrides, cat, book) === book)
}

/** 实际用到的来源书清单（'' 表示"全部来源书"）。 */
export function spawnSourceBooks(
  overrides: SpawnOverrides,
  categories: string[],
  sourceBook: string,
): string[] {
  return [...new Set(categories.map((cat) => catSourceBook(overrides, cat, sourceBook)))]
}
