<template>
  <div class="workspace-list">
    <!-- 页头零重复（1.1.37 用户拍板）：页名「制作工作台」只在全局页头出现一次，
         页内只留一句范围说明 + 新建入口。 -->
    <header class="workspace-list__header">
      <p class="workspace-list__subtitle">模板驱动的知识制品制作间 · 接入资料 → 算子流水线 → 导出制品草稿（归档后详情仍可读）</p>
      <el-button type="primary" data-test="create-workspace" @click="openCreate">新建工作区</el-button>
    </header>

    <el-table v-loading="loading" :data="workspaces" class="workspace-list__table">
      <el-table-column prop="name" label="工作区" min-width="280">
        <template #default="{ row }">
          <button class="workspace-list__name" type="button" @click="open(row.id)">
            <span class="workspace-list__kindicon" aria-hidden="true">W</span>
            <span class="workspace-list__name-text">
              <span class="workspace-list__link">{{ row.name }}</span>
              <span v-if="row.description" class="workspace-list__desc">{{ row.description }}</span>
            </span>
          </button>
        </template>
      </el-table-column>
      <el-table-column label="负责人" min-width="140">
        <template #default="{ row }">{{ ownerLabel(row) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'" effect="plain">
            {{ row.status === 'active' ? '使用中' : '已归档' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="批次" width="70" align="center">
        <template #default="{ row }">{{ row.batch_no }}</template>
      </el-table-column>
      <el-table-column label="更新时间" width="160">
        <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" align="center">
        <template #default="{ row }">
          <!-- 两态 DELETE（用户反馈问题1）：active → 归档；已归档 → 真删 -->
          <el-button
            link
            type="danger"
            size="small"
            :data-test="row.status === 'active' ? 'archive-workspace' : 'delete-workspace'"
            @click="removeWorkspace(row)"
          >{{ row.status === 'active' ? '归档' : '删除' }}</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <EmptyState :text="emptyText">
          <el-button type="primary" plain data-test="empty-create-workspace" @click="openCreate">新建工作区</el-button>
        </EmptyState>
      </template>
    </el-table>

    <el-dialog v-model="createVisible" title="新建工作区" width="520px">
      <el-form label-width="70px">
        <el-form-item label="名称" required>
          <el-input
            v-model="form.name"
            placeholder="如：网络集成制品工坊"
            maxlength="100"
            show-word-limit
            data-test="create-workspace-name"
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            placeholder="这个工作区产出什么制品、给谁用（可选）"
            maxlength="500"
            show-word-limit
          />
        </el-form-item>
      </el-form>
      <p class="workspace-list__hint">创建后进入工作台：接入资料（知识库目录 / Excel）→ 组装算子 → 运行 → 导出为制品草稿。</p>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" data-test="submit-create-workspace" @click="createWorkspace">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useWorkspacesApi } from '@/api/workspaces'
import { apiErrorDetail } from '@/api/proxyClient'
import { useDomainStore } from '@/stores/domain'
import { useDocumentTitle } from '@/composables/useDocumentTitle'
import EmptyState from '@/components/common/EmptyState.vue'
import type { WorkspaceSummary } from '@/api/workspaces'

const api = useWorkspacesApi()
const router = useRouter()
const domainStore = useDomainStore()

useDocumentTitle(() => '制作工作台')

const workspaces = ref<WorkspaceSummary[]>([])
const loading = ref(false)
let loadGeneration = 0
const createVisible = ref(false)
const creating = ref(false)
const form = ref({ name: '', description: '' })

const emptyText = '还没有工作区，点右上角「新建工作区」开始制作知识制品'

async function load() {
  const generation = ++loadGeneration
  const domainId = domainStore.currentDomain
  workspaces.value = []
  if (!domainId) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    // 显式带当前域（同制品列表口径——不依赖后端 first_domain_for_user 兜底）
    const result = await api.listWorkspaces(domainId)
    if (generation === loadGeneration && domainId === domainStore.currentDomain) {
      workspaces.value = result
    }
  } catch (error) {
    if (generation === loadGeneration) {
      ElMessage.error(`加载工作区失败：${await apiErrorDetail(error)}`)
    }
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

function openCreate() {
  form.value = { name: '', description: '' }
  createVisible.value = true
}

async function createWorkspace() {
  const name = form.value.name.trim()
  if (!name) {
    ElMessage.warning('请填写名称')
    return
  }
  const domainId = domainStore.currentDomain
  if (!domainId) {
    ElMessage.warning('无法确定归属知识域，请先在页头选择知识域')
    return
  }
  creating.value = true
  try {
    const workspace = await api.createWorkspace({
      name,
      description: form.value.description.trim() || undefined,
      domain: domainId,
    })
    createVisible.value = false
    await router.push({ name: 'workspace-detail', params: { workspaceId: workspace.id } })
  } catch (error) {
    ElMessage.error(`创建失败：${await apiErrorDetail(error)}`)
  } finally {
    creating.value = false
  }
}

function open(workspaceId: string) {
  router.push({ name: 'workspace-detail', params: { workspaceId } })
}

/** 负责人显示（用户测试反馈修复1）：owner_username 回落 owner_user_id 前 8 位。 */
function ownerLabel(row: WorkspaceSummary): string {
  return row.owner_username || row.owner_user_id.slice(0, 8) || '—'
}

/**
 * 行操作（两态 DELETE，用户反馈问题1——两步门禁）：active → 归档（就地换
 * 返回行）；已归档 → 真删（从列表移除该行——已导出的制品不受影响）。
 * owner/admin；confirm 后调 DELETE，不整页重拉。
 */
async function removeWorkspace(row: WorkspaceSummary) {
  const isArchived = row.status !== 'active'
  try {
    await ElMessageBox.confirm(
      isArchived
        ? `永久删除工作区「${row.name}」及其全部来源、运行历史与输出，不可恢复；已导出的制品不受影响。`
        : `归档后「${row.name}」不再出现在日常使用中（详情仍可读），确定归档？`,
      isArchived ? '删除工作区' : '归档工作区',
      {
        confirmButtonText: isArchived ? '永久删除' : '归档',
        cancelButtonText: '取消',
        type: isArchived ? 'error' : 'warning',
      })
  } catch {
    return
  }
  try {
    const result = await api.deleteWorkspace(row.id)
    if (result.status === 'deleted') {
      workspaces.value = workspaces.value.filter(w => w.id !== row.id)
      ElMessage.success('已删除')
    } else {
      workspaces.value = workspaces.value.map(
        w => (w.id === result.id ? { ...w, ...result } : w))
      ElMessage.success('已归档')
    }
  } catch (error) {
    ElMessage.error(`${isArchived ? '删除' : '归档'}失败：${await apiErrorDetail(error)}`)
  }
}

function formatTime(value: string | null): string {
  return value ? value.replace('T', ' ').slice(0, 19) : '—'
}

watch(() => domainStore.currentDomain, load, { immediate: true })
</script>

<style scoped>
.workspace-list { padding: 4px 2px; }

/* ── 页头零重复：只留说明行 + 新建按钮（页名由全局页头唯一承载） ── */
.workspace-list__header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 14px; }
.workspace-list__subtitle { margin: 0; color: var(--kb-text-tertiary); font-size: 12px; }

/* ── 表格：发丝线 + 软表头 + 名称双行（同制品列表 ar-list-table 质感） ── */
.workspace-list__table :deep(th.el-table__cell) {
  background: var(--kb-bg-card-hover, #f8fafc);
  color: var(--kb-text-secondary, #64748b);
  font-weight: 500; font-size: 11.5px;
}
.workspace-list__table :deep(td.el-table__cell) { padding: 10px 0; }
.workspace-list__table :deep(.el-table__row:hover td.el-table__cell) { background: var(--kb-bg-card-hover, #f8fafc); }
.workspace-list__name {
  display: flex; align-items: center; gap: 10px;
  border: 0; background: none; padding: 2px 4px;
  cursor: pointer; text-align: left; min-width: 0;
}
.workspace-list__kindicon {
  display: inline-grid; place-items: center; flex-shrink: 0;
  width: 32px; height: 32px; border-radius: 8px;
  border: 1px solid var(--kb-border, #e2e8f0);
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.08));
  color: var(--kb-accent, #0891b2);
  font-size: 12px; font-weight: 700;
}
.workspace-list__name-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.workspace-list__link {
  border: 0; background: none; padding: 0; text-align: left;
  color: var(--kb-text-primary, inherit); font-weight: 600; font-size: 13.5px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.workspace-list__name:hover .workspace-list__link { color: var(--kb-accent, #0891b2); }
.workspace-list__desc {
  color: var(--kb-text-tertiary); font-size: 11.5px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.workspace-list__hint { margin: -5px 0 0 70px; color: var(--kb-text-tertiary); font-size: 12px; }
</style>
