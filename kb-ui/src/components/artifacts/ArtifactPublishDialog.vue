<template>
  <el-dialog
    :model-value="modelValue"
    title="发布制品"
    width="680px"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <div v-loading="diffLoading" class="publish-diff">
      <p class="publish-diff__title">本次发布将产生的变化（draft vs 当前已发布版）：</p>
      <el-alert
        v-if="noPendingChanges"
        type="info"
        :closable="false"
        data-test="publish-no-pending"
        title="无待发布变更"
        description="当前已是发布态（草稿未开放）——编辑数据或改列后自动检出草稿，再来发布。"
      />
      <el-empty
        v-else-if="!diffLoading && !diffResult"
        description="差异加载失败或无差异数据"
        :image-size="60"
      />
      <el-collapse v-else-if="diffResult" v-model="activeSections">
        <el-collapse-item name="added">
          <template #title>
            新增 <b class="publish-diff__count">{{ diffResult.added_count }}</b> {{ unitLabel }}
          </template>
          <div class="publish-diff__keys">
            <el-tag v-for="key in diffResult.added" :key="key" size="small" type="success" effect="plain">
              {{ key }}
            </el-tag>
            <span v-if="!diffResult.added.length" class="publish-diff__none">无</span>
          </div>
        </el-collapse-item>
        <el-collapse-item name="changed">
          <template #title>
            修改
            <b class="publish-diff__count">{{ diffResult.changed_count }}</b>
            {{ wikiDiff ? '页（页级明细）' : '处（列级明细）' }}
          </template>
          <!-- wiki 形态（66号 §10：不按表格行列格式硬套）——每页折叠卡：
               title/frontmatter/page_type 直显 old→new；body_md 渲染后端
               hunks 行级 +/- 着色。diff 数据只有 page_key（弹窗无页清单
               映射），页标题即显示 page_key，页型无从映射则不显示。 -->
          <template v-if="wikiDiff">
            <el-collapse v-if="wikiDiff.changed.length" v-model="openPages" class="publish-diff__pages">
              <el-collapse-item v-for="page in wikiDiff.changed" :key="page.page_key" :name="page.page_key">
                <template #title>
                  <span class="publish-diff__page-key">{{ page.page_key }}</span>
                  <el-tag
                    v-for="field in page.fields"
                    :key="field"
                    size="small"
                    effect="plain"
                    class="publish-diff__field-tag"
                  >{{ field }}</el-tag>
                </template>
                <div v-for="field in page.fields" :key="field" class="publish-diff__field">
                  <span class="publish-diff__field-name">{{ field }}</span>
                  <template v-if="fieldHunks(page.changes[field]).length">
                    <div class="publish-diff__hunks">
                      <div
                        v-for="(line, idx) in fieldHunks(page.changes[field])"
                        :key="idx"
                        class="publish-diff__hunk-line"
                        :class="hunkLineClass(line)"
                      >{{ line }}</div>
                    </div>
                    <div v-if="fieldTruncated(page.changes[field])" class="publish-diff__truncated">
                      差异过长已截断
                    </div>
                  </template>
                  <template v-else>
                    <span class="publish-diff__field-old">{{ fieldOld(page.changes[field]) }}</span>
                    <span class="publish-diff__arrow">→</span>
                    <span class="publish-diff__field-new">{{ fieldNew(page.changes[field]) }}</span>
                  </template>
                </div>
              </el-collapse-item>
            </el-collapse>
            <span v-else class="publish-diff__none">无</span>
          </template>
          <!-- 表格形态（零改动） -->
          <template v-else>
            <el-table v-if="diffResult.changed.length" :data="diffResult.changed" size="small" max-height="260">
              <el-table-column prop="row_key" label="行键" min-width="150" show-overflow-tooltip />
              <el-table-column prop="column" label="列" width="130" />
              <el-table-column label="旧值" min-width="120" show-overflow-tooltip>
                <template #default="{ row }">{{ cellText(row.old) }}</template>
              </el-table-column>
              <el-table-column label="新值" min-width="120" show-overflow-tooltip>
                <template #default="{ row }">{{ cellText(row.new) }}</template>
              </el-table-column>
            </el-table>
            <span v-else class="publish-diff__none">无</span>
          </template>
        </el-collapse-item>
        <el-collapse-item name="removed">
          <template #title>
            删除 <b class="publish-diff__count">{{ diffResult.removed_count }}</b> {{ unitLabel }}
          </template>
          <div class="publish-diff__keys">
            <el-tag v-for="key in diffResult.removed" :key="key" size="small" type="danger" effect="plain">
              {{ key }}
            </el-tag>
            <span v-if="!diffResult.removed.length" class="publish-diff__none">无</span>
          </div>
        </el-collapse-item>
      </el-collapse>

      <el-form label-width="90px" class="publish-diff__form">
        <el-form-item label="使用说明">
          <el-input
            v-model="usageNote"
            type="textarea"
            :rows="3"
            data-test="publish-usage-note"
            placeholder="给 Agent 使用者的说明——发布必填：何时用 / 何时不用"
          />
          <span v-if="!usageNote.trim()" class="publish-diff__required">
            发布必填：何时用 / 何时不用（SRA 实证：说明卡缺失等于没发布）
          </span>
        </el-form-item>
        <el-form-item label="变更摘要">
          <el-input
            v-model="changeSummary"
            type="textarea"
            :rows="2"
            placeholder="本次发布改了什么（写入版本历史）"
          />
        </el-form-item>
      </el-form>
    </div>
    <template #footer>
      <el-button @click="$emit('update:modelValue', false)">取消</el-button>
      <el-button
        type="primary"
        :loading="publishing"
        :disabled="!canPublish"
        data-test="publish-submit"
        @click="confirmPublish"
      >确认发布</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useArtifactsApi } from '@/api/artifacts'
import { apiErrorDetail } from '@/api/proxyClient'
import type {
  ArtifactKind,
  DiffResult,
  PublishResult,
  WikiDiffResult,
  WikiPageDiffFieldChange,
} from '@/types/artifacts'

const props = withDefaults(defineProps<{
  artifactId: string
  modelValue: boolean
  /** 乐观锁令牌（artifact.updated_at）。 */
  expectedUpdatedAt?: string
  /** 当前制品使用说明（usage_note 门禁——发布前必填）。 */
  usageNote: string
  /** 干净态判定底料：draft_open=false 且 current_revision>0 → 无待发布变更（409 门禁前端镜像）。 */
  draftOpen?: boolean
  currentRevision?: number
  /** 差异/结果计数单位（表格=行；wiki=页——DetailView 按 kind 传入；缺省行=表格旧文案不变）。 */
  unitLabel?: string
  /** 制品形态（DetailView 按 artifact.kind 传入）：wiki → 页级差异渲染
   * （diff<WikiDiffResult>）；缺省 = 表格行/列形态（行为零改动）。 */
  kind?: ArtifactKind
}>(), { unitLabel: '行' })

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  published: [result: PublishResult]
  /** 元信息已被本对话框推进（乐观锁令牌变化，父级应 reload）。 */
  changed: []
}>()

const api = useArtifactsApi()

const diffResult = ref<DiffResult | WikiDiffResult | null>(null)
const diffLoading = ref(false)
const publishing = ref(false)
const activeSections = ref(['added', 'changed', 'removed'])
const usageNote = ref('')
const changeSummary = ref('')
/** wiki 修改页折叠卡展开清单（默认全收起——body hunks 可能很长）。 */
const openPages = ref<string[]>([])

/** wiki 形态差异（kind=wiki 时非空）——模板按此分支，表格形态零改动。 */
const wikiDiff = computed<WikiDiffResult | null>(() =>
  props.kind === 'wiki' ? (diffResult.value as WikiDiffResult | null) : null)

/** 干净态（draft 关闭且已有发布版）= 无待发布变更——后端 409 门禁的前端镜像。 */
const noPendingChanges = computed(() =>
  props.draftOpen === false && (props.currentRevision ?? 0) > 0)

/** usage_note 非空才允许发布（后端同门禁 422 兜底）；干净态禁发（409 兜底）。 */
const canPublish = computed(() =>
  usageNote.value.trim().length > 0 && !noPendingChanges.value)

watch(() => props.modelValue, (open) => {
  if (!open) return
  usageNote.value = props.usageNote
  changeSummary.value = ''
  activeSections.value = ['added', 'changed', 'removed']
  openPages.value = []
  void loadDiff()
}, { immediate: true })

async function loadDiff(): Promise<void> {
  diffLoading.value = true
  diffResult.value = null
  try {
    diffResult.value = props.kind === 'wiki'
      ? await api.diff<WikiDiffResult>(props.artifactId)
      : await api.diff(props.artifactId)
  } catch (error) {
    // viewer / 无 draft 权限等场景：仍开放表单（后端发布时再拦），差异区留空提示
    ElMessage.error(`差异加载失败：${await apiErrorDetail(error)}`)
  } finally {
    diffLoading.value = false
  }
}

// ── wiki 页级差异取值 helpers（联合两形态的安全访问；纯文本渲染，无 HTML 注入）──

type FieldChange = WikiPageDiffFieldChange | undefined

/** body_md 形态判别（含 diff_hunks 即长正文行级差异）。 */
function fieldHunks(change: FieldChange): string[] {
  return change && 'diff_hunks' in change ? change.diff_hunks : []
}

/** 整体截断标记（hunks 超 100 行被截）——行级超长以行尾后缀文本呈现，不走此分支。 */
function fieldTruncated(change: FieldChange): boolean {
  return !!change && 'truncated' in change && change.truncated === true
}

/** 非正文字段（title/frontmatter/page_type/origin）旧新直给。 */
function fieldOld(change: FieldChange): string {
  return change && 'old' in change ? cellText(change.old) : '—'
}

function fieldNew(change: FieldChange): string {
  return change && 'new' in change ? cellText(change.new) : '—'
}

/** 行前缀按原始行文本判断（---/+++ 头两行带尾随空格，不先 trim）：
 * +绿 -红 @@灰，其余（上下文行）默认。 */
function hunkLineClass(line: string): string {
  if (line.startsWith('+')) return 'is-add'
  if (line.startsWith('-')) return 'is-del'
  if (line.startsWith('@@')) return 'is-meta'
  return ''
}

function cellText(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function isStale(error: unknown): boolean {
  const resp = (error as { response?: { status?: number; data?: { detail?: { code?: string } } } })?.response
  return resp?.status === 409 && resp.data?.detail?.code === 'stale_artifact'
}

async function confirmPublish(): Promise<void> {
  if (!canPublish.value) return
  publishing.value = true
  try {
    // usage_note 与制品不一致（含原为空）→ 先 PATCH 元信息，拿新乐观锁令牌再发布
    let token = props.expectedUpdatedAt
    const next = usageNote.value.trim()
    if (next !== props.usageNote) {
      const updated = await api.updateMeta(props.artifactId, {
        usage_note: next, expected_updated_at: props.expectedUpdatedAt,
      })
      token = updated.updated_at
      emit('changed')
    }
    const result = await api.publish(props.artifactId, {
      change_summary: changeSummary.value.trim(),
      expected_updated_at: token,
    })
    ElMessage.success(`已发布 r${result.revision}（${result.row_count} ${props.unitLabel}）`)
    emit('published', result)
    emit('update:modelValue', false)
  } catch (error) {
    if (isStale(error)) {
      ElMessage.error('制品已被他人修改（409 stale_artifact），请刷新页面后重试')
    } else {
      ElMessage.error(`发布失败：${await apiErrorDetail(error)}`)
    }
  } finally {
    publishing.value = false
  }
}

defineExpose({ confirmPublish, canPublish })
</script>

<style scoped>
.publish-diff { display: flex; flex-direction: column; gap: 10px; }
.publish-diff__title { margin: 0; font-size: 13.5px; }
.publish-diff__count { margin: 0 4px; color: var(--kb-accent, #2563eb); }
.publish-diff__keys { display: flex; gap: 6px; flex-wrap: wrap; max-height: 180px; overflow: auto; }
.publish-diff__none { font-size: 12.5px; color: var(--kb-text-tertiary); }
.publish-diff__form { margin-top: 6px; }
.publish-diff__required { font-size: 12px; color: var(--el-color-warning, #e6a23c); }

/* ── wiki 页级差异 ── */
.publish-diff__pages :deep(.el-collapse-item__header) { font-size: 13px; }
.publish-diff__page-key {
  font-family: ui-monospace, SFMono-Regular, Consolas, 'Courier New', monospace;
  font-size: 12.5px;
  margin-right: 8px;
  word-break: break-all;
}
.publish-diff__field-tag { margin-right: 4px; }
.publish-diff__field { display: flex; align-items: flex-start; gap: 8px; flex-wrap: wrap; margin: 6px 0; }
.publish-diff__field-name { font-weight: 600; font-size: 12.5px; min-width: 64px; }
.publish-diff__field-old { color: var(--el-color-danger, #f56c6c); word-break: break-all; }
.publish-diff__arrow { color: var(--kb-text-tertiary, #909399); }
.publish-diff__field-new { color: var(--el-color-success, #67c23a); word-break: break-all; }
.publish-diff__hunks {
  flex-basis: 100%;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 4px;
  overflow: auto;
  max-height: 260px;
}
.publish-diff__hunk-line {
  font-family: ui-monospace, SFMono-Regular, Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.7;
  padding: 0 10px;
  white-space: pre-wrap;
  word-break: break-all;
}
.publish-diff__hunk-line.is-add {
  background: rgba(103, 194, 58, 0.12);
  color: var(--el-color-success, #67c23a);
}
.publish-diff__hunk-line.is-del {
  background: rgba(245, 108, 108, 0.12);
  color: var(--el-color-danger, #f56c6c);
}
.publish-diff__hunk-line.is-meta { color: var(--kb-text-tertiary, #909399); }
.publish-diff__truncated { flex-basis: 100%; font-size: 12px; color: var(--el-color-warning, #e6a23c); }
</style>
