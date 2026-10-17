import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  api: {
    listColumns: vi.fn(),
    uploadTable: vi.fn(),
    importTable: vi.fn(),
  },
  ui: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => 'mock-error' }))
vi.mock('element-plus', () => ({
  ElMessage: {
    success: state.ui.success, error: state.ui.error, warning: state.ui.warning,
  },
}))

import ArtifactImportWizard from '../ArtifactImportWizard.vue'

interface WizardVm {
  envelope: { preview_token: string } | null
  sheet: string
  mappings: Array<{
    source: string
    action: string
    targetColumnId: string
    targetName: string
    columnType: string
    enumValues: string[]
    isPrimaryKey: boolean
    isIndexed: boolean
  }>
  report: Record<string, unknown> | null
  mappingConfirmed: boolean
  missingColumns: string[]
  doUpload: (file: File) => Promise<void>
  confirmMapping: () => void
  buildColumnOps: () => Array<Record<string, unknown>>
  runImport: () => Promise<void>
  resetWizard: () => void
}

function mountWizard(): VueWrapper {
  return shallowMount(ArtifactImportWizard, {
    props: { artifactId: 'art-1' },
    global: {
      // 卡片/告警渲染插槽——渐进披露与报告区文本断言需要（默认浅桩不渲染 slot）
      stubs: {
        ElCard: { template: '<div class="el-card"><slot name="header" /><slot /></div>' },
        ElAlert: { props: ['title'], template: '<div class="el-alert">{{ title }}<slot /></div>' },
      },
    },
  })
}

const previewEnvelope = {
  preview_token: 'tok_1234567890ab',
  preview: {
    file_name: 'A.xlsx',
    sheets: ['Sheet1', 'Sheet2'],
    default_sheet: 'Sheet1',
    columns: [
      { source_name: 'MOP', inferred_type: 'text', enum_candidates: [] },
      { source_name: 'L1分类', inferred_type: 'enum', enum_candidates: ['数据业务', '网络维护', '云资源'] },
    ],
    sample_rows: [
      { MOP: 'NC001', L1分类: '数据业务' },
      { MOP: 'NC002', L1分类: '网络维护' },
    ],
  },
}

const existingColumns = [
  {
    artifact_id: 'art-1', column_id: 'c001', column_name: 'MOP', column_type: 'text',
    enum_values: null, is_primary_key: true, is_indexed: true, alias: null,
    status: 'active', ordinal: 1,
  },
  {
    artifact_id: 'art-1', column_id: 'c002', column_name: 'L2分类', column_type: 'text',
    enum_values: null, is_primary_key: false, is_indexed: false, alias: null,
    status: 'active', ordinal: 2,
  },
]

const importReport = {
  artifact_id: 'art-1', mode: 'append', batch_id: 'bat_1', sheet: 'Sheet1',
  row_count: 8, inserted: 6, updated: 2, type_errors: 0, key_conflicts: 0,
  conflicts_sample: [], missing_columns: [],
}

beforeEach(() => {
  state.api.listColumns.mockReset().mockResolvedValue([])
  state.api.uploadTable.mockReset().mockResolvedValue(previewEnvelope)
  state.api.importTable.mockReset().mockResolvedValue(importReport)
  state.ui.success.mockReset()
  state.ui.error.mockReset()
  state.ui.warning.mockReset()
})

describe('ArtifactImportWizard', () => {
  it('hides mapping card until a preview arrives (progressive disclosure)', async () => {
    const wrapper = mountWizard()
    await flushPromises()

    // active 默认 true：先取已有列（map_to 选项底料），但未上传时不出现卡②
    expect(state.api.listColumns).toHaveBeenCalledWith('art-1')
    expect(state.api.uploadTable).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('② 列映射')
    expect(wrapper.text()).not.toContain('③ 预览确认')
    expect((wrapper.vm as unknown as WizardVm).envelope).toBeNull()
  })

  it('initializes all-new mappings with inferred types for an empty artifact', async () => {
    const wrapper = mountWizard()
    await flushPromises()
    await (wrapper.vm as unknown as WizardVm).doUpload(new File(['x'], 'A.xlsx'))
    await flushPromises()

    const vm = wrapper.vm as unknown as WizardVm
    expect(state.api.uploadTable).toHaveBeenCalledWith('art-1', expect.any(File))
    expect(vm.envelope?.preview_token).toBe('tok_1234567890ab')
    expect(vm.sheet).toBe('Sheet1')
    expect(vm.mappings).toHaveLength(2)
    expect(vm.mappings.map(m => m.action)).toEqual(['new', 'new'])
    expect(vm.mappings[0].targetName).toBe('MOP')
    expect(vm.mappings[0].columnType).toBe('text')
    expect(vm.mappings[1].columnType).toBe('enum')
    expect(vm.mappings[1].enumValues).toEqual(['数据业务', '网络维护', '云资源'])
    expect(vm.mappingConfirmed).toBe(false)
    expect(wrapper.text()).toContain('② 列映射')
  })

  it('auto-maps same-name columns and reports missing schema columns in append mode', async () => {
    state.api.listColumns.mockResolvedValue(existingColumns)
    const wrapper = mountWizard()
    await flushPromises()
    await (wrapper.vm as unknown as WizardVm).doUpload(new File(['x'], 'A.xlsx'))
    await flushPromises()

    const vm = wrapper.vm as unknown as WizardVm
    expect(vm.mappings[0].action).toBe('map_to')
    expect(vm.mappings[0].targetColumnId).toBe('c001')
    expect(vm.mappings[1].action).toBe('new')
    // c002（L2分类）本批无文件列映射 → 置空提示
    expect(vm.missingColumns).toEqual(['L2分类'])
    expect(wrapper.text()).toContain('以下列本批将为空：L2分类')
  })

  it('supports composite primary key selection (multi-select, not exclusive)', async () => {
    const wrapper = mountWizard()
    await flushPromises()
    const vm = wrapper.vm as unknown as WizardVm
    await vm.doUpload(new File(['x'], 'A.xlsx'))
    await flushPromises()

    // 多选：两列同时标记主键（组合主键）互不排斥
    vm.mappings[0].isPrimaryKey = true
    vm.mappings[1].isPrimaryKey = true
    expect(vm.mappings[0].isPrimaryKey).toBe(true)
    expect(vm.mappings[1].isPrimaryKey).toBe(true)
    vm.confirmMapping()
    expect(vm.mappingConfirmed).toBe(true)

    const ops = vm.buildColumnOps().filter(op => op.action === 'new')
    expect(ops.filter(op => op.is_primary_key)).toHaveLength(2)
    // 取消一列不影响另一列
    vm.mappings[1].isPrimaryKey = false
    expect(vm.buildColumnOps().filter(op => op.is_primary_key)).toHaveLength(1)
  })

  it('blocks append confirmation when no column maps to an existing one', async () => {
    state.api.listColumns.mockResolvedValue(existingColumns)
    const wrapper = mountWizard()
    await flushPromises()
    const vm = wrapper.vm as unknown as WizardVm
    await vm.doUpload(new File(['x'], 'A.xlsx'))
    await flushPromises()
    vm.mappings.forEach(m => { m.action = 'new' })

    vm.confirmMapping()
    expect(state.ui.warning).toHaveBeenCalled()
    expect(vm.mappingConfirmed).toBe(false)
  })

  it('sends raw column_ops (map_to/new/drop) plus conflict policy and renders the report', async () => {
    state.api.listColumns.mockResolvedValue(existingColumns)
    const wrapper = mountWizard()
    await flushPromises()
    const vm = wrapper.vm as unknown as WizardVm
    await vm.doUpload(new File(['x'], 'A.xlsx'))
    await flushPromises()

    // MOP→map_to(c001)（自动）；L1分类→新建 enum 带主键+索引+枚举值
    vm.mappings[1].targetName = 'L1分类'
    vm.mappings[1].columnType = 'enum'
    vm.mappings[1].enumValues = ['数据业务', '网络维护']
    vm.mappings[1].isPrimaryKey = true
    vm.mappings[1].isIndexed = true
    vm.confirmMapping()
    expect(vm.mappingConfirmed).toBe(true)

    await vm.runImport()
    await flushPromises()

    expect(state.api.importTable).toHaveBeenCalledWith('art-1', {
      preview_token: 'tok_1234567890ab',
      plan: {
        sheet: 'Sheet1',
        header_row: 1,
        column_ops: [
          { source: 'MOP', action: 'map_to', target: 'c001' },
          {
            source: 'L1分类', action: 'new', target_name: 'L1分类', column_type: 'enum',
            is_primary_key: true, is_indexed: true, enum_values: ['数据业务', '网络维护'],
          },
        ],
      },
      conflict_policy: { type_error: 'strict', key_conflict: 'block' },
    })
    expect(vm.report).toEqual(importReport)
    expect(wrapper.emitted('imported')?.[0]).toEqual([importReport])
    expect(wrapper.text()).toContain('处理 8 行')
    expect(wrapper.text()).toContain('新增 6')
    expect(wrapper.text()).toContain('更新 2')
  })

  it('blocks duplicate target column names before confirming', async () => {
    const wrapper = mountWizard()
    await flushPromises()
    const vm = wrapper.vm as unknown as WizardVm
    await vm.doUpload(new File(['x'], 'A.xlsx'))
    await flushPromises()
    vm.mappings[1].targetName = 'MOP' // 与 mappings[0] 目标重名

    vm.confirmMapping()
    expect(state.ui.warning).toHaveBeenCalledWith(expect.stringContaining('目标列名重复'))
    expect(vm.mappingConfirmed).toBe(false)
  })

  it('resets the wizard on preview expiry (410)', async () => {
    state.api.importTable.mockRejectedValueOnce({ response: { status: 410 } })
    const wrapper = mountWizard()
    await flushPromises()
    const vm = wrapper.vm as unknown as WizardVm
    await vm.doUpload(new File(['x'], 'A.xlsx'))
    await flushPromises()
    vm.confirmMapping()

    await vm.runImport()
    await flushPromises()

    expect(state.ui.error).toHaveBeenCalledWith(expect.stringContaining('重新上传'))
    expect(vm.envelope).toBeNull()
    expect(vm.mappings).toHaveLength(0)
  })
})
