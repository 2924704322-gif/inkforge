<script setup lang="ts">
import { NForm, NFormItem, NInput, NInputNumber, NModal, NSelect, useMessage } from 'naive-ui'
import { computed, ref, watch } from 'vue'

import { api } from '../api'
import type { ModelConfigView, ProviderInfo, RoleBinding } from '../types'

const show = defineModel<boolean>('show', { default: false })
const message = useMessage()

const config = ref<ModelConfigView | null>(null)

async function load(): Promise<void> {
  try {
    config.value = await api<ModelConfigView>('GET', '/api/model-config')
  } catch (err) {
    message.error(err instanceof Error ? err.message : String(err))
  }
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

function openRole(binding: RoleBinding): void {
  roleForm.value = {
    role: binding.role,
    provider: binding.provider,
    model: binding.model,
    temperature: binding.temperature,
    maxTokens: binding.max_tokens ?? null,
  }
  showRole.value = true
}

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
const providerForm = ref({ name: '', type: 'openai_compat', baseUrl: '', apiKey: '' })

function openProviderNew(): void {
  providerEditing.value = false
  providerForm.value = { name: '', type: 'openai_compat', baseUrl: '', apiKey: '' }
  showProvider.value = true
}

function openProviderEdit(info: ProviderInfo): void {
  providerEditing.value = true
  providerForm.value = { name: info.name, type: info.type, baseUrl: info.base_url ?? '', apiKey: info.api_key ?? '****' }
  showProvider.value = true
}

async function saveProvider(): Promise<void> {
  const name = providerForm.value.name.trim()
  if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
    message.warning('接入点名仅限字母 / 数字 / 下划线 / 连字符')
    return
  }
  try {
    const res = await api('PUT', `/api/model-config/providers/${name}`, {
      type: providerForm.value.type,
      base_url: providerForm.value.baseUrl.trim(),
      api_key: providerForm.value.apiKey.trim() || 'none',
    })
    config.value = res as ModelConfigView
    message.success(`接入点 ${name} 已保存`)
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
    <div class="section-title">角色绑定（按 Agent 绑定模型接入点）</div>
    <template v-for="grp in roleGroups" :key="grp.group">
      <div class="group-title">{{ grp.group }}</div>
      <div v-for="item in grp.rows" :key="item.binding.role" class="row-item">
        <div class="row-main">
          <span class="role-tag">{{ item.role }}</span>
          <span class="binding">
            {{ item.binding.provider }} / <b>{{ item.binding.model }}</b>
            <span class="muted"> · temp {{ item.binding.temperature }}</span>
          </span>
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
          <NInput v-model:value="roleForm.model" placeholder="例：deepseek-chat" />
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
        <div class="modal-foot">
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
</style>
