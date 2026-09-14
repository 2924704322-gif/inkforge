<script setup lang="ts">
import { NInput, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'

import { api } from '../api'
import { appStore, type SideWindowId } from '../store'
import type {
  AgentPreset,
  DashboardData,
  LearningItem,
  Material,
} from '../types'

/**
 * 左侧功能导航点击后弹出的小窗页面（一次一个）。
 * 窗体为悬浮面板，覆盖在对话栏之上，带标题与关闭按钮。
 */

const show = computed(() => appStore.sideWindow !== null)

const message = useMessage()

const which = computed<SideWindowId>(() => appStore.sideWindow)

// ── 智能体设置 ──
const presets = ref<AgentPreset[]>([])
const editingPreset = ref<AgentPreset | null>(null)
const savingPreset = ref(false)

async function loadPresets(): Promise<void> {
  const res = await api<{ presets: AgentPreset[] }>('GET', '/api/agent-presets')
  presets.value = res.presets
}

function editPreset(p: AgentPreset): void {
  editingPreset.value = { ...p }
}

async function savePreset(): Promise<void> {
  if (!editingPreset.value) return
  savingPreset.value = true
  try {
    await api('PUT', `/api/agent-presets/${editingPreset.value.key}`, {
      prompt: editingPreset.value.prompt,
    })
    message.success(`「${editingPreset.value.label}」提示词已保存，下一轮对话生效`)
    editingPreset.value = null
    await loadPresets()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    savingPreset.value = false
  }
}

async function resetPreset(p: AgentPreset): Promise<void> {
  await api('DELETE', `/api/agent-presets/${p.key}`)
  message.success('已恢复默认提示词')
  await loadPresets()
  editingPreset.value = null
}

// ── 学习仿写 ──
const learningItems = ref<LearningItem[]>([])
const learnTitle = ref('')
const learnSample = ref('')
const learningBusy = ref(false)
const viewLearning = ref<LearningItem | null>(null)

async function loadLearning(): Promise<void> {
  const res = await api<{ items: LearningItem[] }>('GET', '/api/learning')
  learningItems.value = res.items
}

async function runLearning(): Promise<void> {
  if (!learnTitle.value.trim() || !learnSample.value.trim()) {
    message.warning('标题与样本正文不能为空（样本至少 200 字）')
    return
  }
  learningBusy.value = true
  try {
    const res = await api<{ id: string }>('POST', '/api/learning', {
      title: learnTitle.value.trim(),
      sample: learnSample.value,
    })
    message.success('三阶段学习完成')
    learnTitle.value = ''
    learnSample.value = ''
    await loadLearning()
    await viewDoc(res.id)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    learningBusy.value = false
  }
}

async function viewDoc(id: string): Promise<void> {
  const res = await api<LearningItem>(`GET`, `/api/learning/${id}`)
  viewLearning.value = res
}

async function removeLearning(item: LearningItem): Promise<void> {
  await api('DELETE', `/api/learning/${item.id}`)
  if (viewLearning.value?.id === item.id) viewLearning.value = null
  await loadLearning()
}

// ── 素材库 ──
const materials = ref<Material[]>([])
const newMaterial = ref({ title: '', content: '' })
const expandedMaterial = ref('')

async function loadMaterials(): Promise<void> {
  const res = await api<{ materials: Material[] }>('GET', '/api/materials')
  materials.value = res.materials
}

async function createMaterial(): Promise<void> {
  if (!newMaterial.value.title.trim() || !newMaterial.value.content.trim()) {
    message.warning('名称与内容不能为空')
    return
  }
  await api('POST', '/api/materials', {
    title: newMaterial.value.title.trim(),
    content: newMaterial.value.content.trim(),
  })
  message.success('素材已保存')
  newMaterial.value = { title: '', content: '' }
  await loadMaterials()
}

async function removeMaterial(m: Material): Promise<void> {
  await api('DELETE', `/api/materials/${m.id}`)
  await loadMaterials()
}

// ── 数据看板 ──
const dashboard = ref<DashboardData | null>(null)
const dashBook = computed(() => appStore.bookId)

async function loadDashboard(): Promise<void> {
  if (!dashBook.value) return
  dashboard.value = await api<DashboardData>(
    'GET',
    `/api/dashboard?novel=${encodeURIComponent(dashBook.value)}`,
  )
}

// ── 导出中心 ──
const exporting = ref('')
const includeDraft = ref(true)
async function doExport(format: 'txt' | 'epub'): Promise<void> {
  if (!appStore.bookId) {
    message.warning('请先在书架选择作品')
    return
  }
  exporting.value = format
  try {
    const res = await window.inkforge.exportManuscript(
      appStore.bookId,
      format,
      includeDraft.value,
    )
    if (res.ok) message.success(`已导出：${res.message}`)
    else if (res.message !== '已取消') message.error(res.message)
  } finally {
    exporting.value = ''
  }
}

async function loadAll(): Promise<void> {
  try {
    const jobs: Promise<void>[] = [loadPresets(), loadLearning(), loadMaterials()]
    if (appStore.bookId) jobs.push(loadDashboard())
    await Promise.all(jobs)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

watch(
  () => appStore.sideWindow,
  (w) => {
    if (w) void loadAll()
  },
)
watch(
  () => appStore.bookId,
  () => {
    if (appStore.sideWindow === 'dashboard') void loadDashboard()
  },
)
</script>

<template>
  <transition name="slide">
    <aside v-if="show && which" class="side-window">
      <div class="win-head">
        <span class="win-title">
          {{
            ({
              agents: '智能体设置',
              learning: '学习仿写',
              materials: '素材库',
              dashboard: '数据看板',
              export: '导出中心',
            } as Record<string, string>)[which]
          }}
        </span>
        <button class="close-btn" @click="appStore.sideWindow = null">✕</button>
      </div>

      <div class="win-body scroll-y">
        <!-- 智能体设置 -->
        <template v-if="which === 'agents'">
          <div class="muted" style="margin-bottom: 10px">
            五个阶段智能体的系统提示词；修改后下一轮对话立即生效。绑定素材与约束见「风格工坊」。
          </div>
          <div v-for="p in presets" :key="p.key" class="agent-block">
            <div class="agent-line">
              <b>{{ p.label }}</b>
              <span v-if="p.custom" class="tag">已自定义</span>
              <span class="row-gap">
                <button class="ghost-btn" @click="editPreset(p)">编辑</button>
                <button v-if="p.custom" class="ghost-btn" @click="resetPreset(p)">恢复默认</button>
              </span>
            </div>
            <pre v-if="editingPreset?.key !== p.key" class="prompt-view pre-wrap">{{ p.prompt }}</pre>
            <template v-else>
              <NInput
                v-model:value="editingPreset.prompt"
                type="textarea"
                :rows="6"
                class="prompt-edit"
              />
              <div class="row-gap" style="justify-content: flex-end; margin-top: 6px">
                <button class="ghost-btn" @click="editingPreset = null">取消</button>
                <button class="primary-btn" :disabled="savingPreset" @click="savePreset">保存</button>
              </div>
            </template>
          </div>
        </template>

        <!-- 学习仿写 -->
        <template v-else-if="which === 'learning'">
          <div class="muted" style="margin-bottom: 10px">
            粘贴一段喜欢的正文（建议 500-3000 字），自动完成素材拆解 / 剧情学习 / 文风学习三阶段分析。
          </div>
          <NInput v-model:value="learnTitle" size="small" placeholder="学习任务标题，例：天蚕土豆·战斗章" style="margin-bottom: 8px" />
          <NInput
            v-model:value="learnSample"
            type="textarea"
            :rows="7"
            placeholder="粘贴样本文本……"
            style="margin-bottom: 8px"
          />
          <button class="primary-btn" :disabled="learningBusy" @click="runLearning">
            {{ learningBusy ? '三阶段分析中…（约 1 分钟）' : '开始学习' }}
          </button>

          <div class="section-title">历史成果</div>
          <div v-for="item in learningItems" :key="item.id" class="row-card">
            <span class="row-title" @click="viewDoc(item.id)">{{ item.title }}</span>
            <span class="row-gap">
              <button class="ghost-btn" @click="viewDoc(item.id)">查看</button>
              <button class="del-btn" @click="removeLearning(item)">删除</button>
            </span>
          </div>
          <div v-if="viewLearning" class="view-box scroll-y">
            <div class="row-between">
              <b>{{ viewLearning.title }}</b>
              <button class="ghost-btn" @click="viewLearning = null">收起</button>
            </div>
            <pre class="pre-wrap view-pre">{{ viewLearning.content }}</pre>
          </div>
        </template>

        <!-- 素材库 -->
        <template v-else-if="which === 'materials'">
          <div class="muted" style="margin-bottom: 10px">
            世界观资料、桥段、设定素材……在「绑定到当前作品」页勾选后注入 Writer/Editor。
          </div>
          <div v-for="m in materials" :key="m.id" class="row-card">
            <div class="col-gap">
              <div class="row-between">
                <span class="row-title">{{ m.title }}</span>
                <span class="row-gap">
                  <button class="ghost-btn" @click="expandedMaterial = expandedMaterial === m.id ? '' : m.id">
                    {{ expandedMaterial === m.id ? '收起' : '查看' }}
                  </button>
                  <button class="del-btn" @click="removeMaterial(m)">删除</button>
                </span>
              </div>
              <pre v-if="expandedMaterial === m.id" class="pre-wrap view-pre">{{ m.content }}</pre>
            </div>
          </div>
          <div class="new-block">
            <NInput v-model:value="newMaterial.title" size="small" placeholder="素材名称，例：修真宗门体系" style="margin-bottom: 6px" />
            <NInput v-model:value="newMaterial.content" type="textarea" :rows="4" size="small" placeholder="素材内容" style="margin-bottom: 8px" />
            <button class="primary-btn" @click="createMaterial">添加素材</button>
          </div>
        </template>

        <!-- 数据看板 -->
        <template v-else-if="which === 'dashboard'">
          <div v-if="!dashBook" class="muted">请先在书架选择一部作品。</div>
          <template v-else-if="dashboard">
            <div class="metric-grid">
              <div class="metric-card">
                <div class="metric-num">{{ dashboard.metrics.approved }}/{{ dashboard.metrics.total_chapters }}</div>
                <div class="muted">定稿 / 总章</div>
              </div>
              <div class="metric-card">
                <div class="metric-num">{{ dashboard.metrics.first_pass_rate ?? '—' }}<i v-if="dashboard.metrics.first_pass_rate !== null">%</i></div>
                <div class="muted">一次通过率</div>
              </div>
              <div class="metric-card">
                <div class="metric-num">{{ dashboard.metrics.avg_score ?? '—' }}</div>
                <div class="muted">Editor 均分</div>
              </div>
              <div class="metric-card">
                <div class="metric-num">{{ dashboard.foreshadow.resolved }}/{{ dashboard.foreshadow.total }}</div>
                <div class="muted">伏笔回收</div>
              </div>
            </div>

            <div class="section-title">章节与评分</div>
            <div v-for="ch in dashboard.chapters" :key="String(ch.chapter)" class="row-card">
              <span>第 {{ ch.chapter }} 章</span>
              <span class="muted">{{ ch.status === 'approved' ? `已定稿 · ${ch.score ?? '—'}` : '草稿' }}</span>
            </div>
            <div v-if="dashboard.chapters.length === 0" class="muted" style="padding: 4px 0">暂无章节</div>

            <div class="section-title">伏笔台账</div>
            <div v-for="f in dashboard.foreshadow.items" :key="f.id" class="row-card">
              <div class="col-gap">
                <span class="row-title">{{ f.desc }}</span>
                <span class="muted">埋于 {{ f.planted_ch ?? '?' }} 章 → 回收于 {{ f.resolve_ch ?? '?' }} 章</span>
              </div>
              <span class="tag" :class="f.status === 'resolved' ? 'ok' : ''">
                {{ f.status === 'resolved' ? '已回收' : '未回收' }}
              </span>
            </div>
          </template>
        </template>

        <!-- 导出中心 -->
        <template v-else-if="which === 'export'">
          <div class="muted" style="margin-bottom: 10px">
            导出《{{ appStore.bookTitle || '（未选择作品）' }}》。TXT 含全部定稿章节；EPUB 按卷组装。
          </div>
          <div class="export-row">
            <button class="export-card" :disabled="exporting !== ''" @click="doExport('txt')">
              <span class="export-icon">📄</span>
              <span>导出 TXT</span>
              <small class="muted">纯文本，含分卷分隔</small>
            </button>
            <button class="export-card" :disabled="exporting !== ''" @click="doExport('epub')">
              <span class="export-icon">📚</span>
              <span>导出 EPUB</span>
              <small class="muted">电子书格式</small>
            </button>
          </div>
          <label class="check-line">
            <input v-model="includeDraft" type="checkbox" />
            包含草稿章（对话创作流中尚未走流水线定稿的章节）
          </label>
          <div class="muted" style="margin-top: 8px">
            默认只导出定稿章节；勾选后草稿章一并写入。
          </div>
        </template>
      </div>
    </aside>
  </transition>
</template>

<script lang="ts">
export default { name: 'SideWindows' }
</script>

<style scoped>
.side-window {
  position: fixed;
  left: 184px;
  top: 12px;
  bottom: 12px;
  width: 520px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 14px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.14);
  z-index: 60;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.slide-enter-active,
.slide-leave-active {
  transition: all 0.18s ease;
}
.slide-enter-from,
.slide-leave-to {
  opacity: 0;
  transform: translateX(-16px);
}
.win-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 18px;
  border-bottom: 1px solid #eef0f2;
}
.win-title {
  font-weight: 700;
  font-size: 15px;
}
.close-btn {
  background: none;
  border: none;
  font-size: 14px;
  color: #8a8f99;
  cursor: pointer;
  padding: 2px 8px;
  border-radius: 6px;
}
.close-btn:hover {
  background: #f0f1f3;
  color: #26272b;
}
.win-body {
  flex: 1;
  min-height: 0;
  padding: 14px 18px;
}
.section-title {
  font-weight: 600;
  font-size: 13.5px;
  margin: 14px 0 8px;
}
.agent-block {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.agent-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.prompt-view {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: #5c6470;
  background: #f9fafb;
  border-radius: 8px;
  padding: 8px 10px;
  max-height: 110px;
  overflow-y: auto;
}
.row-card {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 9px 12px;
  margin-bottom: 7px;
}
.row-title {
  font-weight: 600;
  font-size: 13px;
  cursor: pointer;
}
.row-between {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  width: 100%;
}
.col-gap {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
  flex: 1;
}
.row-gap {
  display: flex;
  gap: 6px;
  align-items: center;
}
.tag {
  font-size: 11px;
  background: #f3f4f6;
  border-radius: 999px;
  padding: 1px 8px;
  color: #5c6470;
}
.tag.ok {
  background: #e6f6ec;
  color: #116932;
}
.ghost-btn {
  background: none;
  border: 1px solid #e5e7eb;
  border-radius: 7px;
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
  color: #3a3d44;
}
.del-btn {
  background: none;
  border: none;
  color: #dc2626;
  font-size: 12px;
  cursor: pointer;
}
.primary-btn {
  background: #1f2937;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 6px 16px;
  font-size: 12.5px;
  cursor: pointer;
}
.primary-btn:disabled {
  background: #d1d5db;
}
.view-box {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 10px 12px;
  margin-top: 10px;
  max-height: 320px;
}
.view-pre {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.7;
}
.new-block {
  border: 1px dashed #d9dce1;
  border-radius: 10px;
  padding: 10px;
  margin-top: 10px;
  display: flex;
  flex-direction: column;
}
.metric-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 6px;
}
.metric-card {
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 12px;
  text-align: center;
}
.metric-num {
  font-size: 20px;
  font-weight: 700;
  color: #26272b;
}
.metric-num i {
  font-style: normal;
  font-size: 13px;
}
.export-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.export-card {
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 16px;
  background: #fff;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: center;
  font-size: 13.5px;
  color: #26272b;
}
.export-card:hover:not(:disabled) {
  border-color: #1d4ed8;
}
.export-icon {
  font-size: 22px;
}
.check-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #3a3d44;
  margin-top: 10px;
}
</style>
