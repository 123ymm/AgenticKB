<template>
  <el-dialog
    :model-value="modelValue"
    title="发布制品"
    width="680px"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <div v-loading="diffLoading" class="publish-diff">
      <p class="publish-diff__title">本次发布将产生的变化（draft vs 当前已发布版）：</p>
      <el-empty
        v-if="!diffLoading && !diffResult"
        description="差异加载失败或无差异数据"
        :image-size="60"
      />
      <el-collapse v-else-if="diffResult" v-model="activeSections">
        <el-collapse-item name="added">
          <template #title>
            新增 <b class="publish-diff__count">{{ diffResult.added_count }}</b> 行
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
            修改 <b class="publish-diff__count">{{ diffResult.changed_count }}</b> 处（列级明细）
          </template>
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
        </el-collapse-item>
        <el-collapse-item name="removed">
          <template #title>
            删除 <b class="publish-diff__count">{{ diffResult.removed_count }}</b> 行
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
import type { DiffResult, PublishResult } from '@/types/artifacts'

const props = defineProps<{
  artifactId: string
  modelValue: boolean
  /** 乐观锁令牌（artifact.updated_at）。 */
  expectedUpdatedAt?: string
  /** 当前制品使用说明（usage_note 门禁——发布前必填）。 */
  usageNote: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  published: [result: PublishResult]
  /** 元信息已被本对话框推进（乐观锁令牌变化，父级应 reload）。 */
  changed: []
}>()

const api = useArtifactsApi()

const diffResult = ref<DiffResult | null>(null)
const diffLoading = ref(false)
const publishing = ref(false)
const activeSections = ref(['added', 'changed', 'removed'])
const usageNote = ref('')
const changeSummary = ref('')

/** usage_note 非空才允许发布（后端同门禁 422 兜底）。 */
const canPublish = computed(() => usageNote.value.trim().length > 0)

watch(() => props.modelValue, (open) => {
  if (!open) return
  usageNote.value = props.usageNote
  changeSummary.value = ''
  activeSections.value = ['added', 'changed', 'removed']
  void loadDiff()
}, { immediate: true })

async function loadDiff(): Promise<void> {
  diffLoading.value = true
  diffResult.value = null
  try {
    diffResult.value = await api.diff(props.artifactId)
  } catch (error) {
    // viewer / 无 draft 权限等场景：仍开放表单（后端发布时再拦），差异区留空提示
    ElMessage.error(`差异加载失败：${await apiErrorDetail(error)}`)
  } finally {
    diffLoading.value = false
  }
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
    ElMessage.success(`已发布 r${result.revision}（${result.row_count} 行）`)
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
</style>
