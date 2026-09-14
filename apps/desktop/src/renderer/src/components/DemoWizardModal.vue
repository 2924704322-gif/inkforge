<script setup lang="ts">
import { NInput, NSpin, useMessage } from 'naive-ui'
import { onMounted, onUnmounted, reactive, ref } from 'vue'

import { api, withNovel } from '../api'
import type { DemoOutput, DemoSnapshot } from '../types'

const props = defineProps<{
  novelId: string
  brief: string
  chapters: number
}>()

const emit = defineEmits<{ confirmed: []; cancelled: [] }>()

const message = useMessage()

const snapshot = reactive<DemoSnapshot>({ status: 'running', error: null, demo: null })
let timer: ReturnType<typeof setInterval> | null = null

const demo = ref<DemoOutput | null>(null)
const feedback = ref('')
const confirming = ref(false)

async function pollOnce(): Promise<void> {
  try {
    const snap = await api<DemoSnapshot>('GET', withNovel('/api/demo', props.novelId))
    snapshot.status = snap.status
    snapshot.error = snap.error
    snapshot.demo = snap.demo
    if (snap.status === 'done' && snap.demo && demo.value === null) {
      demo.value = JSON.parse(JSON.stringify(snap.demo)) as DemoOutput
    }
    if (snap.status === 'done' && timer) {
      clearInterval(timer)
      timer = null
    }
    if (snap.status === 'error' && timer) {
      clearInterval(timer)
      timer = null
    }
  } catch (err) {
    snapshot.status = 'error'
    snapshot.error = err instanceof Error ? err.message : String(err)
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }
}

async function regenerate(): Promise<void> {
  snapshot.status = 'running'
  snapshot.error = null
  try {
    await api('POST', withNovel('/api/demo', props.novelId), {
      brief: props.brief,
      chapters: props.chapters,
      feedback: feedback.value.trim(),
    })
    demo.value = null
    if (timer) clearInterval(timer)
    timer = setInterval(() => void pollOnce(), 1500)
  } catch (err) {
    snapshot.status = 'error'
    snapshot.error = err instanceof Error ? err.message : String(err)
  }
}

async function confirmDemo(): Promise<void> {
  if (demo.value === null) return
  if (!demo.value.book_title.trim()) {
    message.warning('书名不能为空')
    return
  }
  confirming.value = true
  try {
    await api('POST', withNovel('/api/demo/confirm', props.novelId), { demo: demo.value })
    message.success('设定已入库')
    emit('confirmed')
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    confirming.value = false
  }
}

onMounted(() => {
  void pollOnce()
  timer = setInterval(() => void pollOnce(), 1500)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div class="wizard" role="dialog" aria-modal="true" aria-label="设定 Demo 审核">
    <div class="wizard-head">
      <span class="wizard-title">设定 Demo 审核</span>
      <span class="muted">先审后入库：AI 产出的世界观与人物，经你逐字段确认后才会写入资料库</span>
    </div>

    <div v-if="snapshot.status === 'running'" class="wizard-body center">
      <NSpin size="large" />
      <div class="muted">Architect 正在生成设定 Demo…（约 1-3 分钟）</div>
    </div>

    <div v-else-if="snapshot.status === 'error'" class="wizard-body">
      <div class="error-text">生成失败：{{ snapshot.error }}</div>
      <NInput
        v-model:value="feedback"
        type="textarea"
        :rows="2"
        placeholder="可填写修改意见后重新生成（可选）"
      />
      <button class="retry-btn" @click="regenerate">重新生成</button>
    </div>

    <div v-else-if="demo" class="wizard-body">
      <div class="scroll-y form-scroll">
        <label class="field">
          <span class="field-label">书名</span>
          <NInput v-model:value="demo.book_title" />
        </label>
        <label class="field">
          <span class="field-label">主题（一句话）</span>
          <NInput v-model:value="demo.theme" />
        </label>
        <label class="field">
          <span class="field-label">故事梗概</span>
          <NInput v-model:value="demo.synopsis" type="textarea" :rows="3" />
        </label>
        <label class="field">
          <span class="field-label">整体概要（开端 / 发展 / 高潮 / 结局）</span>
          <NInput v-model:value="demo.overview" type="textarea" :rows="5" />
        </label>

        <div class="field">
          <div class="field-label row-between">
            <span>世界观文档（{{ demo.worldview.length }}）</span>
            <button
              class="mini-btn"
              @click="demo.worldview.push({ filename: `doc-${demo.worldview.length + 1}`, title: '', content: '' })"
            >
              + 添加
            </button>
          </div>
          <div v-for="(doc, i) in demo.worldview" :key="i" class="sub-card">
            <div class="row-between">
              <NInput v-model:value="doc.title" size="small" placeholder="文档标题" style="width: 260px" />
              <button class="mini-btn danger" @click="demo.worldview.splice(i, 1)">删除</button>
            </div>
            <NInput v-model:value="doc.content" type="textarea" :rows="4" placeholder="世界观正文（具体、可判定）" />
          </div>
        </div>

        <div class="field">
          <div class="field-label row-between">
            <span>人物卡（{{ demo.characters.length }}）</span>
            <button
              class="mini-btn"
              @click="demo.characters.push({ name: '', role: '配角', appearance: '', personality: '', background: '' })"
            >
              + 添加
            </button>
          </div>
          <div v-for="(ch, i) in demo.characters" :key="i" class="sub-card">
            <div class="row-between">
              <div class="char-head">
                <NInput v-model:value="ch.name" size="small" placeholder="姓名" style="width: 140px" />
                <NInput v-model:value="ch.role" size="small" placeholder="主角/配角/反派" style="width: 140px" />
              </div>
              <button class="mini-btn danger" @click="demo.characters.splice(i, 1)">删除</button>
            </div>
            <NInput v-model:value="ch.appearance" size="small" placeholder="外貌特征" />
            <NInput v-model:value="ch.personality" size="small" placeholder="性格、欲望与缺陷" />
            <NInput v-model:value="ch.background" type="textarea" :rows="2" size="small" placeholder="背景故事" />
          </div>
        </div>
      </div>

      <div class="wizard-foot">
        <span class="muted">确认后设定写入 settings/，正式生成时不再推翻。</span>
        <div class="row-gap">
          <button class="retry-btn" @click="emit('cancelled')">取消</button>
          <button class="primary-btn" :disabled="confirming" @click="confirmDemo">
            {{ confirming ? '入库中…' : '同意设定并开始生成' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wizard {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 16px;
  gap: 12px;
  /* 面板底色必须由本组件自绘。
     宿主 NModal 没有 preset，而 naive-ui 只在 preset="card"/"dialog" 时才渲染
     NCard / NDialog 来提供面板背景（见 BodyWrapper.mjs 的 preset 分支）；
     .n-modal 自身样式只有 position / align-self / margin / box-shadow，没有 background。
     若此处不给背景，整块面板就是全透明的 —— 屏幕上只剩 40% 黑遮罩加一圈
     box-shadow，"屏幕变灰、只有内容框是亮的" 正是由此而来。
     scoped 编译后选择器为 .wizard[data-v-*]，特异性高于 .n-modal，
     可一并覆盖它自带的 box-shadow。 */
  background: #ffffff;
  border-radius: 12px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.18);
  overflow: hidden;
}
.wizard-head {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.wizard-title {
  font-size: 17px;
  font-weight: 600;
}
.wizard-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.wizard-body.center {
  align-items: center;
  justify-content: center;
  gap: 14px;
}
.form-scroll {
  flex: 1;
  min-height: 0;
  padding-right: 6px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 14px;
}
.field-label {
  font-size: 13px;
  opacity: 0.85;
}
.row-between {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.row-gap {
  display: flex;
  gap: 10px;
  align-items: center;
}
.sub-card {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: #fafafa;
}
.char-head {
  display: flex;
  gap: 8px;
}
.mini-btn {
  font-size: 12px;
  color: #1d4ed8;
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px 6px;
}
.mini-btn.danger {
  color: #ff9d96;
}
.mini-btn:hover {
  opacity: 0.8;
}
.primary-btn {
  background: #1f2937;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 8px 18px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}
.primary-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.retry-btn {
  background: #fff;
  color: #3a3d44;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 7px 16px;
  cursor: pointer;
}
.error-text {
  color: #dc2626;
}
.wizard-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-top: 1px solid #eef0f2;
  padding-top: 10px;
}
</style>
