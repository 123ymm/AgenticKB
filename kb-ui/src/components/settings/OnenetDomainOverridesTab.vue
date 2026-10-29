<template>
  <div class="onenet-overrides-tab">
    <div class="onenet-overrides-tab__hint">
      知识一张网接入凭据按域覆盖：全局基底在「系统配置」的 onenet.yaml，此处只配置某域与全局的
      <strong>差异</strong>（merge）；<strong>留空的字段继承全局</strong>。常用字段（app_id /
      static_token / token_url / search_url）用表单编辑，调优参数（source_type / timeout /
      verify_tls / chunk_width / page_size / throttle_seconds）在高级 YAML 模式编辑。
      保存后<strong>约 10 秒内生效</strong>（mining 短 TTL 拉取），无需重启服务。
    </div>
    <div class="onenet-overrides-tab__layout">
      <div class="onenet-overrides-tab__sidebar">
        <div class="onenet-overrides-tab__sidebar-header">
          <span class="onenet-overrides-tab__sidebar-title">已覆盖域</span>
          <el-button size="small" :disabled="candidates.length === 0" @click="showCreateDialog = true">
            新增
          </el-button>
        </div>
        <div v-if="overrides.length === 0" class="onenet-overrides-tab__sidebar-empty">
          暂无域覆盖（全部域使用全局配置）
        </div>
        <div
          v-for="o in overrides"
          :key="o.domain_id"
          class="onenet-overrides-tab__item"
          :class="{ 'onenet-overrides-tab__item--active': o.domain_id === selectedDomainId }"
          @click="select(o.domain_id)"
        >
          <span class="onenet-overrides-tab__item-name">{{ displayName(o.domain_id) }}</span>
          <span class="onenet-overrides-tab__item-meta">app_id: {{ o.app_id || '—' }}</span>
        </div>
      </div>
      <div class="onenet-overrides-tab__main">
        <div v-if="!selectedDomainId" class="onenet-overrides-tab__empty">
          从左侧选择已覆盖的域，或点击「新增」为某域创建覆盖
        </div>
        <template v-else>
          <div class="onenet-overrides-tab__header">
            <span class="onenet-overrides-tab__domain-label">{{ selectedDomainId }}.yaml</span>
            <div class="onenet-overrides-tab__actions">
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

          <div class="onenet-overrides-tab__body">
            <el-form class="onenet-overrides-tab__form" label-position="left" label-width="130px">
              <div class="onenet-overrides-tab__section-title">接入凭据（app_id / static_token）</div>
              <el-form-item label="App ID">
                <el-input
                  v-model="appId"
                  clearable
                  :placeholder="inheritPlaceholder(globalCfg?.appId)"
                  class="onenet-overrides-tab__control"
                />
              </el-form-item>
              <el-form-item label="Static Token">
                <el-input
                  v-model="staticToken"
                  clearable
                  show-password
                  type="password"
                  placeholder="继承全局（不回显）"
                  class="onenet-overrides-tab__control"
                />
                <div class="onenet-overrides-tab__field-hint">
                  留空继承全局；只改 app_id 不改 token 时，merge 后仍用全局 token
                </div>
              </el-form-item>

              <div class="onenet-overrides-tab__section-title">端点（可选，一般继承全局）</div>
              <el-form-item label="Token URL">
                <el-input
                  v-model="tokenUrl"
                  clearable
                  :placeholder="inheritPlaceholder(globalCfg?.tokenUrl)"
                  class="onenet-overrides-tab__control"
                />
              </el-form-item>
              <el-form-item label="Search URL">
                <el-input
                  v-model="searchUrl"
                  clearable
                  :placeholder="inheritPlaceholder(globalCfg?.searchUrl)"
                  class="onenet-overrides-tab__control"
                />
              </el-form-item>
            </el-form>

            <el-collapse v-model="advancedOpen" class="onenet-overrides-tab__advanced">
              <el-collapse-item
                name="yaml"
                title="高级：YAML 模式（直接编辑所存原文，表单与它始终一致；调优参数在此编辑）"
              >
                <div class="onenet-overrides-tab__editor-wrap">
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
import { useControlPlaneApi, type ControlPlaneDomainSummary, type OnenetDomainOverrideSummary } from '@/api/controlPlane'
import YamlEditor from '@/components/common/YamlEditor.vue'
import { patchFlat, readFlat } from '@/utils/llmOverrideYaml'

const api = useControlPlaneApi()

const overrides = ref<OnenetDomainOverrideSummary[]>([])
const domains = ref<ControlPlaneDomainSummary[]>([])
const selectedDomainId = ref('')
/** 单一真相源：所存 YAML 原文。表单与高级编辑器都读写它（62号镜像）。 */
const text = ref('')
const original = ref('')
const saving = ref(false)
const deleting = ref(false)
const showCreateDialog = ref(false)
const createDomainId = ref('')
const advancedOpen = ref<string[]>([])

/** 全局基底摘要（继承 placeholder）；拉取失败降级为通用文案。 */
const globalCfg = ref<{
  appId: string
  tokenUrl: string
  searchUrl: string
} | null>(null)

const dirty = computed(() => text.value !== original.value)
const candidates = computed(() =>
  domains.value.filter(d => !overrides.value.some(o => o.domain_id === d.domain_id)),
)

/** 新覆盖模板：全注释平铺形态（表单第一笔输入会重写为等价 YAML）。 */
const TEMPLATE = `# 域覆盖：只写与全局 onenet.yaml 的差异（未列出的键继承全局，平铺 merge）
# 允许的键（10 个）：app_id / static_token / token_url / search_url / source_type /
#   timeout / verify_tls / chunk_width / page_size / throttle_seconds
# 表单模式可编辑凭据与端点；调优参数在此直接写（去掉注释即可用）：
# app_id: odn-app                       # 该域独立租户
# static_token: <该域自己的 token>
# token_url: http://oauth2.example/appToken/get   # 可选：独立端点
# search_url: http://apigw.example/api/search?source_type=0
# timeout: 300                          # 可选调优：请求超时（秒）
# verify_tls: false                     # 可选调优：端点为 https 时按需打开
# chunk_width: 10000                    # 可选调优：part_id 分段宽度（上限 10000）
# page_size: 1000                       # 可选调优：段内翻页每页条数
# throttle_seconds: 0.3                 # 可选调优：相邻请求限速（秒）
`

// ── 表单字段：getter 读 YAML，setter 经 patchFlat 写回（空值=删键） ──

const appId = computed({
  get: () => readFlat(text.value, 'app_id'),
  set: (v: string) => { text.value = patchFlat(text.value, 'app_id', v ?? '') },
})
const staticToken = computed({
  get: () => readFlat(text.value, 'static_token'),
  set: (v: string) => { text.value = patchFlat(text.value, 'static_token', v ?? '') },
})
const tokenUrl = computed({
  get: () => readFlat(text.value, 'token_url'),
  set: (v: string) => { text.value = patchFlat(text.value, 'token_url', v ?? '') },
})
const searchUrl = computed({
  get: () => readFlat(text.value, 'search_url'),
  set: (v: string) => { text.value = patchFlat(text.value, 'search_url', v ?? '') },
})

function inheritPlaceholder(globalValue?: string): string {
  return globalValue ? `继承全局（${globalValue}）` : '继承全局'
}

function displayName(domainId: string): string {
  return domains.value.find(d => d.domain_id === domainId)?.display_name ?? domainId
}

async function load() {
  const [o, d] = await Promise.all([api.listOnenetDomainOverrides(), api.getDomains()])
  overrides.value = o
  domains.value = d
}

/** 全局基底摘要：onenet.yaml 解析值（static_token 不回显）。失败只降级 placeholder。 */
async function loadGlobal() {
  try {
    const raw = await api.getSystemConfig('onenet')
    globalCfg.value = {
      appId: typeof raw?.app_id === 'string' ? raw.app_id : '',
      tokenUrl: typeof raw?.token_url === 'string' ? raw.token_url : '',
      searchUrl: typeof raw?.search_url === 'string' ? raw.search_url : '',
    }
  } catch {
    globalCfg.value = null
  }
}

async function select(domainId: string) {
  selectedDomainId.value = domainId
  text.value = await api.getOnenetDomainOverrideRaw(domainId)
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

/** 后端 400 带结构化中文报文（如「merge 后仍缺必填凭据」），尽量透传给用户。 */
function errDetail(e: unknown): string {
  if (isAxiosError(e)) {
    const d = e.response?.data as { detail?: unknown } | string | undefined
    if (typeof d === 'string' && d.trim()) return d.trim()
    if (d && typeof d === 'object' && d.detail) return String(d.detail)
  }
  return e instanceof Error ? e.message : '未知错误'
}

async function handleSave() {
  if (!selectedDomainId.value) return
  saving.value = true
  try {
    await api.saveOnenetDomainOverrideRaw(selectedDomainId.value, text.value)
    original.value = text.value
    await load()
    ElMessage.success('已保存（约 10 秒内生效）')
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
      `确认删除域 "${id}" 的一张网覆盖？删除后该域回落全局配置。`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return // cancelled
  }
  deleting.value = true
  try {
    await api.deleteOnenetDomainOverride(id)
    selectedDomainId.value = ''
    text.value = ''
    original.value = ''
    await load()
    ElMessage.success('已删除覆盖（约 10 秒内生效）')
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
.onenet-overrides-tab__hint {
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--kb-text-secondary);
  background: rgba(64, 158, 255, 0.05);
  border: 1px solid rgba(64, 158, 255, 0.15);
  border-radius: 6px;
  padding: 10px 14px;
}

.onenet-overrides-tab__hint code {
  background: rgba(0, 0, 0, 0.05);
  padding: 1px 5px;
  border-radius: 3px;
  font-size: 12px;
}

.onenet-overrides-tab__layout {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr);
  grid-template-rows: 1fr;
  gap: 0;
  height: calc(100vh - 300px);
  min-height: 360px;
}

.onenet-overrides-tab__sidebar {
  border-right: 1px solid var(--kb-border-light);
  display: flex;
  flex-direction: column;
}

.onenet-overrides-tab__sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--kb-border-light);
  flex-shrink: 0;
}

.onenet-overrides-tab__sidebar-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.onenet-overrides-tab__sidebar-empty {
  padding: 16px 12px;
  font-size: 12px;
  color: var(--kb-text-tertiary);
  line-height: 1.6;
}

.onenet-overrides-tab__item {
  padding: 8px 12px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.onenet-overrides-tab__item:hover {
  background: var(--kb-bg-hover, rgba(0, 0, 0, 0.03));
}

.onenet-overrides-tab__item--active {
  background: rgba(64, 158, 255, 0.08);
}

.onenet-overrides-tab__item-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--kb-text-primary);
}

.onenet-overrides-tab__item-meta {
  font-size: 11px;
  color: var(--kb-text-tertiary);
}

.onenet-overrides-tab__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.onenet-overrides-tab__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  border-bottom: 1px solid var(--kb-border-light);
  flex-shrink: 0;
}

.onenet-overrides-tab__domain-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--kb-text-primary);
  font-family: var(--el-font-family, monospace);
}

.onenet-overrides-tab__actions {
  display: flex;
  gap: 8px;
}

.onenet-overrides-tab__body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 16px;
}

.onenet-overrides-tab__form {
  max-width: 640px;
}

.onenet-overrides-tab__section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
  margin: 10px 0 12px;
  padding-top: 6px;
  border-top: 1px dashed var(--kb-border-light);
}

.onenet-overrides-tab__section-title:first-child {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}

.onenet-overrides-tab__control {
  width: 100%;
  max-width: 420px;
}

.onenet-overrides-tab__field-hint {
  width: 100%;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--kb-text-tertiary);
}

.onenet-overrides-tab__advanced {
  margin-top: 14px;
  max-width: 640px;
  border-top: none;
}

.onenet-overrides-tab__editor-wrap {
  height: 280px;
  position: relative;
  overflow: hidden;
  border: 1px solid var(--kb-border-light);
  border-radius: 4px;
}

.onenet-overrides-tab__empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
  color: var(--kb-text-tertiary);
  font-size: 14px;
}
</style>
