<template>
  <div class="ws-detail">
    <!-- 顶栏：名称+描述 | 运行 / 导出 -->
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
        <el-button
          data-test="export-drafts"
          :disabled="!canWrite || !hasNodes || running"
          @click="exportDialogOpen = true"
        >导出制品草稿</el-button>
      </div>
    </header>

    <!-- 工具条：负责人 · 批次 · 节点数 · 运行态（轮询时 spinner+当前节点） -->
    <div v-if="workspace" class="ws-detail__toolbar">
      <span class="ws-detail__meta" data-test="ws-owner">负责人 {{ ownerLabel }}</span>
      <span class="ws-detail__meta-dot">·</span>
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

    <!-- 三栏：来源+结果树（左约 180px）｜步骤流+输出区（中）｜Inspector（右约 320px） -->
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
            @add-source="sourceDialogOpen = true"
            @add-operator="nodeDialogOpen = true"
          />
        </section>
        <section class="ws-detail__output">
          <header class="ws-detail__output-head">
            <span class="ws-detail__output-eyebrow">输出预览</span>
            <span class="ws-detail__output-ref">{{ displayedRefLabel }}</span>
          </header>
          <!-- 试跑横幅：选中输出被试跑结果顶替（stale 横幅让位） -->
          <el-alert
            v-if="trialResult"
            type="info"
            :closable="false"
            class="ws-detail__stale-banner"
            data-test="trial-banner"
          >
            <template #title>
              试跑预览 · {{ trialResult.inputRows }} 条输入 · 不落库
              <el-button size="small" text data-test="exit-trial" @click="trialResult = null">退出试跑</el-button>
            </template>
          </el-alert>
          <el-alert
            v-else-if="selectedOutput?.stale_since_run"
            type="warning"
            :closable="false"
            class="ws-detail__stale-banner"
            title="该输出已过期（上游有新批次），重新运行后刷新"
          />
          <div v-loading="outputsLoading || trialing" class="ws-detail__output-body">
            <WorkspaceOutputTable :output="displayedOutput" />
          </div>
        </section>
      </main>

      <aside class="ws-detail__right">
        <WorkspaceInspector
          :node="selectedNode"
          :candidates="inspectorCandidates"
          :port-cols="portCols"
          :source-row="selectedSourceRow"
          :can-write="canWrite"
          :running="running"
          @save="onInspectorSave"
          @trial="onTrial"
          @execute="onExecuteNode"
          @delete-node="onDeleteNode"
        />
      </aside>
    </div>

    <!-- 对话框三件：接入资料 / 添加算子 / 导出草稿 -->
    <WorkspaceSourceDialog
      v-model="sourceDialogOpen"
      :workspace-id="workspaceId"
      :domain="workspace?.domain_id ?? ''"
      :nodes="nodes"
      :graph-revision="graphRevision"
      @created="onSourceCreated"
      @graph-conflict="onGraphConflict"
    />
    <WorkspaceNodeDialog
      v-model="nodeDialogOpen"
      :port-options="allPortOptions"
      :default-ref="selectedRef"
      @confirm="onAddOperator"
    />
    <WorkspaceExportDialog
      v-model="exportDialogOpen"
      :workspace-id="workspaceId"
      :ports="exportPorts"
      :owner-label="ownerLabel"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowLeft, Loading } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useWorkspacesApi } from '@/api/workspaces'
import { apiErrorDetail } from '@/api/proxyClient'
import { useDocumentTitle } from '@/composables/useDocumentTitle'
import { usePolling } from '@/composables/usePolling'
import EmptyState from '@/components/common/EmptyState.vue'
import WorkspaceStepFlow from '@/components/workspaces/WorkspaceStepFlow.vue'
import WorkspaceOutputTable from '@/components/workspaces/WorkspaceOutputTable.vue'
import WorkspaceInspector from '@/components/workspaces/WorkspaceInspector.vue'
import WorkspaceSourceDialog from '@/components/workspaces/WorkspaceSourceDialog.vue'
import WorkspaceNodeDialog from '@/components/workspaces/WorkspaceNodeDialog.vue'
import WorkspaceExportDialog, { type ExportPortOption } from '@/components/workspaces/WorkspaceExportDialog.vue'
import { isTerminalRunStatus, runStatusLabel } from './runStatus'
import type {
  GraphNode,
  RunSummary,
  WorkspaceDetail,
  WorkspaceOutput,
  WorkspaceSource,
} from '@/api/workspaces'

/**
 * 工作台主页（67号 Task 11 三栏骨架 + Task 12 交互接线）：左来源+加工
 * 结果树 / 中步骤流+输出区（含试跑态）/ 右 Inspector 节点配置。执行交互
 * =运行工作区（scope=workspace）与执行此节点（scope=node）共用一套轮询
 * 机制（终态口径见 ./runStatus）；保存配置与删除节点走整图 PUT（乐观锁
 * 409 刷新重拉；删除的后端同事务清理见 onDeleteNode）；接入资料/添加
 * 算子/导出草稿三对话框挂页面级。
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
const graphRevision = ref(0)

const selectedRef = ref<string | null>(null)

const sourceDialogOpen = ref(false)
const nodeDialogOpen = ref(false)
const exportDialogOpen = ref(false)

useDocumentTitle(() => workspace.value?.name || '制作工作台')

// ── 派生态 ──

/** canWrite：2026-11-17 收紧后 my_role 只剩合成角色 admin/domain_admin
 * （管理员天然可写）；owner/editor 保留兼容历史口径。 */
const canWrite = computed(() => {
  const role = workspace.value?.my_role
  return role === 'admin' || role === 'domain_admin'
    || role === 'owner' || role === 'editor'
})

/** 负责人显示（用户测试反馈修复1）：owner_username 回落 owner_user_id 前 8 位。 */
const ownerLabel = computed(() =>
  workspace.value?.owner_username
  || workspace.value?.owner_user_id.slice(0, 8)
  || '—')

const hasNodes = computed(() => nodes.value.length > 0)

const selectedOutput = computed(() =>
  selectedRef.value ? (outputs.value[selectedRef.value] ?? null) : null)

/** 选中端口所属节点（Inspector 的配置对象）。 */
const selectedNode = computed<GraphNode | null>(() => {
  const nodeId = selectedRef.value?.split(':')[0]
  return nodeId ? (nodes.value.find(n => n.id === nodeId) ?? null) : null
})

/** source 节点的来源行（只读展示用）。 */
const selectedSourceRow = computed<WorkspaceSource | null>(() => {
  const node = selectedNode.value
  if (!node || node.op !== 'source') return null
  return sources.value.find(s => s.node_id === node.id) ?? null
})

/** 拓扑序（与步骤流同算法口径：依赖在前、同层保持原序）。 */
const orderedNodes = computed(() => topoSort(nodes.value))

/** Inspector 输入候选：全图端口剔除「自身+全部下游」——引用剩余者不成环。 */
const inspectorCandidates = computed(() => {
  const target = selectedNode.value
  if (!target) return []
  const blocked = new Set<string>([target.id])
  let changed = true
  while (changed) {
    changed = false
    for (const node of nodes.value) {
      if (blocked.has(node.id)) continue
      const consumesBlocked = node.inputs.some(ref => blocked.has(ref.split(':')[0]))
      if (consumesBlocked) {
        blocked.add(node.id)
        changed = true
      }
    }
  }
  return nodes.value
    .filter(n => !blocked.has(n.id))
    .flatMap(n => n.ports.map(p => ({ ref: `${n.id}:${p.id}`, label: `${n.name} · ${p.name}` })))
})

/** 各端口当前输出的列（Inspector 列候选/默认值）。 */
const portCols = computed<Record<string, string[]>>(() => {
  const map: Record<string, string[]> = {}
  for (const [ref, out] of Object.entries(outputs.value)) {
    const cols = (out.content_inline as Record<string, unknown> | null)?.cols
    if (Array.isArray(cols)) map[ref] = cols.map(String)
  }
  return map
})

/** 全部端口选项（添加算子对话框输入候选）。 */
const allPortOptions = computed(() =>
  nodes.value.flatMap(n => n.ports.map(p => ({ ref: `${n.id}:${p.id}`, label: `${n.name} · ${p.name}` }))))

// ── 试跑态（选中输出被试跑结果顶替） ──

const trialResult = ref<{ inputRows: number; output: WorkspaceOutput } | null>(null)
const trialing = ref(false)

const displayedOutput = computed<WorkspaceOutput | null>(() =>
  trialResult.value ? trialResult.value.output : selectedOutput.value)

const displayedRefLabel = computed(() =>
  trialResult.value ? `${trialResult.value.output.node_id}:${trialResult.value.output.port_id}（试跑）`
    : (selectedRef.value ?? '未选择输出'))

// 切换选中端口即退出试跑（试跑结果只对着发起时的端口）
watch(selectedRef, () => { trialResult.value = null })

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
  graphRevision.value = envelope.graph_revision
}

async function refreshSources(): Promise<void> {
  sources.value = await api.listSources(props.workspaceId)
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
    graphRevision.value = graph.graph_revision
    outputs.value = outputsEnvelope.outputs ?? {}
    sources.value = sourcesEnvelope
    selectedRef.value = defaultSelectedRef()
    await resumeInflightRun()
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

// ── 运行面（workspace 全图 / node 单节点共用；轮询 run status 至终态） ──

/**
 * 重进页面恢复在飞 run（用户反馈缺口）：服务端后台一直在跑，但前端此前
 * 只在"本会话点过运行"才轮询——退出去再进来运行指示就断了。挂载加载后
 * 查最新一条 run，queued/running 则接回轮询（终态刷新/toast 复用既有
 * pollRunStatus 全套）；查询失败静默不阻塞主流程。
 */
async function resumeInflightRun(): Promise<void> {
  try {
    const runs = await api.listRuns(props.workspaceId, 1)
    const latest = Array.isArray(runs) ? runs[0] : undefined
    if (latest && !isTerminalRunStatus(latest.status) && !running.value) {
      activeRunId.value = latest.id
      activeRunScope.value = latest.scope === 'node' ? 'node' : 'workspace'
      running.value = true
      lastRun.value = latest
      // 死锁修复（用户反馈：run 期间重进页面后按钮永久禁用）：running 的
      // 唯一"放下"途径是轮询循环发现终态——恢复标志的同时必须启动循环，
      // 否则服务端跑完了也没人通知前端，running 永真，运行/导出全锁死。
      startRunPolling()
    }
  } catch {
    // 恢复失败视为无在飞 run——不阻塞页面主数据
  }
}


const running = ref(false)
const activeRunId = ref<string | null>(null)
const activeRunScope = ref<'workspace' | 'node'>('workspace')
const lastRun = ref<RunSummary | null>(null)
/** progress_json.current_node（节点 id）→ 展示名。 */
const currentNodeLabel = ref('')

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
    if (isTerminalRunStatus(envelope.run.status)) {
      running.value = false
      activeRunId.value = null
      currentNodeLabel.value = ''
      trialResult.value = null
      const isNodeScope = activeRunScope.value === 'node'
      if (envelope.run.status === 'completed') {
        ElMessage.success(isNodeScope ? '节点执行完成' : '运行完成')
      } else if (envelope.run.status === 'partial') {
        ElMessage.warning('运行部分完成——部分节点失败，可在下次运行重试')
      } else {
        ElMessage.error(`运行未完成：${envelope.run.error || runStatusLabel(envelope.run.status)}`)
      }
      // 终态后统一刷新：批次号（workspace）+ pending/stale 标志（graph）+ 输出面
      const settled = await Promise.allSettled([
        api.getWorkspace(props.workspaceId).then(d => { workspace.value = d }),
        refreshGraph(),
        refreshOutputs(),
      ])
      reportRefreshFailures(settled)
    }
  } catch {
    // 瞬时网络抖动：静默跳过本次，等下个轮询周期（终态由后端落库）
  }
}

/** 终态刷新的 rejected 分支不静默（allSettled 吞错 → 补 toast 提示手刷）。 */
function reportRefreshFailures(settled: PromiseSettledResult<unknown>[]): void {
  if (settled.some(r => r.status === 'rejected')) {
    ElMessage.error('运行完成，但刷新数据失败——请手动刷新页面')
  }
}

/** 运行中每 2s 轮询（shouldRun=非运行态时空转直返，卸载自动停）。 */
const { start: startRunPolling } = usePolling(pollRunStatus, 2000, {
  immediate: false,
  shouldRun: () => running.value,
})

async function startRun(scope: 'workspace' | 'node', targetNodeId?: string): Promise<void> {
  if (running.value) return
  running.value = true
  activeRunScope.value = scope
  try {
    const { run_id: runId } = await api.startRun(props.workspaceId, {
      scope,
      ...(scope === 'node' && targetNodeId ? { target_node_id: targetNodeId } : {}),
    })
    activeRunId.value = runId
    startRunPolling()
    await pollRunStatus()
  } catch (error) {
    running.value = false
    activeRunId.value = null
    ElMessage.error(`启动运行失败：${await apiErrorDetail(error)}`)
  }
}

function runWorkspace(): Promise<void> {
  return startRun('workspace')
}

function onExecuteNode(): Promise<void> {
  const node = selectedNode.value
  if (!node) return Promise.resolve()
  return startRun('node', node.id)
}

// ── 保存配置（Inspector → 整图 PUT，乐观锁 409 刷新重拉） ──
//    第三参 names 同图生效：node.name 与 ports[0].name（下游 inputs 引用
//    的是 node:port 稳定 id，改名不影响连线；其余节点原样透传）。

async function onInspectorSave(
  config: Record<string, unknown>,
  inputs: string[],
  names: { name: string; portName: string },
): Promise<void> {
  const target = selectedNode.value
  if (!target) return
  try {
    const nextNodes = nodes.value.map(n =>
      n.id === target.id
        ? {
            ...n,
            name: names.name,
            config,
            inputs,
            ports: n.ports.map((p, i) => (i === 0 ? { ...p, name: names.portName } : p)),
          }
        : n)
    const envelope = await api.putGraph(
      props.workspaceId, { nodes: nextNodes }, graphRevision.value)
    nodes.value = envelope.graph.nodes ?? nextNodes
    graphRevision.value = envelope.graph_revision
    ElMessage.success('配置已保存——节点标记为待执行')
  } catch (error) {
    const status = (error as { response?: { status?: number } })?.response?.status
    if (status === 409) {
      ElMessage.error('图已被他人修改，已刷新——请重新保存')
      await refreshGraph().catch(() => {})
    } else {
      ElMessage.error(`保存配置失败：${await apiErrorDetail(error)}`)
    }
  }
}

// ── 删除节点（Inspector emit → 组装去掉该节点的新图 → PUT；后端同事务
//    清理来源绑定/当前输出/导出去处，下游仍引用则 422 点名违规节点） ──

async function onDeleteNode(): Promise<void> {
  const target = selectedNode.value
  if (!target) return
  try {
    await ElMessageBox.confirm(
      `删除「${target.name}」将同时清除其来源绑定与当前输出（历史运行输出保留）。`
      + '若下游节点仍引用它，需先改接或删除下游。',
      '删除节点', { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' })
  } catch {
    return
  }
  try {
    const nextNodes = nodes.value.filter(n => n.id !== target.id)
    const envelope = await api.putGraph(
      props.workspaceId, { nodes: nextNodes }, graphRevision.value)
    nodes.value = envelope.graph.nodes ?? nextNodes
    graphRevision.value = envelope.graph_revision
    // 后端同事务清理了被删节点的来源/当前输出/绑定——三面刷新回真
    const settled = await Promise.allSettled([
      refreshOutputs(), refreshSources(),
    ])
    reportRefreshFailures(settled)
    selectedRef.value = defaultSelectedRef()
    ElMessage.success('节点已删除')
  } catch (error) {
    const status = (error as { response?: { status?: number } })?.response?.status
    if (status === 409) {
      ElMessage.error('图已被他人修改，已刷新——请重新删除')
      await refreshGraph().catch(() => {})
    } else {
      // 422 等：透传后端消息（下游仍引用时点名违规节点，提示先处理下游）
      ElMessage.error(`删除节点失败：${await apiErrorDetail(error)}`)
    }
  }
}

// ── 试跑（不建 run 不写库；结果顶替输出区并出横幅） ──

function pseudoTrialOutput(
  nodeId: string, portId: string, dataset: Record<string, unknown>,
): WorkspaceOutput {
  const rows = Array.isArray(dataset.rows) ? dataset.rows : []
  return {
    run_id: 'trial',
    workspace_id: props.workspaceId,
    node_id: nodeId,
    port_id: portId,
    kind: 'table',
    content_inline: dataset,
    row_count: rows.length,
    content_hash: '',
    quality_json: null,
    is_current: false,
    stale_since_run: null,
    created_at: new Date().toISOString(),
  }
}

async function onTrial(): Promise<void> {
  const ref = selectedRef.value
  if (!ref || trialing.value) return
  const [nodeId, portId] = ref.split(':')
  trialing.value = true
  try {
    const result = await api.trialOutput(props.workspaceId, nodeId, portId)
    const dataset = result.outputs?.[portId]
    if (!dataset || typeof dataset !== 'object') {
      ElMessage.warning('试跑未返回该端口的结果')
      return
    }
    trialResult.value = {
      inputRows: result.input_rows,
      output: pseudoTrialOutput(nodeId, portId, dataset as Record<string, unknown>),
    }
  } catch (error) {
    ElMessage.error(`试跑失败：${await apiErrorDetail(error)}`)
  } finally {
    trialing.value = false
  }
}

// ── 接入资料（对话框落图+来源后：刷新全量并自动跑一次 source 节点） ──

async function onSourceCreated(nodeId: string): Promise<void> {
  const settled = await Promise.allSettled([
    api.getWorkspace(props.workspaceId).then(d => { workspace.value = d }),
    refreshGraph(),
    refreshOutputs(),
    refreshSources(),
  ])
  reportRefreshFailures(settled)
  selectedRef.value = `${nodeId}:main`
  // POST /sources 只冻快照不执行——前端补一次 scope=node 让底表立即有数据
  await startRun('node', nodeId)
}

async function onGraphConflict(): Promise<void> {
  await refreshGraph().catch(() => {})
}

// ── 添加算子（对话框确认 → 前端生成节点 → PUT 图 → 选中新节点） ──

const OP_NODE_META: Record<string, { label: string; portName: string; config: Record<string, unknown> }> = {
  select: { label: '选取列', portName: '选择结果', config: {} },
  filter: { label: '过滤行', portName: '筛选结果', config: { cond: 'eq' } },
  join: { label: '关联', portName: '关联结果', config: { method: 'left' } },
  split_sections: { label: '章节切分', portName: '章节长表', config: { level: 1 } },
  llm_extract: {
    label: 'LLM 抽取', portName: '抽取结果',
    config: { output_fields: [{ name: '字段1', type: 'text' }] },
  },
}

function nextNodeId(op: string): { id: string; seq: number } {
  const prefix = `${op}_`
  let max = 0
  for (const n of nodes.value) {
    if (!n.id.startsWith(prefix)) continue
    const seq = Number(n.id.slice(prefix.length))
    if (Number.isFinite(seq)) max = Math.max(max, seq)
  }
  const seq = max + 1
  return { id: `${prefix}${seq}`, seq }
}

async function onAddOperator(payload: { op: string; inputs: string[] }): Promise<void> {
  const meta = OP_NODE_META[payload.op]
  if (!meta) return
  const { id, seq } = nextNodeId(payload.op)
  const newNode: GraphNode = {
    id,
    op: payload.op,
    name: `${meta.label} ${seq}`,
    config: { ...meta.config },
    inputs: payload.inputs,
    ports: [{ id: 'main', name: meta.portName, kind: 'table' }],
    x: 0,
    y: nodes.value.reduce((max, n) => Math.max(max, n.y ?? 0), 0) + 1,
  }
  try {
    const envelope = await api.putGraph(
      props.workspaceId, { nodes: [...nodes.value, newNode] }, graphRevision.value)
    nodes.value = envelope.graph.nodes ?? [...nodes.value, newNode]
    graphRevision.value = envelope.graph_revision
    selectedRef.value = `${id}:main`
    ElMessage.success('算子已添加——在右侧配置参数后执行')
  } catch (error) {
    const status = (error as { response?: { status?: number } })?.response?.status
    if (status === 409) {
      ElMessage.error('图已被他人修改，已刷新——请重新添加')
      await refreshGraph().catch(() => {})
    } else {
      ElMessage.error(`添加算子失败：${await apiErrorDetail(error)}`)
    }
  }
}

// ── 导出（可导出端口判定：有 current 输出且非 stale 且有行） ──

const exportPorts = computed<ExportPortOption[]>(() =>
  nodes.value.flatMap(node => node.ports.map(port => {
    const ref = `${node.id}:${port.id}`
    const out = outputs.value[ref]
    const base = {
      nodeId: node.id, portId: port.id, ref,
      name: port.name, nodeLabel: node.name,
    }
    if (!out) {
      return { ...base, rowCount: null, exportable: false, reason: '该输出尚未执行' }
    }
    const rowCount = out.row_count ?? 0
    if (out.stale_since_run) {
      return { ...base, rowCount, exportable: false, reason: '输出已过期，需重新运行' }
    }
    if (!rowCount) {
      return { ...base, rowCount, exportable: false, reason: '输出无数据行' }
    }
    return { ...base, rowCount, exportable: true, reason: '' }
  })))

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
.ws-detail__run.is-completed { color: var(--kb-success, #10b981); }
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
.ws-detail__stale-banner :deep(.el-button) { margin-left: 10px; }
.ws-detail__output-body { min-width: 0; }

/* 右栏：Inspector */
.ws-detail__right {
  flex: 0 0 320px; width: 320px; min-width: 0;
}
</style>
