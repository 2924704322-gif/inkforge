<script setup lang="ts">
import {
  NAlert,
  NButton,
  NDivider,
  NInput,
  NModal,
  NRadio,
  NRadioGroup,
  NScrollbar,
  NSpace,
  NSpin,
  NSwitch,
  NTag,
  useMessage,
} from 'naive-ui'
import { computed, ref } from 'vue'

import { api, scoreColor, withNovel } from '../api'
import type { PendingItem, StatusSnapshot } from '../types'
import DiffView from './DiffView.vue'

const props = defineProps<{
  novelId: string
  snapshot: StatusSnapshot | null
  pending: PendingItem
}>()

const emit = defineEmits<{ decided: [] }>()

const message = useMessage()

const outline = computed(() =>
  props.pending.type === 'outline_review' ? props.pending.outline : null,
)
const chapter = computed(() =>
  props.pending.type === 'chapter_review' ? props.pending : null,
)

const feedback = ref('')
const revisionMode = ref<'targeted' | 'rewrite'>('targeted')
const submitting = ref(false)

const showDiff = ref(true)
const expandDraft = ref(false)
const hasPrevious = computed(
  () => typeof chapter.value?.previous_draft === 'string',
)

function dimensionLabel(dimension: string): string {
  switch (dimension) {
    case 'consistency':
      return '设定一致性'
    case 'plot':
      return '大纲符合度'
    case 'continuity':
      return '衔接连贯性'
    case 'prose':
      return '文笔质量'
    case 'length':
      return '字数符合度'
    default:
      return dimension
  }
}

async function decide(action: 'approve' | 'reject'): Promise<void> {
  if (action === 'reject' && !feedback.value.trim()) {
    message.warning('打回需要填写审阅意见')
    return
  }
  submitting.value = true
  try {
    await api('POST', withNovel('/api/decision', props.novelId), {
      action,
      feedback: feedback.value.trim(),
      revision_mode: revisionMode.value,
    })
    if (action === 'approve') {
      message.success('已通过')
    } else {
      message.success('已打回，Writer 将按意见修改')
    }
    feedback.value = ''
    emit('decided')
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="review-card">
    <!-- 大纲审阅 -->
    <template v-if="outline">
      <div class="review-head">
        <NTag type="info" size="small">大纲审阅</NTag>
        <span class="review-title">《{{ outline.book_title }}》</span>
        <span class="muted">{{ outline.theme }}</span>
      </div>
      <div class="outline-brief">
        <div v-for="vol in outline.volumes" :key="vol.volume" class="vol-line">
          <NTag size="tiny" :bordered="false">第{{ vol.volume }}卷</NTag>
          <span class="vol-title">{{ vol.title }}</span>
          <span class="muted">{{ vol.chapters.length }} 章</span>
        </div>
        <div v-if="outline.foreshadowing?.length" class="vol-line">
          <NTag size="tiny" :bordered="false">伏笔</NTag>
          <span class="muted">{{ outline.foreshadowing.length }} 条贯穿性伏笔</span>
        </div>
      </div>
      <NScrollbar class="outline-scroll">
        <pre class="pre-wrap outline-detail">{{ JSON.stringify(outline.volumes, null, 2) }}</pre>
      </NScrollbar>
    </template>

    <!-- 章节审阅 -->
    <template v-else-if="chapter">
      <div class="review-head">
        <NTag type="warning" size="small">章节审阅</NTag>
        <span class="review-title">第 {{ chapter.chapter }} 章 · {{ chapter.volume }} 卷</span>
        <span class="muted">第 {{ chapter.attempt }} 稿</span>
        <span v-if="chapter.model" class="muted">{{ chapter.model }}</span>
        <NTag
          v-if="chapter.retry_exceeded"
          type="error"
          size="tiny"
          :bordered="false"
        >自动重试超限</NTag>
      </div>

      <div class="score-row">
        <NTag v-for="d in (['consistency','plot','continuity','prose'] as const)" :key="d"
             :type="(scoreColor(chapter.review[d]) as any)" size="small" round>
          {{ dimensionLabel(d) }} {{ chapter.review[d] }}
        </NTag>
        <NTag size="small" round :bordered="false">字数 {{ chapter.review.length }}</NTag>
      </div>
      <div v-if="chapter.review.comment" class="muted comment">{{ chapter.review.comment }}</div>

      <NAlert
        v-if="chapter.review.issues.length"
        type="default"
        :show-icon="false"
        class="issue-box"
      >
        <div v-for="(issue, i) in chapter.review.issues" :key="i" class="issue-item">
          <NTag :type="issue.severity === 'major' ? 'error' : 'warning'" size="tiny" :bordered="false">
            {{ issue.severity === 'major' ? '重大' : '轻微' }}
          </NTag>
          <span class="issue-dim">{{ dimensionLabel(issue.dimension) }}</span>
          <span>{{ issue.description }}</span>
          <div v-if="issue.quote" class="muted issue-quote">原文：{{ issue.quote }}</div>
          <div class="issue-suggestion">建议：{{ issue.suggestion }}</div>
        </div>
      </NAlert>

      <div class="draft-toolbar">
        <span class="muted">正文预览</span>
        <template v-if="hasPrevious">
          <span class="muted">与上一稿（第 {{ chapter.previous_attempt }} 稿）对比</span>
          <NSwitch v-model:value="showDiff" size="small">
            <template #checked>diff</template>
            <template #unchecked>原文</template>
          </NSwitch>
        </template>
        <NButton v-if="!showDiff" size="tiny" quaternary @click="expandDraft = !expandDraft">
          {{ expandDraft ? '收起' : '展开全文' }}
        </NButton>
      </div>
      <div v-if="hasPrevious && showDiff" class="draft-scroll-box">
        <DiffView :base="chapter.previous_draft ?? ''" :target="chapter.draft_text" />
      </div>
      <pre
        v-else
        class="pre-wrap draft-scroll-box draft-pre"
        :style="expandDraft ? undefined : { maxHeight: '300px' }"
        >{{ chapter.draft_text }}</pre
      >
    </template>

    <NDivider style="margin: 10px 0" />

    <!-- 审阅决策区（sticky：无论正文多长都悬浮可见） -->
    <div class="decision-area">
      <NInput
        v-model:value="feedback"
        type="textarea"
        :rows="2"
        placeholder="审阅意见（打回时必填）：例如『第 3 段与角色状态冲突，主角此时应当还在城南』"
      />
      <div class="decision-row">
        <NRadioGroup v-model:value="revisionMode" size="small">
          <NRadio value="targeted">定向修订</NRadio>
          <NRadio value="rewrite">整章重写</NRadio>
        </NRadioGroup>
        <NSpace>
          <NButton type="primary" :loading="submitting" @click="decide('approve')">
            通过
          </NButton>
          <NButton type="warning" secondary :loading="submitting" @click="decide('reject')">
            打回
          </NButton>
        </NSpace>
      </div>
    </div>

    <!-- 加载态 -->
    <NModal v-model:show="submitting" :mask-closable="false">
      <NSpin size="large" />
    </NModal>
  </div>
</template>

<style scoped>
.review-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
}
.review-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.review-title {
  font-weight: 600;
}
.outline-brief {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.vol-line {
  display: flex;
  align-items: center;
  gap: 8px;
}
.vol-title {
  font-size: 13px;
}
.outline-detail {
  font-size: 12px;
  line-height: 1.7;
  margin: 0;
}
.score-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.comment {
  font-size: 13px;
}
.issue-box {
  background: #f9fafb;
}
.issue-item {
  padding: 4px 0;
  border-bottom: 1px dashed #e5e7eb;
  font-size: 13px;
}
.issue-item:last-child {
  border-bottom: none;
}
.issue-dim {
  margin: 0 6px;
  opacity: 0.75;
}
.issue-quote {
  margin-left: 4px;
}
.issue-suggestion {
  margin-left: 4px;
  color: #116932;
}
.outline-scroll {
  max-height: 240px;
}
.draft-pre {
  margin: 0;
  font-size: 13.5px;
  line-height: 1.9;
}
.draft-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.draft-scroll-box {
  max-height: 300px;
  overflow-y: auto;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 10px 12px;
  background: #fafafa;
  scrollbar-width: thin;
}
.decision-area {
  position: sticky;
  bottom: 0;
  z-index: 5;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: #ffffff;
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  box-shadow: 0 -4px 16px rgba(0, 0, 0, 0.06);
}
.decision-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
</style>
