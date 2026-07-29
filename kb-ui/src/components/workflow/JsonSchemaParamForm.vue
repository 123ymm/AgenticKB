<template>
  <div class="param-form">
    <div v-if="fields.length === 0" class="param-form__empty">该算子无可配置参数</div>
    <div v-for="field in fields" :key="field.key" class="param-form__field">
      <label class="param-form__label">{{ field.title }}</label>
      <span v-if="field.description" class="param-form__description">{{ field.description }}</span>

      <el-select
        v-if="field.kind === 'enum'"
        :model-value="value(field.key)"
        size="small"
        style="width: 100%"
        @update:model-value="set(field.key, $event)"
      >
        <el-option v-for="option in field.enum" :key="String(option)" :label="String(option)" :value="option" />
      </el-select>

      <el-input-number
        v-else-if="field.kind === 'number'"
        :model-value="value(field.key) as number"
        :min="field.min"
        :max="field.max"
        :step="field.step"
        size="small"
        controls-position="right"
        style="width: 100%"
        @update:model-value="set(field.key, $event)"
      />

      <el-switch
        v-else-if="field.kind === 'boolean'"
        :model-value="value(field.key) as boolean"
        @update:model-value="set(field.key, $event)"
      />

      <el-select
        v-else-if="field.kind === 'array'"
        :model-value="(value(field.key) as unknown[]) ?? []"
        multiple
        filterable
        allow-create
        default-first-option
        size="small"
        placeholder="输入后回车添加"
        style="width: 100%"
        @update:model-value="set(field.key, $event)"
      />

      <div v-else-if="field.kind === 'map'" class="param-form__map">
        <div v-for="(row, index) in mapRows(field.key)" :key="index" class="param-form__map-row">
          <el-input
            :model-value="row.key"
            size="small"
            placeholder="key"
            @update:model-value="setMapKey(field.key, index, $event)"
          />
          <el-input-number
            :model-value="row.value"
            size="small"
            :step="0.1"
            controls-position="right"
            @update:model-value="setMapValue(field.key, index, $event)"
          />
          <el-button size="small" text @click="removeMapRow(field.key, index)">×</el-button>
        </div>
        <el-button size="small" text type="primary" @click="addMapRow(field.key)">+ 添加</el-button>
      </div>

      <el-input
        v-else
        :model-value="value(field.key) as string"
        size="small"
        @update:model-value="set(field.key, $event)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive } from 'vue'
import type { MiningJsonSchema, MiningJsonSchemaProperty } from '@/types/miningWorkflow'

const props = defineProps<{
  schemaJson: string | MiningJsonSchema
  modelValue: Record<string, unknown>
}>()
const emit = defineEmits<{ 'update:modelValue': [Record<string, unknown>] }>()

interface Field {
  key: string
  title: string
  description?: string
  kind: 'enum' | 'number' | 'boolean' | 'array' | 'map' | 'string'
  enum?: unknown[]
  min?: number
  max?: number
  step?: number
  default?: unknown
}

const schema = computed<MiningJsonSchema>(() => {
  if (typeof props.schemaJson !== 'string') return props.schemaJson ?? {}
  try {
    return JSON.parse(props.schemaJson || '{}') as MiningJsonSchema
  } catch {
    return {}
  }
})

const fields = computed<Field[]>(() => Object.entries(schema.value.properties ?? {})
  .map(([key, property]) => toField(key, property)))

function toField(key: string, property: MiningJsonSchemaProperty): Field {
  const title = property.title || key
  const base = { key, title, description: property.description, default: property.default }
  if (property.enum) return { ...base, kind: 'enum', enum: property.enum }
  if (property.type === 'integer' || property.type === 'number') {
    return {
      ...base,
      kind: 'number',
      min: property.minimum,
      max: property.maximum,
      step: property.type === 'integer' ? 1 : 0.1,
    }
  }
  if (property.type === 'boolean') return { ...base, kind: 'boolean' }
  if (property.type === 'array') return { ...base, kind: 'array' }
  if (property.type === 'object') return { ...base, kind: 'map' }
  return { ...base, kind: 'string' }
}

function value(key: string): unknown {
  const current = props.modelValue?.[key]
  if (current !== undefined) return current
  return fields.value.find(field => field.key === key)?.default
}

function set(key: string, nextValue: unknown) {
  const next = { ...props.modelValue }
  if (nextValue === null || nextValue === undefined) delete next[key]
  else next[key] = nextValue
  emit('update:modelValue', next)
}

interface MapRow { key: string; value: number }
const mapState = reactive<Record<string, MapRow[]>>({})

function mapRows(key: string): MapRow[] {
  if (!mapState[key]) {
    const current = (props.modelValue?.[key] as Record<string, number>) ?? {}
    mapState[key] = Object.entries(current).map(([rowKey, rowValue]) => ({ key: rowKey, value: rowValue }))
  }
  return mapState[key]
}

function emitMap(key: string) {
  const next: Record<string, number> = {}
  for (const row of mapRows(key)) if (row.key) next[row.key] = row.value
  set(key, next)
}

function setMapKey(key: string, index: number, rowKey: string) {
  mapRows(key)[index].key = rowKey
  emitMap(key)
}

function setMapValue(key: string, index: number, rowValue: number | undefined) {
  mapRows(key)[index].value = rowValue ?? 0
  emitMap(key)
}

function addMapRow(key: string) {
  mapRows(key).push({ key: '', value: 1 })
  emitMap(key)
}

function removeMapRow(key: string, index: number) {
  mapRows(key).splice(index, 1)
  emitMap(key)
}
</script>

<style scoped>
.param-form { display: flex; flex-direction: column; gap: 14px; }
.param-form__empty { color: var(--kb-text-tertiary); font-size: 13px; padding: 8px 0; }
.param-form__field { display: flex; flex-direction: column; gap: 6px; }
.param-form__label { font-size: 12px; font-weight: 600; color: var(--kb-text-secondary); }
.param-form__description { font-size: 11px; color: var(--kb-text-tertiary); line-height: 1.4; }
.param-form__map { display: flex; flex-direction: column; gap: 6px; }
.param-form__map-row { display: grid; grid-template-columns: 1fr 110px 28px; gap: 6px; align-items: center; }
</style>

