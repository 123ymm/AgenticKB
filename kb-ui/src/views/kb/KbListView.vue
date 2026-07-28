<template>
  <div class="kb-list-view">
    <!-- Header -->
    <div class="kb-list-view__header">
      <div class="kb-list-view__header-left">
        <h2 class="kb-list-view__title">知识库</h2>
        <span class="kb-list-view__count">{{ kbs.length }} 个</span>
        <span class="kb-list-view__domain">@ {{ domainStore.currentDomain }}</span>
      </div>
      <div class="kb-list-view__actions">
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
        </el-button>
        <el-button type="primary" @click="showCreate = true">
          <el-icon class="el-icon--left"><Plus /></el-icon>
          新建知识库
        </el-button>
      </div>
    </div>

    <!-- Table -->
    <div class="kb-list-view__table-wrap">
      <el-table
        :data="kbs"
        v-loading="loading"
        class="kb-table"
        :header-cell-style="{ background: 'transparent' }"
      >
        <el-table-column label="名称" min-width="220">
          <template #default="{ row }">
            <router-link :to="`/kb/${row.id}`" class="kb-name-link">
              <el-icon class="kb-name-icon"><Document /></el-icon>
              <span>{{ row.name }}</span>
            </router-link>
            <div v-if="row.description" class="kb-desc">{{ row.description }}</div>
          </template>
        </el-table-column>
        <el-table-column label="可见性" width="100">
          <template #default="{ row }">
            <el-tag :type="visibilityTagType(row.visibility)" size="small" effect="light">
              {{ visibilityLabel(row.visibility) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="我的角色" width="110">
          <template #default="{ row }">
            <el-tag :type="roleTagType(row.my_role)" size="small" effect="plain">
              {{ roleLabel(row.my_role) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="文档" width="80" align="right">
          <template #default="{ row }">
            <span class="kb-doc-count">{{ row.document_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" min-width="160">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <router-link :to="`/kb/${row.id}`">
              <el-button type="primary" size="small" text>进入</el-button>
            </router-link>
          </template>
        </el-table-column>
        <template #empty>
          <EmptyState text="当前域还没有知识库，点击右上角「新建知识库」开始" />
        </template>
      </el-table>
    </div>

    <KbCreateDialog v-model="showCreate" :domain="domainStore.currentDomain" @created="load" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { Document, Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useDomainStore } from '@/stores/domain'
import { useKbApi } from '@/api/kb'
import { apiErrorDetail } from '@/api/proxyClient'
import EmptyState from '@/components/common/EmptyState.vue'
import KbCreateDialog from '@/components/kb/KbCreateDialog.vue'
import { roleLabel, roleTagType, visibilityLabel, visibilityTagType } from '@/views/kb/kbMeta'
import type { KbSummary } from '@/types/kb'

const domainStore = useDomainStore()
const kbApi = useKbApi()

const kbs = ref<KbSummary[]>([])
const loading = ref(false)
const showCreate = ref(false)

async function load() {
  if (!domainStore.currentDomain) return
  loading.value = true
  try {
    kbs.value = await kbApi.listKbs(domainStore.currentDomain)
  } catch (e) {
    kbs.value = []
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    loading.value = false
  }
}

function formatTime(t: string): string {
  if (!t) return '-'
  return new Date(t).toLocaleString('zh-CN')
}

onMounted(load)
watch(() => domainStore.currentDomain, load)
</script>

<style scoped>
.kb-list-view {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.kb-list-view__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.kb-list-view__header-left {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.kb-list-view__title {
  font-size: 16px;
  font-weight: 650;
  color: var(--kb-text-primary);
  margin: 0;
  letter-spacing: -0.2px;
}

.kb-list-view__count {
  font-size: 12px;
  color: var(--kb-text-tertiary);
}

.kb-list-view__domain {
  font-size: 12px;
  color: var(--kb-text-tertiary);
  font-family: 'SF Mono', 'Cascadia Code', monospace;
}

.kb-list-view__actions {
  display: flex;
  gap: 8px;
}

.kb-list-view__table-wrap {
  background: var(--kb-bg-card);
  border-radius: var(--kb-radius);
  box-shadow: var(--kb-shadow-card);
  border: 1px solid var(--kb-border-light);
  overflow: hidden;
}

.kb-name-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--kb-text-primary);
  text-decoration: none;
  font-weight: 600;
  font-size: 13.5px;
}
.kb-name-link:hover {
  color: var(--kb-accent);
}

.kb-name-icon {
  color: var(--kb-accent);
}

.kb-desc {
  margin-top: 3px;
  font-size: 12px;
  color: var(--kb-text-tertiary);
  line-height: 1.4;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-doc-count {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: var(--kb-text-secondary);
}
</style>
