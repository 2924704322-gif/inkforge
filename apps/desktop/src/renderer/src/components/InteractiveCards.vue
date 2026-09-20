<script setup lang="ts">
import { computed, ref } from 'vue'

import { NInput, NInputNumber, NRadio, NRadioGroup, useMessage } from 'naive-ui'
import { api, lengthBounds, scoreColor, withNovel } from '../api'
import { useWordTarget } from '../composables/useWordTarget'
import type { InteractiveState, PlotCard } from '../types'

/**
 * 互动创作的「智能体实时操作卡」：普通创作与互动创作共用同一工作区，
 * 由智能体（剧情策划 Plotter）在本卡片中出卡、写章、送审。
 * 状态来自轮询 /api/interactive/state，所有动作自包含。
 */

const props = defineProps<{
  novelId: string
  state: InteractiveState
}>()

const emit = defineEmits<{ refresh: []; exit: [] }>()

const customText = ref('')
const redrawFeedback = ref('')
const rejectFeedback = ref('')
const busy = ref(false)
const showCustom = ref(false)
const message = useMessage()

// ---------- 预期字数（生成前可设；打回可改）----------
// 走 useWordTarget：用户改过之后，**任何服务端快照刷新/组件重挂载都不会把数字顶回去**
// （用户实测：手改后点一下输入框外面就弹回 3000 —— 根因就是轮询快照覆盖编辑态）。
/** 引擎当前生效的目标（未开始时为服务默认值）。 */
const serverTarget = computed(() => props.state.target_words ?? null)
/** 本条草稿回带的目标（写作/评分/门禁实际用的那个数）。 */
const draftTarget = computed(() => props.state.draft?.target_words ?? null)
const draftKey = computed(
  () => `${props.state.chapter}-${props.state.draft?.attempt ?? 0}`,
)

/** 生成前设定（选卡阶段）：随章重置，避免上一章的字数悄悄带进新章。 */
const preTarget = useWordTarget({
  suggested: serverTarget,
  effective: serverTarget,
  resetWhen: computed(() => props.state.chapter),
})
/** 打回重写时改目标：随章/随稿重置，未改则沿用引擎生效值。 */
const reviewTarget = useWordTarget({
  suggested: computed(() => draftTarget.value ?? serverTarget.value),
  effective: computed(() => draftTarget.value ?? serverTarget.value),
  resetWhen: draftKey,
})
/** 打回模式：定向修订（保留未点名内容）/ 整章重写（不携带上一稿）。 */
const revisionMode = ref<'targeted' | 'rewrite'>('targeted')

// 顶层化 ref（模板里不必写 .value；组合式函数内部仍是完整对象）
const preWords = preTarget.display
const preWordsDirty = preTarget.dirty
const reviewWords = reviewTarget.display
const reviewWordsDirty = reviewTarget.dirty

/** 本条草稿的字数体检：目标 / 实际 / 可接受区间。 */
const draftBounds = computed(() =>
  lengthBounds(
    draftTarget.value ?? serverTarget.value,
    props.state.draft?.words ?? (props.state.draft?.draft_text || '').length,
    props.state.draft?.length_floor,
    props.state.draft?.length_ceiling,
  ),
)

/** 生成前设定的字数落到区间上的提示（下浮 500 是硬线，上浮 2000 内都算合格）。 */
const boundsHint = computed(() => {
  const t = preTarget.display.value ?? serverTarget.value
  if (!t) return '生成前可设定本章预期字数'
  const b = lengthBounds(t, null, props.state.length_floor, props.state.length_ceiling)
  return b ? `可接受 ${b.floor}-${b.ceiling} 字（下浮不超过 500）` : ''
})

const DIMS = [
  { key: 'consistency', label: '设定一致性' },
  { key: 'plot', label: '大纲符合度' },
  { key: 'continuity', label: '衔接连贯性' },
  { key: 'prose', label: '文笔质量' },
] as const

/**
 * 一键把审校建议并入打回意见。
 *
 * 由来（用户实测）：多维评分给出的「建议」是分散条目，用户手动复制贴进打回框时
 * 容易漏项、也容易被自己重写措辞——Writer 收到的意见与审校实际建议不一致，
 * 于是"再次生成的内容仍有较大问题"。这里把逐条建议按序号原样合并，保证
 * 送进重写链路的意见 == 审校看到的建议。
 */
const mergeSuggestions = (): void => {
  const issues = props.state.draft?.review?.issues ?? []
  const lines = issues.map((it, i) => {
    const dim = DIMS.find((d) => d.key === it.dimension)?.label ?? it.dimension
    const head = `${i + 1}. [${dim}] ${it.description}`
    return it.suggestion ? `${head}\n   → 建议：${it.suggestion}` : head
  })
  if (!lines.length) return
  const block = `【按审校建议逐条修改】\n${lines.join('\n')}`
  rejectFeedback.value = rejectFeedback.value.trim()
    ? `${rejectFeedback.value.trim()}\n\n${block}`
    : block
  message.success(`已并入 ${lines.length} 条审校建议，可再补充你自己的要求`)
}

const STATUS_LABEL: Record<string, string> = {
  idle: '未开始',
  generating_cards: '正在设计剧情卡',
  awaiting_choice: '等待选卡',
  writing: '正在写章',
  awaiting_review: '等待人审',
  committing: '定稿入库中',
  error: '异常',
  done: '完本',
}

const statusText = computed(() => STATUS_LABEL[props.state.status] ?? props.state.status)

/** 失败来源中文名（引擎 classify_failure 给出 error_source；缺省当"引擎内部异常"）。 */
const SOURCE_LABEL: Record<string, string> = {
  network: '模型服务连接失败',
  provider_auth: '接入点鉴权失败',
  config: '模型配置错误',
  parse: '模型输出无法解析',
  engine: '引擎内部异常',
}
const sourceLabel = computed(() => SOURCE_LABEL[props.state.error_source ?? ''] ?? '引擎内部异常')

/** 本次会话绑定的接入点（plot/writer/editor），排障时要能一眼看到是哪个点在失败。 */
const modelText = computed(() => {
  const models = props.state.models ?? {}
  return Object.entries(models)
    .map(([role, m]) => `${role}=${m}`)
    .join(' · ')
})

async function call(action: () => Promise<void>, label: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    await action()
    emit('refresh')
  } catch (err) {
    // 关键：这里以前只有 finally、没有 catch —— 互动创作的失败会变成**静默的
    // unhandled rejection**：用户看不到任何提示，卡片停在原状态，只能以为"卡死了"。
    message.error(`${label}失败：${err instanceof Error ? err.message : String(err)}`)
    emit('refresh')
  } finally {
    busy.value = false
  }
}

const start = (): void => {
  void call(
    () => api('POST', withNovel('/api/interactive/start', props.novelId)),
    '启动互动创作',
  )
}

/** 清掉引擎侧 error 态后再重新 start：断点由 MD 事实源自证，草稿/剧情卡都不会丢。 */
const retry = (): void => {
  void call(async () => {
    await api('POST', withNovel('/api/interactive/reset', props.novelId))
    await api('POST', withNovel('/api/interactive/start', props.novelId))
  }, '重试')
}

const choose = (cardId: string, custom = ''): void => {
  void call(async () => {
    await api('POST', withNovel('/api/interactive/choose', props.novelId), {
      card_id: cardId,
      custom_text: custom,
      // 生成前设定的预期字数（undefined 时省略该键 → 引擎回落默认值）
      target_words: preTarget.payload.value,
    })
    customText.value = ''
  }, '选卡写章')
}

const redraw = (): void => {
  void call(async () => {
    await api('POST', withNovel('/api/interactive/redraw', props.novelId), {
      feedback: redrawFeedback.value.trim(),
    })
    redrawFeedback.value = ''
  }, '重抽剧情卡')
}

const decide = (action: 'approve' | 'reject'): void => {
  void call(async () => {
    await api('POST', withNovel('/api/interactive/decision', props.novelId), {
      action,
      feedback: rejectFeedback.value.trim(),
      // 打回时可选「整章重写」（此前硬编码 targeted，用户拿大面积意见也换不来重写）
      revision_mode: revisionMode.value,
      // 打回时改了预期字数 → 按新目标重写；未改则沿用引擎生效值
      target_words: action === 'reject' ? reviewTarget.payload.value : undefined,
    })
    rejectFeedback.value = ''
  }, action === 'approve' ? '定稿入库' : '打回重写')
}

/** 四维均分（与自由创作的人审同一口径）。 */
function avgScore(): number | null {
  const r = props.state.draft?.review
  if (!r) return null
  return Math.round(((r.consistency + r.plot + r.continuity + r.prose) / 4) * 100) / 100
}

/**
 * 署名按阶段拆分：
 * 剧情策划智能体**只负责出剧情卡与接收选择**；写正文、审校、定稿由主智能体汇总反馈。
 */
const agentName = computed(() =>
  ['generating_cards', 'awaiting_choice'].includes(props.state.status)
    ? '剧情策划智能体'
    : '主智能体 · 墨师',
)

function pickCard(card: PlotCard): void {
  if (busy.value) return
  void choose(card.card_id)
}
</script>

<template>
  <div class="interactive-card">
    <div class="ic-head">
      <span class="ic-agent">{{ agentName }}</span>
      <span class="chip">{{ statusText }}</span>
      <span class="muted">第 {{ state.chapter }} 章 · 已定稿 {{ state.approved_count ?? 0 }}</span>
      <button class="ic-exit" title="退出互动界面（服务端断点保留，可随时恢复）" @click="emit('exit')">
        退出互动 ✕
      </button>
    </div>

    <!-- 非 error 状态下的错误提示（如"自定义卡内容为空"这类可当场改正的参数错误）；
         error 状态有专门的兜底块，这里不重复渲染。 -->
    <div v-if="state.error && state.status !== 'error'" class="ic-error">{{ state.error }}</div>

    <!-- 未开始 / 完本 -->
    <template v-if="state.status === 'idle'">
      <div class="ic-body">
        互动创作模式：我为每一章设计三条互斥的主线剧情卡，你四选一（或自拟），我再写章送审。
      </div>
      <div class="ic-actions">
        <button class="primary-btn" :disabled="busy" @click="start">开始 / 继续互动创作</button>
      </div>
    </template>
    <template v-else-if="state.status === 'done'">
      <div class="ic-body">本书已完本。</div>
    </template>

    <!-- 异常兜底：原先没有任何分支覆盖 error 态 → 页面只剩一行红字、零个可点按钮，
         用户只能重启整个应用（用户实测的"锁死"）。这里给出确切归因 + 重试出口。 -->
    <template v-else-if="state.status === 'error'">
      <div class="ic-body">
        <div class="ic-error-title">本轮没有跑完：{{ sourceLabel }}</div>
        <div v-if="state.error_hint" class="muted">{{ state.error_hint }}</div>
        <div v-if="modelText" class="muted">本次绑定：{{ modelText }}</div>
      </div>
      <div class="ic-error">{{ state.error }}</div>
      <div class="ic-actions">
        <button class="primary-btn" :disabled="busy" @click="retry">重试（保留已出的剧情卡与草稿）</button>
        <button class="ghost-btn" :disabled="busy" @click="start">从断点继续</button>
      </div>
    </template>

    <!-- 出卡中 / 写章中 / 入库中 -->
    <div v-else-if="state.status === 'generating_cards' || state.status === 'writing' || state.status === 'committing'" class="ic-body">
      {{ statusText }}…（约 1-3 分钟）
    </div>

    <!-- 选卡 -->
    <template v-else-if="state.status === 'awaiting_choice' && state.cards?.length">
      <div class="ic-body">我为第 {{ state.chapter }} 章设计了三条方向互斥的主线走向，请选择其一：</div>
      <!-- 预期字数（生成**之前**设定）：写作 / 评分 / 字数门禁读的都是这个数 -->
      <div class="ic-target-row">
        <span class="ic-target-label">本章预期字数</span>
        <NInputNumber
          v-model:value="preWords"
          size="small"
          :min="500"
          :max="20000"
          :step="500"
          :placeholder="serverTarget ? String(serverTarget) : '默认'"
          style="width: 140px"
        />
        <span class="muted">{{ boundsHint }}</span>
        <button v-if="preWordsDirty" class="link-btn" @click="preTarget.reset()">
          恢复默认
        </button>
      </div>
      <div
        v-for="card in state.cards"
        :key="card.card_id"
        class="plot-card"
        @click="pickCard(card)"
      >
        <div class="pc-head">
          <span class="pc-tag">{{ card.tag }}</span>
          <span class="pc-title">{{ card.title }}</span>
        </div>
        <div class="pc-outline">{{ card.outline }}</div>
        <div class="pc-hook muted">钩子：{{ card.hook }} · 出场：{{ card.characters.join('、') }}</div>
      </div>
      <div class="custom-card">
        <div class="pc-head" @click="showCustom = !showCustom" style="cursor: pointer">
          <span class="pc-tag">自拟</span>
          <span class="pc-title">第 4 张 · 由你决定本章走向</span>
        </div>
        <div class="pc-outline muted" style="cursor: pointer" @click="showCustom = !showCustom">
          {{ showCustom ? '在下面写下你想要的剧情，再点「采用自拟剧情」。' : '点这里展开，写你自己想要的走向（核心事件 / 冲突 / 结尾钩子）。' }}
        </div>
        <button class="link-btn" @click="showCustom = !showCustom">
          {{ showCustom ? '收起' : '展开自拟卡' }}
        </button>
        <template v-if="showCustom">
          <NInput
            v-model:value="customText"
            type="textarea"
            :rows="3"
            size="small"
            placeholder="写下你想要的剧情走向（核心事件 / 冲突 / 结尾钩子）"
          />
          <button class="primary-btn" :disabled="busy || !customText.trim()" @click="choose('custom', customText)">
            采用自拟剧情
          </button>
        </template>
      </div>
      <div class="redraw-row">
        <NInput
          v-model:value="redrawFeedback"
          size="small"
          placeholder="携意见重抽三张（可选）"
        />
        <button class="ghost-btn" :disabled="busy" @click="redraw">重抽</button>
      </div>
    </template>

    <!-- 人审 -->
    <template v-else-if="state.status === 'awaiting_review' && state.draft?.draft_text">
      <div class="ic-body">
        本章已写完，等你审阅。审校主编综合分 <b>{{ avgScore() ?? '—' }}</b>
        <span v-if="state.draft.model" class="muted">（{{ state.draft.model }}，第 {{ state.draft.attempt }} 稿）</span>
      </div>
      <div v-if="state.draft.review" class="ic-scores">
        <span
          v-for="d in DIMS"
          :key="d.key"
          class="ic-score"
          :class="`is-${scoreColor(state.draft.review[d.key])}`"
        >
          {{ d.label }} {{ state.draft.review[d.key] }}
        </span>
        <span class="ic-score is-default">字数 {{ state.draft.review.length }}</span>
        <!-- 目标 vs 实际：把"字数是否达标"从模型自评分变成可核对的两个数字 -->
        <span
          v-if="draftBounds"
          class="ic-score"
          :class="draftBounds.ok ? 'is-success' : 'is-error'"
        >
          目标 {{ draftBounds.target }}
          / 实际 {{ draftBounds.actual }}（{{ draftBounds.deviation >= 0 ? '+' : '' }}{{ draftBounds.deviation }} 字，
          可接受 {{ draftBounds.floor }}-{{ draftBounds.ceiling }} 字）
          {{ draftBounds.ok ? '✓' : (draftBounds.actual < draftBounds.floor ? '· 不足' : '· 超出') }}
        </span>
      </div>
      <div class="ic-target-row">
        <span class="ic-target-label">预期字数</span>
        <NInputNumber
          v-model:value="reviewWords"
          size="small"
          :min="500"
          :max="20000"
          :step="500"
          style="width: 140px"
        />
        <span class="muted">
          打回时按此目标重写（下浮 500 是硬线）<template v-if="reviewWordsDirty"> · 已改</template>
        </span>
        <button v-if="reviewWordsDirty" class="link-btn" @click="reviewTarget.reset()">
          恢复默认
        </button>
      </div>
      <div v-if="state.draft.review?.comment" class="ic-comment">{{ state.draft.review.comment }}</div>
      <div v-if="state.draft.review?.issues?.length" class="ic-issues">
        <div v-for="(it, i) in state.draft.review.issues" :key="i" class="ic-issue">
          <span class="ic-sev" :class="it.severity === 'major' ? 'is-major' : 'is-minor'">
            {{ it.severity === 'major' ? '重大' : '轻微' }}
          </span>
          <span>{{ it.description }}</span>
          <div v-if="it.suggestion" class="ic-sug">建议：{{ it.suggestion }}</div>
        </div>
        <!-- 一键并入：避免"手动复制建议时漏项/改词"导致重写不按建议走 -->
        <button class="ghost-btn" :disabled="busy" @click="mergeSuggestions">
          ↳ 把以上 {{ state.draft.review.issues.length }} 条建议并入打回意见
        </button>
      </div>
      <!-- 正文预览：可**上下拉伸**（右下角拖拽）。
           由来（用户实测）：固定 max-height:260px 太矮，长正文看不方便；
           这里给 resize:vertical + 最小/最大高度，拖高后正文跟着长高、不再各处挤着看。 -->
      <div class="draft-head">
        <span class="muted">正文预览（右下角可上下拖拽调整高度）</span>
        <span class="muted">{{ (state.draft.draft_text || '').length }} 字</span>
      </div>
      <div class="draft-box pre-wrap">{{ state.draft.draft_text }}</div>
      <NInput
        v-model:value="rejectFeedback"
        type="textarea"
        :rows="3"
        size="small"
        placeholder="打回意见（打回时必填）：写清要改什么；也可点上面的「并入建议」把审校意见一次带进来"
      />
      <div class="ic-actions">
        <NRadioGroup v-model:value="revisionMode" size="small">
          <NRadio value="targeted">定向修订</NRadio>
          <NRadio value="rewrite">整章重写</NRadio>
        </NRadioGroup>
        <span class="muted">
          {{ revisionMode === 'targeted'
            ? '保留未点名的内容，只改意见涉及处'
            : '不携带上一稿，按意见重新写一章（大改意见用这个）' }}
        </span>
      </div>
      <div class="ic-actions">
        <button class="ghost-btn warn" :disabled="busy" @click="decide('reject')">打回重写</button>
        <button class="primary-btn" :disabled="busy" @click="decide('approve')">通过，定稿入库</button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.interactive-card {
  border: 1px solid #dbe6fe;
  background: #f8faff;
  border-radius: 12px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 92%;
}
.ic-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.ic-agent {
  font-weight: 600;
  font-size: 13px;
  color: #1d4ed8;
}
.chip {
  font-size: 11px;
  background: #e8f0fe;
  color: #1d4ed8;
  border-radius: 999px;
  padding: 1px 8px;
}
.ic-body {
  font-size: 13px;
  line-height: 1.7;
  color: #3a3d44;
}
.ic-exit {
  margin-left: auto;
  background: none;
  border: 1px solid #e5e7eb;
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 11.5px;
  color: #8a8f99;
  cursor: pointer;
  white-space: nowrap;
}
.ic-exit:hover {
  color: #dc2626;
  border-color: #fca5a5;
}
.ic-error {
  font-size: 12.5px;
  color: #dc2626;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 8px;
  padding: 6px 8px;
  word-break: break-all;
}
.ic-error-title {
  font-size: 13px;
  font-weight: 600;
  color: #b91c1c;
  margin-bottom: 4px;
}
.ic-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.plot-card {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.plot-card:hover {
  border-color: #1d4ed8;
  box-shadow: 0 2px 8px rgba(29, 78, 216, 0.1);
}
.pc-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pc-tag {
  font-size: 11px;
  background: #f3f4f6;
  border-radius: 999px;
  padding: 1px 8px;
  color: #5c6470;
}
.pc-title {
  font-weight: 600;
  font-size: 13.5px;
}
.pc-outline {
  font-size: 12.5px;
  line-height: 1.7;
  color: #3a3d44;
}
.custom-card {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.link-btn {
  background: none;
  border: none;
  color: #1d4ed8;
  font-size: 12.5px;
  cursor: pointer;
  text-align: left;
  padding: 0;
}
.redraw-row {
  display: flex;
  gap: 8px;
}
/* 预期字数（生成前设定 / 打回时改目标）：写作、评分、门禁读的都是这个数 */
.ic-target-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 10px;
  border-radius: 8px;
  background: #ffffff;
  border: 1px solid #e2e8f5;
  font-size: 12px;
}
.ic-target-label {
  font-weight: 600;
  color: #3a3d44;
}
.ic-scores {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.ic-score {
  font-size: 11.5px;
  border-radius: 999px;
  padding: 1px 9px;
  background: #f3f4f6;
  color: #5c6470;
}
.ic-score.is-success {
  background: #e6f6ec;
  color: #116932;
}
.ic-score.is-warning {
  background: #fef3c7;
  color: #92400e;
}
.ic-score.is-error {
  background: #fde8e8;
  color: #b42318;
}
.ic-comment {
  font-size: 12.5px;
  color: #5c6470;
  line-height: 1.7;
}
.ic-issues {
  display: flex;
  flex-direction: column;
  max-height: 22vh;
  overflow-y: auto;
  scrollbar-width: thin;
}
.ic-issue {
  font-size: 12.5px;
  line-height: 1.7;
  padding: 4px 0;
  border-top: 1px dashed #e5e7eb;
}
.ic-sev {
  font-size: 11px;
  border-radius: 999px;
  padding: 0 7px;
  margin-right: 6px;
}
.ic-sev.is-major {
  background: #fde8e8;
  color: #b42318;
}
.ic-sev.is-minor {
  background: #fef3c7;
  color: #92400e;
}
.ic-sug {
  color: #116932;
}
.draft-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin: 8px 0 4px;
  font-size: 12px;
}
.draft-box {
  /* 可上下拉伸：resize 需要 overflow != visible 才生效（原先就是 overflow-y:auto ✓）。
     高度用**兜底 320px + 可拉伸 1200px 上限**：拖大后正文整段放得下，
     拖到最小也不会挤成一条缝；拉伸只影响本框，不改变对话框整体布局。 */
  height: 320px;
  min-height: 120px;
  max-height: 1200px;
  resize: vertical;
  overflow: auto;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.85;
  background: #fff;
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
  cursor: default;
}
.ghost-btn {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 12.5px;
  cursor: pointer;
  color: #3a3d44;
}
.ghost-btn.warn {
  color: #b45309;
  border-color: #fde68a;
}
</style>
