<script setup lang="ts">
/**
 * 墨师动作卡（P3）：展示本轮动作回执，并为「写动作」提供确认/取消闸门。
 *
 * 设计基线：**所有写动作都必须先经用户确认**——未确认时后端只返回 pending_confirm
 * 与影响说明，不产生任何数据变更；本组件的两个按钮就是那道闸门。
 */
import { computed, ref } from 'vue'
import { NButton, NTag, useMessage } from 'naive-ui'

import { api, withNovel } from '../api'
import type { ActionReceipt, PendingAction } from '../types'

const props = defineProps<{
  novelId: string
  chatId: string
  actions: ActionReceipt[]
  pending?: PendingAction | null
  /** 工作区/书内：用于展示作用域徽标 */
  scope?: 'book' | 'workspace'
}>()

const emit = defineEmits<{ (e: 'decided'): void }>()

const message = useMessage()
const busy = ref(false)

const scopePath = computed(() => withNovel('', props.novelId || '__workspace__').slice(1))

const reads = computed(() => props.actions.filter((a) => a.status !== 'pending_confirm'))
const writes = computed(() => props.actions.filter((a) => a.status === 'pending_confirm'))

const OP_LABEL: Record<string, string> = {
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

function label(op: string): string {
  return OP_LABEL[op] ?? op
}

function preview(action: ActionReceipt): string {
  const text = action.summary || ''
  return text.length > 240 ? `${text.slice(0, 240)}…` : text
}

async function decide(confirm: boolean): Promise<void> {
  if (busy.value) return
  busy.value = true
  const base = props.novelId || '__workspace__'
  try {
    if (confirm) {
      const res = await api<{ ok: boolean; summary?: string; error?: string }>(
        'POST',
        withNovel(`/api/chats/${props.chatId}/action`, base),
      )
      if (res.ok) message.success(res.summary || '已执行')
      else message.error(res.error || '执行失败')
    } else {
      await api('DELETE', withNovel(`/api/chats/${props.chatId}/action`, base))
      message.info('已取消待确认动作')
    }
    emit('decided')
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-if="reads.length || writes.length" class="action-card">
    <div class="ac-head">
      <span class="ac-title">墨师动作</span>
      <NTag size="tiny" :type="scope === 'workspace' ? 'info' : 'default'" :bordered="false">
        {{ scope === 'workspace' ? '工作区' : '本书' }}
      </NTag>
      <span class="ac-path">{{ scopePath }}</span>
    </div>

    <div v-for="(a, i) in reads" :key="`r-${i}`" class="ac-row">
      <NTag size="tiny" :type="a.status === 'failed' ? 'error' : 'success'" :bordered="false">
        {{ a.status === 'failed' ? '失败' : '已执行' }}
      </NTag>
      <span class="ac-op">{{ label(a.op) }}</span>
      <span class="ac-summary">{{ a.status === 'failed' ? a.error : preview(a) }}</span>
    </div>

    <div v-for="(a, i) in writes" :key="`w-${i}`" class="ac-pending">
      <div class="ac-pending-head">
        <NTag size="tiny" type="warning" :bordered="false">待确认</NTag>
        <span class="ac-op">{{ label(a.op) }}</span>
      </div>
      <div class="ac-impact">{{ a.summary }}</div>
      <div class="ac-actions">
        <NButton size="tiny" type="primary" :loading="busy" @click="decide(true)">
          确认执行
        </NButton>
        <NButton size="tiny" quaternary :disabled="busy" @click="decide(false)">取消</NButton>
      </div>
    </div>
  </div>
</template>

<style scoped>
.action-card {
  margin: 6px 0 2px;
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
  border-left: 3px solid #2563eb;
  border-radius: 6px;
  background: #f8fafc;
  font-size: 12px;
}
.ac-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.ac-title {
  font-weight: 600;
  color: #1e293b;
}
.ac-path {
  color: #94a3b8;
  font-family: ui-monospace, monospace;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ac-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 2px 0;
}
.ac-op {
  font-weight: 600;
  color: #334155;
  white-space: nowrap;
}
.ac-summary {
  color: #475569;
  word-break: break-word;
}
.ac-pending {
  margin-top: 6px;
  padding: 8px;
  border: 1px dashed #f0b429;
  border-radius: 6px;
  background: #fffbeb;
}
.ac-pending-head {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ac-impact {
  margin: 6px 0;
  color: #7c4a03;
  white-space: pre-wrap;
  word-break: break-word;
}
.ac-actions {
  display: flex;
  gap: 8px;
}
</style>
