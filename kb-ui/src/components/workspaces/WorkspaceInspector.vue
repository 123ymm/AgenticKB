<template>
  <div class="ws-inspector" data-test="inspector">
    <!-- 空态：未选中节点 -->
    <div v-if="!node" class="ws-inspector__empty">
      <el-icon :size="20" class="ws-inspector__empty-icon"><Setting /></el-icon>
      <p class="ws-inspector__empty-text">在步骤流中选择一个节点查看与编辑配置</p>
    </div>

    <template v-else>
      <!-- ── 段一：节点信息 ── -->
      <header class="ws-inspector__head">
        <div class="ws-inspector__head-title">
          <span class="ws-inspector__node-name">{{ node.name }}</span>
          <span class="ws-inspector__op-label">{{ opMeta.label }}</span>
        </div>
        <span v-if="node.pending" class="ws-inspector__badge is-pending">待执行</span>
        <span v-else-if="node.stale" class="ws-inspector__badge is-stale">待更新</span>
        <!-- 删除节点（用户测试反馈修复2）：确认与整图 PUT 归父级——后端同事务
             清理来源绑定/当前输出/导出去处；下游仍引用时 422 由父级提示 -->
        <el-button
          v-if="canWrite"
          class="ws-inspector__delete"
          link
          type="danger"
          size="small"
          data-test="inspector-delete-node"
          :disabled="running"
          @click="emit('delete-node')"
        >删除节点</el-button>
      </header>

      <!-- 命名双字段（用户测试反馈修复3：节点名与产出表名不可改）——草稿
           随「保存配置」整图 PUT 生效；MVP 全部单口算子，输出表名只渲染第一口 -->
      <section v-if="canWrite" class="ws-inspector__sec ws-inspector__identity">
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">节点名称</label>
          <el-input
            v-model="nodeName"
            data-test="inspector-node-name"
            size="small"
            :maxlength="NAME_MAX"
            placeholder="步骤流中显示的名称"
          />
        </div>
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">输出表名</label>
          <el-input
            v-model="portName"
            data-test="inspector-port-name"
            size="small"
            :maxlength="NAME_MAX"
            placeholder="加工结果树与下游引用显示的表名"
          />
        </div>
      </section>

      <!-- 输入选择器：按算子元数渲染（source 语义无输入——后端强制 inputs 空） -->
      <section v-if="arity > 0" class="ws-inspector__sec">
        <h4 class="ws-inspector__sec-title">{{ arity === 2 ? '输入表' : '输入' }}</h4>
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">{{ arity === 2 ? '左表' : '来源表' }}</label>
          <el-select
            v-model="inputs[0]"
            class="ws-inspector__select"
            data-test="inspector-input-0"
            placeholder="选择上游输出"
            size="small"
            filterable
          >
            <el-option v-for="c in candidates" :key="c.ref" :value="c.ref" :label="c.label" />
          </el-select>
        </div>
        <div v-if="arity === 2" class="ws-inspector__field">
          <label class="ws-inspector__field-label">右表</label>
          <el-select
            v-model="inputs[1]"
            class="ws-inspector__select"
            data-test="inspector-input-1"
            placeholder="选择第二张表"
            size="small"
            filterable
          >
            <el-option v-for="c in candidates" :key="c.ref" :value="c.ref" :label="c.label" />
          </el-select>
        </div>
        <p v-if="!candidates.length" class="ws-inspector__hint">没有可用的上游输出——先接入资料并运行</p>
      </section>

      <!-- ── 段二：按 op 分派的参数表单 ── -->

      <!-- source：只读展示（类型/范围/冻结计数，来自 sources 行） -->
      <section v-if="node.op === 'source'" class="ws-inspector__sec">
        <h4 class="ws-inspector__sec-title">来源</h4>
        <template v-if="sourceRow">
          <dl class="ws-inspector__kv">
            <dt>类型</dt>
            <dd>{{ sourceRow.connector === 'kb_dir' ? '知识库目录' : 'Excel 上传' }}</dd>
            <template v-if="sourceRow.connector === 'kb_dir'">
              <dt>知识库</dt>
              <dd>{{ sourceRow.config?.kb_id ?? '—' }}</dd>
              <dt>目录范围</dt>
              <dd class="ws-inspector__dirs">
                <template v-if="dirList.length">
                  <el-tag v-for="d in dirList" :key="d" size="small" effect="plain">{{ d }}</el-tag>
                </template>
                <span v-else class="ws-inspector__muted">全库</span>
              </dd>
            </template>
            <template v-else>
              <dt>文件</dt>
              <dd>{{ sourceRow.config?.file_name ?? '—' }}</dd>
              <dt>工作表</dt>
              <dd>{{ sourceRow.config?.sheet_name ?? '第一张表' }}</dd>
            </template>
            <dt>冻结快照</dt>
            <dd data-test="inspector-source-frozen">{{ frozenText }}</dd>
          </dl>
          <p class="ws-inspector__hint">来源为即读即冻快照——重新接入才会读取新资料</p>
        </template>
        <p v-else class="ws-inspector__warn" data-test="inspector-source-missing">未接入来源——请先「接入资料」</p>
      </section>

      <!-- select：列勾选（输入列）+ 每列改名；无列信息时手输逗号分隔 -->
      <section v-else-if="node.op === 'select'" class="ws-inspector__sec">
        <h4 class="ws-inspector__sec-title">选择列</h4>
        <template v-if="availableCols.length">
          <el-checkbox-group v-model="selectPicked" class="ws-inspector__cols">
            <div v-for="col in availableCols" :key="col" class="ws-inspector__col-row">
              <el-checkbox :value="col" :data-test="`inspector-select-col-${col}`">{{ col }}</el-checkbox>
              <el-input
                v-if="selectPicked.includes(col)"
                v-model="selectRenames[col]"
                class="ws-inspector__rename"
                :data-test="`inspector-rename-${col}`"
                size="small"
                :placeholder="`改名（默认 ${col}）`"
              />
            </div>
          </el-checkbox-group>
        </template>
        <template v-else>
          <el-input
            v-model="selectManual"
            data-test="inspector-select-manual"
            size="small"
            placeholder="逗号分隔列名，如：型号,功率_W"
          />
          <p class="ws-inspector__hint">输入尚未执行——先手输列名，或先执行上游再回来勾选</p>
        </template>
      </section>

      <!-- filter：字段下拉 + 条件下拉 + 值输入（is_empty/not_empty 隐藏值） -->
      <section v-else-if="node.op === 'filter'" class="ws-inspector__sec">
        <h4 class="ws-inspector__sec-title">筛选条件</h4>
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">字段</label>
          <el-select
            v-model="filterField"
            data-test="inspector-filter-field"
            size="small"
            filterable
            allow-create
            placeholder="选择或输入字段"
          >
            <el-option v-for="col in availableCols" :key="col" :value="col" :label="col" />
          </el-select>
        </div>
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">条件</label>
          <el-select v-model="filterCond" data-test="inspector-filter-cond" size="small">
            <el-option v-for="(label, cond) in FILTER_COND_LABELS" :key="cond" :value="cond" :label="label" />
          </el-select>
        </div>
        <div v-if="needsFilterValue" class="ws-inspector__field">
          <label class="ws-inspector__field-label">值</label>
          <el-input
            v-model="filterValue"
            data-test="inspector-filter-value"
            size="small"
            :placeholder="numericCond ? '数值' : '比较值'"
          />
        </div>
      </section>

      <!-- join：左右键下拉（各自输入的列）+ 方式单选 -->
      <section v-else-if="node.op === 'join'" class="ws-inspector__sec">
        <h4 class="ws-inspector__sec-title">关联</h4>
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">左键（{{ inputs[0] || '未选左表' }}）</label>
          <el-select
            v-model="joinLeftKey"
            data-test="inspector-join-left"
            size="small"
            filterable
            allow-create
            placeholder="选择或输入左键"
          >
            <el-option v-for="col in leftCols" :key="col" :value="col" :label="col" />
          </el-select>
        </div>
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">右键（{{ inputs[1] || '未选右表' }}）</label>
          <el-select
            v-model="joinRightKey"
            data-test="inspector-join-right"
            size="small"
            filterable
            allow-create
            placeholder="选择或输入右键"
          >
            <el-option v-for="col in rightCols" :key="col" :value="col" :label="col" />
          </el-select>
        </div>
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">方式</label>
          <el-radio-group v-model="joinMethod" data-test="inspector-join-method" size="small">
            <el-radio value="left">左连接（保留未匹配）</el-radio>
            <el-radio value="inner">内连接（仅匹配）</el-radio>
          </el-radio-group>
        </div>
      </section>

      <!-- split_sections：级别单选（1/2） -->
      <section v-else-if="node.op === 'split_sections'" class="ws-inspector__sec">
        <h4 class="ws-inspector__sec-title">章节级别</h4>
        <el-radio-group v-model="splitLevel" data-test="inspector-split-level" size="small">
          <el-radio :value="1">一级标题</el-radio>
          <el-radio :value="2">二级标题</el-radio>
        </el-radio-group>
        <p class="ws-inspector__hint">输入需含「文档内容_JSON」与「文件」列（kb_dir 底表自带）</p>
      </section>

      <!-- llm_extract：输入列 + 模板 + 输出字段行编辑器 -->
      <section v-else-if="node.op === 'llm_extract'" class="ws-inspector__sec">
        <h4 class="ws-inspector__sec-title">LLM 抽取</h4>
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">输入列</label>
          <el-select
            v-model="llmColumn"
            data-test="inspector-llm-column"
            size="small"
            filterable
            allow-create
            placeholder="选择或输入要抽取的列"
          >
            <el-option v-for="col in availableCols" :key="col" :value="col" :label="col" />
          </el-select>
        </div>
        <div class="ws-inspector__field">
          <label class="ws-inspector__field-label">提示词模板</label>
          <el-input
            v-model="llmPrompt"
            type="textarea"
            :rows="4"
            data-test="inspector-llm-prompt"
            placeholder="每行按此模板抽取，用 {输入} 引用输入列内容"
          />
        </div>
        <div class="ws-inspector__field">
          <div class="ws-inspector__field-head">
            <label class="ws-inspector__field-label">输出字段</label>
            <el-button
              size="small"
              text
              type="primary"
              data-test="inspector-llm-add-field"
              @click="addLlmField"
            >+ 字段</el-button>
          </div>
          <div v-for="(field, i) in llmFields" :key="i" class="ws-inspector__llm-row">
            <el-input
              v-model="field.name"
              :data-test="`inspector-llm-field-name-${i}`"
              size="small"
              placeholder="字段名"
            />
            <el-select v-model="field.type" :data-test="`inspector-llm-field-type-${i}`" size="small">
              <el-option v-for="(label, t) in LLM_TYPE_LABELS" :key="t" :value="t" :label="label" />
            </el-select>
            <el-button
              size="small"
              text
              type="danger"
              :disabled="llmFields.length <= 1"
              :data-test="`inspector-llm-field-del-${i}`"
              @click="removeLlmField(i)"
            >删</el-button>
          </div>
        </div>
      </section>

      <!-- 未知算子兜底（词表外的图——理论上不可达） -->
      <section v-else class="ws-inspector__sec">
        <p class="ws-inspector__warn">未知算子类型：{{ node.op }}</p>
      </section>

      <!-- ── 段三：操作 ── -->
      <footer v-if="canWrite" class="ws-inspector__actions">
        <el-button size="small" data-test="inspector-save" @click="onSave">保存配置</el-button>
        <el-button
          v-if="node.op !== 'source'"
          size="small"
          data-test="inspector-trial"
          :disabled="running"
          @click="emit('trial')"
        >试跑 3 条</el-button>
        <el-button
          type="primary"
          size="small"
          data-test="inspector-execute"
          :disabled="running"
          @click="emit('execute')"
        >{{ running ? '运行中…' : '执行此节点' }}</el-button>
      </footer>
      <p v-else class="ws-inspector__hint ws-inspector__hint--foot">查看模式——配置与执行需要 owner / editor 角色</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { Setting } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { GraphNode, WorkspaceSource } from '@/api/workspaces'

/**
 * 工作台 Inspector（右栏节点配置，67号 Task 12）：三段——节点信息+输入
 * 选择器（按算子元数 0/1/2 渲染；source 语义无输入）→ 按 op 分派参数
 * 表单（config 契约 = workspace_operators.OPERATOR_DEFS 的
 * config_schema）→ 操作（保存配置 / 试跑 / 执行）。
 *
 * 表单为本地草稿（切节点重置），「保存配置」才 emit
 * save(config, inputs, {name, portName})——节点名称与输出表名（MVP 单口
 * 算子只渲染第一口，source 同样可改）也随同一整图 PUT 生效，由父级组装
 * （乐观锁/409 处理在父级）。列候选（portCols 键
 * node:port）由父级从 current outputs 提取，无输出时 select 退化为手输
 * 逗号分隔、其余下拉 allow-create。节点信息段的「删除节点」只 emit
 * delete-node——确认弹窗、整图 PUT 与依赖清理刷新（后端同事务清来源/
 * 当前输出/绑定）都在父级 DetailView。
 */

const props = defineProps<{
  node: GraphNode | null
  /** 无环上游候选（父级已排除自身与全部下游——防环）。 */
  candidates: Array<{ ref: string; label: string }>
  /** 各端口当前输出的列（键 node:port；无输出的端口无键）。 */
  portCols: Record<string, string[]>
  /** source 节点的来源行（op=source 只读展示）。 */
  sourceRow: WorkspaceSource | null
  canWrite: boolean
  running: boolean
}>()

const emit = defineEmits<{
  /** 第三参为重命名载荷：父级组装整图 PUT 时写 node.name 与 ports[0].name。 */
  save: [config: Record<string, unknown>, inputs: string[], names: { name: string; portName: string }]
  trial: []
  execute: []
  /** 删除当前节点（确认弹窗/整图 PUT/依赖刷新都在父级 DetailView）。 */
  'delete-node': []
}>()

// ── 算子元数据（与 workspace_operators.OPERATOR_DEFS 对齐） ──

const OP_META: Record<string, { label: string; arity: number }> = {
  source: { label: '资料来源', arity: 0 },
  select: { label: '选取列', arity: 1 },
  filter: { label: '过滤行', arity: 1 },
  join: { label: '关联', arity: 2 },
  split_sections: { label: '章节切分', arity: 1 },
  llm_extract: { label: 'LLM 抽取', arity: 1 },
}

const opMeta = computed(() => OP_META[props.node?.op ?? ''] ?? { label: props.node?.op ?? '', arity: 0 })
const arity = computed(() => opMeta.value.arity)

const FILTER_COND_LABELS: Record<string, string> = {
  eq: '等于', ne: '不等于', gt: '大于', gte: '大于等于',
  lt: '小于', lte: '小于等于', contains: '包含',
  is_empty: '为空', not_empty: '非空',
}
/** 需要携带值的条件（is_empty/not_empty 隐藏值输入）。 */
const VALUE_CONDS = new Set(['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'contains'])
const NUMERIC_CONDS = new Set(['gt', 'gte', 'lt', 'lte'])

const LLM_TYPE_LABELS: Record<string, string> = {
  text: '文本', int: '整数', float: '小数', date: '日期', enum: '枚举', bool: '布尔',
}

/** 节点名称/输出表名长度上限（与后端 put_graph 非空名校验对齐，超限截断）。 */
const NAME_MAX = 100

// ── 草稿（切节点重置；由 node.config/inputs 注入默认值） ──

const nodeName = ref('')
const portName = ref('')
const inputs = ref<string[]>([])
const selectPicked = ref<string[]>([])
const selectRenames = reactive<Record<string, string>>({})
const selectManual = ref('')
const filterField = ref('')
const filterCond = ref('eq')
const filterValue = ref('')
const joinLeftKey = ref('')
const joinRightKey = ref('')
const joinMethod = ref<'left' | 'inner'>('left')
const splitLevel = ref<1 | 2>(1)
const llmColumn = ref('')
const llmPrompt = ref('')
const llmFields = ref<Array<{ name: string; type: string }>>([])

function hydrateDrafts(): void {
  const node = props.node
  nodeName.value = ''
  portName.value = ''
  selectPicked.value = []
  Object.keys(selectRenames).forEach(k => { delete selectRenames[k] })
  selectManual.value = ''
  filterField.value = ''
  filterCond.value = 'eq'
  filterValue.value = ''
  joinLeftKey.value = ''
  joinRightKey.value = ''
  joinMethod.value = 'left'
  splitLevel.value = 1
  llmColumn.value = ''
  llmPrompt.value = ''
  llmFields.value = [{ name: '', type: 'text' }]
  if (!node) { inputs.value = []; return }

  nodeName.value = node.name
  portName.value = node.ports[0]?.name ?? ''
  inputs.value = [...node.inputs]
  const config = node.config ?? {}
  const asString = (v: unknown): string => (typeof v === 'string' ? v : '')
  switch (node.op) {
    case 'select': {
      const columns = Array.isArray(config.columns) ? config.columns : []
      selectPicked.value = columns
        .map(c => (typeof c === 'object' && c !== null ? c : {}))
        .map(c => asString((c as Record<string, unknown>).id))
        .filter(Boolean)
      for (const c of columns) {
        if (typeof c === 'object' && c !== null) {
          const { id, name } = c as { id?: unknown; name?: unknown }
          if (typeof id === 'string' && typeof name === 'string' && name && name !== id) {
            selectRenames[id] = name
          }
        }
      }
      break
    }
    case 'filter':
      filterField.value = asString(config.field)
      filterCond.value = asString(config.cond) || 'eq'
      filterValue.value = config.value == null ? '' : String(config.value)
      break
    case 'join':
      joinLeftKey.value = asString(config.left_key)
      joinRightKey.value = asString(config.right_key)
      joinMethod.value = config.method === 'inner' ? 'inner' : 'left'
      break
    case 'split_sections':
      splitLevel.value = config.level === 2 ? 2 : 1
      break
    case 'llm_extract': {
      llmColumn.value = asString(config.input_column)
      llmPrompt.value = asString(config.prompt_template)
      const fields = Array.isArray(config.output_fields) ? config.output_fields : []
      const parsed = fields
        .map(f => (typeof f === 'object' && f !== null ? f : {}))
        .map(f => ({
          name: asString((f as Record<string, unknown>).name),
          type: asString((f as Record<string, unknown>).type) || 'text',
        }))
        .filter(f => f.name)
      llmFields.value = parsed.length ? parsed : [{ name: '', type: 'text' }]
      break
    }
    default:
      break
  }
}

watch(() => props.node?.id, hydrateDrafts, { immediate: true })

// ── 列候选（当前输入的输出列；join 左右各自） ──

const availableCols = computed<string[]>(() => {
  const ref = inputs.value[0]
  return ref ? (props.portCols[ref] ?? []) : []
})
const leftCols = availableCols
const rightCols = computed<string[]>(() => {
  const ref = inputs.value[1]
  return ref ? (props.portCols[ref] ?? []) : []
})

const needsFilterValue = computed(() => VALUE_CONDS.has(filterCond.value))
const numericCond = computed(() => NUMERIC_CONDS.has(filterCond.value))

// ── source 只读展示 ──

const dirList = computed<string[]>(() => {
  const dirs = props.sourceRow?.config?.directories
  return Array.isArray(dirs) ? dirs.map(String) : []
})

const frozenText = computed(() => {
  const manifest = (props.sourceRow?.input_manifest ?? null) as Record<string, unknown> | null
  if (!manifest) return '—'
  if (props.sourceRow?.connector === 'kb_dir') {
    const docs = Number(manifest.doc_count ?? 0)
    const unparsed = Array.isArray(manifest.unparsed_docs) ? manifest.unparsed_docs.length : 0
    return unparsed > 0 ? `已冻结 ${docs} 份文档（${unparsed} 份未解析）` : `已冻结 ${docs} 份文档`
  }
  const rows = Number(manifest.row_count ?? 0)
  const sheet = typeof manifest.sheet_name === 'string' ? manifest.sheet_name : ''
  return sheet ? `已冻结 ${rows} 行（${sheet}）` : `已冻结 ${rows} 行`
})

// ── 输出字段行编辑器（≥1 行） ──

function addLlmField(): void {
  llmFields.value = [...llmFields.value, { name: '', type: 'text' }]
}

function removeLlmField(index: number): void {
  if (llmFields.value.length <= 1) return
  llmFields.value = llmFields.value.filter((_, i) => i !== index)
}

// ── 保存（组装 config → emit save；校验失败就地提示不 emit） ──

function buildConfig(): Record<string, unknown> | null {
  const op = props.node?.op
  if (op === 'source') return {}
  if (op === 'select') {
    const columns = availableCols.value.length
      ? selectPicked.value
        .filter(id => availableCols.value.includes(id))
        .map(id => ({ id, name: selectRenames[id]?.trim() || id }))
      : selectManual.value.split(/[，,]/).map(s => s.trim()).filter(Boolean).map(id => ({ id, name: id }))
    if (!columns.length) { ElMessage.warning('请至少选择一个输出列'); return null }
    return { columns }
  }
  if (op === 'filter') {
    if (!filterField.value.trim()) { ElMessage.warning('请选择筛选字段'); return null }
    const config: Record<string, unknown> = { field: filterField.value.trim(), cond: filterCond.value }
    if (needsFilterValue.value) config.value = filterValue.value
    return config
  }
  if (op === 'join') {
    if (!joinLeftKey.value.trim() || !joinRightKey.value.trim()) {
      ElMessage.warning('请选择左右关联键'); return null
    }
    return {
      method: joinMethod.value,
      left_key: joinLeftKey.value.trim(),
      right_key: joinRightKey.value.trim(),
    }
  }
  if (op === 'split_sections') return { level: splitLevel.value }
  if (op === 'llm_extract') {
    if (!llmColumn.value.trim()) { ElMessage.warning('请选择 LLM 抽取的输入列'); return null }
    if (!llmPrompt.value.trim()) { ElMessage.warning('请填写提示词模板'); return null }
    const outputFields = llmFields.value
      .map(f => ({ name: f.name.trim(), type: f.type }))
      .filter(f => f.name)
    if (!outputFields.length) { ElMessage.warning('请至少保留一个输出字段'); return null }
    const config: Record<string, unknown> = {
      input_column: llmColumn.value.trim(),
      prompt_template: llmPrompt.value,
      output_fields: outputFields,
    }
    // model 为表单外字段：保留既有值（词表外键随保存丢散会静默换模型）
    const model = props.node?.config?.model
    if (typeof model === 'string' && model.trim()) config.model = model.trim()
    return config
  }
  return {}
}

/** 节点名称/输出表名校验（非空 trim 后 1..100；失败就地提示不 emit）。 */
function validateNames(): { name: string; portName: string } | null {
  const name = nodeName.value.trim()
  const port = portName.value.trim()
  if (!name) { ElMessage.warning('节点名称不能为空'); return null }
  if (!port) { ElMessage.warning('输出表名不能为空'); return null }
  if (name.length > NAME_MAX || port.length > NAME_MAX) {
    ElMessage.warning(`名称不能超过 ${NAME_MAX} 字`); return null
  }
  return { name, portName: port }
}

function onSave(): void {
  const names = validateNames()
  if (names === null) return
  const config = buildConfig()
  if (config === null) return
  emit('save', config, [...inputs.value], names)
}
</script>

<style scoped>
.ws-inspector {
  height: 100%;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--kb-border-light, #f1f5f9);
  border-radius: 10px;
  background: var(--kb-bg-card, #fff);
  padding: 12px 12px 10px;
  overflow: auto;
}
.ws-inspector__empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--kb-text-tertiary, #94a3b8);
  text-align: center;
}
.ws-inspector__empty-icon { opacity: .6; }
.ws-inspector__empty-text { margin: 0; font-size: 12px; }

/* 段一：节点信息 */
.ws-inspector__head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--kb-border-light, #f1f5f9);
}
.ws-inspector__head-title {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}
.ws-inspector__node-name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--kb-text-primary, #0f172a);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-inspector__op-label {
  font-size: 11px;
  color: var(--kb-text-tertiary, #94a3b8);
}
.ws-inspector__badge {
  flex-shrink: 0;
  border-radius: 4px;
  padding: 1px 8px;
  font-size: 11px;
  font-weight: 500;
}
.ws-inspector__badge.is-pending {
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.1));
  color: var(--kb-accent, #0891b2);
}
.ws-inspector__badge.is-stale {
  background: var(--kb-warning-soft, rgba(245, 158, 11, 0.12));
  color: var(--kb-warning, #f59e0b);
}
.ws-inspector__delete {
  flex-shrink: 0;
  margin-left: auto;
  height: auto;
  padding: 2px 0;
}

/* 通用节 */
.ws-inspector__sec {
  padding: 12px 0 2px;
  border-bottom: 1px dashed var(--kb-border-light, #f1f5f9);
}
/* 命名双字段：紧接标题栏（自带实线分隔），无节标题 */
.ws-inspector__identity { padding-top: 10px; }
.ws-inspector__sec-title {
  margin: 0 0 10px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .04em;
  color: var(--kb-text-tertiary, #94a3b8);
  text-transform: uppercase;
}
.ws-inspector__field { margin-bottom: 10px; }
.ws-inspector__field:last-child { margin-bottom: 0; }
.ws-inspector__field-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.ws-inspector__field-label {
  display: block;
  margin-bottom: 5px;
  font-size: 11.5px;
  color: var(--kb-text-secondary, #64748b);
}
.ws-inspector__field-head .ws-inspector__field-label { margin-bottom: 0; }
.ws-inspector__select { width: 100%; }
.ws-inspector__hint {
  margin: 8px 0 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--kb-text-tertiary, #94a3b8);
}
.ws-inspector__hint--foot { margin: 10px 0 0; }
.ws-inspector__warn {
  margin: 0;
  font-size: 12px;
  color: var(--kb-warning, #f59e0b);
}

/* source 只读 kv */
.ws-inspector__kv {
  margin: 0;
  display: grid;
  grid-template-columns: 64px 1fr;
  row-gap: 7px;
  column-gap: 10px;
  font-size: 12px;
}
.ws-inspector__kv dt { color: var(--kb-text-tertiary, #94a3b8); }
.ws-inspector__kv dd {
  margin: 0;
  color: var(--kb-text-primary, inherit);
  overflow-wrap: anywhere;
}
.ws-inspector__dirs { display: flex; flex-wrap: wrap; gap: 4px; }
.ws-inspector__muted { color: var(--kb-text-tertiary, #94a3b8); }

/* select 列表 */
.ws-inspector__cols { display: flex; flex-direction: column; gap: 6px; width: 100%; }
.ws-inspector__col-row {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.ws-inspector__col-row .el-checkbox { flex-shrink: 0; max-width: 55%; }
.ws-inspector__rename { flex: 1; min-width: 0; }

/* llm 字段行 */
.ws-inspector__llm-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.ws-inspector__llm-row .el-select { flex: 0 0 92px; }

/* 操作区 */
.ws-inspector__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 12px;
}
.ws-inspector__actions .el-button + .el-button { margin-left: 0; }
</style>
