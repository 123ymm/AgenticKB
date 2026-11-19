<template>
  <div class="ws-stepflow">
    <article
      v-for="node in orderedNodes"
      :key="node.id"
      class="ws-stepflow__card"
      :data-node-id="node.id"
      :data-test="`step-node-${node.id}`"
      @click="selectNode(node)"
    >
      <header class="ws-stepflow__card-head">
        <el-icon class="ws-stepflow__op-icon" :class="`is-${node.op}`">
          <component :is="opIcon(node.op)" />
        </el-icon>
        <div class="ws-stepflow__card-title">
          <span class="ws-stepflow__node-name">{{ node.name }}</span>
          <span class="ws-stepflow__op-label">{{ opLabel(node.op) }}</span>
        </div>
        <span
          class="ws-stepflow__badge"
          :class="`is-${nodeStatus(node).key}`"
          :data-test="`step-badge-${node.id}`"
        >{{ nodeStatus(node).label }}</span>
      </header>

      <!-- 输入口行：依赖的上游 node:port（解析为「节点名 · 端口名」） -->
      <p v-if="node.inputs.length" class="ws-stepflow__inputs">
        <span class="ws-stepflow__inputs-label">输入</span>
        <span
          v-for="ref in node.inputs"
          :key="ref"
          class="ws-stepflow__input-chip"
          :title="ref"
        >{{ describeRef(ref) }}</span>
      </p>

      <!-- 输出端口按钮组：点击选中 → 父级输出区切换 -->
      <div class="ws-stepflow__ports">
        <button
          v-for="port in node.ports"
          :key="port.id"
          type="button"
          class="ws-stepflow__port"
          :class="{
            'is-selected': selectedRef === portRef(node.id, port.id),
            'is-stale': portSummary(node.id, port.id).stale,
          }"
          :data-test="`port-${node.id}-${port.id}`"
          @click.stop="emit('select', portRef(node.id, port.id))"
        >
          <span class="ws-stepflow__port-name">{{ port.name }}</span>
          <span class="ws-stepflow__port-meta">{{ portMeta(node.id, port.id) }}</span>
        </button>
      </div>
    </article>

    <!-- 入口按钮：本任务只 emit，对话框归 Task 12 -->
    <div class="ws-stepflow__entries">
      <el-button data-test="add-source" @click="emit('add-source')">+ 接入资料</el-button>
      <el-button data-test="add-operator" @click="emit('add-operator')">+ 添加算子</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  Connection,
  Filter,
  FolderOpened,
  List,
  MagicStick,
  Scissor,
} from '@element-plus/icons-vue'
import type { Component } from 'vue'
import type { GraphNode, WorkspaceOutput } from '@/api/workspaces'

/**
 * 步骤流（垂直步骤卡，替代画布——高保真 workspace-1116 窄屏形态）。
 * 纯展示+选中交互组件：图数据/输出概要/选中 ref 全由父级喂入，编辑面
 * （接入资料/添加算子对话框）只 emit 事件由父级承接（Task 12 接线）。
 *
 * 状态口径：
 * - pending / stale 节点级标志来自 graph GET（服务端 diff 写回——客户端
 *   PUT 时写的两标志被丢弃）；
 * - 端口级 stale 来自 current outputs 的 stale_since_run；
 * - 优先级 pending > stale > 已完成（任一端口有 current 输出）> 未执行。
 */
const props = defineProps<{
  nodes: GraphNode[]
  /** current outputs 信封的 outputs（键=node:port）。 */
  outputs: Record<string, WorkspaceOutput>
  selectedRef?: string | null
}>()

const emit = defineEmits<{
  select: [ref: string]
  'add-source': []
  'add-operator': []
}>()

const OP_ICONS: Record<string, Component> = {
  source: FolderOpened,
  select: List,
  filter: Filter,
  join: Connection,
  split_sections: Scissor,
  llm_extract: MagicStick,
}

const OP_LABELS: Record<string, string> = {
  source: '资料来源',
  select: '选取列',
  filter: '过滤行',
  join: '关联',
  split_sections: '章节切分',
  llm_extract: 'LLM 抽取',
}

function opIcon(op: string): Component {
  return OP_ICONS[op] ?? List
}

function opLabel(op: string): string {
  return OP_LABELS[op] ?? op
}

function portRef(nodeId: string, portId: string): string {
  return `${nodeId}:${portId}`
}

/**
 * 整卡点击选中（用户反馈问题2）：卡片任意处（header/卡片体）点击 → 选中
 * 该节点首个输出端口；端口按钮点击仍精确选对应口（@click.stop 阻止冒泡，
 * 否则会追加一次首口选中）。
 */
function selectNode(node: GraphNode): void {
  const first = node.ports[0]
  if (first) emit('select', portRef(node.id, first.id))
}

function portSummary(nodeId: string, portId: string): {
  hasOutput: boolean
  rowCount: number | null
  stale: boolean
} {
  const found = props.outputs[portRef(nodeId, portId)]
  return {
    hasOutput: !!found,
    rowCount: found?.row_count ?? null,
    stale: !!found?.stale_since_run,
  }
}

function portMeta(nodeId: string, portId: string): string {
  const summary = portSummary(nodeId, portId)
  if (!summary.hasOutput) return '未执行'
  const count = `${summary.rowCount ?? '?'} 行`
  return summary.stale ? `${count} · 已过期` : count
}

type NodeStatusKey = 'idle' | 'done' | 'stale' | 'pending'

function nodeStatus(node: GraphNode): { key: NodeStatusKey; label: string } {
  if (node.pending) return { key: 'pending', label: '配置待执行' }
  const portStale = node.ports.some(p => portSummary(node.id, p.id).stale)
  if (node.stale || portStale) return { key: 'stale', label: '待更新' }
  const hasOutput = node.ports.some(p => portSummary(node.id, p.id).hasOutput)
  if (hasOutput) return { key: 'done', label: '已完成' }
  return { key: 'idle', label: '未执行' }
}

/** "node:port" → 「节点名 · 端口名」（解析不到回落原始 ref）。 */
function describeRef(ref: string): string {
  const [nodeId, portId] = ref.split(':')
  const node = nodeIndex.value.get(nodeId ?? '')
  if (!node || !portId) return ref
  const port = node.ports.find(p => p.id === portId)
  return port ? `${node.name} · ${port.name}` : ref
}

const nodeIndex = computed(() => new Map(props.nodes.map(n => [n.id, n])))

/**
 * 拓扑序（Kahn）：依赖（inputs 的 node:port 前段）在前。环不可能——后端
 * PUT graph 校验拒绝；防御性兜底把未入序节点按原序追加。无依赖节点间保持
 * 传入顺序（稳定）。
 */
const orderedNodes = computed(() => topoSort(props.nodes))

function topoSort(nodes: GraphNode[]): GraphNode[] {
  const indegree = new Map<string, number>(nodes.map(n => [n.id, 0]))
  const dependents = new Map<string, string[]>()
  for (const node of nodes) {
    for (const input of node.inputs) {
      const upstream = input.split(':')[0]
      if (!indegree.has(upstream) || upstream === node.id) continue
      indegree.set(node.id, (indegree.get(node.id) ?? 0) + 1)
      dependents.set(upstream, [...(dependents.get(upstream) ?? []), node.id])
    }
  }
  const queue = nodes.filter(n => (indegree.get(n.id) ?? 0) === 0).map(n => n.id)
  const ordered: GraphNode[] = []
  while (queue.length > 0) {
    const id = queue.shift()!
    const node = nodeIndex.value.get(id)
    if (node) ordered.push(node)
    for (const next of dependents.get(id) ?? []) {
      const left = (indegree.get(next) ?? 0) - 1
      indegree.set(next, left)
      if (left === 0) queue.push(next)
    }
  }
  if (ordered.length < nodes.length) {
    const seen = new Set(ordered.map(n => n.id))
    for (const node of nodes) if (!seen.has(node.id)) ordered.push(node)
  }
  return ordered
}
</script>

<style scoped>
.ws-stepflow { display: flex; flex-direction: column; gap: 0; }

.ws-stepflow__card {
  position: relative;
  padding: 12px 14px;
  border: 1px solid var(--kb-border, #e2e8f0);
  border-radius: 10px;
  background: var(--kb-bg-card, #fff);
  /* 整卡可点击（用户反馈问题2）：hover 给可点击视觉 + 指针 */
  cursor: pointer;
  transition: border-color .12s var(--kb-ease, ease);
}
.ws-stepflow__card:hover {
  border-color: var(--kb-accent, #0891b2);
}

/* 步骤连线：卡之间的竖向发丝线（最后一张卡后不画） */
.ws-stepflow__card + .ws-stepflow__card { margin-top: 26px; }
.ws-stepflow__card + .ws-stepflow__card::before {
  content: '';
  position: absolute;
  top: -27px; left: 30px;
  width: 1px; height: 28px;
  background: var(--kb-border, #e2e8f0);
}

.ws-stepflow__card-head { display: flex; align-items: center; gap: 10px; }

.ws-stepflow__op-icon {
  display: inline-grid; place-items: center; flex-shrink: 0;
  width: 32px; height: 32px; border-radius: 8px;
  border: 1px solid var(--kb-border, #e2e8f0);
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.08));
  color: var(--kb-accent, #0891b2);
  font-size: 15px;
}
.ws-stepflow__card-title {
  display: flex; flex-direction: column; gap: 1px;
  min-width: 0; flex: 1;
}
.ws-stepflow__node-name {
  font-size: 13.5px; font-weight: 600;
  color: var(--kb-text-primary, #0f172a);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ws-stepflow__op-label {
  font-size: 11px; letter-spacing: .02em;
  color: var(--kb-text-tertiary, #94a3b8);
}

/* 状态徽章四态（soft 章，同制品 soft-badge 语言） */
.ws-stepflow__badge {
  flex-shrink: 0;
  border-radius: 4px;
  padding: 1px 8px;
  font-size: 11px; font-weight: 500;
  background: var(--kb-bg-card-hover, #f1f5f9);
  color: var(--kb-text-secondary, #64748b);
}
.ws-stepflow__badge.is-done {
  background: var(--kb-success-soft, rgba(16, 185, 129, 0.1));
  color: var(--kb-success, #10b981);
}
.ws-stepflow__badge.is-stale {
  background: var(--kb-warning-soft, rgba(245, 158, 11, 0.12));
  color: var(--kb-warning, #f59e0b);
}
.ws-stepflow__badge.is-pending {
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.1));
  color: var(--kb-accent, #0891b2);
}

.ws-stepflow__inputs {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  margin: 10px 0 0; padding: 0;
}
.ws-stepflow__inputs-label {
  font-size: 11px; color: var(--kb-text-tertiary, #94a3b8);
}
.ws-stepflow__input-chip {
  border-radius: 4px;
  padding: 1px 7px;
  font-size: 11px;
  background: var(--kb-bg-card-hover, #f1f5f9);
  color: var(--kb-text-secondary, #64748b);
  max-width: 220px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 输出端口按钮组 */
.ws-stepflow__ports {
  display: flex; flex-wrap: wrap; gap: 8px;
  margin-top: 10px;
}
.ws-stepflow__port {
  display: inline-flex; align-items: center; gap: 7px;
  border: 1px solid var(--kb-border, #e2e8f0);
  border-radius: 7px;
  padding: 5px 10px;
  cursor: pointer;
  background: var(--kb-bg-card, #fff);
  font-family: inherit;
  transition: border-color .12s var(--kb-ease, ease), background .12s var(--kb-ease, ease);
}
.ws-stepflow__port:hover {
  border-color: var(--kb-accent, #0891b2);
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.06));
}
.ws-stepflow__port.is-selected {
  border-color: var(--kb-accent, #0891b2);
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.1));
}
.ws-stepflow__port.is-stale:not(.is-selected) { border-style: dashed; }
.ws-stepflow__port-name {
  font-size: 12px; font-weight: 500;
  color: var(--kb-text-primary, inherit);
}
.ws-stepflow__port.is-selected .ws-stepflow__port-name { color: var(--kb-accent, #0891b2); }
.ws-stepflow__port-meta {
  font-size: 11px;
  color: var(--kb-text-tertiary, #94a3b8);
  font-variant-numeric: tabular-nums;
}

/* 入口按钮 */
.ws-stepflow__entries {
  display: flex; gap: 10px;
  margin-top: 22px;
}
.ws-stepflow__entries .el-button + .el-button { margin-left: 0; }
</style>
