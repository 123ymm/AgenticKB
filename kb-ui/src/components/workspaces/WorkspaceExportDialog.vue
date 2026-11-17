<template>
  <el-dialog
    :model-value="modelValue"
    title="导出制品草稿"
    width="560px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <!-- 选择前：端口复选列表 + 逐项名称 + 责任人（MVP 只读 owner） -->
    <template v-if="!result">
      <p v-if="!ports.length" class="ws-expdlg__empty">图上还没有可导出的输出端口——先运行工作区</p>
      <template v-else>
        <ul class="ws-expdlg__ports">
          <li v-for="port in ports" :key="port.ref" class="ws-expdlg__port">
            <el-tooltip :disabled="port.exportable" :content="port.reason" placement="top">
              <div class="ws-expdlg__port-line" :data-test="`export-port-${port.nodeId}-${port.portId}`">
                <el-checkbox
                  :model-value="selected.has(port.ref)"
                  :disabled="!port.exportable"
                  :data-test="`export-check-${port.nodeId}-${port.portId}`"
                  @update:model-value="toggle(port.ref)"
                >
                  <span class="ws-expdlg__port-name">{{ port.nodeLabel }} · {{ port.name }}</span>
                </el-checkbox>
                <span class="ws-expdlg__port-meta">{{ portMeta(port) }}</span>
                <span v-if="!port.exportable" class="ws-expdlg__port-reason">{{ port.reason }}</span>
              </div>
            </el-tooltip>
            <el-input
              v-if="selected.has(port.ref)"
              v-model="names[port.ref]"
              class="ws-expdlg__name"
              :data-test="`export-name-${port.nodeId}-${port.portId}`"
              size="small"
              maxlength="200"
              placeholder="制品名称"
            />
          </li>
        </ul>

        <div class="ws-expdlg__owner">
          <label class="ws-expdlg__owner-label">责任人</label>
          <el-input :model-value="ownerLabel" disabled data-test="export-owner" />
          <p class="ws-expdlg__hint">导出制品归工作区所有者（MVP 固定，暂不支持改派）</p>
        </div>
      </template>
    </template>

    <!-- 选择后：结果回执（成功=制品名+未发布+查看；失败=错误中文化） -->
    <template v-else>
      <el-alert
        v-if="result.duplicate"
        type="info"
        :closable="false"
        class="ws-expdlg__dup"
        title="该请求为重复提交——已返回既有导出结果"
      />
      <ul class="ws-expdlg__results">
        <li
          v-for="item in result.exports"
          :key="`${item.node_id}:${item.port_id}`"
          class="ws-expdlg__result"
          :class="`is-${item.status}`"
          :data-test="`export-result-${item.node_id}-${item.port_id}`"
        >
          <span class="ws-expdlg__result-name">{{ item.name }}</span>
          <template v-if="item.status === 'succeeded'">
            <el-tag size="small" type="info" effect="plain">未发布</el-tag>
            <el-button size="small" text type="primary" data-test="export-view" @click="viewArtifact(item)">查看制品</el-button>
          </template>
          <span v-else class="ws-expdlg__result-error">{{ errorLabel(item.error) }}</span>
        </li>
      </ul>
    </template>

    <template #footer>
      <el-button @click="close">{{ result ? '关闭' : '取消' }}</el-button>
      <el-button
        v-if="!result"
        type="primary"
        :loading="submitting"
        :disabled="!selected.size"
        data-test="export-submit"
        @click="submit"
      >导出所选</el-button>
      <el-button v-else type="primary" data-test="export-again" @click="reset">再导出一批</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useWorkspacesApi, type ExportResult } from '@/api/workspaces'
import { apiErrorDetail } from '@/api/proxyClient'
import { generateRequestId } from '@/utils/uuid'

/**
 * 导出制品草稿对话框（67号 Task 12）：可导出端口复选（不可选禁用+原因
 * ——无输出/空输出/已过期；「已绑定」类失败后端导出门禁才可知，走结果
 * 回执的错误中文化）→ 逐项名称（默认端口名，1..200）→ 责任人（拍板：
 * MVP 只读工作区 owner）→ exportDrafts（request_id 幂等键——经
 * generateRequestId 三层回退：内网 HTTP 无 crypto.randomUUID 也能生成）
 * → 回执：成功项「未发布」+查看制品（router.push），失败项错误码中文化；
 * duplicate 重放同样展示既有结果。
 */

export interface ExportPortOption {
  nodeId: string
  portId: string
  ref: string
  name: string
  nodeLabel: string
  rowCount: number | null
  exportable: boolean
  reason: string
}

const props = defineProps<{
  modelValue: boolean
  workspaceId: string
  ports: ExportPortOption[]
  /** 工作区 owner 展示名（MVP 只读）。 */
  ownerLabel: string
}>()

const emit = defineEmits<{
  'update:modelValue': [boolean]
  exported: [result: ExportResult]
}>()

const api = useWorkspacesApi()
const router = useRouter()

const selected = reactive(new Set<string>())
const names = reactive<Record<string, string>>({})
const submitting = ref(false)
const result = ref<ExportResult | null>(null)

watch(() => props.modelValue, (open) => { if (open) reset() })

function reset(): void {
  selected.clear()
  Object.keys(names).forEach(k => { delete names[k] })
  result.value = null
}

function toggle(ref: string): void {
  if (selected.has(ref)) {
    selected.delete(ref)
  } else {
    selected.add(ref)
    const port = props.ports.find(p => p.ref === ref)
    if (port && !names[ref]) names[ref] = port.name
  }
}

function portMeta(port: ExportPortOption): string {
  return port.rowCount == null ? '未执行' : `${port.rowCount} 行`
}

const EXPORT_ERROR_LABELS: Record<string, string> = {
  output_not_found: '输出端口不存在或尚未执行',
  stale_output: '输出已过期，重新运行后再导出',
  empty_output: '输出无数据行',
  invalid_owner_user: '责任人无效',
  source_permission_revoked: '来源库权限已变化，无权读取',
  artifact_modified_since_export: '制品在导出后已被人工修改',
  invalid_output: '输出数据不可读',
  workspace_validation: '校验未通过',
  export_failed: '导出失败',
}

function errorLabel(code?: string): string {
  if (!code) return '导出失败'
  return EXPORT_ERROR_LABELS[code] ?? `导出失败（${code}）`
}

function close(): void {
  emit('update:modelValue', false)
}

async function submit(): Promise<void> {
  const items = [...selected].map(ref => {
    const port = props.ports.find(p => p.ref === ref)!
    return { node_id: port.nodeId, port_id: port.portId, artifact_name: (names[ref] ?? '').trim() }
  })
  const invalid = items.find(item => !item.artifact_name || item.artifact_name.length > 200)
  if (invalid) {
    ElMessage.warning('请为每个勾选端口填写 1-200 字的制品名称')
    return
  }
  submitting.value = true
  try {
    const response = await api.exportDrafts(props.workspaceId, {
      request_id: generateRequestId(),
      items,
    })
    result.value = response
    emit('exported', response)
    const failed = response.exports.filter(item => item.status === 'failed')
    if (response.exports.length > failed.length) {
      ElMessage.success(`已导出 ${response.exports.length - failed.length} 项草稿（未发布）`)
    }
  } catch (error) {
    ElMessage.error(`导出失败：${await apiErrorDetail(error)}`)
  } finally {
    submitting.value = false
  }
}

function viewArtifact(item: { artifact_id: string | null }): void {
  if (!item.artifact_id) return
  void router.push({ name: 'artifact-detail', params: { artifactId: item.artifact_id } })
}
</script>

<style scoped>
.ws-expdlg__empty {
  margin: 8px 0 0;
  font-size: 12.5px;
  color: var(--kb-text-tertiary, #94a3b8);
}
.ws-expdlg__ports {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 300px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.ws-expdlg__port { display: flex; flex-direction: column; gap: 4px; }
.ws-expdlg__port-line {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.ws-expdlg__port-name {
  font-size: 12.5px;
  color: var(--kb-text-primary, inherit);
}
.ws-expdlg__port-meta {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--kb-text-tertiary, #94a3b8);
  font-variant-numeric: tabular-nums;
}
.ws-expdlg__port-reason {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--kb-warning, #f59e0b);
}
.ws-expdlg__name { max-width: 320px; }

.ws-expdlg__owner { margin-top: 14px; }
.ws-expdlg__owner-label {
  display: block;
  margin-bottom: 5px;
  font-size: 11.5px;
  color: var(--kb-text-secondary, #64748b);
}
.ws-expdlg__hint {
  margin: 5px 0 0;
  font-size: 11px;
  color: var(--kb-text-tertiary, #94a3b8);
}

.ws-expdlg__dup { margin-bottom: 10px; }
.ws-expdlg__results {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ws-expdlg__result {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--kb-border-light, #f1f5f9);
  border-radius: 8px;
}
.ws-expdlg__result.is-failed { border-color: var(--kb-warning-soft, rgba(245, 158, 11, 0.3)); }
.ws-expdlg__result-name {
  flex: 1;
  min-width: 0;
  font-size: 12.5px;
  color: var(--kb-text-primary, inherit);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-expdlg__result-error {
  font-size: 11.5px;
  color: var(--kb-warning, #f59e0b);
}
</style>
