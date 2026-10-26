import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import ArtifactPipelineWorkbench from '../ArtifactPipelineWorkbench.vue'
import ArtifactKbSourcePicker from '../ArtifactKbSourcePicker.vue'

const api = vi.hoisted(() => ({ runs: vi.fn(), snapshot: vi.fn(), extract: vi.fn(), step: vi.fn(), operators: vi.fn() }))
vi.mock('@/api/artifactPipeline', () => ({ useArtifactPipelineApi: () => api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async (e: Error) => e.message }))

const run = { id: 'r1', status: 'completed', started_at: '2026-10-25T00:00:00Z', output: { row_count: 1 } }
const snapshot = { ...run, sources: [{ id: 'doc', document_name: '产品.md' }], rows: [], total: 1, issues: [] }
const wrappers: ReturnType<typeof shallowMount>[] = []
function mount() {
  const wrapper = shallowMount(ArtifactPipelineWorkbench, {
    props: { artifactId: 'a', domain: 'd', active: true },
    global: { directives: { loading: () => {} }, stubs: {
      ElDialog: { template: '<div><slot /></div>' },
      ElButton: { template: '<button><slot /></button>' },
      ElTooltip: { template: '<div><slot /></div>' },
      ElSelect: { name: 'ElSelect', template: '<div><slot /></div>' },
      ElAlert: { name: 'ElAlert', template: '<div />' },
      ElCollapse: true, ElCollapseItem: true, ElTabs: true, ElTabPane: true,
    } },
  })
  wrappers.push(wrapper)
  return wrapper
}

describe('制品 Pipeline 工作台', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.runs.mockResolvedValue([run])
    api.snapshot.mockResolvedValue(snapshot)
    api.extract.mockResolvedValue({ run_id: 'r2' })
    api.step.mockResolvedValue({ run_id: 'r2' })
    api.operators.mockResolvedValue([{ id: 'select', name: '筛选 / 保留列' }])
  })
  afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()) })

  it('重新打开恢复历史快照，未选择新来源不能提交', async () => {
    const wrapper = mount()
    await flushPromises()
    expect(api.snapshot).toHaveBeenCalledWith('a', 'r1', 1)
    expect(wrapper.text()).toContain('表格抽取')
    expect(wrapper.get('[data-test="execute-extraction"]').attributes('disabled')).toBeDefined()
    expect(api.extract).not.toHaveBeenCalled()
  })

  it('确认来源不执行，点击执行才提交文件清单', async () => {
    const wrapper = mount()
    await flushPromises()
    wrapper.findComponent(ArtifactKbSourcePicker).vm.$emit('confirmed', {
      kbId: 'kb', documents: [{ id: 'doc', document_name: '产品.md' }],
    })
    await flushPromises()
    expect(api.extract).not.toHaveBeenCalled()
    expect(wrapper.get('[data-test="execute-extraction"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('[data-test="execute-extraction"]').trigger('click')
    await flushPromises()
    expect(api.extract).toHaveBeenCalledWith('a', 'kb', ['doc'])
    expect(api.snapshot).toHaveBeenLastCalledWith('a', 'r2', 1)
  })

  it('切换历史记录只读取，不重新执行算子', async () => {
    const wrapper = mount()
    await flushPromises()
    const history = wrapper.findAllComponents({ name: 'ElSelect' })[0]!
    history.vm.$emit('update:modelValue', 'old')
    history.vm.$emit('change', 'old')
    await flushPromises()
    expect(api.snapshot).toHaveBeenLastCalledWith('a', 'old', 1)
    expect(api.extract).not.toHaveBeenCalled()
  })

  it('读取失败明确展示错误', async () => {
    api.snapshot.mockRejectedValue(new Error('无访问权限'))
    const wrapper = mount()
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ElAlert' }).attributes('title')).toContain('无访问权限')
  })

  it('底部回看早期步骤保留后续步骤，并可切回末步和其他分支', async () => {
    const r1 = { ...run, step_index: 1 }
    const r2 = { ...run, id: 'r2', parent_run_id: 'r1', step_index: 2 }
    const r3 = { ...run, id: 'r3', parent_run_id: 'r2', step_index: 3 }
    const branch = { ...run, id: 'branch', parent_run_id: 'r1', step_index: 2 }
    api.runs.mockResolvedValue([r3, branch, r2, r1])
    api.snapshot.mockImplementation(async (_id, id) => ({ ...snapshot, ...[r1, r2, r3, branch].find(r => r.id === id) }))
    const wrapper = mount()
    await flushPromises()
    const steps = () => wrapper.findAll('[data-test="step-one"]')
    expect(steps()).toHaveLength(3)
    await steps()[0]!.trigger('click')
    await flushPromises()
    expect(steps()).toHaveLength(3)
    expect(api.snapshot).toHaveBeenLastCalledWith('a', 'r1', 1)
    await wrapper.findAll('button').find(b => b.text() === '刷新')!.trigger('click')
    await flushPromises()
    expect(steps()).toHaveLength(3)
    await steps()[2]!.trigger('click')
    await flushPromises()
    expect(api.snapshot).toHaveBeenCalledWith('a', 'r3', 1)
    const history = wrapper.findAllComponents({ name: 'ElSelect' })[0]!
    history.vm.$emit('update:modelValue', 'branch')
    history.vm.$emit('change', 'branch')
    await flushPromises()
    expect(steps()).toHaveLength(2)
    await steps()[0]!.trigger('click')
    await flushPromises()
    expect(steps()).toHaveLength(2)
    await steps()[1]!.trigger('click')
    await flushPromises()
    expect(api.snapshot).toHaveBeenCalledWith('a', 'branch', 1)
    expect(api.step).not.toHaveBeenCalled()
    expect(api.extract).not.toHaveBeenCalled()
  })

  it('从当前产物继续时只读取输入，执行后才创建线性下一步', async () => {
    const wrapper = mount()
    await flushPromises()
    await wrapper.findAll('button').find(b => b.text().includes('下一步'))!.trigger('click')
    await flushPromises()
    expect(api.step).not.toHaveBeenCalled()
    expect(api.snapshot).toHaveBeenLastCalledWith('a', 'r1', 1, [])
    await wrapper.get('[data-test="execute-extraction"]').trigger('click')
    await flushPromises()
    expect(api.step).toHaveBeenCalledWith('a', 'r1', 'select', expect.objectContaining({ filters: [], keep_columns: [], preview: false }))
    expect(api.extract).not.toHaveBeenCalled()
  })

  it('回看历史后从该步分支，不把最新产物作为输入', async () => {
    api.snapshot.mockImplementation(async (_id, runId) => ({ ...snapshot, id: runId }))
    const wrapper = mount()
    await flushPromises()
    const history = wrapper.findAllComponents({ name: 'ElSelect' })[0]!
    history.vm.$emit('update:modelValue', 'historical')
    history.vm.$emit('change', 'historical')
    await flushPromises()
    await wrapper.findAll('button').find(b => b.text().includes('下一步'))!.trigger('click')
    await flushPromises()
    await wrapper.get('[data-test="execute-extraction"]').trigger('click')
    await flushPromises()
    expect(api.step).toHaveBeenCalledWith('a', 'historical', 'select', expect.any(Object))
  })

  it('试跑样本不能发布或直接继续加工', async () => {
    api.snapshot.mockResolvedValue({ ...snapshot, preview: true, operator: 'llm_columns', parent_run_id: 'input' })
    const wrapper = mount()
    await flushPromises()
    expect(wrapper.findAll('button').find(b => b.text().includes('发布当前步'))!.attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('button').find(b => b.text().includes('下一步'))!.attributes('disabled')).toBeDefined()
  })
})
