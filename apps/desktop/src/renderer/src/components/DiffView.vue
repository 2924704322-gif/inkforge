<script setup lang="ts">
import { diffLines } from 'diff'
import { computed } from 'vue'

const props = defineProps<{
  base: string
  target: string
}>()

interface DiffPart {
  type: 'added' | 'removed' | 'same'
  value: string
}

const parts = computed<DiffPart[]>(() => {
  return diffLines(props.base ?? '', props.target ?? '').map((p) => ({
    type: p.added ? 'added' : p.removed ? 'removed' : 'same',
    value: p.value,
  }))
})
</script>

<template>
  <div class="diff-box">
    <span
      v-for="(part, i) in parts"
      :key="i"
      :class="{ 'diff-add': part.type === 'added', 'diff-del': part.type === 'removed' }"
      >{{ part.value }}</span
    >
  </div>
</template>
