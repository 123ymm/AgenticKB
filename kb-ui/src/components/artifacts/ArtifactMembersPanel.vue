<template>
  <div class="art-members">
    <div v-if="canManage" class="art-members__add">
      <el-select
        v-model="newUserId"
        filterable
        remote
        clearable
        :remote-method="searchUsers"
        :loading="searching"
        placeholder="搜索本域用户（登录名 / 显示名）"
        class="art-members__user-input"
        data-test="member-candidate"
      >
        <el-option
          v-for="u in candidates"
          :key="u.id"
          :value="u.id"
          :label="`${u.display_name || u.username}（${u.username}）`"
          :disabled="memberIds.has(u.id)"
        />
      </el-select>
      <el-select v-model="newRole" size="default" class="art-members__role">
        <el-option label="只读 viewer" value="viewer" />
        <el-option label="编辑者 editor" value="editor" />
        <el-option label="拥有者 owner" value="owner" />
      </el-select>
      <el-button type="primary" :loading="adding" data-test="member-add" @click="addOne">
        添加成员
      </el-button>
    </div>
    <p v-else class="art-members__hint">仅拥有者（owner）可管理成员。</p>
    <p class="art-members__hint">非成员不可见本制品（404 反探测）；viewer 只见运行态，不见设计态。</p>

    <el-table v-loading="loading" :data="members" size="small">
      <el-table-column label="用户" min-width="200">
        <template #default="{ row }">
          <span class="art-members__name">{{ row.user_name || row.username || row.user_id }}</span>
          <span v-if="row.username && row.username !== (row.user_name || row.user_id)" class="art-members__username">
            {{ row.username }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="角色" width="140">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="roleTagType(row.role)">
            {{ roleLabel(row.role) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column v-if="canManage" label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="danger" @click="removeOne(row)">移除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <EmptyState text="尚无成员行（创建者由 owner_user_id 兜底，可不在列表中）" />
      </template>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { useAuthApi } from '@/api/auth'
import { apiErrorDetail } from '@/api/proxyClient'
import { useDomainStore } from '@/stores/domain'
import EmptyState from '@/components/common/EmptyState.vue'
import type { ArtifactRole, MemberInfo } from '@/types/artifacts'

const props = defineProps<{
  artifactId: string
  /** 成员管理后端仅 owner（ROLE_OWNER_ONLY）——editor 传入 false 隐藏入口。 */
  canManage: boolean
}>()

const emit = defineEmits<{ changed: [] }>()

const api = useArtifactsApi()
const authApi = useAuthApi()
const domainStore = useDomainStore()

const members = ref<MemberInfo[]>([])
const loading = ref(false)
const adding = ref(false)
const newUserId = ref('')
const newRole = ref<ArtifactRole>('viewer')

// 008 UI：候选搜索（复用 55号域用户候选接口）——user_id 是内部 id，
// 手填 uuid 不现实；下拉按登录名/显示名搜索，提交时传 id。
const candidates = ref<Array<{ id: string; username: string; display_name: string | null }>>([])
const searching = ref(false)

const memberIds = computed(() => new Set(members.value.map(m => m.user_id)))

onMounted(loadMembers)

async function loadMembers(): Promise<void> {
  loading.value = true
  try {
    members.value = await api.listMembers(props.artifactId)
  } catch (error) {
    members.value = []
    ElMessage.error(`加载成员失败：${await apiErrorDetail(error)}`)
  } finally {
    loading.value = false
  }
}

async function searchUsers(query: string): Promise<void> {
  const domain = domainStore.currentDomain
  if (!query.trim() || !domain) {
    candidates.value = []
    return
  }
  searching.value = true
  try {
    candidates.value = await authApi.listDomainUserCandidates(domain, query)
  } catch {
    candidates.value = []
  } finally {
    searching.value = false
  }
}

async function addOne(): Promise<void> {
  const userId = newUserId.value
  if (!userId) {
    ElMessage.warning('请先搜索并选择用户')
    return
  }
  adding.value = true
  try {
    await api.addMember(props.artifactId, userId, newRole.value)
    ElMessage.success(`已添加（${newRole.value}）`)
    newUserId.value = ''
    await loadMembers()
    emit('changed')
  } catch (error) {
    ElMessage.error(await apiErrorDetail(error))
  } finally {
    adding.value = false
  }
}

async function removeOne(row: MemberInfo): Promise<void> {
  const label = row.user_name || row.username || row.user_id
  try {
    await ElMessageBox.confirm(
      `确定将 ${label} 移出制品？${row.role === 'owner' ? '（最后一个 owner 不可移除）' : ''}`,
      '移除成员', { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await api.removeMember(props.artifactId, row.user_id)
    ElMessage.success(`已移除 ${label}`)
    await loadMembers()
    emit('changed')
  } catch (error) {
    ElMessage.error(await apiErrorDetail(error))
  }
}

function roleLabel(role: string): string {
  if (role === 'owner') return '拥有者'
  if (role === 'editor') return '编辑者'
  return '只读'
}

function roleTagType(role: string): 'danger' | 'warning' | 'info' {
  if (role === 'owner') return 'danger'
  if (role === 'editor') return 'warning'
  return 'info'
}

defineExpose({ addOne, removeOne })
</script>

<style scoped>
.art-members { display: flex; flex-direction: column; gap: 10px; }
.art-members__add { display: flex; gap: 8px; align-items: center; }
.art-members__user-input { width: 300px; }
.art-members__role { width: 150px; }
.art-members__hint { font-size: 12.5px; color: var(--kb-text-tertiary); margin: 0; }
.art-members__name { font-weight: 550; }
.art-members__username { margin-left: 8px; color: var(--kb-text-tertiary); font-size: 12.5px; }
</style>
