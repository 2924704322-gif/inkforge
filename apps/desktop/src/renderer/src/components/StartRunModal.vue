<script setup lang="ts">
import { NButton, NCheckbox, NInput, NInputNumber, NModal, NSelect, NSwitch, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'

import { api, withNovel } from '../api'
import { BRIEF_FIELD_DEFS, emptyBriefFields, hasBriefFields } from '../types'
import type { BriefFields, CustomSkill } from '../types'

const props = defineProps<{
  novelId: string
  defaultBrief?: string
  defaultBriefFields?: BriefFields
  defaultChapters?: number
}>()

const emit = defineEmits<{ started: [] }>()

const message = useMessage()

const visible = defineModel<boolean>('show', { default: false })

const brief = ref(props.defaultBrief ?? '')
const briefFields = ref<BriefFields>(props.defaultBriefFields ?? emptyBriefFields())
const chapters = ref(props.defaultChapters ?? 10)
const parallel = ref(false)
const workers = ref(2)
const selectedSkills = ref<string[]>([])
const customSkills = ref<CustomSkill[]>([])
const starting = ref(false)

const skillOptions = computed(() =>
  customSkills.value.map((s) => ({ label: s.title, value: s.skill_id })),
)

watch(visible, (opened) => {
  if (!opened) return
  if (props.defaultBrief) brief.value = props.defaultBrief
  if (props.defaultBriefFields) briefFields.value = { ...props.defaultBriefFields }
  if (props.defaultChapters) chapters.value = props.defaultChapters
  void api<{ skills: CustomSkill[] }>('GET', '/api/custom-skills')
    .then((res) => {
      customSkills.value = res.skills ?? []
    })
    .catch(() => undefined)
})

async function start(): Promise<void> {
  console.info('[StartRunModal] start clicked, brief length =', brief.value.trim().length)
  if (!hasBriefFields(briefFields.value) && !brief.value.trim()) {
    message.warning('创作需求不能为空：请至少填写一个结构化字段，或补充说明')
    return
  }
  starting.value = true
  try {
    await api('POST', withNovel('/api/start', props.novelId), {
      brief: brief.value.trim(),
      brief_fields: briefFields.value,
      chapters: chapters.value ?? 10,
      parallel: parallel.value,
      workers: parallel.value ? workers.value : 0,
      skill_ids: selectedSkills.value,
    })
    console.info('[StartRunModal] start ok')
    message.success('流水线已启动')
    emit('started')
    visible.value = false
  } catch (err) {
    console.error('[StartRunModal] start failed:', err)
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    starting.value = false
  }
}
</script>

<template>
  <NModal v-model:show="visible" preset="card" title="开始生成" style="width: 620px">
    <div class="form">
      <div class="field">
        <span class="label">结构化创作需求（X1：字段越具体，产出越不跑偏）</span>
        <div class="brief-grid">
          <label v-for="d in BRIEF_FIELD_DEFS" :key="d.key" class="field">
            <span class="label">{{ d.label }}</span>
            <NInput
              v-model:value="briefFields[d.key]"
              size="small"
              :placeholder="d.placeholder"
            />
          </label>
        </div>
      </div>
      <label class="field">
        <span class="label">补充说明（可选）</span>
        <NInput
          v-model:value="brief"
          type="textarea"
          :rows="2"
          placeholder="例：另需每章结尾留钩子；避免使用现代网络用语……"
        />
      </label>
      <!-- 现实性口径（用户要求）：默认以作者创作目标为准，勾上才允许按现实逻辑提建议 -->
      <label class="realism-row">
        <input v-model="briefFields.allow_realism" type="checkbox" />
        <span>
          <b>要求现实合理性约束</b>
          <small class="muted">
            默认关闭：审校与写手只以你的创作目标为准，不会因为「不符合现实 / 不合理」要求你改设定或改稿。
            勾上后才会按现实逻辑（物理 / 生理 / 社会 / 常识）提建议。
          </small>
        </span>
      </label>
      <div class="row">
        <label class="field half">
          <span class="label">总章数</span>
          <NInputNumber v-model:value="chapters" :min="1" :max="2000" />
        </label>
        <label class="field half">
          <span class="label">卷级并行</span>
          <div class="inline">
            <NSwitch v-model:value="parallel" />
            <NInputNumber v-if="parallel" v-model:value="workers" :min="2" :max="8" size="small" />
            <span v-if="parallel" class="muted">并发数</span>
          </div>
        </label>
      </div>
      <label class="field">
        <span class="label">自定义创作约束 Skill（注入 Writer / Editor 最高优先级）</span>
        <NSelect v-model:value="selectedSkills" multiple clearable :options="skillOptions"
                 placeholder="可选；从蒸馏工坊左侧『自定义约束』中管理" />
      </label>
      <NCheckbox size="small" :checked="true" :disabled="true">
        大纲产出后将进入人工审阅关卡，通过后才进入章节循环
      </NCheckbox>
      <div class="footer">
        <NButton @click="visible = false">取消</NButton>
        <NButton type="primary" :loading="starting" @click="start">启动流水线</NButton>
      </div>
    </div>
  </NModal>
</template>

<style scoped>
.form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.label {
  font-size: 13px;
  opacity: 0.85;
}
.brief-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 14px;
}
/* 现实性口径开关：默认关闭 = 以作者创作目标为准 */
.realism-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
  color: #3a3d44;
  padding: 8px 10px;
  border: 1px solid #eceef1;
  border-radius: 8px;
  background: #f9fafb;
}
.realism-row input {
  margin-top: 3px;
}
.realism-row small {
  display: block;
  line-height: 1.6;
  margin-top: 2px;
}
.row {
  display: flex;
  gap: 16px;
}
.half {
  flex: 1;
}
.inline {
  display: flex;
  align-items: center;
  gap: 8px;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
