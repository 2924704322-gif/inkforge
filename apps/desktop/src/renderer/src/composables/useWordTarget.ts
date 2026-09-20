import { computed, ref, watch, type ComputedRef, type Ref } from 'vue'

/**
 * 「预期字数」编辑态（生成前可设 / 打回时可改）。
 *
 * 由来（用户实测）：手改预期字数后，**鼠标点一下输入框外面**数字就弹回默认的 3000。
 * 根因是"编辑态被服务端快照反复覆盖"：
 *   · 互动创作的状态每 2-5 秒轮询一次，每次返回**新对象**，而 watch 的取值表达式读了
 *     `state.draft…` / `state.target_words`，于是每次轮询都被判成"值变了" → 重新赋默认值；
 *   · 自由创作的审阅卡用 `:key` 绑定 chapter+attempt 强制重挂载（换稿要重置 diff 等），
 *     重挂载会把 ref 初始化回默认值。
 *
 * 修法：显示值不再由"服务端快照"驱动，而由"**用户是否改过**"驱动：
 *   用户改过 → 一直用用户的数（快照怎么刷都不动）；
 *   用户没改过 → 跟随服务端（新值到来时只在"当前值仍是旧的服务端值"时才更新，
 *   避免把用户刚清空的输入框又填满）。
 *
 * `resetWhen`：换章（生成前的设定）或换稿（打回后的设定）时清空手改，
 * 重新跟随服务端——否则第 1 章设的 5000 会悄悄带到第 2 章。
 */

export interface WordTargetOptions {
  /** 服务端**建议**值（大纲预算 / 引擎默认 / 上一稿目标），未改动时显示它。 */
  suggested: Ref<number | null | undefined> | ComputedRef<number | null | undefined>
  /** 服务端**当前生效**值（互动快照的 state.target_words；没有就传 null）。 */
  effective?: Ref<number | null | undefined> | ComputedRef<number | null | undefined>
  /** 这个键一变 → 清空手改，重新跟随服务端。 */
  resetWhen?: Ref<string | number> | ComputedRef<string | number>
  min?: number
  max?: number
}

export interface WordTarget {
  /** 输入框绑定的显示值。 */
  display: Ref<number | null>
  /** 提交值：null → undefined（请求体省略该键，引擎回落默认）。 */
  payload: ComputedRef<number | undefined>
  /** 用户是否手动改过（改过才显示「恢复默认」）。 */
  dirty: Ref<boolean>
  /** 服务端当前生效值（只读展示用）。 */
  serverTarget: ComputedRef<number | null>
  /** 服务端建议值（只读展示用）。 */
  suggestion: ComputedRef<number | null>
  /** 恢复为服务端建议值。 */
  reset: () => void
}

function _norm(v: number | null | undefined): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? Math.round(v) : null
}

export function useWordTarget(opts: WordTargetOptions): WordTarget {
  const min = opts.min ?? 500
  const max = opts.max ?? 20000

  const suggestion = computed(() => _norm(opts.suggested.value))
  const effective = computed(() => _norm(opts.effective?.value))

  const serverTarget = computed(
    () => effective.value ?? suggestion.value,
  )

  const display = ref<number | null>(serverTarget.value)
  const dirty = ref(false)

  // dirty → 用户改过，快照不再覆盖显示值
  watch(dirty, (isDirty) => {
    if (!isDirty) display.value = serverTarget.value
  })

  // 服务端建议值变化：只更新那些"仍显示着服务端值"的输入框（用户清空的输入框不填回来）
  watch(suggestion, (next) => {
    if (dirty.value) return
    if (display.value === null) return
    display.value = next
  })

  // 服务端生效值变化（本章目标落了盘）：用户没改过就跟随
  watch(effective, (next) => {
    if (dirty.value || typeof next !== 'number' || next <= 0) return
    display.value = _norm(next)
  })

  if (opts.resetWhen) {
    watch(opts.resetWhen, () => {
      dirty.value = false
      display.value = serverTarget.value
    })
  }

  function reset(): void {
    dirty.value = false
    display.value = serverTarget.value
  }

  // 直接在输入框上 v-model 时，靠这个 watch 记录"用户改过"
  watch(display, (v) => {
    if (v === null || v === undefined) return
    const n = Math.min(Math.max(_norm(v) ?? min, min), max)
    if (n !== v) display.value = n
    if (n !== serverTarget.value) dirty.value = true
  })

  return {
    display,
    payload: computed(() => display.value ?? undefined),
    dirty,
    serverTarget,
    suggestion,
    reset,
  }
}
