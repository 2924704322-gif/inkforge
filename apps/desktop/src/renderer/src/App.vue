<script setup lang="ts">
import { NConfigProvider, NDialogProvider, NMessageProvider, zhCN, dateZhCN } from 'naive-ui'
import { onMounted, ref } from 'vue'

import { api } from './api'
import { appStore, rememberBook } from './store'
import BookshelfDialog from './components/BookshelfDialog.vue'
import ChatPanel from './components/ChatPanel.vue'
import EditorPane from './components/EditorPane.vue'
import FeatureNav from './components/FeatureNav.vue'
import ModelConfigDialog from './components/ModelConfigDialog.vue'
import SideWindows from './components/SideWindows.vue'
import StyleForgeDialog from './components/StyleForgeDialog.vue'
import WorkspaceTree from './components/WorkspaceTree.vue'

/* ── 三栏拖拽缩放（智能体 | 正文 | 创作空间）── */
const editorWidth = ref(clampWidth(Number(localStorage.getItem('inkforge.w.editor')) || 470, 360, 780))
const treeWidth = ref(clampWidth(Number(localStorage.getItem('inkforge.w.tree')) || 288, 224, 480))
const dragging = ref('')

function clampWidth(w: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, w))
}

function startDrag(which: 'editor' | 'tree', event: MouseEvent): void {
  event.preventDefault()
  dragging.value = which
  const startX = event.clientX
  const startW = which === 'editor' ? editorWidth.value : treeWidth.value
  const onMove = (ev: MouseEvent): void => {
    const delta = ev.clientX - startX
    if (which === 'editor') editorWidth.value = clampWidth(startW + delta, 360, 780)
    else treeWidth.value = clampWidth(startW - delta, 224, 480)
  }
  const onUp = (): void => {
    dragging.value = ''
    localStorage.setItem('inkforge.w.editor', String(editorWidth.value))
    localStorage.setItem('inkforge.w.tree', String(treeWidth.value))
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

onMounted(() => {
  void window.inkforge
    .engineState()
    .then((state) => {
      appStore.engineState = state.state
      appStore.enginePort = state.port
      appStore.engineMessage = state.message
    })
    .catch(() => undefined)
  window.inkforge.onEngineState((event) => {
    appStore.engineState = event.state
    appStore.enginePort = event.port
    appStore.engineMessage = event.message
  })
  // 首次启动**不**强制弹书架：无作品时直接进入"工作区"模式，
  // 墨师仍可对话、查书目、建新书（顶部书目标识处可随时切换/新建）。
  if (!appStore.bookId) void syncActiveBook()
})

/** 与引擎对齐"工作台当前书目"（墨师在工作区会话里操作的目标）。 */
async function syncActiveBook(): Promise<void> {
  try {
    const res = await api<{ novel_id: string; exists?: boolean }>(
      'GET',
      '/api/book-select',
    ).catch(() => null)
    if (res?.novel_id && res.exists !== false) {
      appStore.bookId = res.novel_id
      appStore.bookTitle = res.novel_id
      // 也要记住"最近打开过的书"：工作区顶栏的「↩ 回到《X》」靠它渲染。
      // 这里只记录、不调用 openBook()——启动同步不该触发选中/会话重置。
      rememberBook(res.novel_id, res.novel_id)
    }
    await api('PUT', '/api/book-select', { novel_id: appStore.bookId }).catch(
      () => undefined,
    )
  } catch {
    /* 静默：书目标记不影响其它功能 */
  }
}
</script>

<template>
  <NConfigProvider :locale="zhCN" :date-locale="dateZhCN">
    <NMessageProvider placement="top">
      <NDialogProvider>
        <div class="shell" :class="{ dragging: dragging !== '' }">
          <FeatureNav />
          <SideWindows />
          <ChatPanel />
          <div class="splitter" title="拖拽调整宽度" @mousedown="startDrag('editor', $event)" />
          <EditorPane :style="{ width: editorWidth + 'px', flex: '0 0 ' + editorWidth + 'px' }" />
          <div class="splitter" title="拖拽调整宽度" @mousedown="startDrag('tree', $event)" />
          <WorkspaceTree :style="{ width: treeWidth + 'px', flex: '0 0 ' + treeWidth + 'px' }" />

          <BookshelfDialog v-model:show="appStore.dialogs.bookshelf" />
          <ModelConfigDialog v-model:show="appStore.dialogs.modelConfig" />
          <StyleForgeDialog v-model:show="appStore.dialogs.styleForge" />
        </div>
      </NDialogProvider>
    </NMessageProvider>
  </NConfigProvider>
</template>

<style scoped>
.shell {
  display: flex;
  height: 100vh;
  background: #ffffff;
}
.shell.dragging {
  cursor: col-resize;
  user-select: none;
}
.splitter {
  width: 5px;
  flex-shrink: 0;
  cursor: col-resize;
  background: transparent;
  transition: background 0.15s;
}
.splitter:hover,
.shell.dragging .splitter {
  background: #dbe6fe;
}
</style>
