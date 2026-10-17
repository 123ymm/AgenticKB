<template>
  <div class="versions-panel">
    <el-table v-loading="loading" :data="revisions" size="small">
      <el-table-column label="版本" width="90">
        <template #default="{ row }">
          r{{ row.revision }}
          <el-tag
            v-if="row.revision === currentRevision"
            size="small"
            type="success"
          >当前</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="row_count" label="行数" width="90" />
      <el-table-column prop="change_summary" label="变更摘要" min-width="200" show-overflow-tooltip />
      <el-table-column prop="published_by" label="发布人" width="140" />
      <el-table-column label="发布时间" width="170">
        <template #default="{ row }">{{ formatTime(row.published_at) }}</template>
      </el-table-column>
      <el-table-column v-if="canWrite" label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.revision !== currentRevision"
            size="small"
            text
            type="warning"
            data-test="rollback-btn"
            @click="rollbackTo(row)"
          >回滚</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <EmptyState text="尚未发布过版本" />
      </template>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import EmptyState from '@/components/common/EmptyState.vue'
import type { RevisionInfo } from '@/types/artifacts'

const props = defineProps<{
  artifactId: string
  canWrite: boolean
  currentRevision: number
  /** 乐观锁令牌（artifact.updated_at）。 */
  expectedUpdatedAt?: string
}>()

const emit = defineEmits<{ changed: [] }>()

const api = useArtifactsApi()

const revisions = ref<RevisionInfo[]>([])
const loading = ref(false)

onMounted(loadRevisions)

async function loadRevisions(): Promise<void> {
  loading.value = true
  try {
    revisions.value = await api.listRevisions(props.artifactId)
  } catch (error) {
    revisions.value = []
    ElMessage.error(`加载版本历史失败：${await apiErrorDetail(error)}`)
  } finally {
    loading.value = false
  }
}

/** 回滚 = 拨指针（数据不动）：current_revision 直接指回历史版，运行态立即生效。 */
async function rollbackTo(row: RevisionInfo): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `回滚到 r${row.revision}？当前指针将拨回该版本（数据不动，历史版本保留），运行态立即以 r${row.revision} 为准。`,
      '回滚版本', { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.rollback(props.artifactId, row.revision, props.expectedUpdatedAt)
    ElMessage.success(`已回滚：当前版本 = r${row.revision}`)
    emit('changed')
    await loadRevisions()
  } catch (error) {
    const resp = (error as { response?: { status?: number; data?: { detail?: { code?: string } } } })?.response
    if (resp?.status === 409 && resp.data?.detail?.code === 'stale_artifact') {
      ElMessage.error('制品已被他人修改（409 stale_artifact），已为你刷新——请重试')
      emit('changed')
    } else {
      ElMessage.error(await apiErrorDetail(error))
    }
  }
}

function formatTime(value: string | null): string {
  return value ? value.replace('T', ' ').slice(0, 19) : '—'
}

defineExpose({ rollbackTo, loadRevisions })
</script>

<style scoped>
.versions-panel { display: flex; flex-direction: column; gap: 10px; }
</style>
