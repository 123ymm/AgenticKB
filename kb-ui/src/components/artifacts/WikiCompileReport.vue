<template>
  <div class="wiki-report" data-test="wiki-compile-report">
    <div v-if="errorText" class="wiki-report__error" data-test="wiki-compile-error">
      失败原因：{{ errorText }}
    </div>
    <dl class="wiki-report__stats">
      <div v-for="row in REPORT_ROWS" :key="row.key" class="wiki-report__stat">
        <template v-if="statValue(row.key) !== null">
          <dt>{{ row.label }}</dt>
          <dd>{{ statValue(row.key) }}</dd>
        </template>
      </div>
    </dl>
    <div v-if="conflicts.length > 0" class="wiki-report__conflicts" data-test="wiki-compile-conflicts">
      <div class="wiki-report__conflicts-title">
        人工页冲突（机器页被跳过——请人工裁决同键页面后重编译）：
      </div>
      <div v-for="(c, i) in conflicts" :key="i" class="wiki-report__conflict-row">
        {{ c.page_key }}：人工「{{ c.manual_title }}」 vs 机器「{{ c.machine_title }}」
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { WikiCompileConflict, WikiCompileStatus } from '@/types/artifacts'

/** 报告表格行（key=后端 stats 键；prepare 段与 writing 段合并口径，
 * 端点未带的键不渲染该行）。 */
const REPORT_ROWS = [
  { key: 'pages_written', label: '写入页数' },
  { key: 'unit_count', label: '取材单元' },
  { key: 'cluster_count', label: '聚类数' },
  { key: 'failed_pages', label: '失败页' },
  { key: 'dropped_pages', label: '超限截断页' },
  { key: 'broken_links', label: '断链' },
  { key: 'dangling_pages', label: '悬空引用页' },
  { key: 'contradiction_pages', label: '矛盾专页' },
  { key: 'open_question_pages', label: '开放问题专页' },
  { key: 'glossary_pages', label: '术语页' },
  { key: 'conflicts_n', label: '人工页冲突' },
] as const

const props = defineProps<{ status: WikiCompileStatus }>()

/** 失败原因只在 failed 态展示（completed/cancelled 无 error 语义）。 */
const errorText = computed(() =>
  String(props.status.state) === 'failed' ? (props.status.error ?? '') : '')

const conflicts = computed<WikiCompileConflict[]>(() => props.status.conflicts ?? [])

function statValue(key: string): string | number | null {
  const raw = props.status.stats?.[key]
  return raw === undefined || raw === null || raw === '' ? null : (raw as string | number)
}
</script>

<style scoped>
.wiki-report { display: flex; flex-direction: column; gap: 8px; }
.wiki-report__error {
  font-size: 12.5px; color: var(--el-color-danger, #f56c6c);
  background: var(--el-color-danger-light-9, #fef0f0); border-radius: 4px; padding: 6px 10px;
}
.wiki-report__stats {
  margin: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 4px 16px;
}
.wiki-report__stat { display: flex; justify-content: space-between; gap: 8px; font-size: 12.5px; }
.wiki-report__stat dt { color: var(--kb-text-tertiary); }
.wiki-report__stat dd { margin: 0; font-weight: 600; }
.wiki-report__conflicts { font-size: 12.5px; }
.wiki-report__conflicts-title { color: var(--el-color-warning); margin-bottom: 4px; }
.wiki-report__conflict-row { padding-left: 12px; color: var(--kb-text-secondary, inherit); }
</style>
