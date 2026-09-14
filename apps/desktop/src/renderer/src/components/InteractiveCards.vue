<script setup lang="ts">
import { computed, ref } from 'vue'

import { NInput } from 'naive-ui'
import { api, withNovel } from '../api'
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

async function call(action: () => Promise<void>): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    await action()
    emit('refresh')
  } finally {
    busy.value = false
  }
}

const start = (): void => {
  void call(async () => {
    await api('POST', withNovel('/api/interactive/start', props.novelId))
  })
}

const choose = (cardId: string, custom = ''): void => {
  void call(async () => {
    await api('POST', withNovel('/api/interactive/choose', props.novelId), {
      card_id: cardId,
      custom_text: custom,
    })
    customText.value = ''
  })
}

const redraw = (): void => {
  void call(async () => {
    await api('POST', withNovel('/api/interactive/redraw', props.novelId), {
      feedback: redrawFeedback.value.trim(),
    })
    redrawFeedback.value = ''
  })
}

const decide = (action: 'approve' | 'reject'): void => {
  void call(async () => {
    await api('POST', withNovel('/api/interactive/decision', props.novelId), {
      action,
      feedback: rejectFeedback.value.trim(),
      revision_mode: 'targeted',
    })
    rejectFeedback.value = ''
  })
}

function scoreOf(): number | null {
  const r = props.state.draft?.review
  if (!r) return null
  return Math.round(((r.consistency + r.plot + r.continuity + r.prose) / 4) * 100) / 100
}

function pickCard(card: PlotCard): void {
  if (busy.value) return
  void choose(card.card_id)
}
</script>

<template>
  <div class="interactive-card">
    <div class="ic-head">
      <span class="ic-agent">剧情策划智能体</span>
      <span class="chip">{{ statusText }}</span>
      <span class="muted">第 {{ state.chapter }} 章 · 已定稿 {{ state.approved_count ?? 0 }}</span>
      <button class="ic-exit" title="退出互动界面（服务端断点保留，可随时恢复）" @click="emit('exit')">
        退出互动 ✕
      </button>
    </div>

    <div v-if="state.error" class="ic-error">{{ state.error }}</div>

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

    <!-- 出卡中 / 写章中 / 入库中 -->
    <div v-else-if="state.status === 'generating_cards' || state.status === 'writing' || state.status === 'committing'" class="ic-body">
      {{ statusText }}…（约 1-3 分钟）
    </div>

    <!-- 选卡 -->
    <template v-else-if="state.status === 'awaiting_choice' && state.cards?.length">
      <div class="ic-body">我为第 {{ state.chapter }} 章设计了三条方向互斥的主线走向，请选择其一：</div>
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
        <button class="link-btn" @click="showCustom = !showCustom">
          {{ showCustom ? '收起自拟卡' : '＋ 自拟本章剧情' }}
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
        本章已写完，请审阅。Editor 参考评分：
        <b>{{ scoreOf() ?? '—' }}</b>
        <span v-if="state.draft.model" class="muted">（{{ state.draft.model }}，第 {{ state.draft.attempt }} 稿）</span>
      </div>
      <div class="draft-box pre-wrap">{{ state.draft.draft_text }}</div>
      <NInput
        v-model:value="rejectFeedback"
        type="textarea"
        :rows="2"
        size="small"
        placeholder="打回意见（打回时必填）"
      />
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
  border: 1px dashed #d9dce1;
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
.draft-box {
  max-height: 260px;
  overflow-y: auto;
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
