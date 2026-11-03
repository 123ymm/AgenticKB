<template>
  <div class="wiki-toc" data-test="wiki-toc-editor">
    <div class="wiki-toc__head">
      <span>目录确认（{{ pageCount }} 页）</span>
      <span v-if="overLimit" class="wiki-toc__over" data-test="wiki-toc-over">
        页数超过 {{ MAX_PAGES }} 上限——请取消勾选删减后再确认
      </span>
    </div>
    <p class="wiki-toc__hint">勾选=纳入写作；节标题与页标题可直接编辑。</p>

    <div v-for="(section, si) in sections" :key="si" class="wiki-toc__section" data-test="wiki-toc-section">
      <el-input
        v-model="section.section_title"
        size="small"
        data-test="wiki-toc-section-title"
      />
      <div
        v-for="(hint, hi) in section.page_hints"
        :key="hi"
        class="wiki-toc__hint-row"
        :class="{ 'is-excluded': !hint.included }"
      >
        <el-checkbox v-model="hint.included" size="small" data-test="wiki-toc-page-check" />
        <el-input v-model="hint.title" size="small" data-test="wiki-toc-page-title" />
      </div>
    </div>

    <div class="wiki-toc__foot">
      <el-button
        v-if="canWrite"
        type="primary"
        size="small"
        :loading="busy"
        :disabled="!canConfirm"
        data-test="wiki-toc-confirm"
        @click="confirm"
      >确认目录开始写作（{{ pageCount }} 页）</el-button>
      <el-button v-if="canWrite" size="small" data-test="wiki-toc-cancel" @click="emit('cancel')">
        放弃本次编译
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { WikiTocSection } from '@/types/artifacts'

/** 与后端 CompileService.MAX_PAGES 对齐（confirm 端点 >200 → 422）。 */
const MAX_PAGES = 200

interface EditableHint {
  title: string
  cluster_ids: string[]
  page_type: string
  /** 勾选位：false=从写作范围排除。 */
  included: boolean
}

interface EditableSection {
  section_title: string
  page_hints: EditableHint[]
}

const props = defineProps<{
  toc: WikiTocSection[]
  canWrite: boolean
  busy?: boolean
}>()

const emit = defineEmits<{
  /** 提交编辑后的目录（只含勾选页提示）。 */
  confirm: [toc: WikiTocSection[]]
  cancel: []
}>()

/** 可编辑本地副本（页提示附勾选位，默认全包含）。重置守卫=内容指纹：
 * 同内容新引用（父级重渲染/重复 status 响应）**不重置**——静默清空用户
 * 半程编辑是审查 HIGH；内容真变（新一轮 awaiting_toc）才以服务端为准。 */
const sections = ref<EditableSection[]>(props.toc.map(toEditable))
let tocFingerprint = JSON.stringify(props.toc)
watch(() => props.toc, (toc) => {
  const fingerprint = JSON.stringify(toc)
  if (fingerprint === tocFingerprint) return
  tocFingerprint = fingerprint
  sections.value = toc.map(toEditable)
})

function toEditable(section: WikiTocSection): EditableSection {
  return {
    section_title: section.section_title,
    page_hints: (section.page_hints ?? []).map(hint => ({
      title: hint.title, cluster_ids: [...hint.cluster_ids],
      page_type: hint.page_type, included: true,
    })),
  }
}

/** 展开页数=勾选页提示总数（与后端 toc_page_count 同口径）。 */
const pageCount = computed(() =>
  sections.value.reduce((sum, s) => sum + s.page_hints.filter(h => h.included).length, 0))

const overLimit = computed(() => pageCount.value > MAX_PAGES)
/** 零页（全取消）同后端 invalid_toc——一并禁提交。 */
const canConfirm = computed(() => pageCount.value > 0 && !overLimit.value)

/** 提交：剥离勾选位，仅保留纳入的页提示（空节保留节头，后端容许）。 */
function confirm(): void {
  if (!canConfirm.value) return
  emit('confirm', sections.value.map(section => ({
    section_title: section.section_title.trim(),
    page_hints: section.page_hints
      .filter(hint => hint.included)
      .map(({ title, cluster_ids, page_type }) => ({ title: title.trim(), cluster_ids, page_type })),
  })))
}
</script>

<style scoped>
.wiki-toc { display: flex; flex-direction: column; gap: 10px; }
.wiki-toc__head { display: flex; align-items: center; gap: 12px; font-weight: 600; }
.wiki-toc__over { color: var(--el-color-warning); font-weight: 400; font-size: 12.5px; }
.wiki-toc__hint { margin: 0; font-size: 12.5px; color: var(--kb-text-tertiary); }
.wiki-toc__section {
  display: flex; flex-direction: column; gap: 6px; padding: 8px;
  border: 1px solid var(--el-border-color-lighter, #ebeef5); border-radius: 6px;
}
.wiki-toc__section > :deep(.el-input) { max-width: 420px; font-weight: 600; }
.wiki-toc__hint-row { display: flex; align-items: center; gap: 8px; padding-left: 16px; }
.wiki-toc__hint-row.is-excluded { opacity: 0.5; }
.wiki-toc__hint-row :deep(.el-input) { max-width: 420px; }
.wiki-toc__foot { display: flex; gap: 8px; }
</style>
