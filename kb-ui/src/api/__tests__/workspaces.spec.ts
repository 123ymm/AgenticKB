import { beforeEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  domain: { currentDomain: 'plant/a' },
  requestInterceptor: undefined as ((config: Record<string, unknown>) => Record<string, unknown>) | undefined,
  requests: [] as Array<{ method: string; url: string; config: Record<string, unknown>; body?: unknown }>,
}))

vi.mock('@/stores/domain', () => ({ useDomainStore: () => state.domain }))
vi.mock('axios', () => ({
  default: {
    create: () => {
      const dispatch = async (method: string, url: string, body?: unknown, initial: Record<string, unknown> = {}) => {
        const config = state.requestInterceptor?.({ ...initial, url, method }) ?? { ...initial, url, method }
        state.requests.push({ method, url, config, body })
        if (url === '/api/workspaces' && method === 'get') return { data: { workspaces: [{ id: 'wsp_1' }] } }
        if (url === '/api/workspaces/wsp_1' && method === 'delete') return { data: { id: 'wsp_1', status: 'deleted' } }
        if (url.endsWith('/members')) return { data: { members: [{ user_id: 'u1', role: 'viewer' }] } }
        if (url.endsWith('/runs')) return { data: { runs: [{ id: 'run_9', status: 'completed' }] } }
        if (url.endsWith('/sources')) return { data: { sources: [{ node_id: 's1', connector: 'kb_dir' }] } }
        if (url.endsWith('/graph')) return { data: { graph: { nodes: [] }, graph_revision: 2, batch_no: 1 } }
        if (url.endsWith('/outputs/current')) return { data: { outputs: { 's1:out': { row_count: 3 } } } }
        return { data: { id: 'wsp_1' } }
      }
      return {
        interceptors: { request: { use: (fn: typeof state.requestInterceptor) => { state.requestInterceptor = fn } } },
        get: (url: string, config?: Record<string, unknown>) => dispatch('get', url, undefined, config),
        post: (url: string, body?: unknown, config?: Record<string, unknown>) => dispatch('post', url, body, config),
        put: (url: string, body?: unknown, config?: Record<string, unknown>) => dispatch('put', url, body, config),
        patch: (url: string, body?: unknown, config?: Record<string, unknown>) => dispatch('patch', url, body, config),
        delete: (url: string, config?: Record<string, unknown>) => dispatch('delete', url, undefined, config),
      }
    },
  },
}))

import { useWorkspacesApi } from '@/api/workspaces'

const file = new File(['x'], '数据.xlsx', {
  type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
})

describe('workspaces API', () => {
  beforeEach(() => {
    state.requests.length = 0
    state.requestInterceptor = undefined
    state.domain.currentDomain = 'plant/a'
  })

  it('uses the active-domain proxy path without attaching a domain query', async () => {
    const api = useWorkspacesApi()

    await api.listWorkspaces('plant/a')

    expect(state.requests[0]).toMatchObject({ method: 'get', url: '/api/workspaces' })
    expect(state.requests[0].config.baseURL).toBe('/api/control-plane/api/v1/proxy/plant%2Fa/mining')
    expect(state.requests[0].config.params).toEqual({ domain_id: 'plant/a' })
    expect(state.requests[0].config.params).not.toHaveProperty('domain')
  })

  it('exposes the full workspace REST surface with correct paths', async () => {
    const api = useWorkspacesApi()

    await api.listWorkspaces()
    await api.createWorkspace({ name: '网络制品工坊', description: '做表', domain: 'plant/a' })
    await api.getWorkspace('wsp_1')
    await api.patchWorkspace('wsp_1', { name: '改名' })
    await api.deleteWorkspace('wsp_1')
    await api.listMembers('wsp_1')
    await api.addMember('wsp_1', 'u2', 'editor')
    await api.updateMemberRole('wsp_1', 'u2', 'viewer')
    await api.removeMember('wsp_1', 'u2')
    await api.getGraph('wsp_1')
    await api.putGraph('wsp_1', { nodes: [] }, 3)
    await api.listSources('wsp_1')
    await api.upsertSourceKbDir('wsp_1', { node_id: 's1', kb_id: 'kb-1', directories: ['docs/net'] })
    await api.uploadSourceExcel('wsp_1', { node_id: 's1', file })
    await api.deleteSource('wsp_1', 's1')
    await api.startRun('wsp_1', { scope: 'workspace' })
    await api.listRuns('wsp_1')
    await api.getRunStatus('wsp_1', 'run_9')
    await api.cancelRun('wsp_1', 'run_9')
    await api.getCurrentOutputs('wsp_1')
    await api.trialOutput('wsp_1', 's1', 'out')
    await api.exportDrafts('wsp_1', {
      request_id: 'req-1',
      items: [{ node_id: 's1', port_id: 'out', artifact_name: '网络表' }],
    })

    expect(state.requests.map(({ method, url }) => `${method} ${url}`)).toEqual([
      'get /api/workspaces',
      'post /api/workspaces',
      'get /api/workspaces/wsp_1',
      'patch /api/workspaces/wsp_1',
      'delete /api/workspaces/wsp_1',
      'get /api/workspaces/wsp_1/members',
      'post /api/workspaces/wsp_1/members',
      'patch /api/workspaces/wsp_1/members/u2',
      'delete /api/workspaces/wsp_1/members/u2',
      'get /api/workspaces/wsp_1/graph',
      'put /api/workspaces/wsp_1/graph',
      'get /api/workspaces/wsp_1/sources',
      'post /api/workspaces/wsp_1/sources',
      'post /api/workspaces/wsp_1/sources/excel',
      'delete /api/workspaces/wsp_1/sources/s1',
      'post /api/workspaces/wsp_1/runs',
      'get /api/workspaces/wsp_1/runs',
      'get /api/workspaces/wsp_1/runs/run_9',
      'post /api/workspaces/wsp_1/runs/run_9/cancel',
      'get /api/workspaces/wsp_1/outputs/current',
      'post /api/workspaces/wsp_1/outputs/s1/out/trial',
      'post /api/workspaces/wsp_1/exports',
    ])
  })

  it('builds create/patch bodies with domain_id and optimistic-lock tokens', async () => {
    const api = useWorkspacesApi()

    await api.createWorkspace({ name: '工坊', description: 'd', domain: 'plant/a' })
    expect(state.requests[0].body).toEqual({
      name: '工坊', description: 'd', domain_id: 'plant/a',
    })

    // description 缺省不发空串（后端 max 2000，前端只传有值字段）
    await api.createWorkspace({ name: '工坊2' })
    expect(state.requests[1].body).toEqual({ name: '工坊2', domain_id: undefined })

    await api.patchWorkspace('wsp_1', {
      name: '新名', description: 'nd',
      expected_updated_at: '2026-11-16T00:00:00Z',
    })
    expect(state.requests[2].body).toEqual({
      name: '新名', description: 'nd', expected_updated_at: '2026-11-16T00:00:00Z',
    })
  })

  it('sends putGraph as whole-replace with expected_revision lock token', async () => {
    const api = useWorkspacesApi()
    const graph = {
      nodes: [{
        id: 's1', op: 'source', name: '资料', config: {},
        inputs: [], ports: [{ id: 'out', name: '出', kind: 'table' }], x: 0, y: 0,
      }],
    }

    await api.putGraph('wsp_1', graph, 3)

    expect(state.requests[0]).toMatchObject({ method: 'put', url: '/api/workspaces/wsp_1/graph' })
    expect(state.requests[0].body).toEqual({ graph, expected_revision: 3 })
  })

  it('builds source calls: kb_dir JSON body and excel multipart fields', async () => {
    const api = useWorkspacesApi()

    await api.upsertSourceKbDir('wsp_1', { node_id: 's1', kb_id: 'kb-1', directories: ['docs/net'] })
    expect(state.requests[0].body).toEqual({
      connector: 'kb_dir', node_id: 's1', kb_id: 'kb-1', directories: ['docs/net'],
    })

    await api.uploadSourceExcel('wsp_1', { node_id: 's1', file })
    expect(state.requests[1].method).toBe('post')
    expect(state.requests[1].url).toBe('/api/workspaces/wsp_1/sources/excel')
    expect(state.requests[1].body).toBeInstanceOf(FormData)
    expect((state.requests[1].body as FormData).get('file')).toBe(file)
    expect((state.requests[1].body as FormData).get('node_id')).toBe('s1')
    expect((state.requests[1].body as FormData).get('sheet_name')).toBeNull()

    await api.uploadSourceExcel('wsp_1', { node_id: 's1', file, sheet_name: 'Sheet1' })
    expect((state.requests[2].body as FormData).get('sheet_name')).toBe('Sheet1')
  })

  it('builds run calls with scope body, limit query and node targeting', async () => {
    const api = useWorkspacesApi()

    await api.startRun('wsp_1', { scope: 'node', target_node_id: 'op_2' })
    expect(state.requests[0].body).toEqual({ scope: 'node', target_node_id: 'op_2' })

    // workspace 级不带 target_node_id 键（undefined 经 JSON 序列化丢弃）
    await api.startRun('wsp_1', { scope: 'workspace' })
    expect(state.requests[1].body).toEqual({ scope: 'workspace' })

    await api.listRuns('wsp_1')
    expect(state.requests[2].config.params).toBeUndefined()

    await api.listRuns('wsp_1', 20)
    expect(state.requests[3].config.params).toEqual({ limit: 20 })
  })

  it('encodes arbitrary node/port path segments and passes export payload through', async () => {
    const api = useWorkspacesApi()

    await api.deleteSource('wsp_1', '源节点')
    expect(state.requests[0].url).toBe(`/api/workspaces/wsp_1/sources/${encodeURIComponent('源节点')}`)

    await api.trialOutput('wsp_1', '源节点', '出端口')
    expect(state.requests[1].url).toBe(
      `/api/workspaces/wsp_1/outputs/${encodeURIComponent('源节点')}/${encodeURIComponent('出端口')}/trial`)

    await api.exportDrafts('wsp_1', {
      request_id: 'req-1',
      items: [{
        node_id: 's1', port_id: 'out', artifact_name: '网络表', owner_user_id: 'u2',
      }],
    })
    expect(state.requests[2].body).toEqual({
      request_id: 'req-1',
      items: [{ node_id: 's1', port_id: 'out', artifact_name: '网络表', owner_user_id: 'u2' }],
    })
  })

  it('unwraps workspaces/members/runs/sources envelopes', async () => {
    const api = useWorkspacesApi()

    await expect(api.listWorkspaces()).resolves.toEqual([{ id: 'wsp_1' }])
    await expect(api.listMembers('wsp_1')).resolves.toEqual([{ user_id: 'u1', role: 'viewer' }])
    await expect(api.listRuns('wsp_1')).resolves.toEqual([{ id: 'run_9', status: 'completed' }])
    await expect(api.listSources('wsp_1')).resolves.toEqual([{ node_id: 's1', connector: 'kb_dir' }])
    await expect(api.getCurrentOutputs('wsp_1')).resolves.toEqual({ outputs: { 's1:out': { row_count: 3 } } })
    // DELETE 两态（用户反馈问题1）：已归档工作区 → 真删只回 {id, status}
    await expect(api.deleteWorkspace('wsp_1')).resolves.toEqual({ id: 'wsp_1', status: 'deleted' })
  })
})
