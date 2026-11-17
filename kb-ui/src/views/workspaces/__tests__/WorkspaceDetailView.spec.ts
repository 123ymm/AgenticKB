import { describe, expect, it, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

/**
 * 死锁回归钉测（用户反馈 2026-11-17）：run 在飞时重进页面，
 * resumeInflightRun 立起 running 标志后**必须启动轮询循环**——
 * running 的唯一放下途径是轮询发现终态，循环不转 = 服务端跑完了
 * 前端也不知道，运行/导出按钮永久禁用。
 */

const pollingStart = vi.fn()
const apiMocks = {
  getWorkspace: vi.fn(),
  getGraph: vi.fn(),
  getCurrentOutputs: vi.fn(),
  listSources: vi.fn(),
  listRuns: vi.fn(),
  getRunStatus: vi.fn(),
}

vi.mock('@/api/workspaces', () => ({
  useWorkspacesApi: () => apiMocks,
}))
vi.mock('@/api/proxyClient', () => ({
  apiErrorDetail: async () => 'err',
}))
vi.mock('@/composables/usePolling', () => ({
  usePolling: () => ({ start: pollingStart }),
}))
vi.mock('@/composables/useDocumentTitle', () => ({
  useDocumentTitle: () => {},
}))
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
}))

import WorkspaceDetailView from '../WorkspaceDetailView.vue'

function stubApi(base: Record<string, unknown> = {}) {
  apiMocks.getWorkspace.mockResolvedValue({
    id: 'wsp_x', domain_id: 'd', name: '台', description: '',
    owner_user_id: 'u', owner_username: 'admin', status: 'active',
    graph_revision: 1, batch_no: 1, my_role: 'admin',
    graph_json: null, created_at: '', updated_at: '', ...base,
  })
  apiMocks.getGraph.mockResolvedValue({ graph: { nodes: [] }, graph_revision: 1, batch_no: 1 })
  apiMocks.getCurrentOutputs.mockResolvedValue({ outputs: {} })
  apiMocks.listSources.mockResolvedValue([])
}

const STUBS = {
  WorkspaceStepFlow: { template: '<div />' },
  WorkspaceOutputTable: { template: '<div />' },
  WorkspaceInspector: { template: '<div />' },
  WorkspaceSourceDialog: { template: '<div />' },
  WorkspaceNodeDialog: { template: '<div />' },
  WorkspaceExportDialog: { template: '<div />' },
  EmptyState: { template: '<div />' },
  RouterLink: { template: '<a><slot /></a>' },
}

beforeEach(() => {
  vi.clearAllMocks()
  stubApi()
})

describe('重进页面恢复在飞 run', () => {
  it('立起 running 标志的同时启动轮询循环（防永久死锁）', async () => {
    apiMocks.listRuns.mockResolvedValue([
      { id: 'wsr_1', status: 'running', scope: 'workspace' },
    ])
    const wrapper = mount(WorkspaceDetailView, {
      props: { workspaceId: 'wsp_x' },
      global: { stubs: STUBS },
    })
    await flushPromises()
    expect(wrapper.find('[data-test="run-state"]').text()).toContain('运行中')
    expect(pollingStart).toHaveBeenCalledTimes(1)
  })

  it('最新 run 已终态时不立标志、不启轮询（按钮可用）', async () => {
    apiMocks.listRuns.mockResolvedValue([
      { id: 'wsr_1', status: 'completed', scope: 'workspace' },
    ])
    const wrapper = mount(WorkspaceDetailView, {
      props: { workspaceId: 'wsp_x' },
      global: { stubs: STUBS },
    })
    await flushPromises()
    expect(wrapper.find('[data-test="run-state"]').exists()).toBe(false)
    expect(pollingStart).not.toHaveBeenCalled()
  })
})
