import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  api: {
    listRows: vi.fn(),
    updateCell: vi.fn(),
    batchAssign: vi.fn(),
    deleteRow: vi.fn(),
    insertRow: vi.fn(),
  },
  ui: { confirm: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => 'mock-error' }))
vi.mock('element-plus', () => ({
  ElMessageBox: { confirm: state.ui.confirm },
  ElMessage: {
    success: state.ui.success, error: state.ui.error, warning: state.ui.warning,
  },
}))

import ArtifactRowsPanel from '../ArtifactRowsPanel.vue'
import type { ArtifactColumn, ArtifactRow } from '@/types/artifacts'

interface RowsVm {
  rows: ArtifactRow[]
  total: number
  draft: boolean
  editableColumns: ArtifactColumn[]
  pkColumns: ArtifactColumn[]
  addForm: Record<string, unknown>
  editForm: { rowKey: string; values: Record<string, unknown> }
  assignForm: { column: string; value: string; conditions: Array<{ column: string; op: string; value: string }> }
  loadRows: () => Promise<void>
  onDraftChange: () => void
  openEdit: (row: ArtifactRow) => void
  submitEdit: () => Promise<void>
  openAssign: () => void
  submitAssign: () => Promise<void>
  openAdd: () => void
  submitAdd: () => Promise<void>
  buildAssignPayload: () => {
    column: string
    value: unknown
    where: Array<{ column: string; op: string; value?: unknown }>
  }
  removeRow: (row: ArtifactRow) => Promise<void>
}

const columns: ArtifactColumn[] = [
  {
    artifact_id: 'art-1', column_id: 'c001', column_name: 'MOP', column_type: 'text',
    enum_values: null, is_primary_key: true, is_indexed: true, alias: null,
    status: 'active', ordinal: 1,
  },
  {
    artifact_id: 'art-1', column_id: 'c002', column_name: 'L1分类', column_type: 'enum',
    enum_values: ['数据业务', '网络维护'], is_primary_key: false, is_indexed: false,
    alias: null, status: 'active', ordinal: 2,
  },
  {
    artifact_id: 'art-1', column_id: 'c003', column_name: '旧备注', column_type: 'text',
    enum_values: null, is_primary_key: false, is_indexed: false, alias: null,
    status: 'dropped', ordinal: 3,
  },
]

const rowsPage = {
  rows: [
    {
      row_key: 'NC001',
      data: { MOP: 'NC001', L1分类: '数据业务' },
      provenance: [{ batch_id: 'bat_1', file_name: 'A.xlsx', sheet_name: 'Sheet1', row_index: 2 }],
      conflicts: null,
      ordinal: 1,
    },
  ],
  total: 1, page: 1, size: 50,
}

function mountPanel(expectedUpdatedAt = '2026-10-16T08:00:00+00:00'): VueWrapper {
  return shallowMount(ArtifactRowsPanel, {
    props: {
      artifactId: 'art-1',
      canWrite: true,
      columns,
      expectedUpdatedAt,
      draftOpen: false,
    },
  })
}

beforeEach(() => {
  state.api.listRows.mockReset().mockResolvedValue(rowsPage)
  state.api.updateCell.mockReset().mockResolvedValue({ row_key: 'NC001', data: {} })
  state.api.batchAssign.mockReset().mockResolvedValue({ updated: 1 })
  state.api.deleteRow.mockReset().mockResolvedValue(undefined)
  state.api.insertRow.mockReset().mockResolvedValue({
    row_key: 'NC999', data: { MOP: 'NC999', L1分类: '数据业务' },
    provenance: [{ channel: 'manual' }], ordinal: 2,
  })
  state.ui.confirm.mockReset().mockResolvedValue('confirm')
  state.ui.success.mockReset()
  state.ui.error.mockReset()
  state.ui.warning.mockReset()
})

describe('ArtifactRowsPanel', () => {
  it('loads draft rows server-paginated with dynamic active columns (dropped filtered)', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    expect(state.api.listRows).toHaveBeenCalledWith('art-1', { draft: true, page: 1, size: 50 })
    expect(vm.rows).toHaveLength(1)
    expect(vm.total).toBe(1)
    expect(vm.editableColumns.map(c => c.column_name)).toEqual(['MOP', 'L1分类'])
    // enum 列识别（cell 渲染走 el-tag 分支）
    expect(vm.editableColumns[1].column_type).toBe('enum')
    expect(vm.editableColumns[1].enum_values).toEqual(['数据业务', '网络维护'])
  })

  it('reloads with draft=false when the draft switch is turned off', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    vm.draft = false
    vm.onDraftChange()
    await flushPromises()

    expect(state.api.listRows).toHaveBeenLastCalledWith('art-1', { draft: false, page: 1, size: 50 })
  })

  it('edits a row cell with the optimistic lock token and emits changed', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    vm.openEdit(vm.rows[0])
    expect(vm.editForm.values.MOP).toBe('NC001')
    vm.editForm.values.MOP = 'NC001X'
    await vm.submitEdit()
    await flushPromises()

    // 仅变化格落请求；未变化的 L1分类 不发
    expect(state.api.updateCell).toHaveBeenCalledTimes(1)
    expect(state.api.updateCell).toHaveBeenCalledWith('art-1', 'NC001', {
      column: 'MOP', value: 'NC001X', expected_updated_at: '2026-10-16T08:00:00+00:00',
    })
    expect(wrapper.emitted('changed')).toBeTruthy()
  })

  it('carries the lock token only on the first cell of a multi-cell edit', async () => {
    // 后端每格 touch updated_at——第二格复用同令牌必 409，故首格校验后续格省略
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    vm.openEdit(vm.rows[0])
    vm.editForm.values.MOP = 'NC001X'
    vm.editForm.values['L1分类'] = '网络维护'
    await vm.submitEdit()
    await flushPromises()

    expect(state.api.updateCell).toHaveBeenCalledTimes(2)
    expect(state.api.updateCell).toHaveBeenNthCalledWith(1, 'art-1', 'NC001', {
      column: 'MOP', value: 'NC001X', expected_updated_at: '2026-10-16T08:00:00+00:00',
    })
    expect(state.api.updateCell).toHaveBeenNthCalledWith(2, 'art-1', 'NC001', {
      column: 'L1分类', value: '网络维护',
    })
  })

  it('builds batch-assign payload with typed values and in-splitting conditions', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    vm.openAssign()
    vm.assignForm = {
      column: 'L1分类',
      value: '数据业务',
      conditions: [
        { column: 'MOP', op: 'in', value: 'NC001, NC002' },
        { column: 'MOP', op: 'contains', value: 'NC' },
      ],
    }
    expect(vm.buildAssignPayload()).toEqual({
      column: 'L1分类',
      value: '数据业务',
      where: [
        { column: 'MOP', op: 'in', value: ['NC001', 'NC002'] },
        { column: 'MOP', op: 'contains', value: 'NC' },
      ],
    })

    await vm.submitAssign()
    await flushPromises()
    expect(state.api.batchAssign).toHaveBeenCalledWith('art-1', {
      column: 'L1分类',
      value: '数据业务',
      where: [
        { column: 'MOP', op: 'in', value: ['NC001', 'NC002'] },
        { column: 'MOP', op: 'contains', value: 'NC' },
      ],
      expected_updated_at: '2026-10-16T08:00:00+00:00',
    })
    expect(wrapper.emitted('changed')).toBeTruthy()
  })

  it('deletes a row after confirmation', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    await vm.removeRow(vm.rows[0])
    await flushPromises()

    expect(state.api.deleteRow).toHaveBeenCalledWith('art-1', 'NC001', '2026-10-16T08:00:00+00:00')
    expect(wrapper.emitted('changed')).toBeTruthy()
  })

  it('omits value for is_null / not_null conditions', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    vm.openAssign()
    vm.assignForm = {
      column: 'MOP', value: 'NC001X',
      conditions: [
        { column: 'L1分类', op: 'is_null', value: '' },
        { column: 'MOP', op: 'not_null', value: '' },
      ],
    }
    expect(vm.buildAssignPayload().where).toEqual([
      { column: 'L1分类', op: 'is_null' },
      { column: 'MOP', op: 'not_null' },
    ])
  })

  it('surfaces stale_artifact 409 with a refresh hint and emits changed', async () => {
    state.api.batchAssign.mockRejectedValueOnce({
      response: { status: 409, data: { detail: { code: 'stale_artifact' } } },
    })
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    vm.openAssign()
    vm.assignForm = { column: 'L1分类', value: '数据业务', conditions: [] }
    await vm.submitAssign()
    await flushPromises()

    expect(state.ui.error).toHaveBeenCalledWith(expect.stringContaining('刷新'))
    expect(wrapper.emitted('changed')).toBeTruthy()
  })

  it('inserts a manual row via POST /table/rows with typed values and the lock token', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    vm.openAdd()
    expect(vm.pkColumns.map(c => c.column_name)).toEqual(['MOP'])
    vm.addForm.MOP = 'NC999'
    vm.addForm['L1分类'] = '数据业务'
    await vm.submitAdd()
    await flushPromises()

    expect(state.api.insertRow).toHaveBeenCalledWith('art-1', {
      data: { MOP: 'NC999', L1分类: '数据业务' },
      expected_updated_at: '2026-10-16T08:00:00+00:00',
    })
    expect(state.ui.success).toHaveBeenCalled()
    expect(wrapper.emitted('changed')).toBeTruthy()
  })

  it('blocks manual insert when a primary key column is empty', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as RowsVm

    vm.openAdd()
    vm.addForm['L1分类'] = '数据业务'   // 主键 MOP 留空
    await vm.submitAdd()
    await flushPromises()

    expect(state.ui.warning).toHaveBeenCalledWith(expect.stringContaining('主键列必须填写'))
    expect(state.api.insertRow).not.toHaveBeenCalled()
  })
})
