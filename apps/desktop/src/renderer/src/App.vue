<script setup lang="ts">
import { NConfigProvider, NDialogProvider, NMessageProvider, zhCN, dateZhCN } from 'naive-ui'
import { onMounted, ref, watch } from 'vue'

import { appStore } from './store'
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
  if (!appStore.bookId) appStore.dialogs.bookshelf = true
})

watch(
  () => appStore.bookId,
  (id) => {
    if (!id) appStore.dialogs.bookshelf = true
  },
)
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
