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
        if (url.endsWith('/revisions')) return { data: { revisions: [{ artifact_id: 'a1', revision: 1 }] } }
        if (url.endsWith('/members')) return { data: { members: [{ user_id: 'u1', role: 'viewer' }] } }
        if (url.endsWith('/batches')) return { data: { batches: [{ batch_id: 'b1' }] } }
        if (url.endsWith('/table/columns')) return { data: { columns: [{ column_id: 'c1' }] } }
        if (url.includes('/table/rows')) return { data: { rows: [], total: 0, page: 1, size: 50 } }
        if (url.endsWith('/diff')) return { data: { added: [], removed: [], changed: [], added_count: 0, removed_count: 0, changed_count: 0 } }
        if (url === '/api/artifacts') return { data: { artifacts: [{ artifact_id: 'a1' }] } }
        return { data: { artifact_id: 'a1' } }
      }
      return {
        interceptors: { request: { use: (fn: typeof state.requestInterceptor) => { state.requestInterceptor = fn } } },
        get: (url: string, config?: Record<string, unknown>) => dispatch('get', url, undefined, config),
        post: (url: string, body?: unknown, config?: Record<string, unknown>) => dispatch('post', url, body, config),
        patch: (url: string, body?: unknown, config?: Record<string, unknown>) => dispatch('patch', url, body, config),
        delete: (url: string, config?: Record<string, unknown>) => dispatch('delete', url, undefined, config),
      }
    },
  },
}))

import { useArtifactsApi } from '@/api/artifacts'

const file = new File(['x'], 'A.xlsx', { type: 'application/octet-stream' })

describe('artifacts API', () => {
  beforeEach(() => {
    state.requests.length = 0
    state.requestInterceptor = undefined
    state.domain.currentDomain = 'plant/a'
  })

  it('uses the active-domain proxy path without attaching a domain query', async () => {
    const api = useArtifactsApi()

    await api.list({ domain_id: 'plant/a' })

    expect(state.requests[0]).toMatchObject({ method: 'get', url: '/api/artifacts' })
    expect(state.requests[0].config.baseURL).toBe('/api/control-plane/api/v1/proxy/plant%2Fa/mining')
    expect(state.requests[0].config.params).toEqual({ domain_id: 'plant/a' })
    expect(state.requests[0].config.params).not.toHaveProperty('domain')
  })

  it('exposes the full 61号 REST surface with correct paths', async () => {
    const api = useArtifactsApi()

    await api.create({ title: 'MOP 表' })
    await api.list()
    await api.get('a1')
    await api.updateMeta('a1', { usage_note: '查责任域' })
    await api.remove('a1')
    await api.publish('a1', { change_summary: '首版' })
    await api.rollback('a1', 1)
    await api.deprecate('a1')
    await api.republish('a1')
    await api.discardDraft('a1')
    await api.diff('a1')
    await api.listRevisions('a1')
    await api.listMembers('a1')
    await api.addMember('a1', 'u2', 'editor')
    await api.updateMember('a1', 'u2', 'viewer')
    await api.removeMember('a1', 'u2')
    await api.listBatches('a1')
    await api.listColumns('a1')
    await api.addColumn('a1', { column_name: 'MOP', column_type: 'text' })
    await api.updateColumn('a1', 'c1', { status: 'dropped' })
    await api.setPrimaryKey('a1', ['c1'])
    await api.listRows('a1', { draft: true, page: 2, size: 20 })
    await api.updateCell('a1', 'NC-1', { column: 'L1分类', value: '数据业务' })
    await api.insertRow('a1', { data: { MOP: 'NC-2' } })
    await api.batchAssign('a1', { column: 'L1分类', value: 'x', where: [] })
    await api.deleteRow('a1', 'NC-1')

    expect(state.requests.map(({ method, url }) => `${method} ${url}`)).toEqual([
      'post /api/artifacts',
      'get /api/artifacts',
      'get /api/artifacts/a1',
      'patch /api/artifacts/a1',
      'delete /api/artifacts/a1',
      'post /api/artifacts/a1/publish',
      'post /api/artifacts/a1/rollback/1',
      'post /api/artifacts/a1/deprecate',
      'post /api/artifacts/a1/republish',
      'post /api/artifacts/a1/discard',
      'get /api/artifacts/a1/diff',
      'get /api/artifacts/a1/revisions',
      'get /api/artifacts/a1/members',
      'post /api/artifacts/a1/members',
      'patch /api/artifacts/a1/members/u2',
      'delete /api/artifacts/a1/members/u2',
      'get /api/artifacts/a1/batches',
      'get /api/artifacts/a1/table/columns',
      'post /api/artifacts/a1/table/columns',
      'patch /api/artifacts/a1/table/columns/c1',
      'post /api/artifacts/a1/table/primary-key',
      'get /api/artifacts/a1/table/rows',
      'patch /api/artifacts/a1/table/rows/NC-1',
      'post /api/artifacts/a1/table/rows',
      'post /api/artifacts/a1/table/rows/batch-assign',
      'delete /api/artifacts/a1/table/rows/NC-1',
    ])
  })

  it('builds wizard upload/preview/import calls with FormData and payload', async () => {
    const api = useArtifactsApi()

    await api.uploadTable('a1', file)
    const upload = state.requests[0]
    expect(upload.method).toBe('post')
    expect(upload.url).toBe('/api/artifacts/a1/table/upload')
    expect(upload.body).toBeInstanceOf(FormData)
    expect((upload.body as FormData).get('file')).toBe(file)

    await api.getPreview('a1', 'tok123')
    expect(state.requests[1].url).toBe('/api/artifacts/a1/table/preview/tok123')

    await api.importTable('a1', {
      preview_token: 'tok123',
      plan: { column_ops: [] },
      conflict_policy: { type_error: 'tolerant' },
    })
    expect(state.requests[2].body).toEqual({
      preview_token: 'tok123',
      plan: { column_ops: [] },
      conflict_policy: { type_error: 'tolerant' },
    })

    await api.listColumns('a1', { includeDropped: true })
    expect(state.requests[3].config.params).toEqual({ include_dropped: true })

    await api.listColumns('a1', { runtime: true })
    expect(state.requests[4].config.params).toEqual({ runtime: true })

    // 无选项 → 不附带查询参数
    await api.listColumns('a1')
    expect(state.requests[5].config.params).toBeUndefined()
  })

  it('passes optimistic-lock fields and row query params through', async () => {
    const api = useArtifactsApi()

    await api.publish('a1', { change_summary: '二版', expected_updated_at: '2026-10-16T00:00:00Z' })
    expect(state.requests[0].body).toEqual({
      change_summary: '二版', expected_updated_at: '2026-10-16T00:00:00Z',
    })

    await api.listRows('a1', { draft: true, page: 3, size: 10 })
    expect(state.requests[1].config.params).toEqual({ draft: true, page: 3, size: 10 })

    await api.deleteRow('a1', 'NC-1', '2026-10-16T00:00:00Z')
    expect(state.requests[2].config.data).toEqual({ expected_updated_at: '2026-10-16T00:00:00Z' })
  })

  it('unwraps revisions/members/batches/columns envelopes', async () => {
    const api = useArtifactsApi()

    await expect(api.listRevisions('a1')).resolves.toHaveLength(1)
    await expect(api.listMembers('a1')).resolves.toEqual([{ user_id: 'u1', role: 'viewer' }])
    await expect(api.listBatches('a1')).resolves.toEqual([{ batch_id: 'b1' }])
    await expect(api.listColumns('a1')).resolves.toEqual([{ column_id: 'c1' }])
    await expect(api.list()).resolves.toEqual([{ artifact_id: 'a1' }])
  })
})
