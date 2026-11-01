<script setup lang="ts">
import {
  NConfigProvider,
  NDialogProvider,
  NMessageProvider,
  dateZhCN,
  zhCN,
  type GlobalThemeOverrides,
} from 'naive-ui'
import { computed, onMounted, onUnmounted, ref } from 'vue'

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

/* ═══════════════════════════════════════════════════════════════
 * 四区栅格（导航 | 对话 | 正文 | 创作空间）
 *
 * 改造前是 flex + 四列定宽：176 + 对话(flex,min340) + 正文(0 0 Npx) + 创作空间(0 0 Npx)。
 * 只要用户把正文/创作空间拖宽过一次（宽度写进 localStorage，下次启动还原），
 * 总和就会超过视口，而 body{overflow:hidden} 又让人滚不到 —— 右侧整列被裁掉。
 * 仓库自带的 ui-probe 实测过：1366 屏拖满后 shellScrollW 1786 / clientWidth 1366。
 *
 * 现在：grid + 每列可折叠 + **宽度按视口夹紧**，任何组合都不会横向溢出。
 * ═══════════════════════════════════════════════════════════════ */
const NAV_W = 176
const NAV_W_COLLAPSED = 56
const SPLITTER_W = 8
const CHAT_MIN = 360
const DOC_MIN = 360
const DOC_MAX = 860
const TREE_MIN = 224
const TREE_MAX = 480
const TREE_W_COLLAPSED = 56

const editorWidth = ref(clampWidth(readNumber('inkforge.w.editor', 470), DOC_MIN, DOC_MAX))
const treeWidth = ref(clampWidth(readNumber('inkforge.w.tree', 288), TREE_MIN, TREE_MAX))
const dragging = ref('')
const viewportW = ref(typeof window === 'undefined' ? 1366 : window.innerWidth)

function clampWidth(w: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, w))
}

function readNumber(key: string, fallback: number): number {
  try {
    const raw = Number(localStorage.getItem(key))
    return Number.isFinite(raw) && raw > 0 ? raw : fallback
  } catch {
    return fallback
  }
}

function writeNumber(key: string, value: number): void {
  try {
    localStorage.setItem(key, String(Math.round(value)))
  } catch {
    /* 偏好丢了只是下次恢复默认 */
  }
}

const railW = computed(() => (appStore.navCollapsed ? NAV_W_COLLAPSED : NAV_W))

/**
 * 把「正文列 + 创作空间」压进视口预算里（**纯函数**，不写回状态，避免自我循环）。
 *
 * 预算 = 视口 − 导航 − 两条中线 − 对话列下限(360)。
 * 压缩顺序：正文降到 360 → 创作空间降到 224 → 创作空间折叠成 56。
 */
const layout = computed(() => {
  const vw = viewportW.value || 1366
  const rail = railW.value
  const budget = Math.max(DOC_MIN + TREE_W_COLLAPSED, vw - rail - SPLITTER_W * 2 - CHAT_MIN)
  const manual = appStore.treeCollapsed
  let doc = clampWidth(editorWidth.value, DOC_MIN, DOC_MAX)
  let tree = manual ? TREE_W_COLLAPSED : clampWidth(treeWidth.value, TREE_MIN, TREE_MAX)
  let auto = false
  if (doc + tree > budget) {
    if (manual) {
      doc = Math.max(DOC_MIN, Math.min(doc, budget - tree))
    } else {
      tree = Math.max(TREE_MIN, Math.min(tree, budget - doc))
      if (doc + tree > budget) doc = Math.max(DOC_MIN, budget - tree)
      if (doc + tree > budget) {
        auto = true
        tree = TREE_W_COLLAPSED
        doc = Math.max(DOC_MIN, budget - tree)
      }
    }
  }
  return { rail, doc, tree, auto, budget }
})

/** 生效的折叠态（用户手动 或 视口自动）。 */
const treeCollapsedNow = computed(() => layout.value.tree <= TREE_W_COLLAPSED)

const shellStyle = computed(() => ({
  '--rail-w': `${layout.value.rail}px`,
  '--doc-w': `${layout.value.doc}px`,
  '--tree-w': `${layout.value.tree}px`,
  '--splitter-w': `${SPLITTER_W}px`,
}))

/**
 * 分栏拖拽。
 *
 * 方向口径：**中线跟着鼠标走**。中线左边是对话列（grid 的 1fr 吸收剩余），
 * 右边是正文列（定宽）。
 *   · 对话|正文：右拖 ⇒ 对话列变宽、正文列变窄 ⇒ 正文宽 = 起始 − 位移
 *   · 正文|创作空间：两侧同时让/收，对话列不动
 * 改造前是 `clampWidth(startW + delta)`：正文列反向长，中线被挤得逆着鼠标跑。
 */
function startDrag(which: 'editor' | 'tree', event: MouseEvent): void {
  if (which === 'tree' && treeCollapsedNow.value) return
  event.preventDefault()
  dragging.value = which
  const startX = event.clientX
  const startDoc = layout.value.doc
  const startTree = layout.value.tree
  const budget = layout.value.budget
  const maxDoc = Math.max(DOC_MIN, Math.min(DOC_MAX, budget - startTree))

  const onMove = (ev: MouseEvent): void => {
    const delta = ev.clientX - startX
    if (which === 'editor') {
      editorWidth.value = clampWidth(startDoc - delta, DOC_MIN, maxDoc)
      return
    }
    const tree = clampWidth(startTree - delta, TREE_MIN, TREE_MAX)
    const applied = startTree - tree
    treeWidth.value = tree
    editorWidth.value = clampWidth(startDoc + applied, DOC_MIN, Math.max(DOC_MIN, budget - tree))
  }
  const onUp = (): void => {
    dragging.value = ''
    writeNumber('inkforge.w.editor', editorWidth.value)
    writeNumber('inkforge.w.tree', treeWidth.value)
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
}

function onResize(): void {
  viewportW.value = window.innerWidth
}

/** 双击中线复位到默认宽度。 */
function resetWidths(): void {
  editorWidth.value = 470
  treeWidth.value = 288
  writeNumber('inkforge.w.editor', 470)
  writeNumber('inkforge.w.tree', 288)
}

/** 与 naive-ui 对齐：改造前没传 themeOverrides，原生组件与自研组件两套观感。 */
const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#1d4ed8',
    primaryColorHover: '#2563eb',
    primaryColorPressed: '#1e40af',
    primaryColorSuppl: '#2563eb',
    borderRadius: '8px',
    borderRadiusSmall: '6px',
    fontSize: '13px',
    fontSizeSmall: '12px',
    fontSizeMedium: '13px',
    textColorBase: '#1e2024',
    textColor1: '#1e2024',
    textColor2: '#3a3d44',
    textColor3: '#5c6470',
    borderColor: '#dcdfe4',
    dividerColor: '#eef0f2',
    fontFamily:
      "'Segoe UI', 'Microsoft YaHei', 'PingFang SC', 'Noto Sans SC', system-ui, sans-serif",
  },
}

onMounted(() => {
  viewportW.value = window.innerWidth
  window.addEventListener('resize', onResize)
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

onUnmounted(() => {
  window.removeEventListener('resize', onResize)
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
  <NConfigProvider :locale="zhCN" :date-locale="dateZhCN" :theme-overrides="themeOverrides">
    <NMessageProvider placement="top">
      <NDialogProvider>
        <div class="shell" :class="{ dragging: dragging !== '' }" :style="shellStyle">
          <FeatureNav />
          <ChatPanel />
          <div
            class="splitter editor"
            title="拖拽调整宽度（右拖 = 对话列变宽）· 双击复位"
            @mousedown="startDrag('editor', $event)"
            @dblclick="resetWidths"
          >
            <span class="grip" />
          </div>
          <!-- 功能小窗（智能体设置 / 学习仿写 / 素材库 / 正文改写 / 数据看板 / 导出中心）
               用悬浮页形态盖在左侧，正文列保持可见（作者指定形态）。 -->
          <EditorPane />
          <div
            class="splitter tree"
            :class="{ off: treeCollapsedNow }"
            :title="
              treeCollapsedNow
                ? '创作空间已折叠（点标题栏的 ▯ 展开）'
                : '拖拽调整宽度（右拖 = 创作空间变窄）'
            "
            @mousedown="startDrag('tree', $event)"
          >
            <span class="grip" />
          </div>
          <WorkspaceTree :collapsed="treeCollapsedNow" />

          <SideWindows />
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
  display: grid;
  grid-template-columns:
    var(--rail-w, 176px) minmax(0, 1fr) var(--splitter-w, 8px) var(--doc-w, 470px)
    var(--splitter-w, 8px) var(--tree-w, 288px);
  grid-template-rows: minmax(0, 1fr);
  height: 100vh;
  overflow: hidden;
  background: var(--bg-0);
}

/*
 * 显式落位：DOM 顺序与视觉顺序解耦（侧窗要能借用正文列那一格）。
 * Vue 会把父组件的 scope id 打到子组件根元素上，所以这里能直接选中子组件根。
 */
.shell > :deep(.feature-nav) {
  grid-column: 1;
}
.shell > :deep(.chat-panel) {
  grid-column: 2;
}
.shell > .splitter.editor {
  grid-column: 3;
}
.shell > :deep(.editor-pane) {
  grid-column: 4;
  min-width: 0;
}
.shell > .splitter.tree {
  grid-column: 5;
}
.shell > :deep(.workspace) {
  grid-column: 6;
  min-width: 0;
}

.shell.dragging {
  cursor: col-resize;
  user-select: none;
}

/* 中线：8px 命中区（原 5px 且全透明，几乎点不中），视觉上只有 1px 线 */
.splitter {
  position: relative;
  cursor: col-resize;
  background: transparent;
  display: flex;
  justify-content: center;
  transition: background var(--dur-1) var(--ease-out);
}
.splitter .grip {
  width: 1px;
  height: 100%;
  background: var(--line-1);
  transition:
    width var(--dur-1) var(--ease-out),
    background var(--dur-1) var(--ease-out);
}
.splitter:hover .grip,
.shell.dragging .splitter .grip {
  width: 3px;
  background: #93b4f8;
}
.splitter.off {
  cursor: default;
}
.splitter.off:hover .grip {
  width: 1px;
  background: var(--line-1);
}
</style>
