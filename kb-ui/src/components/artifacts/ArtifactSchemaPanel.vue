<template>
  <div class="schema-panel">
    <div class="schema-panel__bar">
      <span class="schema-panel__count">{{ activeColumns.length }} 列（active）</span>
      <label class="schema-panel__check">
        <input type="checkbox" v-model="includeDropped" @change="onIncludeDropped" />显示已删列
      </label>
      <div v-if="canWrite" class="schema-panel__actions">
        <el-button size="small" data-test="schema-set-pk" @click="openPk">设置主键</el-button>
        <el-button size="small" type="primary" data-test="schema-add-col" @click="openAdd">新建列</el-button>
      </div>
    </div>

    <div class="schema-panel__cards">
      <div
        v-for="col in displayedColumns"
        :key="col.column_id"
        class="schema-panel__card"
        :class="{ 'schema-panel__card--dropped': col.status === 'dropped' }"
      >
        <div class="schema-panel__card-head">
          <b class="schema-panel__col-name">{{ col.column_name }}</b>
          <span class="schema-panel__col-id">{{ col.column_id }}</span>
          <el-tag size="small" effect="plain" :type="typeTagType(col.column_type)">
            {{ col.column_type }}
          </el-tag>
          <el-tag v-if="col.is_primary_key" size="small" type="danger" effect="dark">主键</el-tag>
          <el-tag v-if="(col.alias ?? []).length" size="small" type="info" effect="plain">
            旧名：{{ (col.alias ?? []).join('、') }}
          </el-tag>
          <span v-if="col.status === 'dropped'" class="schema-panel__dropped-text">已删（单调扩展铁律：不物理删）</span>
        </div>
        <div v-if="col.column_type === 'enum'" class="schema-panel__enums">
          <el-tag
            v-for="v in col.enum_values ?? []"
            :key="String(v)"
            size="small"
            closable
            :disable-transitions="true"
            @close="canWrite && removeEnumValue(col, v)"
          >{{ v }}</el-tag>
          <el-input
            v-if="canWrite"
            v-model="enumInputs[col.column_id]"
            size="small"
            style="width: 110px"
            placeholder="+ 枚举值"
            @keyup.enter="addEnumValue(col)"
          />
        </div>
        <div class="schema-panel__card-foot">
          <label class="schema-panel__check">
            索引
            <!-- 失败回弹：checkbox DOM 已被用户翻转，state 未变不会重渲染——key 强制回同步 -->
            <input
              :key="`idx-${col.column_id}-${indexTick}`"
              type="checkbox"
              :checked="col.is_indexed"
              :disabled="!canWrite || col.status === 'dropped'"
              @change="toggleIndexed(col, ($event.target as HTMLInputElement).checked)"
            />
          </label>
          <el-button
            v-if="canWrite && col.status === 'active'"
            size="small"
            text
            type="primary"
            data-test="schema-edit-col"
            @click="openEdit(col)"
          >编辑</el-button>
          <el-button
            v-if="canWrite && col.status === 'active'"
            size="small"
            text
            type="danger"
            data-test="schema-drop-col"
            @click="dropColumn(col)"
          >软删</el-button>
        </div>
      </div>
      <EmptyState v-if="displayedColumns.length === 0" text="尚无列定义（上传 Excel 或手工新建）" />
    </div>

    <!-- 新建列 -->
    <el-dialog v-model="addVisible" title="新建列" width="480px">
      <el-form label-width="90px">
        <el-form-item label="列名">
          <el-input v-model="addForm.column_name" maxlength="200" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="addForm.column_type">
            <el-option
              v-for="t in COLUMN_TYPES"
              :key="t.value"
              :value="t.value"
              :label="`${t.label}（${t.value}）`"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-if="addForm.column_type === 'enum'" label="枚举值">
          <el-input v-model="addForm.enumText" type="textarea" :rows="2" placeholder="逗号分隔，如：高,中,低" />
        </el-form-item>
        <el-form-item label="索引">
          <el-switch v-model="addForm.is_indexed" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-test="schema-add-submit" @click="submitAdd">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑列：改名走 alias（后端兼容旧名读取） -->
    <el-dialog v-model="editVisible" title="编辑列" width="480px">
      <el-form label-width="90px">
        <el-form-item label="列名">
          <el-input v-model="editForm.column_name" maxlength="200" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="editForm.column_type">
            <el-option
              v-for="t in COLUMN_TYPES"
              :key="t.value"
              :value="t.value"
              :label="`${t.label}（${t.value}）`"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-if="editForm.column_type === 'enum'" label="枚举值">
          <el-input v-model="editForm.enumText" type="textarea" :rows="2" placeholder="逗号分隔；留空=清除枚举" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-test="schema-edit-submit" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 设置主键（列组合，重算行键） -->
    <el-dialog v-model="pkVisible" title="设置主键" width="480px">
      <p class="schema-panel__pk-hint">
        当前主键：{{ primaryKeyNames.join('、') || '（无——行键自动 uuid）' }}
      </p>
      <el-checkbox-group v-model="pkSelection">
        <el-checkbox v-for="col in activeColumns" :key="col.column_id" :value="col.column_id">
          {{ col.column_name }}
        </el-checkbox>
      </el-checkbox-group>
      <p class="schema-panel__pk-hint">确认后将重算全部行键（row_key）；键冲突会以 409 阻断。</p>
      <template #footer>
        <el-button @click="pkVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-test="schema-pk-submit" @click="submitPk">确认重算</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import EmptyState from '@/components/common/EmptyState.vue'
import type { ArtifactColumn } from '@/types/artifacts'

const COLUMN_TYPES = [
  { value: 'text', label: '文本' },
  { value: 'int', label: '整数' },
  { value: 'float', label: '小数' },
  { value: 'date', label: '日期' },
  { value: 'enum', label: '枚举' },
  { value: 'bool', label: '布尔' },
] as const

const props = defineProps<{
  artifactId: string
  canWrite: boolean
  /** 详情 schema（active 列；本面板只读展示源，编辑后由 changed → 父级 reload 刷新）。 */
  columns: ArtifactColumn[]
  expectedUpdatedAt?: string
}>()

const emit = defineEmits<{ changed: [] }>()

const api = useArtifactsApi()

const includeDropped = ref(false)
const droppedColumns = ref<ArtifactColumn[]>([])
const saving = ref(false)

const addVisible = ref(false)
const addForm = ref({ column_name: '', column_type: 'text', enumText: '', is_indexed: false })

const editVisible = ref(false)
const editingColumn = ref<ArtifactColumn | null>(null)
const editForm = ref({ column_name: '', column_type: 'text', enumText: '' })

const pkVisible = ref(false)
const pkSelection = ref<string[]>([])

/** 枚举快捷输入框状态（列 id → 输入值）。 */
const enumInputs = ref<Record<string, string>>({})
/** 索引 checkbox 失败回弹的强制重渲染 tick。 */
const indexTick = ref(0)

const activeColumns = computed(() => props.columns.filter(c => c.status === 'active'))
const primaryKeyNames = computed(() => activeColumns.value
  .filter(c => c.is_primary_key).map(c => c.column_name))
const displayedColumns = computed(() => includeDropped.value
  ? [...activeColumns.value, ...droppedColumns.value]
  : activeColumns.value)

async function onIncludeDropped(): Promise<void> {
  if (!includeDropped.value) return
  try {
    const all = await api.listColumns(props.artifactId, { includeDropped: true })
    droppedColumns.value = all.filter(c => c.status === 'dropped')
  } catch (error) {
    includeDropped.value = false
    ElMessage.error(`加载已删列失败：${await apiErrorDetail(error)}`)
  }
}

/** 统一错误出口：409 stale → 提示刷新并 emit changed（父级 reload 推进乐观锁令牌）。 */
async function guard(action: () => Promise<unknown>, successText: string): Promise<boolean> {
  saving.value = true
  try {
    await action()
    ElMessage.success(successText)
    emit('changed')
    return true
  } catch (error) {
    const resp = (error as { response?: { status?: number; data?: { detail?: { code?: string } } } })?.response
    if (resp?.status === 409 && resp.data?.detail?.code === 'stale_artifact') {
      ElMessage.error('制品已被他人修改（409 stale_artifact），已为你刷新——请重试')
      emit('changed')
    } else {
      ElMessage.error(await apiErrorDetail(error))
    }
    return false
  } finally {
    saving.value = false
  }
}

function openAdd(): void {
  addForm.value = { column_name: '', column_type: 'text', enumText: '', is_indexed: false }
  addVisible.value = true
}

function parseEnumText(text: string): string[] {
  return text.split(/[，,]/).map(v => v.trim()).filter(Boolean)
}

async function submitAdd(): Promise<void> {
  const name = addForm.value.column_name.trim()
  if (!name) {
    ElMessage.warning('列名不能为空')
    return
  }
  const enumValues = parseEnumText(addForm.value.enumText)
  const ok = await guard(
    () => api.addColumn(props.artifactId, {
      column_name: name,
      column_type: addForm.value.column_type,
      ...(addForm.value.column_type === 'enum' && enumValues.length
        ? { enum_values: enumValues }
        : {}),
      is_indexed: addForm.value.is_indexed,
      expected_updated_at: props.expectedUpdatedAt,
    }),
    '列已创建（旧行该列读出 NULL）',
  )
  if (ok) addVisible.value = false
}

function openEdit(col: ArtifactColumn): void {
  editingColumn.value = col
  editForm.value = {
    column_name: col.column_name,
    column_type: col.column_type,
    enumText: (col.enum_values ?? []).join(','),
  }
  editVisible.value = true
}

async function submitEdit(): Promise<void> {
  const col = editingColumn.value
  if (!col) return
  const name = editForm.value.column_name.trim()
  if (!name) {
    ElMessage.warning('列名不能为空')
    return
  }
  const payload: Record<string, unknown> = { expected_updated_at: props.expectedUpdatedAt }
  if (name !== col.column_name) payload.column_name = name
  if (editForm.value.column_type !== col.column_type) payload.column_type = editForm.value.column_type
  if (editForm.value.column_type === 'enum') {
    payload.enum_values = parseEnumText(editForm.value.enumText)
  } else if (col.column_type === 'enum') {
    // 从枚举切走：清掉残留枚举定义（防 int/bool 列挂着 enum_values 元数据）
    payload.enum_values = []
  }
  const ok = await guard(
    () => api.updateColumn(props.artifactId, col.column_id, payload),
    '列已更新（改名写 alias，旧名兼容读取）',
  )
  if (ok) editVisible.value = false
}

function addEnumValue(col: ArtifactColumn): void {
  const input = enumInputs.value[col.column_id]?.trim()
  if (!input) return
  void guard(
    () => api.updateColumn(props.artifactId, col.column_id, {
      enum_values: [...(col.enum_values ?? []), input],
      expected_updated_at: props.expectedUpdatedAt,
    }),
    `枚举值已追加：${input}`,
  ).then(ok => { if (ok) enumInputs.value[col.column_id] = '' })
}

function removeEnumValue(col: ArtifactColumn, value: string): void {
  void guard(
    () => api.updateColumn(props.artifactId, col.column_id, {
      enum_values: (col.enum_values ?? []).filter(v => v !== value),
      expected_updated_at: props.expectedUpdatedAt,
    }),
    `枚举值已移除：${value}`,
  )
}

function toggleIndexed(col: ArtifactColumn, next: boolean): void {
  void guard(
    () => api.updateColumn(props.artifactId, col.column_id, {
      is_indexed: next, expected_updated_at: props.expectedUpdatedAt,
    }),
    next ? '已标记索引（下次发布生效）' : '已取消索引',
  ).then(ok => { if (!ok) indexTick.value++ })
}

async function dropColumn(col: ArtifactColumn): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `软删列「${col.column_name}」？存量数据不动（单调扩展铁律），运行态读取层随之隐藏。`,
      '软删列', { type: 'warning' },
    )
  } catch {
    return
  }
  await guard(
    () => api.updateColumn(props.artifactId, col.column_id, {
      status: 'dropped', expected_updated_at: props.expectedUpdatedAt,
    }),
    '列已软删',
  )
}

function openPk(): void {
  pkSelection.value = activeColumns.value.filter(c => c.is_primary_key).map(c => c.column_id)
  pkVisible.value = true
}

async function submitPk(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `将按 ${pkSelection.value.length} 列组合重算全部行键（row_key）；若多行折到同一行键会以 409 阻断。确认？`,
      '设置主键', { type: 'warning' },
    )
  } catch {
    return
  }
  const ok = await guard(
    () => api.setPrimaryKey(props.artifactId, pkSelection.value, props.expectedUpdatedAt),
    '主键已设置',
  )
  if (ok) pkVisible.value = false
}

function typeTagType(value: string): 'primary' | 'success' | 'warning' | 'info' {
  if (value === 'enum') return 'warning'
  if (value === 'int' || value === 'float') return 'success'
  if (value === 'date' || value === 'bool') return 'primary'
  return 'info'
}

defineExpose({ openAdd, submitAdd, openEdit, submitEdit, submitPk, dropColumn })
</script>

<style scoped>
.schema-panel { display: flex; flex-direction: column; gap: 10px; }
.schema-panel__bar { display: flex; align-items: center; gap: 14px; }
.schema-panel__count { font-size: 13px; color: var(--kb-text-tertiary); }
.schema-panel__check { font-size: 12.5px; color: var(--kb-text-tertiary); display: inline-flex; align-items: center; gap: 4px; }
.schema-panel__actions { margin-left: auto; display: flex; gap: 8px; }
.schema-panel__cards { display: flex; flex-direction: column; gap: 8px; }
.schema-panel__card { border: 1px solid var(--kb-border-light, #e4e7ed); border-radius: 6px; padding: 10px 14px; }
.schema-panel__card--dropped { opacity: 0.55; background: var(--kb-bg-card, #fafafa); }
.schema-panel__card-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.schema-panel__col-name { font-size: 14px; }
.schema-panel__col-id { font-size: 12px; color: var(--kb-text-tertiary); }
.schema-panel__dropped-text { font-size: 12px; color: var(--el-color-danger, #f56c6c); }
.schema-panel__enums { margin-top: 8px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.schema-panel__card-foot { margin-top: 8px; display: flex; align-items: center; gap: 10px; }
.schema-panel__pk-hint { font-size: 12.5px; color: var(--kb-text-tertiary); margin: 0 0 8px; }
</style>
