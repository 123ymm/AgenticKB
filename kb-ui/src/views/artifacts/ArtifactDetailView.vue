<template>
  <div class="artifact-detail">
    <!-- Header：标题 + 状态/类型 + 生命周期操作 -->
    <div class="artifact-detail__header">
      <div class="artifact-detail__head-left">
        <router-link to="/artifacts" class="artifact-back">
          <el-icon><ArrowLeft /></el-icon>
          <span>知识制品</span>
        </router-link>
        <h2 class="artifact-detail__title">{{ artifact?.title || '加载中…' }}</h2>
        <el-tag v-if="artifact" size="small" :type="statusTagType(artifact.status)">
          {{ statusLabel(artifact.status) }}
        </el-tag>
        <el-tag v-if="artifact" size="small" effect="plain">
          {{ artifact.kind === 'wiki' ? 'Wiki' : '表格' }}
        </el-tag>
        <el-tag v-if="artifact?.draft_open" size="small" type="warning" effect="plain">
          draft 开放中
        </el-tag>
      </div>
      <div v-if="artifact && canWrite" class="artifact-detail__actions">
        <el-button type="primary" data-test="open-publish" @click="publishVisible = true">发布</el-button>
        <el-button v-if="artifact.status === 'published'" @click="deprecateArtifact">停用</el-button>
        <el-button v-if="artifact.status === 'deprecated'" @click="republishArtifact">恢复</el-button>
        <el-button v-if="artifact.draft_open" @click="discardDraftAction">丢弃 draft</el-button>
      </div>
    </div>

    <p v-if="artifact?.description" class="artifact-detail__desc">{{ artifact.description }}</p>

    <!-- Not found / no access -->
    <div v-if="!loading && loadError" class="artifact-detail__missing">
      <EmptyState :text="loadError" />
      <el-button type="primary" @click="reload">重试</el-button>
    </div>
    <div v-else-if="!loading && !artifact" class="artifact-detail__missing">
      <EmptyState text="制品不存在或无权访问" />
      <router-link to="/artifacts">
        <el-button type="primary">返回列表</el-button>
      </router-link>
    </div>

    <el-tabs v-if="artifact" v-model="activeTab" class="artifact-detail__tabs">
      <!-- ── 设计态：导入向导 / 列定义 / 行编辑三面板（owner+editor；viewer 无设计态）── -->
      <el-tab-pane label="设计态" name="design">
        <template v-if="canWrite">
          <ArtifactImportWizard
            :artifact-id="artifactId"
            :active="activeTab === 'design'"
            @imported="onDesignChanged"
          />
          <el-divider content-position="left">列定义</el-divider>
          <ArtifactSchemaPanel
            :artifact-id="artifactId"
            :can-write="canWrite"
            :columns="artifact.schema"
            :expected-updated-at="artifact.updated_at"
            @changed="refreshAfterMutation"
          />
          <el-divider content-position="left">数据编辑</el-divider>
          <ArtifactRowsPanel
            ref="rowsPanel"
            :artifact-id="artifactId"
            :can-write="canWrite"
            :columns="artifact.schema"
            :expected-updated-at="artifact.updated_at"
            :draft-open="artifact.draft_open"
            @changed="reload"
          />
        </template>
        <EmptyState v-else text="设计态仅 owner / editor 可见（你当前是 viewer——请看运行态）" />
      </el-tab-pane>

      <!-- ── 运行态：说明卡 + 只读行表格（本棒可用）── -->
      <el-tab-pane label="运行态" name="runtime">
        <div class="artifact-detail__meta-bar">
          <el-descriptions :column="1" border size="small" class="artifact-detail__desc-card">
            <el-descriptions-item label="标题">{{ artifact.title }}</el-descriptions-item>
            <el-descriptions-item label="描述">{{ artifact.description || '—' }}</el-descriptions-item>
            <el-descriptions-item label="使用说明">
              <span :class="{ 'artifact-detail__usage-missing': !artifact.usage_note }">
                {{ artifact.usage_note || '未填写（发布前必填）' }}
              </span>
            </el-descriptions-item>
            <el-descriptions-item label="当前版本">
              {{ artifact.current_revision ? `r${artifact.current_revision}` : '未发布' }}
            </el-descriptions-item>
          </el-descriptions>
          <el-button data-test="edit-meta" @click="openMetaEdit">编辑元信息</el-button>
        </div>

        <el-table v-loading="rowsLoading" :data="runtimeRows" size="small">
          <el-table-column prop="row_key" label="行键" min-width="160" show-overflow-tooltip />
          <el-table-column
            v-for="col in runtimeColumns"
            :key="col.column_id"
            :label="col.column_name"
            min-width="130"
            show-overflow-tooltip
          >
            <template #default="{ row }">{{ displayValue(row.data[col.column_name]) }}</template>
          </el-table-column>
          <template #empty>
            <EmptyState text="暂无已发布数据（发布后在运行态可见）" />
          </template>
        </el-table>
        <el-pagination
          v-if="rowsTotal > rowsPageSize"
          class="artifact-detail__pager"
          layout="total, prev, pager, next"
          :total="rowsTotal"
          :page-size="rowsPageSize"
          :current-page="rowsPage"
          @current-change="onRowsPageChange"
        />
      </el-tab-pane>

      <!-- ── 成员：成员管理仅 owner（后端 ROLE_OWNER_ONLY）── -->
      <el-tab-pane label="成员" name="members" lazy>
        <ArtifactMembersPanel
          :artifact-id="artifactId"
          :can-manage="isOwner"
          @changed="reload"
        />
      </el-tab-pane>

      <!-- ── 版本：revisions 列表 + 回滚（面板内）── -->
      <el-tab-pane label="版本" name="versions" lazy>
        <ArtifactVersionsPanel
          ref="versionsPanel"
          :artifact-id="artifactId"
          :can-write="canWrite"
          :current-revision="artifact.current_revision"
          :expected-updated-at="artifact.updated_at"
          @changed="refreshAfterMutation"
        />
      </el-tab-pane>
    </el-tabs>

    <!-- 编辑元信息（PATCH：title/description/usage_note + 乐观锁） -->
    <el-dialog v-model="metaVisible" title="编辑元信息" width="560px">
      <el-form label-width="80px">
        <el-form-item label="标题">
          <el-input v-model="metaForm.title" maxlength="200" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="metaForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="使用说明">
          <el-input
            v-model="metaForm.usage_note"
            type="textarea"
            :rows="3"
            placeholder="给 Agent 使用者的说明——发布前必填（usage_note 门禁）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="metaVisible = false">取消</el-button>
        <el-button type="primary" :loading="metaSaving" data-test="submit-meta" @click="saveMeta">保存</el-button>
      </template>
    </el-dialog>

    <!-- 发布：diff 三段预览 + usage_note 门禁 + change_summary（Task 12） -->
    <ArtifactPublishDialog
      v-model="publishVisible"
      :artifact-id="artifactId"
      :expected-updated-at="artifact?.updated_at"
      :usage-note="artifact?.usage_note || ''"
      @published="onPublished"
      @changed="reload"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ArrowLeft } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import EmptyState from '@/components/common/EmptyState.vue'
import ArtifactImportWizard from '@/components/artifacts/ArtifactImportWizard.vue'
import ArtifactSchemaPanel from '@/components/artifacts/ArtifactSchemaPanel.vue'
import ArtifactRowsPanel from '@/components/artifacts/ArtifactRowsPanel.vue'
import ArtifactMembersPanel from '@/components/artifacts/ArtifactMembersPanel.vue'
import ArtifactVersionsPanel from '@/components/artifacts/ArtifactVersionsPanel.vue'
import ArtifactPublishDialog from '@/components/artifacts/ArtifactPublishDialog.vue'
import type { ArtifactColumn, ArtifactDetail, ArtifactRow, ImportReport, PublishResult } from '@/types/artifacts'

const props = defineProps<{ artifactId: string }>()

const api = useArtifactsApi()

const artifact = ref<ArtifactDetail | null>(null)
const loading = ref(false)
const loadError = ref('')
const activeTab = ref<'design' | 'runtime' | 'members' | 'versions'>('design')

// 运行态只读行（非 draft，服务端分页）
const runtimeRows = ref<ArtifactRow[]>([])
const rowsTotal = ref(0)
const rowsPage = ref(1)
const rowsPageSize = 20
const rowsLoading = ref(false)
const rowsLoaded = ref(false)

// 版本面板（发布后命令式刷新 revisions）+ 行面板（跨面板变更后刷新行）
const versionsPanel = ref<InstanceType<typeof ArtifactVersionsPanel> | null>(null)
const rowsPanel = ref<InstanceType<typeof ArtifactRowsPanel> | null>(null)

// 元信息编辑
const metaVisible = ref(false)
const metaSaving = ref(false)
const metaForm = ref({ title: '', description: '', usage_note: '' })

// 发布
const publishVisible = ref(false)

/** canWrite = my_role ∈ {owner, editor}（详情响应附带；admin 后端归一 owner）。 */
const canWrite = computed(() => artifact.value?.my_role === 'owner' || artifact.value?.my_role === 'editor')
/** 成员管理仅 owner（后端 ROLE_OWNER_ONLY）。 */
const isOwner = computed(() => artifact.value?.my_role === 'owner')

/** 只读行列 = 详情 schema（viewer 已由后端回落到发布快照列）。 */
const runtimeColumns = computed<ArtifactColumn[]>(() => artifact.value?.schema ?? [])

async function reload() {
  loading.value = true
  loadError.value = ''
  try {
    artifact.value = await api.get(props.artifactId)
  } catch (error) {
    artifact.value = null
    loadError.value = `加载失败：${await apiErrorDetail(error)}`
  } finally {
    loading.value = false
  }
}

/** 跨面板变更（导入/列变更/回滚/发布）后：重载详情（乐观锁令牌+schema）并刷新行面板。 */
async function refreshAfterMutation() {
  await reload()
  rowsPanel.value?.loadRows()
}

/** 导入向导回报：schema/行数都变了——重载详情并刷新行编辑面板。 */
async function onDesignChanged(_report: ImportReport) {
  await refreshAfterMutation()
}

/** 发布成功：详情 + 运行态 + 行面板 + 版本面板刷新。 */
async function onPublished(_result: PublishResult) {
  await refreshAfterMutation()
  if (rowsLoaded.value) await loadRuntimeRows()
  versionsPanel.value?.loadRevisions()
}

async function loadRuntimeRows() {
  rowsLoading.value = true
  try {
    const page = await api.listRows(props.artifactId, {
      page: rowsPage.value, size: rowsPageSize,
    })
    runtimeRows.value = page.rows
    rowsTotal.value = page.total
    rowsLoaded.value = true
  } catch (error) {
    ElMessage.error(`加载运行态数据失败：${await apiErrorDetail(error)}`)
  } finally {
    rowsLoading.value = false
  }
}

// tab 首次切入再拉数据（运行态懒加载；成员/版本面板走 el-tab-pane lazy 自加载）
watch(activeTab, (tab) => {
  if (tab === 'runtime' && !rowsLoaded.value) loadRuntimeRows()
})

function onRowsPageChange(page: number) {
  rowsPage.value = page
  loadRuntimeRows()
}

function openMetaEdit() {
  if (!artifact.value) return
  metaForm.value = {
    title: artifact.value.title,
    description: artifact.value.description || '',
    usage_note: artifact.value.usage_note || '',
  }
  metaVisible.value = true
}

async function saveMeta() {
  if (!artifact.value) return
  const title = metaForm.value.title.trim()
  if (!title) {
    ElMessage.warning('标题不能为空')
    return
  }
  metaSaving.value = true
  try {
    const updated = await api.updateMeta(props.artifactId, {
      title,
      description: metaForm.value.description.trim(),
      usage_note: metaForm.value.usage_note.trim(),
      expected_updated_at: artifact.value.updated_at,
    })
    artifact.value = { ...artifact.value, ...updated }
    metaVisible.value = false
    ElMessage.success('元信息已保存')
  } catch (error) {
    ElMessage.error(`保存失败：${await apiErrorDetail(error)}`)
  } finally {
    metaSaving.value = false
  }
}

/** 简单生命周期动作（停用/恢复/丢弃）：确认 → POST → 重载。 */
async function lifecycleAction(fn: () => Promise<unknown>, confirmText: string) {
  try {
    await ElMessageBox.confirm(confirmText, { type: 'warning' })
  } catch {
    return
  }
  try {
    await fn()
    ElMessage.success('操作成功')
    await reload()
    if (rowsLoaded.value) await loadRuntimeRows()
    versionsPanel.value?.loadRevisions()
  } catch (error) {
    ElMessage.error(await apiErrorDetail(error))
  }
}

function deprecateArtifact() {
  if (!artifact.value) return
  void lifecycleAction(
    () => api.deprecate(props.artifactId, artifact.value?.updated_at),
    '停用后 Agent 侧不可见（deprecated），可随时恢复。确认停用？',
  )
}

function republishArtifact() {
  if (!artifact.value) return
  void lifecycleAction(
    () => api.republish(props.artifactId, artifact.value?.updated_at),
    '恢复发布后 Agent 侧重新可见。确认恢复？',
  )
}

function discardDraftAction() {
  if (!artifact.value) return
  void lifecycleAction(
    () => api.discardDraft(props.artifactId, artifact.value?.updated_at),
    '丢弃 draft 将放弃所有未发布的修改（回到当前已发布版本）。确认丢弃？',
  )
}

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

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

onMounted(reload)
</script>

<style scoped>
.artifact-detail__header { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.artifact-detail__head-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.artifact-detail__title { margin: 0; font-size: 20px; }
.artifact-back { display: inline-flex; align-items: center; gap: 4px; color: var(--kb-text-tertiary); text-decoration: none; font-size: 13px; }
.artifact-back:hover { color: var(--kb-accent, #2563eb); }
.artifact-detail__desc { margin: 8px 0 12px; color: var(--kb-text-tertiary); font-size: 13px; }
.artifact-detail__missing { display: flex; flex-direction: column; align-items: center; gap: 12px; }
.artifact-detail__meta-bar { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 12px; }
.artifact-detail__desc-card { flex: 1; max-width: 720px; }
.artifact-detail__usage-missing { color: var(--el-color-warning); }
.artifact-detail__pager { margin-top: 12px; justify-content: flex-end; }
</style>
