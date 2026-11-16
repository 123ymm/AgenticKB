<template>
  <!-- 67号 Task 10 占位壳：路由/导航/创建落地链路先通，三栏工作台在 Task 11 落地。 -->
  <div class="workspace-detail-stub">
    <el-button link data-test="back-to-workspaces" @click="router.push({ name: 'workspaces' })">
      ← 返回工作区列表
    </el-button>
    <el-skeleton v-if="loading" :rows="4" animated class="workspace-detail-stub__skeleton" />
    <EmptyState v-else :text="`「${workspace?.name ?? workspaceId}」的工作台编辑器建设中（Task 11 接线三栏步骤流）`" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useWorkspacesApi } from '@/api/workspaces'
import { apiErrorDetail } from '@/api/proxyClient'
import { useDocumentTitle } from '@/composables/useDocumentTitle'
import EmptyState from '@/components/common/EmptyState.vue'
import type { WorkspaceDetail } from '@/api/workspaces'

const props = defineProps<{ workspaceId: string }>()

const api = useWorkspacesApi()
const router = useRouter()
const workspace = ref<WorkspaceDetail | null>(null)
const loading = ref(false)

useDocumentTitle(() => workspace.value?.name || '制作工作台')

onMounted(async () => {
  loading.value = true
  try {
    workspace.value = await api.getWorkspace(props.workspaceId)
  } catch (error) {
    ElMessage.error(`加载工作区失败：${await apiErrorDetail(error)}`)
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.workspace-detail-stub { padding: 8px 2px; }
.workspace-detail-stub__skeleton { margin-top: 16px; }
</style>
