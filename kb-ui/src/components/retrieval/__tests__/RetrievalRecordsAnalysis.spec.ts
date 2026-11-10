import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, shallowMount } from '@vue/test-utils'

const api = vi.hoisted(() => ({ getSummary: vi.fn() }))
const domain = vi.hoisted(() => ({ currentDomain: 'domain-a' }))
const kbApi = vi.hoisted(() => ({ listKbs: vi.fn() }))
const operatorApi = vi.hoisted(() => ({ listParadigms: vi.fn() }))
vi.mock('@/api/retrievalRecords', () => ({ useRetrievalRecordsApi: () => api }))
vi.mock('@/stores/domain', () => ({ useDomainStore: () => domain }))
vi.mock('@/api/kb', () => ({ useKbApi: () => kbApi }))
vi.mock('@/api/operator', () => ({ useOperatorApi: () => operatorApi }))

import RetrievalRecordsAnalysis from '@/components/retrieval/RetrievalRecordsAnalysis.vue'
import { useRetrievalNames } from '@/components/retrieval/useRetrievalNames'

enableAutoUnmount(afterEach)

describe('RetrievalRecordsAnalysis', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useRetrievalNames().reset()
    kbApi.listKbs.mockResolvedValue([{ id: 'kb-1', name: '云核心网手册' }])
    operatorApi.listParadigms.mockResolvedValue([{ id: 'p-1', name: '告警范式' }])
    api.getSummary.mockResolvedValue({
      available: true, days: 7,
      summary: { total_calls: 10, calls: 7, no_result: 2, failed: 1, no_result_rate: .2, failure_rate: .1, p95_duration_ms: 300, avg_duration_ms: 100, active_paradigms: 1 },
      trend: [{ date: '2026-09-28', total_calls: 10, calls: 7, no_result: 2, failed: 1 }],
      sources: { web: 4, mcp: 6 },
      tools: [{ tool_name: 'search_knowledge', calls: 5, no_result: 1 }],
      paradigms: [{ paradigm_id: 'p-1', calls: 7, no_result: 2 }],
      top_queries: [
        { query_text: '5GC 计费规则是什么', count: 5, no_result: 0, last_at: '2026-09-28T00:00:00Z' },
        { query_text: '计费', count: 3, no_result: 0, last_at: '2026-09-28T00:00:00Z' },
      ],
    })
  })

  it('renders trend and every migrated analysis section', async () => {
    const wrapper = shallowMount(RetrievalRecordsAnalysis)
    await flushPromises()

    expect(wrapper.findComponent({ name: 'LineChart' }).exists()).toBe(true)
    expect(wrapper.findAllComponents({ name: 'BarChart' })).toHaveLength(3)
    expect(wrapper.text()).toContain('范式分布')
    expect(wrapper.text()).toContain('MCP Tool 分布')
    expect(wrapper.text()).toContain('来源分布')
    // 范式分布按名称展示，不露原始 ID
    const paradigmBars = wrapper.findAllComponents({ name: 'BarChart' })[0]
    expect(paradigmBars.props('data')[0].name).toBe('告警范式')
  })

  it('热门查询走分词词云：WordCloud 按词频喂词，QueryList/答不上来卡片已下线', async () => {
    const wrapper = shallowMount(RetrievalRecordsAnalysis)
    await flushPromises()

    expect(wrapper.text()).not.toContain('答不上来的问题')
    expect(wrapper.findComponent({ name: 'QueryList' }).exists()).toBe(false)

    const cloud = wrapper.findComponent({ name: 'WordCloud' })
    expect(cloud.exists()).toBe(true)
    const names = cloud.props('items').map((i: { name: string }) => i.name)
    // 两句都含"计费"，词频合并后它排最前；"是什么"是功能词被过滤
    expect(names[0]).toBe('计费')
    expect(names).not.toContain('是什么')
  })

  it('MCP Tool 分布直接用 tool_name，不再有"非 MCP"兜底桶', async () => {
    const wrapper = shallowMount(RetrievalRecordsAnalysis)
    await flushPromises()

    const toolBars = wrapper.findAllComponents({ name: 'BarChart' })[1]
    expect(toolBars.props('data')).toEqual([{ name: 'search_knowledge', value: 5 }])
  })

  it('defaults to the 7-day window and switches presets immediately', async () => {
    const wrapper = shallowMount(RetrievalRecordsAnalysis)
    await flushPromises()

    expect(api.getSummary).toHaveBeenCalledTimes(1)
    expect(api.getSummary).toHaveBeenCalledWith('domain-a', expect.objectContaining({ days: 7 }))

    await wrapper.find('[aria-label="时间范围"]').setValue('30')
    await flushPromises()
    expect(api.getSummary).toHaveBeenCalledTimes(2)
    expect(api.getSummary).toHaveBeenLastCalledWith('domain-a', expect.objectContaining({ days: 30 }))
  })

  it('switching to custom prefills the last 7 days and queries the custom window', async () => {
    const wrapper = shallowMount(RetrievalRecordsAnalysis)
    await flushPromises()
    expect(api.getSummary).toHaveBeenCalledTimes(1)

    await wrapper.find('[aria-label="时间范围"]').setValue('custom')
    await flushPromises()

    // 预填近 7 天 → 一次带 start/end（不再送 days）的请求
    expect(api.getSummary).toHaveBeenCalledTimes(2)
    const filters = api.getSummary.mock.calls[1][1]
    expect(filters.days).toBeUndefined()
    expect(filters.startDate).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(filters.endDate).toMatch(/^\d{4}-\d{2}-\d{2}$/)

    await wrapper.find('[aria-label="开始日期"]').setValue('2026-10-01')
    await wrapper.find('[aria-label="结束日期"]').setValue('2026-10-20')
    await flushPromises()
    expect(api.getSummary).toHaveBeenLastCalledWith('domain-a', expect.objectContaining({
      startDate: '2026-10-01', endDate: '2026-10-20',
    }))
  })

  it('shows an inline error and skips the request for invalid custom ranges', async () => {
    const wrapper = shallowMount(RetrievalRecordsAnalysis)
    await flushPromises()
    expect(api.getSummary).toHaveBeenCalledTimes(1)

    await wrapper.find('[aria-label="时间范围"]').setValue('custom')
    await flushPromises()
    expect(api.getSummary).toHaveBeenCalledTimes(2)

    // 开始日期先改成仍合法的值（与预填的结束日期构成合法区间）→ 正常加载一次
    await wrapper.find('[aria-label="开始日期"]').setValue('2026-10-21')
    await flushPromises()
    expect(api.getSummary).toHaveBeenCalledTimes(3)

    // 结束日期改到开始之前 → 倒序区间只提示不请求
    await wrapper.find('[aria-label="结束日期"]').setValue('2026-10-20')
    await flushPromises()

    expect(api.getSummary).toHaveBeenCalledTimes(3)
    expect(wrapper.find('.records-analysis__range-error').text()).toContain('开始日期不能晚于结束日期')
  })
})
