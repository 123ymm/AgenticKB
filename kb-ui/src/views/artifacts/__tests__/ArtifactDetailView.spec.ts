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
  /** 设计态空制品引导（66号 §4）；非空制品为 null。 */
  emptyGuide: { title: string; description: string } | null
}

function mountDetail(): VueWrapper {
  return shallowMount(ArtifactDetailView, {
    props: { artifactId: 'art-1' },
    global: {
      // el-tabs/el-tab-pane 桩渲染插槽——版本面板（回滚事件源）得以挂载；
      // el-table/-column 桩不渲染作用域插槽（行作用域 props 缺省会炸渲染）；
      // el-tag 桩渲染插槽——D01 头部「服务中/待发布」并存表述按文本断言
      stubs: {
        ElTabs: { template: '<div class="el-tabs"><slot /></div>' },
        ElTabPane: { template: '<div class="el-tab-pane"><slot /></div>' },
        ElTable: { template: '<div class="el-table-stub" />' },
        ElTableColumn: { template: '<div class="el-table-column-stub" />' },
        ElTag: { template: '<span class="el-tag-stub"><slot /></span>' },
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

    // 表格路径回归（批次2 拆守卫后）：wiki 三面板（编译/编辑/阅读）不渲染，发布单位缺省=行
    expect(wrapper.findComponent({ name: 'WikiCompilePanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'WikiEditorPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'WikiReaderPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'ArtifactPublishDialog' }).props('unitLabel')).toBe('行')

    // V01：版本面板计数列头按类型——table 传 kind=table（列头=行数）
    expect(wrapper.findComponent({ name: 'ArtifactVersionsPanel' }).props('kind')).toBe('table')
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

    // 批次3：设计态顶部挂编译面板（来源绑定+目录确认+进度轮询），
    // 终态 emit changed → reloadAll 刷新乐观锁令牌（编译页写推进令牌）
    const compile = wrapper.findComponent({ name: 'WikiCompilePanel' })
    expect(compile.exists()).toBe(true)
    expect(compile.props('artifactId')).toBe('art-1')
    expect(compile.props('canWrite')).toBe(true)
    expect(compile.props('expectedUpdatedAt')).toBe('2026-10-16T09:00:00+00:00')
    const getBefore = state.api.get.mock.calls.length
    compile.vm.$emit('changed')
    await flushPromises()
    expect(state.api.get.mock.calls.length).toBe(getBefore + 1)

    // 表格三组件不渲染（wiki 路径）
    expect(wrapper.findComponent({ name: 'ArtifactImportWizard' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'ArtifactSchemaPanel' }).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'ArtifactRowsPanel' }).exists()).toBe(false)

    // 发布对话框单位 kind 化：wiki → 页
    expect(wrapper.findComponent({ name: 'ArtifactPublishDialog' }).props('unitLabel')).toBe('页')

    // V01：版本面板计数列头按类型——wiki 传 kind=wiki（列头=页数）
    expect(wrapper.findComponent({ name: 'ArtifactVersionsPanel' }).props('kind')).toBe('wiki')
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

describe('artifact detail view — 设计态空制品引导（66号 §4）', () => {
  const emptyArtifact = {
    ...artifactDetail, status: 'draft', current_revision: 0, draft_open: false,
    schema: [], sample_rows: [],
  }

  beforeEach(() => {
    state.api.get.mockReset().mockResolvedValue(emptyArtifact)
  })

  it('空 wiki 制品设计态提示「从知识库生成」', async () => {
    state.api.get.mockResolvedValue({ ...emptyArtifact, kind: 'wiki' })
    const wrapper = mountDetail()
    await flushPromises()
    const vm = wrapper.vm as unknown as DetailVm

    expect(wrapper.find('[data-test="design-empty-guide"]').exists()).toBe(true)
    expect(vm.emptyGuide?.title).toContain('从知识库生成')
  })

  it('空表格制品设计态提示「上传 Excel 或编辑字段」', async () => {
    const wrapper = mountDetail()
    await flushPromises()
    const vm = wrapper.vm as unknown as DetailVm

    expect(wrapper.find('[data-test="design-empty-guide"]').exists()).toBe(true)
    expect(vm.emptyGuide?.title).toContain('上传 Excel')
    expect(vm.emptyGuide?.title).toContain('编辑字段')
  })

  it('非空制品（已发布 / 已检出草稿 / 已有列定义）不显示引导', async () => {
    // 已发布有版本（默认 fixture：rev2 + draft_open）
    state.api.get.mockResolvedValue(artifactDetail)
    const published = mountDetail()
    await flushPromises()
    expect(published.find('[data-test="design-empty-guide"]').exists()).toBe(false)
    expect((published.vm as unknown as DetailVm).emptyGuide).toBeNull()

    // rev0 但已检出草稿（导入/编译进行中——用户已知道下一步）
    state.api.get.mockResolvedValue({ ...emptyArtifact, draft_open: true })
    const drafting = mountDetail()
    await flushPromises()
    expect(drafting.find('[data-test="design-empty-guide"]').exists()).toBe(false)

    // rev0 表格但已有列定义（手工建列起步）
    state.api.get.mockResolvedValue({ ...emptyArtifact, schema: [column('c1', 'MOP')] })
    const withColumns = mountDetail()
    await flushPromises()
    expect(withColumns.find('[data-test="design-empty-guide"]').exists()).toBe(false)
  })

  it('viewer 空制品不显示操作引导（设计态写入口对只读者收敛）', async () => {
    state.api.get.mockResolvedValue({ ...emptyArtifact, kind: 'wiki', my_role: 'viewer' })
    const wrapper = mountDetail()
    await flushPromises()

    expect(wrapper.find('[data-test="design-empty-guide"]').exists()).toBe(false)
    expect((wrapper.vm as unknown as DetailVm).emptyGuide).toBeNull()
  })
})

describe('artifact detail view — D03.1 wiki 内容区「页面｜图谱」切换', () => {
  interface WikiViewVm {
    activeTab: string
    designWikiView: 'pages' | 'graph'
    runtimeWikiView: 'pages' | 'graph'
  }

  it('图谱懒挂载（design draft=canWrite / runtime 恒 false）；打开页面切回页面视图；表格制品无入口', async () => {
    state.api.get.mockReset().mockResolvedValue({ ...artifactDetail, kind: 'wiki' })
    const wrapper = mountDetail()
    await flushPromises()
    const vm = wrapper.vm as unknown as WikiViewVm

    // 初始：页面视图，图谱未挂载（不预拉数据）
    expect(vm.designWikiView).toBe('pages')
    expect(wrapper.findComponent({ name: 'WikiGraphPanel' }).exists()).toBe(false)

    // 设计态切图谱 → 懒挂载；owner 读草稿图（draft=canWrite，入口守卫口径）
    await wrapper.find('[data-test="wiki-design-view-graph"]').trigger('click')
    const designGraph = wrapper.findComponent({ name: 'WikiGraphPanel' })
    expect(designGraph.exists()).toBe(true)
    expect(designGraph.props('artifactId')).toBe('art-1')
    expect(designGraph.props('draft')).toBe(true)

    // 图谱「打开页面」→ 切回页面视图（编辑器 v-show 常驻，状态不丢）
    designGraph.vm.$emit('open-page', 'home')
    await flushPromises()
    expect(vm.designWikiView).toBe('pages')

    // 运行态切图谱 → 恒 draft=false（正式版图，viewer 不可借图谱读草稿）
    vm.activeTab = 'runtime'
    await flushPromises()
    await wrapper.find('[data-test="wiki-runtime-view-graph"]').trigger('click')
    const runtimeGraphs = wrapper.findAllComponents({ name: 'WikiGraphPanel' })
      .filter(g => g.props('draft') === false)   // 设计态图 draft=true（owner）
    expect(runtimeGraphs).toHaveLength(1)   // 运行态独立懒挂载
    runtimeGraphs[0].vm.$emit('open-page', 'home')
    await flushPromises()
    expect(vm.runtimeWikiView).toBe('pages')

    // 表格制品：两内容区均无「图谱」入口（66号：表格不加此入口）
    state.api.get.mockReset().mockResolvedValue({ ...artifactDetail, kind: 'table' })
    const tableDetail = mountDetail()
    await flushPromises()
    expect(tableDetail.find('[data-test="wiki-design-view-graph"]').exists()).toBe(false)
    expect(tableDetail.find('[data-test="wiki-runtime-view-graph"]').exists()).toBe(false)
  })
})

describe('artifact detail view — D01 头部服务版本与待发布并存表述（66号 §5）', () => {
  /** 头部 tag 区文本（标题不含版本语义，tag 区是并存表述的落点）。 */
  const headerTags = (wrapper: VueWrapper): string =>
    wrapper.find('.artifact-detail__head-left').text()

  it('已发布 r1 同时有草稿：头部双信息「r1 正在服务」+「另有待发布内容」', async () => {
    state.api.get.mockReset().mockResolvedValue(
      { ...artifactDetail, current_revision: 1, draft_open: true })
    const wrapper = mountDetail()
    await flushPromises()

    expect(headerTags(wrapper)).toContain('r1 正在服务')
    expect(headerTags(wrapper)).toContain('另有待发布内容')
  })

  it('已发布且无草稿：只显示「rN 正在服务」，不出现待发布/草稿措辞', async () => {
    state.api.get.mockReset().mockResolvedValue(
      { ...artifactDetail, current_revision: 3, draft_open: false })
    const wrapper = mountDetail()
    await flushPromises()

    expect(headerTags(wrapper)).toContain('r3 正在服务')
    expect(headerTags(wrapper)).not.toContain('另有待发布内容')
    expect(headerTags(wrapper)).not.toContain('draft 开放中')
  })

  it('rev0 未发布：只显示草稿态，无「正在服务」表述', async () => {
    state.api.get.mockReset().mockResolvedValue({
      ...artifactDetail, status: 'draft', current_revision: 0,
      draft_open: true, schema: [], sample_rows: [],
    })
    const wrapper = mountDetail()
    await flushPromises()

    expect(headerTags(wrapper)).not.toContain('正在服务')
    expect(headerTags(wrapper)).toContain('draft 开放中')
    expect(headerTags(wrapper)).not.toContain('另有待发布内容')
  })

  it('已停用制品不再宣称「正在服务」（deprecate 后 Agent 侧不可见）', async () => {
    state.api.get.mockReset().mockResolvedValue(
      { ...artifactDetail, status: 'deprecated', current_revision: 2, draft_open: false })
    const wrapper = mountDetail()
    await flushPromises()

    expect(headerTags(wrapper)).not.toContain('正在服务')
    expect(headerTags(wrapper)).toContain('已停用')
  })

  it('已停用+rev>0+草稿开放：草稿措辞与 servingRevisionLabel 同源回落「draft 开放中」，不悬空「另有待发布内容」', async () => {
    // Task 3.3 审查 Minor 修复：原以 current_revision>0 判「另有待发布内容」，
    // deprecated 下无「正在服务」表述，「另有待发布」措辞悬空——改与
    // servingRevisionLabel 同源（其为 null 的语境一律「draft 开放中」）
    state.api.get.mockReset().mockResolvedValue(
      { ...artifactDetail, status: 'deprecated', current_revision: 2, draft_open: true })
    const wrapper = mountDetail()
    await flushPromises()

    expect(headerTags(wrapper)).not.toContain('正在服务')
    expect(headerTags(wrapper)).not.toContain('另有待发布内容')
    expect(headerTags(wrapper)).toContain('draft 开放中')
  })
})

describe('artifact detail view — C01 运行态说明卡子组件（ArtifactUsagePanel）', () => {
  it('运行态说明区挂载子组件并透传制品（含 artifact_ref），不内联重复渲染', async () => {
    state.api.get.mockReset().mockResolvedValue({ ...artifactDetail, artifact_ref: 'art:art-1' })
    const wrapper = mountDetail()
    await flushPromises()

    const panel = wrapper.findComponent({ name: 'ArtifactUsagePanel' })
    expect(panel.exists()).toBe(true)
    const passed = panel.props('artifact') as Record<string, unknown>
    expect(passed.artifact_id).toBe('art-1')
    expect(passed.artifact_ref).toBe('art:art-1')
    expect(passed.kind).toBe('table')
    expect(passed.status).toBe('published')

    // 说明卡内容（描述/使用说明/消费方式）整体迁入子组件——详情页不再内联渲染
    expect(wrapper.text()).not.toContain('查责任域')

    // wiki 制品同样透传（消费方式文案在子组件按 kind 分派）
    state.api.get.mockReset().mockResolvedValue(
      { ...artifactDetail, kind: 'wiki', artifact_ref: 'art:art-1' })
    const wikiDetail = mountDetail()
    await flushPromises()
    const wikiPassed = wikiDetail.findComponent({ name: 'ArtifactUsagePanel' })
      .props('artifact') as Record<string, unknown>
    expect(wikiPassed.kind).toBe('wiki')
  })
})
