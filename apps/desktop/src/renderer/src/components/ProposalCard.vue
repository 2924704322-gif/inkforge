<script setup lang="ts">
import { computed, ref } from 'vue'

import { api, withNovel } from '../api'

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
}

const props = defineProps<{
  novelId: string
  chatId: string
  proposal: Proposal
  autoApprove: boolean
}>()

const emit = defineEmits<{ decided: [] }>()

const STATUS_LABELS: Record<Proposal['status'], string> = {
  pending: '待审阅',
  accepting: '正在应用',
  accepted: '已接受',
  rejected: '已拒绝',
  conflict: '版本冲突',
  error: '应用失败',
}

const expanded = ref(false)
const busy = ref(false)

const statusLabel = computed(() => {
  if (props.proposal.status === 'pending' && props.autoApprove) return '待自动保存'
  return STATUS_LABELS[props.proposal.status]
})

const statusMessage = computed(() => {
  if (props.proposal.statusMessage) return props.proposal.statusMessage
  if (props.proposal.status === 'pending') return '接受后将应用到当前文稿并自动保存到本机。'
  return ''
})

const showActions = computed(
  () =>
    props.proposal.status === 'pending' ||
    props.proposal.status === 'conflict' ||
    (props.proposal.status === 'error' && false),
)

async function decide(decision: 'accept' | 'reject'): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    if (decision === 'accept') props.proposal.status = 'accepting'
    const res = await api<Proposal>(
      'POST',
      withNovel(`/api/chats/${props.chatId}/proposals/${props.proposal.id}/decide`, props.novelId),
      { decision },
    )
    Object.assign(props.proposal, {
      status: res.status,
      statusMessage: res.statusMessage,
    })
    emit('decided')
  } catch (err) {
    props.proposal.status = 'error'
    props.proposal.statusMessage = err instanceof Error ? err.message : String(err)
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

    <div v-if="proposal.hunks.length" class="p-diff">
      <button class="p-diff-toggle" @click="expanded = !expanded">
        <span>查看差异</span>
        <small>{{ proposal.hunks.length }} 个变更块</small>
        <span class="chev">{{ expanded ? '▾' : '▸' }}</span>
      </button>
      <div v-if="expanded" class="p-diff-content">
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
    </div>
    <p v-else class="muted" style="padding: 0 14px">没有可显示的行级差异。</p>

    <footer class="p-foot">
      <span class="p-message">{{ statusMessage }}</span>
      <div v-if="showActions" class="p-actions">
        <button class="review-btn is-reject" :disabled="busy" @click="decide('reject')">拒绝</button>
        <button class="review-btn is-accept" :disabled="busy" @click="decide('accept')">
          {{ proposal.status === 'accepting' ? '保存中…' : '接受并保存' }}
        </button>
      </div>
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
.p-diff-toggle {
  width: calc(100% - 28px);
  margin: 0 14px;
  display: flex;
  align-items: center;
  gap: 8px;
  background: #f6f7f8;
  border: 1px solid #eef0f2;
  border-radius: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 12.5px;
  color: #3a3d44;
}
.p-diff-toggle small {
  color: #8a8f99;
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
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 8px 14px 12px;
}
.p-message {
  font-size: 12px;
  color: #8a8f99;
}
.p-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
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
