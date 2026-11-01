<script setup lang="ts">
import { NForm, NFormItem, NInput, NInputNumber, NModal, NSelect, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'

import { api } from '../api'
import type { ModelConfigView, ProviderInfo, ProviderModels, RoleBinding } from '../types'

const show = defineModel<boolean>('show', { default: false })
const message = useMessage()

const config = ref<ModelConfigView | null>(null)

async function load(): Promise<void> {
  try {
    config.value = await api<ModelConfigView>('GET', '/api/model-config')
    // 打开就先备好清单：否则角色行里的下拉点开只有「当前值」一项，
    // 用户看到的就是「模型已拉取，但没有选项可选」。
    for (const provider of providerNames()) prefetchModels(provider)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

/** 角色实际引用到的接入点名（去重，保持出现顺序）。 */
function providerNames(): string[] {
  return [...new Set((config.value?.roles ?? []).map((r) => r.provider).filter(Boolean))]
}

const providerEntries = computed(() => config.value?.providers ?? [])
const roleEntries = computed(() => config.value?.roles ?? [])
const providerOptions = computed(() => providerEntries.value.map((p) => ({ label: p.name, value: p.name })))

/** 角色元数据：与实际智能体一一对应，未登记的角色归入「其他」。 */
const ROLE_META: Record<string, { label: string; group: string }> = {
  master: { label: '主智能体（墨师）', group: '对话智能体' },
  character: { label: '人物设计师', group: '对话智能体' },
  plot: { label: '剧情策划', group: '对话智能体' },
  outline: { label: '大纲规划', group: '对话智能体' },
  prose: { label: '正文写手', group: '对话智能体' },
  review: { label: '审校主编', group: '对话智能体' },
  constraint: { label: '约束提炼（风格工坊）', group: '对话智能体' },
  chat: { label: '通用对话（兼容旧会话）', group: '对话智能体' },
  architect: { label: '大纲 Architect', group: '流水线智能体' },
  writer: { label: '正文 Writer', group: '流水线智能体' },
  editor: { label: '审校 Editor', group: '流水线智能体' },
  distiller: { label: '蒸馏 Distiller', group: '流水线智能体' },
}

const roleGroups = computed(() => {
  const groups = new Map<string, { role: string; binding: RoleBinding }[]>()
  for (const binding of roleEntries.value) {
    const meta = ROLE_META[binding.role] ?? { label: binding.role, group: '其他' }
    const list = groups.get(meta.group) ?? []
    list.push({ role: meta.label, binding })
    groups.set(meta.group, list)
  }
  return [...groups.entries()].map(([group, rows]) => ({ group, rows }))
})

const testing = ref('')
const testResult = ref<{ role: string; ok: boolean; text: string } | null>(null)

async function testRole(role: string): Promise<void> {
  testing.value = role
  try {
    const res = await api<{ ok: boolean; provider?: string; model?: string; reply?: string; error?: string }>(
      'POST',
      '/api/model-test',
      { role },
    )
    testResult.value = {
      role,
      ok: res.ok,
      text: res.ok ? `✓ ${res.provider}/${res.model}` : `✗ ${res.error ?? '失败'}`,
    }
  } catch (err) {
    testResult.value = { role, ok: false, text: `✗ ${err instanceof Error ? err.message : String(err)}` }
  } finally {
    testing.value = ''
  }
}

const showRole = ref(false)
const roleForm = ref({ role: '', provider: '', model: '', temperature: 0.7, maxTokens: null as number | null })

// ── 获取模型：把「手抄模型名」变成「从接入点拉清单再选」 ──

/** 接入点 → 已拉到的模型清单（会话内缓存：同一接入点不重复打网络）。 */
const modelsByProvider = ref<Record<string, string[]>>({})
const fetching = ref('')
const fetchError = ref('')

/**
 * 拉取某接入点当前可用的模型清单。
 *
 * `override` 服务于「接入点还没保存，想先看看有哪些模型」：把弹窗表单里的
 * type/base_url/api_key 直接带上，后端以表单值优先（api_key 传 **** = 沿用已存密钥）。
 * `silent=true`（打开弹窗时的预取）不弹提示，只把失败原因留在页面内联显示。
 */
async function fetchModels(
  provider: string,
  override: { type?: string; baseUrl?: string; apiKey?: string } = {},
  silent = false,
): Promise<void> {
  const name = provider.trim()
  if (!name) {
    if (!silent) message.warning('请先选择/填写接入点名称')
    return
  }
  const qs = new URLSearchParams()
  if (override.type) qs.set('type', override.type)
  if (override.baseUrl) qs.set('base_url', override.baseUrl)
  if (override.apiKey) qs.set('api_key', override.apiKey)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''

  fetching.value = name
  fetchError.value = ''
  try {
    const res = await api<ProviderModels>(
      'GET',
      `/api/model-config/providers/${encodeURIComponent(name)}/models${suffix}`,
    )
    if (!res.ok) {
      fetchError.value = res.error ?? '拉取模型列表失败'
      if (!silent) message.warning(`获取模型失败：${fetchError.value}`)
      return
    }
    modelsByProvider.value = { ...modelsByProvider.value, [name]: res.models }
    if (!silent) {
      message.success(
        res.count > 0
          ? `${name}：拉到 ${res.count} 个模型，可在「模型名」里直接选`
          : `${name}：接口没返回任何模型，请手填模型名`,
      )
    }
  } catch (err) {
    fetchError.value = err instanceof Error ? err.message : String(err)
    if (!silent) message.error(`获取模型失败：${fetchError.value}`)
  } finally {
    if (fetching.value === name) fetching.value = ''
  }
}

/** 打开弹窗时静默预取（已有缓存 / 正在请求就不重复打网络；要刷新点「获取模型」）。 */
function prefetchModels(provider: string): void {
  const name = provider.trim()
  if (!name || modelsByProvider.value[name] || fetching.value === name) return
  void fetchModels(name, {}, true)
}

/** 模型下拉候选：已拉到的清单 + 当前绑定值（清单里没有也必须显示得出来）。 */
function optionsFor(provider: string, current: string): { label: string; value: string }[] {
  const list = modelsByProvider.value[provider] ?? []
  const cur = (current ?? '').trim()
  const merged = cur && !list.includes(cur) ? [cur, ...list] : list
  return merged.map((m) => ({ label: m, value: m }))
}

const modelOptions = computed(() => optionsFor(roleForm.value.provider, roleForm.value.model))

/** 挨个拉一遍所有被角色引用的接入点（顶部「获取模型」按钮：拉取失败的补救入口）。 */
async function fetchAllProviders(): Promise<void> {
  const names = providerNames()
  if (!names.length) {
    message.warning('还没有接入点可拉取：先「新增接入点」')
    return
  }
  for (const name of names) await fetchModels(name, {}, true)
  const summary = names.map((n) => `${n}：${modelsByProvider.value[n]?.length ?? 0} 个`).join('；')
  if (names.every((n) => (modelsByProvider.value[n]?.length ?? 0) === 0)) {
    message.warning(`没拉到任何模型（${fetchError.value || '接口未返回'}）`)
  } else {
    message.success(`模型清单已更新 —— ${summary}`)
  }
}

/** 行内下拉的悬停说明：拉取失败时必须能告诉用户「怎么补救」，别只剩一个空列表。 */
function rowTitle(binding: RoleBinding): string {
  const count = modelsByProvider.value[binding.provider]?.length ?? 0
  return count > 0
    ? `${binding.role}：展开可选 ${binding.provider} 上已拉取到的 ${count} 个模型（也可直接输入）`
    : `${binding.role}：还没拿到 ${binding.provider} 的模型清单 —— 点右上「获取模型」重试，或直接输入模型名`
}

/** 行内换接入点：先把它的模型清单拉好，再连「接入点 + 模型」一起落库。
 *
 *  只换 provider 不换 model 会留下一个「新接入点上不存在的模型名」，
 *  下一次调用就会 404；所以这里在新清单里挑一个真实存在的模型（原模型不在清单里时取第一个）。
 */
async function quickSetProvider(binding: RoleBinding, provider: string): Promise<void> {
  const name = (provider ?? '').trim()
  if (!name || name === binding.provider) return
  savingRole.value = binding.role
  try {
    if (!(modelsByProvider.value[name] ?? []).length) await fetchModels(name, {}, true)
    const list = modelsByProvider.value[name] ?? []
    const nextModel = !list.length || list.includes(binding.model) ? binding.model : list[0]!
    const res = await api('PUT', `/api/model-config/roles/${binding.role}`, {
      provider: name,
      model: nextModel,
      temperature: binding.temperature,
      max_tokens: binding.max_tokens ?? undefined,
    })
    config.value = res as ModelConfigView
    message.success(
      list.length
        ? `${binding.role} → ${name} / ${nextModel}`
        : `${binding.role} → ${name}（没拉到模型清单，请确认模型名）`,
    )
  } catch (err) {
    message.error(`换接入点失败：${err instanceof Error ? err.message : String(err)}`)
  } finally {
    savingRole.value = ''
  }
}

/** 角色行内联换模型：改一下立即落库，不必「编辑 → 选 → 保存」三步。 */
const savingRole = ref('')

async function quickSetModel(binding: RoleBinding, model: string): Promise<void> {
  const next = (model ?? '').trim()
  if (!next || next === binding.model) return
  savingRole.value = binding.role
  try {
    const res = await api('PUT', `/api/model-config/roles/${binding.role}`, {
      provider: binding.provider,
      model: next,
      temperature: binding.temperature,
      max_tokens: binding.max_tokens ?? undefined,
    })
    config.value = res as ModelConfigView
    message.success(`${binding.role} → ${next}`)
  } catch (err) {
    message.error(`改绑失败：${err instanceof Error ? err.message : String(err)}`)
  } finally {
    savingRole.value = ''
  }
}

const roleModelHint = computed(() => {
  if (fetching.value === roleForm.value.provider) return '正在获取模型…'
  const count = modelsByProvider.value[roleForm.value.provider]?.length ?? 0
  return count > 0 ? `已获取 ${count} 个，点开即可选` : '点右侧「获取模型」拉清单，也可直接手填'
})

function openRole(binding: RoleBinding): void {
  roleForm.value = {
    role: binding.role,
    provider: binding.provider,
    model: binding.model,
    temperature: binding.temperature,
    maxTokens: binding.max_tokens ?? null,
  }
  fetchError.value = ''
  showRole.value = true
  prefetchModels(binding.provider)
}

watch(
  () => roleForm.value.provider,
  (provider) => {
    fetchError.value = ''
    prefetchModels(provider)
  },
)

async function saveRole(): Promise<void> {
  try {
    const res = await api('PUT', `/api/model-config/roles/${roleForm.value.role}`, {
      provider: roleForm.value.provider,
      model: roleForm.value.model,
      temperature: roleForm.value.temperature,
      max_tokens: roleForm.value.maxTokens ?? undefined,
    })
    config.value = res as ModelConfigView
    message.success(`角色 ${roleForm.value.role} 绑定已更新`)
    showRole.value = false
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

const showProvider = ref(false)
const providerEditing = ref(false)
const providerForm = ref({ name: '', type: 'openai_compat', baseUrl: '', apiKey: '', headers: '' })

/** headers 的文本形态：每行 `key: value`（用户能看懂、能粘贴；不做成 JSON 玩具）。 */
function headersToText(headers?: Record<string, string>): string {
  return Object.entries(headers ?? {})
    .map(([k, v]) => `${k}: ${v}`)
    .join('\n')
}

/** 解析请求头文本；返回 [对象, 错误行]（错误行给用户看，不静默吞掉）。 */
function parseHeaders(text: string): [Record<string, string>, string[]] {
  const out: Record<string, string> = {}
  const bad: string[] = []
  for (const rawLine of text.split('\n')) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const idx = line.indexOf(':')
    if (idx <= 0) {
      bad.push(line)
      continue
    }
    const key = line.slice(0, idx).trim()
    const value = line.slice(idx + 1).trim()
    if (!key) bad.push(line)
    else out[key] = value
  }
  return [out, bad]
}

/** 常用头一键填入：OpenCode Go（Zen/Go）网关要求会话头，否则 /chat/completions 400。 */
function fillOpencodeHeaders(): void {
  const session = `inkforge-${Date.now().toString(36)}`
  providerForm.value.headers = headersToText({
    'x-opencode-session': session,
    'User-Agent': 'inkforge/0.1',
  })
}

function openProviderNew(): void {
  providerEditing.value = false
  providerForm.value = { name: '', type: 'openai_compat', baseUrl: '', apiKey: '', headers: '' }
  fetchError.value = ''
  showProvider.value = true
}

function openProviderEdit(info: ProviderInfo): void {
  providerEditing.value = true
  providerForm.value = {
    name: info.name,
    type: info.type,
    baseUrl: info.base_url ?? '',
    apiKey: info.api_key ?? '****',
    headers: headersToText(info.headers),
  }
  fetchError.value = ''
  showProvider.value = true
  prefetchModels(info.name)
}

/** 用弹窗里**还没保存**的接入点定义拉一次模型清单（表单值优先，无需先保存）。 */
function fetchProviderModels(): void {
  const form = providerForm.value
  const name = form.name.trim()
  if (!name || !/^[a-zA-Z0-9_-]+$/.test(name)) {
    message.warning('请先填写合法的接入点名称（字母 / 数字 / 下划线 / 连字符）')
    return
  }
  // 只有**用户真的新填了密钥**才把 api_key 带上。「****」（掩码）与「${ENV_VAR}」
  // （占位符）表达的都是「沿用已存配置」：引擎对占位符不掩码，编辑弹窗会把它原样
  // 回传，若当成字面量 Key 发出去，一个正常的接入点会被判成缺 Key（2026-09-21 实测）。
  const typed = form.apiKey.trim()
  const isCarriedOver = !typed || typed.includes('****') || /^\$\{\w+\}$/.test(typed)
  void fetchModels(name, {
    type: form.type,
    baseUrl: form.baseUrl.trim(),
    ...(isCarriedOver ? {} : { apiKey: typed }),
  })
}

/** 接入点弹窗里的清单回执：确认「这个接入点到底有哪些模型」，并指路去哪选。 */
const providerModelPreview = computed(() => {
  const list = modelsByProvider.value[providerForm.value.name.trim()] ?? []
  if (!list.length) return ''
  const head = list.slice(0, 6).join('、')
  return `${list.length} 个可选：${head}${list.length > 6 ? ' …' : ''}（在上方「角色绑定」行里直接选）`
})

async function saveProvider(): Promise<void> {
  const name = providerForm.value.name.trim()
  if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
    message.warning('接入点名仅限字母 / 数字 / 下划线 / 连字符')
    return
  }
  const [headers, badLines] = parseHeaders(providerForm.value.headers)
  if (badLines.length) {
    message.warning(`请求头格式不对（每行要写 key: value）：${badLines[0]}`)
    return
  }
  try {
    const res = await api('PUT', `/api/model-config/providers/${name}`, {
      type: providerForm.value.type,
      base_url: providerForm.value.baseUrl.trim(),
      api_key: providerForm.value.apiKey.trim() || 'none',
      headers,
    })
    config.value = res as ModelConfigView
    message.success(`接入点 ${name} 已保存${Object.keys(headers).length ? `（含 ${Object.keys(headers).length} 个自定义请求头）` : ''}`)
    showProvider.value = false
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

async function deleteProvider(name: string): Promise<void> {
  try {
    const res = await api('DELETE', `/api/model-config/providers/${name}`)
    config.value = res as ModelConfigView
    message.success(`接入点 ${name} 已删除`)
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
}

watch(show, (opened) => {
  if (opened) void load()
})
</script>

<template>
  <NModal v-model:show="show" preset="card" title="模型配置" class="dw-dialog" style="width: 720px">
    <div class="section-title with-action">
      <span>角色绑定（按 Agent 绑定模型接入点）<span class="muted"> · 每行可直接换模型</span></span>
      <button class="ghost-btn nowrap" :disabled="!!fetching" @click="fetchAllProviders">
        {{ fetching ? '获取中…' : '获取模型' }}
      </button>
    </div>
    <template v-for="grp in roleGroups" :key="grp.group">
      <div class="group-title">{{ grp.group }}</div>
      <div v-for="item in grp.rows" :key="item.binding.role" class="row-item">
        <div class="row-main">
          <span class="role-tag">{{ item.role }}</span>
          <NSelect
            class="row-provider"
            size="tiny"
            :value="item.binding.provider"
            :options="providerOptions"
            :loading="savingRole === item.binding.role"
            :title="`${item.binding.role}：换接入点（换完会拉这个接入点的模型清单并落到一个真实存在的模型）`"
            @update:value="(v: string) => quickSetProvider(item.binding, v)"
          />
          <span class="sep">/</span>
          <NSelect
            class="row-model"
            size="tiny"
            :value="item.binding.model"
            :options="optionsFor(item.binding.provider, item.binding.model)"
            :loading="fetching === item.binding.provider || savingRole === item.binding.role"
            :title="rowTitle(item.binding)"
            filterable
            tag
            @update:value="(v: string) => quickSetModel(item.binding, v)"
          />
          <span class="muted">temp {{ item.binding.temperature }}</span>
        </div>
        <div class="row-actions">
          <span v-if="testResult && testResult.role === item.binding.role" class="test-result" :class="{ ok: testResult.ok }">
            {{ testResult.text }}
          </span>
          <button class="ghost-btn" :disabled="testing === item.binding.role" @click="testRole(item.binding.role)">
            {{ testing === item.binding.role ? '测试中…' : '测试' }}
          </button>
          <button class="ghost-btn" @click="openRole(item.binding)">编辑</button>
        </div>
      </div>
    </template>

    <div class="section-title" style="margin-top: 16px">接入点（providers）</div>
    <div v-for="info in providerEntries" :key="info.name" class="row-item">
      <div class="row-main">
        <span class="role-tag blue">{{ info.name }}</span>
        <span class="binding">
          {{ info.type }}
          <span v-if="info.base_url" class="muted"> · {{ info.base_url }}</span>
          <span v-if="info.headers && Object.keys(info.headers).length" class="muted">
            · 自定义头 {{ Object.keys(info.headers).length }} 个
          </span>
        </span>
      </div>
      <div class="row-actions">
        <button class="ghost-btn" @click="openProviderEdit(info)">编辑</button>
        <button class="del-btn" @click="deleteProvider(info.name)">删除</button>
      </div>
    </div>
    <button class="ghost-btn" style="margin-top: 8px" @click="openProviderNew">＋ 新增接入点</button>
    <div v-if="config" class="muted" style="margin-top: 10px">配置文件：{{ config.path }}</div>

    <!-- 角色编辑 -->
    <NModal v-model:show="showRole" preset="card" :title="`编辑角色绑定 · ${roleForm.role}`" style="width: 460px">
      <NForm label-placement="top" size="small">
        <NFormItem label="接入点">
          <NSelect v-model:value="roleForm.provider" :options="providerOptions" />
        </NFormItem>
        <NFormItem label="模型名">
          <div class="model-row">
            <NSelect
              v-model:value="roleForm.model"
              class="model-select"
              filterable
              tag
              :options="modelOptions"
              :loading="fetching === roleForm.provider"
              :placeholder="roleModelHint"
            />
            <button
              class="ghost-btn nowrap"
              :disabled="fetching === roleForm.provider"
              @click="fetchModels(roleForm.provider)"
            >
              {{ fetching === roleForm.provider ? '获取中…' : '获取模型' }}
            </button>
          </div>
          <div v-if="fetchError" class="fetch-error">{{ fetchError }}</div>
        </NFormItem>
        <NFormItem label="温度">
          <NInputNumber v-model:value="roleForm.temperature" :min="0" :max="2" :step="0.1" />
        </NFormItem>
        <div class="modal-foot">
          <button class="ghost-btn" @click="showRole = false">取消</button>
          <button class="primary-btn" @click="saveRole">保存</button>
        </div>
      </NForm>
    </NModal>

    <!-- 接入点编辑 -->
    <NModal v-model:show="showProvider" preset="card" :title="providerEditing ? '编辑接入点' : '新增接入点'" style="width: 500px">
      <NForm label-placement="top" size="small">
        <NFormItem label="名称（唯一 ID）">
          <NInput v-model:value="providerForm.name" :disabled="providerEditing" placeholder="例：deepseek / local-ollama" />
        </NFormItem>
        <NFormItem label="协议类型">
          <NSelect
            v-model:value="providerForm.type"
            :options="[
              { label: 'OpenAI 兼容', value: 'openai_compat' },
              { label: 'Anthropic Messages', value: 'anthropic' },
            ]"
          />
        </NFormItem>
        <NFormItem label="Base URL">
          <NInput v-model:value="providerForm.baseUrl" placeholder="https://api.deepseek.com/v1" />
        </NFormItem>
        <NFormItem label="API Key（含 **** 表示保持原值）">
          <NInput
            v-model:value="providerForm.apiKey"
            type="password"
            show-password-on="click"
            :placeholder="providerEditing ? '保持原值可留 ****' : 'sk-…'"
          />
        </NFormItem>
        <NFormItem label="自定义请求头（每行 key: value，可留空）">
          <NInput
            v-model:value="providerForm.headers"
            class="headers-input"
            type="textarea"
            :rows="3"
            placeholder="x-opencode-session: my-session-01"
          />
        </NFormItem>
        <div class="hint-row">
          <span class="muted">
            有些网关要求密钥之外的头：OpenCode Go 不带 x-opencode-session 时，
            模型列表拿得到、聊天接口却报 400。
          </span>
          <button class="ghost-btn nowrap" @click="fillOpencodeHeaders">填入 OpenCode 常用头</button>
        </div>
        <div v-if="fetchError" class="fetch-error">{{ fetchError }}</div>
        <div v-else-if="providerModelPreview" class="fetch-hint">{{ providerModelPreview }}</div>
        <div class="modal-foot">
          <button
            class="ghost-btn foot-left nowrap"
            :disabled="fetching === providerForm.name.trim()"
            @click="fetchProviderModels"
          >
            {{ fetching === providerForm.name.trim() ? '获取中…' : '获取模型' }}
          </button>
          <button class="ghost-btn" @click="showProvider = false">取消</button>
          <button class="primary-btn" @click="saveProvider">保存</button>
        </div>
      </NForm>
    </NModal>
  </NModal>
</template>

<style scoped>
.section-title {
  font-weight: 600;
  font-size: 13.5px;
  margin: 4px 0 8px;
}
/* 带操作按钮的小节标题：「获取模型」靠右，与角色行下拉形成呼应 */
.section-title.with-action {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.group-title {
  font-size: 11.5px;
  font-weight: 600;
  color: #9aa0aa;
  letter-spacing: 0.5px;
  margin: 12px 0 4px;
}
.row-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 2px;
  border-bottom: 1px solid #f3f4f6;
}
.row-main {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex-wrap: wrap;
  row-gap: 4px;
}
.role-tag {
  font-size: 11.5px;
  background: #f3f4f6;
  border-radius: 999px;
  padding: 2px 10px;
  color: #3a3d44;
  white-space: nowrap;
}
.role-tag.blue {
  background: #e8f0fe;
  color: #1d4ed8;
}
.binding {
  font-size: 13px;
  color: #3a3d44;
  white-space: nowrap;
}
/* 角色行内的模型下拉：给足宽度但不许把「测试/编辑」挤出去 */
.row-model {
  width: 200px;
  min-width: 140px;
  flex-shrink: 1;
}
/* 角色行内的接入点下拉：换接入点与换模型一样，都是一步到位的行内操作 */
.row-provider {
  width: 132px;
  min-width: 96px;
  flex-shrink: 1;
}
.sep {
  color: #c3c7ce;
  font-size: 12px;
}
.row-actions {
  display: flex;
  gap: 6px;
}
.ghost-btn {
  background: none;
  border: 1px solid #e5e7eb;
  border-radius: 7px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
  color: #3a3d44;
}
.test-result {
  font-size: 12px;
  color: #b42318;
}
.test-result.ok {
  color: #116932;
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
  font-size: 13px;
  cursor: pointer;
}
.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
/* 「获取模型」靠左，与右对齐的「取消 / 保存」拉开，避免误点 */
.foot-left {
  margin-right: auto;
}
.model-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}
.model-select {
  flex: 1;
  min-width: 0;
}
.nowrap {
  white-space: nowrap;
  flex-shrink: 0;
}
.fetch-error {
  font-size: 12px;
  color: #b42318;
  margin-top: 6px;
  word-break: break-all;
}
.fetch-hint {
  font-size: 12px;
  color: #6b7280;
  margin-top: 6px;
  word-break: break-all;
}
.hint-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
  margin-top: -4px;
}
.hint-row .muted {
  line-height: 1.5;
}
</style>
