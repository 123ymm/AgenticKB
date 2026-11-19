import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { defineComponent } from 'vue'
import type { PropType } from 'vue'

import WorkspaceOutputTable from '../WorkspaceOutputTable.vue'
import type { WorkspaceOutput } from '@/api/workspaces'

type ColumnStubWrapper = ReturnType<VueWrapper['findAll']>[number]

/**
 * 输出表格：列渲染（_id 隐藏/_source 固定右侧）、三态单元格、JSON 查看
 * 弹窗、前端分页。mount + 智能 el-table/el-table-column stub（provide/
 * inject 让列 stub 逐行渲染作用域插槽——真实组件无此机制，纯测试观察层）。
 */

// ── 测试观察层 stub：el-table 把 data provide 出去，el-table-column 逐行渲染作用域插槽
//    （键名不带下划线前缀——Vue 代理 has 陷阱排除 _ 开头的键，模板会 ReferenceError） ──
const ElTableStub = defineComponent({
  name: 'ElTable',
  props: { data: { type: Array as PropType<unknown[]>, default: () => [] } },
  provide() {
    return { stubTableRows: (): unknown[] => this.data }
  },
  template: '<div class="el-table-stub"><slot /></div>',
})

const ElTableColumnStub = {
  name: 'ElTableColumn',
  props: ['prop', 'label', 'fixed', 'width', 'minWidth', 'align', 'showOverflowTooltip'],
  inject: { stubTableRows: { default: () => () => [] } },
  template: `
    <div
      class="el-table-column-stub"
      :data-prop="prop ?? null"
      :data-label="label"
      :data-fixed="fixed ?? null"
    >
      <div v-for="(row, i) in stubTableRows()" :key="i" class="stub-row">
        <slot :row="row" />
      </div>
    </div>
  `,
}

const ElPaginationStub = {
  name: 'ElPagination',
  props: ['total', 'pageSize', 'currentPage', 'pageSizes', 'pagerCount', 'layout'],
  emits: ['current-change', 'size-change'],
  template: `
    <div class="el-pagination-stub" :data-total="total" :data-page-size="pageSize">
      <button type="button" class="stub-next" @click="$emit('current-change', Number(currentPage) + 1)">next</button>
    </div>
  `,
}

const ElDialogStub = {
  name: 'ElDialog',
  props: ['modelValue', 'title', 'width'],
  emits: ['update:modelValue'],
  template: `
    <div v-if="modelValue" class="el-dialog-stub" :data-title="title">
      <slot /><slot name="footer" />
    </div>
  `,
}

const ElPopoverStub = {
  name: 'ElPopover',
  props: ['placement', 'trigger', 'width', 'title'],
  template: '<div class="el-popover-stub"><slot name="reference" /><slot /></div>',
}

const stubs = {
  ElTable: ElTableStub,
  ElTableColumn: ElTableColumnStub,
  ElPagination: ElPaginationStub,
  ElDialog: ElDialogStub,
  ElPopover: ElPopoverStub,
}

interface Dataset {
  cols: string[]
  rows: Array<Record<string, unknown>>
  grain?: string
  [key: string]: unknown
}

function output(contentInline: Dataset, rowCount?: number): WorkspaceOutput {
  return {
    run_id: 'run-1', workspace_id: 'ws-1', node_id: 'src1', port_id: 'main',
    kind: 'table', content_inline: contentInline,
    row_count: rowCount ?? contentInline.rows.length,
    content_hash: 'h', quality_json: null, is_current: true,
    stale_since_run: null, created_at: '2026-11-16T08:00:00+00:00',
  }
}

function mountTable(outputProp: WorkspaceOutput | null): VueWrapper {
  return mount(WorkspaceOutputTable, { props: { output: outputProp }, global: { stubs } })
}

function columnStubs(wrapper: VueWrapper): ColumnStubWrapper[] {
  return wrapper.findAll('.el-table-column-stub')
}

describe('WorkspaceOutputTable', () => {
  it('renders dataset columns but hides _id, with the source column fixed right', () => {
    const wrapper = mountTable(output({
      cols: ['_id', '型号', '功率_W', '_source'],
      rows: [{ _id: 'r1', 型号: 'P1', 功率_W: 3200, _source: 'a.pdf#L2' }],
    }))

    const cols = columnStubs(wrapper)
    const labels = cols.map(c => c.attributes('data-label'))
    expect(labels).toEqual(['型号', '功率_W', '来源'])
    // _id 不显示；来源列固定右侧
    expect(cols.map(c => c.attributes('data-prop'))).not.toContain('_id')
    const sourceCol = cols[cols.length - 1]!
    expect(sourceCol.attributes('data-fixed')).toBe('right')
  })

  it('renders the empty state when the output has not been executed', () => {
    const wrapper = mountTable(null)
    expect(wrapper.text()).toContain('该输出尚未执行')
    expect(wrapper.find('.el-table-stub').exists()).toBe(false)
  })

  it('renders bool / null / JSON-string cells in the three display states', () => {
    const wrapper = mountTable(output({
      cols: ['型号', '在库', '备注', '参数'],
      rows: [{
        型号: 'P1',
        在库: true,
        备注: null,
        参数: '{"电压_V": 220, "尺寸": {"深": 460}}',
      }],
    }))

    // 1 数据行 × 4 数据列（来源列无 data-prop，不混入）：每个 .stub-row 即该列该行的单元格
    const cells = wrapper.findAll('.el-table-column-stub[data-prop] .stub-row')
    expect(cells).toHaveLength(4)
    expect(cells[0]!.text()).toBe('P1')
    // bool → 是/否 soft chip
    expect(cells[1]!.find('.ws-outtable__bool').exists()).toBe(true)
    expect(cells[1]!.text()).toBe('是')
    // null → —
    expect(cells[2]!.text()).toBe('—')
    // JSON 串 → { } 查看按钮
    expect(cells[3]!.find('.ws-outtable__json-btn').exists()).toBe(true)
    expect(cells[3]!.text()).toBe('{ } 查看')
  })

  it('renders false as 否 for bool cells', () => {
    const wrapper = mountTable(output({
      cols: ['在库'], rows: [{ 在库: false }],
    }))
    expect(wrapper.get('.ws-outtable__bool').text()).toBe('否')
  })

  it('opens a read-only pretty-JSON dialog when the { } 查看 button is clicked', async () => {
    const raw = '{"电压_V": 220, "尺寸": {"深": 460}}'
    const wrapper = mountTable(output({ cols: ['参数'], rows: [{ 参数: raw }] }))

    expect(wrapper.find('.el-dialog-stub').exists()).toBe(false)
    await wrapper.get('.ws-outtable__json-btn').trigger('click')

    const dialog = wrapper.get('.el-dialog-stub')
    expect(dialog.attributes('data-title')).toBe('参数 · JSON 内容（只读）')
    expect(dialog.text()).toContain('"电压_V": 220')
    expect(dialog.text()).toContain('"深": 460')
    // pretty：解析后两键缩进换行（原始串无换行）
    expect(dialog.get('pre').text()).toContain('\n')
  })

  it('falls back to the raw string when JSON-looking content fails to parse', async () => {
    const raw = '{"电压_V": 220'
    const wrapper = mountTable(output({ cols: ['参数'], rows: [{ 参数: raw }] }))
    await wrapper.get('.ws-outtable__json-btn').trigger('click')
    expect(wrapper.get('.el-dialog-stub').text()).toContain(raw)
  })

  it('shows the row _source full text via the source column popover', () => {
    const wrapper = mountTable(output({
      cols: ['型号', '_source'],
      rows: [{ 型号: 'P1', _source: 'docs/产品/P1.pdf#基本属性；库存表.xlsx#Sheet1' }],
    }))

    const sourceCol = columnStubs(wrapper).at(-1)!
    expect(sourceCol.find('.ws-outtable__src-btn').exists()).toBe(true)
    expect(sourceCol.text()).toContain('docs/产品/P1.pdf#基本属性；库存表.xlsx#Sheet1')
  })

  it('paginates frontend-side at 100 rows per page (250 rows → 3 pages)', async () => {
    const rows = Array.from({ length: 250 }, (_, i) => ({
      _id: `r${i}`, 型号: `P${i}`, _source: 's',
    }))
    const wrapper = mountTable(output({ cols: ['型号'], rows }, 250))

    const pager = wrapper.get('.el-pagination-stub')
    expect(pager.attributes('data-total')).toBe('250')
    expect(pager.attributes('data-page-size')).toBe('100')

    // 首页只渲染前 100 行（按数据列计——来源列另有自己的行渲染）；翻页后切到第 2 页
    let rendered = wrapper.findAll('.el-table-column-stub[data-prop="型号"] .stub-row')
    expect(rendered).toHaveLength(100)
    expect(rendered[0]!.text()).toContain('P0')

    await pager.get('.stub-next').trigger('click')
    rendered = wrapper.findAll('.el-table-column-stub[data-prop="型号"] .stub-row')
    expect(rendered).toHaveLength(100)
    expect(rendered[0]!.text()).toContain('P100')
  })

  it('hides the pager when rows fit on one page', () => {
    const wrapper = mountTable(output({
      cols: ['型号'], rows: [{ 型号: 'P1' }],
    }))
    expect(wrapper.find('.el-pagination-stub').exists()).toBe(false)
  })

  it('resets to page 1 when the output prop changes', async () => {
    const rowsA = Array.from({ length: 150 }, (_, i) => ({ 型号: `A${i}` }))
    const rowsB = Array.from({ length: 150 }, (_, i) => ({ 型号: `B${i}` }))
    const wrapper = mountTable(output({ cols: ['型号'], rows: rowsA }, 150))

    await wrapper.get('.stub-next').trigger('click')
    expect(wrapper.findAll('.el-table-column-stub[data-prop="型号"] .stub-row')[0]!.text()).toContain('A100')

    await wrapper.setProps({ output: output({ cols: ['型号'], rows: rowsB }, 150) })
    expect(wrapper.findAll('.el-table-column-stub[data-prop="型号"] .stub-row')[0]!.text()).toContain('B0')
  })
})
