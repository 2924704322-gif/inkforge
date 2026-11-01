<script setup lang="ts">
import { computed, onMounted } from 'vue'

import {
  appStore,
  inWorkspace,
  resetToMainChat,
  setNavCollapsed,
  type SideWindowId,
} from '../store'

type NavId = Exclude<SideWindowId, null> | 'bookshelf' | 'styleForge' | 'modelConfig' | 'mainChat'

interface NavItem {
  id: NavId
  icon: IconName
  label: string
  kind: 'window' | 'dialog' | 'action'
  title?: string
}

type IconName =
  | 'chat'
  | 'sliders'
  | 'pen'
  | 'folder'
  | 'editdoc'
  | 'chart'
  | 'boxout'
  | 'book'
  | 'flask'
  | 'gear'

/**
 * 单色 SVG 图标（16px，stroke:currentColor）。
 *
 * 为什么不用 emoji：emoji 无法跟随 hover/选中态变色，Windows 与 macOS 尺寸也不一致，
 * 折叠成图标条后尤其明显。整套 10 个图标的路径都写死在这里，不引外部依赖。
 */
const ICONS: Record<IconName, string> = {
  chat: '<path d="M2.7 4.4h10.6v7.2H6.6L3.6 13.3v-1.7H2.7Z"/>',
  sliders:
    '<path d="M2.5 4.6h11M2.5 8h11M2.5 11.4h11"/>' +
    '<circle cx="6" cy="4.6" r="1.4"/><circle cx="10.4" cy="8" r="1.4"/><circle cx="5" cy="11.4" r="1.4"/>',
  pen: '<path d="M3 13l.8-3 6.6-6.6 2.2 2.2L6 12.2 3 13Z"/><path d="M9.6 4.2 11 2.8l2.2 2.2-1.4 1.4"/>',
  folder: '<path d="M2.4 4.6h4l1.2 1.6h6v6H2.4Z"/>',
  editdoc: '<path d="M4 2.6h5l3 3v8H4Z"/><path d="M6.4 11.4l.5-1.8 3-3 1.3 1.3-3 3Z"/>',
  chart: '<path d="M2.6 13.2h10.8"/><path d="M4.6 13V9.2M7.4 13V6.2M10.2 13V4"/>',
  boxout: '<path d="M3 9.4v3.4h10V9.4"/><path d="M8 10.4V2.8"/><path d="M5.6 5.2 8 2.8l2.4 2.4"/>',
  book: '<path d="M2.8 3.2h4.4v9.6H2.8Z"/><path d="M8.8 3.2h4.4v9.6H8.8Z"/>',
  flask:
    '<path d="M6.4 2.6h3.2v3.2l2.8 6.2a1 1 0 0 1-.9 1.4H4.5a1 1 0 0 1-.9-1.4L6.4 5.8Z"/>',
  gear: '<circle cx="8" cy="8" r="2.4"/><path d="M8 2.2v1.6M8 12.2v1.6M2.2 8h1.6M12.2 8h1.6"/>',
}

/** v-html 必须给出完整 <svg> 根元素，否则 path/circle 不会被解析。 */
const SVG_ATTRS =
  'viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" ' +
  'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"'

function iconMarkup(name: IconName): string {
  return `<svg ${SVG_ATTRS}>${ICONS[name]}</svg>`
}

const groups: { title: string; items: NavItem[] }[] = [
  {
    title: '主对话',
    items: [
      {
        id: 'mainChat',
        icon: 'chat',
        label: '主对话（工作区）',
        kind: 'action',
        title: '回到不隶属任何一本书的主对话：开一个空白会话，可建书/查资料/切书；旧会话仍在「历史对话」里',
      },
    ],
  },
  {
    title: '创作功能',
    items: [
      { id: 'agents', icon: 'sliders', label: '智能体设置', kind: 'window' },
      { id: 'learning', icon: 'pen', label: '学习仿写', kind: 'window' },
      { id: 'materials', icon: 'folder', label: '素材库', kind: 'window' },
      {
        id: 'revision',
        icon: 'editdoc',
        label: '正文改写',
        kind: 'window',
        title: '给你已有的正文 + 你的要求，改出整篇：只改你点名的地方，其余逐字保留',
      },
    ],
  },
  {
    title: '数据与产出',
    items: [
      { id: 'dashboard', icon: 'chart', label: '数据看板', kind: 'window' },
      { id: 'export', icon: 'boxout', label: '导出中心', kind: 'window' },
    ],
  },
  {
    title: '配置',
    items: [
      { id: 'bookshelf', icon: 'book', label: '书架 / 新建作品', kind: 'dialog' },
      { id: 'styleForge', icon: 'flask', label: '风格工坊', kind: 'dialog' },
      { id: 'modelConfig', icon: 'gear', label: '模型配置', kind: 'dialog' },
    ],
  },
]

const collapsed = computed(() => appStore.navCollapsed)
/** 主对话按钮的激活态：当前就在工作区作用域。 */
const onMainChat = computed(() => inWorkspace())

function toggleNav(): void {
  setNavCollapsed(!appStore.navCollapsed)
}

function open(item: NavItem): void {
  if (item.kind === 'dialog') {
    const key = item.id as 'bookshelf' | 'styleForge' | 'modelConfig'
    appStore.dialogs[key] = true
    return
  }
  if (item.kind === 'action') {
    // 回到主对话：切工作区 + 开全新空白会话（旧会话保留在历史里）
    resetToMainChat()
    appStore.sideWindow = null
    return
  }
  const wid = item.id as Exclude<SideWindowId, null>
  appStore.sideWindow = appStore.sideWindow === wid ? null : wid
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
})
</script>

<template>
  <nav class="feature-nav" :class="{ collapsed }" aria-label="主导航">
    <div class="brand">
      <button
        class="brand-mark"
        :title="collapsed ? '展开导航（176px）' : 'Inkforge'"
        @click="toggleNav"
      >
        In
      </button>
      <span v-if="!collapsed" class="brand-name">Inkforge</span>
      <button
        v-if="!collapsed"
        class="collapse-btn"
        title="折叠导航：省下 120px 给对话列"
        aria-label="折叠导航"
        @click="toggleNav"
      >
        ‹‹
      </button>
    </div>

    <div v-for="(group, gi) in groups" :key="group.title" class="nav-group">
      <div v-if="!collapsed" class="nav-group-title">{{ group.title }}</div>
      <div v-else-if="gi > 0" class="nav-sep" />
      <button
        v-for="item in group.items"
        :key="item.id"
        class="nav-btn"
        :class="{
          active: appStore.sideWindow === item.id || (item.id === 'mainChat' && onMainChat),
        }"
        :title="collapsed ? item.label : item.title"
        @click="open(item)"
      >
        <!-- eslint-disable-next-line vue/no-v-html -- 常量图标路径，无外部输入 -->
        <span class="nav-icon" v-html="iconMarkup(item.icon)" />
        <span v-if="!collapsed" class="nav-label">{{ item.label }}</span>
      </button>
    </div>

    <div class="nav-foot">
      <div class="engine-line" :title="`本地引擎：${appStore.engineState}${appStore.enginePort ? ` :${appStore.enginePort}` : ''}`">
        <span class="engine-dot" :class="`dot-${appStore.engineState}`" />
        <span v-if="!collapsed" class="engine-text">
          {{
            appStore.engineState === 'ready'
              ? '引擎就绪'
              : appStore.engineState === 'starting'
                ? '启动中…'
                : appStore.engineState === 'crashed'
                  ? '引擎异常'
                  : '引擎停止'
          }}
        </span>
      </div>
      <div class="author-line" title="作者">
        <div class="avatar">作</div>
        <span v-if="!collapsed">作者</span>
      </div>
    </div>
  </nav>
</template>

<style scoped>
.feature-nav {
  width: var(--rail-w, 176px);
  border-right: 1px solid var(--line-1);
  background: var(--bg-1);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  overflow-x: hidden;
  scrollbar-width: thin;
  /* 折叠/展开是布局属性，只有用户主动点时才动，且不参与 hover */
  transition: none;
}
.feature-nav.collapsed {
  overflow-x: hidden;
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 14px 14px 10px;
}
.collapsed .brand {
  justify-content: center;
  padding: 14px 0 10px;
}
.brand-mark {
  width: 27px;
  height: 27px;
  flex: 0 0 auto;
  border: none;
  border-radius: var(--r-1);
  background: linear-gradient(135deg, #1f2937, #374151);
  color: #fff;
  font-weight: 700;
  font-size: var(--fs-12);
  display: grid;
  place-items: center;
  cursor: pointer;
  font-family: inherit;
}
.brand-mark:hover {
  filter: brightness(1.15);
}
.brand-name {
  font-size: var(--fs-15);
  font-weight: 700;
  color: var(--ink-1);
  white-space: nowrap;
  overflow: hidden;
}
.collapse-btn {
  margin-left: auto;
  border: none;
  background: none;
  color: var(--ink-5);
  cursor: pointer;
  font-size: var(--fs-12);
  padding: 2px 4px;
  border-radius: var(--r-1);
  line-height: 1;
}
.collapse-btn:hover {
  background: var(--bg-2);
  color: var(--ink-2);
}

.nav-group {
  padding: 4px 8px 8px;
}
.collapsed .nav-group {
  padding: 0 0 2px;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.nav-group-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--ink-5);
  padding: 6px 8px 3px;
  letter-spacing: 0.5px;
}
.nav-sep {
  width: 22px;
  height: 1px;
  background: var(--line-2);
  margin: 6px 0;
}
.nav-btn {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 9px;
  background: none;
  border: none;
  border-radius: var(--r-2);
  padding: 7px 9px;
  cursor: pointer;
  color: var(--ink-2);
  font-size: var(--fs-13);
  text-align: left;
  font-family: inherit;
  transition:
    background var(--dur-1) var(--ease-out),
    color var(--dur-1) var(--ease-out);
}
.collapsed .nav-btn {
  width: 38px;
  height: 34px;
  justify-content: center;
  padding: 0;
  gap: 0;
}
.nav-btn:hover {
  background: var(--bg-2);
  color: var(--ink-1);
}
.nav-btn.active {
  background: var(--accent-bg);
  color: var(--accent);
  font-weight: 600;
}
.nav-icon {
  width: 18px;
  height: 18px;
  flex: 0 0 auto;
  display: grid;
  place-items: center;
}
.nav-icon :deep(svg) {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.4;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.nav-label {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nav-foot {
  margin-top: auto;
  border-top: 1px solid var(--line-2);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.collapsed .nav-foot {
  align-items: center;
  padding: 10px 0;
}
.engine-line {
  display: flex;
  align-items: center;
  gap: 7px;
}
.collapsed .engine-line {
  justify-content: center;
}
.engine-text {
  font-size: var(--fs-12);
  color: var(--ink-4);
  white-space: nowrap;
}
.engine-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-ready {
  background: var(--ok-dot);
}
.dot-starting {
  background: #3b82f6;
}
.dot-crashed {
  background: var(--bad-dot);
}
.dot-stopped {
  background: #d1d5db;
}
.author-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-13);
  color: var(--ink-2);
}
.avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #eef1f5;
  display: grid;
  place-items: center;
  font-size: var(--fs-11);
  color: var(--ink-3);
  flex: 0 0 auto;
}
</style>
