<template>
  <div class="llm-overrides-tab">
    <div class="llm-overrides-tab__hint">
      LLM 配置按域覆盖：全局基底在「系统配置」的 llm_service.yaml，此处只写某域与全局的
      <strong>差异</strong>（deep-merge）；未列出的字段继承全局。允许的顶层键
      <code>provider</code> / <code>rerank</code>——<strong>embedding 锁全局</strong>，
      写它会保存失败。保存后自动热加载，无需重启服务。
    </div>
    <div class="llm-overrides-tab__layout">
      <div class="llm-overrides-tab__sidebar">
        <div class="llm-overrides-tab__sidebar-header">
          <span class="llm-overrides-tab__sidebar-title">已覆盖域</span>
          <el-button size="small" :disabled="candidates.length === 0" @click="showCreateDialog = true">
            新增
          </el-button>
        </div>
        <div v-if="overrides.length === 0" class="llm-overrides-tab__sidebar-empty">
          暂无域覆盖（全部域使用全局配置）
        </div>
        <div
          v-for="o in overrides"
          :key="o.domain_id"
          class="llm-overrides-tab__item"
          :class="{ 'llm-overrides-tab__item--active': o.domain_id === selectedDomainId }"
          @click="select(o.domain_id)"
        >
          <span class="llm-overrides-tab__item-name">{{ displayName(o.domain_id) }}</span>
          <span class="llm-overrides-tab__item-meta">
            chat: {{ o.active_model || '—' }} · rerank: {{ o.rerank_model || '—' }}
          </span>
        </div>
      </div>
      <div class="llm-overrides-tab__main">
        <div v-if="!selectedDomainId" class="llm-overrides-tab__empty">
          从左侧选择已覆盖的域，或点击「新增」为某域创建覆盖
        </div>
        <template v-else>
          <div class="llm-overrides-tab__header">
            <span class="llm-overrides-tab__domain-label">{{ selectedDomainId }}.yaml</span>
            <div class="llm-overrides-tab__actions">
              <el-button
                type="primary"
                size="small"
                :loading="saving"
                :disabled="!dirty"
                @click="handleSave"
              >
                保存
              </el-button>
              <el-button size="small" type="danger" :loading="deleting" @click="handleDelete">
                删除覆盖
              </el-button>
            </div>
          </div>
          <div class="llm-overrides-tab__editor-wrap">
            <YamlEditor v-model="text" />
          </div>
        </template>
      </div>
    </div>

    <el-dialog v-model="showCreateDialog" title="新增域覆盖" width="420px" @close="createDomainId = ''">
      <el-select v-model="createDomainId" placeholder="选择域" style="width: 100%">
        <el-option
          v-for="d in candidates"
          :key="d.domain_id"
          :value="d.domain_id"
          :label="`${d.display_name} (${d.domain_id})`"
        />
      </el-select>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" :disabled="!createDomainId" @click="handleCreate">
          创建（编辑器预填模板）
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { isAxiosError } from 'axios'
import { useControlPlaneApi, type ControlPlaneDomainSummary, type LlmDomainOverrideSummary } from '@/api/controlPlane'
import YamlEditor from '@/components/common/YamlEditor.vue'

const api = useControlPlaneApi()

const overrides = ref<LlmDomainOverrideSummary[]>([])
const domains = ref<ControlPlaneDomainSummary[]>([])
const selectedDomainId = ref('')
const text = ref('')
const original = ref('')
const saving = ref(false)
const deleting = ref(false)
const showCreateDialog = ref(false)
const createDomainId = ref('')

const dirty = computed(() => text.value !== original.value)
const candidates = computed(() =>
  domains.value.filter(d => !overrides.value.some(o => o.domain_id === d.domain_id)),
)

/** 新覆盖的编辑器模板：全注释形态（示例去掉注释即可保存）。 */
const TEMPLATE = `# 域覆盖：只写与全局 llm_service.yaml 的差异（未列出的字段继承全局）
# 允许的顶层键：provider / rerank —— embedding 锁全局，禁止覆盖
# 示例（去掉注释即可用）：
# provider:
#   active_model: glm-4-flash          # 该域默认 chat 模型（须在 merge 后 models 里可解析）
#   models:                            # 也可为该域加自有模型（含独立密钥）
#     odn-chat:
#       model: glm-4-flash
#       base_url: https://open.bigmodel.cn/api/paas/v4/
#       api_key: <该域自己的 key>
# rerank:
#   model: rerank-pro                  # 该域默认 rerank 模型
`

function displayName(domainId: string): string {
  return domains.value.find(d => d.domain_id === domainId)?.display_name ?? domainId
}

async function load() {
  const [o, d] = await Promise.all([api.listLlmDomainOverrides(), api.getDomains()])
  overrides.value = o
  domains.value = d
}

async function select(domainId: string) {
  selectedDomainId.value = domainId
  text.value = await api.getLlmDomainOverrideRaw(domainId)
  original.value = text.value
}

function handleCreate() {
  const id = createDomainId.value
  if (!id) return
  showCreateDialog.value = false
  createDomainId.value = ''
  selectedDomainId.value = id
  text.value = TEMPLATE
  original.value = TEMPLATE
}

/** 后端 400 带结构化中文报文（如「embedding 锁全局」），尽量透传给用户。 */
function errDetail(e: unknown): string {
  if (isAxiosError(e)) {
    const d = e.response?.data as { detail?: unknown } | string | undefined
    if (typeof d === 'string' && d.trim()) return d.trim()
    if (d && typeof d === 'object' && d.detail) return String(d.detail)
  }
  return e instanceof Error ? e.message : '未知错误'
}

function reloadMessage(prefix: string, reload?: { ok: boolean; detail: string | null }) {
  if (reload?.ok) {
    ElMessage.success(`${prefix}，已热加载`)
  } else if (reload) {
    ElMessage.warning(`${prefix}，但热加载失败（${reload.detail ?? '未知原因'}）——重启后台服务后生效`)
  } else {
    ElMessage.success(prefix)
  }
}

async function handleSave() {
  if (!selectedDomainId.value) return
  saving.value = true
  try {
    const result = await api.saveLlmDomainOverrideRaw(selectedDomainId.value, text.value)
    original.value = text.value
    await load()
    reloadMessage('已保存', result.llm_reload)
  } catch (e) {
    ElMessage.error(`保存失败：${errDetail(e)}`)
  } finally {
    saving.value = false
  }
}

async function handleDelete() {
  if (!selectedDomainId.value) return
  const id = selectedDomainId.value
  try {
    await ElMessageBox.confirm(
      `确认删除域 "${id}" 的 LLM 覆盖？删除后该域回落全局配置。`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return // cancelled
  }
  deleting.value = true
  try {
    const result = await api.deleteLlmDomainOverride(id)
    selectedDomainId.value = ''
    text.value = ''
    original.value = ''
    await load()
    reloadMessage('已删除覆盖', result.llm_reload)
  } catch (e) {
    ElMessage.error(`删除失败：${errDetail(e)}`)
  } finally {
    deleting.value = false
  }
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    ElMessage.error(`加载失败：${errDetail(e)}`)
  }
})
</script>

<style scoped>
.llm-overrides-tab__hint {
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--kb-text-secondary);
  background: rgba(64, 158, 255, 0.05);
  border: 1px solid rgba(64, 158, 255, 0.15);
  border-radius: 6px;
  padding: 10px 14px;
}

.llm-overrides-tab__hint code {
  background: rgba(0, 0, 0, 0.05);
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 12px;
}

.llm-overrides-tab__layout {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr);
  grid-template-rows: 1fr;
  gap: 0;
  height: calc(100vh - 300px);
  min-height: 360px;
}

.llm-overrides-tab__sidebar {
  border-right: 1px solid var(--kb-border-light);
  display: flex;
  flex-direction: column;
}

.llm-overrides-tab__sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--kb-border-light);
  flex-shrink: 0;
}

.llm-overrides-tab__sidebar-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.llm-overrides-tab__sidebar-empty {
  padding: 16px 12px;
  font-size: 12px;
  color: var(--kb-text-tertiary);
  line-height: 1.6;
}

.llm-overrides-tab__item {
  padding: 8px 12px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.llm-overrides-tab__item:hover {
  background: var(--kb-bg-hover, rgba(0, 0, 0, 0.03));
}

.llm-overrides-tab__item--active {
  background: rgba(64, 158, 255, 0.08);
}

.llm-overrides-tab__item-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--kb-text-primary);
}

.llm-overrides-tab__item-meta {
  font-size: 11px;
  color: var(--kb-text-tertiary);
}

.llm-overrides-tab__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.llm-overrides-tab__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  border-bottom: 1px solid var(--kb-border-light);
  flex-shrink: 0;
}

.llm-overrides-tab__domain-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--kb-text-primary);
  font-family: var(--el-font-family, monospace);
}

.llm-overrides-tab__actions {
  display: flex;
  gap: 8px;
}

.llm-overrides-tab__editor-wrap {
  flex: 1;
  min-height: 0;
  position: relative;
  overflow: hidden;
}

.llm-overrides-tab__empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
  color: var(--kb-text-tertiary);
  font-size: 14px;
}
</style>
