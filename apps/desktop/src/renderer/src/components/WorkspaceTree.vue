<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { api, withNovel } from '../api'
import { appStore, type DocSelection } from '../store'
import type { ChapterSummary, SettingsTreeItem } from '../types'

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

function isSelected(sel: DocSelection, kind: string, key: string | number): boolean {
  if (!sel) return false
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
  <aside class="workspace">
    <div class="ws-head">
      <div class="ws-title">创作空间</div>
      <button
        v-if="appStore.bookId"
        class="ws-switch"
        title="切换作品"
        @click="appStore.dialogs.bookshelf = true"
      >
        切换
      </button>
    </div>
    <div v-if="appStore.bookId" class="ws-book">📖 {{ appStore.bookTitle }}</div>

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
            <span class="t-icon ch-icon" :class="ch.status === 'approved' ? 'st-ok' : 'st-draft'">●</span>
            <span class="t-no">{{ ch.chapter }}</span>
            <span class="t-title">{{ ch.title || '未命名' }}</span>
            <span class="row-ops" @click.stop>
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
  </aside>
</template>

<style scoped>
.workspace {
  display: flex;
  flex-direction: column;
  background: #fbfbfc;
  min-width: 0;
}
.ws-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 16px 6px;
}
.ws-title {
  font-weight: 700;
  font-size: 14px;
  color: #26272b;
}
.ws-switch {
  background: none;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  padding: 2px 10px;
  font-size: 12px;
  color: #5c6470;
  cursor: pointer;
}
.ws-book {
  padding: 0 16px 8px;
  font-size: 13px;
  color: #3a3d44;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-empty {
  padding: 16px;
  color: #8a8f99;
  font-size: 13px;
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
  font-size: 12px;
  font-weight: 600;
  color: #5c6470;
  padding: 8px 10px 4px;
  cursor: pointer;
  text-align: left;
}
.chev {
  color: #9aa0aa;
  font-size: 10px;
  width: 12px;
}
.head-op {
  margin-left: auto;
  color: #8a8f99;
  font-size: 13px;
  padding: 0 4px;
  border-radius: 4px;
}
.head-op:hover {
  background: #e8eaee;
  color: #26272b;
}
.sub-head {
  font-size: 11.5px;
  color: #9aa0aa;
  padding: 4px 10px 2px 28px;
}
.tree-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 10px;
  border-radius: 7px;
  cursor: pointer;
  font-size: 13px;
  color: #3a3d44;
  margin-left: 12px;
}
.tree-item:hover {
  background: #f0f1f3;
}
.tree-item.active {
  background: #e8f0fe;
  color: #1d4ed8;
}
.t-icon {
  width: 18px;
  text-align: center;
  font-size: 12px;
  color: #9aa0aa;
  flex-shrink: 0;
}
.tree-item.active .t-icon {
  color: #1d4ed8;
}
.ch-icon {
  font-size: 8px;
}
.st-ok {
  color: #16a34a;
}
.st-draft {
  color: #f59e0b;
}
.t-no {
  width: 20px;
  text-align: right;
  color: #9aa0aa;
  font-size: 12px;
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
.tree-item:hover .row-ops {
  display: inline-flex;
}
.row-op {
  border: none;
  background: none;
  color: #8a8f99;
  font-size: 11px;
  cursor: pointer;
  padding: 1px 5px;
  border-radius: 5px;
}
.row-op:hover {
  background: #e8eaee;
  color: #dc2626;
}
</style>
