<template>
  <div class="kb-source-picker">
    <div class="kb-source-picker__controls">
      <span>知识库</span>
      <el-select v-model="kbId" placeholder="选择知识库" filterable :loading="loading" :disabled="loading" data-test="source-kb">
        <el-option v-for="kb in kbs" :key="kb.id" :value="kb.id" :label="kb.name" />
      </el-select>
      <el-checkbox v-model="includeSubdirectories">选择目录时包含子目录</el-checkbox>
      <el-button v-if="loadError" @click="loadKbs">重试</el-button>
    </div>
    <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" />
    <el-empty v-else-if="!loading && !kbs.length" description="当前领域没有可访问的知识库" />
    <p class="kb-source-picker__hint">切换知识库会清空当前选择；修改子目录选项后，请重新勾选来源。</p>
    <KbFileManager
      v-if="kbId" :key="kbId" ref="fileManager"
      :kb-id="kbId" :can-write="false" :workflow-id="null" :active="active"
      selection-only :include-subdirectories="includeSubdirectories"
      @selection-change="onSelectionChange" @selection-loading="selecting = $event"
    />
    <template v-if="kbId">
      <div class="kb-source-picker__controls">
        <b data-test="source-count">已选 {{ documents.length }} 个文件（已去重）</b>
        <el-button :disabled="!documents.length && !selecting" @click="fileManager?.clearSelection()">清空选择</el-button>
      </div>
      <el-table :data="visibleDocuments" size="small" max-height="360" row-key="id">
        <el-table-column prop="document_name" label="文件名" min-width="180" show-overflow-tooltip />
        <el-table-column label="所在目录" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.directory_path || '根目录' }}</template>
        </el-table-column>
        <el-table-column label="挖掘状态" min-width="180">
          <template #default="{ row }">
            <el-tag :type="docStatusTagType(row.status || 'unknown')" size="small">
              {{ docStatusLabel(row.status || 'unknown', row.rd_action === 'SKIP') }}
            </el-tag>
            <span v-if="row.knowledge_outdated"> 待更新（旧知识可用）</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button link @click="fileManager?.removeSelected(row.id)">移除</el-button>
          </template>
        </el-table-column>
        <template #empty>请勾选目录或文件</template>
      </el-table>
      <el-pagination v-if="documents.length > 50" v-model:current-page="page" :total="documents.length" :page-size="50" layout="total, prev, pager, next" />
      <el-button type="primary" :disabled="!documents.length || selecting" data-test="confirm-kb-source" @click="confirmSelection">
        确认来源选择
      </el-button>
      <el-alert v-if="confirmed" type="success" :closable="false" :title="`已确认 ${documents.length} 个文件，可在工作台执行表格抽取。`" />
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useKbApi } from '@/api/kb'
import { apiErrorDetail } from '@/api/proxyClient'
import KbFileManager from '@/components/kb/KbFileManager.vue'
import { docStatusLabel, docStatusTagType } from '@/views/kb/kbMeta'
import type { KbDocument, KbSummary } from '@/types/kb'

const props = defineProps<{ domain: string; active: boolean }>()
const emit = defineEmits<{ confirmed: [value: { kbId: string; documents: KbDocument[] }] }>()
const api = useKbApi()
const kbs = ref<KbSummary[]>([])
const kbId = ref('')
const loading = ref(false)
const loadError = ref('')
const includeSubdirectories = ref(true)
const documents = ref<KbDocument[]>([])
const selecting = ref(false)
const confirmed = ref(false)
const page = ref(1)
const fileManager = ref<InstanceType<typeof KbFileManager>>()
const visibleDocuments = computed(() => documents.value.slice((page.value - 1) * 50, page.value * 50))
let generation = 0

function confirmSelection() {
  if (!documents.value.length || selecting.value) return
  confirmed.value = true
  emit('confirmed', { kbId: kbId.value, documents: [...documents.value] })
}

async function loadKbs() {
  const request = ++generation
  loading.value = true
  loadError.value = ''
  kbs.value = []
  kbId.value = ''
  try {
    if (!props.domain) throw new Error('缺少制品所属领域')
    const result = await api.listKbs(props.domain)
    if (request !== generation) return
    kbs.value = result.filter(kb => !kb.deleted_at)
  } catch (error) {
    if (request === generation) loadError.value = `加载知识库失败：${await apiErrorDetail(error)}`
  } finally {
    if (request === generation) loading.value = false
  }
}
function onSelectionChange(value: KbDocument[]) {
  documents.value = value
  confirmed.value = false
  page.value = Math.min(page.value, Math.max(1, Math.ceil(value.length / 50)))
}
watch(kbId, () => {
  onSelectionChange([])
  selecting.value = false
})
watch(() => props.domain, loadKbs, { immediate: true })
</script>

<style scoped>
.kb-source-picker { display: flex; flex-direction: column; gap: 16px; }
.kb-source-picker__controls { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.kb-source-picker__controls .el-select { width: 280px; }
.kb-source-picker__hint { margin: 0; color: var(--kb-text-secondary); font-size: 13px; }
</style>
