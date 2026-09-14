<script setup lang="ts">
import { NInput, NInputNumber, NModal, NSelect, useDialog, useMessage } from 'naive-ui'
import { ref, watch } from 'vue'

import { api, withNovel } from '../api'
import { appStore, openBook } from '../store'
import type { Book, CustomSkill } from '../types'
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
  chapters: 12,
  withDemo: true,
  skillIds: [] as string[],
})
const creating = ref(false)

const wizardBook = ref('')
const wizardBrief = ref('')
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
  createForm.value = { novelId: '', brief: '', chapters: 12, withDemo: true, skillIds: [] }
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
  if (!createForm.value.brief.trim()) {
    message.warning('创作需求不能为空')
    return
  }
  creating.value = true
  try {
    await api('POST', '/api/books', { novel_id: novelId })
    message.success(`书目 ${novelId} 已创建`)
    showCreate.value = false
    if (createForm.value.withDemo) {
      await api('POST', withNovel('/api/demo', novelId), {
        brief: createForm.value.brief.trim(),
        chapters: createForm.value.chapters,
        skill_ids: createForm.value.skillIds,
      })
      wizardBook.value = novelId
      wizardBrief.value = createForm.value.brief.trim()
      wizardChapters.value = createForm.value.chapters
      void refresh()
    } else {
      await api('POST', withNovel('/api/start', novelId), {
        brief: createForm.value.brief.trim(),
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
  wizardBook.value = ''
  api('POST', withNovel('/api/start', novelId), {
    brief: wizardBrief.value,
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
        if (book.novel_id === appStore.bookId) appStore.bookId = ''
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
  <NModal v-model:show="show" preset="card" title="书架" class="dw-dialog" style="width: 780px">
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
        <label class="field">
          <span class="label">创作需求（brief）</span>
          <NInput
            v-model:value="createForm.brief"
            type="textarea"
            :rows="4"
            placeholder="例：东方玄幻，冷峻剑修主角，复仇主线，注重战力体系一致性……"
          />
        </label>
        <div class="row">
          <label class="field half">
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

    <!-- 设定 Demo 审核向导 -->
    <NModal
      :show="wizardBook !== ''"
      :mask-closable="false"
      :close-on-esc="false"
      style="width: 860px; height: 86vh"
      content-style="height:calc(86vh - 62px); padding:0"
    >
      <DemoWizardModal
        :novel-id="wizardBook"
        :brief="wizardBrief"
        :chapters="wizardChapters"
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
</style>
