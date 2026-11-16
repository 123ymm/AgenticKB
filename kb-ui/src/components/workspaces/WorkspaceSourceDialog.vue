<template>
  <el-dialog
    :model-value="modelValue"
    title="接入资料"
    width="520px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-tabs v-model="tab" class="ws-srcdlg__tabs">
      <!-- Tab 1：知识库目录（即读即冻） -->
      <el-tab-pane label="知识库目录" name="kb_dir">
        <el-form label-position="top" @submit.prevent>
          <el-form-item label="知识库（当前域内可见）" required>
            <el-select
              v-model="kbId"
              data-test="source-kb"
              :loading="kbsLoading"
              filterable
              placeholder="选择知识库"
              style="width: 100%"
            >
              <el-option v-for="kb in kbs" :key="kb.id" :value="kb.id" :label="kb.name" />
            </el-select>
          </el-form-item>
          <el-form-item label="目录范围">
            <el-input-tag
              v-model="directories"
              data-test="source-dirs"
              placeholder="目录路径（回车添加，留空=全库）"
              style="width: 100%"
            />
            <p class="ws-srcdlg__hint">读取即冻结——后续库内变更不自动同步，重新接入才会读取新资料</p>
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- Tab 2：Excel 上传 -->
      <el-tab-pane label="Excel" name="excel">
        <el-form label-position="top" @submit.prevent>
          <el-form-item label="文件（.xlsx）" required>
            <el-upload
              class="ws-srcdlg__upload"
              drag
              accept=".xlsx"
              :limit="1"
              :auto-upload="false"
              :on-change="onExcelPicked"
              :on-remove="() => (excelFile = null)"
            >
              <div class="ws-srcdlg__upload-inner">
                <el-icon :size="26"><UploadFilled /></el-icon>
                <p class="ws-srcdlg__upload-text">拖拽或点击选择 .xlsx 文件</p>
              </div>
            </el-upload>
          </el-form-item>
          <el-form-item label="工作表（可选）">
            <el-input
              v-model="sheetName"
              data-test="source-sheet"
              placeholder="默认第一张表"
            />
          </el-form-item>
        </el-form>
      </el-tab-pane>
    </el-tabs>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="submitting" data-test="source-submit" @click="submit">接入</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { UploadFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { UploadFile } from 'element-plus'
import { useWorkspacesApi } from '@/api/workspaces'
import { useKbApi } from '@/api/kb'
import { apiErrorDetail } from '@/api/proxyClient'
import type { GraphNode } from '@/api/workspaces'

/**
 * 接入资料对话框（67号 Task 12）：两类来源——kb_dir（域内 KB+目录多选，
 * 复用 listKbs；目录手输路径 tag，留空=全库）与 excel（.xlsx 上传+可选
 * sheet）。
 *
 * node_id 语义（后端 POST /sources 必传——前端生成）：接入即同步把
 * source 节点写进图（PUT graph 整图替换）再落 sources 行；成功后 emit
 * created(nodeId)，由父级刷新并自动跑一次 source 节点（scope=node）让
 * 底表立即有数据——POST /sources 本身只冻快照不执行（executor 口径）。
 * PUT 409（图被并发修改）→ emit graph-conflict，父级刷新令牌后可重试。
 */

const props = defineProps<{
  modelValue: boolean
  workspaceId: string
  /** 来源 KB 候选域（=工作区 domain_id；KB 列表按域取）。 */
  domain: string
  /** 当前图节点（生成不冲突的 src_<n> 与画布落点）。 */
  nodes: GraphNode[]
  /** PUT graph 乐观锁令牌（父级持权威值）。 */
  graphRevision: number
}>()

const emit = defineEmits<{
  'update:modelValue': [boolean]
  created: [nodeId: string]
  'graph-conflict': []
}>()

const api = useWorkspacesApi()
const kbApi = useKbApi()

const tab = ref<'kb_dir' | 'excel'>('kb_dir')
const kbs = ref<Array<{ id: string; name: string }>>([])
const kbsLoading = ref(false)
const kbId = ref('')
const directories = ref<string[]>([])
const excelFile = ref<File | null>(null)
const sheetName = ref('')
const submitting = ref(false)

// 打开重置 + 按域拉 KB 候选（失败静默退化为空下拉，不挡对话框）；
// immediate——首挂即打开（如路由恢复态）也要初始化
watch(() => props.modelValue, (open) => {
  if (!open) return
  tab.value = 'kb_dir'
  kbId.value = ''
  directories.value = []
  excelFile.value = null
  sheetName.value = ''
  kbs.value = []
  if (!props.domain) return
  kbsLoading.value = true
  kbApi.listKbs(props.domain)
    .then(rows => { kbs.value = rows })
    .catch(() => { kbs.value = [] })
    .finally(() => { kbsLoading.value = false })
}, { immediate: true })

function onExcelPicked(file: UploadFile): void {
  excelFile.value = file.raw ?? null
}

function close(): void {
  emit('update:modelValue', false)
}

/** 生成不冲突的 src_<n> 节点 id（后端无代填——客户端权威）。 */
function nextSourceNodeId(): string {
  const used = new Set(props.nodes.map(n => n.id))
  let n = used.size + 1
  while (used.has(`src_${n}`)) n += 1
  return `src_${n}`
}

function frozenToast(connector: string, frozen: Record<string, unknown>): void {
  if (connector === 'kb_dir') {
    ElMessage.success(`来源已冻结：${Number(frozen.doc_count ?? 0)} 份文档`)
  } else {
    ElMessage.success(`来源已冻结：${Number(frozen.row_count ?? 0)} 行`)
  }
}

async function submit(): Promise<void> {
  const isKb = tab.value === 'kb_dir'
  if (isKb && !kbId.value) { ElMessage.warning('请选择知识库'); return }
  if (!isKb) {
    if (!excelFile.value) { ElMessage.warning('请选择 .xlsx 文件'); return }
    if (!excelFile.value.name.toLowerCase().endsWith('.xlsx')) {
      ElMessage.warning('仅支持 .xlsx 文件'); return
    }
  }

  const nodeId = nextSourceNodeId()
  const label = isKb
    ? (kbs.value.find(kb => kb.id === kbId.value)?.name || '知识库资料')
    : (excelFile.value?.name.replace(/\.xlsx$/i, '') || 'Excel 资料')
  const newNode: GraphNode = {
    id: nodeId,
    op: 'source',
    name: label,
    config: {},
    inputs: [],
    ports: [{ id: 'main', name: '底表', kind: 'table' }],
    x: 0,
    y: props.nodes.reduce((max, n) => Math.max(max, n.y ?? 0), 0) + 1,
  }

  submitting.value = true
  try {
    // ① 先把 source 节点写进图（整图替换+乐观锁）
    await api.putGraph(props.workspaceId, { nodes: [...props.nodes, newNode] }, props.graphRevision)
    // ② 再落 sources 行（kb_dir 即读即冻 / excel 整表冻结）
    const result = isKb
      ? await api.upsertSourceKbDir(props.workspaceId, {
        node_id: nodeId, kb_id: kbId.value,
        directories: directories.value.map(d => d.trim()).filter(Boolean),
      })
      : await api.uploadSourceExcel(props.workspaceId, {
        node_id: nodeId, file: excelFile.value!, sheet_name: sheetName.value.trim() || undefined,
      })
    frozenToast(result.connector, result.frozen ?? {})
    emit('update:modelValue', false)
    emit('created', nodeId)
  } catch (error) {
    const status = (error as { response?: { status?: number } })?.response?.status
    if (status === 409) {
      ElMessage.error('图已被他人修改，已为您刷新——请重试')
      emit('graph-conflict')
    } else {
      ElMessage.error(`接入资料失败：${await apiErrorDetail(error)}`)
    }
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.ws-srcdlg__tabs { margin-top: -6px; }
.ws-srcdlg__hint {
  margin: 4px 0 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--kb-text-tertiary, #94a3b8);
  width: 100%;
}
.ws-srcdlg__upload :deep(.el-upload-dragger) { padding: 18px 0; }
.ws-srcdlg__upload-inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  color: var(--kb-text-tertiary, #94a3b8);
}
.ws-srcdlg__upload-text { margin: 0; font-size: 12px; }
</style>
