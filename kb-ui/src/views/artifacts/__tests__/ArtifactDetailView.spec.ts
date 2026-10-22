import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  api: {
    get: vi.fn(),
    listRows: vi.fn(),
    listColumns: vi.fn(),
    remove: vi.fn(),
  },
  ui: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/proxyClient', () => ({
  apiErrorDetail: async () => 'mock-error',
  // MembersPanel 经 api/auth 引入的 proxyClient 具名导出（本套件用不到真实实现）
  installAuthInterceptors: vi.fn(),
  createProxyClient: vi.fn(() => ({ get: vi.fn() })),
  extractOne: vi.fn(),
}))
vi.mock('@/api/auth', () => ({
  useAuthApi: () => ({ listDomainUserCandidates: async () => [] }),
}))
vi.mock('element-plus', () => ({
  ElMessage: {
    success: state.ui.success, error: state.ui.error, warning: state.ui.warning,
  },
  ElMessageBox: { confirm: vi.fn(), prompt: vi.fn() },
}))

import ArtifactDetailView from '../ArtifactDetailView.vue'

interface DetailVm {
  activeTab: string
  runtimeColumns: Array<{ column_id: string; column_name: string }>
  runtimeRows: unknown[]
  reloadAll: () => Promise<void>
}

function mountDetail(): VueWrapper {
  return shallowMount(ArtifactDetailView, {
    props: { artifactId: 'art-1' },
    global: {
      // el-tabs/el-tab-pane 桩渲染插槽——版本面板（回滚事件源）得以挂载；
      // el-table/-column 桩不渲染作用域插槽（行作用域 props 缺省会炸渲染）
      stubs: {
        ElTabs: { template: '<div class="el-tabs"><slot /></div>' },
        ElTabPane: { template: '<div class="el-tab-pane"><slot /></div>' },
        ElTable: { template: '<div class="el-table-stub" />' },
        ElTableColumn: { template: '<div class="el-table-column-stub" />' },
        RouterLink: true,
      },
    },
  })
}

const column = (id: string, name: string) => ({
  artifact_id: 'art-1', column_id: id, column_name: name, column_type: 'text',
  enum_values: null, is_primary_key: false, is_indexed: false, alias: null,
  status: 'active', ordinal: 1,
})

const artifactDetail = {
  artifact_id: 'art-1', domain_id: 'plant-a', kind: 'table',
  title: 'MOP 表', description: '', usage_note: '查责任域',
  owner_user_id: 'u1', status: 'published', current_revision: 2, draft_open: true,
  created_at: '2026-10-16T08:00:00+00:00', updated_at: '2026-10-16T09:00:00+00:00',
  my_role: 'owner',
  // 设计态 schema：草稿含未发布新列
  schema: [column('c1', 'MOP'), column('c2', '草稿新列')],
  sample_rows: [],
}

const runtimeSchema = [column('c1', 'MOP')]

beforeEach(() => {
  state.api.get.mockReset().mockResolvedValue(artifactDetail)
  state.api.listColumns.mockReset().mockResolvedValue(runtimeSchema)
  state.api.listRows.mockReset().mockResolvedValue(
    { rows: [{ row_key: '16:NC-1', data: { MOP: 'NC-1' }, provenance: [], conflicts: {}, ordinal: 1 }], total: 1, page: 1, size: 20 })
  state.ui.success.mockReset()
  state.ui.error.mockReset()
  state.ui.warning.mockReset()
})

describe('artifact detail view', () => {
  it('derives runtime table columns from the runtime schema snapshot, not the design schema', async () => {
    const wrapper = mountDetail()
    await flushPromises()
    const vm = wrapper.vm as unknown as DetailVm

    // 详情加载即带设计态 schema；运行态 schema 懒加载（未切 tab 不拉）
    expect(state.api.get).toHaveBeenCalledWith('art-1')
    expect(state.api.listColumns).not.toHaveBeenCalled()

    vm.activeTab = 'runtime'
    await flushPromises()

    expect(state.api.listColumns).toHaveBeenCalledWith('art-1', { runtime: true })
    expect(vm.runtimeColumns.map(c => c.column_name)).toEqual(['MOP'])
    // 设计态草稿新列不得进入运行态表头（codex二审功能项②）
    expect(vm.runtimeColumns.map(c => c.column_name)).not.toContain('草稿新列')

    // 表格路径回归（批次2 拆守卫后）：wiki 双面板不渲染，发布单位缺省=行
    expect(wrapper.findComponent({ name: 'WikiEditorPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'WikiReaderPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'ArtifactPublishDialog' }).props('unitLabel')).toBe('行')
  })

  it('wiki 制品：设计态/运行态挂 wiki 面板，切运行态不触发表格接口', async () => {
    state.api.get.mockReset().mockResolvedValue({ ...artifactDetail, kind: 'wiki' })
    const wrapper = mountDetail()
    await flushPromises()
    const vm = wrapper.vm as unknown as DetailVm

    vm.activeTab = 'runtime'
    await flushPromises()

    // wiki 守卫保留：运行态 tab 不拉表格列/行（阅读器面板自加载，无 API 死调用）
    expect(state.api.listColumns).not.toHaveBeenCalled()
    expect(state.api.listRows).not.toHaveBeenCalled()

    // 双面板接线（shallowMount 桩）：设计态编辑面板 + 运行态阅读面板
    const editor = wrapper.findComponent({ name: 'WikiEditorPanel' })
    expect(editor.exists()).toBe(true)
    expect(editor.props('artifactId')).toBe('art-1')
    expect(editor.props('canWrite')).toBe(true)
    expect(editor.props('expectedUpdatedAt')).toBe('2026-10-16T09:00:00+00:00')
    const reader = wrapper.findComponent({ name: 'WikiReaderPanel' })
    expect(reader.exists()).toBe(true)
    expect(reader.props('artifactId')).toBe('art-1')

    // 表格三组件不渲染（wiki 路径）
    expect(wrapper.findComponent({ name: 'ArtifactImportWizard' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'ArtifactSchemaPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'ArtifactRowsPanel' }).exists()).toBe(false)

    // 发布对话框单位 kind 化：wiki → 页
    expect(wrapper.findComponent({ name: 'ArtifactPublishDialog' }).props('unitLabel')).toBe('页')
  })

  it('refetches detail, runtime schema and rows after a rollback (versions panel changed)', async () => {
    const wrapper = mountDetail()
    await flushPromises()
    const vm = wrapper.vm as unknown as DetailVm
    vm.activeTab = 'runtime'
    await flushPromises()

    const before = {
      get: state.api.get.mock.calls.length,
      columns: state.api.listColumns.mock.calls.length,
      rows: state.api.listRows.mock.calls.length,
    }
    expect(before.rows).toBe(1)

    // 版本面板回滚成功 → emit('changed') → 统一 reloadAll
    wrapper.findComponent({ name: 'ArtifactVersionsPanel' }).vm.$emit('changed')
    await flushPromises()

    expect(state.api.get.mock.calls.length).toBe(before.get + 1)
    expect(state.api.listColumns.mock.calls.length).toBe(before.columns + 1)
    expect(state.api.listRows.mock.calls.length).toBe(before.rows + 1)
    expect(state.api.listColumns).toHaveBeenLastCalledWith('art-1', { runtime: true })
  })

  it('refetches runtime data on discard draft via reloadAll', async () => {
    const wrapper = mountDetail()
    await flushPromises()
    const vm = wrapper.vm as unknown as DetailVm
    vm.activeTab = 'runtime'
    await flushPromises()

    await vm.reloadAll()
    await flushPromises()

    expect(state.api.get.mock.calls.length).toBe(2)
    expect(state.api.listColumns.mock.calls.length).toBe(2)
    expect(state.api.listRows.mock.calls.length).toBe(2)
  })

  it('viewer 默认落在运行态 tab（设计态对非写者只是空提示）', async () => {
    state.api.get.mockReset().mockResolvedValue({ ...artifactDetail, my_role: 'viewer' })
    const wrapper = mountDetail()
    await flushPromises()
    const vm = wrapper.vm as unknown as DetailVm
    expect(vm.activeTab).toBe('runtime')
  })

  it('删除门禁：未停用制品删除按钮置灰；停用后输入标题确认才级联删除', async () => {
    // published 态：按钮存在（owner）但 disabled
    const wrapper = mountDetail()
    await flushPromises()
    const btn = wrapper.find('[data-test="delete-artifact"]')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes('disabled')).toBeDefined()

    // deprecated 态：可点 → 输错标题取消 → 输对标题级联删除并回列表
    state.api.get.mockReset().mockResolvedValue({ ...artifactDetail, status: 'deprecated' })
    state.api.remove = vi.fn().mockResolvedValue(undefined)
    const wrapper2 = mountDetail()
    await flushPromises()
    const btn2 = wrapper2.find('[data-test="delete-artifact"]')
    expect(btn2.attributes('disabled')).toBeUndefined()

    const { ElMessageBox } = await import('element-plus')
    const prompt = ElMessageBox.prompt as unknown as ReturnType<typeof vi.fn>
    prompt.mockReset()
      .mockResolvedValueOnce({ value: '输错的标题' })   // 不一致 → 取消
      .mockResolvedValueOnce({ value: 'MOP 表' })       // 一致 → 删除
    await (wrapper2.vm as unknown as { deleteArtifact: () => Promise<void> }).deleteArtifact()
    expect(state.api.remove).not.toHaveBeenCalled()
    expect(state.ui.warning).toHaveBeenCalled()

    await (wrapper2.vm as unknown as { deleteArtifact: () => Promise<void> }).deleteArtifact()
    expect(state.api.remove).toHaveBeenCalledWith('art-1')
  })
})
