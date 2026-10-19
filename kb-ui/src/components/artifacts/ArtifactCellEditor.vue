<template>
  <!-- 按列类型分派控件（008 UI 批次）：
       text → 自适应多行；int/float → 数字输入；bool → 三态下拉；
       date → 日期选择器；enum → 枚举下拉。空值=三态可清空。 -->
  <el-select
    v-if="column.column_type === 'enum'"
    :model-value="normalized"
    clearable
    filterable
    allow-create
    placeholder="（空）"
    class="cell-editor__full"
    :data-test="`cell-editor-enum-${column.column_name}`"
    @update:model-value="emit('update:modelValue', $event ?? null)"
  >
    <el-option v-for="v in column.enum_values ?? []" :key="String(v)" :value="v" :label="String(v)" />
  </el-select>

  <el-select
    v-else-if="column.column_type === 'bool'"
    :model-value="normalized === null ? undefined : normalized"
    clearable
    placeholder="（空）"
    class="cell-editor__bool"
    :data-test="`cell-editor-bool-${column.column_name}`"
    @update:model-value="emit('update:modelValue', $event ?? null)"
  >
    <el-option :value="true" label="是（true）" />
    <el-option :value="false" label="否（false）" />
  </el-select>

  <el-date-picker
    v-else-if="column.column_type === 'date'"
    :model-value="normalized === null ? undefined : String(normalized)"
    type="date"
    value-format="YYYY-MM-DD"
    placeholder="（空）"
    class="cell-editor__full"
    :data-test="`cell-editor-date-${column.column_name}`"
    @update:model-value="emit('update:modelValue', $event ?? null)"
  />

  <el-input-number
    v-else-if="column.column_type === 'int' || column.column_type === 'float'"
    :model-value="numericValue"
    :precision="column.column_type === 'int' ? 0 : undefined"
    :controls="false"
    placeholder="（空）"
    class="cell-editor__full"
    :data-test="`cell-editor-num-${column.column_name}`"
    @update:model-value="emit('update:modelValue', $event ?? null)"
  />

  <el-input
    v-else
    :model-value="normalized === null ? '' : String(normalized)"
    type="textarea"
    :autosize="{ minRows: 1, maxRows: 5 }"
    placeholder="（空）"
    :data-test="`cell-editor-text-${column.column_name}`"
    @update:model-value="emit('update:modelValue', $event === '' ? null : $event)"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ArtifactColumn } from '@/types/artifacts'

const props = defineProps<{
  column: ArtifactColumn
  modelValue: unknown
}>()

const emit = defineEmits<{ 'update:modelValue': [value: unknown] }>()

const normalized = computed<unknown>(() =>
  props.modelValue === undefined ? null : props.modelValue)

const numericValue = computed<number | undefined>(() => {
  if (props.modelValue === null || props.modelValue === undefined) return undefined
  const num = Number(props.modelValue)
  return Number.isFinite(num) ? num : undefined
})
</script>

<style scoped>
.cell-editor__full { width: 100%; }
.cell-editor__bool { width: 140px; }
</style>
