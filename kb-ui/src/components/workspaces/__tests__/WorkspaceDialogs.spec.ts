import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { defineComponent } from 'vue'
import type { PropType } from 'vue'

/**
 * 轻量 spec：接入资料对话框（SourceDialog）与添加算子对话框（NodeDialog）
 * ——关键契约：src_<n> 生成+source 节点先入图再落 sources 行、409 语义、
 * 算子确认载荷（inputs 默认当前选中 ref，join 双表必选）。
 */

const api = vi.hoisted(() => ({
  putGraph: vi.fn(),
  upsertSourceKbDir: vi.fn(),
  uploadSourceExcel: vi.fn(),
}))
const kbApi = vi.hoisted(() => ({ listKbs: vi.fn() }))

vi.mock('@/api/workspaces', () => ({
  useWorkspacesApi: () => api,
}))
vi.mock('@/api/kb', () => ({ useKbApi: () => kbApi }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => '错误详情' }))

import WorkspaceSourceDialog from '../WorkspaceSourceDialog.vue'
import WorkspaceNodeDialog from '../WorkspaceNodeDialog.vue'
import type { GraphNode } from '@/api/workspaces'

// ── 测试观察层 stub ──

const ElDialogStub = {
  name: 'ElDialog',
  props: ['modelValue', 'title', 'width'],
  emits: ['update:modelValue'],
  template: '<div v-if="modelValue" class="el-dialog-stub"><slot /><slot name="footer" /></div>',
}

const ElTabsStub = defineComponent({
  name: 'ElTabs',
  props: { modelValue: { type: String, default: '' } },
  emits: ['update:modelValue'],
  provide() {
    return {
      stubTabsCtx: {
        current: (): string => this.modelValue as string,
        select: (v: string): void => { this.$emit('update:modelValue', v) },
      },
    }
  },
  template: '<div class="el-tabs-stub"><slot /></div>',
})

const ElTabPaneStub = defineComponent({
  name: 'ElTabPane',
  props: ['label', 'name'],
  inject: { stubTabsCtx: { default: null } },
  template: `
    <div class="el-tab-pane-stub">
      <button type="button" class="stub-tab-head" :data-test="'tab-' + name" @click="stubTabsCtx?.select(name)">{{ label }}</button>
      <section v-show="stubTabsCtx?.current() === name"><slot /></section>
    </div>`,
})

const ElSelectStub = defineComponent({
  name: 'ElSelect',
  props: { modelValue: { type: String, default: '' } },
  emits: ['update:modelValue'],
  template: `
    <div class="el-select-stub" :data-value="modelValue">
      <input class="el-select-stub__input" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />
      <slot />
    </div>`,
})

const ElOptionStub = {
  name: 'ElOption',
  props: ['value', 'label'],
  template: '<option class="el-option-stub" :value="value">{{ label ?? value }}</option>',
}

const ElInputStub = defineComponent({
  name: 'ElInput',
  props: { modelValue: { type: String, default: '' }, placeholder: { type: String, default: '' }, disabled: Boolean },
  emits: ['update:modelValue'],
  template: `
    <input class="el-input-stub" :placeholder="placeholder" :disabled="disabled" :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)" />`,
})

const ElInputTagStub = defineComponent({
  name: 'ElInputTag',
  props: { modelValue: { type: Array as PropType<string[]>, default: () => [] } },
  emits: ['update:modelValue'],
  template: `
    <input class="el-input-tag-stub"
      @keydown.enter.prevent="$emit('update:modelValue', [...modelValue, String($event.target.value).trim()].filter(Boolean))" />`,
})

// el-upload 的 on-change 是 prop（EP 回调口径——非事件），stub 同构承接
const ElUploadStub = defineComponent({
  name: 'ElUpload',
  props: {
    accept: String,
    limit: Number,
    autoUpload: Boolean,
    drag: Boolean,
    onChange: {
      type: Function as PropType<(file: { name: string; raw: File | null }) => void>,
      default: null,
    },
  },
  methods: {
    onPick(event: Event): void {
      const files = (event.target as HTMLInputElement).files
      const raw = files?.[0] ?? null
      this.onChange?.({ name: raw?.name ?? '', raw })
    },
  },
  template: `
    <div class="el-upload-stub">
      <input type="file" data-test="excel-file-input" @change="onPick" />
      <slot />
    </div>`,
})

const ElFormStub = { name: 'ElForm', template: '<form class="el-form-stub"><slot /></form>' }
const ElFormItemStub = { name: 'ElFormItem', props: ['label', 'required'], template: '<div class="el-form-item-stub"><slot /></div>' }
const ElButtonStub = {
  name: 'ElButton',
  props: ['disabled', 'type', 'size', 'text', 'loading'],
  emits: ['click'],
  template: '<button class="el-button-stub" :disabled="disabled || loading" @click="$emit(\'click\')"><slot /></button>',
}

const stubs = {
  ElDialog: ElDialogStub,
  ElTabs: ElTabsStub,
  ElTabPane: ElTabPaneStub,
  ElSelect: ElSelectStub,
  ElOption: ElOptionStub,
  ElInput: ElInputStub,
  ElInputTag: ElInputTagStub,
  ElUpload: ElUploadStub,
  ElForm: ElFormStub,
  ElFormItem: ElFormItemStub,
  ElButton: ElButtonStub,
  ElIcon: { name: 'ElIcon', template: '<i class="el-icon-stub"><slot /></i>' },
}

// ── 工具 ──

function srcNode(id: string): GraphNode {
  return {
    id, op: 'source', name: `资料 ${id}`, config: {}, inputs: [],
    ports: [{ id: 'main', name: '底表', kind: 'table' }], x: 0, y: 0,
  }
}

function mountSource(nodes: GraphNode[] = [], revision = 3): VueWrapper {
  return mount(WorkspaceSourceDialog, {
    props: {
      modelValue: true, workspaceId: 'wsp_1', domain: 'plant/a',
      nodes, graphRevision: revision,
    },
    global: { stubs },
  })
}

function setSel(wrapper: VueWrapper, testId: string, value: string): Promise<void> {
  return wrapper.get(`[data-test="${testId}"]`).find('input').setValue(value)
}

beforeEach(() => {
  vi.clearAllMocks()
  kbApi.listKbs.mockResolvedValue([
    { id: 'kb-net', name: '网络规范库' },
    { id: 'kb-ops', name: '运维手册库' },
  ])
  api.putGraph.mockResolvedValue({ graph: { nodes: [] }, graph_revision: 4, batch_no: 1 })
  api.upsertSourceKbDir.mockResolvedValue({
    id: 'row-1', node_id: 'src_2', connector: 'kb_dir', config: {},
    last_read_at: null, created_by: 'u1', created_at: '', updated_at: '',
    frozen: { doc_count: 12, unparsed_count: 0 }, batch_no: 2,
  })
  api.uploadSourceExcel.mockResolvedValue({
    id: 'row-2', node_id: 'src_2', connector: 'excel', config: {},
    last_read_at: null, created_by: 'u1', created_at: '', updated_at: '',
    frozen: { row_count: 8, sheet_name: 'Sheet1' }, batch_no: 2,
  })
})

describe('WorkspaceSourceDialog', () => {
  it('loads domain KB candidates on open and blocks submit without a KB', async () => {
    const wrapper = mountSource()
    expect(kbApi.listKbs).toHaveBeenCalledWith('plant/a')

    await wrapper.get('[data-test="source-submit"]').trigger('click')
    expect(api.putGraph).not.toHaveBeenCalled()
    expect(wrapper.emitted('created')).toBeUndefined()
  })

  it('kb_dir flow: appends the source node to the graph (PUT) before freezing the source row, then emits created', async () => {
    const existing = [srcNode('src_1')]
    const wrapper = mountSource(existing, 3)

    await setSel(wrapper, 'source-kb', 'kb-net')
    await wrapper.get('[data-test="source-submit"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.emitted('created')).toHaveLength(1))

    // ① 整图替换：原节点 + 新 source 节点（src_<n> 不冲突 / inputs 空 / 单 main 端口）
    expect(api.putGraph).toHaveBeenCalledTimes(1)
    const [wsId, graph, revision] = api.putGraph.mock.calls[0]!
    expect(wsId).toBe('wsp_1')
    expect(revision).toBe(3)
    expect(graph.nodes).toHaveLength(2)
    const added = graph.nodes[1] as GraphNode
    expect(added.id).toBe('src_2')
    expect(added.op).toBe('source')
    expect(added.inputs).toEqual([])
    expect(added.ports).toEqual([{ id: 'main', name: '底表', kind: 'table' }])
    // ② 落 sources 行：同一 node_id + kb + 目录
    expect(api.upsertSourceKbDir).toHaveBeenCalledWith('wsp_1', {
      node_id: 'src_2', kb_id: 'kb-net', directories: [],
    })
    expect(wrapper.emitted('created')).toEqual([['src_2']])
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('passes trimmed directory tags through to the source row', async () => {
    const wrapper = mountSource()
    await setSel(wrapper, 'source-kb', 'kb-ops')
    const tagInput = wrapper.get('.el-input-tag-stub')
    await tagInput.setValue('docs/5g')
    await tagInput.trigger('keydown.enter')

    await wrapper.get('[data-test="source-submit"]').trigger('click')
    await vi.waitFor(() => expect(api.upsertSourceKbDir).toHaveBeenCalled())
    expect(api.upsertSourceKbDir.mock.calls[0]![1].directories).toEqual(['docs/5g'])
  })

  it('excel flow: uploads the picked .xlsx with optional sheet name on the excel tab', async () => {
    const wrapper = mountSource()

    await wrapper.get('[data-test="tab-excel"]').trigger('click')
    const file = new File(['x'], '库存.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    const picker = wrapper.get('[data-test="excel-file-input"]')
    Object.defineProperty(picker.element, 'files', { value: [file] })
    await picker.trigger('change')
    await wrapper.get('[data-test="source-sheet"]').setValue('Sheet2')

    await wrapper.get('[data-test="source-submit"]').trigger('click')
    await vi.waitFor(() => expect(api.uploadSourceExcel).toHaveBeenCalled())
    expect(api.uploadSourceExcel).toHaveBeenCalledWith('wsp_1', {
      node_id: 'src_1', file, sheet_name: 'Sheet2',
    })
    expect(wrapper.emitted('created')).toEqual([['src_1']])
  })

  it('maps a graph 409 to graph-conflict (dialog stays open, no source row written)', async () => {
    const error = Object.assign(new Error('conflict'), { response: { status: 409 } })
    api.putGraph.mockRejectedValue(error)
    const wrapper = mountSource()

    await setSel(wrapper, 'source-kb', 'kb-net')
    await wrapper.get('[data-test="source-submit"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.emitted('graph-conflict')).toHaveLength(1))

    expect(api.upsertSourceKbDir).not.toHaveBeenCalled()
    expect(wrapper.emitted('update:modelValue')?.at(-1)).not.toEqual([false])
  })
})

describe('WorkspaceNodeDialog', () => {
  const PORT_OPTIONS = [
    { ref: 'src_1:main', label: '产品资料 · 底表' },
    { ref: 'op_1:main', label: '筛功率 · 筛选结果' },
  ]

  function mountNode(): VueWrapper {
    return mount(WorkspaceNodeDialog, {
      props: {
        modelValue: true, portOptions: PORT_OPTIONS, defaultRef: 'src_1:main',
      },
      global: { stubs },
    })
  }

  it('renders the five operator cards', () => {
    const wrapper = mountNode()
    for (const op of ['select', 'filter', 'join', 'split_sections', 'llm_extract']) {
      expect(wrapper.find(`[data-test="node-card-${op}"]`).exists()).toBe(true)
    }
    // 未选算子时确定禁用
    expect((wrapper.get('[data-test="node-submit"]').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('confirms an arity-1 op with inputs defaulting to the current selection', async () => {
    const wrapper = mountNode()
    await wrapper.get('[data-test="node-card-filter"]').trigger('click')
    await wrapper.get('[data-test="node-submit"]').trigger('click')
    expect(wrapper.emitted('confirm')).toEqual([[{ op: 'filter', inputs: ['src_1:main'] }]])
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([false])
  })

  it('join requires both tables; left defaults to the current selection', async () => {
    const wrapper = mountNode()
    await wrapper.get('[data-test="node-card-join"]').trigger('click')
    expect(wrapper.find('[data-test="node-join-left"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="node-join-right"]').exists()).toBe(true)
    // 左表默认=当前选中；右表未选 → 确定禁用
    expect(wrapper.get('[data-test="node-join-left"]').attributes('data-value')).toBe('src_1:main')
    expect((wrapper.get('[data-test="node-submit"]').element as HTMLButtonElement).disabled).toBe(true)

    await setSel(wrapper, 'node-join-right', 'op_1:main')
    expect((wrapper.get('[data-test="node-submit"]').element as HTMLButtonElement).disabled).toBe(false)
    await wrapper.get('[data-test="node-submit"]').trigger('click')
    expect(wrapper.emitted('confirm')).toEqual([[{
      op: 'join', inputs: ['src_1:main', 'op_1:main'],
    }]])
  })

  it('resets the pick when reopened', async () => {
    const wrapper = mountNode()
    await wrapper.get('[data-test="node-card-select"]').trigger('click')
    await wrapper.setProps({ modelValue: false })
    await wrapper.setProps({ modelValue: true })
    expect(wrapper.find('[data-test="node-join-left"]').exists()).toBe(false)
    expect((wrapper.get('[data-test="node-submit"]').element as HTMLButtonElement).disabled).toBe(true)
  })
})
