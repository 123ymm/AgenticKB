<template>
  <div class="wiki-reader" data-test="wiki-reader">
    <!-- 加载中/清单失败/未发布（current_revision=0 → 清单为空即判）——单 EmptyState，不白屏 -->
    <EmptyState
      v-if="loading || pages.length === 0"
      :text="emptyText"
      data-test="wiki-reader-empty"
    />

    <!-- 三栏：左目录 / 中渲染页 / 右上下文（窄屏 media query 纵向堆叠，不引新依赖） -->
    <template v-else>
      <aside class="wiki-reader__toc">
        <div class="wiki-reader__toc-head">目录 · 共 {{ sortedPages.length }} 页</div>
        <div class="wiki-reader__toc-body">
          <button
            v-for="item in sortedPages"
            :key="item.page_key"
            type="button"
            class="wiki-reader__toc-item"
            :class="{ 'is-active': item.page_key === selectedKey }"
            data-test="wiki-reader-toc-item"
            @click="selectPage(item.page_key)"
          >
            <el-tag size="small" effect="plain" :type="pageTypeMeta(item.page_type)?.tag ?? 'info'">
              {{ pageTypeMeta(item.page_type)?.label ?? item.page_type }}
            </el-tag>
            <span class="wiki-reader__toc-title">{{ item.title }}</span>
          </button>
        </div>
      </aside>

      <section v-loading="pageLoading" class="wiki-reader__main">
        <template v-if="page">
          <header class="wiki-reader__page-head">
            <h3 class="wiki-reader__page-title">{{ page.title }}</h3>
            <el-tag size="small" effect="plain" :type="pageTypeMeta(page.page_type)?.tag ?? 'info'">
              {{ pageTypeMeta(page.page_type)?.label ?? page.page_type }}
            </el-tag>
            <el-tag size="small" effect="plain" :type="page.origin === 'manual' ? 'primary' : 'info'">
              {{ page.origin }}
            </el-tag>
            <span class="wiki-reader__page-time">更新于 {{ page.updated_at }}</span>
          </header>
          <!-- 点击委托：a[data-wiki-page] 切页 / sup.wiki-src 复制切片 ID（标记见 utils/markdown.ts） -->
          <div
            class="wiki-reader__body markdown-body"
            data-test="wiki-reader-body"
            v-html="pageHtml"
            @click="onContentClick"
          />
        </template>
        <EmptyState v-else text="在左侧目录选择页面" />
      </section>

      <aside v-if="page" class="wiki-reader__ctx">
        <section class="wiki-reader__ctx-block">
          <h4 class="wiki-reader__ctx-title">链接（{{ outLinks.length }}）</h4>
          <button
            v-for="(link, index) in outLinks"
            :key="`out-${index}`"
            type="button"
            class="wiki-reader__link"
            :class="{ 'is-broken': link.status === 'broken' }"
            data-test="wiki-reader-out-link"
            @click="goToKey(link.to_page)"
          >
            <span class="wiki-reader__link-text">{{ link.anchor_text || link.to_page }}</span>
            <span v-if="link.status === 'broken'" class="wiki-reader__link-flag">断链</span>
          </button>
          <p v-if="outLinks.length === 0" class="wiki-reader__none">无</p>
        </section>

        <section class="wiki-reader__ctx-block">
          <h4 class="wiki-reader__ctx-title">被链（{{ inLinks.length }}）</h4>
          <button
            v-for="(link, index) in inLinks"
            :key="`in-${index}`"
            type="button"
            class="wiki-reader__link"
            :class="{ 'is-broken': link.status === 'broken' }"
            data-test="wiki-reader-in-link"
            @click="goToKey(link.from_page)"
          >
            <!-- 被链主文案=来源页（谁链到我）；锚文本作次要提示 -->
            <span class="wiki-reader__link-text">
              {{ link.from_page }}<template v-if="link.anchor_text">（{{ link.anchor_text }}）</template>
            </span>
            <span v-if="link.status === 'broken'" class="wiki-reader__link-flag">断链</span>
          </button>
          <p v-if="inLinks.length === 0" class="wiki-reader__none">无</p>
        </section>

        <section class="wiki-reader__ctx-block">
          <h4 class="wiki-reader__ctx-title">溯源（{{ provenanceViews.length }}）</h4>
          <div
            v-for="(item, index) in provenanceViews"
            :key="`prov-${index}`"
            class="wiki-reader__prov"
            data-test="wiki-reader-provenance"
          >
            <p class="wiki-reader__prov-line">kb：{{ item.kbId }}</p>
            <p class="wiki-reader__prov-line">快照：{{ item.snapshotId }}</p>
            <p class="wiki-reader__prov-line">切片 {{ item.unitIds.length }} 条</p>
            <ul v-if="item.unitIds.length" class="wiki-reader__prov-units">
              <li v-for="unitId in item.unitIds" :key="unitId">{{ unitId }}</li>
            </ul>
            <!-- 结构未知字段：容错为 k=v 一行展示 -->
            <p v-for="extra in item.extra" :key="extra" class="wiki-reader__prov-line">{{ extra }}</p>
          </div>
          <p v-if="provenanceViews.length === 0" class="wiki-reader__none">无溯源记录</p>
        </section>
      </aside>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import EmptyState from '@/components/common/EmptyState.vue'
import { extractWikiNavTarget, extractWikiSrcUnit, renderWikiMarkdown } from '@/utils/markdown'
import type { WikiLink, WikiPage, WikiPageMeta, WikiPageType } from '@/types/artifacts'

/** 四页型徽标表（63号 §4.3）——与 WikiEditorPanel 同表（纯展示映射，两处独立维护）。 */
const PAGE_TYPES = [
  { value: 'topic', label: '主题', tag: 'primary' },
  { value: 'glossary', label: '术语', tag: 'success' },
  { value: 'contradiction', label: '矛盾', tag: 'danger' },
  { value: 'open_question', label: '开放问题', tag: 'warning' },
] as const

/** 溯源条目视图模型（provenance 结构宽松——字段缺失/未知结构一律容错降级显示）。 */
interface ProvenanceView {
  kbId: string
  snapshotId: string
  unitIds: string[]
  extra: string[]
}

const props = defineProps<{ artifactId: string }>()

const api = useArtifactsApi()

const loading = ref(false)
const pageLoading = ref(false)
const loadFailedText = ref('')
const pages = ref<WikiPageMeta[]>([])
const selectedKey = ref('')
const page = ref<WikiPage | null>(null)
const outLinks = ref<WikiLink[]>([])
const inLinks = ref<WikiLink[]>([])

const sortedPages = computed(() =>
  [...pages.value].sort((a, b) => a.ordinal - b.ordinal || a.page_key.localeCompare(b.page_key)))

/** 正文 HTML（renderWikiMarkdown：站内双链/溯源角标标记 → marked → DOMPurify）。 */
const pageHtml = computed(() => (page.value ? renderWikiMarkdown(page.value.body_md ?? '') : ''))

const provenanceViews = computed<ProvenanceView[]>(() => toProvenanceViews(page.value?.provenance))

const emptyText = computed(() => {
  if (loading.value) return 'Wiki 页面加载中…'
  if (loadFailedText.value) return `页面清单加载失败：${loadFailedText.value}`
  return '尚未发布版本——在设计态编辑并发布后，这里会显示 Wiki 页面'
})

onMounted(loadPages)

/** 页面清单（运行态：draft=false 读 current_revision 投影）+ 首选页/保持选中页。 */
async function loadPages(): Promise<void> {
  loading.value = true
  loadFailedText.value = ''
  try {
    pages.value = await api.listWikiPages(props.artifactId, { draft: false })
    const keepKey = selectedKey.value &&
      pages.value.some(p => p.page_key === selectedKey.value)
    if (keepKey) {
      await selectPage(selectedKey.value)
    } else if (sortedPages.value[0]) {
      await selectPage(sortedPages.value[0].page_key)
    } else {
      selectedKey.value = ''
      page.value = null
      outLinks.value = []
      inLinks.value = []
    }
  } catch (error) {
    pages.value = []
    page.value = null
    loadFailedText.value = await apiErrorDetail(error)
  } finally {
    loading.value = false
  }
}

/** 选中并加载一页（运行态）；过期响应（选中已被覆盖）丢弃。 */
async function selectPage(pageKey: string): Promise<void> {
  selectedKey.value = pageKey
  pageLoading.value = true
  outLinks.value = []
  inLinks.value = []
  try {
    const loaded = await api.getWikiPage(props.artifactId, pageKey, { draft: false })
    if (selectedKey.value !== pageKey) return
    page.value = loaded
    void loadLinks(pageKey)
  } catch (error) {
    if (selectedKey.value === pageKey) {
      ElMessage.error(`加载页面失败：${await apiErrorDetail(error)}`)
    }
  } finally {
    pageLoading.value = false
  }
}

/** 出入边并行拉取；仅当仍是当前页时落地（切页竞态防串台）。 */
async function loadLinks(pageKey: string): Promise<void> {
  try {
    const [out, inn] = await Promise.all([
      api.listWikiLinks(props.artifactId, { page_key: pageKey, direction: 'out' }),
      api.listWikiLinks(props.artifactId, { page_key: pageKey, direction: 'in' }),
    ])
    if (selectedKey.value !== pageKey) return
    outLinks.value = out
    inLinks.value = inn
  } catch (error) {
    if (selectedKey.value === pageKey) {
      ElMessage.error(`加载链接信息失败：${await apiErrorDetail(error)}`)
    }
  }
}

/** 正文点击委托：双链 → 切页；溯源角标 → 复制切片 ID；其余点击不拦截。 */
function onContentClick(event: MouseEvent): void {
  const navKey = extractWikiNavTarget(event.target)
  if (navKey !== null) {
    event.preventDefault()
    goToKey(navKey)
    return
  }
  const unitId = extractWikiSrcUnit(event.target)
  if (unitId !== null) {
    event.preventDefault()
    void copyUnitId(unitId)
  }
}

/**
 * 双链目标解析：page_key 直配 → aliases 命中。aliases 唯一可信口径是
 * listWikiPages 的 WikiPageMeta.aliases（getWikiPage 返回体类型声明偏宽，
 * 运行态不含顶层 aliases——不依赖它）。未命中 → 提示未发布（断链可见）。
 */
function goToKey(key: string): void {
  const target = pages.value.find(p => p.page_key === key)
    ?? pages.value.find(p => p.aliases.includes(key))
  if (!target) {
    ElMessage.warning('目标页面未发布')
    return
  }
  if (target.page_key !== selectedKey.value) void selectPage(target.page_key)
}

/** 溯源角标：复制切片 ID（clipboard 不可用/失败 → 降级 info 直接展示）。 */
async function copyUnitId(unitId: string): Promise<void> {
  const clipboard = (navigator as Navigator & {
    clipboard?: { writeText(text: string): Promise<void> }
  }).clipboard
  if (!clipboard?.writeText) {
    ElMessage.info(`切片 ID：${unitId}`)
    return
  }
  try {
    await clipboard.writeText(unitId)
    ElMessage.success('已复制切片 ID')
  } catch {
    ElMessage.info(`切片 ID：${unitId}`)
  }
}

/** provenance 宽松归一：对象条目取 kb_id/snapshot_id/unit_ids，其余原始值键值行。 */
function toProvenanceViews(provenance: unknown): ProvenanceView[] {
  if (!Array.isArray(provenance)) return []
  return provenance.map(item => {
    if (item !== null && typeof item === 'object' && !Array.isArray(item)) {
      const record = item as Record<string, unknown>
      const unitIds = Array.isArray(record.unit_ids) ? record.unit_ids.map(String) : []
      const known = new Set(['kb_id', 'snapshot_id', 'unit_ids'])
      const extra = Object.entries(record)
        .filter(([key, value]) => !known.has(key) && (value === null || typeof value !== 'object'))
        .map(([key, value]) => `${key}=${String(value)}`)
      return {
        kbId: record.kb_id === undefined || record.kb_id === null ? '—' : String(record.kb_id),
        snapshotId: record.snapshot_id === undefined || record.snapshot_id === null
          ? '—' : String(record.snapshot_id),
        unitIds,
        extra,
      }
    }
    // 非对象条目（结构漂移）：原样一行容错展示
    return { kbId: '—', snapshotId: '—', unitIds: [], extra: [String(item)] }
  })
}

function pageTypeMeta(pageType: WikiPageType | string) {
  return PAGE_TYPES.find(t => t.value === pageType)
}

/** 命令式刷新（发布后父级 reloadAll 可不接线——面板自管理；暴露给外层按需调用）。 */
defineExpose({ reload: loadPages })
</script>

<style scoped>
.wiki-reader { display: flex; gap: 14px; min-height: 420px; }
.wiki-reader__toc { width: 240px; flex-shrink: 0; }
.wiki-reader__toc-head {
  font-size: 12.5px; color: var(--kb-text-tertiary); margin-bottom: 8px;
}
.wiki-reader__toc-body { display: flex; flex-direction: column; gap: 4px; }
.wiki-reader__toc-item {
  display: flex; align-items: center; gap: 6px; width: 100%; padding: 6px 8px;
  border: 1px solid transparent; border-radius: 6px; background: none;
  cursor: pointer; text-align: left; font-size: 13px;
}
.wiki-reader__toc-item:hover { background: var(--el-fill-color-light, #f5f7fa); }
.wiki-reader__toc-item.is-active {
  background: var(--el-color-primary-light-9, #ecf5ff);
  border-color: var(--el-color-primary-light-7, #d9ecff);
}
.wiki-reader__toc-title {
  flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: var(--kb-text-primary, inherit);
}
.wiki-reader__main { flex: 1; min-width: 0; }
.wiki-reader__page-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.wiki-reader__page-title { margin: 0; font-size: 17px; }
.wiki-reader__page-time { font-size: 12px; color: var(--kb-text-tertiary); }
.wiki-reader__body {
  border: 1px solid var(--el-border-color-lighter, #ebeef5); border-radius: 6px;
  padding: 12px 16px; line-height: 1.7; overflow-wrap: anywhere; min-height: 200px;
}
/* 站内双链/溯源角标（渲染标记由 utils/markdown.ts 预处理注入） */
.wiki-reader__body :deep(a[data-wiki-page]) { color: var(--kb-accent, #2563eb); cursor: pointer; }
.wiki-reader__body :deep(sup.wiki-src) {
  color: var(--kb-text-tertiary); cursor: copy; font-size: 11px; margin: 0 1px;
}
.wiki-reader__ctx { width: 240px; flex-shrink: 0; display: flex; flex-direction: column; gap: 14px; }
.wiki-reader__ctx-title { margin: 0 0 6px; font-size: 13px; }
.wiki-reader__link {
  display: flex; align-items: center; gap: 6px; width: 100%; padding: 4px 6px;
  border: none; border-radius: 4px; background: none; cursor: pointer;
  text-align: left; font-size: 12.5px; color: var(--kb-accent, #2563eb);
}
.wiki-reader__link:hover { background: var(--el-fill-color-light, #f5f7fa); }
.wiki-reader__link.is-broken { color: var(--el-color-danger, #f56c6c); }
.wiki-reader__link-text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wiki-reader__link-flag { font-size: 11px; flex-shrink: 0; }
.wiki-reader__none { margin: 0; font-size: 12.5px; color: var(--kb-text-tertiary); }
.wiki-reader__prov {
  border: 1px solid var(--el-border-color-lighter, #ebeef5); border-radius: 6px;
  padding: 8px 10px; margin-bottom: 8px; font-size: 12.5px;
}
.wiki-reader__prov-line { margin: 0 0 2px; overflow-wrap: anywhere; }
.wiki-reader__prov-units { margin: 4px 0 0; padding-left: 18px; color: var(--kb-text-tertiary); }
/* 窄屏：三栏纵向堆叠（简单折叠，不引新依赖） */
@media (max-width: 1100px) {
  .wiki-reader { flex-direction: column; }
  .wiki-reader__toc, .wiki-reader__ctx { width: auto; }
}
</style>
