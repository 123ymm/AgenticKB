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
      <div v-if="artifact" class="artifact-detail__actions">
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
      <!-- ── 设计态：批次4b 填充（导入向导 / 列定义 / 行编辑三面板）── -->
      <el-tab-pane label="设计态" name="design">
        <!--
          下一棒（61号批次4b）子组件 props 接口约定：
          <ArtifactImportWizard :artifact-id="artifactId" :active="activeTab === 'design'"
                                @imported="onDesignChanged" />
            — 三步向导（上传 xlsx → 预览/列映射 → 导入），emit('imported', ImportReport)
          <ArtifactSchemaPanel :artifact-id="artifactId" :can-write="canWrite"
                               :columns="artifact.schema" @changed="reload" />
            — 列定义（加列/改列/主键/软删），emit('changed')
          <ArtifactRowsPanel :artifact-id="artifactId" :can-write="canWrite"
                             :columns="artifact.schema" @changed="reload" />
            — draft 行编辑（单格/批量赋值/删行），emit('changed')
          canWrite = my_role ∈ {owner, editor}（my_role 需从列表页路由参数或 members 面板带入）。
        -->
        <EmptyState text="导入向导 / 列定义 / 数据编辑将在下一批接入（批次4b）">
          <template #action>
            <el-button disabled>上传 Excel</el-button>
          </template>
        </EmptyState>
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

      <!-- ── 成员：批次4b 填充（ArtifactMembersPanel）── -->
      <el-tab-pane label="成员" name="members">
        <!--
          下一棒 props 接口约定：
          <ArtifactMembersPanel :artifact-id="artifactId" :can-write="canWrite" @changed="reload" />
            — 成员增删改（owner/editor/viewer），emit('changed')
        -->
        <EmptyState text="成员管理将在下一批接入（批次4b）" />
      </el-tab-pane>

      <!-- ── 版本：revisions 列表（本棒可用；回滚在批次4b 面板内做）── -->
      <el-tab-pane label="版本" name="versions">
        <el-table v-loading="revisionsLoading" :data="revisions" size="small">
          <el-table-column label="版本" width="80">
            <template #default="{ row }">
              r{{ row.revision }}
              <el-tag v-if="artifact && row.revision === artifact.current_revision" size="small" type="success">当前</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="row_count" label="行数" width="90" />
          <el-table-column prop="change_summary" label="变更摘要" min-width="200" show-overflow-tooltip />
          <el-table-column prop="published_by" label="发布人" width="130" />
          <el-table-column label="发布时间" width="170">
            <template #default="{ row }">{{ formatTime(row.published_at) }}</template>
          </el-table-column>
          <template #empty>
            <EmptyState text="尚未发布过版本" />
          </template>
        </el-table>
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

    <!-- 发布占位 dialog：diff 预览与确认在批次4b（Task 12）填充 -->
    <el-dialog v-model="publishVisible" title="发布制品" width="560px">
      <el-alert
        type="info"
        :closable="false"
        title="发布 diff 预览与确认将在下一批接入（批次4b）"
        description="届时此处先展示 draft vs current 的行级/列级差异（新增/删除/修改计数），再确认 change_summary 后调用 POST /publish。"
      />
      <el-form label-width="80px" class="artifact-detail__publish-form">
        <el-form-item label="变更摘要">
          <el-input v-model="publishSummary" type="textarea" :rows="2" placeholder="本次发布改了什么" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="publishVisible = false">取消</el-button>
        <el-button type="primary" disabled>确认发布（下一批开放）</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ArrowLeft } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import EmptyState from '@/components/common/EmptyState.vue'
import type { ArtifactColumn, ArtifactDetail, ArtifactRow, RevisionInfo } from '@/types/artifacts'

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

// 版本历史（revisions 倒序）
const revisions = ref<RevisionInfo[]>([])
const revisionsLoading = ref(false)
const revisionsLoaded = ref(false)

// 元信息编辑
const metaVisible = ref(false)
const metaSaving = ref(false)
const metaForm = ref({ title: '', description: '', usage_note: '' })

// 发布占位
const publishVisible = ref(false)
const publishSummary = ref('')

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

async function loadRevisions() {
  revisionsLoading.value = true
  try {
    revisions.value = await api.listRevisions(props.artifactId)
    revisionsLoaded.value = true
  } catch (error) {
    ElMessage.error(`加载版本历史失败：${await apiErrorDetail(error)}`)
  } finally {
    revisionsLoading.value = false
  }
}

// tab 首次切入再拉数据（运行态/版本懒加载）
watch(activeTab, (tab) => {
  if (tab === 'runtime' && !rowsLoaded.value) loadRuntimeRows()
  if (tab === 'versions' && !revisionsLoaded.value) loadRevisions()
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
    if (revisionsLoaded.value) await loadRevisions()
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

function formatTime(value: string | null): string {
  return value ? value.replace('T', ' ').slice(0, 19) : '—'
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
.artifact-detail__publish-form { margin-top: 12px; }
</style>
