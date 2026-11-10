import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, shallowMount } from '@vue/test-utils'

const api = vi.hoisted(() => ({ getSummary: vi.fn(), list: vi.fn(), getOne: vi.fn(), exportAll: vi.fn() }))
const domain = vi.hoisted(() => ({ currentDomain: 'domain-a' }))
const kbApi = vi.hoisted(() => ({ listKbs: vi.fn() }))
const operatorApi = vi.hoisted(() => ({ listParadigms: vi.fn() }))
const saveBlob = vi.hoisted(() => vi.fn())

vi.mock('@/api/retrievalRecords', () => ({ useRetrievalRecordsApi: () => api }))
vi.mock('@/stores/domain', () => ({ useDomainStore: () => domain }))
vi.mock('@/api/kb', () => ({ useKbApi: () => kbApi }))
vi.mock('@/api/operator', () => ({ useOperatorApi: () => operatorApi }))
vi.mock('@/utils/download', () => ({ saveBlob }))

import RetrievalRecordsPanel from '@/components/retrieval/RetrievalRecordsPanel.vue'
import { useRetrievalNames } from '@/components/retrieval/useRetrievalNames'

enableAutoUnmount(afterEach)

const record = {
  id: 'call-1', occurred_at: '2026-09-28T01:02:03Z', completed_at: '2026-09-28T01:02:04Z',
  domain: 'domain-a', actor_user_id: 'u-1', actor_username: 'alice', source: 'mcp',
  operation: 'search', tool_name: 'search_knowledge', mcp_key_id: 'key-1', kb_ids: ['kb-1'],
  query_text: '核心网告警', paradigm_id: 'p-1', paradigm_version: 2, status: 'success',
  result_count: 3, duration_ms: 120, error_code: null, details_json: {},
}

const readRecord = {
  ...record,
  id: 'call-read-1',
  operation: 'read',
  tool_name: 'get_knowledge',
  query_text: null,
  paradigm_id: null,
  paradigm_version: null,
  details_json: {
    action: 'structured_query', ref_type: 'st_', file_count: 4,
    uploaded_count: 2, failed_count: 1, arbitrary_payload: 'must-not-render',
  },
}

const snapshotRecord = {
  ...record,
  payload: {
    request_json: {
      query: '核心网告警', top_k: 2,
      within: { document_refs: ['doc-1'] },
    },
    effective_context_json: {
      domain: 'domain-a', kb_ids: ['kb-1'], paradigm_id: 'p-1',
    },
    response_mode: 'snapshot',
    response_json: {
      query: '核心网告警',
      evidence: [
        {
          ref: 'ev-1', type: 'prose', content: '第一条证据',
          source: { document_ref: 'doc-1', file_name: 'manual-a.pdf', section: '告警处理' },
          truncated: false,
        },
        {
          ref: 'ev-2', type: 'table_row', content: '<img src=x onerror=alert(1)>第二条证据',
          source: { document_ref: 'doc-2', file_name: 'manual-b.pdf' },
          truncated: true,
        },
      ],
      has_more: true,
    },
    response_refs_json: [],
    request_bytes: 88,
    response_bytes: 512,
    response_truncated: true,
    response_original_bytes: 1024,
    response_omitted_count: 3,
    response_sha256: 'a'.repeat(64),
    redactions_json: ['authorization'],
    payload_schema_version: 1,
  },
}

const referenceRecord = {
  ...readRecord,
  payload: {
    request_json: { ref: 'doc-1', mode: 'window', limit: 100 },
    effective_context_json: { domain: 'domain-a', kb_ids: ['kb-1'] },
    response_mode: 'reference',
    response_json: null,
    response_refs_json: [{
      view: 'document_content', ref: 'doc-1', next_cursor: 'next-1',
      segments: [{ ref: 'st-1', source: { document_ref: 'doc-1' } }],
    }],
    request_bytes: 42,
    response_bytes: 96,
    response_truncated: false,
    response_original_bytes: 96,
    response_omitted_count: 0,
    response_sha256: 'b'.repeat(64),
    redactions_json: [],
    payload_schema_version: 1,
  },
}

describe('RetrievalRecordsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useRetrievalNames().reset()
    domain.currentDomain = 'domain-a'
    api.getSummary.mockResolvedValue({
      days: 7,
      available: true,
      summary: { total_calls: 1, calls: 1, no_result: 0, no_result_rate: 0, failed: 0, failure_rate: 0, p95_duration_ms: 120, avg_duration_ms: 100, active_paradigms: 1 },
      trend: [], paradigms: [], tools: [], sources: {}, top_queries: [],
    })
    api.list.mockResolvedValue({ items: [record], next_cursor: null, has_more: false, page_size: 25 })
    api.getOne.mockResolvedValue(record)
    api.exportAll.mockResolvedValue({ items: [record, readRecord], truncated: false, row_limit: 10000 })
    kbApi.listKbs.mockResolvedValue([{ id: 'kb-1', name: '云核心网手册' }, { id: 'kb-2', name: '传输手册' }])
    operatorApi.listParadigms.mockResolvedValue([{ id: 'p-1', name: '告警范式' }, { id: 'p-2', name: '配置范式' }])
  })

  it('loads summary and the first page with a fixed knowledge base scope', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel, { props: { kbId: 'kb-1' } })
    await flushPromises()

    expect(api.getSummary).toHaveBeenCalledWith('domain-a', expect.objectContaining({ kbId: 'kb-1' }))
    expect(api.list).toHaveBeenCalledWith('domain-a', expect.objectContaining({ kbId: 'kb-1' }))
    expect(wrapper.text()).toContain('核心网告警')
    expect(wrapper.text()).toContain('search_knowledge')
    // 目标知识库与范式显示名称而非原始 ID
    expect(wrapper.text()).toContain('云核心网手册')
    expect(wrapper.text()).toContain('告警范式 v2')
  })

  it('falls back to raw IDs when the name lists cannot be loaded', async () => {
    kbApi.listKbs.mockRejectedValue(new Error('kb list down'))
    operatorApi.listParadigms.mockRejectedValue(new Error('paradigm list down'))
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()

    expect(wrapper.text()).toContain('kb-1')
    expect(wrapper.text()).toContain('p-1 v2')
  })

  it('applies one time window and the same complete filter set to list and summary', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel, { props: { mcpKeyId: 'key-1' } })
    await flushPromises()
    vi.clearAllMocks()
    api.getSummary.mockResolvedValue({ days: 30, available: true, summary: {}, trend: [], paradigms: [], tools: [], sources: {} })
    api.list.mockResolvedValue({ items: [], next_cursor: null, has_more: false, page_size: 25 })

    await wrapper.find('[aria-label="时间范围"]').setValue('30')
    await wrapper.find('[aria-label="来源"]').setValue('mcp')
    await wrapper.find('[aria-label="状态"]').setValue('failed')
    await wrapper.find('[aria-label="操作"]').setValue('read')
    await wrapper.find('[aria-label="Tool 名称"]').setValue('get_knowledge')
    await wrapper.find('[aria-label="知识库"]').setValue('kb-1')
    await wrapper.find('[aria-label="用户 ID"]').setValue('u-2')
    await wrapper.find('[aria-label="范式"]').setValue('p-2')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    const expected = {
      days: 30, source: 'mcp', status: 'failed', operation: 'read',
      toolName: 'get_knowledge', kbId: 'kb-1', actorUserId: 'u-2',
      paradigmId: 'p-2', mcpKeyId: 'key-1',
    }
    expect(api.getSummary).toHaveBeenCalledWith('domain-a', expected)
    expect(api.list).toHaveBeenCalledWith('domain-a', expect.objectContaining(expected))
  })

  it('applies a custom date range that replaces the day preset', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()
    vi.clearAllMocks()
    api.getSummary.mockResolvedValue({ days: 20, available: true, summary: {}, trend: [], paradigms: [], tools: [], sources: {} })
    api.list.mockResolvedValue({ items: [], next_cursor: null, has_more: false, page_size: 25 })

    await wrapper.find('[aria-label="时间范围"]').setValue('custom')
    // 切到自定义时预填近 7 天，再手改成目标区间
    expect((wrapper.find('[aria-label="开始日期"]').element as HTMLInputElement).value).not.toBe('')
    await wrapper.find('[aria-label="开始日期"]').setValue('2026-10-01')
    await wrapper.find('[aria-label="结束日期"]').setValue('2026-10-20')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(api.getSummary).toHaveBeenCalledWith('domain-a', expect.objectContaining({
      startDate: '2026-10-01', endDate: '2026-10-20',
    }))
    expect(api.getSummary.mock.calls[0][1].days).toBeUndefined()
    expect(api.list).toHaveBeenCalledWith('domain-a', expect.objectContaining({
      startDate: '2026-10-01', endDate: '2026-10-20',
    }))
    expect(api.list.mock.calls[0][1].days).toBeUndefined()
  })

  it('rejects incomplete or reversed custom ranges without hitting the API', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()
    vi.clearAllMocks()

    await wrapper.find('[aria-label="时间范围"]').setValue('custom')
    await wrapper.find('[aria-label="开始日期"]').setValue('2026-10-01')
    await wrapper.find('[aria-label="结束日期"]').setValue('')
    await wrapper.find('form').trigger('submit')
    expect(wrapper.find('.records-panel__filter-error').text()).toContain('开始与结束日期')
    expect(api.list).not.toHaveBeenCalled()

    await wrapper.find('[aria-label="结束日期"]').setValue('2026-09-30')
    await wrapper.find('form').trigger('submit')
    expect(wrapper.find('.records-panel__filter-error').text()).toContain('开始日期不能晚于结束日期')
    expect(api.getSummary).not.toHaveBeenCalled()
    expect(api.list).not.toHaveBeenCalled()

    // 修正区间后错误消除并正常请求
    api.list.mockResolvedValue({ items: [], next_cursor: null, has_more: false, page_size: 25 })
    api.getSummary.mockResolvedValue({ days: 2, available: true, summary: {}, trend: [], paradigms: [], tools: [], sources: {} })
    await wrapper.find('[aria-label="结束日期"]').setValue('2026-10-02')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('.records-panel__filter-error').exists()).toBe(false)
    expect(api.list).toHaveBeenCalledWith('domain-a', expect.objectContaining({
      startDate: '2026-10-01', endDate: '2026-10-02',
    }))
  })

  it('reset returns to the 7-day preset and clears custom dates', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()
    vi.clearAllMocks()
    api.getSummary.mockResolvedValue({ days: 7, available: true, summary: {}, trend: [], paradigms: [], tools: [], sources: {} })
    api.list.mockResolvedValue({ items: [], next_cursor: null, has_more: false, page_size: 25 })

    await wrapper.find('[aria-label="时间范围"]').setValue('custom')
    await wrapper.find('[aria-label="开始日期"]').setValue('2026-10-01')
    await wrapper.find('[aria-label="结束日期"]').setValue('2026-10-20')
    await wrapper.find('form').trigger('submit')
    await flushPromises()
    vi.clearAllMocks()
    api.getSummary.mockResolvedValue({ days: 7, available: true, summary: {}, trend: [], paradigms: [], tools: [], sources: {} })
    api.list.mockResolvedValue({ items: [], next_cursor: null, has_more: false, page_size: 25 })

    await wrapper.findAll('button').find(b => b.text() === '重置')!.trigger('click')
    await flushPromises()

    expect(api.list).toHaveBeenCalledWith('domain-a', expect.objectContaining({ days: 7 }))
    expect(api.list.mock.calls[0][1].startDate).toBeUndefined()
    expect(wrapper.find('[aria-label="开始日期"]').exists()).toBe(false)
  })

  it('exports the applied filters as a BOM-prefixed CSV with resolved names', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()
    vi.clearAllMocks()

    await wrapper.findAll('button').find(b => b.text() === '导出')!.trigger('click')
    await flushPromises()

    expect(api.exportAll).toHaveBeenCalledWith('domain-a', expect.objectContaining({ days: 7 }))
    expect(saveBlob).toHaveBeenCalledTimes(1)
    const [blob, filename] = saveBlob.mock.calls[0]
    expect(filename).toMatch(/^检索记录_\d{8}-\d{6}\.csv$/)
    const text = await blob.text()
    // BOM 让 Excel 以 UTF-8 打开中文（text() 按 spec 会剥 BOM，从原始字节断言）
    const head = new Uint8Array(await blob.arrayBuffer()).slice(0, 3)
    expect([...head]).toEqual([0xEF, 0xBB, 0xBF])
    expect(text).toContain('时间,用户,来源,Tool / 操作,查询或动作,目标知识库,范式,状态,结果数,耗时(ms),错误码,调用 ID')
    expect(text).toContain('核心网告警')
    expect(text).toContain('云核心网手册')
    expect(text).toContain('告警范式 v2')
    expect(text).toContain('structured_query · st_')
    expect(text).toContain('call-1')
    // 未截断时无提示
    expect(wrapper.text()).not.toContain('上限')
  })

  it('shows a notice instead of an empty file when nothing matches', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()
    vi.clearAllMocks()
    api.exportAll.mockResolvedValue({ items: [], truncated: false, row_limit: 10000 })

    await wrapper.findAll('button').find(b => b.text() === '导出')!.trigger('click')
    await flushPromises()

    expect(saveBlob).not.toHaveBeenCalled()
    expect(wrapper.find('.records-panel__filter-notice').text()).toContain('没有可导出的记录')
  })

  it('warns when the export is truncated at the row limit', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()
    vi.clearAllMocks()
    api.exportAll.mockResolvedValue({ items: [record], truncated: true, row_limit: 10000 })

    await wrapper.findAll('button').find(b => b.text() === '导出')!.trigger('click')
    await flushPromises()

    expect(saveBlob).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.records-panel__filter-notice').text()).toContain('已按上限截断')
  })

  it('surfaces export failures inline without breaking the panel', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()
    vi.clearAllMocks()
    api.exportAll.mockRejectedValue(new Error('export downstream down'))

    await wrapper.findAll('button').find(b => b.text() === '导出')!.trigger('click')
    await flushPromises()

    expect(saveBlob).not.toHaveBeenCalled()
    expect(wrapper.find('.records-panel__filter-error').text()).toContain('export downstream down')
  })

  it('opens a safe detail drawer without displaying secrets or response bodies', async () => {
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()
    await wrapper.find('[data-test="record-detail-call-1"]').trigger('click')
    await flushPromises()

    expect(api.getOne).toHaveBeenCalledWith('domain-a', 'call-1')
    expect(wrapper.text()).toContain('调用详情')
    expect(wrapper.text()).toContain('call-1')
    expect(wrapper.text()).not.toContain('Authorization')
  })

  it('shows the MCP action summary and only whitelisted detail fields', async () => {
    api.list.mockResolvedValue({ items: [readRecord], next_cursor: null, has_more: false, page_size: 25 })
    api.getOne.mockResolvedValue(readRecord)
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()

    expect(wrapper.text()).toContain('structured_query')
    expect(wrapper.text()).toContain('st_')
    await wrapper.find('[data-test="record-detail-call-read-1"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('动作')
    expect(wrapper.text()).toContain('引用类型')
    expect(wrapper.text()).toContain('文件数')
    expect(wrapper.text()).toContain('上传成功数')
    expect(wrapper.text()).toContain('失败数')
    expect(wrapper.text()).not.toContain('must-not-render')
    expect(wrapper.text()).not.toContain('arbitrary_payload')
  })

  it('uses the new-version empty-state copy', async () => {
    api.list.mockResolvedValue({ items: [], next_cursor: null, has_more: false, page_size: 25 })
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()

    expect(wrapper.text()).toContain('本版本上线后开始记录')
  })

  it('shows complete request context and ordered search evidence with safe JSON text', async () => {
    api.getOne.mockResolvedValue(snapshotRecord)
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()

    await wrapper.find('[data-test="record-detail-call-1"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('调用参数')
    expect(wrapper.text()).toContain('有效执行参数')
    expect(wrapper.text()).toContain('返回内容')
    expect(wrapper.text()).toContain('top_k')
    expect(wrapper.text()).toContain('第一条证据')
    expect(wrapper.text()).toContain('第二条证据')
    expect(wrapper.text().indexOf('第一条证据')).toBeLessThan(wrapper.text().indexOf('第二条证据'))
    expect(wrapper.text()).toContain('manual-a.pdf')
    expect(wrapper.text()).toContain('原始 JSON')
    expect(wrapper.html()).not.toContain('<img src="x"')
  })

  it('renders Java evidenceResponse snapshots with the same evidence view', async () => {
    api.getOne.mockResolvedValue({
      ...snapshotRecord,
      payload: {
        ...snapshotRecord.payload,
        response_json: {
          evidenceResponse: {
            query: '核心网告警',
            evidence: [{ ref: 'ev-java', type: 'prose', content: 'Java 证据快照', source: { file_name: 'java.pdf' } }],
          },
        },
      },
    })
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()

    await wrapper.find('[data-test="record-detail-call-1"]').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.records-panel__evidence')).toHaveLength(1)
    expect(wrapper.find('.records-panel__evidence').text()).toContain('Java 证据快照')
    expect(wrapper.find('.records-panel__evidence').text()).toContain('java.pdf')
  })
  it('explains reference-only responses without duplicating document bodies', async () => {
    api.getOne.mockResolvedValue(referenceRecord)
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()

    await wrapper.find('[data-test="record-detail-call-1"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('正文未重复保存')
    expect(wrapper.text()).toContain('doc-1')
    expect(wrapper.text()).toContain('st-1')
  })

  it('makes response truncation explicit in the detail drawer', async () => {
    api.getOne.mockResolvedValue(snapshotRecord)
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()

    await wrapper.find('[data-test="record-detail-call-1"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('返回快照已截断')
    expect(wrapper.text()).toContain('省略 3 项')
  })

  it('handles detail request failures and renders a friendly fallback', async () => {
    api.getOne.mockRejectedValue(new Error('detail unavailable'))
    const wrapper = shallowMount(RetrievalRecordsPanel)
    await flushPromises()

    await wrapper.find('[data-test="record-detail-call-1"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('详情加载失败')
    expect(wrapper.text()).toContain('detail unavailable')
  })
})
