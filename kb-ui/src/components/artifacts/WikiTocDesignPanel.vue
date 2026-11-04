<template>
  <div class="wiki-toc-design" data-test="wiki-toc-design">
    <div class="wiki-toc-design__bar">
      <span class="wiki-toc-design__status">
        {{ loading ? '目录加载中…' : sections.length === 0 ? '目录未分节' : `共 ${sections.length} 节` }}
      </span>
      <el-button
        type="primary"
        size="small"
        :loading="saving"
        :disabled="!dirty"
        data-test="toc-save"
        @click="save"
      >保存目录{{ dirty ? '' : '（无变更）' }}</el-button>
    </div>
    <!-- 无 toc 行（seed 失败/未编译）→ 平铺回落可见（Task 1.3 审查 M-4） -->
    <div v-if="flat" class="wiki-toc-design__flat" data-test="toc-flat-hint" role="note">
      目录未生成——当前按平铺展示；可新增节并把页面归入，保存后生效
    </div>

    <div v-loading="loading" class="wiki-toc-design__body">
      <section v-for="(sec, si) in sections" :key="si" class="wiki-toc-design__section" data-test="toc-section">
        <header class="wiki-toc-design__section-head">
          <el-input v-model="sec.sectionTitle" size="small" data-test="toc-section-title" />
          <span class="wiki-toc-design__section-ops">
            <el-button size="small" text :disabled="si === 0" data-test="toc-section-up" @click="moveSection(si, -1)">上移</el-button>
            <el-button size="small" text :disabled="si === sections.length - 1" data-test="toc-section-down" @click="moveSection(si, 1)">下移</el-button>
            <el-button size="small" text type="danger" data-test="toc-section-remove" @click="removeSection(si)">删节</el-button>
          </span>
        </header>
        <div v-for="key in sec.pageKeys" :key="key" class="wiki-toc-design__page" data-test="toc-section-page">
          <span class="wiki-toc-design__page-title">{{ titleOf(key) }}</span>
          <el-select
            :model-value="si"
            size="small"
            class="wiki-toc-design__page-select"
            data-test="toc-page-select"
            @change="onSectionSelect(key, $event)"
          >
            <el-option v-for="(s, ti) in sections" :key="ti" :value="ti" :label="s.sectionTitle || `节 ${ti + 1}`" />
            <el-option :value="-1" label="未分节" />
          </el-select>
        </div>
        <p v-if="sec.pageKeys.length === 0" class="wiki-toc-design__empty-sec">（空节——从下方未分区把页归入，或直接删除本节）</p>
      </section>

      <el-button size="small" data-test="toc-add-section" @click="addSection">＋ 新增节</el-button>

      <section class="wiki-toc-design__unassigned" data-test="toc-unassigned">
        <h4 class="wiki-toc-design__unassigned-title">未分节（{{ unassignedPages.length }}）</h4>
        <div v-for="meta in unassignedPages" :key="meta.page_key" class="wiki-toc-design__page" data-test="toc-unassigned-page">
          <span class="wiki-toc-design__page-title">{{ meta.title }}</span>
          <el-select
            :model-value="-1"
            size="small"
            class="wiki-toc-design__page-select"
            data-test="toc-page-select"
            @change="onSectionSelect(meta.page_key, $event)"
          >
            <el-option v-for="(s, ti) in sections" :key="ti" :value="ti" :label="s.sectionTitle || `节 ${ti + 1}`" />
            <el-option :value="-1" label="未分节" />
          </el-select>
        </div>
        <p v-if="unassignedPages.length === 0" class="wiki-toc-design__empty-sec">所有页面均已归节</p>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import type { WikiPageMeta, WikiTocSnapshotResolved } from '@/types/artifacts'

interface EditableSection {
  sectionTitle: string
  pageKeys: string[]
}

const props = defineProps<{
  artifactId: string
  /** 设计态页清单（EditorPanel 的 sortedPages）——移动页与未分区列表的素材。 */
  pages: WikiPageMeta[]
}>()

/** dirty=[当前是否有未保存修改]（父级切换守卫依据）；saved=[保存成功]。 */
const emit = defineEmits<{ dirty: [isDirty: boolean]; saved: [] }>()

const api = useArtifactsApi()

const loading = ref(false)
const saving = ref(false)
/** 无 toc 行/空目录（GET flat=true）→ 平铺回落提示。 */
const flat = ref(false)
const sections = ref<EditableSection[]>([])
/** 目录编辑乐观锁令牌（GET draft 的 updated_at；行不存在=undefined=首次创建可省）。 */
const updatedAt = ref<string | undefined>(undefined)
/** dirty 基线（加载/保存成功时的归一形态序列——对比判定未保存修改；
 * ref 化让保存后基线重置能触发 dirty computed 重算）。 */
const baseline = ref('')

/** 归一序列化：与 PUT 载荷同形态（节名 trim + 键快照）。 */
function serialize(): WikiTocSnapshotResolved[] {
  return sections.value.map(s => ({ section_title: s.sectionTitle.trim(), page_keys: [...s.pageKeys] }))
}

const dirty = computed(() => JSON.stringify(serialize()) !== baseline.value)

/** 未入任何节的页（按传入清单序）——「其他」口径的设计态素材。 */
const unassignedPages = computed(() => {
  const assigned = new Set(sections.value.flatMap(s => s.pageKeys))
  return props.pages.filter(p => !assigned.has(p.page_key))
})

// dirty 变化即时上抛（父级切换守卫用）；序列化 source 兼做深侦听。
watch(() => JSON.stringify(serialize()), () => emit('dirty', dirty.value))

onMounted(load)

/** 加载 draft 目录：节/页落地 + 令牌 + 基线。失败 → 平铺回落编辑（不白屏）。 */
async function load(): Promise<void> {
  loading.value = true
  try {
    const snap = await api.getWikiToc(props.artifactId, { draft: true })
    sections.value = (snap.sections ?? []).map(s => ({ sectionTitle: s.section_title, pageKeys: [...s.page_keys] }))
    flat.value = snap.flat === true
    updatedAt.value = snap.updated_at
    baseline.value = JSON.stringify(serialize())
  } catch (error) {
    sections.value = []
    flat.value = true
    ElMessage.error(`目录加载失败：${await apiErrorDetail(error)}——可从平铺开始建立目录`)
  } finally {
    loading.value = false
  }
}

/** 全量 PUT；成功后新令牌回存（下次编辑的乐观锁依据）+ 基线复位。 */
async function save(): Promise<void> {
  const payloadSections = serialize()
  if (payloadSections.some(s => !s.section_title)) {
    ElMessage.warning('节名不能为空——请补全或删除空名节')
    return
  }
  saving.value = true
  try {
    const resp = await api.saveWikiToc(props.artifactId, {
      sections: payloadSections,
      ...(updatedAt.value ? { expected_updated_at: updatedAt.value } : {}),
    })
    updatedAt.value = resp.updated_at
    baseline.value = JSON.stringify(serialize())
    emit('dirty', false)
    emit('saved')
    ElMessage.success('目录已保存（发布后生效于运行态）')
  } catch (error) {
    if (isStale(error)) {
      ElMessage.error('目录已被他人修改，已重新加载最新目录——请在此基础上重做修改')
      await load()
    } else {
      ElMessage.error(`保存目录失败：${await apiErrorDetail(error)}`)
    }
  } finally {
    saving.value = false
  }
}

function addSection(): void {
  sections.value = [...sections.value, { sectionTitle: `第 ${sections.value.length + 1} 节`, pageKeys: [] }]
}

/** 删节：节内页自动回落未分区（不在任何节=运行态归「其他」）。 */
function removeSection(index: number): void {
  sections.value = sections.value.filter((_, i) => i !== index)
}

function moveSection(index: number, delta: -1 | 1): void {
  const target = index + delta
  if (target < 0 || target >= sections.value.length) return
  const next = [...sections.value]
  const [moved] = next.splice(index, 1)
  next.splice(target, 0, moved)
  sections.value = next
}

/** 移动页到节：先从所有节移除（跨节互斥），-1=移回未分区；入节追加到尾部。 */
function movePage(pageKey: string, targetIndex: number): void {
  sections.value = sections.value.map(s => ({ ...s, pageKeys: s.pageKeys.filter(k => k !== pageKey) }))
  if (targetIndex >= 0 && targetIndex < sections.value.length) {
    sections.value = sections.value.map((s, i) =>
      i === targetIndex ? { ...s, pageKeys: [...s.pageKeys, pageKey] } : s)
  }
}

/** el-select change（联合类型值）→ 节索引归一。 */
function onSectionSelect(pageKey: string, value: unknown): void {
  movePage(pageKey, Number(value))
}

/** 409 stale_artifact → 他人已改（复用两面板既有口径）。 */
function isStale(error: unknown): boolean {
  const resp = (error as { response?: { status?: number; data?: { detail?: { code?: string } } } })
    ?.response
  return resp?.status === 409 && resp.data?.detail?.code === 'stale_artifact'
}

function titleOf(pageKey: string): string {
  return props.pages.find(p => p.page_key === pageKey)?.title ?? pageKey
}

defineExpose({ reload: load })
</script>

<style scoped>
.wiki-toc-design { display: flex; flex-direction: column; gap: 10px; }
.wiki-toc-design__bar { display: flex; align-items: center; justify-content: space-between; }
.wiki-toc-design__status { font-size: 12.5px; color: var(--kb-text-tertiary); }
.wiki-toc-design__flat {
  padding: 7px 10px; border-radius: 6px; font-size: 12.5px;
  background: var(--el-color-primary-light-9, #ecf5ff);
  border: 1px solid var(--el-color-primary-light-7, #d9ecff);
  color: var(--kb-text-primary, inherit);
}
.wiki-toc-design__body { display: flex; flex-direction: column; gap: 10px; }
.wiki-toc-design__section, .wiki-toc-design__unassigned {
  border: 1px solid var(--el-border-color-lighter, #ebeef5); border-radius: 6px; padding: 8px 10px;
}
.wiki-toc-design__section-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.wiki-toc-design__section-ops { display: flex; flex-shrink: 0; }
.wiki-toc-design__page {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  padding: 3px 0; font-size: 13px;
}
.wiki-toc-design__page-title {
  flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--kb-text-primary, inherit);
}
.wiki-toc-design__page-select { width: 150px; flex-shrink: 0; }
.wiki-toc-design__empty-sec { margin: 4px 0 0; font-size: 12px; color: var(--kb-text-tertiary); }
.wiki-toc-design__unassigned-title { margin: 0 0 6px; font-size: 13px; }
</style>
