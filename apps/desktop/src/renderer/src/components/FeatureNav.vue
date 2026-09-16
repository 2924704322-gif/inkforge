<script setup lang="ts">
import { onMounted } from 'vue'

import { appStore, type SideWindowId } from '../store'

type NavId = Exclude<SideWindowId, null> | 'bookshelf' | 'styleForge' | 'modelConfig'

interface NavItem {
  id: NavId
  icon: string
  label: string
  kind: 'window' | 'dialog'
}

const groups: { title: string; items: NavItem[] }[] = [
  {
    title: '创作功能',
    items: [
      { id: 'agents', icon: '🤖', label: '智能体设置', kind: 'window' },
      { id: 'learning', icon: '📝', label: '学习仿写', kind: 'window' },
      { id: 'materials', icon: '🗂', label: '素材库', kind: 'window' },
    ],
  },
  {
    title: '数据与产出',
    items: [
      { id: 'dashboard', icon: '📊', label: '数据看板', kind: 'window' },
      { id: 'export', icon: '📦', label: '导出中心', kind: 'window' },
    ],
  },
  {
    title: '配置',
    items: [
      { id: 'bookshelf', icon: '📚', label: '书架 / 新建作品', kind: 'dialog' },
      { id: 'styleForge', icon: '🧪', label: '风格工坊', kind: 'dialog' },
      { id: 'modelConfig', icon: '⚙️', label: '模型配置', kind: 'dialog' },
    ],
  },
]

function open(item: NavItem): void {
  if (item.kind === 'dialog') {
    const key = item.id as 'bookshelf' | 'styleForge' | 'modelConfig'
    appStore.dialogs[key] = true
  } else {
    const wid = item.id as Exclude<SideWindowId, null>
    appStore.sideWindow = appStore.sideWindow === wid ? null : wid
  }
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
  <nav class="feature-nav">
    <div class="brand">
      <div class="brand-mark">In</div>
      <div class="brand-name">Inkforge</div>
    </div>

    <div v-for="group in groups" :key="group.title" class="nav-group">
      <div class="nav-group-title">{{ group.title }}</div>
      <button
        v-for="item in group.items"
        :key="item.id"
        class="nav-btn"
        :class="{ active: appStore.sideWindow === item.id }"
        @click="open(item)"
      >
        <span class="nav-icon">{{ item.icon }}</span>
        <span class="nav-label">{{ item.label }}</span>
      </button>
    </div>

    <div class="nav-foot">
      <div class="engine-line">
        <span class="engine-dot" :class="`dot-${appStore.engineState}`" />
        <span class="muted">
          引擎{{ appStore.engineState === 'ready' ? `就绪 :${appStore.enginePort}` : appStore.engineState === 'starting' ? '启动中…' : appStore.engineState === 'crashed' ? '异常' : '停止' }}
        </span>
      </div>
      <div class="author-line">
        <div class="avatar">作</div>
        <span>作者</span>
      </div>
    </div>
  </nav>
</template>

<style scoped>
.feature-nav {
  width: 176px;
  flex-shrink: 0;
  border-right: 1px solid #e9ebee;
  background: #fbfbfc;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  scrollbar-width: thin;
}
.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 14px 14px 10px;
}
.brand-mark {
  width: 27px;
  height: 27px;
  border-radius: 7px;
  background: linear-gradient(135deg, #1f2937, #374151);
  color: #fff;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
}
.brand-name {
  font-size: 15px;
  font-weight: 700;
  color: #26272b;
}
.nav-group {
  padding: 4px 8px 8px;
}
.nav-group-title {
  font-size: 10.5px;
  font-weight: 600;
  color: #9aa0aa;
  padding: 6px 8px 3px;
  letter-spacing: 0.5px;
}
.nav-btn {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 9px;
  background: none;
  border: none;
  border-radius: 8px;
  padding: 7px 9px;
  cursor: pointer;
  color: #3a3d44;
  font-size: 13px;
  text-align: left;
}
.nav-btn:hover {
  background: #f0f1f3;
}
.nav-btn.active {
  background: #e8f0fe;
  color: #1d4ed8;
  font-weight: 600;
}
.nav-icon {
  font-size: 14px;
  width: 18px;
  text-align: center;
}
.nav-foot {
  margin-top: auto;
  border-top: 1px solid #eef0f2;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.engine-line {
  display: flex;
  align-items: center;
  gap: 7px;
}
.engine-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-ready {
  background: #16a34a;
}
.dot-starting {
  background: #3b82f6;
}
.dot-crashed {
  background: #dc2626;
}
.dot-stopped {
  background: #d1d5db;
}
.author-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #3a3d44;
}
.avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: #eef1f5;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: #5c6470;
}
</style>
