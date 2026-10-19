<template>
  <div class="artifact-list">
    <header class="artifact-list__header">
      <div>
        <p>上传 Excel 建表发布，供 Agent 结构化查询；列表=我参与的 + 本域公开制品（管理员见全部）。</p>
      </div>
      <el-button type="primary" data-test="create-artifact" @click="openCreate">新建制品</el-button>
    </header>

    <el-table v-loading="loading" :data="artifacts">
      <el-table-column prop="title" label="标题" min-width="200">
        <template #default="{ row }">
          <button class="artifact-list__link" type="button" @click="open(row.artifact_id)">{{ row.title }}</button>
        </template>
      </el-table-column>
      <el-table-column label="类型" width="90">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ row.kind === 'wiki' ? 'Wiki' : '表格' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="可见性" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.visibility === 'public' ? 'success' : 'info'" effect="plain">
            {{ row.visibility === 'public' ? '公开' : '私有' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="当前版本" width="100">
        <template #default="{ row }">{{ row.current_revision ? `r${row.current_revision}` : '未发布' }}</template>
      </el-table-column>
      <el-table-column label="我的角色" width="100">
        <template #default="{ row }">{{ row.my_role ? roleLabel(row.my_role) : '—' }}</template>
      </el-table-column>
      <el-table-column label="更新时间" width="170">
        <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="open(row.artifact_id)">进入详情</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <EmptyState text="还没有知识制品，点右上角「新建制品」从上传 Excel 开始" />
      </template>
    </el-table>

    <el-dialog v-model="createVisible" title="新建知识制品" width="520px">
      <el-form label-width="70px">
        <el-form-item label="标题">
          <el-input v-model="form.title" placeholder="如：MOP 责任域表" maxlength="200" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="3" placeholder="这份表给谁用、解决什么问题（可选）" />
        </el-form-item>
        <el-form-item label="归属域">
          <el-select
            v-model="form.domainId"
            placeholder="选择知识域"
            style="width: 100%"
            data-test="create-artifact-domain"
          >
            <el-option
              v-for="d in domainStore.enabledDomains"
              :key="d.domain_id"
              :value="d.domain_id"
              :label="d.domain_id"
            />
          </el-select>
          <div class="artifact-list__domain-hint">制品将创建到该知识域（默认当前域：{{ domainStore.currentDomain || '未知' }}）</div>
        </el-form-item>
        <p class="artifact-list__hint">创建后进入设计态：上传 Excel → 列映射 → 导入 → 填使用说明 → 发布。</p>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" data-test="submit-create-artifact" @click="createArtifact">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import { useDomainStore } from '@/stores/domain'
import EmptyState from '@/components/common/EmptyState.vue'
import type { Artifact } from '@/types/artifacts'

const api = useArtifactsApi()
const router = useRouter()
const domainStore = useDomainStore()

const artifacts = ref<Artifact[]>([])
const loading = ref(false)
const createVisible = ref(false)
const creating = ref(false)
const form = ref({ title: '', description: '', domainId: '' })

async function load() {
  loading.value = true
  try {
    artifacts.value = await api.list()
  } catch (error) {
    ElMessage.error(`加载知识制品失败：${await apiErrorDetail(error)}`)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  // codex二审功能项③：显式带当前域创建——不再依赖后端 first_domain_for_user
  // 兜底（多域用户在 B 域页面会落到 A 域）。域选择对齐 Header/McpAccessView
  // 的 domain store 机制（enabledDomains），不新造状态源。
  form.value = { title: '', description: '', domainId: domainStore.currentDomain }
  createVisible.value = true
}

async function createArtifact() {
  const title = form.value.title.trim()
  if (!title) {
    ElMessage.warning('请填写标题')
    return
  }
  if (!form.value.domainId) {
    ElMessage.warning('无法确定归属知识域，请在新建窗口选择目标域')
    return
  }
  creating.value = true
  try {
    const artifact = await api.create({
      title,
      description: form.value.description.trim() || undefined,
      domain_id: form.value.domainId,
    })
    createVisible.value = false
    await router.push({ name: 'artifact-detail', params: { artifactId: artifact.artifact_id } })
  } catch (error) {
    ElMessage.error(`创建失败：${await apiErrorDetail(error)}`)
  } finally {
    creating.value = false
  }
}

function open(artifactId: string) {
  router.push({ name: 'artifact-detail', params: { artifactId } })
}

/** 状态 tag 映射：draft 灰 / published 绿 / deprecated 红（测试钉死）。 */
function statusTagType(status: string): 'info' | 'success' | 'danger' {
  if (status === 'published') return 'success'
  if (status === 'deprecated') return 'danger'
  return 'info'
}

function statusLabel(status: string): string {
  if (status === 'published') return '已发布'
  if (status === 'deprecated') return '已停用'
  return '草稿'
}

function roleLabel(role: string): string {
  return role === 'owner' ? '拥有者' : role === 'editor' ? '编辑者' : '查看者'
}

function formatTime(value: string | null): string {
  return value ? value.replace('T', ' ').slice(0, 19) : '—'
}

onMounted(load)
</script>

<style scoped>
.artifact-list { padding: 4px; }
.artifact-list__header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
.artifact-list__header p { margin: 5px 0 0; color: var(--kb-text-tertiary); font-size: 13px; }
.artifact-list__link { border: 0; background: none; padding: 0; color: var(--kb-accent, #2563eb); font-weight: 650; cursor: pointer; }
.artifact-list__hint { margin: -5px 0 0 70px; color: var(--kb-text-tertiary); font-size: 12px; }
.artifact-list__domain-hint { width: 100%; color: var(--kb-text-tertiary); font-size: 12px; }
</style>
