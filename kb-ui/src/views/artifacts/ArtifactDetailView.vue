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
        <el-tag v-if="artifact" size="small" :type="artifact.visibility === 'public' ? 'success' : 'info'">
          {{ artifact.visibility === 'public' ? '公开' : '私有' }}
        </el-tag>
      </div>
      <div v-if="artifact && canWrite" class="artifact-detail__actions">
        <el-tooltip
          content="无待发布变更（编辑数据或改列后自动检出草稿）"
          :disabled="hasPendingChanges"
          placement="bottom"
        >
          <span>
            <el-button
              type="primary"
              data-test="open-publish"
              :disabled="!hasPendingChanges"
              @click="publishVisible = true"
            >发布</el-button>
          </span>
        </el-tooltip>
        <el-button v-if="artifact.status !== 'deprecated'" @click="deprecateArtifact">停用</el-button>
        <el-button v-if="artifact.status === 'deprecated'" @click="republishArtifact">恢复</el-button>
        <el-button v-if="artifact.draft_open" @click="discardDraftAction">丢弃 draft</el-button>
        <!-- 008 删除门禁：owner 且已停用才可删（级联硬删不可恢复）；未停用时置灰指路 -->
        <el-tooltip
          :content="artifact.status === 'deprecated'
            ? '级联硬删（行/列/版本/成员），不可恢复——请再次确认'
            : '必须先停用（deprecate）制品，删除入口才会开放'"
          placement="bottom"
        >
          <span>
            <el-button
              v-if="isOwner"
              type="danger"
              plain
              :disabled="artifact.status !== 'deprecated'"
              data-test="delete-artifact"
              @click="deleteArtifact"
            >删除</el-button>
          </span>
        </el-tooltip>
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
        <!-- wiki 编辑面板（63号批次2）：viewer 也可读（面板内部按 canWrite 收敛写入口） -->
        <WikiEditorPanel
          v-if="artifact.kind === 'wiki'"
          :artifact-id="artifactId"
          :can-write="canWrite"
          :expected-updated-at="artifact.updated_at"
          :draft-open="artifact.draft_open"
          @changed="reloadAll"
        />
        <template v-else-if="canWrite">
          <ArtifactImportWizard
            :artifact-id="artifactId"
            :active="activeTab === 'design'"
            @imported="reloadAll"
          />
          <el-divider content-position="left">列定义</el-divider>
          <ArtifactSchemaPanel
            :artifact-id="artifactId"
            :can-write="canWrite"
            :columns="artifact.schema"
            :expected-updated-at="artifact.updated_at"
            @changed="reloadAll"
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

        <!-- wiki 运行态阅读器（63号批次2）：面板自加载 current_revision 投影；
             表格制品走下方只读行表（两条路径互斥，DOM 结构各不受扰） -->
        <WikiReaderPanel
          v-if="artifact.kind === 'wiki'"
          :artifact-id="artifactId"
        />
        <template v-else>
          <el-table v-loading="rowsLoading" :data="runtimeRows" size="small">
            <el-table-column label="行键" min-width="150">
              <template #default="{ row }">
                <el-tooltip :content="row.row_key" placement="top" :disabled="row.row_key === displayRowKey(row)">
                  <span>{{ displayRowKey(row) }}</span>
                </el-tooltip>
              </template>
            </el-table-column>
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
        </template>
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
          @changed="reloadAll"
        />
      </el-tab-pane>
    </el-tabs>

    <!-- 编辑元信息（PATCH：title/description/usage_note[/visibility owner] + 乐观锁） -->
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
        <!-- 008：公开/私有是 owner 专属治理决策（后端 403 兜底，前端同时收敛入口） -->
        <el-form-item v-if="isOwner" label="可见性">
          <el-radio-group v-model="metaForm.visibility" data-test="meta-visibility">
            <el-radio value="private">私有（仅成员可见）</el-radio>
            <el-radio value="public">公开（本域所有用户可读）</el-radio>
          </el-radio-group>
          <div class="artifact-detail__visibility-hint">
            公开只放大「读」——编辑/发布/成员管理仍走成员角色；Agent 侧还需在 MCP 钥匙里显式开放。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="metaVisible = false">取消</el-button>
        <el-button type="primary" :loading="metaSaving" data-test="submit-meta" @click="saveMeta">保存</el-button>
      </template>
    </el-dialog>

    <!-- 发布：diff 三段预览 + usage_note 门禁 + change_summary + 干净态禁发（Task 12） -->
    <ArtifactPublishDialog
      v-model="publishVisible"
      :artifact-id="artifactId"
      :expected-updated-at="artifact?.updated_at"
      :usage-note="artifact?.usage_note || ''"
      :draft-open="artifact?.draft_open"
      :current-revision="artifact?.current_revision ?? 0"
      :unit-label="artifact?.kind === 'wiki' ? '页' : '行'"
      @published="reloadAll"
      @changed="reload"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
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
import WikiEditorPanel from '@/components/artifacts/WikiEditorPanel.vue'
import WikiReaderPanel from '@/components/artifacts/WikiReaderPanel.vue'
import { decodeRowKey } from '@/utils/rowKey'
import type { ArtifactColumn, ArtifactDetail, ArtifactRow } from '@/types/artifacts'

const props = defineProps<{ artifactId: string }>()

const api = useArtifactsApi()
const router = useRouter()

const artifact = ref<ArtifactDetail | null>(null)
const loading = ref(false)
const loadError = ref('')
const activeTab = ref<'design' | 'runtime' | 'members' | 'versions'>('design')

// 运行态只读行（非 draft，服务端分页）+ 运行态列（发布定格快照，懒加载）
const runtimeRows = ref<ArtifactRow[]>([])
const runtimeColumns = ref<ArtifactColumn[]>([])
const rowsTotal = ref(0)
const rowsPage = ref(1)
const rowsPageSize = 20
const rowsLoading = ref(false)
const runtimeLoaded = ref(false)

// 版本面板（发布后命令式刷新 revisions）+ 行面板（跨面板变更后刷新行）
const versionsPanel = ref<InstanceType<typeof ArtifactVersionsPanel> | null>(null)
const rowsPanel = ref<InstanceType<typeof ArtifactRowsPanel> | null>(null)

// 元信息编辑
const metaVisible = ref(false)
const metaSaving = ref(false)
const metaForm = ref({ title: '', description: '', usage_note: '', visibility: 'private' })

// 发布
const publishVisible = ref(false)

/** canWrite = my_role ∈ {owner, editor}（详情响应附带；admin 后端归一 owner）。 */
const canWrite = computed(() => artifact.value?.my_role === 'owner' || artifact.value?.my_role === 'editor')
/** 成员管理仅 owner（后端 ROLE_OWNER_ONLY）。 */
const isOwner = computed(() => artifact.value?.my_role === 'owner')
/** 干净态 = draft 关闭且已有发布版 → 无待发布变更（后端 409 门禁镜像）。 */
const hasPendingChanges = computed(() =>
  !!artifact.value && (artifact.value.draft_open || artifact.value.current_revision === 0))

async function reload() {
  loading.value = true
  loadError.value = ''
  try {
    artifact.value = await api.get(props.artifactId)
    // 008 UI：viewer（含公开读者）默认落在运行态——设计态对非写者只是空提示
    if (!loadingFirstTabDone.value && !canWrite.value) activeTab.value = 'runtime'
    loadingFirstTabDone.value = true
  } catch (error) {
    artifact.value = null
    loadError.value = `加载失败：${await apiErrorDetail(error)}`
  } finally {
    loading.value = false
  }
}

const loadingFirstTabDone = ref(false)

/**
 * 统一刷新（codex二审功能项②）：详情（乐观锁令牌+设计态 schema）+ 运行态
 * schema/行数据 + 行编辑面板 + 版本面板。导入/列变更/回滚/discard/发布/
 * 停用/恢复后一律走这里——原先分散的局部刷新是「回滚后部分缓存不刷新」的根因。
 */
async function reloadAll() {
  await reload()
  if (runtimeLoaded.value) {
    await Promise.all([loadRuntimeSchema(), loadRuntimeRows()])
  }
  rowsPanel.value?.loadRows?.()
  versionsPanel.value?.loadRevisions?.()
}

/**
 * 运行态列 = 发布定格快照（GET /table/columns?runtime=true 读
 * revisions.schema_json）——设计态草稿新列不再漏进运行态表头（空列）。
 */
async function loadRuntimeSchema() {
  try {
    runtimeColumns.value = await api.listColumns(
      props.artifactId, { runtime: true })
  } catch (error) {
    runtimeColumns.value = []
    ElMessage.error(`加载运行态列定义失败：${await apiErrorDetail(error)}`)
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
  } catch (error) {
    ElMessage.error(`加载运行态数据失败：${await apiErrorDetail(error)}`)
  } finally {
    rowsLoading.value = false
  }
}

/** 运行态 tab 首次切入：schema + 行一起拉（懒加载；成员/版本面板走 lazy 自加载）。 */
function loadRuntimeData() {
  runtimeLoaded.value = true
  void Promise.all([loadRuntimeSchema(), loadRuntimeRows()])
}

watch(activeTab, (tab) => {
  // wiki 守卫保留：无表格运行态，不触发 table 列/行懒加载（阅读器面板自加载）
  if (tab === 'runtime' && !runtimeLoaded.value && artifact.value?.kind !== 'wiki') {
    loadRuntimeData()
  }
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
    visibility: (artifact.value.visibility as string) || 'private',
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
      // 后端对非 owner 改 visibility 一律 403——前端只在 owner 表单露出该字段
      ...(isOwner.value ? { visibility: metaForm.value.visibility } : {}),
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

/** 删除（008 两级确认第二级）：仅已停用制品可删；输入标题确认后级联硬删。 */
async function deleteArtifact() {
  if (!artifact.value) return
  const title = artifact.value.title
  let typed: string
  try {
    const result = await ElMessageBox.prompt(
      `此操作将**级联硬删**制品「${title}」的全部行数据、列定义、版本快照与成员，不可恢复。\n请输入制品标题确认：`,
      '删除知识制品',
      { type: 'warning', confirmButtonText: '永久删除', confirmButtonClass: 'el-button--danger' },
    )
    typed = String(result.value ?? '').trim()
  } catch {
    return
  }
  if (typed !== title) {
    ElMessage.warning('输入的标题不一致，已取消删除')
    return
  }
  try {
    await api.remove(props.artifactId)
    ElMessage.success('制品已删除')
    await router.push('/artifacts')
  } catch (error) {
    const resp = (error as { response?: { status?: number; data?: { detail?: { code?: string } } } })?.response
    if (resp?.status === 409 && resp.data?.detail?.code === 'artifact_not_deprecated') {
      ElMessage.error('必须先停用制品才能删除——已为你刷新状态')
      await reloadAll()
    } else {
      ElMessage.error(`删除失败：${await apiErrorDetail(error)}`)
    }
  }
}

/** 简单生命周期动作（停用/恢复/丢弃）：确认 → POST → 统一刷新（reloadAll）。 */
async function lifecycleAction(fn: () => Promise<unknown>, confirmText: string) {
  try {
    await ElMessageBox.confirm(confirmText, { type: 'warning' })
  } catch {
    return
  }
  try {
    await fn()
    ElMessage.success('操作成功')
    await reloadAll()
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

/** 运行态行键展示：有主键 → hex 解码回原文；无主键（uuid）原样。 */
const runtimeHasPk = computed(() => runtimeColumns.value.some(c => c.is_primary_key))

function displayRowKey(row: ArtifactRow): string {
  return decodeRowKey(row.row_key, runtimeHasPk.value)
}

onMounted(reload)

defineExpose({ reloadAll })
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
.artifact-detail__visibility-hint { width: 100%; color: var(--kb-text-tertiary); font-size: 12px; line-height: 1.5; }
.artifact-detail__pager { margin-top: 12px; justify-content: flex-end; }
</style>
