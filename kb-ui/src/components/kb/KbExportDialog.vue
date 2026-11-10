<template>
  <el-dialog
    :model-value="modelValue"
    title="导出下载"
    width="720px"
    @update:model-value="emit('update:modelValue', $event)"
    @open="refresh"
  >
    <el-alert type="info" :closable="false" class="kb-export__notice">
      <p>把「{{ kb?.name }}」的全部原始文档与目录结构打包为 zip（含 manifest.json 清单）下载到本地。</p>
      <p>大库打包需要时间（数 GB 约 1-2 小时），完成前可关闭本窗口，任务会在后台继续；产物保留 72 小时，过期自动清理。</p>
    </el-alert>

    <div class="kb-export__toolbar">
      <el-button type="primary" size="small" :loading="creating" @click="create">
        <el-icon class="el-icon--left"><Download /></el-icon>新建导出
      </el-button>
      <span class="kb-export__hint">同库同时只跑一个导出任务</span>
    </div>

    <el-table v-loading="loading" :data="tasks" size="small" empty-text="还没有导出任务">
      <el-table-column label="状态" width="92">
        <template #default="{ row }">
          <el-tag :type="statusTagType(row.status)" size="small" disable-transitions>
            {{ statusLabel(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="进度" min-width="240">
        <template #default="{ row }">
          <template v-if="isPacking(row)">
            <el-progress
              :percentage="packPercent(row)" :stroke-width="8"
              :format="() => `${row.progress.packed_docs ?? 0}/${row.progress.total_docs ?? '?'} 文档`"
            />
            <div class="kb-export__meta">
              已打包 {{ formatBytes(row.progress.packed_bytes ?? 0) }}
              <el-tooltip v-if="row.progress.warnings_count" placement="top">
                <template #content>
                  {{ row.progress.warnings_count }} 个文档读取失败已跳过，详见包内 manifest.json 的 warnings
                </template>
                <el-link type="warning" :underline="false">· {{ row.progress.warnings_count }} 个跳过</el-link>
              </el-tooltip>
            </div>
          </template>
          <span v-else class="kb-export__meta">{{ phaseLabel(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="产物" width="150">
        <template #default="{ row }">
          <template v-if="row.status === 'succeeded'">
            <div>{{ row.doc_count ?? 0 }} 个文档 · {{ formatBytes(row.artifact_size ?? 0) }}</div>
            <el-tooltip :content="`sha256：${row.artifact_sha256 ?? ''}`" placement="top">
              <el-link type="primary" :underline="false" @click="copySha(row)">
                复制校验值
              </el-link>
            </el-tooltip>
          </template>
          <el-tooltip v-else-if="row.error" :content="row.error" placement="top">
            <span class="kb-export__error">失败原因</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="发起时间" width="150">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.status === 'succeeded'" size="small" type="primary" plain
            :loading="downloadingId === row.id" @click="download(row)"
          >下载</el-button>
          <el-button
            v-if="row.status === 'failed' || row.status === 'cancelled' || row.status === 'expired'"
            size="small" plain @click="create"
          >重新导出</el-button>
          <el-button
            v-if="row.status === 'queued' || row.status === 'running'"
            size="small" type="warning" plain @click="cancel(row)"
          >取消</el-button>
          <el-button
            v-if="row.status !== 'queued' && row.status !== 'running'"
            size="small" type="danger" text @click="remove(row)"
          >删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="expiredHint" class="kb-export__expired">{{ expiredHint }}</div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download } from '@element-plus/icons-vue'
import { useKbApi } from '@/api/kb'
import { apiErrorDetail } from '@/api/proxyClient'
import { usePolling } from '@/composables/usePolling'
import type { KbExportTask, KbSummary } from '@/types/kb'

const props = defineProps<{ modelValue: boolean; kb: KbSummary | null }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const kbApi = useKbApi()
const tasks = ref<KbExportTask[]>([])
const loading = ref(false)
const creating = ref(false)
const downloadingId = ref<string | null>(null)

const hasActive = computed(() =>
  tasks.value.some((t) => t.status === 'queued' || t.status === 'running' || t.status === 'cancelling'))

const expiredHint = computed(() => {
  const done = tasks.value.filter((t) => t.status === 'succeeded' && t.expires_at)
  if (!done.length) return ''
  const soonest = done
    .map((t) => Date.parse(t.expires_at as string))
    .filter((ts) => !Number.isNaN(ts) && ts > Date.now())
    .sort((a, b) => a - b)[0]
  if (!soonest) return ''
  const hours = Math.max(1, Math.round((soonest - Date.now()) / 3600_000))
  return `产物保留 72 小时：最早一份约 ${hours} 小时后过期，过期自动清理`
})

async function refresh() {
  if (!props.kb) return
  loading.value = true
  try {
    tasks.value = await kbApi.listExportTasks(props.kb.id)
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    loading.value = false
  }
}

// 有活跃任务才轮询（5s）；对话框关闭即停
usePolling(refresh, 5000, {
  shouldRun: () => props.modelValue && hasActive.value,
})

async function create() {
  if (!props.kb) return
  try {
    await ElMessageBox.confirm(
      `将把「${props.kb.name}」的全部原始文档打包为 zip（不压缩，数 GB 大库约需 1-2 小时）。确认开始？`,
      '新建整库导出',
      { confirmButtonText: '开始导出', cancelButtonText: '取消', type: 'info' },
    )
  } catch { return }
  creating.value = true
  try {
    const task = await kbApi.createExportTask(props.kb.id)
    if (task.already_started) ElMessage.info('该库已有导出任务在进行中')
    else ElMessage.success('导出任务已创建，打包完成后可下载')
    await refresh()
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    creating.value = false
  }
}

async function download(task: KbExportTask) {
  downloadingId.value = task.id
  try {
    const access = await kbApi.getExportDownloadUrl(task.id)
    // presign 直连对象存储（绝不经代理走 blob：大包会撞网关读超时）；
    // attachment disposition 已带中文保存名，天然支持断点续传。
    window.open(access.url, '_blank')
    ElMessage.success(`已开始下载「${access.filename}」，请在浏览器下载列表查看`)
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    downloadingId.value = null
  }
}

async function cancel(task: KbExportTask) {
  try {
    await ElMessageBox.confirm('取消后本次打包作废（已上传部分不保留），确认取消？', '取消导出', {
      confirmButtonText: '取消任务', cancelButtonText: '再等等', type: 'warning',
    })
  } catch { return }
  try {
    await kbApi.deleteExportTask(task.id)
    ElMessage.success('已请求取消')
    await refresh()
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  }
}

async function remove(task: KbExportTask) {
  try {
    await ElMessageBox.confirm('删除后产物与任务记录一并清除，不可恢复。', '删除导出记录', {
      confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
    })
  } catch { return }
  try {
    await kbApi.deleteExportTask(task.id)
    ElMessage.success('已删除')
    await refresh()
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  }
}

async function copySha(task: KbExportTask) {
  const sha = task.artifact_sha256 ?? ''
  if (!sha) return
  try {
    await navigator.clipboard.writeText(sha)
    ElMessage.success('校验值已复制——下载后可对照 `sha256sum 文件名.zip`')
  } catch {
    ElMessage.warning(`复制失败，校验值：${sha}`)
  }
}

// ── 展示辅助 ──

function isPacking(task: KbExportTask): boolean {
  return task.status === 'running' && task.phase === 'packaging'
}

function packPercent(task: KbExportTask): number {
  const total = task.progress.total_docs ?? 0
  if (!total) return 0
  return Math.min(100, Math.round(((task.progress.packed_docs ?? 0) / total) * 100))
}

function statusLabel(status: KbExportTask['status']): string {
  return ({
    queued: '排队中', running: '进行中', cancelling: '取消中', succeeded: '已完成',
    failed: '失败', cancelled: '已取消', expired: '已过期',
  } as const)[status] ?? status
}

function statusTagType(status: KbExportTask['status']) {
  return ({
    queued: 'info', running: 'primary', cancelling: 'warning', succeeded: 'success',
    failed: 'danger', cancelled: 'info', expired: 'info',
  } as const)[status] ?? 'info'
}

function phaseLabel(task: KbExportTask): string {
  if (task.status === 'failed') return '导出失败'
  if (task.status === 'cancelled') return '已取消'
  if (task.status === 'expired') return '产物已过期清理'
  if (task.status === 'succeeded') return '打包完成，可下载'
  const phase = ({
    queued: '排队等待打包', prepare: '准备文档清单', packaging: '打包中',
    uploading: '上传产物到对象存储', finalize: '收尾',
  } as const)[task.phase]
  return phase ?? task.phase
}

function formatBytes(n: number): string {
  if (!n) return '0 MB'
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

function formatTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false })
}
</script>

<style scoped>
.kb-export__notice {
  margin-bottom: 12px;
}
.kb-export__notice p {
  margin: 0 0 4px;
  font-size: 12.5px;
  line-height: 1.6;
}
.kb-export__notice p:last-child {
  margin-bottom: 0;
}
.kb-export__toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.kb-export__hint {
  font-size: 12px;
  color: var(--kb-text-tertiary, #909399);
}
.kb-export__meta {
  margin-top: 2px;
  font-size: 12px;
  color: var(--kb-text-tertiary, #909399);
}
.kb-export__error {
  color: var(--el-color-danger);
  cursor: help;
  border-bottom: 1px dashed currentColor;
}
.kb-export__expired {
  margin-top: 8px;
  font-size: 12px;
  color: var(--kb-text-tertiary, #909399);
}
</style>
