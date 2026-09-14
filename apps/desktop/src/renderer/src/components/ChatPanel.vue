<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

import { api, withNovel } from '../api'
import { useMessage } from 'naive-ui'
import { appStore } from '../store'
import { type ChatData, type ChatSummary, type StatusSnapshot } from '../types'
import InteractiveCards from './InteractiveCards.vue'
import ProposalCard, { type Proposal } from './ProposalCard.vue'
import ReviewCard from './ReviewCard.vue'
import type { Book, InteractiveState } from '../types'

const messages = ref<ChatData['messages']>([])
const chats = ref<ChatSummary[]>([])
const input = ref('')
const sending = ref(false)
const loadingChat = ref(false)
const snapshot = ref<StatusSnapshot | null>(null)
const showHistory = ref(false)

/** DeepWrite 语义：改稿模式（发送即产出提案卡）与审批模式（替我审批）。 */
const proposeMode = ref(false)
const approvalMode = ref<'request-approval' | 'auto-approve'>('request-approval')
const proposals = ref<Proposal[]>([])
const interactive = ref<InteractiveState | null>(null)
/** 互动创作模式开关：仅正文逐章创作使用；服务端断点始终保留。 */
const interactiveMode = ref(false)
/** 互动会话在服务端存在未完结断点（用于按钮上的恢复提示）。 */
const resumeHint = ref(false)

const showInteractive = computed(() => interactiveMode.value && interactive.value !== null)

const streamRef = ref<HTMLElement | null>(null)
const historyWrap = ref<HTMLElement | null>(null)
const delegOpen = ref('')
const message = useMessage()

function toggleDeleg(key: string): void {
  delegOpen.value = delegOpen.value === key ? '' : key
}

const agentLabel = '主智能体 · 墨师'

/** 主智能体欢迎页（子智能体由墨师按任务自动调度）。 */
const welcome = {
  title: '从一个想法开始',
  desc: '我是主智能体「墨师」：与你对接、拆解任务，并调度人物设计 / 剧情策划 / 大纲规划 / 正文写手 / 审校主编五位子智能体协作。委派过程会显示在消息上方。',
  chips: [
    '概括这本书当前的主角与核心悬念',
    '设计一对互为镜像的人物',
    '为主线的下一阶段设计核心冲突',
    '点评最近一章的文笔，给出具体改法',
  ],
}

const proposeTarget = computed(() => {
  const sel = appStore.selection
  if (!sel) return null
  if (sel.kind === 'chapter') return { kind: 'chapter', key: `ch-${sel.chapter}` }
  return { kind: 'settings', key: sel.rel }
})

const proposeTargetLabel = computed(() => {
  const sel = appStore.selection
  if (!sel) return '未选择'
  if (sel.kind === 'chapter') return `第 ${sel.chapter} 章正文`
  return sel.title
})

async function loadChats(): Promise<void> {
  if (!appStore.bookId) return
  try {
    const res = await api<{ chats: ChatSummary[] }>(
      'GET',
      withNovel('/api/chats', appStore.bookId),
    )
    chats.value = res.chats
    // 自动恢复最近会话（仅当前没有任何会话时；不打断进行中的对话）
    if (!appStore.chatId && chats.value.length > 0) {
      await openChat(chats.value[0]!.id)
    }
  } catch {
    /* 静默 */
  }
}

async function removeChat(id: string): Promise<void> {
  try {
    await api('DELETE', withNovel(`/api/chats/${id}`, appStore.bookId))
    if (appStore.chatId === id) {
      appStore.chatId = ''
      messages.value = []
      proposals.value = []
    }
    chats.value = chats.value.filter((c) => c.id !== id)
    message.success('历史对话已删除')
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

function fmtRelative(ts: number): string {
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)} 天前`
  return new Date(ts * 1000).toLocaleDateString('zh-CN')
}

async function loadProposals(): Promise<void> {
  if (!appStore.bookId || !appStore.chatId) return
  try {
    const res = await api<{ proposals: Proposal[] }>(
      'GET',
      withNovel(`/api/chats/${appStore.chatId}/proposals`, appStore.bookId),
    )
    proposals.value = res.proposals.filter((p) => p.status === 'pending' || p.status === 'conflict')
  } catch {
    proposals.value = []
  }
}

async function openChat(id: string): Promise<void> {
  if (!appStore.bookId || !id) return
  loadingChat.value = true
  try {
    const chat = await api<ChatData>('GET', withNovel(`/api/chats/${id}`, appStore.bookId))
    messages.value = chat.messages
    appStore.chatId = chat.id
    await loadProposals()
  } catch {
    appStore.chatId = ''
    messages.value = []
    proposals.value = []
  } finally {
    loadingChat.value = false
    showHistory.value = false
    void scrollBottom()
  }
}

async function newChat(): Promise<void> {
  if (!appStore.bookId) return
  try {
    const res = await api<{ chat: ChatData }>('POST', withNovel('/api/chats', appStore.bookId), {
      agent: 'master',
    })
    appStore.chatId = res.chat.id
    messages.value = []
    proposals.value = []
    void loadChats()
  } catch (err) {
    message.error(`新建会话失败：${err instanceof Error ? err.message : String(err)}`)
  }
}

/** 中文输入法组合期间不拦截回车（否则拼音无法上屏，表现为“无法发送”）。 */
function onComposerEnter(e: KeyboardEvent): void {
  if (e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  void send()
}

async function send(text?: string): Promise<void> {
  const content = (text ?? input.value).trim()
  if (sending.value) return
  if (!content) return
  if (!appStore.bookId) {
    message.warning('请先在左侧选择一部作品')
    return
  }
  if (proposeMode.value && !proposeTarget.value) {
    message.warning('改稿模式需要在右侧打开一篇文档作为目标')
    return
  }
  if (!appStore.chatId) await newChat()
  if (!appStore.chatId) return

  if (proposeMode.value) {
    await sendPropose(content)
    return
  }

  input.value = ''
  messages.value.push({ role: 'user', content, ts: Date.now() / 1000 })
  sending.value = true
  void scrollBottom()
  try {
    const res = await api<{ reply: string }>(
      'POST',
      withNovel(`/api/chats/${appStore.chatId}/send`, appStore.bookId),
      {
        message: content,
        target: appStore.selection
          ? appStore.selection.kind === 'chapter'
            ? { kind: 'chapter', key: `ch-${appStore.selection.chapter}` }
            : { kind: 'settings', key: appStore.selection.rel }
          : undefined,
      },
    )
    messages.value.push({ role: 'assistant', content: res.reply, ts: Date.now() / 1000 })
    void loadChats()
  } catch (err) {
    messages.value.push({
      role: 'assistant',
      content: `⚠ 模型调用失败：${err instanceof Error ? err.message : String(err)}`,
      ts: Date.now() / 1000,
    })
  } finally {
    sending.value = false
    void scrollBottom()
  }
}

/** 改稿模式：产出提案卡（auto-approve 时自动接受保存）。 */
async function sendPropose(instruction: string): Promise<void> {
  if (!proposeTarget.value) return
  input.value = ''
  messages.value.push({
    role: 'user',
    content: `✎ 改稿指令（目标：${proposeTargetLabel.value}）：${instruction}`,
    ts: Date.now() / 1000,
  })
  sending.value = true
  void scrollBottom()
  try {
    const proposal = await api<Proposal>(
      'POST',
      withNovel(`/api/chats/${appStore.chatId}/propose`, appStore.bookId),
      {
        instruction,
        target: proposeTarget.value,
        agent: 'prose',
      },
    )
    proposals.value.unshift(proposal)
    messages.value.push({
      role: 'assistant',
      content: `已根据指令生成改稿提案（+${proposal.additions} / −${proposal.deletions}），请在下方卡片审阅。`,
      ts: Date.now() / 1000,
    })
    if (approvalMode.value === 'auto-approve') {
      const target = proposals.value[0]!
      target.status = 'accepting'
      try {
        const res = await api<Proposal>(
          'POST',
          withNovel(`/api/chats/${appStore.chatId}/proposals/${target.id}/decide`, appStore.bookId),
          { decision: 'accept' },
        )
        Object.assign(target, { status: res.status, statusMessage: res.statusMessage })
        appStore.treeVersion += 1
      } catch (err) {
        target.status = 'error'
        target.statusMessage = err instanceof Error ? err.message : String(err)
      }
    }
    void loadChats()
  } catch (err) {
    messages.value.push({
      role: 'assistant',
      content: `⚠ 提案生成失败：${err instanceof Error ? err.message : String(err)}`,
      ts: Date.now() / 1000,
    })
  } finally {
    sending.value = false
    void scrollBottom()
  }
}

async function pollStatus(): Promise<void> {
  if (!appStore.bookId) return
  try {
    snapshot.value = await api<StatusSnapshot>(
      'GET',
      withNovel('/api/status', appStore.bookId),
    )
  } catch {
    /* 静默 */
  }
}

async function pollInteractive(): Promise<void> {
  if (!appStore.bookId) return
  try {
    const st = await api<InteractiveState>(
      'GET',
      withNovel('/api/interactive/state', appStore.bookId),
    )
    interactive.value = st
    resumeHint.value = !['idle', 'done'].includes(st.status)
  } catch {
    /* 静默 */
  }
}

async function detectBookKind(): Promise<void> {
  if (!appStore.bookId) {
    interactive.value = null
    resumeHint.value = false
    return
  }
  try {
    const res = await api<{ books: Book[] }>('GET', '/api/books')
    const book = res.books.find((b) => b.novel_id === appStore.bookId)
    if (book?.interactive) {
      // 互动模式书：探测一次断点状态（不自动打开界面，由用户在模式切换器恢复）
      void pollInteractive()
    }
  } catch {
    /* 静默 */
  }
}

async function scrollBottom(): Promise<void> {
  await nextTick()
  const el = streamRef.value
  if (el) el.scrollTop = el.scrollHeight
}

function onProposalDecided(): void {
  void pollStatus()
  appStore.treeVersion += 1
}

function onReviewDecided(): void {
  void pollStatus()
  appStore.treeVersion += 1
}

function enterInteractive(): void {
  if (snapshot.value?.started && !snapshot.value.done) return
  interactiveMode.value = true
  void pollInteractive()
}

function exitInteractive(): void {
  interactiveMode.value = false
  resumeHint.value = interactive.value
    ? !['idle', 'done'].includes(interactive.value.status)
    : false
}



function fmtTime(ts?: number): string {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function onDocClick(e: MouseEvent): void {
  const wrap = historyWrap.value
  if (wrap && !wrap.contains(e.target as Node)) showHistory.value = false
}

let timer: ReturnType<typeof setInterval> | null = null

watch(
  () => appStore.bookId,
  () => {
    messages.value = []
    appStore.chatId = ''
    snapshot.value = null
    proposals.value = []
    interactive.value = null
    interactiveMode.value = false
    resumeHint.value = false
    void loadChats()
    void pollStatus()
    void detectBookKind()
  },
)

onMounted(() => {
  document.addEventListener('mousedown', onDocClick)
  void loadChats()
  void pollStatus()
  void detectBookKind()
  timer = setInterval(() => {
    void pollStatus()
    if (interactiveMode.value) void pollInteractive()
  }, 2500)
})

onUnmounted(() => {
  document.removeEventListener('mousedown', onDocClick)
  if (timer) clearInterval(timer)
})
</script>

<template>
  <section class="chat-panel">
    <div class="chat-head">
      <div class="agent-block">
        <span class="agent-name">{{ agentLabel }}</span>
        <span v-if="appStore.bookTitle" class="muted ctx-label">
          主上下文：{{ proposeTargetLabel === '未选择' ? appStore.bookTitle : proposeTargetLabel }}
        </span>
        <span v-if="snapshot?.pending" class="chip warn">待审</span>
        <span v-else-if="snapshot?.started && !snapshot?.done" class="chip busy">生成中</span>
        <span v-else-if="snapshot?.done" class="chip ok">已完本</span>
      </div>
      <div class="head-actions">
        <div class="mode-switch">
          <button
            class="mode-btn"
            :class="{ active: !interactiveMode }"
            @click="exitInteractive"
          >
            ✍ 自由协作
          </button>
          <button
            class="mode-btn"
            :class="{ active: interactiveMode }"
            :disabled="snapshot?.started === true && snapshot?.done === false"
            :title="snapshot?.started && !snapshot?.done ? '全自动流水线运行中，暂不可进入互动创作' : '仅用于正文逐章创作；进入后由剧情策划智能体出卡'"
            @click="enterInteractive"
          >
            🎬 互动创作
            <span v-if="resumeHint && !interactiveMode" class="resume-dot" />
          </button>
        </div>
        <div ref="historyWrap" class="history-wrap">
          <button class="ghost-btn" @click="showHistory = !showHistory">🕘 历史对话</button>
          <div v-if="showHistory" class="history-pop" @click.stop>
            <div class="history-pop-head muted">点击继续对话 · ✕ 删除</div>
            <div
              v-for="c in chats"
              :key="c.id"
              class="history-item"
              :class="{ active: c.id === appStore.chatId }"
              @click="openChat(c.id)"
            >
              <span class="h-title">{{ c.title || '新对话' }}</span>
              <span class="h-meta">
                <span class="muted">{{ fmtRelative(c.updated) }}</span>
                <button class="h-del" title="删除该对话" @click.stop="removeChat(c.id)">✕</button>
              </span>
            </div>
            <div v-if="chats.length === 0" class="muted" style="padding: 8px 10px">
              暂无历史对话——发第一条消息后自动保存
            </div>
          </div>
        </div>
        <button class="ghost-btn" @click="newChat">＋ 新建对话</button>
      </div>
    </div>

<div ref="streamRef" class="stream scroll-y">
      <div v-if="messages.length === 0 && proposals.length === 0 && !snapshot?.pending" class="welcome">
        <div class="welcome-mark">In</div>
        <div class="welcome-title">{{ welcome.title }}</div>
        <div class="muted welcome-desc">{{ welcome.desc }}</div>
        <div class="welcome-chips">
          <button v-for="chip in welcome.chips" :key="chip" class="chip-btn" @click="send(chip)">
            {{ chip }}
          </button>
        </div>
      </div>

      <template v-for="(m, i) in messages" :key="i">
        <div class="msg" :class="m.role">
          <div class="msg-meta">
            {{ m.role === 'user' ? '你' : agentLabel }}
            <span class="muted">{{ fmtTime(m.ts) }}</span>
          </div>
          <div
            v-if="m.delegations?.length"
            class="deleg-row"
          >
            <div v-for="(d, di) in m.delegations" :key="di" class="deleg-card">
              <button class="deleg-toggle" @click="toggleDeleg(`${i}-${di}`)">
                🤝 已委派：{{ d.label }}
                <span class="muted">· {{ d.instruction.slice(0, 26) }}{{ d.instruction.length > 26 ? '…' : '' }}</span>
                <span class="chev">{{ delegOpen === `${i}-${di}` ? '▾' : '▸' }}</span>
              </button>
              <pre
                v-if="delegOpen === `${i}-${di}`"
                class="deleg-output pre-wrap scroll-y"
              >{{ d.output }}</pre>
            </div>
          </div>
          <div class="msg-body pre-wrap">{{ m.content }}</div>
        </div>
      </template>

      <!-- 改稿提案卡（DeepWrite 的 proposal 语义：pending→accepting→accepted/rejected/conflict） -->
      <ProposalCard
        v-for="p in proposals"
        :key="p.id"
        :novel-id="appStore.bookId"
        :chat-id="appStore.chatId"
        :proposal="p"
        :auto-approve="approvalMode === 'auto-approve'"
        @decided="onProposalDecided"
      />

      <!-- 流水线人审关卡（审阅卡进入对话流） -->
      <ReviewCard
        v-if="snapshot?.pending"
        :key="JSON.stringify(snapshot.pending.type === 'chapter_review'
          ? `ch-${snapshot.pending.chapter}-${snapshot.pending.attempt}`
          : 'outline')"
        :novel-id="appStore.bookId"
        :snapshot="snapshot"
        :pending="snapshot.pending"
        @decided="onReviewDecided"
      />

      <!-- 互动创作：智能体实时操作卡（与普通流水线共用工作区） -->
      <InteractiveCards
        v-if="showInteractive && interactive"
        @exit="exitInteractive"
        :key="`${interactive.status}-${interactive.chapter}`"
        :novel-id="appStore.bookId"
        :state="interactive"
        @refresh="() => { void pollInteractive(); void pollStatus(); appStore.treeVersion += 1 }"
      />

      <div v-if="sending" class="msg assistant">
        <div class="msg-meta">{{ agentLabel }} <span class="muted">思考中…</span></div>
        <div class="msg-body muted">
          {{ proposeMode ? '正在结合全书上下文与目标文稿生成改稿提案…' : '正在结合全书上下文生成回复…' }}
        </div>
      </div>
    </div>

    <div class="composer">
      <div v-if="proposeMode" class="propose-target">
        ✎ 改稿模式 · 目标：<b>{{ proposeTargetLabel }}</b>
        <span v-if="!appStore.selection" class="muted">（在右侧打开一篇文档后可改稿）</span>
      </div>
      <textarea
        v-model="input"
        class="composer-input"
        :rows="proposeMode ? 3 : 2"
        :placeholder="proposeMode
          ? '写下改稿指令，例：把结尾改得更悬念，最后一段留钩子，其余保持原样'
          : '随心输入，与智能体协作。流水线产出的章节会以审阅卡形式出现在上方。'"
        @keydown.enter.exact="onComposerEnter"
      />
      <div class="composer-bar">
        <div class="composer-left">
          <button
            class="tool-btn"
            :class="{ on: proposeMode }"
            title="改稿模式：发送即对右侧文档产出改稿提案"
            @click="proposeMode = !proposeMode"
          >
            ✎ 改稿
          </button>
          <button
            class="tool-btn"
            :class="{ on: approvalMode === 'auto-approve' }"
            title="替我审批：提案生成后自动应用保存（仍做版本冲突校验）"
            @click="approvalMode = approvalMode === 'auto-approve' ? 'request-approval' : 'auto-approve'"
          >
            ✓ 替我审批
          </button>
        </div>
        <div class="composer-right">
          <span class="model-chip">deepseek-chat</span>
          <button class="send-btn" :disabled="sending || !input.trim()" @click="send()">↑</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.chat-panel {
  flex: 1;
  min-width: 340px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid #e9ebee;
}
.chat-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 18px 8px;
  gap: 8px;
}
.agent-block {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
  overflow: hidden;
}
.agent-name {
  font-weight: 600;
  font-size: 15px;
  white-space: nowrap;
}
.agent-block .muted {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chip {
  font-size: 11px;
  border-radius: 999px;
  padding: 1px 8px;
  white-space: nowrap;
}
.chip.ok {
  background: #e6f6ec;
  color: #116932;
}
.chip.busy {
  background: #e8f0fe;
  color: #1d4ed8;
}
.chip.warn {
  background: #fef3c7;
  color: #92400e;
}
.head-actions {
  display: flex;
  gap: 4px;
  align-items: center;
  flex-shrink: 0;
}
.ghost-btn {
  white-space: nowrap;
  background: none;
  border: none;
  color: #5c6470;
  font-size: 12.5px;
  cursor: pointer;
  padding: 5px 8px;
  border-radius: 6px;
}
.ghost-btn:hover {
  background: #f0f1f3;
}
.mode-switch {
  display: flex;
  background: #f3f4f6;
  border-radius: 8px;
  padding: 2px;
}
.mode-btn {
  position: relative;
  border: none;
  background: none;
  padding: 4px 12px;
  font-size: 12.5px;
  border-radius: 6px;
  cursor: pointer;
  color: #5c6470;
  white-space: nowrap;
}
.mode-btn.active {
  background: #fff;
  color: #1d4ed8;
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
}
.mode-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.resume-dot {
  position: absolute;
  top: 3px;
  right: 4px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #f59e0b;
}
.history-wrap {
  position: relative;
}
.history-pop {
  position: absolute;
  right: 0;
  top: 30px;
  width: 260px;
  max-height: 320px;
  overflow-y: auto;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
  z-index: 30;
  padding: 4px;
}
.history-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 7px;
  cursor: pointer;
  font-size: 13px;
}
.history-item:hover {
  background: #f3f4f6;
}
.history-item.active {
  background: #e8f0fe;
  color: #1d4ed8;
}
.h-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.history-pop-head {
  padding: 4px 10px 2px;
  border-bottom: 1px solid #f3f4f6;
}
.h-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.h-del {
  background: none;
  border: none;
  color: #c3c8cf;
  font-size: 12px;
  cursor: pointer;
  padding: 1px 5px;
  border-radius: 5px;
}
.h-del:hover {
  color: #dc2626;
  background: #fdebec;
}

.agent-tab {
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  padding: 7px 11px;
  font-size: 12.5px;
  color: #5c6470;
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
}
.agent-tab:hover {
  color: #26272b;
}
.agent-tab.active {
  color: #1d4ed8;
  border-bottom-color: #1d4ed8;
  font-weight: 600;
}
.stream {
  flex: 1;
  min-height: 0;
  padding: 18px 22px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.welcome {
  margin: auto;
  text-align: center;
  max-width: 460px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.welcome-mark {
  width: 52px;
  height: 52px;
  border-radius: 14px;
  background: linear-gradient(135deg, #1f2937, #374151);
  color: #fff;
  font-weight: 700;
  font-size: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.welcome-title {
  font-size: 18px;
  font-weight: 700;
}
.welcome-desc {
  line-height: 1.7;
}
.welcome-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  margin-top: 6px;
}
.chip-btn {
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 999px;
  padding: 7px 14px;
  font-size: 12.5px;
  color: #3a3d44;
  cursor: pointer;
}
.chip-btn:hover {
  border-color: #c7cdd6;
  background: #f9fafb;
}
.msg {
  max-width: 92%;
}
.msg.user {
  align-self: flex-end;
}
.msg.user .msg-body {
  background: #e8f0fe;
  border-radius: 12px 12px 3px 12px;
  padding: 10px 14px;
}
.msg-meta {
  font-size: 12px;
  color: #8a8f99;
  margin-bottom: 4px;
  display: flex;
  gap: 8px;
}
.msg.user .msg-meta {
  justify-content: flex-end;
}
.msg-body {
  font-size: 14px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg.assistant .msg-body {
  background: #f6f7f8;
  border-radius: 3px 12px 12px 3px;
  padding: 12px 16px;
}
.composer {
  border-top: 1px solid #eef0f2;
  padding: 12px 18px 10px;
}
.propose-target {
  font-size: 12.5px;
  color: #1d4ed8;
  background: #f0f5ff;
  border: 1px solid #dbe6fe;
  border-radius: 8px;
  padding: 5px 10px;
  margin-bottom: 8px;
}
.composer-input {
  width: 100%;
  border: 1px solid #e2e5ea;
  border-radius: 10px;
  padding: 10px 12px;
  font-family: inherit;
  font-size: 13.5px;
  resize: none;
  outline: none;
  color: #26272b;
  background: #fff;
}
.composer-input:focus {
  border-color: #93b4f8;
}
.composer-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 6px;
}
.composer-left {
  display: flex;
  gap: 6px;
}
.tool-btn {
  background: none;
  border: 1px solid #e5e7eb;
  border-radius: 999px;
  padding: 3px 12px;
  font-size: 12px;
  color: #5c6470;
  cursor: pointer;
}
.tool-btn:hover {
  border-color: #c7cdd6;
}
.tool-btn.on {
  background: #1d4ed8;
  border-color: #1d4ed8;
  color: #fff;
}
.composer-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.model-chip {
  font-size: 11.5px;
  color: #5c6470;
  background: #f3f4f6;
  border-radius: 999px;
  padding: 2px 10px;
}
.send-btn {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  border: none;
  background: #1f2937;
  color: #fff;
  font-size: 14px;
  cursor: pointer;
}
.send-btn:disabled {
  background: #d1d5db;
  cursor: default;
}
</style>
