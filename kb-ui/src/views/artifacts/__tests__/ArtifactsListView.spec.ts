import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  domain: {
    currentDomain: 'plant-a',
    enabledDomains: [
      { domain_id: 'plant-a', enabled: true },
      { domain_id: 'plant-b', enabled: true },
    ],
  },
  router: { push: vi.fn() },
  api: {
    list: vi.fn(), create: vi.fn(),
  },
  ui: { confirm: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/stores/domain', () => ({ useDomainStore: () => state.domain }))
vi.mock('vue-router', () => ({ useRouter: () => state.router }))
vi.mock('element-plus', () => ({
  ElMessageBox: { confirm: state.ui.confirm },
  ElMessage: { success: state.ui.success, error: state.ui.error, warning: state.ui.warning },
}))

import ArtifactsListView from '../ArtifactsListView.vue'

interface ListVm {
  artifacts: unknown[]
  form: { title: string; description: string; domainId: string; kind: string }
  createVisible: boolean
  openCreate: () => void
  createArtifact: () => Promise<void>
  statusTagType: (status: string) => 'info' | 'success' | 'danger'
  statusLabel: (status: string) => string
}

// vitest 链路无 unplugin-vue-components：模板里的 el-radio-group 是未解析的
// 原生元素，v-model 事件无法经组件通道驱动——注册最小具名桩让 resolveComponent
// 命中（shallowMount 再包自动桩，data-test 随 attrs 透传到桩根元素）。
const ElRadioGroupStub = {
  name: 'ElRadioGroup',
  props: { modelValue: { type: String, default: '' } },
  emits: ['update:modelValue'],
  template: '<div class="el-radio-group"><slot /></div>',
}

function mountList(): VueWrapper {
  return shallowMount(ArtifactsListView, {
    global: { components: { ElRadioGroup: ElRadioGroupStub } },
  })
}

const artifacts = [
  {
    artifact_id: 'art-draft', domain_id: 'plant-a', kind: 'table',
    title: '割接 MOP 表', description: '', usage_note: '',
    owner_user_id: 'u1', status: 'draft', current_revision: 0, draft_open: false,
    created_at: '2026-10-16T08:00:00+00:00', updated_at: '2026-10-16T08:00:00+00:00',
    my_role: 'owner',
  },
  {
    artifact_id: 'art-pub', domain_id: 'plant-a', kind: 'table',
    title: '责任域表', description: 'd', usage_note: '查责任域',
    owner_user_id: 'u2', status: 'published', current_revision: 3, draft_open: true,
    created_at: '2026-10-15T08:00:00+00:00', updated_at: '2026-10-16T09:30:00+00:00',
    my_role: 'viewer',
  },
  {
    artifact_id: 'art-dep', domain_id: 'plant-a', kind: 'table',
    title: '旧表', description: '', usage_note: '',
    owner_user_id: 'u1', status: 'deprecated', current_revision: 2, draft_open: false,
    created_at: '2026-10-14T08:00:00+00:00', updated_at: '2026-10-14T08:00:00+00:00',
    my_role: 'editor',
  },
]

describe('artifacts list view', () => {
  beforeEach(() => {
    state.domain.currentDomain = 'plant-a'
    state.router.push.mockReset()
    state.api.list.mockReset().mockResolvedValue(artifacts)
    state.api.create.mockReset().mockResolvedValue({ ...artifacts[0], artifact_id: 'art_new' })
    state.ui.success.mockReset()
    state.ui.error.mockReset()
    state.ui.warning.mockReset()
  })

  it('loads artifacts on mount and renders rows', async () => {
    const wrapper = mountList()
    await flushPromises()

    expect(state.api.list).toHaveBeenCalledTimes(1)
    // el-table 在 shallowMount 下被 stub，行数据断言走 vm（渲染链路由状态驱动）
    expect(wrapper.text()).toContain('我参与的')
    const vm = wrapper.vm as unknown as ListVm
    expect(vm.artifacts).toHaveLength(3)
    expect(vm.artifacts.map(a => (a as { title: string }).title)).toEqual([
      '割接 MOP 表', '责任域表', '旧表',
    ])
  })

  it('maps artifact status to grey/green/red tags with labels', async () => {
    const wrapper = mountList()
    await flushPromises()
    const vm = wrapper.vm as unknown as ListVm

    expect(vm.statusTagType('draft')).toBe('info')
    expect(vm.statusTagType('published')).toBe('success')
    expect(vm.statusTagType('deprecated')).toBe('danger')
    expect(vm.statusLabel('draft')).toBe('草稿')
    expect(vm.statusLabel('published')).toBe('已发布')
    expect(vm.statusLabel('deprecated')).toBe('已停用')
  })

  it('creates an artifact with the current domain and routes into its detail page', async () => {
    const wrapper = mountList()
    await flushPromises()
    const vm = wrapper.vm as unknown as ListVm

    vm.form = { title: '  新制品  ', description: ' 说明 ', domainId: 'plant-a', kind: 'table' }
    await vm.createArtifact()

    // codex二审功能项③：create payload 显式带当前域（不再依赖后端绑定域兜底）；
    // 类型默认 table（不选 radio 时的缺省请求体口径）
    expect(state.api.create).toHaveBeenCalledWith({
      title: '新制品', description: '说明', domain_id: 'plant-a', kind: 'table',
    })
    expect(state.router.push).toHaveBeenCalledWith({
      name: 'artifact-detail', params: { artifactId: 'art_new' },
    })
  })

  it('sends kind=wiki when the Wiki type is picked in the create dialog', async () => {
    const wrapper = mountList()
    await flushPromises()
    const vm = wrapper.vm as unknown as ListVm

    // 打开对话框：openCreate 重置表单（kind 回到默认 table）
    vm.openCreate()
    await nextTick()
    expect(vm.createVisible).toBe(true)
    expect(vm.form.kind).toBe('table')

    // shallowMount 下 el-radio-group 为桩（data-test 透传）——经桩 emit
    // update:modelValue 模拟选择 wiki（v-model 回写 form.kind）
    const radioGroup = wrapper.findComponent('[data-test="create-artifact-kind"]')
    expect(radioGroup.exists()).toBe(true)
    // CSS 选择器重载的返回类型是 WrapperLike（可能命中原生元素）——此处已由
    // mountList 注册具名桩，断言存在后收窄为 VueWrapper 走组件事件通道
    ;(radioGroup as unknown as VueWrapper).vm.$emit('update:modelValue', 'wiki')
    await nextTick()
    expect(vm.form.kind).toBe('wiki')

    vm.form.title = '割接 Wiki 手册'
    await vm.createArtifact()

    expect(state.api.create).toHaveBeenCalledWith(expect.objectContaining({
      title: '割接 Wiki 手册', domain_id: 'plant-a', kind: 'wiki',
    }))
  })

  it('blocks creation when the target domain is unknown', async () => {
    state.domain.currentDomain = ''
    const wrapper = mountList()
    await flushPromises()
    const vm = wrapper.vm as unknown as ListVm

    vm.form = { title: '无域制品', description: '', domainId: '', kind: 'table' }
    await vm.createArtifact()

    expect(state.ui.warning).toHaveBeenCalledWith(expect.stringContaining('归属知识域'))
    expect(state.api.create).not.toHaveBeenCalled()
    expect(state.router.push).not.toHaveBeenCalled()
  })

  it('blocks empty title and surfaces api errors without routing', async () => {
    const wrapper = mountList()
    await flushPromises()
    const vm = wrapper.vm as unknown as ListVm

    vm.form = { title: '   ', description: '', domainId: 'plant-a', kind: 'table' }
    await vm.createArtifact()
    expect(state.ui.warning).toHaveBeenCalled()
    expect(state.api.create).not.toHaveBeenCalled()

    state.api.create.mockRejectedValueOnce({
      response: { status: 422, data: { detail: 'domain_id_required: x' } },
    })
    vm.form = { title: '无域制品', description: '', domainId: 'plant-a', kind: 'table' }
    await vm.createArtifact()
    expect(state.router.push).not.toHaveBeenCalled()
    expect(state.ui.error).toHaveBeenCalled()
  })
})
