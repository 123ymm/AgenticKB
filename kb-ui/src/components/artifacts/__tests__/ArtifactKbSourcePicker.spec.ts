import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import KbFileManager from '@/components/kb/KbFileManager.vue'

const api = vi.hoisted(() => ({ listKbs: vi.fn() }))
vi.mock('@/api/kb', () => ({ useKbApi: () => api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => '无访问权限' }))
import ArtifactKbSourcePicker from '../ArtifactKbSourcePicker.vue'

async function mountPicker() {
  const wrapper = shallowMount(ArtifactKbSourcePicker, {
    props: { domain: 'domain-A', active: true },
    global: { stubs: {
      ElButton: { template: '<button><slot /></button>' },
      ElSelect: { name: 'ElSelect', template: '<div><slot /></div>' },
      ElAlert: { name: 'ElAlert', template: '<div />' },
      ElEmpty: true,
    } },
  })
  await flushPromises()
  return wrapper
}

describe('知识库来源选择', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listKbs.mockResolvedValue([{ id: 'kb-A', name: '资料库' }, { id: 'kb-B', name: '产品库' }])
  })

  it('按资产所属领域查询，复用只读文件管理器，切库清空选择', async () => {
    const wrapper = await mountPicker()
    expect(api.listKbs).toHaveBeenCalledWith('domain-A')
    wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'kb-A')
    await flushPromises()
    const manager = wrapper.findComponent(KbFileManager)
    expect(manager.props()).toMatchObject({ kbId: 'kb-A', selectionOnly: true, canWrite: false })
    manager.vm.$emit('selection-change', [{ id: 'doc-A', document_name: '测试.xlsx', status: 'mined' }])
    await flushPromises()
    expect(wrapper.get('[data-test="source-count"]').text()).toContain('1')
    await wrapper.get('[data-test="confirm-kb-source"]').trigger('click')
    expect(wrapper.findAllComponents({ name: 'ElAlert' }).some(alert => alert.attributes('type') === 'success')).toBe(true)
    wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'kb-B')
    await flushPromises()
    expect(wrapper.get('[data-test="source-count"]').text()).toContain('0')
    expect(wrapper.findAllComponents({ name: 'ElAlert' }).some(alert => alert.attributes('type') === 'success')).toBe(false)
  })

  it('展开目录期间不能确认；后续调整使确认结果失效', async () => {
    const wrapper = await mountPicker()
    wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'kb-A')
    await flushPromises()
    const manager = wrapper.findComponent(KbFileManager)
    manager.vm.$emit('selection-change', [{ id: 'doc-A', document_name: '测试.xlsx' }])
    manager.vm.$emit('selection-loading', true)
    await flushPromises()
    expect(wrapper.get('[data-test="confirm-kb-source"]').attributes('disabled')).toBeDefined()
    manager.vm.$emit('selection-loading', false)
    await flushPromises()
    await wrapper.get('[data-test="confirm-kb-source"]').trigger('click')
    manager.vm.$emit('selection-change', [])
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'ElAlert' }).some(alert => alert.attributes('type') === 'success')).toBe(false)
  })

  it('知识库加载失败显示错误，不残留可选列表', async () => {
    api.listKbs.mockRejectedValue(new Error('403'))
    const wrapper = await mountPicker()
    expect(wrapper.findComponent(KbFileManager).exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'ElAlert' }).attributes('title')).toContain('无访问权限')
  })
})
