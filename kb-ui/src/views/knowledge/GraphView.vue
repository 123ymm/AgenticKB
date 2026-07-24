<template>
  <div class="graph-view">
    <!-- Header -->
    <div class="graph-view__header">
      <h2 class="graph-view__title">知识图谱</h2>
      <div class="graph-view__actions">
        <el-select v-model="filterType" placeholder="关系类型" size="default" clearable style="width: 160px">
          <el-option v-for="rt in relationTypes" :key="rt" :label="relationTypeLabel(rt)" :value="rt" />
        </el-select>
        <el-button @click="loadData" :loading="loading">
          <el-icon><Refresh /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- Stats Bar -->
    <div class="graph-view__stats" v-if="relations.length">
      <span class="graph-view__stat">
        <strong>{{ graphNodes.length }}</strong> 节点
      </span>
      <span class="graph-view__stat">
        <strong>{{ graphEdges.length }}</strong> 条关系
      </span>
      <span class="graph-view__stat">
        <strong>{{ relationTypes.length }}</strong> 种类型
      </span>
    </div>

    <!-- Graph -->
    <div class="graph-view__canvas" v-if="graphNodes.length">
      <ForceGraph :nodes="graphNodes" :edges="graphEdges" :categories="categories" height="560px" />
    </div>

    <!-- Empty -->
    <EmptyState v-if="!loading && !graphNodes.length && loadedOnce" text="暂无关系数据，请先执行 Mining 构建" />

    <!-- Relation Table -->
    <div class="graph-view__table-section" v-if="filteredRelations.length">
      <h4 class="card-heading">关系列表</h4>
      <div class="graph-view__table-wrap">
        <el-table
          :data="pagedRelations"
          class="kb-table"
          :header-cell-style="{ background: 'transparent' }"
          size="default"
        >
          <el-table-column label="源分段" min-width="200">
            <template #default="{ row }">
              <span class="text-preview expandable" :class="{ 'is-expanded': expandedTexts.has(`s-${row.source_segment_id}`) }" @click="toggleText(`s-${row.source_segment_id}`)">{{ row.source_text || row.source_segment_id }}</span>
            </template>
          </el-table-column>
          <el-table-column label="关系" width="140">
            <template #default="{ row }">
              <span class="relation-badge">{{ relationTypeLabel(row.relation_type) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="目标分段" min-width="200">
            <template #default="{ row }">
              <span class="text-preview expandable" :class="{ 'is-expanded': expandedTexts.has(`t-${row.target_segment_id}`) }" @click="toggleText(`t-${row.target_segment_id}`)">{{ row.target_text || row.target_segment_id }}</span>
            </template>
          </el-table-column>
          <el-table-column label="置信度" width="100" align="center">
            <template #default="{ row }">
              {{ row.confidence?.toFixed(2) ?? '-' }}
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div class="graph-view__pagination" v-if="filteredRelations.length > pageSize">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="pageSize"
          :total="filteredRelations.length"
          layout="prev, pager, next"
          size="small"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { useDomainStore } from '@/stores/domain'
import { useMiningApi } from '@/api/mining'
import type { KnowledgeRelation } from '@/types'
import type { GraphNode, GraphEdge } from '@/components/charts/ForceGraph.vue'
import ForceGraph from '@/components/charts/ForceGraph.vue'
import EmptyState from '@/components/common/EmptyState.vue'

const domainStore = useDomainStore()
const miningApi = useMiningApi()

const loading = ref(false)
const loadedOnce = ref(false)
const relations = ref<KnowledgeRelation[]>([])
const filterType = ref('')
const currentPage = ref(1)
const pageSize = 30
const expandedTexts = ref(new Set<string>())
const serverPage = ref(1)
const SERVER_PAGE_SIZE = 200

function toggleText(key: string) {
  if (expandedTexts.value.has(key)) {
    expandedTexts.value.delete(key)
  } else {
    expandedTexts.value.add(key)
  }
  // Trigger reactivity
  expandedTexts.value = new Set(expandedTexts.value)
}

const relationTypes = computed(() => {
  const types = new Set(relations.value.map(r => r.relation_type))
  return Array.from(types).sort()
})

const filteredRelations = computed(() => {
  if (!filterType.value) return relations.value
  return relations.value.filter(r => r.relation_type === filterType.value)
})

const pagedRelations = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  return filteredRelations.value.slice(start, start + pageSize)
})

const categories = computed(() =>
  relationTypes.value.map(t => ({ name: relationTypeLabel(t) }))
)

// Build graph nodes/edges from flat relation data
const graphNodes = computed<GraphNode[]>(() => {
  const nodeMap = new Map<string, GraphNode>()
  filteredRelations.value.forEach(r => {
    if (!nodeMap.has(r.source_segment_id)) {
      const catIdx = relationTypes.value.indexOf(r.relation_type)
      nodeMap.set(r.source_segment_id, {
        id: r.source_segment_id,
        name: shortName(r.source_text, r.source_segment_id.slice(0, 8)),
        category: catIdx >= 0 ? catIdx : 0,
        value: 1,
      })
    } else {
      const existing = nodeMap.get(r.source_segment_id)!
      nodeMap.set(r.source_segment_id, { ...existing, value: existing.value! + 1 })
    }
    if (!nodeMap.has(r.target_segment_id)) {
      nodeMap.set(r.target_segment_id, {
        id: r.target_segment_id,
        name: shortName(r.target_text, r.target_segment_id.slice(0, 8)),
        category: 0,
        value: 1,
      })
    } else {
      const existing = nodeMap.get(r.target_segment_id)!
      nodeMap.set(r.target_segment_id, { ...existing, value: existing.value! + 1 })
    }
  })
  return Array.from(nodeMap.values())
})

const graphEdges = computed<GraphEdge[]>(() =>
  filteredRelations.value.map(r => ({
    source: r.source_segment_id,
    target: r.target_segment_id,
    relationType: r.relation_type,
    weight: r.confidence,
  }))
)

function shortName(text: string | null | undefined, fallback: string) {
  if (!text) return fallback
  return text.length > 24 ? text.slice(0, 24) + '...' : text
}

function relationTypeLabel(type: string) {
  const map: Record<string, string> = {
    elaboration: '详述', contrast: '对比', sequence: '顺序',
    cause_effect: '因果', problem_solution: '问题-方案',
    similarity: '相似', dependency: '依赖', reference: '引用',
  }
  return map[type] || type
}

async function loadData() {
  loading.value = true
  try {
    const res = await miningApi.getRelations({
      limit: SERVER_PAGE_SIZE,
      offset: (serverPage.value - 1) * SERVER_PAGE_SIZE,
    })
    relations.value = res.items ?? []
    loadedOnce.value = true
  } catch {
    relations.value = []
    loadedOnce.value = true
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
watch(() => domainStore.currentDomain, () => { serverPage.value = 1; loadData() })
watch(serverPage, loadData)
</script>

<style scoped>
.graph-view {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.graph-view__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.graph-view__title {
  font-size: 16px;
  font-weight: 650;
  color: var(--kb-text-primary);
  margin: 0;
  letter-spacing: -0.2px;
}

.graph-view__actions {
  display: flex;
  gap: 8px;
}

/* Stats bar */
.graph-view__stats {
  display: flex;
  gap: 20px;
  font-size: 13px;
  color: var(--kb-text-secondary);
}

.graph-view__stat strong {
  color: var(--kb-accent);
  font-weight: 700;
}

/* Canvas */
.graph-view__canvas {
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border-light);
  border-radius: var(--kb-radius);
  overflow: hidden;
}

/* Table */
.graph-view__table-section {
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border-light);
  border-radius: var(--kb-radius);
  padding: 18px 20px;
}

.card-heading {
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 0 0 14px;
}

.graph-view__table-wrap {
  overflow: hidden;
}

.graph-view__pagination {
  display: flex;
  justify-content: center;
  margin-top: 12px;
}

.text-preview {
  font-size: 12px;
  color: var(--kb-text-secondary);
  line-height: 1.4;
}

.text-preview.expandable {
  cursor: pointer;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-all;
  transition: all 0.15s ease;
}

.text-preview.expandable:hover {
  color: var(--kb-accent);
}

.text-preview.expandable.is-expanded {
  -webkit-line-clamp: unset;
  display: block;
  white-space: pre-wrap;
}

.relation-badge {
  display: inline-block;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 3px;
  background: var(--kb-accent-soft);
  color: var(--kb-accent);
  font-weight: 600;
}
</style>
