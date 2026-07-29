<template>
  <div class="create-run">
    <header class="create-run__header">
      <el-button text @click="router.push('/mining')">返回列表</el-button>
      <div>
        <h2>上传文件并创建挖掘 Run</h2>
        <p>文件先形成 Domain 内上传批次，再绑定全局 Workflow 的精确已发布版本。</p>
      </div>
    </header>

    <div class="create-run__body">
      <section
        class="create-run__upload-zone"
        :class="{ 'is-dragover': isDragOver }"
        @dragover.prevent="isDragOver = true"
        @dragleave.prevent="isDragOver = false"
        @drop.prevent="handleDrop"
      >
        <input
          ref="fileInput"
          type="file"
          multiple
          :accept="acceptedExtensionsStr"
          hidden
          @change="handleFileSelect"
        />
        <template v-if="!files.length">
          <div class="create-run__upload-icon">+</div>
          <strong>拖拽文件到此处</strong>
          <p>支持 {{ acceptedExtensionsStr }}；ZIP 会在服务端解压为同一批次。</p>
          <el-button @click="fileInput?.click()">选择文件</el-button>
        </template>
        <template v-else>
          <div class="create-run__files">
            <div v-for="(file, index) in files" :key="`${file.name}-${index}`" class="create-run__file">
              <span>{{ file.name }}</span>
              <small>{{ formatFileSize(file.size) }}</small>
              <el-button text size="small" @click="removeFile(index)">×</el-button>
            </div>
          </div>
          <div>
            <el-button size="small" @click="fileInput?.click()">添加更多</el-button>
            <el-button size="small" text type="danger" @click="files = []">清空</el-button>
          </div>
        </template>
      </section>

      <div class="create-run__config">
        <section class="config-card">
          <h3>运行范围</h3>
          <el-form label-position="top">
            <el-form-item label="Domain（提交开始时锁定）">
              <el-input :model-value="domainStore.currentDomain" disabled />
            </el-form-item>
            <el-form-item v-if="submissionEngine === 'legacy'" label="或使用服务端路径（兼容模式）">
              <el-input v-model="inputPath" placeholder="未上传文件时可填写旧 input_path" />
            </el-form-item>
          </el-form>
        </section>

        <section v-if="submissionEngine === 'workflow'" data-test="workflow-selector" class="config-card">
          <div class="config-card__title-row">
            <h3>挖掘 Workflow</h3>
            <el-tag size="small" type="success">全局已发布</el-tag>
          </div>
          <el-form label-position="top">
            <el-form-item label="Workflow">
              <el-select v-model="selectedWorkflowId" :loading="workflowLoading" style="width: 100%">
                <el-option
                  v-for="option in workflowOptions"
                  :key="option.id"
                  :value="option.id"
                  :label="`${option.name}${option.is_system_default ? '（默认 FULL）' : ''}`"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="已发布版本（精确绑定）">
              <el-select v-model="selectedWorkflowVersion" :loading="versionLoading" style="width: 100%">
                <el-option
                  v-for="version in workflowVersions"
                  :key="version.version"
                  :value="version.version"
                  :label="`v${version.version}${version.version === selectedWorkflowCurrentVersion ? '（当前）' : ''}`"
                />
              </el-select>
            </el-form-item>
          </el-form>
          <p class="config-card__hint">Run 创建后会冻结 Workflow Manifest；后续编辑或发布不影响该 Run。</p>
          <div v-if="ontologyWarning" class="config-card__warning">
            当前 Domain 未发布本体；本体相关节点运行时将按不适用跳过，不阻止创建 Run。
          </div>
        </section>

        <section v-else class="config-card config-card--legacy">
          当前为 legacy 提交模式：暂不展示 Workflow 选择，沿用旧 Pipeline 请求。
        </section>

        <section class="config-card">
          <h3>安全运行覆盖</h3>
          <el-form label-position="top">
            <el-form-item label="并发数"><el-input-number v-model="maxWorkers" :min="1" :max="8" /></el-form-item>
            <el-form-item label="仅产出资产（Phase 1 兼容项）"><el-switch v-model="phase1Only" /></el-form-item>
          </el-form>
        </section>

        <section v-if="uploadProgress > 0" class="config-card">
          <h3>上传进度</h3>
          <el-progress :percentage="uploadProgress" :status="uploadProgress >= 100 ? 'success' : ''" />
          <p v-if="extractInfo" class="config-card__hint">{{ extractInfo }}</p>
        </section>

        <section v-if="preflightResult" data-test="preflight-panel" class="config-card preflight-panel">
          <div class="config-card__title-row">
            <h3>上传预检确认</h3>
            <el-tag type="info">{{ preflightResult.workflow.id }} v{{ preflightResult.workflow.version }}</el-tag>
          </div>
          <div class="preflight-summary">
            <el-tag v-for="(count, key) in preflightResult.summary" :key="key" effect="plain">
              {{ classificationLabel(String(key)) }} {{ count }}
            </el-tag>
          </div>
          <div class="preflight-items">
            <div v-for="item in preflightResult.items" :key="`${item.relative_path}-${item.raw_content_hash}`" class="preflight-item">
              <div class="preflight-item__identity">
                <strong>{{ item.file_name }}</strong>
                <small>{{ classificationLabel(item.classification) }}</small>
                <small v-if="item.current_snapshot?.workflow_id">
                  当前：{{ item.current_snapshot.workflow_id }} v{{ item.current_snapshot.workflow_version }}
                </small>
              </div>
              <el-select v-model="item.selected_action" size="small" style="width: 148px">
                <el-option
                  v-for="action in item.allowed_actions"
                  :key="action"
                  :value="action"
                  :label="actionLabel(action)"
                />
              </el-select>
            </div>
          </div>
          <p class="config-card__hint">Workflow 冲突默认保留当前版本；只有确认后才会创建挖掘 Run。</p>
        </section>

        <div class="create-run__actions">
          <el-button @click="router.push('/mining')">取消</el-button>
          <el-button v-if="preflightResult" @click="resetPreflight">返回修改</el-button>
          <el-button
            v-if="preflightResult"
            type="primary"
            :loading="creating"
            @click="handleConfirmCreate"
          >确认并创建 Run</el-button>
          <el-button v-else type="primary" :loading="creating" @click="handleCreate">
            {{ submissionEngine === 'workflow' ? '上传并预检' : '上传并创建 Run' }}
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useDomainStore } from '@/stores/domain'
import { useMiningStore } from '@/stores/mining'
import { useMiningApi } from '@/api/mining'
import { useMiningWorkflowApi } from '@/api/miningWorkflow'
import type {
  CreateMiningRunRequest,
  MiningPreflightAction,
  MiningPreflightResult,
  MiningSubmissionEngine,
  UploadConfig,
} from '@/types'
import type { MiningWorkflowOption, MiningWorkflowVersion } from '@/types/miningWorkflow'

const router = useRouter()
const domainStore = useDomainStore()
const miningStore = useMiningStore()
const miningApi = useMiningApi()
const workflowApi = useMiningWorkflowApi()

const files = ref<File[]>([])
const inputPath = ref('')
const maxWorkers = ref(2)
const phase1Only = ref(false)
const creating = ref(false)
const isDragOver = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const uploadProgress = ref(0)
const extractInfo = ref('')
const uploadConfig = ref<UploadConfig | null>(null)
const submissionEngine = ref<MiningSubmissionEngine>('workflow')
const preflightResult = ref<MiningPreflightResult | null>(null)
const uploadedBatchId = ref('')
const preflightDomain = ref('')

const workflowOptions = ref<MiningWorkflowOption[]>([])
const workflowVersions = ref<MiningWorkflowVersion[]>([])
const selectedWorkflowId = ref('')
const selectedWorkflowVersion = ref(0)
const selectedWorkflowCurrentVersion = ref(0)
const selectedVersionDetail = ref<MiningWorkflowVersion | null>(null)
const workflowLoading = ref(false)
const versionLoading = ref(false)
const ontologyAvailable = ref<boolean | null>(null)
let workflowSelectionGeneration = 0

const acceptedExtensions = computed(() => uploadConfig.value?.accepted_extensions
  ?? ['.md', '.txt', '.pdf', '.html', '.htm', '.doc', '.docx', '.zip', '.chm', '.hdx'])
const acceptedExtensionsStr = computed(() => acceptedExtensions.value.join(','))
const archiveExtensions = computed(() => new Set(uploadConfig.value?.archive_extensions ?? ['.zip']))
const ontologyWarning = computed(() => submissionEngine.value === 'workflow'
  && ontologyAvailable.value === false
  && (selectedVersionDetail.value?.graph_json.nodes ?? []).some(node => ONTOLOGY_OPERATOR_TYPES.has(node.operatorType)))

const ONTOLOGY_OPERATOR_TYPES = new Set([
  'entity_extract', 'entity_resolve', 'entity_relation_extract', 'entity_review_gate',
  'ontology_induction', 'ontology_review_gate', 'graph_write',
])

onMounted(async () => {
  try {
    uploadConfig.value = await miningApi.getUploadConfig()
    submissionEngine.value = uploadConfig.value.mining_run_submission_engine ?? 'workflow'
  } catch {
    submissionEngine.value = 'workflow'
  }
  if (submissionEngine.value === 'workflow') {
    await Promise.all([loadWorkflowOptions(), checkActiveOntology(domainStore.currentDomain)])
  }
})

watch(selectedWorkflowId, async (workflowId, previous) => {
  if (!workflowId || workflowId === previous) return
  resetPreflight()
  await loadWorkflowVersions(workflowId)
})

watch(selectedWorkflowVersion, async version => {
  if (preflightResult.value && version !== preflightResult.value.workflow.version) resetPreflight()
  if (version > 0 && selectedWorkflowId.value) await loadSelectedVersion(selectedWorkflowId.value, version)
})

async function loadWorkflowOptions() {
  workflowLoading.value = true
  try {
    workflowOptions.value = await workflowApi.options()
    const selected = workflowOptions.value.find(option => option.is_system_default)
      ?? workflowOptions.value.find(option => option.id === 'system-full-baseline')
      ?? workflowOptions.value[0]
    if (!selected) {
      ElMessage.error('没有可用的已发布 Workflow')
      return
    }
    selectedWorkflowId.value = selected.id
    selectedWorkflowCurrentVersion.value = selected.current_version
    selectedWorkflowVersion.value = selected.current_version
    await loadWorkflowVersions(selected.id, selected.current_version)
  } catch (error) {
    ElMessage.error(`加载 Workflow 选项失败：${errorMessage(error)}`)
  } finally {
    workflowLoading.value = false
  }
}

async function loadWorkflowVersions(workflowId: string, preferredVersion?: number) {
  const generation = ++workflowSelectionGeneration
  versionLoading.value = true
  try {
    const versions = await workflowApi.listVersions(workflowId)
    if (generation !== workflowSelectionGeneration || workflowId !== selectedWorkflowId.value) return
    workflowVersions.value = versions
    const current = workflowOptions.value.find(option => option.id === workflowId)?.current_version ?? versions[0]?.version ?? 0
    selectedWorkflowCurrentVersion.value = current
    const exact = preferredVersion && versions.some(version => version.version === preferredVersion)
      ? preferredVersion
      : current
    selectedWorkflowVersion.value = exact
    if (exact) await loadSelectedVersion(workflowId, exact, generation)
  } catch (error) {
    if (generation === workflowSelectionGeneration) ElMessage.error(`加载 Workflow 版本失败：${errorMessage(error)}`)
  } finally {
    if (generation === workflowSelectionGeneration) versionLoading.value = false
  }
}

async function loadSelectedVersion(workflowId: string, version: number, generation = workflowSelectionGeneration) {
  try {
    const detail = await workflowApi.getVersion(workflowId, version)
    if (
      generation === workflowSelectionGeneration
      && workflowId === selectedWorkflowId.value
      && version === selectedWorkflowVersion.value
    ) selectedVersionDetail.value = detail
  } catch {
    selectedVersionDetail.value = null
  }
}

async function checkActiveOntology(domain: string) {
  try {
    const active = await miningApi.getActiveOntology(domain)
    ontologyAvailable.value = Boolean(active.version)
  } catch {
    ontologyAvailable.value = false
  }
}

function getFileExtension(name: string): string {
  const index = name.lastIndexOf('.')
  return index >= 0 ? name.slice(index).toLowerCase() : ''
}

function isArchive(name: string): boolean {
  return archiveExtensions.value.has(getFileExtension(name))
}

function validateFiles(incoming: File[]): File[] {
  const valid: File[] = []
  for (const file of incoming) {
    const extension = getFileExtension(file.name)
    if (!acceptedExtensions.value.includes(extension)) {
      ElMessage.warning(`不支持的文件格式：${file.name}`)
      continue
    }
    const limit = isArchive(file.name)
      ? uploadConfig.value?.max_archive_size
      : uploadConfig.value?.max_file_size
    if (limit !== undefined && file.size > limit) {
      ElMessage.warning(`${file.name} 超过上传大小限制`)
      continue
    }
    valid.push(file)
  }
  return valid
}

function handleDrop(event: DragEvent) {
  isDragOver.value = false
  if (!event.dataTransfer?.files) return
  files.value = [...files.value, ...validateFiles(Array.from(event.dataTransfer.files))]
  resetPreflight()
}

function handleFileSelect(event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files) files.value = [...files.value, ...validateFiles(Array.from(input.files))]
  resetPreflight()
  input.value = ''
}

function removeFile(index: number) {
  files.value = files.value.filter((_, itemIndex) => itemIndex !== index)
  resetPreflight()
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function handleCreate() {
  const capturedDomain = domainStore.currentDomain
  creating.value = true
  uploadProgress.value = 0
  extractInfo.value = ''
  try {
    let uploaded: Awaited<ReturnType<typeof miningApi.uploadFiles>> | null = null
    if (files.value.length) {
      uploaded = await miningApi.uploadFiles(capturedDomain, files.value, event => {
        uploadProgress.value = event.progress
      })
      renderExtractionSummary(uploaded.extracted_archives)
    }

    let request: CreateMiningRunRequest
    if (submissionEngine.value === 'workflow') {
      if (!uploaded) {
        ElMessage.warning('Workflow 模式请先选择并上传文件')
        return
      }
      if (!selectedWorkflowId.value || selectedWorkflowVersion.value <= 0) {
        ElMessage.warning('请选择一个已发布的 Workflow 精确版本')
        return
      }
      uploadedBatchId.value = uploaded.upload_batch_id
      preflightDomain.value = capturedDomain
      preflightResult.value = await miningApi.preflightRun({
        domain: capturedDomain,
        upload_batch_id: uploaded.upload_batch_id,
        workflow_id: selectedWorkflowId.value,
        workflow_version: selectedWorkflowVersion.value,
      })
      return
    } else {
      const path = uploaded?.storage_path || inputPath.value.trim()
      if (!path) {
        ElMessage.warning('请上传文件或输入服务端路径')
        return
      }
      request = {
        domain: capturedDomain,
        input_path: path,
        max_workers: maxWorkers.value,
        phase1_only: phase1Only.value,
      }
    }

    await miningStore.createRun(request)
    ElMessage.success('Run 已创建')
    await router.push('/mining')
  } catch (error) {
    ElMessage.error(`创建失败：${errorMessage(error)}`)
  } finally {
    creating.value = false
  }
}

async function handleConfirmCreate() {
  const result = preflightResult.value
  if (!result || !uploadedBatchId.value || !preflightDomain.value) return
  creating.value = true
  try {
    const request: CreateMiningRunRequest = {
      domain: preflightDomain.value,
      upload_batch_id: uploadedBatchId.value,
      workflow_id: result.workflow.id,
      workflow_version: result.workflow.version,
      preflight_id: result.preflight_id,
      document_decisions: result.items.map(item => ({
        relative_path: item.relative_path,
        raw_content_hash: item.raw_content_hash,
        selected_action: item.selected_action,
        state_token: item.state_token,
      })),
      max_workers: maxWorkers.value,
      phase1_only: phase1Only.value,
    }
    await miningStore.createRun(request)
    ElMessage.success('Run 已创建')
    await router.push('/mining')
  } catch (error) {
    ElMessage.error(`创建失败：${errorMessage(error)}`)
  } finally {
    creating.value = false
  }
}

function resetPreflight() {
  preflightResult.value = null
  uploadedBatchId.value = ''
  preflightDomain.value = ''
}

function classificationLabel(value: string): string {
  return ({
    NEW: '新文件', REUSED: '可直接复用', RESTORABLE: '可恢复',
    RESTORABLE_CONFLICT: '历史版本冲突', WORKFLOW_CONFLICT: 'Workflow 冲突',
    IN_PROGRESS: '正在处理中', HISTORY_UNAVAILABLE: '需要重新挖掘',
  } as Record<string, string>)[value] ?? value
}

function actionLabel(value: MiningPreflightAction): string {
  return ({
    NEW: '执行挖掘', REUSED: '复用当前版本', RESTORED: '恢复历史版本',
    REMINED: '使用新 Workflow 重挖', KEPT_CURRENT: '保留当前版本',
    JOINED_EXISTING: '等待已有任务',
  } as Record<MiningPreflightAction, string>)[value]
}

function renderExtractionSummary(items: Array<{ archive: string; error: string | null; file_count: number }>) {
  const completed = items.filter(item => !item.error)
  if (completed.length) extractInfo.value = `已解压：${completed.map(item => `${item.archive} → ${item.file_count} 个文件`).join('，')}`
  for (const item of items.filter(item => item.error)) ElMessage.warning(`解压失败 ${item.archive}：${item.error}`)
}

function errorMessage(error: unknown): string {
  const value = error as { response?: { data?: { detail?: { message?: string } | string } }; message?: string }
  const detail = value?.response?.data?.detail
  return (typeof detail === 'string' ? detail : detail?.message) || value?.message || '未知错误'
}
</script>

<style scoped>
.create-run { display: flex; flex-direction: column; gap: 16px; }
.create-run__header { display: flex; align-items: flex-start; gap: 12px; }
.create-run__header h2 { margin: 0; font-size: 18px; }
.create-run__header p { margin: 4px 0 0; color: var(--kb-text-tertiary); font-size: 12px; }
.create-run__body { display: grid; grid-template-columns: minmax(360px, 1fr) minmax(360px, 1fr); gap: 16px; align-items: start; }
.preflight-summary { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.preflight-items { display: flex; flex-direction: column; gap: 8px; max-height: 320px; overflow: auto; }
.preflight-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px; border: 1px solid var(--kb-border); border-radius: 6px; }
.preflight-item__identity { display: flex; min-width: 0; flex-direction: column; gap: 3px; }
.preflight-item__identity strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.preflight-item__identity small { color: var(--kb-text-tertiary); }
.create-run__upload-zone { min-height: 320px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px; padding: 28px; border: 2px dashed var(--kb-border); border-radius: var(--kb-radius); background: var(--kb-bg-card); }
.create-run__upload-zone.is-dragover { border-color: var(--kb-accent); background: var(--kb-accent-soft); }
.create-run__upload-zone > p { max-width: 420px; margin: 0; color: var(--kb-text-tertiary); text-align: center; line-height: 1.6; }
.create-run__upload-icon { width: 48px; height: 48px; display: grid; place-items: center; border-radius: 50%; background: var(--kb-border-light); color: var(--kb-text-tertiary); font-size: 25px; }
.create-run__files { width: 100%; max-height: 250px; overflow: auto; display: flex; flex-direction: column; gap: 6px; }
.create-run__file { display: grid; grid-template-columns: 1fr auto auto; gap: 8px; align-items: center; padding: 8px 10px; border: 1px solid var(--kb-border-light); border-radius: 6px; }
.create-run__file small { color: var(--kb-text-tertiary); }
.create-run__config { display: flex; flex-direction: column; gap: 14px; }
.config-card { padding: 18px; border: 1px solid var(--kb-border-light); border-radius: var(--kb-radius); background: var(--kb-bg-card); }
.config-card h3 { margin: 0 0 13px; color: var(--kb-text-secondary); font-size: 13px; text-transform: uppercase; letter-spacing: .4px; }
.config-card__title-row { display: flex; justify-content: space-between; align-items: center; }
.config-card__hint { margin: 0; color: var(--kb-text-tertiary); font-size: 11px; line-height: 1.5; }
.config-card__warning { margin-top: 10px; padding: 9px; color: #92400e; background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; font-size: 12px; line-height: 1.5; }
.config-card--legacy { color: var(--kb-text-secondary); background: #f8fafc; font-size: 12px; }
.create-run__actions { display: flex; justify-content: flex-end; gap: 8px; }
@media (max-width: 850px) { .create-run__body { grid-template-columns: 1fr; } }
</style>
