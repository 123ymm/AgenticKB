<template>
  <el-table :data="rows" size="small" max-height="460" row-key="row_id" border>
    <el-table-column v-for="column in columns" :key="column.key" :label="column.name" min-width="150">
      <template #default="{ row }"><button class="cell" @click="show(row, column)">{{ text(row[column.key]) || '—' }}</button></template>
    </el-table-column>
    <template #empty>暂无产物</template>
  </el-table>
  <el-dialog v-model="visible" :title="selectedColumn?.name" width="70%" append-to-body>
    <el-input v-model="value" type="textarea" :rows="14" :readonly="!editable" />
    <el-collapse><el-collapse-item title="来源与任务追踪"><pre>{{ JSON.stringify(selectedRow?.provenance, null, 2) }}
{{ JSON.stringify(selectedRow?._llm, null, 2) }}</pre></el-collapse-item></el-collapse>
    <template #footer><el-button @click="visible = false">关闭</el-button><el-button v-if="editable" type="primary" @click="save">生成调整步骤</el-button></template>
  </el-dialog>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import type { PipelineColumn, PipelineRow } from '@/api/artifactPipeline'
defineProps<{ rows: PipelineRow[]; columns: PipelineColumn[]; editable?: boolean }>()
const emit = defineEmits<{ edit: [change: { row_id: string; column: string; value: string }] }>()
const visible = ref(false), value = ref('')
const selectedColumn = ref<PipelineColumn>(), selectedRow = ref<PipelineRow>()
function text(v: unknown) { return v === null || v === undefined ? '' : typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v) }
function show(row: PipelineRow, column: PipelineColumn) { selectedRow.value = row; selectedColumn.value = column; value.value = text(row[column.key]); visible.value = true }
function save() { if (selectedRow.value && selectedColumn.value) emit('edit', { row_id: selectedRow.value.row_id, column: selectedColumn.value.key, value: value.value }); visible.value = false }
</script>
<style scoped>
.cell { border: 0; background: transparent; color: inherit; cursor: pointer; text-align: left; width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
