<template>
  <div class="ws-outtable">
    <!-- 空态：无 current 输出（stale/试跑态横幅由父级表达，不在此渲染） -->
    <EmptyState v-if="!output" text="该输出尚未执行" />
    <template v-else>
      <el-table :data="pagedRows" class="ws-outtable__table" size="small">
        <el-table-column
          v-for="col in dataCols"
          :key="col"
          :prop="col"
          :label="col"
          min-width="150"
          show-overflow-tooltip
        >
          <template #default="{ row }">
            <!-- 单元格三态：bool→是/否章；null→—；形似 { 开头的 JSON 串→查看弹窗 -->
            <span
              v-if="isBool(row[col])"
              class="ws-outtable__bool"
              :class="row[col] ? 'is-true' : 'is-false'"
            >{{ row[col] ? '是' : '否' }}</span>
            <span v-else-if="isNull(row[col])" class="ws-outtable__null">—</span>
            <button
              v-else-if="isJsonText(row[col])"
              type="button"
              class="ws-outtable__json-btn"
              :title="`查看 ${col} 的 JSON 内容`"
              @click="openJson(col, row[col] as string)"
            >{ } 查看</button>
            <span v-else class="ws-outtable__text">{{ formatScalar(row[col]) }}</span>
          </template>
        </el-table-column>

        <!-- 来源列：固定右侧，link 图标 + popover 全文（_source 串接符为全角分号） -->
        <el-table-column label="来源" fixed="right" width="72" align="center">
          <template #default="{ row }">
            <el-popover
              placement="left"
              trigger="click"
              :width="360"
              title="行来源"
              popper-class="ws-outtable__src-popper"
            >
              <template #reference>
                <button
                  type="button"
                  class="ws-outtable__src-btn"
                  :disabled="!sourceOf(row)"
                  title="查看行来源"
                >
                  <el-icon><Link /></el-icon>
                </button>
              </template>
              <p class="ws-outtable__src-text">{{ sourceOf(row) || '无来源记录' }}</p>
            </el-popover>
          </template>
        </el-table-column>
        <template #empty>该输出暂无数据行</template>
      </el-table>

      <el-pagination
        v-if="pageCount > 1"
        class="ws-outtable__pager"
        layout="total, prev, pager, next"
        :total="allRows.length"
        :page-size="PAGE_SIZE"
        :pager-count="7"
        :current-page="page"
        @current-change="onPageChange"
      />
      <p v-else class="ws-outtable__foot">
        {{ allRows.length }} 行 · {{ dataCols.length }} 列
      </p>
    </template>

    <!-- JSON 只读查看弹窗（pretty 渲染；解析失败回落原文） -->
    <el-dialog v-model="jsonVisible" :title="jsonTitle" width="640px">
      <pre class="ws-outtable__json">{{ jsonPretty }}</pre>
      <template #footer>
        <el-button @click="jsonVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Link } from '@element-plus/icons-vue'
import EmptyState from '@/components/common/EmptyState.vue'
import type { WorkspaceOutput } from '@/api/workspaces'

/**
 * 工作台输出表格：渲染 content_inline 数据集（cols/rows）。只读视图——
 * stale/试跑态提示由父级横幅表达。_id 不显示；_source 固定右侧走 popover。
 *
 * 分页为纯前端切片（数据已在手）：每页 100 行，超出一页即出分页器
 * （数百行的大输出同样适用，无服务端二次请求）。
 */
const props = defineProps<{
  output: WorkspaceOutput | null
}>()

const PAGE_SIZE = 100

// ── 数据集拆解（content_inline 形态 {cols, rows, grain}；防御性缺省） ──

const content = computed<Record<string, unknown>>(
  () => (props.output?.content_inline ?? {}) as Record<string, unknown>)

const allCols = computed<string[]>(() =>
  Array.isArray(content.value.cols) ? content.value.cols.map(String) : [])

const allRows = computed<Array<Record<string, unknown>>>(() =>
  Array.isArray(content.value.rows)
    ? (content.value.rows as Array<Record<string, unknown>>)
    : [])

/** 展示列：剔除系统列 _id/_source（_source 走固定右侧来源列）。 */
const dataCols = computed(() => allCols.value.filter(c => c !== '_id' && c !== '_source'))

// ── 分页（前端切片） ──

const page = ref(1)
const pageCount = computed(() => Math.ceil(allRows.value.length / PAGE_SIZE))
const pagedRows = computed(() =>
  allRows.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE))

function onPageChange(next: number): void {
  page.value = next
}

// 输出切换（选中别的端口/刷新后）回到第一页
watch(() => props.output, () => { page.value = 1 })

// ── 单元格形态判定 ──

function isBool(value: unknown): boolean {
  return typeof value === 'boolean'
}

function isNull(value: unknown): boolean {
  return value === null || value === undefined
}

/** 「长得像 JSON」：字符串以 { 开头（任务口径——不做完整预解析）。 */
function isJsonText(value: unknown): value is string {
  return typeof value === 'string' && value.trimStart().startsWith('{')
}

function formatScalar(value: unknown): string {
  if (typeof value === 'object' && value !== null) return JSON.stringify(value)
  return String(value)
}

function sourceOf(row: Record<string, unknown>): string {
  const value = row._source
  return typeof value === 'string' ? value : ''
}

// ── JSON 查看弹窗 ──

const jsonVisible = ref(false)
const jsonTitle = ref('')
const jsonPretty = ref('')

function openJson(col: string, raw: string): void {
  jsonTitle.value = `${col} · JSON 内容（只读）`
  try {
    jsonPretty.value = JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    jsonPretty.value = raw
  }
  jsonVisible.value = true
}
</script>

<style scoped>
.ws-outtable { display: flex; flex-direction: column; min-width: 0; }

/* 软表头 + 发丝线（1.1.36/37 表格语言） */
.ws-outtable__table :deep(th.el-table__cell) {
  background: var(--kb-bg-card-hover, #f8fafc);
  color: var(--kb-text-secondary, #64748b);
  font-weight: 500; font-size: 11.5px;
}
.ws-outtable__table :deep(td.el-table__cell) { font-size: 12.5px; }

.ws-outtable__bool {
  display: inline-block;
  border-radius: 4px;
  padding: 1px 7px;
  font-size: 11px;
}
.ws-outtable__bool.is-true {
  background: var(--kb-success-soft, rgba(16, 185, 129, 0.1));
  color: var(--kb-success, #10b981);
}
.ws-outtable__bool.is-false {
  background: var(--kb-bg-card-hover, #f1f5f9);
  color: var(--kb-text-tertiary, #94a3b8);
}

.ws-outtable__null { color: var(--kb-text-tertiary, #94a3b8); }

.ws-outtable__json-btn {
  border: 1px solid var(--kb-border, #e2e8f0);
  border-radius: 5px;
  padding: 1px 8px;
  background: var(--kb-bg-card, #fff);
  color: var(--kb-text-secondary, #64748b);
  font-size: 11.5px;
  font-family: inherit;
  cursor: pointer;
}
.ws-outtable__json-btn:hover {
  color: var(--kb-accent, #0891b2);
  border-color: var(--kb-accent, #0891b2);
}

.ws-outtable__src-btn {
  display: inline-grid; place-items: center;
  border: 0; background: none; padding: 3px;
  color: var(--kb-text-tertiary, #94a3b8);
  cursor: pointer;
}
.ws-outtable__src-btn:not(:disabled):hover { color: var(--kb-accent, #0891b2); }
.ws-outtable__src-btn:disabled { cursor: default; opacity: .35; }

.ws-outtable__src-text {
  margin: 0;
  font-size: 12px; line-height: 1.6;
  word-break: break-all;
  color: var(--kb-text-secondary, #64748b);
  max-height: 260px; overflow: auto;
}

.ws-outtable__pager { margin-top: 10px; justify-content: flex-end; }
.ws-outtable__foot {
  margin: 8px 2px 0;
  color: var(--kb-text-tertiary, #94a3b8);
  font-size: 11.5px;
}

.ws-outtable__json {
  margin: 0;
  max-height: 420px; overflow: auto;
  padding: 12px 14px;
  border-radius: 8px;
  border: 1px solid var(--kb-border-light, #f1f5f9);
  background: var(--kb-bg-card-hover, #f8fafc);
  font-size: 12px; line-height: 1.6;
  font-family: var(--kb-font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
  white-space: pre;
}
</style>
