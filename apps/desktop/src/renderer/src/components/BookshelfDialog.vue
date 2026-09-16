<script setup lang="ts">
import { NInput, NInputNumber, NModal, NSelect, useDialog, useMessage } from 'naive-ui'
import { ref, watch } from 'vue'

import { api, withNovel } from '../api'
import { appStore, openBook, openWorkspace } from '../store'
import { BRIEF_FIELD_DEFS, emptyBriefFields, hasBriefFields } from '../types'
import type { Book, BriefFields, CustomSkill } from '../types'
import DemoWizardModal from './DemoWizardModal.vue'

const show = defineModel<boolean>('show', { default: false })
const message = useMessage()
const dialog = useDialog()

const books = ref<Book[]>([])
const customSkills = ref<CustomSkill[]>([])

const showCreate = ref(false)
const createForm = ref({
  novelId: '',
  brief: '',
  fields: emptyBriefFields(),
  chapters: 12,
  mode: 'pipeline' as 'pipeline' | 'interactive',
  withDemo: true,
  skillIds: [] as string[],
})
const creating = ref(false)

const wizardBook = ref('')
const wizardBrief = ref('')
const wizardFields = ref<BriefFields>(emptyBriefFields())
const wizardChapters = ref(12)

let retryTimer: ReturnType<typeof setInterval> | null = null

async function refresh(): Promise<void> {
  try {
    const res = await api<{ books: Book[] }>('GET', '/api/books')
    books.value = res.books
    const current = books.value.find((b) => b.novel_id === appStore.bookId)
    if (current && current.title && current.title !== appStore.bookTitle) {
      appStore.bookTitle = current.title
    }
    if (retryTimer) {
      clearInterval(retryTimer)
      retryTimer = null
    }
  } catch {
    // 引擎未就绪：对话框打开期间每 2s 重试
    if (!retryTimer && show.value) {
      retryTimer = setInterval(() => void refresh(), 2000)
    }
  }
}

function openCreate(): void {
  createForm.value = {
    novelId: '',
    brief: '',
    fields: emptyBriefFields(),
    mode: 'pipeline',
    chapters: 12,
    withDemo: true,
    skillIds: [],
  }
  showCreate.value = true
  void api<{ skills: CustomSkill[] }>('GET', '/api/custom-skills')
    .then((res) => {
      customSkills.value = res.skills ?? []
    })
    .catch(() => undefined)
}

async function submitCreate(): Promise<void> {
  const novelId = createForm.value.novelId.trim()
  if (!/^[a-zA-Z0-9_-]+$/.test(novelId)) {
    message.warning('书名标识仅限字母 / 数字 / 下划线 / 连字符')
    return
  }
  // X1 结构化 brief 优先；自由补充文本可作为兜底
  if (!hasBriefFields(createForm.value.fields) && !createForm.value.brief.trim()) {
    message.warning('创作需求不能为空：请至少填写一个结构化字段，或补充说明')
    return
  }
  creating.value = true
  try {
    await api('POST', '/api/books', { novel_id: novelId, mode: createForm.value.mode })
    message.success(`书目 ${novelId} 已创建`)
    showCreate.value = false
    if (createForm.value.withDemo) {
      await api('POST', withNovel('/api/demo', novelId), {
        brief: createForm.value.brief.trim(),
        brief_fields: createForm.value.fields,
        chapters: createForm.value.chapters,
        skill_ids: createForm.value.skillIds,
      })
      wizardBook.value = novelId
      wizardBrief.value = createForm.value.brief.trim()
      wizardFields.value = { ...createForm.value.fields }
      wizardChapters.value = createForm.value.chapters
      void refresh()
    } else {
      await api('POST', withNovel('/api/start', novelId), {
        brief: createForm.value.brief.trim(),
        brief_fields: createForm.value.fields,
        chapters: createForm.value.chapters,
        skill_ids: createForm.value.skillIds,
      })
      message.success('流水线已启动')
      openBook(novelId)
      show.value = false
    }
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  } finally {
    creating.value = false
  }
}

function onWizardConfirmed(): void {
  const novelId = wizardBook.value
  const mode = createForm.value.mode
  wizardBook.value = ''
  if (mode === 'interactive') {
    // 互动创作：设定确认后**不启动章节流水线**，直接进互动创作开始出剧情卡
    openBook(novelId)
    show.value = false
    api('POST', withNovel('/api/interactive/start', novelId))
      .then(() => message.success('设定已入库，进入互动创作：正在为你设计本章剧情卡'))
      .catch((err) =>
        message.error(`进入互动创作失败：${err instanceof Error ? err.message : String(err)}`),
      )
    return
  }
  api('POST', withNovel('/api/start', novelId), {
    brief: wizardBrief.value,
    brief_fields: wizardFields.value,
    chapters: wizardChapters.value,
  })
    .then(() => {
      message.success('流水线已启动')
      openBook(novelId)
      show.value = false
    })
    .catch((err) => {
      message.error(`启动失败：${err instanceof Error ? err.message : String(err)}`)
      openBook(novelId)
      show.value = false
    })
}

function removeBook(book: Book): void {
  dialog.warning({
    title: '删除书目',
    content: `将删除《${book.title}》的全部 MD 事实源与派生数据，不可恢复。确认？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await api('DELETE', `/api/books/${encodeURIComponent(book.novel_id)}`)
        message.success('已删除')
        // 删掉的就是当前书 → 回到工作区（清空标题/选中/待办，并刷新资源树）
        if (book.novel_id === appStore.bookId) openWorkspace()
        void refresh()
      } catch (err) {
        message.error(err instanceof Error ? err.message : String(err))
      }
    },
  })
}

function enter(book: Book): void {
  openBook(book.novel_id, book.title)
  show.value = false
}

function progress(book: Book): number {
  if (book.chapters === 0) return 0
  return Math.round((book.approved / book.chapters) * 100)
}

watch(show, (opened) => {
  if (opened) {
    void refresh()
    if (!retryTimer) retryTimer = setInterval(() => void refresh(), 2000)
  } else if (retryTimer) {
    clearInterval(retryTimer)
    retryTimer = null
  }
})
</script>

<template>
  <!-- ⚠ 向导打开时隐藏书架自身的遮罩，避免两层 rgba(0,0,0,.4) 叠加（实测 0.36×255 ≈ 92 灰）。
       两个坑（已由真实浏览器探针验证，勿凭记忆改）：
       1) 属性名是 show-mask，不是 mask。naive-ui 2.44.1 的 modalProps 里只有
          showMask / maskClosable（旧版 unstable-show-mask 已废弃），写 :mask 不会被识别，
          会静默落入 $attrs 变成无效 DOM 属性 —— 遮罩照旧显示。
       2) 必须同时把 mask-closable 一起关掉：naive-ui 在 showMask=false 时会**转而挂载
          clickoutside 指令**（Modal.mjs: onClickoutside: showMask ? undefined : handleClickoutside），
          此时点击向导周围的区域会关掉书架；而书架用 displayDirective='if'（Modal 默认值），
          一旦关闭就会连带卸载嵌套在它内部的向导弹窗 —— 向导只能在书架槽位里存活，
          所以这里绝不能让书架被意外关掉。
          向导自身带遮罩（z-index 更高、mask-closable=false），本就拦截了这些点击，
          这里是把安全性做成显式约束，而不是依赖 z-index 的隐式顺序。 -->
  <NModal
    v-model:show="show"
    preset="card"
    title="书架"
    class="dw-dialog"
    style="width: 780px"
    :show-mask="wizardBook === ''"
    :mask-closable="wizardBook === ''"
  >
    <div class="shelf-head">
      <span class="muted">全部作品以本地 Markdown 文件夹为唯一事实源，可 Git 管理</span>
      <button class="primary-btn" @click="openCreate">新建作品</button>
    </div>

    <div class="grid">
      <div v-for="book in books" :key="book.novel_id" class="book-card">
        <div class="book-head">
          <span class="book-title">{{ book.title }}</span>
          <span v-if="book.active" class="tag busy">生成中</span>
          <span v-else-if="book.finished" class="tag ok">已完结</span>
        </div>
        <div class="muted">{{ book.novel_id }}</div>
        <div class="progress-line">
          <div class="progress-track">
            <div class="progress-fill" :style="{ width: progress(book) + '%' }" />
          </div>
          <span class="muted">{{ book.approved }}/{{ book.chapters }}</span>
        </div>
        <div class="card-actions">
          <button class="open-btn" @click="enter(book)">打开工作台</button>
          <button v-if="!book.is_default" class="del-btn" @click="removeBook(book)">删除</button>
        </div>
      </div>
      <div v-if="books.length === 0" class="muted" style="padding: 20px 4px">
        书架空空如也——点击右上角「新建作品」开始第一部小说。
      </div>
    </div>

    <!-- 新建作品 -->
    <NModal v-model:show="showCreate" preset="card" title="新建作品" style="width: 620px">
      <div class="form">
        <label class="field">
          <span class="label">书名标识（novel_id，仅字母数字下划线连字符）</span>
          <NInput v-model:value="createForm.novelId" placeholder="例：my-first-novel" />
        </label>
        <div class="field">
          <span class="label">创作模式</span>
          <div class="mode-row">
            <label class="mode-opt" :class="{ on: createForm.mode === 'pipeline' }">
              <input v-model="createForm.mode" type="radio" value="pipeline" />
              <b>自由创作</b>
              <small class="muted">大纲 → 章节流水线；按计划推进，逐章人审</small>
            </label>
            <label class="mode-opt" :class="{ on: createForm.mode === 'interactive' }">
              <input v-model="createForm.mode" type="radio" value="interactive" />
              <b>互动创作</b>
              <small class="muted">只要世界观与人物（不产大纲）；剧情卡逐章推进，自由度更高</small>
            </label>
          </div>
        </div>
        <label class="field">
          <span class="label">创作需求 brief（你要写什么，直接由你提供）</span>
          <NInput
            v-model:value="createForm.brief"
            type="textarea"
            :rows="4"
            placeholder="例：东方玄幻，冷峻剑修主角，复仇主线，女主是卧底；每章结尾留钩子；不要金手指……"
          />
        </label>
        <div class="field">
          <span class="label">结构化创作需求（选填 · 字段越具体，产出越不跑偏）</span>
          <div class="brief-grid">
            <label v-for="d in BRIEF_FIELD_DEFS" :key="d.key" class="field">
              <span class="label">{{ d.label }}</span>
              <NInput
                v-model:value="createForm.fields[d.key]"
                size="small"
                :placeholder="d.placeholder"
              />
            </label>
          </div>
        </div>
        <div class="row">
          <label v-if="createForm.mode === 'pipeline'" class="field half">
            <span class="label">总章数</span>
            <NInputNumber v-model:value="createForm.chapters" :min="1" :max="2000" />
          </label>
          <label class="field half">
            <span class="label">自定义约束 Skill</span>
            <NSelect
              v-model:value="createForm.skillIds"
              multiple
              clearable
              :options="customSkills.map((s) => ({ label: s.title, value: s.skill_id }))"
              placeholder="可选"
            />
          </label>
        </div>
        <label class="check-line">
          <input v-model="createForm.withDemo" type="checkbox" />
          先生成设定 Demo 供我逐字段审核（推荐；否则直接开始全自动生成）
        </label>
        <div class="footer">
          <button class="ghost-btn" @click="showCreate = false">取消</button>
          <button class="primary-btn" :disabled="creating" @click="submitCreate">
            {{ creating ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </NModal>

    <!-- 设定 Demo 审核向导。
         注意：这里**故意不写 preset** —— 面板外观由 DemoWizardModal 自绘
         （它自带标题栏与"先审后入库"副标题）。因此：
         · 不能写 content-style：它属于 presetProps（cardBaseProps ∪ dialogProps），
           只在 preset="card" 时由 NCard 消费，无 preset 时会被静默忽略；
         · style 会经 BodyWrapper 的 mergeProps 直接落到向导根元素 .wizard 上，
           所以宽高写在这里是生效的；
         · 不要在 NModal 上写 role —— role 是 NModal 自己声明的 prop，会被它消费掉，
           不会透传到面板元素（aria-modal 不是 prop 才会透传）。
           可访问性属性统一写在 DemoWizardModal 的根元素上，语义明确且不依赖透传行为。 -->
    <NModal
      :show="wizardBook !== ''"
      :mask-closable="false"
      :close-on-esc="false"
      style="width: 860px; height: 86vh"
    >
      <DemoWizardModal
        :novel-id="wizardBook"
        :brief="wizardBrief"
        :brief-fields="wizardFields"
        :chapters="wizardChapters"
        :mode="createForm.mode"
        @confirmed="onWizardConfirmed"
        @cancelled="wizardBook = ''"
      />
    </NModal>
  </NModal>
</template>

<style scoped>
.shelf-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}
.primary-btn {
  background: #1f2937;
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 7px 16px;
  font-size: 13px;
  cursor: pointer;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}
.book-card {
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.book-card:hover {
  border-color: #c7cdd6;
}
.book-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.book-title {
  font-weight: 600;
  font-size: 14.5px;
}
.tag {
  font-size: 11px;
  border-radius: 999px;
  padding: 1px 8px;
  white-space: nowrap;
}
.tag.ok {
  background: #e6f6ec;
  color: #116932;
}
.tag.busy {
  background: #e8f0fe;
  color: #1d4ed8;
}
.progress-line {
  display: flex;
  align-items: center;
  gap: 8px;
}
.progress-track {
  flex: 1;
  height: 5px;
  background: #eef0f2;
  border-radius: 999px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: #1d4ed8;
  border-radius: 999px;
}
.card-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 4px;
}
.open-btn {
  background: #f3f4f6;
  color: #26272b;
  border: none;
  border-radius: 7px;
  padding: 6px 14px;
  font-size: 12.5px;
  cursor: pointer;
}
.open-btn:hover {
  background: #e8eaee;
}
.del-btn {
  background: none;
  border: none;
  color: #dc2626;
  font-size: 12.5px;
  cursor: pointer;
}
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
  color: #5c6470;
}
.brief-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 14px;
}
.row {
  display: flex;
  gap: 14px;
}
.half {
  flex: 1;
}
.check-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #3a3d44;
}
.footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
.ghost-btn {
  background: none;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 6px 16px;
  font-size: 13px;
  cursor: pointer;
  color: #3a3d44;
}
.mode-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.mode-opt {
  display: flex;
  flex-direction: column;
  gap: 3px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 9px 12px;
  cursor: pointer;
  font-size: 13px;
}
.mode-opt.on {
  border-color: #1d4ed8;
  background: #f0f5ff;
}
.mode-opt b {
  font-weight: 600;
}
.mode-opt small {
  line-height: 1.5;
}
</style>
