import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  domain: { currentDomain: 'plant-a' },
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
  form: { title: string; description: string }
  createArtifact: () => Promise<void>
  statusTagType: (status: string) => 'info' | 'success' | 'danger'
  statusLabel: (status: string) => string
}

function mountList(): VueWrapper {
  return shallowMount(ArtifactsListView)
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

  it('creates an artifact and routes into its detail page', async () => {
    const wrapper = mountList()
    await flushPromises()
    const vm = wrapper.vm as unknown as ListVm

    vm.form = { title: '  新制品  ', description: ' 说明 ' }
    await vm.createArtifact()

    expect(state.api.create).toHaveBeenCalledWith({
      title: '新制品', description: '说明',
    })
    expect(state.router.push).toHaveBeenCalledWith({
      name: 'artifact-detail', params: { artifactId: 'art_new' },
    })
  })

  it('blocks empty title and surfaces api errors without routing', async () => {
    const wrapper = mountList()
    await flushPromises()
    const vm = wrapper.vm as unknown as ListVm

    vm.form = { title: '   ', description: '' }
    await vm.createArtifact()
    expect(state.ui.warning).toHaveBeenCalled()
    expect(state.api.create).not.toHaveBeenCalled()

    state.api.create.mockRejectedValueOnce({
      response: { status: 422, data: { detail: 'domain_id_required: x' } },
    })
    vm.form = { title: '无域制品', description: '' }
    await vm.createArtifact()
    expect(state.router.push).not.toHaveBeenCalled()
    expect(state.ui.error).toHaveBeenCalled()
  })
})
