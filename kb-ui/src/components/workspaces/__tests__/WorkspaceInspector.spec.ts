import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { defineComponent } from 'vue'
import type { PropType } from 'vue'

import WorkspaceInspector from '../WorkspaceInspector.vue'
import type { GraphNode, WorkspaceSource } from '@/api/workspaces'

/**
 * Inspector（右栏节点配置）：按 op 分派表单 / 草稿默认值 / save 组装
 * config 契约（第三参 {name, portName}——节点名与输出表名随同一整图
 * PUT 生效）/ trial·execute emit / source 只读展示。mount + 本地
 * el-* 表单 stub（select/option/input/checkbox/radio/button/tag——原生
 * 控件承接 v-model，setValue/trigger 直驱）。
 */

// ── 测试观察层 stub ──

// select stub 用 input 承值（原生 select 在值无匹配 option 时会归空——
// allow-create 场景值可以不在候选里）；data-value 暴露给断言。
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
  props: {
    modelValue: { type: String, default: '' },
    type: { type: String, default: 'text' },
    placeholder: { type: String, default: '' },
  },
  emits: ['update:modelValue'],
  computed: {
    tag(): string { return this.type === 'textarea' ? 'textarea' : 'input' },
  },
  template: `
    <component
      :is="tag"
      class="el-input-stub"
      :placeholder="placeholder"
      :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)"
    />`,
})

const ElCheckboxGroupStub = defineComponent({
  name: 'ElCheckboxGroup',
  props: { modelValue: { type: Array as PropType<unknown[]>, default: () => [] } },
  emits: ['update:modelValue'],
  provide() {
    return {
      stubCheckboxGroup: {
        values: (): unknown[] => this.modelValue as unknown[],
        toggle: (v: unknown): void => {
          const cur = this.modelValue as unknown[]
          this.$emit('update:modelValue',
            cur.includes(v) ? cur.filter(x => x !== v) : [...cur, v])
        },
      },
    }
  },
  template: '<div class="el-checkbox-group-stub"><slot /></div>',
})

const ElCheckboxStub = defineComponent({
  name: 'ElCheckbox',
  props: ['value', 'label'],
  inject: { stubCheckboxGroup: { default: null } },
  computed: {
    group(): { values: () => unknown[]; toggle: (v: unknown) => void } | null {
      return this.stubCheckboxGroup as { values: () => unknown[]; toggle: (v: unknown) => void } | null
    },
    isChecked(): boolean {
      return !!this.group?.values().includes(this.value)
    },
  },
  template: `
    <label class="el-checkbox-stub">
      <input type="checkbox" :checked="isChecked" @change="group?.toggle(value)" />
      <slot>{{ label ?? value }}</slot>
    </label>`,
})

const ElRadioGroupStub = defineComponent({
  name: 'ElRadioGroup',
  props: { modelValue: { type: [String, Number], default: '' } },
  emits: ['update:modelValue'],
  provide() {
    return {
      stubRadioGroup: {
        current: (): unknown => this.modelValue,
        set: (v: unknown): void => { this.$emit('update:modelValue', v) },
      },
    }
  },
  template: '<div class="el-radio-group-stub"><slot /></div>',
})

const ElRadioStub = defineComponent({
  name: 'ElRadio',
  props: ['value', 'label'],
  inject: { stubRadioGroup: { default: null } },
  template: `
    <label class="el-radio-stub">
      <input type="radio" :checked="stubRadioGroup?.current?.() === value" :value="value" @change="stubRadioGroup?.set(value)" />
      <slot>{{ label ?? value }}</slot>
    </label>`,
})

const ElButtonStub = {
  name: 'ElButton',
  props: ['disabled', 'type', 'size', 'text', 'loading'],
  emits: ['click'],
  template: '<button class="el-button-stub" :disabled="disabled" @click="$emit(\'click\')"><slot /></button>',
}

const stubs = {
  ElSelect: ElSelectStub,
  ElOption: ElOptionStub,
  ElInput: ElInputStub,
  ElCheckboxGroup: ElCheckboxGroupStub,
  ElCheckbox: ElCheckboxStub,
  ElRadioGroup: ElRadioGroupStub,
  ElRadio: ElRadioStub,
  ElButton: ElButtonStub,
  ElTag: { name: 'ElTag', props: ['size', 'effect'], template: '<span class="el-tag-stub"><slot /></span>' },
}

// ── 工具 ──

function node(partial: Partial<GraphNode> & Pick<GraphNode, 'id' | 'op' | 'name'>): GraphNode {
  return {
    config: {},
    inputs: [],
    ports: [{ id: 'main', name: '主输出', kind: 'table' }],
    x: 0,
    y: 0,
    ...partial,
  }
}

function sourceRow(partial: Partial<WorkspaceSource> = {}): WorkspaceSource {
  return {
    id: 'src-row-1', workspace_id: 'ws-1', node_id: 'src1', connector: 'kb_dir',
    config: { kb_id: 'kb-net', directories: ['docs/5g'] },
    input_manifest: { doc_count: 12, unparsed_docs: [] },
    last_read_at: null, created_by: 'u1',
    created_at: '2026-11-16T08:00:00+00:00', updated_at: '2026-11-16T08:00:00+00:00',
    ...partial,
  }
}

function mountInspector(options: {
  node?: GraphNode | null
  candidates?: Array<{ ref: string; label: string }>
  portCols?: Record<string, string[]>
  sourceRow?: WorkspaceSource | null
  canWrite?: boolean
  running?: boolean
} = {}): VueWrapper {
  return mount(WorkspaceInspector, {
    props: {
      node: options.node ?? null,
      candidates: options.candidates ?? [],
      portCols: options.portCols ?? {},
      sourceRow: options.sourceRow ?? null,
      canWrite: options.canWrite ?? true,
      running: options.running ?? false,
    },
    global: { stubs },
  })
}

const CANDIDATES = [
  { ref: 'src1:main', label: '产品资料 · 主输出' },
  { ref: 'src2:main', label: '库存表 · 主输出' },
]

/** el-select stub 读值（data-value）与驱动（内嵌 input setValue）。 */
function selValue(wrapper: VueWrapper, testId: string): string {
  return wrapper.get(`[data-test="${testId}"]`).attributes('data-value') ?? ''
}

async function setSel(wrapper: VueWrapper, testId: string, value: string): Promise<void> {
  await wrapper.get(`[data-test="${testId}"]`).find('input').setValue(value)
}

describe('WorkspaceInspector', () => {
  it('shows the empty hint when no node is selected', () => {
    const wrapper = mountInspector()
    expect(wrapper.get('[data-test="inspector"]').text()).toContain('选择一个节点')
    expect(wrapper.find('[data-test="inspector-save"]').exists()).toBe(false)
  })

  it('renders zero input selectors for source and shows the frozen read-only summary', () => {
    const wrapper = mountInspector({
      node: node({ id: 'src1', op: 'source', name: '产品资料' }),
      sourceRow: sourceRow(),
    })
    expect(wrapper.find('[data-test="inspector-input-0"]').exists()).toBe(false)
    const text = wrapper.get('[data-test="inspector"]').text()
    expect(text).toContain('知识库目录')
    expect(text).toContain('kb-net')
    expect(text).toContain('docs/5g')
    expect(wrapper.get('[data-test="inspector-source-frozen"]').text()).toBe('已冻结 12 份文档')
    // source 无试跑（后端拒绝 source 节点试跑）
    expect(wrapper.find('[data-test="inspector-trial"]').exists()).toBe(false)
  })

  it('warns when a source node has no connected source row', () => {
    const wrapper = mountInspector({
      node: node({ id: 'src1', op: 'source', name: '产品资料' }),
      sourceRow: null,
    })
    expect(wrapper.get('[data-test="inspector-source-missing"]').text()).toContain('未接入来源')
  })

  it('renders one input selector for arity-1 ops and two for join', () => {
    const single = mountInspector({
      node: node({ id: 'f1', op: 'filter', name: '筛功率' }), candidates: CANDIDATES,
    })
    expect(single.find('[data-test="inspector-input-0"]').exists()).toBe(true)
    expect(single.find('[data-test="inspector-input-1"]').exists()).toBe(false)

    const join = mountInspector({
      node: node({ id: 'j1', op: 'join', name: '关联库存' }), candidates: CANDIDATES,
    })
    expect(join.findAll('.el-select-stub').length).toBeGreaterThanOrEqual(2)
    expect(join.find('[data-test="inspector-input-1"]').exists()).toBe(true)
    expect(join.text()).toContain('左表')
    expect(join.text()).toContain('右表')
  })

  it('hydrates drafts from node.config/inputs and emits save with the same contract', async () => {
    const wrapper = mountInspector({
      node: node({
        id: 'f1', op: 'filter', name: '筛功率',
        inputs: ['src1:main'],
        config: { field: '功率_W', cond: 'gte', value: 2000 },
      }),
      candidates: CANDIDATES,
      portCols: { 'src1:main': ['型号', '功率_W'] },
    })

    expect(selValue(wrapper, 'inspector-input-0')).toBe('src1:main')
    expect(selValue(wrapper, 'inspector-filter-field')).toBe('功率_W')
    expect(selValue(wrapper, 'inspector-filter-cond')).toBe('gte')
    expect((wrapper.get('[data-test="inspector-filter-value"]').element as HTMLInputElement).value).toBe('2000')

    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    expect(wrapper.emitted('save')).toEqual([[{ field: '功率_W', cond: 'gte', value: '2000' }, ['src1:main'], { name: '筛功率', portName: '主输出' }]])
  })

  // ── 节点改名+输出表名改名（用户测试反馈修复3） ──

  it('renders node name and port name inputs hydrated from the node', () => {
    const wrapper = mountInspector({
      node: node({
        id: 'f1', op: 'filter', name: '筛功率',
        ports: [{ id: 'main', name: '筛选结果', kind: 'table' }],
      }),
    })
    expect((wrapper.get('[data-test="inspector-node-name"]').element as HTMLInputElement).value).toBe('筛功率')
    expect((wrapper.get('[data-test="inspector-port-name"]').element as HTMLInputElement).value).toBe('筛选结果')
  })

  it('emits save with edited node name and port name in the third payload slot', async () => {
    const wrapper = mountInspector({
      node: node({
        id: 'f1', op: 'filter', name: '筛功率',
        inputs: ['src1:main'],
        config: { field: '功率_W', cond: 'gte', value: 2000 },
        ports: [{ id: 'main', name: '筛选结果', kind: 'table' }],
      }),
      candidates: CANDIDATES,
    })

    await wrapper.get('[data-test="inspector-node-name"]').setValue('大功率筛选')
    await wrapper.get('[data-test="inspector-port-name"]').setValue('高功率清单')

    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    expect(wrapper.emitted('save')).toEqual([[{
      field: '功率_W', cond: 'gte', value: '2000',
    }, ['src1:main'], { name: '大功率筛选', portName: '高功率清单' }]])
  })

  it('blocks save with a warning when node name or port name is emptied', async () => {
    const wrapper = mountInspector({
      node: node({
        id: 'f1', op: 'filter', name: '筛功率',
        inputs: ['src1:main'],
        config: { field: '功率_W', cond: 'gte', value: 2000 },
      }),
      candidates: CANDIDATES,
    })

    // 纯空白 = 空（trim 后校验）
    await wrapper.get('[data-test="inspector-node-name"]').setValue('   ')
    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    expect(wrapper.emitted('save')).toBeUndefined()

    await wrapper.get('[data-test="inspector-node-name"]').setValue('筛功率')
    await wrapper.get('[data-test="inspector-port-name"]').setValue('')
    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    expect(wrapper.emitted('save')).toBeUndefined()

    // 名称回填后放行（其余契约不变）
    await wrapper.get('[data-test="inspector-port-name"]').setValue('主输出')
    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    expect(wrapper.emitted('save')).toHaveLength(1)
  })

  it('hides the filter value input for is_empty / not_empty and drops value on save', async () => {
    const wrapper = mountInspector({
      node: node({ id: 'f1', op: 'filter', name: '空值筛', inputs: ['src1:main'], config: {} }),
      candidates: CANDIDATES,
      portCols: { 'src1:main': ['备注'] },
    })

    await setSel(wrapper, 'inspector-filter-cond', 'is_empty')
    expect(wrapper.find('[data-test="inspector-filter-value"]').exists()).toBe(false)

    await setSel(wrapper, 'inspector-filter-field', '备注')
    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    // is_empty 不携带 value 键（后端 VALUE_CONDS 之外不校验值）
    expect(wrapper.emitted('save')).toEqual([[{ field: '备注', cond: 'is_empty' }, ['src1:main'], { name: '空值筛', portName: '主输出' }]])
  })

  it('select: checkbox columns from input cols with per-column rename', async () => {
    const wrapper = mountInspector({
      node: node({ id: 's1', op: 'select', name: '选列', inputs: ['src1:main'], config: {} }),
      candidates: CANDIDATES,
      portCols: { 'src1:main': ['型号', '功率_W', '备注'] },
    })

    await wrapper.get('[data-test="inspector-select-col-功率_W"]').find('input').setValue(true)
    await wrapper.get('[data-test="inspector-select-col-型号"]').find('input').setValue(true)
    // 改名输入只出现在已勾选列
    expect(wrapper.find('[data-test="inspector-rename-备注"]').exists()).toBe(false)
    await wrapper.get('[data-test="inspector-rename-型号"]').setValue('产品型号')

    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    // 勾选顺序即输出列序（后进在后）
    expect(wrapper.emitted('save')).toEqual([[{
      columns: [
        { id: '功率_W', name: '功率_W' },
        { id: '型号', name: '产品型号' },
      ],
    }, ['src1:main'], { name: '选列', portName: '主输出' }]])
  })

  it('select: falls back to manual comma-separated input when cols are unknown', async () => {
    const wrapper = mountInspector({
      node: node({ id: 's1', op: 'select', name: '选列', inputs: ['src1:main'], config: {} }),
      candidates: CANDIDATES,
    })
    expect(wrapper.find('[data-test="inspector-select-col-x"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="inspector-select-manual"]').exists()).toBe(true)

    await wrapper.get('[data-test="inspector-select-manual"]').setValue('型号，功率_W, 备注')
    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    expect(wrapper.emitted('save')).toEqual([[{
      columns: [
        { id: '型号', name: '型号' },
        { id: '功率_W', name: '功率_W' },
        { id: '备注', name: '备注' },
      ],
    }, ['src1:main'], { name: '选列', portName: '主输出' }]])
  })

  it('join: hydrates left/right keys from each input and emits method+keys', async () => {
    const wrapper = mountInspector({
      node: node({
        id: 'j1', op: 'join', name: '关联库存',
        inputs: ['src1:main', 'src2:main'],
        config: { method: 'inner', left_key: '型号', right_key: '产品型号' },
      }),
      candidates: CANDIDATES,
      portCols: { 'src1:main': ['型号', '功率_W'], 'src2:main': ['产品型号', '库存'] },
    })

    expect(selValue(wrapper, 'inspector-join-left')).toBe('型号')
    expect(selValue(wrapper, 'inspector-join-right')).toBe('产品型号')

    await wrapper.get('[data-test="inspector-join-method"]').findAll('input[type="radio"]')[1]!.setValue()
    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    expect(wrapper.emitted('save')).toEqual([[{
      method: 'inner', left_key: '型号', right_key: '产品型号',
    }, ['src1:main', 'src2:main'], { name: '关联库存', portName: '主输出' }]])
  })

  it('split_sections: level radio defaults from config and emits level', async () => {
    const wrapper = mountInspector({
      node: node({ id: 'sp1', op: 'split_sections', name: '切章节', inputs: ['src1:main'], config: { level: 2 } }),
      candidates: CANDIDATES,
    })
    const radios = wrapper.get('[data-test="inspector-split-level"]').findAll('input[type="radio"]')
    expect((radios[1]!.element as HTMLInputElement).checked).toBe(true)

    await radios[0]!.setValue()
    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    expect(wrapper.emitted('save')).toEqual([[{ level: 1 }, ['src1:main'], { name: '切章节', portName: '主输出' }]])
  })

  it('llm_extract: field row editor keeps >=1 row, adds and removes', async () => {
    const wrapper = mountInspector({
      node: node({
        id: 'l1', op: 'llm_extract', name: '抽取',
        inputs: ['src1:main'],
        config: {
          input_column: '内容', prompt_template: '抽取 {输入}',
          output_fields: [{ name: '厂家', type: 'enum' }],
          model: 'glm-4.7',
        },
      }),
      candidates: CANDIDATES,
      portCols: { 'src1:main': ['文件', '章节', '内容'] },
    })

    expect((wrapper.get('[data-test="inspector-llm-field-name-0"]').element as HTMLInputElement).value).toBe('厂家')
    expect(selValue(wrapper, 'inspector-llm-field-type-0')).toBe('enum')

    await wrapper.get('[data-test="inspector-llm-add-field"]').trigger('click')
    await wrapper.get('[data-test="inspector-llm-field-name-1"]').setValue('功率')
    await setSel(wrapper, 'inspector-llm-field-type-1', 'float')
    // 单行时删除禁用
    expect((wrapper.get('[data-test="inspector-llm-field-del-1"]').element as HTMLButtonElement).disabled).toBe(false)

    await wrapper.get('[data-test="inspector-llm-field-del-0"]').trigger('click')
    expect(wrapper.find('[data-test="inspector-llm-field-name-1"]').exists()).toBe(false)

    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    // 表单外的 model 键随保存保留（不静默换模型）
    expect(wrapper.emitted('save')).toEqual([[{
      input_column: '内容',
      prompt_template: '抽取 {输入}',
      output_fields: [{ name: '功率', type: 'float' }],
      model: 'glm-4.7',
    }, ['src1:main'], { name: '抽取', portName: '主输出' }]])
  })

  it('blocks save with a warning when required fields are missing', async () => {
    const wrapper = mountInspector({
      node: node({ id: 'f1', op: 'filter', name: '筛功率', inputs: ['src1:main'], config: {} }),
      candidates: CANDIDATES,
    })
    await wrapper.get('[data-test="inspector-save"]').trigger('click')
    expect(wrapper.emitted('save')).toBeUndefined()
  })

  it('emits trial and execute; execute disabled while running', async () => {
    const wrapper = mountInspector({
      node: node({ id: 'f1', op: 'filter', name: '筛功率', inputs: ['src1:main'], config: { field: 'x', cond: 'eq', value: '1' } }),
      candidates: CANDIDATES,
    })
    await wrapper.get('[data-test="inspector-trial"]').trigger('click')
    await wrapper.get('[data-test="inspector-execute"]').trigger('click')
    expect(wrapper.emitted('trial')).toHaveLength(1)
    expect(wrapper.emitted('execute')).toHaveLength(1)

    const busy = mountInspector({
      node: node({ id: 'f1', op: 'filter', name: '筛功率' }),
      running: true,
    })
    expect((busy.get('[data-test="inspector-execute"]').element as HTMLButtonElement).disabled).toBe(true)
    expect((busy.get('[data-test="inspector-trial"]').element as HTMLButtonElement).disabled).toBe(true)
  })

  it('emits delete-node from the danger button; hidden for viewers, disabled while running', async () => {
    // 删除节点（用户测试反馈修复2）：按钮在节点信息段，emit 交父级组装
    // 整图 PUT——确认弹窗与后端 422（下游仍引用）文案都在父级处理
    const wrapper = mountInspector({
      node: node({ id: 'f1', op: 'filter', name: '筛功率' }),
    })
    await wrapper.get('[data-test="inspector-delete-node"]').trigger('click')
    expect(wrapper.emitted('delete-node')).toHaveLength(1)

    const busy = mountInspector({
      node: node({ id: 'f1', op: 'filter', name: '筛功率' }),
      running: true,
    })
    expect((busy.get('[data-test="inspector-delete-node"]').element as HTMLButtonElement).disabled).toBe(true)

    const viewer = mountInspector({
      node: node({ id: 'f1', op: 'filter', name: '筛功率' }),
      canWrite: false,
    })
    expect(viewer.find('[data-test="inspector-delete-node"]').exists()).toBe(false)
  })

  it('hides the action footer for read-only viewers', () => {
    const wrapper = mountInspector({
      node: node({ id: 'f1', op: 'filter', name: '筛功率' }),
      canWrite: false,
    })
    expect(wrapper.find('[data-test="inspector-save"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('查看模式')
  })

  it('resets drafts when the selected node changes', async () => {
    const wrapper = mountInspector({
      node: node({
        id: 'f1', op: 'filter', name: '筛功率',
        inputs: ['src1:main'], config: { field: '功率_W', cond: 'gte', value: 2000 },
      }),
      candidates: CANDIDATES,
    })
    expect(selValue(wrapper, 'inspector-filter-field')).toBe('功率_W')

    await wrapper.setProps({
      node: node({ id: 's1', op: 'select', name: '选列', config: {} }),
    })
    expect(wrapper.find('[data-test="inspector-filter-field"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="inspector-select-manual"]').exists()).toBe(true)
  })
})
