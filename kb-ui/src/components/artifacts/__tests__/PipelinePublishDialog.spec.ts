import { beforeEach, describe, expect, it, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import PipelinePublishDialog from '../PipelinePublishDialog.vue'
const api = vi.hoisted(() => ({ targets: vi.fn(), publish: vi.fn() }))
vi.mock('@/api/artifactPipeline', () => ({ useArtifactPipelineApi: () => api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async (e: Error) => e.message }))
function mount() {
  return shallowMount(PipelinePublishDialog, {
    props: { visible: true, artifactId: 'a', snapshot: { id: 'r', status: 'completed', started_at: '', output: {}, sources: [], rows: [], total: 1, issues: [], columns: [{ key: 'name', name: '名称', type: 'text' }] } },
    global: { stubs: {
      ElDialog: { template: '<div><slot /><slot name="footer" /></div>' },
      ElForm: { template: '<div><slot /></div>' }, ElFormItem: { template: '<div><slot /></div>' },
      ElButton: { template: '<button><slot /></button>' }, ElInput: { name: 'ElInput', template: '<input />' },
      RouterLink: true,
    } },
  })
}
describe('Pipeline 发布定版', () => {
  beforeEach(() => { vi.clearAllMocks(); api.targets.mockResolvedValue([]); api.publish.mockResolvedValue({ preview_token: 'token', input_rows: 1, inserted: 1, updated: 0, sample: [] }) })
  it('先预览后确认，确认携带预览令牌；完成后禁止再次点击', async () => {
    const wrapper = mount(); await flushPromises()
    const button = (text: string) => wrapper.findAll('button').find(b => b.text() === text)!
    expect(button('确认发布').attributes('disabled')).toBeDefined()
    await button('预览发布').trigger('click'); await flushPromises()
    expect(api.publish).toHaveBeenLastCalledWith('a', 'r', expect.any(Object), false)
    expect(button('确认发布').attributes('disabled')).toBeUndefined()
    api.publish.mockResolvedValue({ artifact_id: 'target', revision: 1, row_count: 1 })
    await button('确认发布').trigger('click'); await flushPromises()
    expect(api.publish).toHaveBeenLastCalledWith('a', 'r', expect.objectContaining({ preview_token: 'token' }), true)
    expect(wrapper.emitted('published')).toHaveLength(1)
    expect(button('确认发布').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
  it('修改发布配置会失效已有预览，必须重新预览', async () => {
    const wrapper = mount(); await flushPromises()
    await wrapper.findAll('button').find(b => b.text() === '预览发布')!.trigger('click'); await flushPromises()
    wrapper.findAllComponents({ name: 'ElInput' })[0]!.vm.$emit('update:modelValue', '新标题')
    await flushPromises()
    expect(wrapper.findAll('button').find(b => b.text() === '确认发布')!.attributes('disabled')).toBeDefined()
    expect(api.publish).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
  it('默认发布到当前制品，避免无意中额外创建一个资产', async () => {
    api.targets.mockResolvedValue([{ artifact_id: 'a', title: '当前资产', columns: [] }])
    const wrapper = mount(); await flushPromises()
    await wrapper.findAll('button').find(b => b.text() === '预览发布')!.trigger('click'); await flushPromises()
    expect(api.publish).toHaveBeenCalledWith('a', 'r', expect.objectContaining({ target_artifact_id: 'a' }), false)
    wrapper.unmount()
  })
})
