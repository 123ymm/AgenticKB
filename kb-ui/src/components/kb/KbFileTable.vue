<template>
  <div class="kb-files">
    <!-- Upload -->
    <div v-if="canWrite" class="kb-files__upload">
      <el-upload
        drag
        multiple
        :show-file-list="false"
        :auto-upload="true"
        :http-request="handleUpload"
        :disabled="uploading > 0"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">
          拖拽文件到此处上传，或<em>点击选择</em>
        </div>
        <template #tip>
          <div class="el-upload__tip">支持多文件；<code>.zip</code> 将自动解压为多个文档</div>
        </template>
      </el-upload>
    </div>

    <!-- Filters -->
    <div class="kb-files__toolbar">
      <el-select
        v-model="directoryFilter"
        placeholder="目录筛选"
        size="small"
        clearable
        class="kb-files__dir-select"
        @change="load"
      >
        <el-option label="全部目录" :value="ALL_DIRS" />
        <el-option
          v-for="d in directoryOptions"
          :key="d.value"
          :label="d.label"
          :value="d.value"
        />
      </el-select>
      <span class="kb-files__count">{{ docs.length }} 个文档</span>
      <el-button text size="small" :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>
      </el-button>
    </div>

    <!-- Table -->
    <div class="kb-files__table-wrap">
      <el-table
        :data="docs"
        v-loading="loading"
        class="kb-table"
        :header-cell-style="{ background: 'transparent' }"
      >
        <el-table-column label="文件名" min-width="220">
          <template #default="{ row }">
            <div class="kb-file-name">
              <el-icon><Document /></el-icon>
              <span>{{ row.document_name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="目录" width="160">
          <template #default="{ row }">
            <span class="kb-file-dir">{{ row.directory_path || '/' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <span class="kb-file-type">{{ row.document_type || extOf(row.document_name) || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag
              v-if="row.status"
              :type="docStatusTagType(row.status)"
              size="small"
              effect="light"
            >
              <el-icon v-if="row.status === 'mining'" class="is-loading"><Loading /></el-icon>
              {{ docStatusLabel(row.status) }}
            </el-tag>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="上传时间" min-width="150">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text @click="download(row)">下载</el-button>
            <el-button v-if="canWrite" size="small" text @click="openRename(row)">改名</el-button>
            <el-tooltip
              v-if="canWrite"
              content="撤回功能待接（后端 §10）"
              placement="top"
            >
              <el-button size="small" text disabled>撤回</el-button>
            </el-tooltip>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState text="还没有文档，上传一个开始吧" />
        </template>
      </el-table>
    </div>

    <!-- Rename / 标类型 dialog -->
    <el-dialog v-model="renameOpen" title="编辑文档" width="420px">
      <el-form :model="renameForm" label-width="72px" @submit.prevent>
        <el-form-item label="文件名">
          <el-input v-model="renameForm.document_name" />
        </el-form-item>
        <el-form-item label="类型">
          <el-input v-model="renameForm.document_type" placeholder="可选，如 参考 / 规范" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="renameOpen = false">取消</el-button>
        <el-button type="primary" :loading="renaming" @click="submitRename">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Document, Loading, Refresh, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import { useKbApi } from '@/api/kb'
import { apiErrorDetail } from '@/api/proxyClient'
import { filenameFromDisposition, saveBlob } from '@/utils/download'
import EmptyState from '@/components/common/EmptyState.vue'
import { docStatusLabel, docStatusTagType } from '@/views/kb/kbMeta'
import type { KbDocument } from '@/types/kb'

const props = defineProps<{ kbId: string; canWrite: boolean }>()
const kbApi = useKbApi()

const ALL_DIRS = '__all__'
const docs = ref<KbDocument[]>([])
const loading = ref(false)
const uploading = ref(0)
const directoryFilter = ref<string>(ALL_DIRS)

const directoryOptions = computed(() => {
  const seen = new Map<string, number>()
  for (const d of docs.value) {
    const dir = d.directory_path || ''
    seen.set(dir, (seen.get(dir) ?? 0) + 1)
  }
  return Array.from(seen.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([value, count]) => ({
      value,
      label: `${value || '/'}（${count}）`,
    }))
})

async function load() {
  loading.value = true
  try {
    const dir = directoryFilter.value === ALL_DIRS ? undefined : directoryFilter.value
    // 目录筛选时先取全量再筛（后端按 directory 精确匹配，根目录是空串）
    docs.value = await kbApi.listDocuments(props.kbId, dir)
  } catch (e) {
    docs.value = []
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    loading.value = false
  }
}

async function handleUpload(opts: UploadRequestOptions) {
  const file = opts.file as File
  uploading.value += 1
  try {
    if (file.name.toLowerCase().endsWith('.zip')) {
      const created = await kbApi.uploadZip(props.kbId, file)
      ElMessage.success(`已解压上传 ${created.length} 个文档`)
    } else {
      await kbApi.uploadDocument(props.kbId, file)
      ElMessage.success(`已上传 ${file.name}`)
    }
    await load()
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    uploading.value -= 1
  }
}

async function download(row: KbDocument) {
  try {
    const blob = await kbApi.downloadDocument(props.kbId, row.id)
    saveBlob(blob, filenameFromDisposition(null, row.document_name))
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  }
}

// ── rename / 标类型 ──
const renameOpen = ref(false)
const renaming = ref(false)
const renameForm = ref<{ id: string; document_name: string; document_type: string }>({
  id: '', document_name: '', document_type: '',
})

function openRename(row: KbDocument) {
  renameForm.value = {
    id: row.id,
    document_name: row.document_name,
    document_type: row.document_type ?? '',
  }
  renameOpen.value = true
}

async function submitRename() {
  renaming.value = true
  try {
    await kbApi.patchDocument(props.kbId, renameForm.value.id, {
      document_name: renameForm.value.document_name.trim() || undefined,
      document_type: renameForm.value.document_type.trim() || undefined,
    })
    ElMessage.success('已保存')
    renameOpen.value = false
    await load()
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    renaming.value = false
  }
}

function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toLowerCase() : ''
}

function formatTime(t?: string): string {
  if (!t) return '-'
  return new Date(t).toLocaleString('zh-CN')
}

onMounted(load)
</script>

<style scoped>
.kb-files {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.kb-files__upload :deep(.el-upload-dragger) {
  width: 100%;
  padding: 20px;
  border-radius: var(--kb-radius);
}

.kb-files__toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.kb-files__dir-select {
  width: 220px;
}

.kb-files__count {
  font-size: 12px;
  color: var(--kb-text-tertiary);
}

.kb-files__table-wrap {
  background: var(--kb-bg-card);
  border-radius: var(--kb-radius);
  border: 1px solid var(--kb-border-light);
  overflow: hidden;
}

.kb-file-name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
  color: var(--kb-text-primary);
  font-size: 13px;
}

.kb-file-dir,
.kb-file-type {
  font-size: 12px;
  color: var(--kb-text-secondary);
  font-family: 'SF Mono', 'Cascadia Code', monospace;
}

.text-muted {
  color: var(--kb-text-tertiary);
}
</style>
