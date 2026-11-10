<template>
  <div class="llm-overrides-tab">
    <div class="llm-overrides-tab__hint">
      LLM 配置按域覆盖：全局基底在「系统配置」的 llm_service.yaml，此处只配置某域与全局的
      <strong>差异</strong>（deep-merge）；<strong>留空的字段继承全局</strong>。可按域覆盖
      <code>provider</code>（chat）/ <code>rerank</code> / <code>embedding</code> 的模型、端点与密钥；
      <strong>embedding 的 dimensions 锁全局</strong>——所有域共享同一定长向量列，维度不可按域改变。
      保存后自动热加载，无需重启服务。
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
            chat: {{ o.active_model || '—' }} · rerank: {{ o.rerank_model || '—' }} · emb:
            {{ o.embedding_model || '—' }}
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

          <div class="llm-overrides-tab__body">
            <el-alert
              v-if="embeddingTouched"
              class="llm-overrides-tab__emb-alert"
              type="warning"
              :closable="false"
              show-icon
              title="本域使用独立 embedding 配置"
              description="保存后该域新向量由覆盖模型生成；存量向量（旧模型）不会自动重嵌——新旧混存可能使检索质量劣化，重嵌工具将在下一批提供。dimensions 与全局一致，向量维度不会漂移。"
            />

            <el-form class="llm-overrides-tab__form" label-position="left" label-width="130px">
              <div class="llm-overrides-tab__section-title">Chat 对话模型（provider）</div>
              <el-form-item label="默认模型">
                <el-select
                  v-model="chatModel"
                  clearable
                  filterable
                  allow-create
                  :placeholder="inheritPlaceholder(globalCfg?.activeModel)"
                  class="llm-overrides-tab__control"
                >
                  <el-option
                    v-for="m in chatModelOptions"
                    :key="m"
                    :value="m"
                    :label="m === globalCfg?.activeModel ? `${m}（全局默认）` : m"
                  />
                </el-select>
                <div class="llm-overrides-tab__field-hint">
                  留空继承全局；也可在 YAML 模式为该域添加自有模型（独立端点/密钥）
                </div>
              </el-form-item>

              <div class="llm-overrides-tab__section-title">Rerank 重排（rerank）</div>
              <el-form-item label="模型">
                <el-input
                  v-model="rerankModel"
                  clearable
                  :placeholder="inheritPlaceholder(globalCfg?.rerankModel)"
                  class="llm-overrides-tab__control"
                />
              </el-form-item>
              <el-form-item label="Base URL">
                <el-input
                  v-model="rerankBaseUrl"
                  clearable
                  placeholder="继承全局"
                  class="llm-overrides-tab__control"
                />
              </el-form-item>
              <el-form-item label="API Key">
                <el-input
                  v-model="rerankApiKey"
                  clearable
                  show-password
                  type="password"
                  placeholder="继承全局（不回显）"
                  class="llm-overrides-tab__control"
                />
                <div class="llm-overrides-tab__field-hint">
                  留空继承全局；填写后该域用独立端点/密钥调用 rerank
                </div>
              </el-form-item>

              <div class="llm-overrides-tab__section-title">
                Embedding 向量（embedding）
                <el-tag size="small" type="info" class="llm-overrides-tab__lock-tag">🔒 同维度</el-tag>
              </div>
              <el-form-item label="模型">
                <el-input
                  v-model="embeddingModel"
                  clearable
                  :placeholder="inheritPlaceholder(globalCfg?.embeddingModel)"
                  class="llm-overrides-tab__control"
                />
              </el-form-item>
              <el-form-item label="Base URL">
                <el-input
                  v-model="embeddingBaseUrl"
                  clearable
                  placeholder="继承全局"
                  class="llm-overrides-tab__control"
                />
              </el-form-item>
              <el-form-item label="API Key">
                <el-input
                  v-model="embeddingApiKey"
                  clearable
                  show-password
                  type="password"
                  placeholder="继承全局（不回显）"
                  class="llm-overrides-tab__control"
                />
                <div class="llm-overrides-tab__field-hint">
                  留空继承全局密钥；填写后该域用独立密钥调用
                </div>
              </el-form-item>
              <el-form-item label="向量维度">
                <el-input
                  :model-value="lockedDimensions"
                  readonly
                  class="llm-overrides-tab__control"
                >
                  <template #suffix>
                    <el-tooltip
                      content="所有域共享同一定长向量列（vector 列宽）。更换嵌入模型请保持 dimensions 与全局一致——服务端会拒绝维度漂移的覆盖。"
                      placement="top"
                    >
                      <span class="llm-overrides-tab__lock-hint">🔒 锁全局</span>
                    </el-tooltip>
                  </template>
                </el-input>
              </el-form-item>
            </el-form>

            <el-collapse v-model="advancedOpen" class="llm-overrides-tab__advanced">
              <el-collapse-item
                name="yaml"
                title="高级：YAML 模式（直接编辑所存原文，表单与它始终一致）"
              >
                <div class="llm-overrides-tab__editor-wrap">
                  <YamlEditor v-model="text" />
                </div>
              </el-collapse-item>
            </el-collapse>
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
          创建（预填表单模板）
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
import { embeddingSection, ownModelKeys, patchForm, readForm } from '@/utils/llmOverrideYaml'

const api = useControlPlaneApi()

const overrides = ref<LlmDomainOverrideSummary[]>([])
const domains = ref<ControlPlaneDomainSummary[]>([])
const selectedDomainId = ref('')
/** 单一真相源：所存 YAML 原文。表单与高级编辑器都读写它（62号二期表单化）。 */
const text = ref('')
const original = ref('')
const saving = ref(false)
const deleting = ref(false)
const showCreateDialog = ref(false)
const createDomainId = ref('')
const advancedOpen = ref<string[]>([])

/** 全局基底摘要（继承 placeholder / 锁定维度展示）；拉取失败降级为通用文案。 */
const globalCfg = ref<{
  activeModel: string
  modelKeys: string[]
  rerankModel: string
  embeddingModel: string
  embeddingDimensions: string
} | null>(null)

const dirty = computed(() => text.value !== original.value)
const candidates = computed(() =>
  domains.value.filter(d => !overrides.value.some(o => o.domain_id === d.domain_id)),
)
/** embedding 段非空 = 本域使用独立 embedding 配置（常驻警告 + 保存确认）。 */
const embeddingTouched = computed(() => Object.keys(embeddingSection(text.value)).length > 0)

/** 新覆盖模板：全注释形态（表单第一笔输入会重写为等价 YAML）。 */
const TEMPLATE = `# 域覆盖：只写与全局 llm_service.yaml 的差异（未列出的字段继承全局）
# 允许的顶层键：provider / rerank / embedding（embedding.dimensions 锁全局）
# 表单模式可直接编辑常用字段；此处为等价 YAML 示例（去掉注释即可用）：
# provider:
#   active_model: glm-4-flash          # 该域默认 chat 模型（须在 merge 后 models 里可解析）
#   models:                            # 也可为该域加自有模型（含独立密钥）
#     odn-chat:
#       model: glm-4-flash
#       base_url: https://open.bigmodel.cn/api/paas/v4/
#       api_key: <该域自己的 key>
# rerank:
#   model: rerank-pro                  # 该域默认 rerank 模型
#   base_url: https://open.bigmodel.cn/api/paas/v4/   # 可选：该域独立 rerank 端点/密钥
#   api_key: <该域自己的 key>
# embedding:                           # 同维度才能保存（dimensions 继承全局，勿写）
#   model: embedding-3
#   base_url: https://open.bigmodel.cn/api/paas/v4/
#   api_key: <该域自己的 key>
`

// ── 表单字段：getter 读 YAML，setter 经 patchForm 写回（空值=删键） ──

const chatModel = computed({
  get: () => readForm(text.value, 'provider', 'active_model'),
  set: (v: string) => { text.value = patchForm(text.value, 'provider', 'active_model', v ?? '') },
})
const rerankModel = computed({
  get: () => readForm(text.value, 'rerank', 'model'),
  set: (v: string) => { text.value = patchForm(text.value, 'rerank', 'model', v ?? '') },
})
const rerankBaseUrl = computed({
  get: () => readForm(text.value, 'rerank', 'base_url'),
  set: (v: string) => { text.value = patchForm(text.value, 'rerank', 'base_url', v ?? '') },
})
const rerankApiKey = computed({
  get: () => readForm(text.value, 'rerank', 'api_key'),
  set: (v: string) => { text.value = patchForm(text.value, 'rerank', 'api_key', v ?? '') },
})
const embeddingModel = computed({
  get: () => readForm(text.value, 'embedding', 'model'),
  set: (v: string) => { text.value = patchForm(text.value, 'embedding', 'model', v ?? '') },
})
const embeddingBaseUrl = computed({
  get: () => readForm(text.value, 'embedding', 'base_url'),
  set: (v: string) => { text.value = patchForm(text.value, 'embedding', 'base_url', v ?? '') },
})
const embeddingApiKey = computed({
  get: () => readForm(text.value, 'embedding', 'api_key'),
  set: (v: string) => { text.value = patchForm(text.value, 'embedding', 'api_key', v ?? '') },
})

/** chat 候选 = 全局 models ∪ 域自有 models ∪ 当前值。 */
const chatModelOptions = computed(() => {
  const keys = [
    ...(globalCfg.value?.modelKeys ?? []),
    ...ownModelKeys(text.value),
    chatModel.value,
  ].filter(Boolean)
  return [...new Set(keys)]
})

/** 维度锁全局：只读展示全局值（覆盖不可改；服务端拒绝维度漂移）。 */
const lockedDimensions = computed(() => {
  const dims = globalCfg.value?.embeddingDimensions
  return dims ? `${dims}（锁全局）` : '全局配置值（锁全局）'
})

function inheritPlaceholder(globalValue?: string): string {
  return globalValue ? `继承全局（${globalValue}）` : '继承全局'
}

function displayName(domainId: string): string {
  return domains.value.find(d => d.domain_id === domainId)?.display_name ?? domainId
}

async function load() {
  const [o, d] = await Promise.all([api.listLlmDomainOverrides(), api.getDomains()])
  overrides.value = o
  domains.value = d
}

/** 全局基底摘要：llm_service.yaml 解析值。失败只降级 placeholder，不阻塞页面。 */
async function loadGlobal() {
  try {
    const raw = await api.getSystemConfig('llm_service')
    const provider = (raw?.provider ?? {}) as Record<string, unknown>
    const rerank = (raw?.rerank ?? {}) as Record<string, unknown>
    const embedding = (raw?.embedding ?? {}) as Record<string, unknown>
    globalCfg.value = {
      activeModel: typeof provider.active_model === 'string' ? provider.active_model : '',
      modelKeys: Object.keys((provider.models ?? {}) as Record<string, unknown>),
      rerankModel: typeof rerank.model === 'string' ? rerank.model : '',
      embeddingModel: typeof embedding.model === 'string' ? embedding.model : '',
      embeddingDimensions: embedding.dimensions != null ? String(embedding.dimensions) : '',
    }
  } catch {
    globalCfg.value = null
  }
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

/** 后端 400 带结构化中文报文（如「embedding.dimensions 锁全局」），尽量透传给用户。 */
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
  // 护栏：embedding 段与保存前有差异 → 显式确认（存量向量不自动重嵌，混存风险）
  const before = JSON.stringify(embeddingSection(original.value))
  const after = JSON.stringify(embeddingSection(text.value))
  if (before !== after) {
    try {
      await ElMessageBox.confirm(
        'embedding 覆盖将变更该域后续新向量的生成模型：存量向量不会自动重嵌（新旧混存，检索质量可能劣化；重嵌工具下一批提供）。确认保存？',
        'embedding 配置变更确认',
        { type: 'warning' },
      )
    } catch {
      return // cancelled
    }
  }
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
    await Promise.all([load(), loadGlobal()])
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

.llm-overrides-tab__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px;
}

.llm-overrides-tab__emb-alert {
  margin-bottom: 14px;
}

.llm-overrides-tab__form {
  max-width: 640px;
}

.llm-overrides-tab__section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
  margin: 10px 0 12px;
  padding-top: 6px;
  border-top: 1px dashed var(--kb-border-light);
}

.llm-overrides-tab__section-title:first-child {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}

.llm-overrides-tab__lock-tag {
  margin-left: 8px;
}

.llm-overrides-tab__control {
  width: 100%;
  max-width: 420px;
}

.llm-overrides-tab__field-hint {
  width: 100%;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--kb-text-tertiary);
}

.llm-overrides-tab__lock-hint {
  font-size: 12px;
  color: var(--kb-text-tertiary);
  cursor: help;
}

.llm-overrides-tab__advanced {
  margin-top: 14px;
  max-width: 640px;
  border-top: none;
}

.llm-overrides-tab__editor-wrap {
  height: 280px;
  position: relative;
  overflow: hidden;
  border: 1px solid var(--kb-border-light);
  border-radius: 4px;
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
