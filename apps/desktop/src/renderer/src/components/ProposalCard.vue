<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, scoreColor, withNovel } from '../api'
import { useMessage } from 'naive-ui'

export interface DiffLine {
  type: 'context' | 'addition' | 'deletion'
  old?: number | string
  new?: number | string
  text: string
}

export interface DiffHunk {
  oldStart: number
  oldLines: number
  newStart: number
  newLines: number
  lines: DiffLine[]
}

export interface ProposalIssue {
  dimension: string
  severity: string
  description: string
  quote?: string
  suggestion?: string
}

/** 审校主编对提案成稿的评审（与流水线人审同一口径）。 */
export interface ProposalReview {
  consistency: number
  plot: number
  continuity: number
  prose: number
  length: number
  comment: string
  issues: ProposalIssue[]
}

export interface Proposal {
  id: string
  title: string
  summary: string
  target: { kind: string; key: string }
  status: 'pending' | 'accepting' | 'accepted' | 'rejected' | 'conflict' | 'error'
  statusMessage: string
  additions: number
  deletions: number
  hunks: DiffHunk[]
  truncated?: boolean
  /** 提案创建时间（引擎 `time.time()`，与消息 ts 同一时钟域）。用于插回对话流原位。 */
  created?: number
  /** 修改后的完整文稿（引擎 `_public()` 只剥掉 `original`，全文照发）。 */
  proposed?: string
  /** 审校主编评分与意见（引擎生成提案时一并产出）。 */
  review?: ProposalReview | null
  /** 评审失败原因：失败要能看见，否则用户只看到「评分没工作」。 */
  reviewError?: string
  /** 打回后按意见重做的新提案 id。 */
  replacementId?: string | null
}

const props = defineProps<{
  novelId: string
  chatId: string
  proposal: Proposal
}>()

const emit = defineEmits<{ decided: [] }>()

const message = useMessage()

const STATUS_LABELS: Record<Proposal['status'], string> = {
  pending: '待审阅',
  accepting: '正在应用',
  accepted: '已通过并写入',
  rejected: '已打回',
  conflict: '版本冲突',
  error: '应用失败',
}

const DIMS = [
  { key: 'consistency', label: '设定一致性' },
  { key: 'plot', label: '大纲符合度' },
  { key: 'continuity', label: '衔接连贯性' },
  { key: 'prose', label: '文笔质量' },
] as const

const expanded = ref(false)
const busy = ref(false)
/** 差异视图 / 修改后全文：改稿后必须能直接看到成稿，不能只有 diff。 */
const view = ref<'diff' | 'prose'>('diff')
/** 打回意见：打回必填，引擎据此重做一版提案。 */
const feedback = ref('')

const statusLabel = computed(() => STATUS_LABELS[props.proposal.status])

const statusMessage = computed(() => {
  if (props.proposal.statusMessage) return props.proposal.statusMessage
  if (props.proposal.status === 'pending') {
    return '通过后才会写入创作空间并同步资料库；打回请写意见，会按意见重做一版。'
  }
  return ''
})

function dimensionLabel(dimension: string): string {
  return DIMS.find((d) => d.key === dimension)?.label ?? dimension
}

const showActions = computed(
  () =>
    props.proposal.status === 'pending' ||
    props.proposal.status === 'conflict' ||
    // 应用失败也要给按钮：否则卡片卡死在 error 态，既不能重试也不能丢弃
    props.proposal.status === 'error',
)

async function decide(decision: 'accept' | 'reject'): Promise<void> {
  if (busy.value) return
  if (decision === 'reject' && !feedback.value.trim()) {
    message.warning('打回需要填写意见，引擎会按意见重做一版')
    return
  }
  busy.value = true
  try {
    if (decision === 'accept') props.proposal.status = 'accepting'
    const res = await api<Proposal>(
      'POST',
      withNovel(`/api/chats/${props.chatId}/proposals/${props.proposal.id}/decide`, props.novelId),
      { decision, feedback: feedback.value.trim() },
    )
    Object.assign(props.proposal, {
      status: res.status,
      statusMessage: res.statusMessage,
      replacementId: res.replacementId ?? null,
    })
    message.success(decision === 'accept' ? '已通过，已写入创作空间' : '已打回，正在按意见重做一版')
    feedback.value = ''
    emit('decided')
  } catch (err) {
    props.proposal.status = 'error'
    props.proposal.statusMessage = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

async function reReview(): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    const res = await api<Proposal>(
      'POST',
      withNovel(`/api/chats/${props.chatId}/proposals/${props.proposal.id}/review`, props.novelId),
    )
    props.proposal.review = res.review ?? null
    props.proposal.reviewError = ''
    message.success('已补齐审校评分')
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    busy.value = false
  }
}

function lineMark(type: DiffLine['type']): string {
  if (type === 'addition') return '+'
  if (type === 'deletion') return '−'
  return ' '
}
</script>

<template>
  <article class="proposal-card" :class="`is-${proposal.status}`">
    <header class="p-head">
      <span class="p-icon">📄</span>
      <div class="p-heading">
        <div class="p-title-row">
          <strong>{{ proposal.title }}</strong>
          <span class="p-status" :class="`is-${proposal.status}`">{{ statusLabel }}</span>
        </div>
        <p class="p-summary">{{ proposal.summary }}</p>
      </div>
      <div class="p-stats" :aria-label="`增加 ${proposal.additions} 行，删除 ${proposal.deletions} 行`">
        <span class="is-add">+{{ proposal.additions }}</span>
        <span class="is-del">−{{ proposal.deletions }}</span>
      </div>
    </header>

    <!-- 评审失败要显式说明：否则卡片静默不出评分，看起来像"功能没做" -->
    <div v-if="!proposal.review" class="p-review-error">
      <span>
        审校评分未生成{{ proposal.reviewError ? `：${proposal.reviewError}` : '（引擎未返回 review 字段，可能是升级前生成的旧提案）' }}
        —— 仍可通过下方差异审阅并决策。
      </span>
      <button class="p-rereview" :disabled="busy" @click="reReview">重新评审</button>
    </div>

    <!-- 审校主编评审：评分 + 意见（与流水线人审同一口径） -->
    <div v-if="proposal.review" class="p-review">
      <div class="p-score-row">
        <span
          v-for="d in DIMS"
          :key="d.key"
          class="p-score"
          :class="`is-${scoreColor(proposal.review[d.key])}`"
        >
          {{ d.label }} {{ proposal.review[d.key] }}
        </span>
        <span class="p-score is-default">字数 {{ proposal.review.length }}</span>
      </div>
      <div v-if="proposal.review.comment" class="p-comment">{{ proposal.review.comment }}</div>
      <div v-if="proposal.review.issues.length" class="p-issues">
        <div v-for="(issue, i) in proposal.review.issues" :key="i" class="p-issue">
          <span class="p-sev" :class="issue.severity === 'major' ? 'is-major' : 'is-minor'">
            {{ issue.severity === 'major' ? '重大' : '轻微' }}
          </span>
          <span class="p-issue-dim">{{ dimensionLabel(issue.dimension) }}</span>
          <span>{{ issue.description }}</span>
          <div v-if="issue.quote" class="muted">原文：{{ issue.quote }}</div>
          <div v-if="issue.suggestion" class="p-suggestion">建议：{{ issue.suggestion }}</div>
        </div>
      </div>
    </div>

    <div v-if="proposal.hunks.length || proposal.proposed" class="p-diff">
      <div class="p-viewbar">
        <button
          class="p-diff-toggle"
          :class="{ on: view === 'diff' }"
          @click="view = 'diff'; expanded = !expanded"
        >
          <span>行级差异</span>
          <small>{{ proposal.hunks.length }} 个变更块</small>
          <span class="chev">{{ view === 'diff' && expanded ? '▾' : '▸' }}</span>
        </button>
        <button
          v-if="proposal.proposed"
          class="p-diff-toggle"
          :class="{ on: view === 'prose' }"
          @click="view = 'prose'"
        >
          <span>修改后全文</span>
        </button>
      </div>
      <div v-if="view === 'diff' && expanded" class="p-diff-content">
        <div v-for="(hunk, hi) in proposal.hunks" :key="hi" class="hunk">
          <div class="hunk-head">@@ -{{ hunk.oldStart }},{{ hunk.oldLines }} +{{ hunk.newStart }},{{ hunk.newLines }} @@</div>
          <div
            v-for="(line, li) in hunk.lines"
            :key="li"
            class="diff-line"
            :class="`is-${line.type}`"
          >
            <span class="ln">{{ line.old ?? '' }}</span>
            <span class="ln">{{ line.new ?? '' }}</span>
            <span class="mark">{{ lineMark(line.type) }}</span>
            <code class="line-text">{{ line.text }}</code>
          </div>
        </div>
        <p v-if="proposal.truncated" class="muted diff-truncated">
          差异较大，仅显示部分变更；行数统计包含完整提案。
        </p>
      </div>
      <pre v-else-if="view === 'prose'" class="p-diff-content p-prose">{{ proposal.proposed }}</pre>
    </div>
    <p v-else class="muted" style="padding: 0 14px">没有可显示的行级差异。</p>

    <footer class="p-foot">
      <span class="p-message">{{ statusMessage }}</span>
      <template v-if="showActions">
        <textarea
          v-model="feedback"
          class="p-feedback"
          rows="2"
          placeholder="打回意见（打回时必填）：例如『第二段的人物动机不成立，其余保留』"
        />
        <div class="p-actions">
          <button class="review-btn is-reject" :disabled="busy" @click="decide('reject')">
            {{ busy ? '处理中…' : '打回重做' }}
          </button>
          <button class="review-btn is-accept" :disabled="busy" @click="decide('accept')">
            {{ proposal.status === 'accepting' ? '写入中…' : '通过并写入创作空间' }}
          </button>
        </div>
      </template>
    </footer>
  </article>
</template>

<style scoped>
.proposal-card {
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  background: #fff;
  overflow: hidden;
  font-size: 13px;
}
.proposal-card.is-accepted {
  border-color: #bbe7cb;
}
.p-head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px 8px;
}
.p-icon {
  font-size: 15px;
  margin-top: 1px;
}
.p-heading {
  flex: 1;
  min-width: 0;
}
.p-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.p-title-row strong {
  font-size: 13.5px;
  color: #26272b;
}
.p-status {
  font-size: 11px;
  border-radius: 999px;
  padding: 1px 8px;
}
.p-status.is-pending {
  background: #fef3c7;
  color: #92400e;
}
.p-status.is-accepting {
  background: #e8f0fe;
  color: #1d4ed8;
}
.p-status.is-accepted {
  background: #e6f6ec;
  color: #116932;
}
.p-status.is-rejected {
  background: #f3f4f6;
  color: #5c6470;
}
.p-status.is-conflict {
  background: #fde8e8;
  color: #b42318;
}
.p-status.is-error {
  background: #fde8e8;
  color: #b42318;
}
.p-summary {
  margin: 3px 0 0;
  color: #8a8f99;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.p-stats {
  display: flex;
  gap: 6px;
  font-family: Consolas, monospace;
  font-size: 12.5px;
  flex-shrink: 0;
}
.p-stats .is-add {
  color: #116932;
}
.p-stats .is-del {
  color: #b42318;
}
.p-viewbar {
  display: flex;
  gap: 8px;
  margin: 0 14px;
}
.p-diff-toggle {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: #f6f7f8;
  border: 1px solid #eef0f2;
  border-radius: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 12.5px;
  color: #3a3d44;
}
.p-diff-toggle.on {
  background: #e8f0fe;
  border-color: #dbe6fe;
  color: #1d4ed8;
  font-weight: 600;
}
.p-prose {
  margin: 8px 14px;
  max-height: 380px;
  overflow-y: auto;
  border: 1px solid #eef0f2;
  border-radius: 8px;
  padding: 10px 12px;
  background: #fafafa;
  font-size: 13px;
  line-height: 1.9;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
}
.p-diff-toggle small {
  color: inherit;
  opacity: 0.7;
}
.chev {
  margin-left: auto;
  color: #8a8f99;
}
.p-diff-content {
  margin: 8px 14px;
  border: 1px solid #eef0f2;
  border-radius: 8px;
  max-height: 300px;
  overflow-y: auto;
}
.hunk-head {
  font-family: Consolas, monospace;
  font-size: 11.5px;
  color: #8a8f99;
  background: #f6f7f8;
  padding: 3px 10px;
}
.diff-line {
  display: flex;
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.6;
}
.diff-line.is-addition {
  background: #e6f6ec;
}
.diff-line.is-deletion {
  background: #fdebec;
}
.ln {
  width: 34px;
  flex-shrink: 0;
  text-align: right;
  padding-right: 6px;
  color: #b6bac2;
  user-select: none;
}
.mark {
  width: 16px;
  flex-shrink: 0;
  color: #8a8f99;
  user-select: none;
}
.line-text {
  white-space: pre-wrap;
  word-break: break-all;
  color: #26272b;
}
.diff-truncated {
  padding: 6px 10px;
}
.p-foot {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 14px 12px;
  border-top: 1px solid #f0f2f5;
}
.p-message {
  font-size: 12px;
  color: #8a8f99;
}
.p-feedback {
  width: 100%;
  resize: vertical;
  border: 1px solid #e2e5ea;
  border-radius: 8px;
  padding: 8px 10px;
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.7;
  color: #26272b;
  outline: none;
}
.p-feedback:focus {
  border-color: #93b4f8;
}
.p-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  flex-shrink: 0;
}
/* ── 审校主编评审 ── */
.p-review {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0 14px;
  padding: 8px 10px;
  border: 1px solid #eef0f2;
  border-radius: 8px;
  background: #fbfcfd;
}
.p-score-row {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.p-score {
  font-size: 11.5px;
  border-radius: 999px;
  padding: 1px 9px;
  background: #f3f4f6;
  color: #5c6470;
}
.p-score.is-success {
  background: #e6f6ec;
  color: #116932;
}
.p-score.is-warning {
  background: #fef3c7;
  color: #92400e;
}
.p-score.is-error {
  background: #fde8e8;
  color: #b42318;
}
.p-comment {
  font-size: 12.5px;
  color: #5c6470;
  line-height: 1.7;
}
.p-review-error {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 0 14px;
  padding: 7px 10px;
  border: 1px solid #fde68a;
  border-radius: 8px;
  background: #fffbeb;
  color: #92400e;
  font-size: 12px;
  line-height: 1.7;
}
.p-rereview {
  flex-shrink: 0;
  background: #fff;
  border: 1px solid #fbbf24;
  color: #92400e;
  border-radius: 7px;
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
}
.p-rereview:disabled {
  opacity: 0.5;
  cursor: default;
}
.p-issues {
  display: flex;
  flex-direction: column;
  max-height: 22vh;
  overflow-y: auto;
  scrollbar-width: thin;
}
.p-issue {
  font-size: 12.5px;
  line-height: 1.7;
  padding: 4px 0;
  border-top: 1px dashed #e5e7eb;
}
.p-issue:first-child {
  border-top: none;
}
.p-sev {
  font-size: 11px;
  border-radius: 999px;
  padding: 0 7px;
  margin-right: 6px;
}
.p-sev.is-major {
  background: #fde8e8;
  color: #b42318;
}
.p-sev.is-minor {
  background: #fef3c7;
  color: #92400e;
}
.p-issue-dim {
  margin-right: 6px;
  opacity: 0.75;
}
.p-suggestion {
  color: #116932;
}
.review-btn {
  border-radius: 8px;
  padding: 5px 14px;
  font-size: 12.5px;
  cursor: pointer;
  border: 1px solid #e5e7eb;
  background: #fff;
  color: #3a3d44;
}
.review-btn.is-accept {
  background: #1f2937;
  border-color: #1f2937;
  color: #fff;
}
.review-btn.is-accept:hover:not(:disabled) {
  background: #374151;
}
.review-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
</style>
