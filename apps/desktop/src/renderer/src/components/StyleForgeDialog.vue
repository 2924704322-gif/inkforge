<script setup lang="ts">
import { NInput, NModal, NSelect, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'

import { api } from '../api'
import { appStore } from '../store'
import type {
  Bindings,
  CustomSkill,
  ForgeAgentInfo,
  ForgeResult,
  SkillIndexEntry,
} from '../types'
import { DIMENSION_LABELS } from '../types'

const show = defineModel<boolean>('show', { default: false })
const message = useMessage()

const tab = ref<'packs' | 'bind' | 'forge'>('packs')
const skills = ref<SkillIndexEntry[]>([])
const customSkills = ref<CustomSkill[]>([])
const bindings = ref<Bindings>({ bound_custom: [], bound_packs: [] })
const savingBind = ref(false)

// 报告
const reportSkillId = ref('')
const report = ref<Record<string, unknown> | null>(null)
const openDims = ref<string[]>([])

// 新建 / 编辑自定义约束
const newSkill = ref({ title: '', content: '' })
const editingId = ref('')
// 刚创建/编辑过、但尚未绑定到当前作品的约束：给一条显眼的"一键绑定"提示。
// 由来（真实事故）：约束建完只落在全局库，没绑定就完全不影响生成，而界面上毫无线索。
const pendingBind = ref<{ id: string; title: string } | null>(null)

// ────────── 约束提炼（风格工坊第三个模块 · 专职智能体）──────────
// 形态（2026-09-19 用户订正）：**两个内容框** —— ① 正文内容（你粘贴要提炼的文本）
// ② 提炼需求（你要我从这段正文里提炼什么）。它只在①上按要求做忠实提炼。
// 边界：不自己去翻作品库（你作品库里的文件它读不到），看什么完全由你粘贴决定。
const forgeAgent = ref<ForgeAgentInfo | null>(null)
const forgeShowPrompt = ref(false)
const forgeSource = ref('')
const forgeRequirement = ref('')
const forgeMaxItems = ref(18)
const forgeRunning = ref(false)
const forgeResult = ref<ForgeResult | null>(null)
const forgeTitle = ref('')
const forgeConstraints = ref('')
const forgeSaving = ref(false)

const countOptions = [6, 10, 18, 30, 60].map((n) => ({ label: `${n} 条以内`, value: n }))

const forgeSourceLimit = computed(() => forgeAgent.value?.limits?.source_max ?? 20000)
const forgeRequirementLimit = computed(() => forgeAgent.value?.limits?.requirement_max ?? 2000)
const forgeSourceTooLong = computed(() => forgeSource.value.length > forgeSourceLimit.value)

/** 结果区当前条数（按 `- ` 行实时统计，用户手删条目后立刻反映） */
const forgeCount = computed(
  () => forgeConstraints.value.split('\n').filter((l) => l.trim().startsWith('- ')).length,
)

async function loadAll(): Promise<void> {
  try {
    const [list, customs] = await Promise.all([
      api<{ skills: SkillIndexEntry[] }>('GET', '/api/skills/list'),
      api<{ skills: CustomSkill[] }>('GET', '/api/custom-skills'),
    ])
    skills.value = list.skills
    customSkills.value = customs.skills
    if (appStore.bookId) {
      bindings.value = await api<Bindings>('GET', `/api/bindings?novel=${encodeURIComponent(appStore.bookId)}`)
    }
  } catch {
    /* 引擎未就绪时静默 */
  }
}

/** 拉取提炼智能体名片；失败不影响其它两个模块（各自独立降级）。 */
async function loadForgeAgent(): Promise<void> {
  try {
    const info = await api<ForgeAgentInfo>('GET', '/api/style-forge/agent')
    forgeAgent.value = info
    if (!forgeMaxItems.value) forgeMaxItems.value = info.max_items.default
  } catch {
    forgeAgent.value = null
  }
}

/**
 * 复制文本到剪贴板。
 * 双层实现：优先 navigator.clipboard（Electron file:// 下多数情况可用），
 * 失败再退到隐藏 textarea + execCommand —— 只靠前者时，一旦剪贴板权限被拒，
 * 用户点"复制"会毫无反应（静默失败），比报错更糟。
 */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* 落到下面的兜底 */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.left = '-9999px'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

async function copyConstraints(): Promise<void> {
  const text = forgeConstraints.value.trim()
  if (!text) {
    message.warning('还没有可复制的内容，先提炼一次')
    return
  }
  const ok = await copyText(text)
  if (ok) message.success(`已复制 ${forgeCount.value} 条约束到剪贴板`)
  else message.error('复制失败：请在结果框里全选后按 Ctrl+C')
}

async function copyAgentPrompt(): Promise<void> {
  const text = forgeAgent.value?.prompt ?? ''
  if (!text) return
  const ok = await copyText(text)
  if (ok) message.success('智能体提示词已复制')
  else message.error('复制失败：请手动选中后按 Ctrl+C')
}

/** 跑一次提炼（一次模型调用；不落盘、不读库）。 */
async function runForge(): Promise<void> {
  const source = forgeSource.value.trim()
  const requirement = forgeRequirement.value.trim()
  if (!source) {
    message.warning('请先把要提炼的正文粘贴进【正文内容】框')
    return
  }
  if (!requirement) {
    message.warning('请写下【提炼需求】：你要我从这段正文里提炼什么')
    return
  }
  if (forgeSourceTooLong.value) {
    message.warning(`正文 ${source.length} 字，超过上限 ${forgeSourceLimit.value} 字，请分段提炼`)
    return
  }
  forgeRunning.value = true
  forgeResult.value = null
  try {
    const res = await api<ForgeResult>('POST', '/api/style-forge/constraints', {
      source_text: source,
      requirement,
      max_items: forgeMaxItems.value,
    })
    forgeResult.value = res
    forgeTitle.value = res.title
    forgeConstraints.value = res.constraints
    message.success(`已提炼 ${res.count} 条（${res.model} · ${res.elapsed}s）`)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    forgeRunning.value = false
  }
}

/** 把结果存进自定义约束库（复用既有 /api/custom-skills，不新造落盘语义）。 */
async function saveForged(): Promise<void> {
  const title = forgeTitle.value.trim()
  const content = forgeConstraints.value.trim()
  if (!title || !content) {
    message.warning('约束名称与内容不能为空')
    return
  }
  forgeSaving.value = true
  try {
    const created = await api<{ skill_id?: string }>('POST', '/api/custom-skills', { title, content })
    message.success('已存入约束库（下一步：绑定到作品，否则不影响生成）')
    pendingBind.value = created?.skill_id ? { id: created.skill_id, title } : null
    void loadAll()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    forgeSaving.value = false
  }
}

async function openReport(id: string): Promise<void> {
  try {
    const res = await api<{ full_report: Record<string, unknown> }>(
      'GET',
      `/api/distill/report/${encodeURIComponent(id)}`,
    )
    report.value = res.full_report
    reportSkillId.value = id
    openDims.value = []
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

async function exportZip(entry: SkillIndexEntry): Promise<void> {
  const res = await window.inkforge.exportSkillZip(entry.skill_id)
  if (res.ok) message.success(`已导出：${res.message}`)
  else if (res.message !== '已取消') message.error(res.message)
}

async function removeSkill(entry: SkillIndexEntry): Promise<void> {
  try {
    await api('DELETE', `/api/skills/${encodeURIComponent(entry.skill_id)}`)
    message.success('技能包已删除')
    void loadAll()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

async function createCustom(): Promise<void> {
  if (!newSkill.value.title.trim() || !newSkill.value.content.trim()) {
    message.warning('名称与内容不能为空')
    return
  }
  const editing = editingId.value
  try {
    if (editing) {
      const res = await api<{ summary?: string }>(
        'PUT',
        `/api/custom-skills/${encodeURIComponent(editing)}`,
        { title: newSkill.value.title.trim(), content: newSkill.value.content.trim() },
      )
      message.success(res.summary || '约束已更新')
    } else {
      const created = await api<{ skill_id?: string }>('POST', '/api/custom-skills', {
        title: newSkill.value.title.trim(),
        content: newSkill.value.content.trim(),
      })
      message.success('约束已创建（下一步：绑定到作品，否则不影响生成）')
      if (created?.skill_id) {
        pendingBind.value = { id: created.skill_id, title: newSkill.value.title.trim() }
      }
    }
    newSkill.value = { title: '', content: '' }
    editingId.value = ''
    void loadAll()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

function startEdit(skill: CustomSkill): void {
  editingId.value = skill.skill_id
  newSkill.value = { title: skill.title, content: skill.content ?? '' }
}

function cancelEdit(): void {
  editingId.value = ''
  newSkill.value = { title: '', content: '' }
}

async function removeCustom(skill: CustomSkill): Promise<void> {
  try {
    await api('DELETE', `/api/custom-skills/${encodeURIComponent(skill.skill_id)}`)
    message.success('已删除')
    void loadAll()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

function toggleBind(kind: 'custom' | 'pack', id: string): void {
  const key = kind === 'custom' ? 'bound_custom' : 'bound_packs'
  const list = bindings.value[key]
  const idx = list.indexOf(id)
  if (idx >= 0) list.splice(idx, 1)
  else list.push(id)
}

async function saveBindings(): Promise<void> {
  if (!appStore.bookId) {
    message.warning('请先在书架选择一部作品')
    return
  }
  savingBind.value = true
  try {
    const res = await api<{ summary?: string }>(
      'POST',
      `/api/bindings?novel=${encodeURIComponent(appStore.bookId)}`,
      {
        custom_skill_ids: bindings.value.bound_custom,
        pack_ids: bindings.value.bound_packs,
      },
    )
    message.success(res.summary || `已绑定到《${appStore.bookTitle}》`)
    pendingBind.value = null
    appStore.treeVersion += 1
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    savingBind.value = false
  }
}

/**
 * 一键把刚创建/编辑的约束追加绑定到当前作品（merge 语义，不动已有技能包绑定）。
 * 没有打开作品时给出明确指引，避免用户以为"保存了就生效"。
 */
async function quickBind(): Promise<void> {
  const target = pendingBind.value
  if (!target) return
  if (!appStore.bookId) {
    message.warning('还没打开作品：请先在书架打开要应用这条约束的作品')
    return
  }
  savingBind.value = true
  try {
    const merged = Array.from(new Set([...bindings.value.bound_custom, target.id]))
    const res = await api<{ summary?: string }>(
      'POST',
      `/api/bindings?novel=${encodeURIComponent(appStore.bookId)}`,
      { custom_skill_ids: merged, pack_ids: bindings.value.bound_packs, merge: true },
    )
    bindings.value.bound_custom = merged
    message.success(res.summary || `《${target.title}》已绑定到《${appStore.bookTitle}》`)
    pendingBind.value = null
    appStore.treeVersion += 1
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    savingBind.value = false
  }
}

watch(show, (opened) => {
  if (opened) {
    void loadAll()
    void loadForgeAgent()
  }
})
</script>

<template>
  <NModal v-model:show="show" preset="card" title="风格工坊" class="dw-dialog" style="width: 860px">
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'packs' }" @click="tab = 'packs'">技能包与约束</button>
      <button class="tab" :class="{ active: tab === 'bind' }" @click="tab = 'bind'">绑定到当前作品</button>
      <button class="tab" :class="{ active: tab === 'forge' }" @click="tab = 'forge'">
        约束提炼<span class="tab-badge">智能体</span>
      </button>
    </div>

    <!-- 技能包与约束 -->
    <div v-if="tab === 'packs'" class="scroll-y body">
      <div class="section-title">蒸馏技能包（16 维写作知识库）</div>
      <div v-if="skills.length === 0" class="muted" style="padding: 4px 0 10px">
        暂无技能包。蒸馏一本完整书籍后，可在此查看报告并绑定给任何作品。
      </div>
      <div v-for="entry in skills" :key="entry.skill_id" class="row-card">
        <div class="row-main">
          <span class="row-title">{{ entry.book_title }}</span>
          <span class="tag">v{{ entry.version }}</span>
          <span class="tag ok">{{ entry.status }}</span>
        </div>
        <div class="row-actions">
          <button class="ghost-btn" @click="openReport(entry.skill_id)">查看报告</button>
          <button class="ghost-btn" @click="exportZip(entry)">导出 ZIP</button>
          <button class="del-btn" @click="removeSkill(entry)">删除</button>
        </div>
      </div>

      <div class="section-title" style="margin-top: 18px">自定义创作约束</div>
      <div v-for="skill in customSkills" :key="skill.skill_id" class="row-card">
        <div class="row-main">
          <span class="row-title">{{ skill.title }}</span>
          <span class="muted">{{ skill.skill_id }}</span>
        </div>
        <div class="row-actions">
          <button class="ghost-btn" @click="startEdit(skill)">编辑</button>
          <button class="del-btn" @click="removeCustom(skill)">删除</button>
        </div>
      </div>
      <div class="new-skill">
        <div class="new-skill-head">
          {{ editingId ? `编辑约束（${editingId}）` : '新建约束' }}
          <button v-if="editingId" class="del-btn" @click="cancelEdit">取消编辑</button>
        </div>
        <NInput v-model:value="newSkill.title" size="small" placeholder="约束名称，例：仙侠规范" />
        <NInput
          v-model:value="newSkill.content"
          type="textarea"
          :rows="4"
          size="small"
          placeholder="约束内容（世界观体系 / 内容尺度 / 禁忌清单……）。用「- 」分行写条目，系统会把它渲染成 Writer/Editor 的执行清单"
        />
        <button class="primary-btn" @click="createCustom">
          {{ editingId ? '保存修改（同步到已绑作品）' : '添加约束' }}
        </button>
      </div>
    </div>

    <!-- 绑定 -->
    <div v-else-if="tab === 'bind'" class="scroll-y body">
      <div v-if="pendingBind" class="bind-hint">
        约束「{{ pendingBind.title }}」已保存到约束库，但<b>还没绑定到任何作品</b>——不绑定则完全不影响生成。
        <div class="bind-hint-actions">
          <button class="primary-btn" :disabled="savingBind || !appStore.bookId" @click="quickBind">
            {{ appStore.bookId ? `一键绑定到《${appStore.bookTitle}》` : '请先打开一部作品' }}
          </button>
          <button class="ghost-btn" @click="pendingBind = null">稍后再说</button>
        </div>
      </div>
      <div v-if="!appStore.bookId" class="muted" style="padding: 6px 0 12px">
        尚未选择作品——请先在书架打开一部作品再回来绑定。
      </div>
      <div v-else class="muted" style="padding: 6px 0 12px">
        绑定后写入《{{ appStore.bookTitle }}》的 settings/custom-skills.md，Writer / Editor
        将按「最高优先级」遵循其中的风格与技法要求。
      </div>

      <div class="section-title">自定义约束</div>
      <div
        v-for="skill in customSkills"
        :key="skill.skill_id"
        class="bind-row"
        :class="{ on: bindings.bound_custom.includes(skill.skill_id) }"
        @click="toggleBind('custom', skill.skill_id)"
      >
        <span class="bind-check">{{ bindings.bound_custom.includes(skill.skill_id) ? '✓' : '' }}</span>
        {{ skill.title }}
      </div>
      <div v-if="customSkills.length === 0" class="muted" style="padding: 2px 0 8px">暂无</div>

      <div class="section-title" style="margin-top: 14px">蒸馏技能包</div>
      <div
        v-for="entry in skills"
        :key="entry.skill_id"
        class="bind-row"
        :class="{ on: bindings.bound_packs.includes(entry.skill_id) }"
        @click="toggleBind('pack', entry.skill_id)"
      >
        <span class="bind-check">{{ bindings.bound_packs.includes(entry.skill_id) ? '✓' : '' }}</span>
        {{ entry.book_title }} v{{ entry.version }}
      </div>
      <div v-if="skills.length === 0" class="muted" style="padding: 2px 0 8px">暂无</div>

      <div class="footer">
        <button class="primary-btn" :disabled="savingBind || !appStore.bookId" @click="saveBindings">
          {{ savingBind ? '绑定中…' : '保存绑定' }}
        </button>
      </div>
    </div>

    <!-- 约束提炼（第三个模块 · 专职智能体：你粘贴正文 + 写需求，它按需求在正文上提炼） -->
    <div v-else class="scroll-y body">
      <div class="forge-banner">
        <div class="forge-banner-line">
          <b>{{ forgeAgent?.label || '约束提炼智能体' }}</b>
          <span class="muted">在<b>你粘贴的正文</b>上，按<b>你的提炼需求</b>提炼出对应内容</span>
        </div>
        <div class="forge-scope">
          <span class="scope-yes">✅ 它只看得到：{{ (forgeAgent?.sees || ['你在【正文内容】框里粘贴的文本', '你在【提炼需求】框里写的要求']).join('、') }}</span>
          <span class="scope-no">🚫 它看不到：{{ (forgeAgent?.never_sees || ['你作品库里的任何文件', '你没粘贴进输入框的任何内容']).join('、') }}</span>
        </div>
      </div>

      <div class="forge-field">
        <div class="forge-field-head">
          <span>① 正文内容<span class="muted">（把要提炼的文本粘贴进来）</span></span>
          <span class="forge-counter" :class="{ over: forgeSourceTooLong }">
            {{ forgeSource.length }} / {{ forgeSourceLimit }}
          </span>
        </div>
        <NInput
          v-model:value="forgeSource"
          type="textarea"
          :rows="12"
          size="small"
          placeholder="粘贴要提炼的正文，例：一个章节片段 / 一段设定稿 / 一段笔记……（一次一段，效果更好）"
        />
      </div>

      <div class="forge-field">
        <div class="forge-field-head">
          <span>② 提炼需求<span class="muted">（你要我从这段正文里提炼什么）</span></span>
          <span class="forge-counter">{{ forgeRequirement.length }} / {{ forgeRequirementLimit }}</span>
        </div>
        <NInput
          v-model:value="forgeRequirement"
          type="textarea"
          :rows="4"
          size="small"
          placeholder="例：把这段里可复用的写作约束提炼出来（文风 / 节奏 / 禁忌，要能判定）；或：提炼出这段的人物设定与世界观要点；或：提炼出作者刻意避开的写法"
        />
      </div>

      <div class="forge-controls">
        <NSelect v-model:value="forgeMaxItems" size="small" :options="countOptions" style="width: 130px" />
        <button class="primary-btn" :disabled="forgeRunning || forgeSourceTooLong" @click="runForge">
          {{ forgeRunning ? '提炼中…' : '提炼' }}
        </button>
        <span class="muted forge-hint" style="margin: 0">
          忠实提炼：每条都要能在正文里找到依据，不会替你补充正文没有的设定。
        </span>
      </div>

      <template v-if="forgeResult">
        <div class="section-title" style="margin-top: 14px">
          提炼结果
          <span class="muted" style="font-weight: 400">
            {{ forgeCount }} 条 · {{ forgeResult.model }} · {{ forgeResult.elapsed }}s ·
            {{ forgeResult.isolation }}
          </span>
        </div>
        <div class="forge-result-head">
          <NInput v-model:value="forgeTitle" size="small" placeholder="名称（存进约束库时用）" />
          <button class="ghost-btn" @click="copyConstraints">复制</button>
          <button class="primary-btn" :disabled="forgeSaving" @click="saveForged">
            {{ forgeSaving ? '保存中…' : '存入约束库' }}
          </button>
        </div>
        <NInput
          v-model:value="forgeConstraints"
          type="textarea"
          :rows="10"
          size="small"
          class="forge-out"
          placeholder="提炼结果（可直接编辑后再复制 / 存库）"
        />
        <div class="muted forge-hint">
          每行 `- 条目` 是硬性格式：绑到作品后会渲染成 Writer / Editor 的执行清单逐条核对；
          存库后请到「绑定到当前作品」页把它绑给作品，未绑定不影响生成。
        </div>
      </template>

      <div class="forge-foot">
        <button class="ghost-btn" @click="forgeShowPrompt = !forgeShowPrompt">
          {{ forgeShowPrompt ? '收起智能体设定' : '查看智能体设定（它被要求做什么、不许做什么）' }}
        </button>
        <button v-if="forgeShowPrompt" class="ghost-btn" @click="copyAgentPrompt">复制提示词</button>
      </div>
      <pre v-if="forgeShowPrompt" class="dim-body pre-wrap">{{ forgeAgent?.prompt || '（引擎未就绪，读不到智能体设定）' }}</pre>
      <div class="muted forge-hint" style="margin-top: 8px">
        想改它的行为？提示词与其它智能体同源：在左侧「智能体设置」里改（改完下一轮立即生效），模型绑定在「模型配置」里换。
      </div>
    </div>

    <!-- 报告 -->
    <NModal
      :show="report !== null"
      preset="card"
      :title="`16 维蒸馏报告 · ${reportSkillId}`"
      style="width: 780px"
      @update:show="report = null"
    >
      <div class="scroll-y" style="max-height: 62vh">
        <div
          v-for="(label, key) in DIMENSION_LABELS"
          :key="key"
          class="dim-block"
        >
          <div class="dim-title">{{ label }}</div>
          <pre class="dim-body pre-wrap">{{ JSON.stringify((report as Record<string, unknown>)?.[key] ?? {}, null, 1) }}</pre>
        </div>
      </div>
    </NModal>
  </NModal>
</template>

<style scoped>
.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid #eef0f2;
  margin-bottom: 12px;
}
.tab {
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  padding: 8px 14px;
  font-size: 13.5px;
  color: #5c6470;
  cursor: pointer;
}
.tab.active {
  color: #1d4ed8;
  border-bottom-color: #1d4ed8;
  font-weight: 600;
}
.tab-badge {
  font-size: 10px;
  margin-left: 5px;
  padding: 1px 5px;
  border-radius: 999px;
  background: #eef2ff;
  color: #4f46e5;
  vertical-align: 1px;
}
.tab.active .tab-badge {
  background: #1d4ed8;
  color: #fff;
}
.body {
  max-height: 58vh;
  padding-right: 4px;
}
.section-title {
  font-weight: 600;
  font-size: 13.5px;
  margin: 6px 0 8px;
}
.row-card {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 8px;
}
.row-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.row-title {
  font-weight: 600;
  font-size: 13.5px;
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
.row-actions {
  display: flex;
  gap: 6px;
}
.ghost-btn {
  background: none;
  border: 1px solid #e5e7eb;
  border-radius: 7px;
  padding: 4px 10px;
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
.new-skill {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px dashed #d9dce1;
  border-radius: 10px;
  padding: 10px;
}
.new-skill-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12.5px;
  font-weight: 600;
  color: #5c6470;
}
.bind-hint {
  border: 1px solid #fcd34d;
  background: #fffbeb;
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.7;
  margin-bottom: 12px;
}
.bind-hint-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.primary-btn {
  background: #1f2937;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 7px 18px;
  font-size: 13px;
  cursor: pointer;
  align-self: flex-start;
}
.primary-btn:disabled {
  background: #d1d5db;
}
.bind-row {
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 9px 12px;
  margin-bottom: 6px;
  cursor: pointer;
  font-size: 13.5px;
}
.bind-row.on {
  border-color: #1d4ed8;
  background: #f0f5ff;
}
.bind-check {
  width: 18px;
  height: 18px;
  border-radius: 5px;
  border: 1px solid #c7cdd6;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: #1d4ed8;
}
.bind-row.on .bind-check {
  background: #1d4ed8;
  border-color: #1d4ed8;
  color: #fff;
}
.footer {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}
.dim-block {
  margin-bottom: 10px;
}
.dim-title {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 4px;
}
.dim-body {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  background: #f6f7f8;
  border-radius: 8px;
  padding: 8px 10px;
  max-height: 220px;
  overflow-y: auto;
}

/* ---------- 约束提炼（第三个模块） ---------- */
.forge-banner {
  border: 1px solid #dbeafe;
  background: #f5f9ff;
  border-radius: 10px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.forge-banner-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13.5px;
  margin-bottom: 6px;
}
.forge-scope {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 12px;
  line-height: 1.6;
}
.scope-yes {
  color: #116932;
}
.scope-no {
  color: #9a3412;
}
.forge-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}
.forge-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 10px;
}
.forge-field-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  font-size: 12.5px;
  font-weight: 600;
  color: #3a3d44;
}
.forge-field-head .muted {
  font-weight: 400;
  margin-left: 6px;
}
.forge-counter {
  font-size: 11.5px;
  color: #9aa0aa;
  font-variant-numeric: tabular-nums;
}
.forge-counter.over {
  color: #dc2626;
  font-weight: 600;
}
.forge-hint {
  font-size: 12px;
  line-height: 1.7;
  margin-top: 6px;
}
.forge-result-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
/* 按钮不得被输入框挤到换行（"复制"竖排 / "存入约束库"断成两行——探针截图实测） */
.forge-result-head :deep(.n-input) {
  flex: 1;
  min-width: 0;
}
.forge-result-head button {
  flex-shrink: 0;
  white-space: nowrap;
}
.forge-out :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.8;
}
.forge-foot {
  display: flex;
  gap: 8px;
  margin-top: 14px;
}
</style>
