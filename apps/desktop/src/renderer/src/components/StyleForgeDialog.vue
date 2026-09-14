<script setup lang="ts">
import { NInput, NModal, useMessage } from 'naive-ui'
import { ref, watch } from 'vue'

import { api } from '../api'
import { appStore } from '../store'
import type { Bindings, CustomSkill, SkillIndexEntry } from '../types'
import { DIMENSION_LABELS } from '../types'

const show = defineModel<boolean>('show', { default: false })
const message = useMessage()

const tab = ref<'packs' | 'bind'>('packs')
const skills = ref<SkillIndexEntry[]>([])
const customSkills = ref<CustomSkill[]>([])
const bindings = ref<Bindings>({ bound_custom: [], bound_packs: [] })
const savingBind = ref(false)

// 报告
const reportSkillId = ref('')
const report = ref<Record<string, unknown> | null>(null)
const openDims = ref<string[]>([])

// 新建自定义约束
const newSkill = ref({ title: '', content: '' })

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
  try {
    await api('POST', '/api/custom-skills', {
      title: newSkill.value.title.trim(),
      content: newSkill.value.content.trim(),
    })
    message.success('自定义约束已创建')
    newSkill.value = { title: '', content: '' }
    void loadAll()
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
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
    await api('POST', `/api/bindings?novel=${encodeURIComponent(appStore.bookId)}`, {
      custom_skill_ids: bindings.value.bound_custom,
      pack_ids: bindings.value.bound_packs,
    })
    message.success(`已绑定到《${appStore.bookTitle}》：Writer/Editor 将按最高优先级遵循`)
    appStore.treeVersion += 1
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    savingBind.value = false
  }
}

watch(show, (opened) => {
  if (opened) void loadAll()
})
</script>

<template>
  <NModal v-model:show="show" preset="card" title="风格工坊" class="dw-dialog" style="width: 860px">
    <div class="tabs">
      <button class="tab" :class="{ active: tab === 'packs' }" @click="tab = 'packs'">技能包与约束</button>
      <button class="tab" :class="{ active: tab === 'bind' }" @click="tab = 'bind'">绑定到当前作品</button>
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
        <button class="del-btn" @click="removeCustom(skill)">删除</button>
      </div>
      <div class="new-skill">
        <NInput v-model:value="newSkill.title" size="small" placeholder="约束名称，例：仙侠规范" />
        <NInput
          v-model:value="newSkill.content"
          type="textarea"
          :rows="3"
          size="small"
          placeholder="约束内容（世界观体系 / 内容尺度 / 禁忌清单……将注入 Writer/Editor 最高优先级）"
        />
        <button class="primary-btn" @click="createCustom">添加约束</button>
      </div>
    </div>

    <!-- 绑定 -->
    <div v-else class="scroll-y body">
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
</style>
