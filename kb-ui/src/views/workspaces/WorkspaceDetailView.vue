<template>
  <div class="ws-detail">
    <!-- 顶栏：名称+描述 | 运行 / 导出（导出 Task 12-13 接线，先禁用占位） -->
    <header class="ws-detail__header">
      <div class="ws-detail__head-left">
        <router-link
          :to="{ name: 'workspaces' }"
          class="ws-detail__back"
          aria-label="返回工作区列表"
          title="返回工作区列表"
        >
          <el-icon><ArrowLeft /></el-icon>
        </router-link>
        <div class="ws-detail__head-text">
          <h2 class="ws-detail__title">{{ workspace?.name || '加载中…' }}</h2>
          <p v-if="workspace?.description" class="ws-detail__desc">{{ workspace.description }}</p>
        </div>
      </div>
      <div v-if="workspace" class="ws-detail__actions">
        <el-button
          type="primary"
          data-test="run-workspace"
          :loading="running"
          :disabled="!canWrite || !hasNodes"
          @click="runWorkspace"
        >运行工作区</el-button>
        <el-tooltip content="导出制品草稿将在下一步开放（Task 12 接线）" placement="bottom">
          <span>
            <el-button data-test="export-drafts" disabled>导出制品草稿</el-button>
          </span>
        </el-tooltip>
      </div>
    </header>

    <!-- 工具条：批次 · 节点数 · 运行态（轮询时 spinner+当前节点） -->
    <div v-if="workspace" class="ws-detail__toolbar">
      <span class="ws-detail__meta">批次 {{ workspace.batch_no }}</span>
      <span class="ws-detail__meta-dot">·</span>
      <span class="ws-detail__meta">{{ nodes.length }} 节点</span>
      <template v-if="running">
        <span class="ws-detail__meta-dot">·</span>
        <span class="ws-detail__run is-active" data-test="run-state">
          <el-icon class="ws-detail__spinner"><Loading /></el-icon>
          运行中<template v-if="currentNodeLabel"> · {{ currentNodeLabel }}</template>
        </span>
      </template>
      <template v-else-if="lastRun">
        <span class="ws-detail__meta-dot">·</span>
        <span class="ws-detail__run" :class="`is-${lastRun.status}`" data-test="run-state">
          {{ runStatusLabel(lastRun.status) }}<template v-if="lastRun.error"> · {{ lastRun.error }}</template>
        </span>
      </template>
    </div>

    <!-- 加载 / 错误态（404 与无权同文案——反探测口径） -->
    <el-skeleton v-if="loading" :rows="6" animated class="ws-detail__skeleton" />
    <div v-else-if="loadError" class="ws-detail__missing" data-test="ws-load-error">
      <EmptyState :text="loadError" />
      <el-button type="primary" @click="router.push({ name: 'workspaces' })">返回列表</el-button>
    </div>

    <!-- 三栏：来源+结果树（左约 180px）｜步骤流+输出区（中）｜Inspector 占位（右约 320px） -->
    <div v-else class="ws-detail__body">
      <aside class="ws-detail__left">
        <section class="ws-detail__panel">
          <h3 class="ws-detail__panel-title">来源</h3>
          <ul v-if="sources.length" class="ws-detail__sources">
            <li v-for="source in sources" :key="source.id" class="ws-detail__source">
              <span class="ws-detail__source-kind" :class="`is-${source.connector}`">
                {{ source.connector === 'kb_dir' ? '库' : '表' }}
              </span>
              <span class="ws-detail__source-name">{{ nodeNameOf(source.node_id) }}</span>
            </li>
          </ul>
          <p v-else class="ws-detail__panel-empty">尚未接入资料</p>
        </section>
        <section class="ws-detail__panel ws-detail__panel--grow">
          <h3 class="ws-detail__panel-title">加工结果</h3>
          <ul v-if="nodes.length" class="ws-detail__tree">
            <li v-for="node in orderedNodes" :key="node.id" class="ws-detail__tree-node">
              <p class="ws-detail__tree-node-name">{{ node.name }}</p>
              <ul>
                <li v-for="port in node.ports" :key="port.id">
                  <button
                    type="button"
                    class="ws-detail__tree-leaf"
                    :class="{ 'is-selected': selectedRef === `${node.id}:${port.id}` }"
                    :data-test="`tree-ref-${node.id}-${port.id}`"
                    @click="selectedRef = `${node.id}:${port.id}`"
                  >
                    <span class="ws-detail__tree-port">{{ port.name }}</span>
                    <span class="ws-detail__tree-meta">{{ treePortMeta(node.id, port.id) }}</span>
                  </button>
                </li>
              </ul>
            </li>
          </ul>
          <p v-else class="ws-detail__panel-empty">运行后这里按端口列出加工结果</p>
        </section>
      </aside>

      <main class="ws-detail__center">
        <section class="ws-detail__flow">
          <WorkspaceStepFlow
            :nodes="nodes"
            :outputs="outputs"
            :selected-ref="selectedRef"
            @select="ref => (selectedRef = ref)"
            @add-source="toastNextStep"
            @add-operator="toastNextStep"
          />
        </section>
        <section class="ws-detail__output">
          <header class="ws-detail__output-head">
            <span class="ws-detail__output-eyebrow">输出预览</span>
            <span class="ws-detail__output-ref">{{ selectedRef ?? '未选择输出' }}</span>
          </header>
          <el-alert
            v-if="selectedOutput?.stale_since_run"
            type="warning"
            :closable="false"
            class="ws-detail__stale-banner"
            title="该输出已过期（上游有新批次），重新运行后刷新"
          />
          <div v-loading="outputsLoading" class="ws-detail__output-body">
            <WorkspaceOutputTable :output="selectedOutput" />
          </div>
        </section>
      </main>

      <aside class="ws-detail__right">
        <div class="ws-detail__inspector" data-test="inspector-placeholder">
          <el-icon :size="22" class="ws-detail__inspector-icon"><Setting /></el-icon>
          <p class="ws-detail__inspector-title">配置面板</p>
          <p class="ws-detail__inspector-hint">将于下步开放——节点参数配置、试跑与执行入口（Task 12）</p>
        </div>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowLeft, Loading, Setting } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useWorkspacesApi } from '@/api/workspaces'
import { apiErrorDetail } from '@/api/proxyClient'
import { useDocumentTitle } from '@/composables/useDocumentTitle'
import { usePolling } from '@/composables/usePolling'
import EmptyState from '@/components/common/EmptyState.vue'
import WorkspaceStepFlow from '@/components/workspaces/WorkspaceStepFlow.vue'
import WorkspaceOutputTable from '@/components/workspaces/WorkspaceOutputTable.vue'
import type {
  GraphNode,
  RunSummary,
  WorkspaceDetail,
  WorkspaceOutput,
  WorkspaceSource,
} from '@/api/workspaces'

/**
 * 工作台主页（67号 Task 11）：三栏骨架——左来源+加工结果树 / 中步骤流+
 * 输出区 / 右 Inspector 占位。执行交互本任务只做「运行工作区」全图跑
 * （scope=workspace，轮询 run status 至终态刷新图与输出）；试跑/单节点
 * 执行/Inspector 表单/导出对话框归 Task 12-13。
 */
const props = defineProps<{ workspaceId: string }>()

const api = useWorkspacesApi()
const router = useRouter()

const workspace = ref<WorkspaceDetail | null>(null)
const nodes = ref<GraphNode[]>([])
const outputs = ref<Record<string, WorkspaceOutput>>({})
const sources = ref<WorkspaceSource[]>([])
const loading = ref(false)
const loadError = ref('')
const outputsLoading = ref(false)

const selectedRef = ref<string | null>(null)

useDocumentTitle(() => workspace.value?.name || '制作工作台')

// ── 派生态 ──

/** canWrite = my_role ∈ {owner, editor}（admin 旁路后端已归一 owner）。 */
const canWrite = computed(() =>
  workspace.value?.my_role === 'owner' || workspace.value?.my_role === 'editor')

const hasNodes = computed(() => nodes.value.length > 0)

const selectedOutput = computed(() =>
  selectedRef.value ? (outputs.value[selectedRef.value] ?? null) : null)

/** 拓扑序（与步骤流同算法口径：依赖在前、同层保持原序）。 */
const orderedNodes = computed(() => topoSort(nodes.value))

function topoSort(graphNodes: GraphNode[]): GraphNode[] {
  const byId = new Map(graphNodes.map(n => [n.id, n]))
  const indegree = new Map(graphNodes.map(n => [n.id, 0]))
  const dependents = new Map<string, string[]>()
  for (const node of graphNodes) {
    for (const input of node.inputs) {
      const upstream = input.split(':')[0]
      if (!indegree.has(upstream) || upstream === node.id) continue
      indegree.set(node.id, (indegree.get(node.id) ?? 0) + 1)
      dependents.set(upstream, [...(dependents.get(upstream) ?? []), node.id])
    }
  }
  const queue = graphNodes.filter(n => (indegree.get(n.id) ?? 0) === 0).map(n => n.id)
  const ordered: GraphNode[] = []
  while (queue.length > 0) {
    const id = queue.shift()!
    const node = byId.get(id)
    if (node) ordered.push(node)
    for (const next of dependents.get(id) ?? []) {
      const left = (indegree.get(next) ?? 0) - 1
      indegree.set(next, left)
      if (left === 0) queue.push(next)
    }
  }
  if (ordered.length < graphNodes.length) {
    const seen = new Set(ordered.map(n => n.id))
    for (const node of graphNodes) if (!seen.has(node.id)) ordered.push(node)
  }
  return ordered
}

function nodeNameOf(nodeId: string): string {
  return nodes.value.find(n => n.id === nodeId)?.name ?? nodeId
}

function treePortMeta(nodeId: string, portId: string): string {
  const found = outputs.value[`${nodeId}:${portId}`]
  if (!found) return '—'
  return found.stale_since_run ? `${found.row_count} 行 · 过期` : `${found.row_count} 行`
}

// ── 加载（并行 getWorkspace+getGraph+getCurrentOutputs+listSources） ──

function defaultSelectedRef(): string | null {
  // 默认选中第一个有 current 输出的端口（拓扑序），否则第一个端口
  for (const node of orderedNodes.value) {
    for (const port of node.ports) {
      const ref = `${node.id}:${port.id}`
      if (outputs.value[ref]) return ref
    }
  }
  const first = orderedNodes.value[0]
  return first?.ports[0] ? `${first.id}:${first.ports[0].id}` : null
}

async function refreshOutputs(): Promise<void> {
  outputsLoading.value = true
  try {
    outputs.value = (await api.getCurrentOutputs(props.workspaceId)).outputs ?? {}
  } finally {
    outputsLoading.value = false
  }
}

async function refreshGraph(): Promise<void> {
  const envelope = await api.getGraph(props.workspaceId)
  nodes.value = envelope.graph.nodes ?? []
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const [detail, graph, outputsEnvelope, sourcesEnvelope] = await Promise.all([
      api.getWorkspace(props.workspaceId),
      api.getGraph(props.workspaceId),
      api.getCurrentOutputs(props.workspaceId),
      api.listSources(props.workspaceId),
    ])
    workspace.value = detail
    nodes.value = graph.graph.nodes ?? []
    outputs.value = outputsEnvelope.outputs ?? {}
    sources.value = sourcesEnvelope
    selectedRef.value = defaultSelectedRef()
  } catch (error) {
    workspace.value = null
    const status = (error as { response?: { status?: number } })?.response?.status
    loadError.value = status === 404
      ? '工作区不存在或无权访问'
      : `加载失败：${await apiErrorDetail(error)}`
  } finally {
    loading.value = false
  }
}

// ── 运行（scope=workspace 全图跑；轮询 run status 至终态） ──

/** 后端终态集（db_workspace_runs 生命周期：queued/running → 四种终态）。 */
const TERMINAL_STATUSES = new Set(['succeeded', 'partial', 'failed', 'cancelled'])

const running = ref(false)
const activeRunId = ref<string | null>(null)
const lastRun = ref<RunSummary | null>(null)
/** progress_json.current_node（节点 id）→ 展示名。 */
const currentNodeLabel = ref('')

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '上次运行成功',
  partial: '上次运行部分完成',
  failed: '上次运行失败',
  cancelled: '上次运行已取消',
}

function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? `上次运行 ${status}`
}

async function pollRunStatus(): Promise<void> {
  const runId = activeRunId.value
  if (!runId || !running.value) return
  try {
    const envelope = await api.getRunStatus(props.workspaceId, runId)
    lastRun.value = envelope.run
    currentNodeLabel.value =
      typeof envelope.run.progress_json?.current_node === 'string'
        ? nodeNameOf(envelope.run.progress_json.current_node)
        : ''
    if (TERMINAL_STATUSES.has(envelope.run.status)) {
      running.value = false
      activeRunId.value = null
      currentNodeLabel.value = ''
      if (envelope.run.status === 'succeeded') {
        ElMessage.success('运行完成')
      } else if (envelope.run.status === 'partial') {
        ElMessage.warning('运行部分完成——部分节点失败，可在下次运行重试')
      } else {
        ElMessage.error(`运行未完成：${envelope.run.error || runStatusLabel(envelope.run.status)}`)
      }
      // 终态后统一刷新：批次号（workspace）+ pending/stale 标志（graph）+ 输出面
      await Promise.allSettled([
        api.getWorkspace(props.workspaceId).then(d => { workspace.value = d }),
        refreshGraph(),
        refreshOutputs(),
      ])
    }
  } catch {
    // 瞬时网络抖动：静默跳过本次，等下个轮询周期（终态由后端落库）
  }
}

/** 运行中每 2s 轮询（shouldRun=非运行态时空转直返，卸载自动停）。 */
const { start: startRunPolling } = usePolling(pollRunStatus, 2000, {
  immediate: false,
  shouldRun: () => running.value,
})

async function runWorkspace(): Promise<void> {
  if (running.value) return
  running.value = true
  try {
    const { run_id: runId } = await api.startRun(props.workspaceId, { scope: 'workspace' })
    activeRunId.value = runId
    startRunPolling()
    await pollRunStatus()
  } catch (error) {
    running.value = false
    activeRunId.value = null
    ElMessage.error(`启动运行失败：${await apiErrorDetail(error)}`)
  }
}

/** 步骤流入口按钮：对话框归 Task 12，先 toast 指路。 */
function toastNextStep(): void {
  ElMessage.info('该能力将在下一步开放（配置面板与接入向导）')
}

onMounted(load)
</script>

<style scoped>
.ws-detail { display: flex; flex-direction: column; min-height: 0; padding: 4px 2px; }

/* ── 顶栏 ── */
.ws-detail__header {
  display: flex; justify-content: space-between; align-items: flex-start; gap: 12px;
}
.ws-detail__head-left { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
.ws-detail__back {
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; margin-top: 3px;
  border-radius: 7px; flex-shrink: 0;
  color: var(--kb-text-tertiary); text-decoration: none;
}
.ws-detail__back:hover { color: var(--kb-accent, #0891b2); background: var(--kb-accent-soft, rgba(8, 145, 178, 0.08)); }
.ws-detail__head-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.ws-detail__title {
  margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -.4px;
  color: var(--kb-text-primary, #0f172a);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ws-detail__desc {
  margin: 0; font-size: 12.5px; color: var(--kb-text-tertiary, #94a3b8);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 640px;
}
.ws-detail__actions { display: flex; gap: 10px; flex-shrink: 0; }

/* ── 工具条 ── */
.ws-detail__toolbar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin: 10px 0 14px; padding: 6px 12px;
  border: 1px solid var(--kb-border-light, #f1f5f9);
  border-radius: 8px;
  background: var(--kb-bg-card, #fff);
}
.ws-detail__meta { font-size: 12px; color: var(--kb-text-secondary, #64748b); }
.ws-detail__meta-dot { color: var(--kb-border, #e2e8f0); font-size: 12px; }
.ws-detail__run { font-size: 12px; color: var(--kb-text-secondary, #64748b); }
.ws-detail__run.is-active { color: var(--kb-accent, #0891b2); }
.ws-detail__run.is-succeeded { color: var(--kb-success, #10b981); }
.ws-detail__run.is-partial { color: var(--kb-warning, #f59e0b); }
.ws-detail__run.is-failed { color: var(--el-color-danger, #ef4444); }
.ws-detail__spinner { animation: ws-detail-spin 1s linear infinite; vertical-align: -2px; margin-right: 3px; }
@keyframes ws-detail-spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

.ws-detail__skeleton { margin-top: 16px; }
.ws-detail__missing { display: flex; flex-direction: column; align-items: center; gap: 12px; }

/* ── 三栏（flex 不写死宽度；左 180 / 右 320 为基准，可弹性） ── */
.ws-detail__body {
  display: flex; gap: 14px; align-items: stretch; min-height: 0;
}

/* 左栏 */
.ws-detail__left {
  display: flex; flex-direction: column; gap: 12px;
  width: 180px; flex: 0 0 180px; min-width: 0;
}
.ws-detail__panel {
  border: 1px solid var(--kb-border-light, #f1f5f9);
  border-radius: 10px;
  background: var(--kb-bg-card, #fff);
  padding: 10px 10px 12px;
}
.ws-detail__panel--grow { flex: 1; min-height: 0; overflow: auto; }
.ws-detail__panel-title {
  margin: 0 0 8px; padding: 0 2px;
  font-size: 11px; font-weight: 600; letter-spacing: .04em;
  color: var(--kb-text-tertiary, #94a3b8);
  text-transform: uppercase;
}
.ws-detail__panel-empty { margin: 4px 2px; font-size: 12px; color: var(--kb-text-tertiary, #94a3b8); }

.ws-detail__sources { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.ws-detail__source { display: flex; align-items: center; gap: 7px; min-width: 0; }
.ws-detail__source-kind {
  display: inline-grid; place-items: center; flex-shrink: 0;
  width: 20px; height: 20px; border-radius: 5px;
  font-size: 10.5px; font-weight: 700;
  border: 1px solid var(--kb-border, #e2e8f0);
  color: var(--kb-accent, #0891b2);
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.08));
}
.ws-detail__source-kind.is-excel { color: var(--kb-success, #10b981); background: var(--kb-success-soft, rgba(16, 185, 129, 0.1)); }
.ws-detail__source-name {
  font-size: 12.5px; color: var(--kb-text-primary, inherit);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 加工结果树（按端口列 ref，选中态） */
.ws-detail__tree { list-style: none; margin: 0; padding: 0; }
.ws-detail__tree-node { margin-bottom: 8px; }
.ws-detail__tree-node-name {
  margin: 0 0 4px; padding: 0 2px;
  font-size: 11.5px; color: var(--kb-text-tertiary, #94a3b8);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ws-detail__tree-node ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.ws-detail__tree-leaf {
  display: flex; align-items: center; justify-content: space-between; gap: 6px;
  width: 100%; box-sizing: border-box;
  border: 1px solid transparent; border-radius: 6px;
  padding: 4px 7px; background: none; cursor: pointer; text-align: left;
  font-family: inherit;
}
.ws-detail__tree-leaf:hover { background: var(--kb-bg-card-hover, #f8fafc); }
.ws-detail__tree-leaf.is-selected {
  border-color: var(--kb-accent, #0891b2);
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.1));
}
.ws-detail__tree-port {
  font-size: 12px; color: var(--kb-text-secondary, #64748b);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ws-detail__tree-leaf.is-selected .ws-detail__tree-port { color: var(--kb-accent, #0891b2); font-weight: 500; }
.ws-detail__tree-meta { font-size: 10.5px; color: var(--kb-text-tertiary, #94a3b8); flex-shrink: 0; }

/* 中栏：步骤流（上）+ 输出区（下） */
.ws-detail__center {
  flex: 1 1 auto; min-width: 0;
  display: flex; flex-direction: column; gap: 14px;
}
.ws-detail__flow { min-width: 0; }
.ws-detail__output {
  display: flex; flex-direction: column; min-width: 0; min-height: 240px;
  border: 1px solid var(--kb-border-light, #f1f5f9);
  border-radius: 10px;
  background: var(--kb-bg-card, #fff);
  padding: 10px 12px 12px;
}
.ws-detail__output-head {
  display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px;
}
.ws-detail__output-eyebrow {
  font-size: 11px; font-weight: 600; letter-spacing: .04em;
  color: var(--kb-text-tertiary, #94a3b8); text-transform: uppercase;
}
.ws-detail__output-ref {
  font-size: 12px; color: var(--kb-text-secondary, #64748b);
  font-family: var(--kb-font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
}
.ws-detail__stale-banner { margin-bottom: 8px; }
.ws-detail__output-body { min-width: 0; }

/* 右栏：Inspector 占位（Task 12 建真身） */
.ws-detail__right {
  flex: 0 0 320px; width: 320px; min-width: 0;
}
.ws-detail__inspector {
  height: 100%; min-height: 200px;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 8px; padding: 24px;
  border: 1px dashed var(--kb-border, #e2e8f0);
  border-radius: 10px;
  background: var(--kb-bg-card-hover, #f8fafc);
  text-align: center;
}
.ws-detail__inspector-icon { color: var(--kb-text-tertiary, #94a3b8); opacity: .6; }
.ws-detail__inspector-title { margin: 0; font-size: 13px; font-weight: 600; color: var(--kb-text-secondary, #64748b); }
.ws-detail__inspector-hint { margin: 0; font-size: 12px; color: var(--kb-text-tertiary, #94a3b8); }
</style>
