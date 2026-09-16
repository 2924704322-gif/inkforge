<script setup lang="ts">
import { computed, nextTick, onErrorCaptured, onMounted, onUnmounted, ref, watch } from 'vue'

import { api, withNovel } from '../api'
import { useMessage } from 'naive-ui'
import { appStore, inWorkspace, openWorkspace, scopeParam } from '../store'
import {
  type ActionReceipt,
  type ChatData,
  type ChatSummary,
  type PendingAction,
  type StatusSnapshot,
} from '../types'
import ActionCard from './ActionCard.vue'
import InteractiveCards from './InteractiveCards.vue'
import ProposalCard, { type Proposal, type ProposalReview } from './ProposalCard.vue'
import ReviewCard from './ReviewCard.vue'
import type { Book, InteractiveState } from '../types'

const messages = ref<ChatData['messages']>([])
const chats = ref<ChatSummary[]>([])
const input = ref('')
const sending = ref(false)
const loadingChat = ref(false)
const snapshot = ref<StatusSnapshot | null>(null)
const showHistory = ref(false)
/** 墨师动作回执（本轮）与待确认写动作（P2/P3）。 */
const lastActions = ref<ActionReceipt[]>([])
const pendingAction = ref<PendingAction | null>(null)

/** 改稿模式（发送即产出提案卡）。
 *  没有「替我审批 / 自动接受」：生成或修改的内容一律先经人工审批，
 *  通过后才写入创作空间中的资料。 */
const proposeMode = ref(false)
const proposals = ref<Proposal[]>([])
const interactive = ref<InteractiveState | null>(null)
/** 互动创作模式开关：仅正文逐章创作使用；服务端断点始终保留。 */
const interactiveMode = ref(false)
/** 互动会话在服务端存在未完结断点（用于按钮上的恢复提示）。 */
const resumeHint = ref(false)

const showInteractive = computed(() => interactiveMode.value && interactive.value !== null)

/** 四维均分（与审阅卡同一口径：一致性 / 大纲符合度 / 衔接连贯性 / 文笔质量）。 */
function avgScore(r: ProposalReview): string {
  const v = (r.consistency + r.plot + r.continuity + r.prose) / 4
  return (Math.round(v * 100) / 100).toFixed(2)
}

const INTERACTIVE_LABEL: Record<string, string> = {
  generating_cards: '正在设计剧情卡',
  awaiting_choice: '等待选卡',
  writing: '正在写章',
  awaiting_review: '等待人审',
  committing: '定稿入库中',
  error: '互动创作异常',
}

/**
 * 顶部工作状态：**综合所有「智能体在工作」的来源**，而不是只看流水线会话。
 *
 * 之前只看 `/api/status` 的 started/done/pending —— 那三个字段是流水线会话的状态，
 * 于是对话改稿、互动创作期间顶部一直显示"空闲"，看起来像状态坏了。
 */
const workStatus = computed<{ kind: 'idle' | 'busy' | 'warn' | 'ok' | 'err'; text: string }>(() => {
  if (sending.value) return { kind: 'busy', text: '智能体思考中' }
  const iv = interactive.value
  if (iv && !['idle', 'done'].includes(iv.status)) {
    return { kind: 'busy', text: `互动创作 · ${INTERACTIVE_LABEL[iv.status] ?? iv.status}` }
  }
  const p = snapshot.value?.pending
  if (p) {
    if (p.type === 'chapter_gate') {
      const g = p as unknown as { next_chapter: number }
      return { kind: 'warn', text: `待你指令 · 第 ${g.next_chapter} 章` }
    }
    return {
      kind: 'warn',
      text: p.type === 'chapter_review' ? `待审 · 第 ${p.chapter} 章` : '待审 · 大纲',
    }
  }
  if (snapshot.value?.started && !snapshot.value?.done) return { kind: 'busy', text: '流水线生成中' }
  if (proposals.value.length) return { kind: 'warn', text: `待审提案 ${proposals.value.length} 张` }
  if (snapshot.value?.error) return { kind: 'err', text: '流水线异常' }
  if (snapshot.value?.done) return { kind: 'ok', text: '已完本' }
  return { kind: 'idle', text: '空闲' }
})

/**
 * 提案卡按创建时间插回对话流的原位。
 *
 * 旧写法是把所有提案统一渲染在**全部消息之后**，于是每一张待审提案都会
 * 「一直挂在最下面」：后续消息都排在它上面，而每次发送后的 scrollBottom()
 * 又把视野拽到流底，正好停在提案卡上——提案卡因此成了挡在眼前的一块。
 *
 * 引擎给提案写了 `created`（time.time()），消息有 `ts`（同一时钟域），
 * 所以按时间去插回原位在重启后依然成立。
 * 返回 { -1: 早于全部可见消息的提案, i: 应排在 messages[i] 之后的提案 }。
 */
const proposalsByAnchor = computed(() => {
  const buckets: Record<number, Proposal[]> = {}
  const hasTs = messages.value.some((m) => (m.ts ?? 0) > 0)
  for (const p of proposals.value) {
    const created = p.created ?? 0
    // 默认挂在最后一条消息之后（＝追加到末尾，旧行为）；能定位就插回原位
    let anchor = messages.value.length - 1
    if (hasTs && created > 0) {
      anchor = -1
      for (let i = messages.value.length - 1; i >= 0; i -= 1) {
        const ts = messages.value[i]?.ts ?? 0
        if (ts > 0 && ts <= created) {
          anchor = i
          break
        }
      }
    }
    const list = buckets[anchor]
    if (list) list.push(p)
    else buckets[anchor] = [p]
  }
  return buckets
})

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
  // 工作区也允许对话（墨师全域）：无书时用哨兵值取工作区会话列表。
  try {
    const res = await api<{ chats: ChatSummary[] }>(
      'GET',
      withNovel('/api/chats', scopeParam()),
    )
    const wantScope = inWorkspace() ? 'workspace' : 'book'
    const filtered = res.chats.filter((c) => (c.scope ?? 'book') === wantScope)
    chats.value = filtered
    // 自动恢复最近会话（仅当前没有任何会话时；不打断进行中的对话）
    if (!appStore.chatId && filtered.length > 0) {
      await openChat(filtered[0]!.id)
    }
  } catch {
    /* 静默 */
  }
}

async function removeChat(id: string): Promise<void> {
  try {
    await api('DELETE', withNovel(`/api/chats/${id}`, scopeParam()))
    if (appStore.chatId === id) {
      appStore.chatId = ''
      messages.value = []
      proposals.value = []
      lastActions.value = []
      pendingAction.value = null
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
  // 改稿提案仍绑定"当前书的文档"：工作区没有目标文档，直接跳过（后端也会拒绝）
  if (inWorkspace() || !appStore.chatId) return
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
  if (!id) return
  loadingChat.value = true
  try {
    const chat = await api<ChatData>('GET', withNovel(`/api/chats/${id}`, scopeParam()))
    messages.value = chat.messages
    appStore.chatId = chat.id
    // 待确认动作与动作回执随会话恢复（刷新/重启后不丢闸门）
    pendingAction.value = chat.pending ?? null
    lastActions.value = (chat.messages[chat.messages.length - 1]?.actions ?? []) as ActionReceipt[]
    await loadProposals()
  } catch {
    appStore.chatId = ''
    messages.value = []
    proposals.value = []
    lastActions.value = []
    pendingAction.value = null
  } finally {
    loadingChat.value = false
    showHistory.value = false
    void scrollBottom()
  }
}

async function newChat(): Promise<void> {
  try {
    const res = await api<{ chat: ChatData }>('POST', withNovel('/api/chats', scopeParam()), {
      agent: 'master',
    })
    appStore.chatId = res.chat.id
    messages.value = []
    proposals.value = []
    lastActions.value = []
    pendingAction.value = null
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
  // 无书时不再拦人：工作区模式照样能把事交给墨师（建书/取资料/开写都在它的动作里）
  if (proposeMode.value && inWorkspace()) {
    message.warning('改稿模式需要先打开一部作品，并在右侧选中一篇文档')
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
    const res = await api<{
      reply: string
      actions?: ActionReceipt[]
      pending?: PendingAction | null
    }>('POST', withNovel(`/api/chats/${appStore.chatId}/send`, scopeParam()), {
      message: content,
      target:
        !inWorkspace() && appStore.selection
          ? appStore.selection.kind === 'chapter'
            ? { kind: 'chapter', key: `ch-${appStore.selection.chapter}` }
            : { kind: 'settings', key: appStore.selection.rel }
          : undefined,
    })
    messages.value.push({ role: 'assistant', content: res.reply, ts: Date.now() / 1000 })
    lastActions.value = res.actions ?? []
    pendingAction.value = res.pending ?? null
    // 墨师可能刚建了书/切了书：同步前端书目与资源树
    void syncBookAfterActions()
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

/**
 * 动作执行后同步"当前书目"：墨师建书/切书后，前端与右侧资源树要跟上。
 *
 * 每一轮对话结束后都会调用（不只动作轮）——墨师完全可能在纯文本回合里已经改过
 * 当前书目，漏掉这一点就会出现"墨师说已切到 B 书，界面还停在 A 书"。
 */
async function syncBookAfterActions(): Promise<void> {
  try {
    const res = await api<{ novel_id: string }>('GET', '/api/book-select')
    const nid = res.novel_id || ''
    if (nid === appStore.bookId) return
    if (nid) {
      const books = await api<{ books: Book[] }>('GET', '/api/books')
      const found = books.books.find((b) => b.novel_id === nid)
      appStore.bookId = nid
      appStore.bookTitle = found?.title || nid
    } else {
      openWorkspace()
      return
    }
    // 换书即重置：右侧选中、待确认动作、资源树全部跟着走
    appStore.selection = null
    pendingAction.value = null
    lastActions.value = []
    appStore.treeVersion += 1
    void detectBookKind()
  } catch {
    /* 静默：仅影响书目标记的即时性 */
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
      content: `已根据指令生成改稿提案（+${proposal.additions} / −${proposal.deletions}）${proposal.review ? `，审校主编评分 ${avgScore(proposal.review)}` : ''}，请在下方卡片审阅：通过后才会写入创作空间，打回可写意见让它重做一版。`,
      ts: Date.now() / 1000,
    })
    // 人工审批门禁：不自动应用，等用户在卡片上点「通过 / 打回」。
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
  if (inWorkspace()) return    // 工作区没有流水线会话可轮询
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
  if (inWorkspace()) return    // 互动创作是"书内"状态机
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
  if (inWorkspace()) {
    interactive.value = null
    resumeHint.value = false
    interactiveMode.value = false
    snapshot.value = null
    return
  }
  try {
    const res = await api<{ books: Book[] }>('GET', '/api/books')
    const book = res.books.find((b) => b.novel_id === appStore.bookId)
    if (book?.interactive) {
      // 互动模式书：直接进入互动界面（本书只有这一条推进路径）
      interactiveMode.value = true
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

/** 待审卡片的身份键（换章/换稿即变化）。 */
const reviewKey = computed(() => {
  const p = snapshot.value?.pending
  if (!p) return ''
  return p.type === 'chapter_review' ? `ch-${p.chapter}-${p.attempt}` : 'outline'
})

/**
 * 待审出现时把审阅卡**顶部**滚进视野。
 *
 * 审阅卡一屏放不下（评分 + 审阅意见 + 正文预览 + 决策区），而对话流默认停在
 * 最底部——于是「评分系统」和「改稿意见」这两块长在卡片顶部的内容会被滚出视野，
 * 看起来像"没有显示"。这里改为主动对齐卡片顶部。
 */
async function scrollToReviewTop(): Promise<void> {
  await nextTick()
  const stream = streamRef.value
  const card = stream?.querySelector<HTMLElement>('.review-card')
  if (!stream || !card) return
  const delta = card.getBoundingClientRect().top - stream.getBoundingClientRect().top
  stream.scrollTop = Math.max(0, stream.scrollTop + delta - 8)
}

watch(reviewKey, (key) => {
  if (key) void scrollToReviewTop()
})

function onProposalDecided(): void {
  void pollStatus()
  // 打回会立刻按意见重做一版提案，重新拉一次列表才能看到新卡
  void loadProposals()
  appStore.treeVersion += 1
}

function onReviewDecided(): void {
  void pollStatus()
  appStore.treeVersion += 1
}

/** 动作确认/取消后：真正重取会话（拿到新落盘的回执），并刷新书目与资源树。 */
function onActionDecided(): void {
  pendingAction.value = null
  appStore.treeVersion += 1
  void syncBookAfterActions()
  if (appStore.chatId) void openChat(appStore.chatId)
  else void loadChats()
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

/**
 * 渲染错误隔离。
 *
 * 真实反馈过「互动出卡后整页点不动」：Vue 里只要有一个子组件渲染抛错，
 * 后续更新就会停住，表现正是"界面还在、点什么都没反应"。
 * 这里兜住子组件（审阅卡 / 提案卡 / 互动卡）的渲染异常：
 * 出错只在本区域显示提示，其余界面照常可点，并给一个恢复入口。
 */
const renderError = ref('')
onErrorCaptured((err) => {
  renderError.value = err instanceof Error ? err.message : String(err)
  console.error('[ChatPanel] 子组件渲染出错（已隔离，未影响其它区域）', err)
  return false
})

function recoverRender(): void {
  renderError.value = ''
  // 触发一次重渲染：把可能已被污染的卡片状态刷新掉
  void pollStatus()
  appStore.treeVersion += 1
}

/** 暂停生成：当前章写完后停在逐章确认关卡；已停在关卡则立即结束本轮。 */
async function pauseRun(): Promise<void> {
  if (inWorkspace()) return
  try {
    const res = await api<{ paused: boolean; immediate: boolean }>(
      'POST',
      withNovel('/api/pause', appStore.bookId),
    )
    message.success(
      res.immediate
        ? '已暂停：本轮到此为止，随时在对话框发指令即可继续'
        : '已标记暂停：当前这一章写完后停下，不会自动往下写',
    )
    void pollStatus()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

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
    lastActions.value = []
    pendingAction.value = null
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
    <!-- 渲染错误隔离条：出错只显示这一条，界面其余部分照样能点 -->
    <div v-if="renderError" class="render-error">
      <span>⚠ 有卡片渲染出错，已隔离（其它功能不受影响）：{{ renderError }}</span>
      <button class="ghost-btn" @click="recoverRender">重试</button>
    </div>
    <div class="chat-head">
      <div class="agent-block">
        <span class="agent-name">{{ agentLabel }}</span>
        <span v-if="inWorkspace()" class="chip workspace" title="未打开作品：墨师在全域工作台上工作，可建书/取资料/开写">
          🧭 工作区
        </span>
        <span v-if="appStore.bookTitle" class="muted ctx-label">
          主上下文：{{ proposeTargetLabel === '未选择' ? appStore.bookTitle : proposeTargetLabel }}
        </span>
        <!-- 工作状态常驻：综合对话 / 互动创作 / 流水线 / 待审提案，
             不再出现「智能体明明在干活却显示空闲」。 -->
        <span class="chip" :class="workStatus.kind">{{ workStatus.text }}</span>
        <span v-if="snapshot?.metrics" class="chip idle" title="已定稿 / 计划总章">
          已定稿 {{ snapshot.metrics.approved }}/{{ snapshot.metrics.total_chapters }}
        </span>
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
          <button
            v-if="snapshot?.started && !snapshot?.done"
            class="ghost-btn pause-btn"
            title="暂停生成：当前章写完后停在逐章确认关卡，不会自动往下写"
            @click="pauseRun"
          >
            ⏸ 暂停生成
          </button>
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

      <!-- 早于全部可见消息的提案卡（插回对话流原位，而不是永远堆在底部） -->
      <ProposalCard
        v-for="p in proposalsByAnchor[-1] ?? []"
        :key="p.id"
        :novel-id="appStore.bookId"
        :chat-id="appStore.chatId"
        :proposal="p"
        @decided="onProposalDecided"
      />

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

        <!-- 改稿提案卡（DeepWrite 的 proposal 语义：pending→accepting→accepted/rejected/conflict） -->
        <ProposalCard
          v-for="p in proposalsByAnchor[i] ?? []"
          :key="p.id"
          :novel-id="appStore.bookId"
          :chat-id="appStore.chatId"
          :proposal="p"
          @decided="onProposalDecided"
        />
      </template>

      <!--
        墨师动作卡：**固定在对话流末尾**（不属于任何一条历史消息）。
        原因：它承载"待确认"这个实时状态——确认之后这张卡要立刻变成"已执行"，
        若挂在某条消息上就会跟着那条旧消息一起定格，看起来像"按钮没变化"。
      -->
      <ActionCard
        v-if="lastActions.length || pendingAction"
        :novel-id="appStore.bookId"
        :chat-id="appStore.chatId"
        :actions="lastActions"
        :pending="pendingAction"
        :scope="inWorkspace() ? 'workspace' : 'book'"
        @decided="onActionDecided"
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
            title="改稿模式：发送即对右侧文档产出改稿提案（提案需你审批通过后才写入创作空间）"
            @click="proposeMode = !proposeMode"
          >
            ✎ 改稿
          </button>
          <span class="tool-hint" title="所有生成与修改一律先出提案，由你点『通过』后才写入创作空间">
            提案需人工审批
          </span>
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
.render-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 8px 12px 0;
  padding: 8px 12px;
  border: 1px solid #fde68a;
  border-radius: 8px;
  background: #fffbeb;
  color: #92400e;
  font-size: 12.5px;
  line-height: 1.6;
}
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
.chip.idle {
  background: #f3f4f6;
  color: #5c6470;
}
.chip.err {
  background: #fde8e8;
  color: #b42318;
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
.pause-btn {
  color: #b45309;
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
/*
 * 关键：.stream 是「定高列向 flex + overflow-y:auto」的滚动容器。
 * flex 项的默认 flex-shrink:1 会让浏览器**先压缩子项、再产生滚动**，
 * 于是消息卡 / 审阅卡 / 互动卡在窗口偏矮时被压扁（正文预览框可缩到 20 余 px、
 * 并被 sticky 的意见框盖住）。把收缩锁死，容器才会老老实实滚动。
 */
.stream > * {
  flex-shrink: 0;
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
.tool-hint {
  font-size: 11.5px;
  color: #8a8f99;
  align-self: center;
  white-space: nowrap;
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
