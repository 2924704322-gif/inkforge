<script setup lang="ts">
import { NButton, NCheckbox, NInput, NInputNumber, NModal, NSelect, NSwitch, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'

import { api, withNovel } from '../api'
import type { CustomSkill } from '../types'

const props = defineProps<{
  novelId: string
  defaultBrief?: string
  defaultChapters?: number
}>()

const emit = defineEmits<{ started: [] }>()

const message = useMessage()

const visible = defineModel<boolean>('show', { default: false })

const brief = ref(props.defaultBrief ?? '')
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
  if (props.defaultChapters) chapters.value = props.defaultChapters
  void api<{ skills: CustomSkill[] }>('GET', '/api/custom-skills')
    .then((res) => {
      customSkills.value = res.skills ?? []
    })
    .catch(() => undefined)
})

async function start(): Promise<void> {
  console.info('[StartRunModal] start clicked, brief length =', brief.value.trim().length)
  if (!brief.value.trim()) {
    message.warning('创作需求（brief）不能为空')
    return
  }
  starting.value = true
  try {
    await api('POST', withNovel('/api/start', props.novelId), {
      brief: brief.value.trim(),
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
      <label class="field">
        <span class="label">创作需求（brief）</span>
        <NInput
          v-model:value="brief"
          type="textarea"
          :rows="4"
          placeholder="例：东方玄幻，冷峻剑修主角，复仇主线，群像立体……"
        />
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
