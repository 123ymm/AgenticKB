<template>
  <div class="rows-panel">
    <div class="rows-panel__bar">
      <div class="rows-panel__bar-left">
        <el-switch v-model="draft" data-test="rows-draft" @change="onDraftChange" />
        <span class="rows-panel__bar-label">草稿视图（draft）</span>
        <span v-if="draft && !draftOpen" class="rows-panel__hint">
          当前与已发布版本一致；在此编辑将自动检出草稿
        </span>
      </div>
      <span class="rows-panel__count">共 {{ total }} 行</span>
      <div v-if="canWrite" class="rows-panel__bar-actions">
        <el-button size="small" data-test="rows-batch-assign" @click="openAssign">批量赋值</el-button>
        <el-button size="small" data-test="rows-add" @click="openAdd">新增行</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="rows" size="small" row-key="row_key">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="rows-panel__expanded">
            <el-descriptions v-if="provenanceOf(row).length" :column="2" size="small" border>
              <el-descriptions-item
                v-for="(p, i) in provenanceOf(row)"
                :key="i"
                :label="`来源 ${i + 1}`"
              >{{ describeProvenance(p) }}</el-descriptions-item>
            </el-descriptions>
            <p v-else class="rows-panel__hint">无来源记录（手工编辑或来源未记账）</p>
            <el-tag v-if="hasConflicts(row)" type="danger" size="small">
              冲突标记：{{ conflictKeys(row).join('、') }}
            </el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="row_key" label="行键" min-width="160" show-overflow-tooltip fixed="left" />
      <el-table-column
        v-for="col in columns"
        :key="col.column_id"
        :label="col.column_name"
        min-width="130"
        show-overflow-tooltip
      >
        <template #default="{ row }">
          <el-tag
            v-if="isEnumColumn(col) && row.data?.[col.column_name] !== null && row.data?.[col.column_name] !== undefined"
            size="small"
            effect="plain"
          >{{ String(row.data?.[col.column_name]) }}</el-tag>
          <span v-else>{{ cellText(row.data?.[col.column_name]) }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="canWrite" label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" data-test="rows-edit" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" text type="danger" @click="removeRow(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <EmptyState :text="draft ? '草稿无数据行（上传 Excel 或等待导入）' : '已发布版本无数据行'" />
      </template>
    </el-table>

    <el-pagination
      v-if="total > pageSize"
      class="rows-panel__pager"
      layout="total, prev, pager, next"
      :total="total"
      :page-size="pageSize"
      :current-page="page"
      @current-change="onPageChange"
    />

    <!-- 编辑行：逐列表单（enum 列=enum_values 下拉） -->
    <el-dialog v-model="editVisible" title="编辑行" width="560px">
      <el-form label-width="120px">
        <el-form-item label="行键">
          <span>{{ editForm.rowKey }}</span>
        </el-form-item>
        <el-form-item v-for="col in editableColumns" :key="col.column_id" :label="col.column_name">
          <el-select
            v-if="isEnumColumn(col)"
            v-model="editForm.values[col.column_name]"
            clearable
            placeholder="（空）"
          >
            <el-option v-for="v in col.enum_values ?? []" :key="String(v)" :value="v" :label="String(v)" />
          </el-select>
          <el-input v-else :model-value="inputValue(editForm.values[col.column_name])" @update:model-value="editForm.values[col.column_name] = $event" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-test="rows-edit-submit" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 批量赋值：目标列 + 值 + 条件行（可多行） -->
    <el-dialog v-model="assignVisible" title="批量赋值" width="620px">
      <el-form label-width="90px">
        <el-form-item label="目标列">
          <el-select v-model="assignForm.column" data-test="assign-column">
            <el-option
              v-for="col in editableColumns"
              :key="col.column_id"
              :value="col.column_name"
              :label="col.column_name"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="赋值">
          <el-input v-model="assignForm.value" style="max-width: 260px" data-test="assign-value" />
        </el-form-item>
        <el-form-item label="条件（可选）">
          <div class="rows-panel__conditions">
            <div v-for="(cond, i) in assignForm.conditions" :key="i" class="rows-panel__cond">
              <el-select v-model="cond.column" placeholder="列" style="width: 140px">
                <el-option
                  v-for="col in editableColumns"
                  :key="col.column_id"
                  :value="col.column_name"
                  :label="col.column_name"
                />
              </el-select>
              <el-select v-model="cond.op" style="width: 120px" data-test="assign-op">
                <el-option v-for="op in WHERE_OPS" :key="op.value" :value="op.value" :label="op.label" />
              </el-select>
              <el-input
                v-if="cond.op !== 'is_null' && cond.op !== 'not_null'"
                v-model="cond.value"
                placeholder="值（in 用逗号分隔）"
                style="flex: 1"
              />
              <el-button size="small" text type="danger" @click="assignForm.conditions.splice(i, 1)">删</el-button>
            </div>
            <el-button size="small" data-test="assign-add-cond" @click="addCondition">+ 条件</el-button>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assignVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-test="assign-submit" @click="submitAssign">执行</el-button>
      </template>
    </el-dialog>

    <!-- 新增行：按列生成表单（后端行新增端点 61号 MVP 未开放——提交时引导走导入） -->
    <el-dialog v-model="addVisible" title="新增行" width="560px">
      <el-alert
        type="info"
        :closable="false"
        title="行新增后端端点暂未开放（61号 MVP 边界）"
        description="追加行请走上方「Excel 导入向导」（追加模式，主键 UPSERT + 来源记账）。"
      />
      <el-form label-width="120px" class="rows-panel__add-form">
        <el-form-item v-for="col in editableColumns" :key="col.column_id" :label="col.column_name">
          <el-select v-if="isEnumColumn(col)" v-model="addForm[col.column_name]" clearable placeholder="（空）">
            <el-option v-for="v in col.enum_values ?? []" :key="String(v)" :value="v" :label="String(v)" />
          </el-select>
          <el-input v-else v-model="addForm[col.column_name]" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" @click="submitAdd">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import EmptyState from '@/components/common/EmptyState.vue'
import type { ArtifactColumn, ArtifactRow } from '@/types/artifacts'

const WHERE_OPS = [
  { value: 'eq', label: '等于' },
  { value: 'ne', label: '不等于' },
  { value: 'in', label: '在集合' },
  { value: 'contains', label: '包含' },
  { value: 'is_null', label: '为空' },
  { value: 'not_null', label: '非空' },
] as const

interface WhereCondition {
  column: string
  op: string
  value: string
}

const props = defineProps<{
  artifactId: string
  canWrite: boolean
  columns: ArtifactColumn[]
  /** 乐观锁令牌（artifact.updated_at）——编辑/删除/批量赋值随请求携带。 */
  expectedUpdatedAt?: string
  draftOpen?: boolean
}>()

const emit = defineEmits<{ changed: [] }>()

const api = useArtifactsApi()

const draft = ref(true)
const rows = ref<ArtifactRow[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 50
const loading = ref(false)
const saving = ref(false)

const editVisible = ref(false)
const editingRow = ref<ArtifactRow | null>(null)
const editForm = ref<{ rowKey: string; values: Record<string, unknown> }>({ rowKey: '', values: {} })

const assignVisible = ref(false)
const assignForm = ref<{ column: string; value: string; conditions: WhereCondition[] }>({
  column: '', value: '', conditions: [],
})

const addVisible = ref(false)
const addForm = ref<Record<string, unknown>>({})

/** 可编辑列 = active 列（dropped 不进表单）。 */
const editableColumns = computed(() => props.columns.filter(c => c.status === 'active'))

onMounted(loadRows)

async function loadRows(): Promise<void> {
  loading.value = true
  try {
    const result = await api.listRows(props.artifactId, {
      draft: draft.value, page: page.value, size: pageSize,
    })
    rows.value = result.rows
    total.value = result.total
  } catch (error) {
    rows.value = []
    total.value = 0
    ElMessage.error(`加载行数据失败：${await apiErrorDetail(error)}`)
  } finally {
    loading.value = false
  }
}

function onDraftChange(): void {
  page.value = 1
  void loadRows()
}

function onPageChange(next: number): void {
  page.value = next
  void loadRows()
}

function isEnumColumn(col: ArtifactColumn): boolean {
  return col.column_type === 'enum'
}

function cellText(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function inputValue(value: unknown): string {
  return value === null || value === undefined ? '' : String(value)
}

/** provenance 后端记账为数组（[{batch_id, file_name, sheet_name, row_index}]）。 */
function provenanceOf(row: ArtifactRow): Array<Record<string, unknown>> {
  return Array.isArray(row.provenance) ? (row.provenance as Array<Record<string, unknown>>) : []
}

function describeProvenance(p: Record<string, unknown>): string {
  return `${p.file_name ?? '?'} / ${p.sheet_name ?? '?'} 第 ${p.row_index ?? '?'} 行（批次 ${String(p.batch_id ?? '?').slice(0, 12)}）`
}

function hasConflicts(row: ArtifactRow): boolean {
  return !!row.conflicts && Object.keys(row.conflicts).length > 0
}

function conflictKeys(row: ArtifactRow): string[] {
  return row.conflicts ? Object.keys(row.conflicts) : []
}

/** 409 stale_artifact → 提示刷新（emit changed 触发父级 reload 刷新乐观锁令牌）。 */
function isStale(error: unknown): boolean {
  const resp = (error as { response?: { status?: number; data?: { detail?: { code?: string } } } })
    ?.response
  return resp?.status === 409 && resp.data?.detail?.code === 'stale_artifact'
}

function openEdit(row: ArtifactRow): void {
  editingRow.value = row
  const values: Record<string, unknown> = {}
  editableColumns.value.forEach(col => { values[col.column_name] = row.data[col.column_name] ?? null })
  editForm.value = { rowKey: row.row_key, values }
  editVisible.value = true
}

/** 按列类型把表单字符串归一为 jsonb 值（空串→null）。 */
function coerceValue(columnName: string, raw: unknown): unknown {
  if (raw === null || raw === undefined) return null
  if (typeof raw !== 'string') return raw
  const trimmed = raw.trim()
  if (!trimmed) return null
  const col = editableColumns.value.find(c => c.column_name === columnName)
  const type = col?.column_type
  if (type === 'int' || type === 'float') {
    const num = Number(trimmed)
    return Number.isFinite(num) ? num : trimmed
  }
  if (type === 'bool') {
    if (trimmed.toLowerCase() === 'true') return true
    if (trimmed.toLowerCase() === 'false') return false
  }
  return trimmed
}

async function submitEdit(): Promise<void> {
  const original = editingRow.value
  if (!original) return
  const changes = editableColumns.value
    .map(col => ({
      column: col.column_name,
      value: coerceValue(col.column_name, editForm.value.values[col.column_name]),
    }))
    .filter(({ column, value }) => !deepEqual(value, original.data[column] ?? null))
  if (changes.length === 0) {
    editVisible.value = false
    return
  }
  saving.value = true
  try {
    // 乐观锁令牌只在首格携带：后端每格 touch updated_at，同令牌第二格必 409。
    // 首格校验通过即代表动作起点不 stale；后续格省略令牌（后端跳过校验）。
    for (const [index, change] of changes.entries()) {
      await api.updateCell(props.artifactId, original.row_key, {
        ...change,
        ...(index === 0 ? { expected_updated_at: props.expectedUpdatedAt } : {}),
      })
    }
    ElMessage.success(`已更新 ${changes.length} 格`)
    editVisible.value = false
    emit('changed')
    await loadRows()
  } catch (error) {
    if (isStale(error)) {
      ElMessage.error('制品已被他人修改（409 stale_artifact），已为你刷新——请重试编辑')
      emit('changed')
    } else {
      ElMessage.error(`保存失败：${await apiErrorDetail(error)}`)
    }
  } finally {
    saving.value = false
  }
}

async function removeRow(row: ArtifactRow): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除行「${row.row_key}」？（草稿态删除，发布前可丢弃草稿找回）`, '删除行', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api.deleteRow(props.artifactId, row.row_key, props.expectedUpdatedAt)
    ElMessage.success('已删除')
    emit('changed')
    await loadRows()
  } catch (error) {
    if (isStale(error)) {
      ElMessage.error('制品已被他人修改（409 stale_artifact），已为你刷新——请重试删除')
      emit('changed')
    } else {
      ElMessage.error(`删除失败：${await apiErrorDetail(error)}`)
    }
  }
}

function openAssign(): void {
  assignForm.value = { column: '', value: '', conditions: [] }
  assignVisible.value = true
}

function addCondition(): void {
  assignForm.value.conditions.push({ column: '', op: 'eq', value: '' })
}

/** 组装 batch-assign 请求体：is_null/not_null 无值；in 拆数组；值按目标列类型归一。 */
function buildAssignPayload(): {
  column: string
  value: unknown
  where: Array<{ column: string; op: string; value?: unknown }>
} {
  const where = assignForm.value.conditions
    .filter(cond => cond.column)
    .map(cond => {
      if (cond.op === 'is_null' || cond.op === 'not_null') {
        return { column: cond.column, op: cond.op }
      }
      const raw = cond.value.trim()
      if (cond.op === 'in') {
        return {
          column: cond.column, op: cond.op,
          value: raw ? raw.split(/[，,]/).map(v => coerceValue(cond.column, v.trim())) : [],
        }
      }
      return { column: cond.column, op: cond.op, value: coerceValue(cond.column, raw) }
    })
  return { column: assignForm.value.column, value: coerceValue(assignForm.value.column, assignForm.value.value), where }
}

async function submitAssign(): Promise<void> {
  if (!assignForm.value.column) {
    ElMessage.warning('请选择目标列')
    return
  }
  saving.value = true
  try {
    const { updated } = await api.batchAssign(props.artifactId, {
      ...buildAssignPayload(), expected_updated_at: props.expectedUpdatedAt,
    })
    ElMessage.success(`已批量更新 ${updated} 行`)
    assignVisible.value = false
    emit('changed')
    await loadRows()
  } catch (error) {
    if (isStale(error)) {
      ElMessage.error('制品已被他人修改（409 stale_artifact），已为你刷新——请重试')
      emit('changed')
    } else {
      ElMessage.error(`批量赋值失败：${await apiErrorDetail(error)}`)
    }
  } finally {
    saving.value = false
  }
}

function openAdd(): void {
  const form: Record<string, unknown> = {}
  editableColumns.value.forEach(col => { form[col.column_name] = null })
  addForm.value = form
  addVisible.value = true
}

function submitAdd(): void {
  ElMessage.warning('行新增后端端点暂未开放——追加行请使用「Excel 导入向导」（追加模式）')
  addVisible.value = false
}

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

defineExpose({
  loadRows, openEdit, submitEdit, removeRow, openAssign, submitAssign,
  buildAssignPayload, onDraftChange,
})
</script>

<style scoped>
.rows-panel { display: flex; flex-direction: column; gap: 10px; }
.rows-panel__bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.rows-panel__bar-left { display: flex; align-items: center; gap: 8px; flex: 1; }
.rows-panel__bar-label { font-size: 13px; }
.rows-panel__hint { font-size: 12px; color: var(--el-color-warning, #e6a23c); }
.rows-panel__count { font-size: 12.5px; color: var(--kb-text-tertiary); }
.rows-panel__expanded { padding: 8px 16px; display: flex; flex-direction: column; gap: 8px; }
.rows-panel__pager { justify-content: flex-end; }
.rows-panel__conditions { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.rows-panel__cond { display: flex; gap: 6px; align-items: center; width: 100%; }
.rows-panel__add-form { margin-top: 10px; }
</style>
