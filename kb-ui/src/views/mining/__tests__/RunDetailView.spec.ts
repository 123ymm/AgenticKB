import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const state = vi.hoisted(() => ({
  store: {
    currentRun: { id: 'r1', status: 'queued', current_stage: 'queued', total_documents: 0,
      committed_count: 0, failed_count: 0, skipped_count: 0, new_count: 0, updated_count: 0 },
    stages: [], documents: [], documentsTotal: 0, documentsPage: 1,
    progress: { total: 0, completed: 0, failed: 0, skipped: 0, processing: 0,
      progress_percent: 0, current_stage: null, stage_summary: {} },
    error: null,
    fetchRunDetail: vi.fn(async () => undefined),
    fetchProgress: vi.fn(async () => undefined),
    fetchRunDocuments: vi.fn(), clearCurrentRun: vi.fn(), cancelRun: vi.fn(),
  },
}))

const api = vi.hoisted(() => ({
  getRunTrace: vi.fn().mockRejectedValue(new Error('no trace')),
  resumeRun: vi.fn(),
}))

vi.mock('@/stores/mining', () => ({ useMiningStore: () => state.store }))
vi.mock('@/api/mining', () => ({ useMiningApi: () => api }))

import { useDomainStore } from '@/stores/domain'
import RunDetailView from '../RunDetailView.vue'

// v6 的轮询模型：挂载即 pollOnce 一次；仅当 run 处于 running 时才 arm 3 秒定时轮询；
// 状态转终态或组件卸载时清除定时器。切域与 resume 通过 clearCurrentRun / startPolling 处理。
describe('RunDetailView polling lifecycle', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    useDomainStore().currentDomain = 'odn'
    state.store.fetchRunDetail.mockClear()
    state.store.fetchProgress.mockClear()
    state.store.currentRun.status = 'queued'
    api.resumeRun.mockReset()
    api.getRunTrace.mockReset().mockRejectedValue(new Error('no trace'))
  })
  afterEach(() => vi.useRealTimers())

  it('arms 3-second polling for a running run', async () => {
    state.store.currentRun.status = 'running'
    const wrapper = shallowMount(RunDetailView, { props: { runId: 'r1' } })
    await flushPromises()
    const initial = state.store.fetchRunDetail.mock.calls.length // 1，挂载 pollOnce

    await vi.advanceTimersByTimeAsync(3000)
    expect(state.store.fetchRunDetail.mock.calls.length).toBe(initial + 1)
    await vi.advanceTimersByTimeAsync(3000)
    expect(state.store.fetchRunDetail.mock.calls.length).toBe(initial + 2)
    wrapper.unmount()
  })

  it('does not arm interval polling for a non-running run', async () => {
    state.store.currentRun.status = 'queued'
    const wrapper = shallowMount(RunDetailView, { props: { runId: 'r1' } })
    await flushPromises()
    const initial = state.store.fetchRunDetail.mock.calls.length // 1

    await vi.advanceTimersByTimeAsync(9000)
    expect(state.store.fetchRunDetail.mock.calls.length).toBe(initial)
    wrapper.unmount()
  })

  it('stops polling once the run reaches a terminal state', async () => {
    state.store.currentRun.status = 'running'
    const wrapper = shallowMount(RunDetailView, { props: { runId: 'r1' } })
    await flushPromises()
    const initial = state.store.fetchRunDetail.mock.calls.length

    await vi.advanceTimersByTimeAsync(3000) // 轮询一拍
    expect(state.store.fetchRunDetail.mock.calls.length).toBe(initial + 1)

    state.store.currentRun.status = 'completed'
    await vi.advanceTimersByTimeAsync(3000) // 这一拍检测到终态并清除定时器
    const settled = state.store.fetchRunDetail.mock.calls.length

    await vi.advanceTimersByTimeAsync(9000) // 不再有新的拉取
    expect(state.store.fetchRunDetail.mock.calls.length).toBe(settled)
    wrapper.unmount()
  })

  it('clears polling on unmount', async () => {
    state.store.currentRun.status = 'running'
    const wrapper = shallowMount(RunDetailView, { props: { runId: 'r1' } })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(3000)
    const before = state.store.fetchRunDetail.mock.calls.length

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(9000)
    expect(state.store.fetchRunDetail.mock.calls.length).toBe(before)
  })

  it('resumes an awaiting_review run and starts polling after a delay', async () => {
    state.store.currentRun.status = 'awaiting_review'
    api.resumeRun.mockResolvedValue({ status: 'running' })
    const wrapper = shallowMount(RunDetailView, { props: { runId: 'r1' } })
    await flushPromises()
    const initial = state.store.fetchRunDetail.mock.calls.length // 1（挂载 pollOnce，非 running 不 arm 定时器）

    await (wrapper.vm as unknown as { handleResume: () => Promise<void> }).handleResume()
    expect(api.resumeRun).toHaveBeenCalledWith('r1', 'odn')

    // handleResume 内部 setTimeout(startPolling, 1500)
    await vi.advanceTimersByTimeAsync(1500)
    expect(state.store.fetchRunDetail.mock.calls.length).toBe(initial + 1)
    wrapper.unmount()
  })

  it('does not start polling when unmounted during the resume delay', async () => {
    state.store.currentRun.status = 'awaiting_review'
    api.resumeRun.mockResolvedValue({ status: 'running' })
    const wrapper = shallowMount(RunDetailView, { props: { runId: 'r1' } })
    await flushPromises()

    await (wrapper.vm as unknown as { handleResume: () => Promise<void> }).handleResume()
    const before = state.store.fetchRunDetail.mock.calls.length

    // 在 1.5s 延迟窗口内离开页面：卸载应清除 resume 定时器
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(3000)
    expect(state.store.fetchRunDetail.mock.calls.length).toBe(before)
  })

  it('drops a slow trace response captured in the previous Domain generation', async () => {
    let resolveOld!: (value: Record<string, unknown>) => void
    api.getRunTrace
      .mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
      .mockResolvedValueOnce({
        run_id: 'r1', domain: 'plant-b', status: 'running', workflow: {
          id: 'plant-b-workflow', version: 2, graph_hash: 'new', graph: { nodes: [], edges: [], output: {} },
        }, node_events: [], stage_events: [], documents: [], warnings: [], asset_counts: {},
      })
    useDomainStore().currentDomain = 'plant-a'
    const wrapper = shallowMount(RunDetailView, { props: { runId: 'r1' } })
    await flushPromises()

    useDomainStore().currentDomain = 'plant-b'
    await flushPromises()
    resolveOld({
      run_id: 'r1', domain: 'plant-a', status: 'running', workflow: {
        id: 'plant-a-workflow', version: 1, graph_hash: 'old', graph: { nodes: [], edges: [], output: {} },
      }, node_events: [], stage_events: [], documents: [], warnings: [], asset_counts: {},
    })
    await flushPromises()

    const trace = (wrapper.vm as unknown as { trace: { workflow: { id: string } } | null }).trace
    expect(trace?.workflow.id).toBe('plant-b-workflow')
    expect(api.getRunTrace).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
})
