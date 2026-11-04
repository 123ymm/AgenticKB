<template>
  <section class="wiki-graph" data-test="wiki-graph-panel">
    <!-- 错误/混版态：非空白（EmptyState 文案 + 重试入口） -->
    <div v-if="errorText" class="wiki-graph__state" data-test="wiki-graph-error">
      <EmptyState :text="errorText" />
      <button type="button" class="wiki-graph__retry" data-test="wiki-graph-retry" @click="reload">
        重试
      </button>
    </div>

    <!-- 空态：0 页 Wiki（66号 D03.1 验收明文） -->
    <EmptyState
      v-else-if="!loading && pages.length === 0"
      text="该 Wiki 暂无页面——编译或新建页面后，这里展示页面与引用关系"
      data-test="wiki-graph-empty"
    />

    <template v-else>
      <div class="wiki-graph__toolbar">
        <el-input
          v-model="searchQuery"
          size="small"
          clearable
          class="wiki-graph__search"
          placeholder="搜索页面标题"
          data-test="wiki-graph-search"
        />
        <el-switch
          v-model="oneHopOnly"
          size="small"
          active-text="仅所选页及一跳"
          data-test="wiki-graph-one-hop"
        />
        <span class="wiki-graph__stats" data-test="wiki-graph-stats">
          {{ visible.nodes.length }} 页 · {{ visible.edges.length }} 引用
          <template v-if="brokenCount > 0"> · 断链 {{ brokenCount }}</template>
          · r{{ revision }}
        </span>
      </div>

      <div class="wiki-graph__body">
        <div class="wiki-graph__canvas">
          <VueFlow
            :key="flowKey"
            :nodes="flowNodes"
            :edges="flowEdges"
            :nodes-draggable="false"
            :nodes-connectable="false"
            :edges-updatable="false"
            :elements-selectable="true"
            :delete-key-code="null"
            :min-zoom="0.3"
            :max-zoom="1.6"
            fit-view-on-init
            @node-click="onNodeClick"
          >
            <template #node-wiki="nodeProps">
              <Handle type="target" :position="Position.Left" :connectable="false" />
              <article
                class="wiki-graph-node"
                :class="[
                  `is-${nodeProps.data.pageType || 'broken'}`,
                  { 'is-broken': nodeProps.data.isBroken,
                    'is-selected': selectedKey === nodeProps.id },
                ]"
              >
                <span class="wiki-graph-node__type">{{ nodeProps.data.typeLabel }}</span>
                <strong :title="nodeProps.data.label">{{ nodeProps.data.label }}</strong>
                <small v-if="!nodeProps.data.isBroken">
                  出链 {{ nodeProps.data.outCount }} · 被引用 {{ nodeProps.data.inCount }}
                </small>
              </article>
              <Handle type="source" :position="Position.Right" :connectable="false" />
            </template>
            <Background pattern-color="var(--kb-border)" :gap="22" />
            <Controls :show-interactive="false" />
          </VueFlow>
          <p v-if="searchActive && visible.nodes.length === 0" class="wiki-graph__no-hit">
            没有匹配的页面
          </p>
        </div>

        <aside class="wiki-graph__detail" data-test="wiki-graph-detail" aria-live="polite">
          <template v-if="selectedNode">
            <div class="wiki-graph__detail-head">
              <span class="wiki-graph__detail-type">{{ selectedNode.typeLabel }}</span>
              <h4 :title="selectedNode.label">{{ selectedNode.label }}</h4>
            </div>
            <dl class="wiki-graph__detail-meta">
              <div><dt>来源</dt><dd>{{ selectedNode.origin || '—' }}</dd></div>
              <div><dt>页键</dt><dd :title="selectedNode.id">{{ selectedNode.id }}</dd></div>
              <div><dt>出链</dt><dd>{{ selectedNode.outCount }}</dd></div>
              <div><dt>被引用</dt><dd>{{ selectedNode.inCount }}</dd></div>
            </dl>
            <button
              type="button"
              class="wiki-graph__open"
              data-test="wiki-graph-open"
              @click="emit('open-page', selectedNode.id)"
            >打开页面</button>
          </template>
          <p v-else class="wiki-graph__detail-hint">点击节点查看页面详情</p>
        </aside>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { Handle, Position, VueFlow, useVueFlow, type Edge, type Node } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'

import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import EmptyState from '@/components/common/EmptyState.vue'
import {
  buildWikiGraph,
  filterWikiGraph,
  restrictToOneHop,
  type WikiGraphNode,
} from '@/utils/wikiGraph'
import type {
  WikiLink,
  WikiLinksEnvelope,
  WikiPageMeta,
  WikiPagesEnvelope,
} from '@/types/artifacts'

/**
 * Wiki 图谱视图（66号 §7 D03.1）——只读 vue-flow 节点图：节点=本制品页面、
 * 箭头=正文引用方向。设计态读 draft（editor+ 由入口守卫保证，组件不重复
 * 鉴权）；运行态读正式 revision。不提供任何写操作——唯一出口是「打开页面」
 * 导航事件（回页面视图定位该页）。引用解析以服务端 links 为准。
 */
const props = defineProps<{
  artifactId: string
  /** true=设计态草稿视图（rev0/干净态投影）；false=运行态正式版。 */
  draft: boolean
}>()

const emit = defineEmits<{ 'open-page': [pageKey: string] }>()

const api = useArtifactsApi()

const loading = ref(false)
const loadError = ref('')
const revisionMismatch = ref(false)
const pages = ref<WikiPageMeta[]>([])
const links = ref<WikiLink[]>([])
const revision = ref(0)

const searchQuery = ref('')
const oneHopOnly = ref(false)
const selectedKey = ref('')

const model = computed(() => buildWikiGraph(pages.value, links.value))
const visible = computed(() => {
  let current = model.value
  if (searchQuery.value.trim()) current = filterWikiGraph(current, searchQuery.value)
  if (oneHopOnly.value) current = restrictToOneHop(current, selectedKey.value || null)
  return current
})
const searchActive = computed(() => searchQuery.value.trim() !== '')
const brokenCount = computed(() => model.value.nodes.filter(node => node.isBroken).length)
const selectedNode = computed<WikiGraphNode | null>(() => (
  visible.value.nodes.find(node => node.id === selectedKey.value && !node.isBroken) ?? null
))

const errorText = computed(() => {
  if (revisionMismatch.value) return '版本切换中，请重试（页面与引用数据版本不一致，可能刚发生发布）'
  if (loadError.value) return `图谱数据加载失败：${loadError.value}`
  return ''
})

/** 结构性变化（版本/选中/一跳开关）才重建画布（fitViewOnInit 重适配）；
 * 搜索只收窄节点集、不重挂——保留用户缩放/平移（审查 #3），视图适配由
 * watch(searchQuery) 的 fitView 补位。 */
const flowKey = computed(() => (
  `${revision.value}:${selectedKey.value}:${oneHopOnly.value}`))

const flowNodes = computed<Node[]>(() => visible.value.nodes.map(node => ({
  id: node.id,
  type: 'wiki',
  position: { ...node.position },
  class: node.isBroken ? 'wiki-graph-node--broken' : undefined,
  data: { ...node },
  draggable: false,
  connectable: false,
  selectable: !node.isBroken,
})))

const flowEdges = computed<Edge[]>(() => visible.value.edges.map(edge => ({
  id: edge.id,
  source: edge.source,
  target: edge.target,
  type: 'smoothstep',
  class: edge.isBroken ? 'wiki-graph-edge--broken' : undefined,
  animated: false,
  style: {
    stroke: 'var(--kb-text-tertiary)',
    strokeWidth: 1.5,
    ...(edge.isBroken ? { strokeDasharray: '7 4' } : {}),
  },
})))

/** 与模板 <VueFlow> 共享同一 flow store（useVueFlow 先建后 provide，子组件
 * inject 复用）——父层驱动视图适配，搜索收窄不整幅重挂。 */
const { fitView } = useVueFlow()

onMounted(reload)

/** 搜索过滤后节点集变化 → 重适配视图一次（nextTick 等节点 props 落画布）。
 * fitView 对未测量节点（含桩环境无画布）安全早退，不抛错。 */
watch(searchQuery, () => {
  void nextTick(() => { void fitView({ padding: 0.12, duration: 200 }) })
})

/** 制品切换（路由复用组件不重挂）→ 整体重置。 */
watch(() => props.artifactId, () => {
  searchQuery.value = ''
  oneHopOnly.value = false
  selectedKey.value = ''
  void reload()
})

/** 选中页被过滤收窄剔除 → 清选中（详情区不残留不可见节点）。 */
watch(() => visible.value.nodes, nodes => {
  if (selectedKey.value && !nodes.some(node => node.id === selectedKey.value)) {
    selectedKey.value = ''
  }
})

/**
 * 加载 pages/links 两信封 + 混版防护（66号 D03.1：两请求之间发生发布/
 * 回滚须可检出）：revision 不一致 → 整对重拉一次；仍不一致 → 错误态
 * （不用半新半旧数据拼图）。
 *
 * 请求序号守卫（审查 #2）：reload 并发（artifactId 快切/重试双击）时，
 * 迟到的旧响应在任一落地前被丢弃，不串台覆盖新制品数据——先例=
 * Reader 的 selectedKey 竞态守卫。
 */
let loadEpoch = 0

async function reload(): Promise<void> {
  const epoch = ++loadEpoch
  loading.value = true
  loadError.value = ''
  revisionMismatch.value = false
  try {
    let [pagesEnv, linksEnv] = await fetchPair()
    if (epoch !== loadEpoch) return
    if (pagesEnv.revision !== linksEnv.revision) {
      [pagesEnv, linksEnv] = await fetchPair()
      if (epoch !== loadEpoch) return
      if (pagesEnv.revision !== linksEnv.revision) {
        revisionMismatch.value = true
        return
      }
    }
    pages.value = pagesEnv.pages
    links.value = linksEnv.links
    revision.value = pagesEnv.revision
    if (selectedKey.value && !pagesEnv.pages.some(page => page.page_key === selectedKey.value)) {
      selectedKey.value = ''
    }
  } catch (error) {
    const message = await apiErrorDetail(error)
    if (epoch !== loadEpoch) return
    pages.value = []
    links.value = []
    loadError.value = message
  } finally {
    if (epoch === loadEpoch) loading.value = false
  }
}

/** 一对请求（同一 draft 口径；links 不带 page_key=全量——图谱视图口径）。 */
async function fetchPair(): Promise<[WikiPagesEnvelope, WikiLinksEnvelope]> {
  return Promise.all([
    api.listWikiPages(props.artifactId, { draft: props.draft }),
    api.listWikiLinks(props.artifactId, { draft: props.draft }),
  ])
}

/** 画布节点点击：断链伪节点不选中（不可打开，虚线已区分）。 */
function onNodeClick(event: { node: { id: string } }): void {
  const node = model.value.nodes.find(item => item.id === event.node.id)
  if (!node || node.isBroken) return
  selectedKey.value = node.id
}

defineExpose({ reload })
</script>

<style scoped>
.wiki-graph { display: flex; flex-direction: column; gap: 10px; min-height: 420px; }
.wiki-graph__state { display: flex; flex-direction: column; align-items: center; gap: 8px; }
.wiki-graph__retry {
  border: 1px solid var(--kb-border, #dcdfe6);
  border-radius: 6px;
  background: var(--kb-bg-card, #fff);
  padding: 6px 16px;
  cursor: pointer;
  font-size: 13px;
}
.wiki-graph__retry:hover { color: var(--kb-accent, #2563eb); border-color: var(--kb-accent, #2563eb); }
.wiki-graph__toolbar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.wiki-graph__search { width: min(260px, 60vw); }
.wiki-graph__stats { margin-left: auto; color: var(--kb-text-tertiary, #909399); font-size: 12px; }
.wiki-graph__body { display: flex; gap: 12px; min-height: 460px; }
.wiki-graph__canvas {
  position: relative;
  flex: 1;
  min-width: 0;
  border: 1px solid var(--kb-border, #ebeef5);
  border-radius: 6px;
  background: var(--kb-bg-card, #fff);
}
.wiki-graph__canvas :deep(.vue-flow) { height: 460px; }
.wiki-graph__no-hit {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  margin: 0;
  color: var(--kb-text-tertiary, #909399);
  font-size: 13px;
  pointer-events: none;
}
.wiki-graph-node {
  display: grid;
  width: 172px;
  min-height: 58px;
  align-content: center;
  gap: 3px;
  box-sizing: border-box;
  border: 1px solid var(--kb-border, #dcdfe6);
  border-left: 4px solid var(--kb-accent, #2563eb);
  border-radius: 6px;
  background: var(--kb-bg-card, #fff);
  box-shadow: 0 1px 3px rgba(0, 0, 0, .08);
  padding: 8px 10px;
  color: var(--kb-text-primary, #303133);
}
.wiki-graph-node.is-glossary { border-left-color: var(--el-color-success, #67c23a); }
.wiki-graph-node.is-contradiction { border-left-color: var(--el-color-danger, #f56c6c); }
.wiki-graph-node.is-open_question { border-left-color: var(--el-color-warning, #e6a23c); }
.wiki-graph-node.is-selected { box-shadow: 0 0 0 3px var(--kb-accent-soft, #d9ecff); }
/* 断链伪节点：虚线边框 + 弱化（不伪装成有效页面） */
.wiki-graph-node--broken .wiki-graph-node,
.wiki-graph-node.is-broken {
  border-style: dashed;
  border-left-style: dashed;
  opacity: .72;
}
.wiki-graph-node span { color: var(--kb-text-tertiary, #909399); font-size: 10px; font-weight: 700; }
.wiki-graph-node strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12.5px;
}
.wiki-graph-node small { color: var(--kb-text-tertiary, #909399); font-size: 10.5px; }
.wiki-graph__detail {
  width: 250px;
  flex-shrink: 0;
  border: 1px solid var(--kb-border, #ebeef5);
  border-radius: 6px;
  padding: 14px;
  background: var(--kb-bg-card, #fff);
}
.wiki-graph__detail-head h4 {
  margin: 4px 0 10px;
  font-size: 15px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wiki-graph__detail-type { color: var(--kb-accent, #2563eb); font-size: 11px; font-weight: 700; }
.wiki-graph__detail-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 0; }
.wiki-graph__detail-meta > div { border: 1px solid var(--kb-border-light, #f2f6fc); border-radius: 4px; padding: 7px 8px; min-width: 0; }
.wiki-graph__detail-meta dt { color: var(--kb-text-tertiary, #909399); font-size: 10.5px; }
.wiki-graph__detail-meta dd {
  margin: 2px 0 0;
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wiki-graph__open {
  width: 100%;
  margin-top: 12px;
  border: 1px solid var(--kb-accent, #2563eb);
  border-radius: 6px;
  background: var(--kb-accent, #2563eb);
  color: #fff;
  padding: 7px 0;
  cursor: pointer;
  font-size: 13px;
}
.wiki-graph__open:hover { opacity: .88; }
.wiki-graph__detail-hint { margin: 20px 0; color: var(--kb-text-tertiary, #909399); font-size: 12.5px; text-align: center; }
:deep(.vue-flow__handle) { width: 7px; height: 7px; border: 2px solid var(--kb-bg-card, #fff); background: var(--kb-text-tertiary, #909399); }
:deep(.vue-flow__controls) { border: 1px solid var(--kb-border, #dcdfe6); box-shadow: 0 1px 3px rgba(0, 0, 0, .1); }
@media (max-width: 1100px) {
  .wiki-graph__body { flex-direction: column; }
  .wiki-graph__detail { width: auto; }
}
</style>
