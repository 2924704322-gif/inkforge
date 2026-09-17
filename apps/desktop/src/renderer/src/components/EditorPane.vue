<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api } from '../api'
import { appStore } from '../store'
import type { ChapterRaw, ChapterSummary, SettingsDoc } from '../types'

interface DocState {
  kind: 'chapter' | 'settings'
  key: string
  title: string
  status: string
  content: string
  savedContent: string
}

const doc = ref<DocState | null>(null)
const mode = ref<'read' | 'edit'>('read')
const saving = ref(false)
const dirty = ref(false)
const chapters = ref<ChapterSummary[]>([])

/**
 * 当前正文是"由哪一次选中"加载出来的（格式：`<bookId>|<key>`）。
 *
 * 为什么要记：改稿目标取自 `appStore.selection`，而右侧正文是异步拉取的
 * （`loadSelection`）。两者一旦不同步（选中态已切到大纲、正文还停在第 1 章），
 * 改稿就会把指令作用到用户没在看的文稿上——用户看到的是"我明明选的是大纲"。
 * 现在把它作为"服务端目标自查"的判据暴露出去（见 `selectionMismatch`）。
 */
const loadedFrom = ref('')
const currentSelectionKey = computed(() => {
  const sel = appStore.selection
  if (!sel || !appStore.bookId) return ''
  const key = sel.kind === 'chapter' ? `ch-${sel.chapter}` : sel.rel
  return `${appStore.bookId}|${key}`
})
/** 选中态与已加载正文不一致（非空即不一致）。 */
const selectionMismatch = computed(
  () => currentSelectionKey.value !== '' && loadedFrom.value !== currentSelectionKey.value,
)

// 把对齐状态同步到全局：改稿发送前要用它做最后一道自查（见 store.docAligned）
watch(
  [loadedFrom, currentSelectionKey],
  () => {
    appStore.loadedDocKey = loadedFrom.value
    appStore.docAligned = loadedFrom.value !== '' && loadedFrom.value === currentSelectionKey.value
  },
  { immediate: true },
)

const wordCount = computed(() => (doc.value ? doc.value.content.replace(/\s/g, '').length : 0))
const isMarkdown = computed(() => doc.value?.kind === 'settings')
const crumbKind = computed(() => {
  if (!doc.value) return ''
  if (doc.value.kind === 'chapter') return '正文'
  if (doc.value.key.includes('worldview')) return '世界观'
  if (doc.value.key.includes('characters')) return '人物'
  return '设定'
})

async function loadChapters(): Promise<void> {
  if (!appStore.bookId) return
  try {
    const res = await api<{ chapters: ChapterSummary[] }>(
      'GET',
      `/api/chapters?novel=${encodeURIComponent(appStore.bookId)}`,
    )
    chapters.value = res.chapters
  } catch {
    chapters.value = []
  }
}

async function loadSelection(): Promise<void> {
  const sel = appStore.selection
  if (!sel || !appStore.bookId) {
    doc.value = null
    loadedFrom.value = ''
    return
  }
  const wantKey = currentSelectionKey.value
  const base = encodeURIComponent(appStore.bookId)
  try {
    if (sel.kind === 'chapter') {
      const raw = await api<ChapterRaw>('GET', `/api/chapters/${sel.chapter}/raw?novel=${base}`)
      doc.value = {
        kind: 'chapter',
        key: `ch-${raw.chapter}`,
        title: `第 ${raw.chapter} 章 ${raw.title || ''}`.trim(),
        status: raw.status,
        content: raw.content,
        savedContent: raw.content,
      }
    } else {
      const d = await api<SettingsDoc>(
        'GET',
        `/api/settings/doc?novel=${base}&rel=${encodeURIComponent(sel.rel)}`,
      )
      doc.value = {
        kind: 'settings',
        key: sel.rel,
        title: d.title || sel.title,
        status: '',
        content: d.content,
        savedContent: d.content,
      }
    }
    dirty.value = false
    mode.value = 'read'
    loadedFrom.value = wantKey
  } catch (err) {
    doc.value = {
      kind: sel.kind === 'chapter' ? 'chapter' : 'settings',
      key: sel.kind === 'chapter' ? `ch-${sel.chapter}` : sel.rel,
      title: sel.kind === 'chapter' ? `第 ${sel.chapter} 章` : sel.title,
      status: '',
      content: `加载失败：${err instanceof Error ? err.message : String(err)}`,
      savedContent: '',
    }
    // 加载失败时**不**标记为"已对齐"：改稿目标自查必须拦住这种状态下发指令
    loadedFrom.value = ''
  }
}

async function save(): Promise<void> {
  if (!doc.value || !dirty.value || saving.value) return
  saving.value = true
  const base = encodeURIComponent(appStore.bookId)
  try {
    if (doc.value.kind === 'chapter') {
      const chapter = Number(doc.value.key.slice(3))
      await api('PUT', `/api/chapters/${chapter}?novel=${base}`, { content: doc.value.content })
    } else {
      await api('PUT', `/api/settings/doc?novel=${base}`, {
        rel: doc.value.key,
        content: doc.value.content,
      })
    }
    doc.value.savedContent = doc.value.content
    dirty.value = false
    appStore.treeVersion += 1
  } finally {
    saving.value = false
  }
}

watch(
  () => appStore.selection,
  () => {
    void loadSelection()
    void loadChapters()
  },
  { deep: true },
)

watch(
  () => appStore.bookId,
  () => {
    void loadChapters()
  },
)

/**
 * 外部改写后必须重取正文。
 *
 * 接受改稿提案（ProposalCard → onProposalDecided）、流水线定稿、删除章节等
 * 都只自增 `appStore.treeVersion`；本组件原来只监听 selection/bookId，
 * 于是「让智能体改了正文，正文面板还是旧稿」——改动只落到 MD 事实源，
 * 界面看不到。有未保存编辑时不覆盖（那是用户正在写的内容）。
 */
watch(
  () => appStore.treeVersion,
  () => {
    if (!appStore.selection) return
    if (dirty.value) return
    void loadSelection()
    void loadChapters()
  },
)

function onInput(): void {
  dirty.value = doc.value !== null && doc.value.content !== doc.value.savedContent
}

function switchChapter(ch: number): void {
  appStore.selection = { kind: 'chapter', chapter: ch }
}
</script>

<template>
  <section class="editor-pane">
    <template v-if="doc && appStore.bookId">
      <div class="crumb-row">
        <div class="crumb">
          <span class="crumb-book">{{ appStore.bookTitle }}</span>
          <span class="crumb-sep">/</span>
          <span>{{ crumbKind }}</span>
          <template v-if="doc.kind === 'chapter'">
            <span class="crumb-sep">/</span>
            <span>{{ doc.title }}</span>
          </template>
          <span v-if="doc.status === 'approved'" class="status-tag ok">已定稿</span>
          <span v-else-if="doc.status === 'draft'" class="status-tag draft">草稿</span>
        </div>
        <span class="muted">{{ dirty ? '未保存' : '已保存到本机' }}</span>
      </div>

      <!-- 选中态与已加载正文不同步时显式告警：改稿目标取自选中态，
           不对齐就可能把改动落到用户没在看的文稿上（曾出现"选大纲、动第一章"）。 -->
      <div v-if="selectionMismatch" class="align-warn">
        ⚠ 右侧正文还没跟上你选中的文档：改稿前请在创作空间里重新点一次目标文稿
        <button class="align-retry" @click="loadSelection">重新加载</button>
      </div>

      <!-- 章节 Tab（DeepWrite 的横向小节标签） -->
      <div v-if="chapters.length > 0" class="chapter-tabs scroll-y">
        <button
          v-for="ch in chapters"
          :key="ch.chapter"
          class="ch-tab"
          :class="{ active: appStore.selection?.kind === 'chapter' && appStore.selection.chapter === ch.chapter }"
          @click="switchChapter(ch.chapter)"
        >
          第{{ ch.chapter }}章 · {{ ch.title || '未命名' }}
        </button>
      </div>

      <div class="toolbar">
        <div class="mode-group">
          <button class="mode-btn" :class="{ active: mode === 'read' }" @click="mode = 'read'">预览</button>
          <button class="mode-btn" :class="{ active: mode === 'edit' }" @click="mode = 'edit'">编辑</button>
        </div>
        <span class="toolbar-sep" />
        <span class="muted">{{ isMarkdown ? 'Markdown' : '正文' }}</span>
        <div class="toolbar-right">
          <button class="primary-btn" :disabled="!dirty || saving" @click="save">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>

      <div class="doc-area scroll-y">
        <div class="doc-title">{{ doc.title }}</div>
        <textarea
          v-if="mode === 'edit'"
          v-model="doc.content"
          class="doc-textarea"
          @input="onInput"
        />
        <div v-else class="pre-wrap doc-preview">{{ doc.content }}</div>
      </div>

      <div class="doc-foot">
        <span class="muted">{{ wordCount }} 字 · 本机文稿，保存后写入 Markdown 事实源</span>
      </div>
    </template>

    <div v-else class="empty">
      <div class="empty-mark">In</div>
      <div class="muted">从左侧选择大纲、章节或设定文档开始阅读与编辑</div>
    </div>
  </section>
</template>

<style scoped>
.editor-pane {
  flex: 0 0 clamp(400px, 34vw, 640px);
  width: clamp(400px, 34vw, 640px);
  display: flex;
  flex-direction: column;
  background: #fff;
  border-left: 1px solid #e9ebee;
}
.crumb-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 20px 8px;
}
.crumb {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #5c6470;
  overflow: hidden;
  white-space: nowrap;
}
.crumb-book {
  color: #26272b;
  font-weight: 600;
}
.crumb-sep {
  color: #c3c8cf;
}
.status-tag {
  font-size: 11px;
  border-radius: 999px;
  padding: 1px 8px;
}
.status-tag.ok {
  background: #e6f6ec;
  color: #116932;
}
.status-tag.draft {
  background: #fef3c7;
  color: #92400e;
}
.align-warn {
  margin: 0 16px 6px;
  padding: 5px 9px;
  border-radius: 6px;
  background: #fff7ed;
  border: 1px solid #fed7aa;
  color: #9a3412;
  font-size: 11.5px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.align-retry {
  margin-left: auto;
  border: 1px solid #fdba74;
  background: #fff;
  color: #9a3412;
  border-radius: 5px;
  font-size: 11px;
  padding: 1px 8px;
  cursor: pointer;
}
.chapter-tabs {
  display: flex;
  gap: 2px;
  padding: 0 16px;
  border-bottom: 1px solid #eef0f2;
  flex-shrink: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.ch-tab {
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  padding: 7px 12px;
  font-size: 13px;
  color: #5c6470;
  cursor: pointer;
  white-space: nowrap;
}
.ch-tab:hover {
  color: #26272b;
}
.ch-tab.active {
  color: #1d4ed8;
  border-bottom-color: #1d4ed8;
  font-weight: 600;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 20px;
  border-bottom: 1px solid #eef0f2;
}
.mode-group {
  display: flex;
  background: #f3f4f6;
  border-radius: 8px;
  padding: 2px;
}
.mode-btn {
  border: none;
  background: none;
  padding: 4px 14px;
  font-size: 12.5px;
  border-radius: 6px;
  cursor: pointer;
  color: #5c6470;
}
.mode-btn.active {
  background: #fff;
  color: #26272b;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
}
.toolbar-sep {
  width: 1px;
  height: 16px;
  background: #e5e7eb;
}
.toolbar-right {
  margin-left: auto;
}
.primary-btn {
  background: #1f2937;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 6px 18px;
  font-size: 13px;
  cursor: pointer;
}
.primary-btn:disabled {
  background: #d1d5db;
  cursor: default;
}
.doc-area {
  flex: 1;
  min-height: 0;
  padding: 18px 26px;
  display: flex;
  flex-direction: column;
}
.doc-title {
  font-size: 22px;
  font-weight: 700;
  margin-bottom: 14px;
  color: #26272b;
}
.doc-preview {
  font-size: 15px;
  line-height: 1.95;
  color: #26272b;
}
.doc-textarea {
  flex: 1;
}
.doc-foot {
  border-top: 1px solid #eef0f2;
  padding: 8px 20px;
}
.empty {
  margin: auto;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.empty-mark {
  width: 52px;
  height: 52px;
  border-radius: 14px;
  background: #f3f4f6;
  color: #9aa0aa;
  font-weight: 700;
  font-size: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
