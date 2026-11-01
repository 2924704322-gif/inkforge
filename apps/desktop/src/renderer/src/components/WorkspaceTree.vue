<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { api, withNovel } from '../api'
import { appStore, openRevision, setTreeCollapsed, type DocSelection } from '../store'
import type { ChapterSummary, SettingsTreeItem } from '../types'

/**
 * 折叠态由 App.vue 的栅格计算决定后传进来（窄屏自动折叠 / 用户手动折叠都走这里），
 * 组件自身不改宽度 —— 宽度归 --tree-w 管，避免"组件和栅格各写一套"。
 */
const props = defineProps<{ collapsed?: boolean }>()
const collapsed = computed(() => props.collapsed === true)

interface TreeChapter {
  chapter: number
  title: string
  status: string
  volume: number
}

const chapters = ref<TreeChapter[]>([])
const settingsDocs = ref<SettingsTreeItem[]>([])
const outlineExists = ref(false)
const addingChapter = ref(false)
const expanded = ref({ outline: true, volumes: true, settings: true })

const volumes = computed(() => {
  const map = new Map<number, TreeChapter[]>()
  for (const ch of [...chapters.value].sort((a, b) => a.chapter - b.chapter)) {
    const list = map.get(ch.volume) ?? []
    list.push(ch)
    map.set(ch.volume, list)
  }
  return [...map.entries()].sort((a, b) => a[0] - b[0]).map(([volume, items]) => ({ volume, items }))
})

const kindIcon = (item: SettingsTreeItem): string =>
  item.kind === 'character' ? '👤' : item.kind === 'worldview' ? '🌐' : '📄'

async function loadTree(): Promise<void> {
  if (!appStore.bookId) {
    chapters.value = []
    settingsDocs.value = []
    outlineExists.value = false
    return
  }
  const base = encodeURIComponent(appStore.bookId)
  try {
    const [chapterList, tree, outline] = await Promise.all([
      api<{ chapters: ChapterSummary[] }>('GET', `/api/chapters?novel=${base}`),
      api<{ items: SettingsTreeItem[] }>('GET', `/api/settings/tree?novel=${base}`),
      api<{ html: string | null }>('GET', `/api/outline?novel=${base}`),
    ])
    chapters.value = chapterList.chapters.map((c) => ({
      chapter: c.chapter,
      title: c.title,
      status: c.status,
      volume: c.volume,
    }))
    settingsDocs.value = tree.items
    outlineExists.value = outline.html !== null
  } catch {
    /* 引擎未就绪时静默 */
  }
}

async function addChapter(): Promise<void> {
  if (!appStore.bookId || addingChapter.value) return
  addingChapter.value = true
  try {
    const res = await api<{ chapter: number }>(
      'POST',
      withNovel('/api/chapters', appStore.bookId),
      { title: '未命名' },
    )
    appStore.selection = { kind: 'chapter', chapter: res.chapter }
    appStore.treeVersion += 1
  } finally {
    addingChapter.value = false
  }
}

async function removeChapter(ch: TreeChapter): Promise<void> {
  if (!appStore.bookId) return
  if (!window.confirm(`确定删除「第 ${ch.chapter} 章 ${ch.title || '未命名'}」？其正文将不可恢复。`)) return
  await api('DELETE', withNovel(`/api/chapters/${ch.chapter}`, appStore.bookId))
  if (appStore.selection?.kind === 'chapter' && appStore.selection.chapter === ch.chapter) {
    appStore.selection = null
  }
  appStore.treeVersion += 1
}

/**
 * 章节行「✎」：直接对这一章发起改写（不必先切到正文栏）。
 *
 * 与正文栏那个入口共用 `openRevision()` 通道 —— 回填目标一并带上，
 * 改完就能「回填本章草稿」而不是让用户自己复制粘贴。
 */
async function reviseChapter(ch: TreeChapter): Promise<void> {
  if (!appStore.bookId) return
  try {
    const res = await api<{ content: string }>(
      'GET',
      withNovel(`/api/chapters/${ch.chapter}/raw`, appStore.bookId),
    )
    openRevision({
      source: res.content || '',
      origin: `创作空间 · 第 ${ch.chapter} 章`,
      fillBack: { chapter: ch.chapter, novelId: appStore.bookId },
    })
  } catch (err) {
    window.alert(`读取第 ${ch.chapter} 章失败：${err instanceof Error ? err.message : String(err)}`)
  }
}

function isSelected(sel: DocSelection, kind: string, key: string | number): boolean {  if (!sel) return false
  if (sel.kind === 'chapter' && kind === 'chapter') return sel.chapter === key
  if (sel.kind === 'settings' && kind === 'settings') return sel.rel === key
  return false
}

watch(
  () => [appStore.treeVersion, appStore.bookId],
  () => void loadTree(),
)

onMounted(() => {
  void loadTree()
})
</script>

<template>
  <aside class="workspace" :class="{ collapsed }" aria-label="创作空间">
    <!-- 折叠态：56px 竖条（窄屏时把宽度让给对话/正文，点一下即展开） -->
    <template v-if="collapsed">
      <button class="strip-btn" title="展开创作空间" aria-label="展开创作空间" @click="setTreeCollapsed(false)">
        <svg class="strip-ico" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <rect x="2.6" y="3" width="10.8" height="10" rx="1.4" />
          <path d="M10.4 3v10" />
        </svg>
      </button>
      <button class="strip-btn" title="章节" aria-label="章节" @click="setTreeCollapsed(false)">
        <svg class="strip-ico" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 4.4h10M3 8h10M3 11.6h10" />
        </svg>
      </button>
      <button class="strip-btn" title="设定资料库" aria-label="设定资料库" @click="setTreeCollapsed(false)">
        <svg class="strip-ico" viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M2.4 4.6h4l1.2 1.6h6v6H2.4Z" />
        </svg>
      </button>
      <span class="strip-label">创作空间</span>
    </template>

    <template v-else>
      <div class="ws-head">
        <div class="ws-title">创作空间</div>
        <span class="ws-ops">
          <button
            v-if="appStore.bookId"
            class="ws-switch"
            title="切换作品"
            @click="appStore.dialogs.bookshelf = true"
          >
            切换
          </button>
          <button
            class="icon-btn"
            title="折叠成图标条（把宽度让给对话/正文）"
            aria-label="折叠创作空间"
            @click="setTreeCollapsed(true)"
          >
            <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <rect x="2.6" y="3" width="10.8" height="10" rx="1.4" />
              <path d="M10.4 3v10" />
            </svg>
          </button>
        </span>
      </div>
      <div v-if="appStore.bookId" class="ws-book" :title="appStore.bookTitle">《{{ appStore.bookTitle }}》</div>

      <div v-if="!appStore.bookId" class="ws-empty">
        尚未选择作品。<br />点击上方「切换」或左侧「书架」。
      </div>

      <div v-else class="tree scroll-y">
        <!-- 大纲 -->
        <button class="group-head" @click="expanded.outline = !expanded.outline">
          <span class="chev">{{ expanded.outline ? '▾' : '▸' }}</span> 大纲
        </button>
        <template v-if="expanded.outline">
          <div
            class="tree-item"
            :class="{ active: isSelected(appStore.selection, 'settings', 'settings/outline.md') }"
            :style="outlineExists ? '' : 'opacity:.45'"
            @click="appStore.selection = { kind: 'settings', rel: 'settings/outline.md', title: '大纲' }"
          >
            <span class="t-icon">◆</span><span class="t-title">全书大纲</span>
          </div>
        </template>

        <!-- 章节 -->
        <button class="group-head" @click="expanded.volumes = !expanded.volumes">
          <span class="chev">{{ expanded.volumes ? '▾' : '▸' }}</span> 章节
          <span class="head-op" title="追加新章节" @click.stop="addChapter">＋</span>
        </button>
        <template v-if="expanded.volumes">
          <template v-for="group in volumes" :key="group.volume">
            <div class="sub-head">第 {{ group.volume }} 卷</div>
            <div
              v-for="ch in group.items"
              :key="ch.chapter"
              class="tree-item"
              :class="{ active: isSelected(appStore.selection, 'chapter', ch.chapter) }"
              @click="appStore.selection = { kind: 'chapter', chapter: ch.chapter }"
            >
              <span v-if="ch.status === 'approved'" class="t-icon ch-icon st-ok">●</span>
              <span v-else class="t-icon ch-icon st-draft">●</span>
              <span class="t-no">{{ ch.chapter }}</span>
              <span class="t-title">{{ ch.title || '未命名' }}</span>
              <span class="row-ops" @click.stop>
                <button class="row-op" title="改写这一章（只改你点名的地方）" @click="reviseChapter(ch)">✎</button>
                <button class="row-op danger" title="删除本章" @click="removeChapter(ch)">✕</button>
              </span>
            </div>
          </template>
          <div v-if="chapters.length === 0" class="muted" style="padding: 2px 10px 6px">
            暂无章节
          </div>
        </template>

        <!-- 设定资料库 -->
        <button class="group-head" @click="expanded.settings = !expanded.settings">
          <span class="chev">{{ expanded.settings ? '▾' : '▸' }}</span> 设定资料库
        </button>
        <template v-if="expanded.settings">
          <div
            v-for="doc in settingsDocs"
            :key="doc.rel"
            class="tree-item"
            :class="{ active: isSelected(appStore.selection, 'settings', doc.rel) }"
            @click="appStore.selection = { kind: 'settings', rel: doc.rel, title: doc.title }"
          >
            <span class="t-icon">{{ kindIcon(doc) }}</span>
            <span class="t-title">{{ doc.title }}</span>
          </div>
          <div v-if="settingsDocs.length === 0" class="muted" style="padding: 2px 10px 6px">
            暂无设定文档
          </div>
        </template>
      </div>
    </template>
  </aside>
</template>

<style scoped>
/* 宽度由 App.vue 的栅格（--tree-w）决定，这里只声明"填满所在格" */
.workspace {
  display: flex;
  flex-direction: column;
  min-width: 0;
  height: 100%;
  background: var(--bg-1);
  border-left: 1px solid var(--line-1);
}

/* ── 折叠态：56px 竖条 ─────────────────────────────── */
.workspace.collapsed {
  align-items: center;
  padding-top: 12px;
  gap: 6px;
  border-left: 1px solid var(--line-1);
}
.strip-btn {
  width: 34px;
  height: 34px;
  border: none;
  background: none;
  border-radius: var(--r-2);
  color: var(--ink-3);
  cursor: pointer;
  display: grid;
  place-items: center;
}
.strip-btn:hover {
  background: var(--bg-2);
  color: var(--ink-1);
}
.strip-ico {
  width: 16px;
  height: 16px;
}
.strip-label {
  writing-mode: vertical-rl;
  font-size: var(--fs-11);
  color: var(--ink-5);
  letter-spacing: 0.14em;
  margin-top: 6px;
}

.ws-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 12px 6px 16px;
  gap: 8px;
}
.ws-title {
  font-weight: 700;
  font-size: var(--fs-13);
  color: var(--ink-1);
  white-space: nowrap;
}
.ws-ops {
  display: flex;
  align-items: center;
  gap: 4px;
}
.ws-switch {
  background: none;
  border: 1px solid var(--line-3);
  border-radius: var(--r-1);
  padding: 2px 10px;
  font-size: var(--fs-12);
  color: var(--ink-3);
  cursor: pointer;
  font-family: inherit;
}
.ws-switch:hover {
  background: var(--bg-2);
  color: var(--ink-1);
}
.icon-btn {
  width: 26px;
  height: 26px;
  border: none;
  background: none;
  border-radius: var(--r-1);
  color: var(--ink-5);
  cursor: pointer;
  display: grid;
  place-items: center;
}
.icon-btn:hover {
  background: var(--bg-2);
  color: var(--ink-2);
}
.ws-book {
  padding: 0 16px 8px;
  font-size: var(--fs-13);
  color: var(--ink-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-empty {
  padding: 16px;
  color: var(--ink-4);
  font-size: var(--fs-13);
  line-height: 1.8;
}
.tree {
  flex: 1;
  min-height: 0;
  padding: 0 10px 10px;
}
.group-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  background: none;
  border: none;
  font-size: var(--fs-12);
  font-weight: 600;
  color: var(--ink-3);
  padding: 8px 10px 4px;
  cursor: pointer;
  text-align: left;
  font-family: inherit;
}
.group-head:hover {
  color: var(--ink-1);
}
.chev {
  color: var(--ink-5);
  font-size: 10px;
  width: 12px;
}
.head-op {
  margin-left: auto;
  color: var(--ink-4);
  font-size: var(--fs-13);
  padding: 0 4px;
  border-radius: var(--r-1);
}
.head-op:hover {
  background: var(--bg-2);
  color: var(--ink-1);
}
.sub-head {
  font-size: var(--fs-12);
  color: var(--ink-5);
  padding: 4px 10px 2px 28px;
}
.tree-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 10px;
  border-radius: 7px;
  cursor: pointer;
  font-size: var(--fs-13);
  color: var(--ink-2);
  margin-left: 12px;
}
.tree-item:hover {
  background: var(--bg-2);
}
.tree-item.active {
  background: var(--accent-bg);
  color: var(--accent);
}
.t-icon {
  width: 18px;
  text-align: center;
  font-size: var(--fs-12);
  color: var(--ink-5);
  flex-shrink: 0;
}
.tree-item.active .t-icon {
  color: var(--accent);
}
.ch-icon {
  font-size: 8px;
}
.st-ok {
  color: var(--ok-dot);
}
.st-draft {
  color: var(--warn-dot);
}
.t-no {
  width: 20px;
  text-align: right;
  color: var(--ink-5);
  font-size: var(--fs-12);
  flex-shrink: 0;
}
.t-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-ops {
  margin-left: auto;
  display: none;
}
.tree-item:hover .row-ops,
.tree-item:focus-within .row-ops {
  display: inline-flex;
}
.row-op {
  border: none;
  background: none;
  color: var(--ink-4);
  font-size: var(--fs-11);
  cursor: pointer;
  padding: 1px 5px;
  border-radius: 5px;
}
.row-op:hover {
  background: #e8eaee;
  color: var(--ink-1);
}
.row-op.danger:hover {
  color: var(--bad-dot);
}
</style>
