<template>
  <div class="help-view">
    <aside class="help-view__nav">
      <el-input
        v-model="keyword"
        class="help-view__search"
        placeholder="搜索章节标题或正文…"
        clearable
        :prefix-icon="Search"
      />
      <nav class="help-view__tree">
        <section v-for="part in visibleParts" :key="part.key" class="help-view__part">
          <h3 class="help-view__part-label">{{ part.label }}</h3>
          <router-link
            v-for="ch in part.chapters"
            :key="ch.id"
            :to="{ name: 'help-chapter', params: { chapterId: ch.id } }"
            class="help-view__link"
            :class="{ 'help-view__link--active': ch.id === currentId }"
            :data-test="`help-nav-${ch.id}`"
          >
            <span class="help-view__link-id">{{ ch.id }}</span>
            <span class="help-view__link-title">{{ ch.title }}</span>
          </router-link>
        </section>
        <p v-if="visibleParts.length === 0" class="help-view__empty">没有匹配「{{ keyword }}」的章节</p>
      </nav>
    </aside>

    <main class="help-view__main">
      <div v-if="notFound" class="help-view__notfound">
        <p>没有编号为「{{ route.params.chapterId }}」的章节。</p>
        <el-button text type="primary" @click="go(fallbackId)">回到第 {{ fallbackId }} 章</el-button>
      </div>
      <template v-else>
        <div
          v-if="html"
          class="help-view__content markdown-body"
          v-html="html"
          @click="onContentClick"
        />
        <div v-else class="help-view__loading">加载中…</div>
        <nav class="help-view__pager">
          <el-button v-if="prevChapter" text data-test="help-prev" @click="go(prevChapter.id)">
            ← {{ prevChapter.id }} · {{ prevChapter.title }}
          </el-button>
          <span v-else />
          <el-button v-if="nextChapter" text type="primary" data-test="help-next" @click="go(nextChapter.id)">
            {{ nextChapter.id }} · {{ nextChapter.title }} →
          </el-button>
        </nav>
      </template>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search } from '@element-plus/icons-vue'
import { HELP_CHAPTER_COUNT, HELP_PARTS, type HelpChapter } from '@/help/helpManifest'
import { helpContentCache, helpContentVersion, loadHelpRaw } from '@/help/helpContent'
import { renderHelpMarkdown } from '@/help/renderMarkdown'

/** 全书平铺（按章号升序）——上一章/下一章与搜索都吃它。 */
const allChapters = computed<HelpChapter[]>(() =>
  HELP_PARTS.flatMap((p) => p.chapters),
)

const fallbackId = allChapters.value[0]?.id ?? '01'

const route = useRoute()
const router = useRouter()

const keyword = ref('')
const html = ref('')
const loading = ref(false)

const currentId = computed(() => {
  const id = route.params.chapterId
  if (typeof id !== 'string') return fallbackId
  return allChapters.value.some((c) => c.id === id) ? id : ''
})

const notFound = computed(() => currentId.value === '')

async function loadChapter(id: string): Promise<void> {
  if (!id) return
  loading.value = true
  html.value = '' // 先清空：侧栏高亮/pager 立即切到新章，旧章正文若残留会造成"标题B内容A"错位
  try {
    const chapter = allChapters.value.find((c) => c.id === id)
    if (!chapter) return
    const raw = await loadHelpRaw(chapter.file)
    if (currentId.value !== id) return // 快速切换时旧响应不得覆盖新章
    html.value = renderHelpMarkdown(raw)
  } finally {
    loading.value = false
  }
}

// 路由参数变化（含首次）→ 换章并回顶。AppLayout 的主区才是真正的滚动容器。
watch(
  () => route.params.chapterId,
  () => {
    void loadChapter(currentId.value)
    document.querySelector('.app-layout__main')?.scrollTo({ top: 0 })
  },
  { immediate: true },
)

onMounted(() => {
  // 预取其余章节，供正文搜索（失败静默——搜索退化为仅标题匹配，不影响阅读）
  for (const ch of allChapters.value) {
    void loadHelpRaw(ch.file).catch(() => undefined)
  }
})

/** 关键词过滤：标题必匹配源；正文对已预取的内容匹配（依赖版本号感知预取完成）。 */
const visibleParts = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return HELP_PARTS
  void helpContentVersion.value // 建立依赖：预取推进时过滤结果自动刷新
  const hit = (ch: HelpChapter) => {
    if (ch.title.toLowerCase().includes(kw)) return true
    const raw = helpContentCache.get(ch.file)
    return raw !== undefined && raw.toLowerCase().includes(kw)
  }
  return HELP_PARTS.map((p) => ({ ...p, chapters: p.chapters.filter(hit) })).filter(
    (p) => p.chapters.length > 0,
  )
})

const currentIndex = computed(() =>
  allChapters.value.findIndex((c) => c.id === currentId.value),
)
const prevChapter = computed(() =>
  currentIndex.value > 0 ? allChapters.value[currentIndex.value - 1] : undefined,
)
const nextChapter = computed(() => {
  const i = currentIndex.value
  return i >= 0 && i < allChapters.value.length - 1 ? allChapters.value[i + 1] : undefined
})

function go(id: string): void {
  void router.push({ name: 'help-chapter', params: { chapterId: id } })
}

/**
 * 内容区点击委托：
 *  - 站内章跳转（/help/NN，含「第 NN 章」自动链接）→ 交给路由；
 *  - 其余（书中素材来源若出现相对 .md 引用等平台不可达目标）→ 拦下，不触发错误导航。
 */
function onContentClick(event: MouseEvent): void {
  const anchor = (event.target as HTMLElement | null)?.closest('a')
  if (!anchor) return
  const href = anchor.getAttribute('href') ?? ''
  if (href.startsWith('/help/')) {
    event.preventDefault()
    void router.push(href)
  } else if (!/^https?:/i.test(href)) {
    event.preventDefault()
  }
}

// 暴露给测试：关键词（过滤断言）与全书章数
defineExpose({ keyword, chapterCount: HELP_CHAPTER_COUNT })
</script>

<style scoped>
.help-view {
  display: grid;
  grid-template-columns: 264px minmax(0, 1fr);
  gap: 28px;
  align-items: start;
}

/* ── 左：章树 ─────────────────────────────── */
.help-view__nav {
  position: sticky;
  top: 0;
  max-height: calc(100vh - 48px);
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-right: 4px;
}

.help-view__tree {
  overflow-y: auto;
  padding-right: 4px;
}

.help-view__part {
  margin-bottom: 18px;
}

.help-view__part-label {
  margin: 0 0 6px;
  padding: 0 10px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--kb-text-tertiary);
  text-transform: uppercase;
}

.help-view__link {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 8px;
  color: var(--kb-text-secondary, #475569);
  text-decoration: none;
  font-size: 13px;
  line-height: 1.45;
  transition: background var(--kb-duration, 0.2s) ease, color 0.2s ease;
}

.help-view__link:hover {
  background: var(--kb-bg-hover, rgba(0, 0, 0, 0.04));
  color: var(--kb-text-primary, #0f172a);
}

.help-view__link--active {
  background: var(--kb-bg-sidebar-active, rgba(59, 130, 246, 0.12));
  color: var(--kb-accent, #2563eb);
  font-weight: 600;
}

.help-view__link-id {
  flex-shrink: 0;
  font-size: 11px;
  font-weight: 700;
  color: var(--kb-text-tertiary, #94a3b8);
  font-variant-numeric: tabular-nums;
}

.help-view__link--active .help-view__link-id {
  color: inherit;
}

.help-view__empty {
  padding: 8px 10px;
  font-size: 13px;
  color: var(--kb-text-tertiary, #94a3b8);
}

/* ── 右：正文 ─────────────────────────────── */
.help-view__main {
  min-width: 0;
  background: var(--el-bg-color, #fff);
  border: 1px solid var(--el-border-color-lighter, #e2e8f0);
  border-radius: 12px;
  padding: 36px 44px 28px;
}

.help-view__loading,
.help-view__notfound {
  padding: 48px 0;
  text-align: center;
  color: var(--kb-text-tertiary, #94a3b8);
  font-size: 14px;
}

.help-view__pager {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 36px;
  padding-top: 16px;
  border-top: 1px dashed var(--el-border-color-lighter, #e2e8f0);
}

/* ── markdown 排版（v-html 内容需 :deep） ── */
.markdown-body :deep(h1) {
  margin: 0 0 18px;
  font-size: 24px;
  font-weight: 700;
  color: var(--kb-text-primary, #0f172a);
}

.markdown-body :deep(h2) {
  margin: 32px 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--el-border-color-lighter, #e2e8f0);
  font-size: 18px;
  font-weight: 700;
  color: var(--kb-text-primary, #0f172a);
}

.markdown-body :deep(h3) {
  margin: 24px 0 10px;
  font-size: 15.5px;
  font-weight: 700;
  color: var(--kb-text-primary, #0f172a);
}

.markdown-body :deep(p) {
  margin: 10px 0;
  font-size: 14px;
  line-height: 1.75;
  color: var(--kb-text-secondary, #334155);
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 10px 0;
  padding-left: 22px;
  font-size: 14px;
  line-height: 1.75;
  color: var(--kb-text-secondary, #334155);
}

.markdown-body :deep(li) {
  margin: 4px 0;
}

.markdown-body :deep(a) {
  color: var(--kb-accent, #2563eb);
  text-decoration: none;
  border-bottom: 1px dashed currentColor;
}

.markdown-body :deep(a:hover) {
  opacity: 0.8;
}

.markdown-body :deep(code) {
  padding: 2px 6px;
  border-radius: 5px;
  background: var(--el-fill-color-light, #f1f5f9);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  color: #db2777;
}

.markdown-body :deep(pre) {
  margin: 12px 0;
  padding: 14px 16px;
  border-radius: 10px;
  background: #0f172a;
  overflow-x: auto;
}

.markdown-body :deep(pre code) {
  padding: 0;
  background: transparent;
  color: #e2e8f0;
  font-size: 12.5px;
  line-height: 1.7;
}

.markdown-body :deep(table) {
  width: 100%;
  margin: 14px 0;
  border-collapse: collapse;
  font-size: 13px;
  display: block;
  overflow-x: auto;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  padding: 8px 12px;
  border: 1px solid var(--el-border-color-lighter, #e2e8f0);
  text-align: left;
  line-height: 1.6;
}

.markdown-body :deep(th) {
  background: var(--el-fill-color-light, #f8fafc);
  font-weight: 600;
  white-space: nowrap;
}

.markdown-body :deep(blockquote) {
  margin: 12px 0;
  padding: 10px 16px;
  border-left: 3px solid var(--kb-accent, #2563eb);
  border-radius: 0 8px 8px 0;
  background: var(--el-fill-color-light, #f8fafc);
  color: var(--kb-text-secondary, #475569);
}

.markdown-body :deep(blockquote p) {
  margin: 4px 0;
}

.markdown-body :deep(hr) {
  margin: 24px 0;
  border: none;
  border-top: 1px solid var(--el-border-color-lighter, #e2e8f0);
}

@media (max-width: 960px) {
  .help-view {
    grid-template-columns: 1fr;
  }

  .help-view__nav {
    position: static;
    max-height: none;
  }
}
</style>
