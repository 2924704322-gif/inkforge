/** 引擎（Python sidecar）API 的领域类型。与 engine/src 各端点返回结构一一对应。 */

// ---------- 书架 ----------

export interface Book {
  novel_id: string
  title: string
  chapters: number
  approved: number
  active: boolean
  finished: boolean
  interactive: boolean
  is_default: boolean
  /** 已绑定的自定义创作约束条数（0 = 本书没有任何约束在生效） */
  bound_constraints?: number
}

export interface CustomSkill {
  skill_id: string
  title: string
  content?: string
}

/**
 * X1 结构化 brief（W6）：以字段替代单框文本。
 * 键与引擎侧 `architect.BRIEF_FIELDS` / `server.BriefFieldsBody` 一一对应（手写双份）。
 */
export interface BriefFields {
  genre?: string        // 体裁
  pov?: string          // 视角
  tone?: string         // 基调
  protagonist?: string  // 主角
  antagonist?: string   // 对立面
  setting?: string      // 设定
  themes?: string       // 主题
  arc?: string          // 期望弧线
  avoid?: string        // 明确不要的写法
  /**
   * 现实性口径开关（默认 false = 以作者创作目标为准）。
   *
   * 由来（用户要求）："面对用户可能不符合实际的创作需求，不需要因为现实原因修改"。
   * 默认**不**把现实合理性当评判依据；勾上才允许审校按现实逻辑提建议。
   * 它是程序读回的布尔开关，**不进** BRIEF_FIELD_DEFS 的文本字段循环
   * （那个循环会对值调用 .trim()，混进布尔会炸）。
   */
  allow_realism?: boolean
}

/** 结构化 brief 里**文本类**字段的键（排除布尔开关 allow_realism）。 */
export type BriefTextFieldKey = Exclude<keyof BriefFields, 'allow_realism'>

/** 结构化 brief 字段编辑顺序与中文标签（与引擎侧一致）。 */
export const BRIEF_FIELD_DEFS: { key: BriefTextFieldKey; label: string; placeholder: string }[] = [
  { key: 'genre', label: '体裁', placeholder: '例：东方玄幻 / 冷硬派推理 / 惊悚' },
  { key: 'pov', label: '视角', placeholder: '例：第三人称限知，紧贴主角 / 反派第一人称' },
  { key: 'tone', label: '基调', placeholder: '例：冷硬克制 / 阴暗压抑 / 温柔治愈' },
  { key: 'protagonist', label: '主角', placeholder: '例：废柴剑修，隐忍、记仇、有底线' },
  { key: 'antagonist', label: '对立面', placeholder: '例：同门师兄，表面温厚实则夺权' },
  { key: 'setting', label: '设定', placeholder: '例：灵气枯竭的末法时代，宗门垄断灵脉' },
  { key: 'themes', label: '主题', placeholder: '例：复仇与代价 / 身份与自由' },
  { key: 'arc', label: '期望弧线', placeholder: '例：从隐忍到失控，最终以代价收场' },
  { key: 'avoid', label: '明确不要的写法', placeholder: '例：不要说教、不要金手指、不要大团圆' },
]

export function emptyBriefFields(): BriefFields {
  return {
    genre: '',
    pov: '',
    tone: '',
    protagonist: '',
    antagonist: '',
    setting: '',
    themes: '',
    arc: '',
    avoid: '',
    allow_realism: false,
  }
}

export function hasBriefFields(fields: BriefFields): boolean {
  // 只看文本字段：布尔开关（allow_realism）不构成"填了创作需求"
  return BRIEF_FIELD_DEFS.some((d) => (fields[d.key] ?? '').trim().length > 0)
}

// ---------- 流水线状态与审阅 ----------

export interface Metrics {
  total_chapters: number
  approved: number
  first_pass_rate: number | null
  avg_score: number | null
}

export interface ReviewHistoryItem {
  chapter: number
  attempt: number
  overall: number
}

export interface WaveReportInfo {
  wave: number
  volumes: number[]
  consistent: boolean | null
  issue_count: number
}

export interface ReviewIssue {
  dimension: 'consistency' | 'plot' | 'continuity' | 'prose' | 'length'
  severity: 'major' | 'minor'
  description: string
  quote: string
  suggestion: string
}

export interface ChapterReview {
  consistency: number
  plot: number
  continuity: number
  prose: number
  length: number
  issues: ReviewIssue[]
  comment: string
}

export interface ChapterPlanInfo {
  chapter: number
  title: string
  outline: string
  characters: string[]
  /** 本章预期字数（大纲阶段给出，生成前可改）——写作/评分/字数门禁共用同一目标 */
  target_words?: number | null
}

export interface VolumePlanInfo {
  volume: number
  title: string
  depends_on?: number[]
  chapters: ChapterPlanInfo[]
}

export interface ForeshadowItemInfo {
  id: string
  desc: string
  planted_ch: number
  resolve_ch: number
}

export interface OutlinePayload {
  book_title: string
  theme: string
  volumes: VolumePlanInfo[]
  foreshadowing?: ForeshadowItemInfo[]
}

export type PendingItem =
  | ({ type: 'outline_review'; outline: OutlinePayload } & Record<string, unknown>)
  | ({
      /** 逐章确认关卡：上一章已定稿，等用户在对话框发指令才写下一章 */
      type: 'chapter_gate'
      approved_chapter: number
      next_chapter: number
      /** 下一章预期字数（大纲预算；生成前可在关卡上改） */
      target_words?: number | null
      planned_target_words?: number | null
      /** 可接受字数区间（下浮 ≤500 / 上浮 ≤2000），由引擎下发，前端只负责显示 */
      length_floor?: number | null
      length_ceiling?: number | null
    } & Record<string, unknown>)
  | ({
      type: 'chapter_review'
      chapter: number
      volume: number
      draft_text: string
      review: ChapterReview
      retry_exceeded?: boolean
      attempt: number
      /** 本章预期字数（写作/评分/门禁同一目标）；打回时改它会按新目标重写 */
      target_words?: number | null
      /** 本章实际字数（引擎侧统计，与目标并列展示"偏差"） */
      actual_length?: number | null
      /** 字数可接受区间（引擎按非对称口径下发） */
      length_floor?: number | null
      length_ceiling?: number | null
      model?: string
      used_fallback?: boolean
      previous_draft?: string
      previous_attempt?: number
    } & Record<string, unknown>)

export interface StatusSnapshot {
  novel_id: string
  started: boolean
  done: boolean
  error: string | null
  pending: PendingItem | null
  parallel: boolean
  last_wave: WaveReportInfo | null
  review_history: ReviewHistoryItem[]
  metrics?: Metrics
}

export interface QueueItem {
  chapter: number
  volume: number
  title: string
  attempt: number
  overall?: number
}

// ---------- 章节 / 大纲 ----------

export interface ChapterSummary {
  chapter: number
  volume: number
  title: string
  status: string
  score: number | null
  first_review_passed: boolean | null
  model: string | null
  used_fallback: boolean
}

export interface ChapterDetail {
  chapter: number
  title: string
  status: string
  score: number | null
  model: string | null
  used_fallback: boolean
  content_html: string
  review_html: string | null
}

// ---------- 创作向导（设定 Demo） ----------

export interface WorldviewDocInfo {
  filename: string
  title: string
  content: string
}

export interface CharacterInfo {
  name: string
  role: string
  appearance: string
  personality: string
  background: string
  level?: string
  location?: string
  items?: string[]
  relations?: Record<string, string>
}

export interface DemoOutput {
  book_title: string
  synopsis: string
  overview: string
  theme: string
  worldview: WorldviewDocInfo[]
  characters: CharacterInfo[]
}

export interface DemoSnapshot {
  status: 'idle' | 'running' | 'done' | 'error' | 'confirmed'
  error: string | null
  demo: DemoOutput | null
}

// ---------- 模型配置 ----------

export interface RoleBinding {
  role: string
  provider: string
  model: string
  temperature: number
  max_tokens?: number
  fallback?: { provider: string; model: string }
}

export interface ProviderInfo {
  name: string
  type: string
  base_url?: string
  api_key?: string
}

export interface ModelConfigView {
  providers: ProviderInfo[]
  roles: RoleBinding[]
  path: string
}

// ---------- 蒸馏（NDS） ----------

export interface SkillIndexEntry {
  skill_id: string
  book_title: string
  version: string
  status: string
  path?: string
}

export interface DistillStatusInfo {
  skill_id: string
  status: string
  progress?: string
  error?: string | null
  book_title?: string
}

export interface DistillInitResult {
  skill_id: string
  book_title: string
  total_chunks: number
  status: string
}

export interface DistillReport {
  skill_id: string
  manifest: Record<string, unknown>
  full_report: Record<string, unknown>
}

export const DIMENSION_LABELS: Record<string, string> = {
  world: '① 世界构建',
  plot: '② 情节结构',
  theme: '③ 主题星系',
  narrative: '④ 叙事架构',
  characters: '⑤ 人物系统',
  environment: '⑥ 环境与空间',
  env_description: '⑦ 环境描写技法',
  char_description: '⑧ 人物描写技法',
  dialogue: '⑨ 对话艺术',
  action: '⑩ 动作与场景调度',
  style: '⑪ 语体与语气',
  rhythm: '⑫ 节奏与韵律',
  sensory: '⑬ 感官与情感地图',
  time_memory: '⑭ 时间与记忆编码',
  meta_narrative: '⑮ 元叙事与互文性',
  ideology: '⑯ 意识形态与价值观',
}

// ─────────── Inkforge 扩展（对话 / 资料库 / 绑定 / 互动） ───────────

export interface ChatSummary {
  id: string
  agent: string
  agent_label: string
  title: string
  updated: number
  scope?: 'book' | 'workspace'
  novel?: string
}

export interface ChatDelegation {
  agent: string
  label: string
  instruction: string
  output: string
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  ts?: number
  delegations?: ChatDelegation[]
  /** 墨师动作回执（P2/P3）：本轮执行/登记的动作结果 */
  actions?: ActionReceipt[]
  action_result?: { op: string; ok: boolean; status: string; error?: string }
}

/** 墨师动作回执（与 engine/src/web/actions.py 的 ActionExec 对应）。 */
export interface ActionReceipt {
  op: string
  status: 'ok' | 'pending_confirm' | 'failed'
  ok: boolean
  summary?: string
  error?: string
  data?: unknown
}

/** 待用户确认的写动作（确认后才真正执行）。 */
export interface PendingAction {
  op: string
  args: Record<string, unknown>
  impact: string
}

/** 数据看板「墨师操作」页的记录（/api/actions/audit）。 */
export interface ActionAuditRecord {
  ts: number
  session: string
  book: string
  op: string
  scope: string
  status: string
  ok: boolean
  elapsed_ms: number
  summary?: string
  error?: string
}

export interface ChatData {
  id: string
  agent: string
  title: string
  created: number
  scope?: 'book' | 'workspace'
  novel?: string
  messages: ChatMessage[]
  pending?: PendingAction | null
}

export interface SettingsTreeItem {
  rel: string
  title: string
  kind: string
}

export interface SettingsDoc {
  rel: string
  title: string
  content: string
}

export interface ChapterRaw {
  chapter: number
  title: string
  status: string
  content: string
}

export interface Bindings {
  bound_custom: string[]
  bound_packs: string[]
}

// ---------- 风格工坊 · 约束提炼（第三个模块） ----------

/**
 * 约束提炼智能体名片（`GET /api/style-forge/agent`）。
 *
 * `sees` / `never_sees` 是引擎侧对"这个智能体到底看得到什么"的显式声明（2026-09-19 订正）：
 * **看什么由你决定** —— 你在【正文内容】框里粘贴的文本它才看得到；
 * 它不会自己去翻你的作品库（正文/章节/大纲/设定/素材一律读不到）。
 */
export interface ForgeAgentInfo {
  key: string
  label: string
  role: string
  prompt: string
  max_items: { default: number; min: number; max: number }
  /** 两个输入框的字数限制（正文 / 需求） */
  limits: { source_max: number; source_min: number; requirement_max: number }
  sees: string[]
  never_sees: string[]
}

/** 一次提炼的结果（`POST /api/style-forge/constraints`）。 */
export interface ForgeResult {
  ok: boolean
  agent: { key: string; label: string }
  /** 名称建议（由提炼需求首句确定性生成，用户可改） */
  title: string
  /** 可直接复制/入库的条目文本（`- ` 条目行） */
  constraints: string
  items: string[]
  count: number
  role: string
  model: string
  provider: string
  elapsed: number
  /** 引擎回执的输入边界复述：只用了本页粘贴的文本，未读作品库 */
  isolation: string
}

export interface PlotCard {
  card_id: string
  title: string
  tag: string
  outline: string
  hook: string
  characters: string[]
}

export interface InteractiveDraft {
  draft_text?: string
  attempt?: number
  review?: ChapterReview
  model?: string
  /** 本章实际字数（引擎统计） */
  words?: number
  /** 本章目标字数（生成前可设；打回重写时沿用同一个目标） */
  target_words?: number | null
  /** 字数可接受区间（引擎按非对称口径下发） */
  length_floor?: number | null
  length_ceiling?: number | null
  /** 与目标字数的差值 */
  length_deviation?: number | null
}

export interface InteractiveState {
  status: string
  chapter: number
  cards?: PlotCard[]
  draft?: InteractiveDraft | null
  approved_count?: number
  /** 本章目标字数（选卡前可设，生成后回填实际生效值） */
  target_words?: number | null
  /** 本章可接受字数区间（引擎按非对称口径下发：下浮 ≤500 / 上浮 ≤2000） */
  length_floor?: number | null
  length_ceiling?: number | null
  error?: string | null
  /** 失败归因：network / provider_auth / config / parse / engine（引擎 classify_failure） */
  error_source?: string
  /** 可执行提示：告诉用户下一步该做什么（重试 / 查配置 / 导出日志） */
  error_hint?: string
  /** 本次会话绑定的 角色→provider/model，排障用 */
  models?: Record<string, string>
}


export interface AgentPreset {
  key: string
  label: string
  prompt: string
  custom: boolean
}

export interface Material {
  id: string
  title: string
  content: string
  /** 素材分类：世界观 / 人物 / 道具地点 / 桥段 / 技法 / 文风 / 其他（决定二开建书落盘去向） */
  category?: string
  /** 来源：手工 / 墨师 / 蒸馏:<任务>#<章节> */
  source?: string
  /** 归属书籍（素材库按书浏览）；手工新增的为「无来源（手工）」 */
  source_book?: string
  /** 重要度：主级（反复出现/被强调）/ 次级（只出现一次）——道具地点默认只显示主级 */
  importance?: '主级' | '次级'
  /** 由哪几章蒸馏而来 */
  chapters?: number[]
  /** 被重复验证的次数 */
  hits?: number
  updated?: number
}

/** 素材库第一屏：按来源书籍分组 */
export interface MaterialBook {
  book: string
  total: number
  updated: number
  counts: Record<string, number>
  chapters: number[]
}

export interface LearningSource {
  seq: number
  label: string
  chars: number
  distilled_at?: number
  sha256?: string
}

export interface LearningConflict {
  section: string
  existing: string
  existing_chapters?: number[]
  incoming: string
  incoming_chapter: number
  overlap?: number
}

export interface LearningResolution {
  section: string
  action: string
  existing?: string
  incoming?: string
  final_text?: string
  before_text?: string
  resolved_at?: number
}

export interface LearningItemRow {
  id: string
  section: string
  text: string
  chapters: number[]
  hits: number
}

export interface LearningItem {
  id: string
  title: string
  created?: number
  updated?: number
  content?: string
  /** full=全量蒸馏学习（带原书设定/剧情）/ style=只学通用写法（文风+技法，不含原书内容） */
  mode?: 'full' | 'style'
  mode_label?: string
  /** style 模式下被净化的专有名词条数 */
  scrubbed?: number
  /** 已投喂的章节数（累积式蒸馏：一条成果可多次追加） */
  sources?: LearningSource[] | number
  /** 累计条目数（列表里是计数，详情里是数组） */
  items?: LearningItemRow[] | number
  /** 蒸馏产出并入库的素材引用（标题 + 分类） */
  materials?: { title: string; category: string }[] | number
  /** 待裁决冲突（同一条目出现不同说法） */
  conflicts?: LearningConflict[] | number
  /** 已裁决历史（可撤销） */
  resolved?: LearningResolution[]
  /** 旧格式成果（追加时会自动升级为条目化） */
  legacy?: boolean
  changelog?: string[]
}

export interface DashboardChapter {
  chapter: number | null
  attempt: number | null
  score: number | null
  status: string | null
  model: string | null
}

export interface DashboardData {
  metrics: {
    total_chapters: number
    approved: number
    first_pass_rate: number | null
    avg_score: number | null
  }
  chapters: DashboardChapter[]
  foreshadow: {
    total: number
    resolved: number
    items: {
      id: string
      desc: string
      status: string
      planted_ch: number | null
      resolve_ch: number | null
    }[]
  }
}
