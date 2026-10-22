import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  api: {
    listWikiSources: vi.fn(),
    upsertWikiSource: vi.fn(),
    deleteWikiSource: vi.fn(),
    startCompile: vi.fn(),
    compileStatus: vi.fn(),
    confirmToc: vi.fn(),
    cancelCompile: vi.fn(),
  },
  kbApi: { listKbs: vi.fn() },
  domainStore: {
    currentDomain: 'plant-a',
    enabledDomains: [
      { domain_id: 'plant-a', display_name: '甲域', enabled: true },
      { domain_id: 'plant-b', display_name: '乙域', enabled: true },
    ],
  },
  ui: { confirm: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/kb', () => ({ useKbApi: () => state.kbApi }))
vi.mock('@/stores/domain', () => ({ useDomainStore: () => state.domainStore }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => 'mock-error' }))
vi.mock('element-plus', () => ({
  ElMessageBox: { confirm: state.ui.confirm },
  ElMessage: {
    success: state.ui.success, error: state.ui.error, warning: state.ui.warning,
  },
}))

import WikiCompilePanel from '../WikiCompilePanel.vue'
import WikiCompileReport from '../WikiCompileReport.vue'
import WikiTocEditor from '../WikiTocEditor.vue'
import type { WikiCompileStatus, WikiSource, WikiTocSection } from '@/types/artifacts'

const TOKEN = '2026-10-16T08:00:00+00:00'

interface PanelVm {
  addDomain: string
  addKbId: string
  addDirs: string[]
  saveSource: () => Promise<void>
  removeSource: (src: WikiSource) => Promise<void>
  startCompile: () => Promise<void>
  cancelCompile: () => Promise<void>
}

interface TocVm {
  sections: Array<{
    section_title: string
    page_hints: Array<{ title: string; cluster_ids: string[]; page_type: string; included: boolean }>
  }>
  pageCount: number
  canConfirm: boolean
  confirm: () => void
}

/** 三态 dirty 的绑定源（干净 / 已变化 / 不可读）。 */
function sourcesFixture(): WikiSource[] {
  return [
    {
      kb_id: 'kb-1', kb_title: '厂务知识库', status: 'active',
      scope: { directories: ['docs/net'] }, fingerprint: {},
      created_by: 'u1', created_at: '2026-10-16T08:00:00+00:00',
      dirty: false, unreadable: false,
    },
    {
      kb_id: 'kb-2', kb_title: '运维库', status: 'active',
      scope: { directories: [] }, fingerprint: {},
      created_by: 'u1', created_at: '2026-10-16T08:00:00+00:00',
      dirty: true, unreadable: false,
    },
    {
      kb_id: 'kb-3', kb_title: null, status: 'active',
      scope: { directories: ['x'] }, fingerprint: null,
      created_by: 'u1', created_at: '2026-10-16T08:00:00+00:00',
      dirty: null, unreadable: true,
    },
  ]
}

const kbFixture = [
  { id: 'kb-1', name: '厂务知识库', document_count: 3, my_role: 'editor' },
  { id: 'kb-9', name: '乙域新库', document_count: 1, my_role: 'owner' },
]

/** fake timers 下的微任务冲刷（挂载 onMounted / api mock 链全为微任务）。 */
async function flush(times = 8): Promise<void> {
  for (let i = 0; i < times; i++) await Promise.resolve()
}

function mountPanel(canWrite = true, artifactId = 'art-1'): VueWrapper {
  return shallowMount(WikiCompilePanel, {
    props: { artifactId, canWrite, expectedUpdatedAt: TOKEN },
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  state.api.listWikiSources.mockReset().mockResolvedValue(sourcesFixture())
  state.api.upsertWikiSource.mockReset().mockResolvedValue({ kb_id: 'kb-9' })
  state.api.deleteWikiSource.mockReset().mockResolvedValue(undefined)
  state.api.startCompile.mockReset().mockResolvedValue({ batch_id: 'b1', state: 'preparing' })
  state.api.compileStatus.mockReset().mockResolvedValue(
    { batch_id: 'b0', state: 'completed', stats: {}, conflicts: [] } as WikiCompileStatus)
  state.api.confirmToc.mockReset().mockResolvedValue({ state: 'writing' })
  state.api.cancelCompile.mockReset().mockResolvedValue({ state: 'cancelled' })
  state.kbApi.listKbs.mockReset().mockResolvedValue(kbFixture)
  state.ui.confirm.mockReset().mockResolvedValue('confirm')
  state.ui.success.mockReset()
  state.ui.error.mockReset()
  state.ui.warning.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('WikiCompilePanel', () => {
  it('渲染来源列表：目录 chips（空=全库）+ dirty 三态徽标 + 脏横幅', async () => {
    const wrapper = mountPanel()
    await flush()

    const rows = wrapper.findAll('[data-test="wiki-source-row"]')
    expect(rows).toHaveLength(3)
    expect(rows[0].text()).toContain('厂务知识库')
    expect(rows[0].text()).toContain('docs/net')
    expect(rows[0].text()).toContain('最新')        // dirty=false
    expect(rows[1].text()).toContain('全库')        // 空 directories
    expect(rows[1].text()).toContain('来源已变化')   // dirty=true
    expect(rows[2].text()).toContain('kb-3')        // 不可读库隐标题，回落 kb_id
    expect(rows[2].text()).toContain('库不可读')
    expect(rows[2].text()).toContain('未知')        // dirty=null

    // 任一源 dirty → 脏标记横幅（常显于编译区块上方）
    expect(wrapper.find('[data-test="wiki-dirty-banner"]').exists()).toBe(true)

    // 终态（completed）不轮询：mount 一次请求后 advance 无新请求
    await vi.advanceTimersByTimeAsync(10000)
    expect(state.api.compileStatus).toHaveBeenCalledTimes(1)
  })

  it('全部源干净时不渲染脏横幅；无源时开始编译禁用', async () => {
    state.api.listWikiSources.mockResolvedValueOnce(
      [sourcesFixture()[0]])   // 只有干净源
    const wrapper = mountPanel()
    await flush()
    expect(wrapper.find('[data-test="wiki-dirty-banner"]').exists()).toBe(false)

    state.api.listWikiSources.mockReset().mockResolvedValue([])
    const wrapper2 = mountPanel()
    await flush()
    expect(wrapper2.text()).toContain('尚未绑定来源')
    expect(wrapper2.find('[data-test="wiki-compile-start"]').attributes('disabled')).toBeDefined()
  })

  it('添加流程：域→KB 级联（listKbs 必须传 domain）→保存带令牌→changed+重载', async () => {
    const wrapper = mountPanel()
    await flush()
    const vm = wrapper.vm as unknown as PanelVm

    // 默认域=当前域，挂载即拉 KB 列表
    expect(state.kbApi.listKbs).toHaveBeenCalledWith('plant-a')

    vm.addDomain = 'plant-b'
    await flush()
    expect(state.kbApi.listKbs).toHaveBeenLastCalledWith('plant-b')

    vm.addKbId = 'kb-9'
    vm.addDirs = ['docs/a', ' docs/b ']   // trim 归一
    await vm.saveSource()
    await flush()

    expect(state.api.upsertWikiSource).toHaveBeenCalledWith('art-1', {
      kb_id: 'kb-9',
      directories: ['docs/a', 'docs/b'],
      expected_updated_at: TOKEN,
    })
    // 绑定写 touch 制品 → 刷新令牌 + 重载列表
    expect(wrapper.emitted('changed')).toBeTruthy()
    expect(state.api.listWikiSources).toHaveBeenCalledTimes(2)
  })

  it('解绑：确认后 DELETE 随 body 令牌；取消确认不发请求', async () => {
    const wrapper = mountPanel()
    await flush()
    const vm = wrapper.vm as unknown as PanelVm

    await vm.removeSource(sourcesFixture()[0])
    await flush()
    expect(state.api.deleteWikiSource).toHaveBeenCalledWith('art-1', 'kb-1', TOKEN)
    expect(wrapper.emitted('changed')).toBeTruthy()

    state.ui.confirm.mockRejectedValueOnce('cancel')
    await vm.removeSource(sourcesFixture()[1])
    expect(state.api.deleteWikiSource).toHaveBeenCalledTimes(1)   // 取消确认不发
  })

  it('preparing：进度提示 + 取消按钮', async () => {
    state.api.compileStatus.mockResolvedValue(
      { batch_id: 'b1', state: 'preparing' } as WikiCompileStatus)
    const wrapper = mountPanel()
    await flush()

    expect(wrapper.find('[data-test="wiki-compile-preparing"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="wiki-compile-preparing"]').text()).toContain('取材与聚类')
    expect(wrapper.find('[data-test="wiki-compile-cancel"]').exists()).toBe(true)
  })

  it('awaiting_toc：渲染目录编辑器；确认提交编辑后 toc 并转 writing', async () => {
    const toc: WikiTocSection[] = [{
      section_title: '网络',
      page_hints: [{ title: '组网', cluster_ids: ['c1'], page_type: 'topic' }],
    }]
    state.api.compileStatus.mockResolvedValue(
      { batch_id: 'b1', state: 'awaiting_toc', toc } as WikiCompileStatus)
    const wrapper = mountPanel()
    await flush()

    const editor = wrapper.findComponent(WikiTocEditor)
    expect(editor.exists()).toBe(true)
    expect(editor.props('toc')).toEqual(toc)

    const edited: WikiTocSection[] = [{
      section_title: '网络与组网',
      page_hints: [{ title: '组网', cluster_ids: ['c1'], page_type: 'topic' }],
    }]
    editor.vm.$emit('confirm', edited)
    await flush()

    expect(state.api.confirmToc).toHaveBeenCalledWith('art-1', edited, TOKEN)
    // 确认成功 → writing 进度视图（编辑器收起）
    expect(wrapper.findComponent(WikiTocEditor).exists()).toBe(false)
    expect(wrapper.find('[data-test="wiki-compile-writing"]').exists()).toBe(true)
  })

  it('awaiting_toc 停轮询（审查 HIGH）：advance 无新请求——编辑半程不被清', async () => {
    const toc: WikiTocSection[] = [{
      section_title: '网络',
      page_hints: [{ title: '组网', cluster_ids: ['c1'], page_type: 'topic' }],
    }]
    state.api.compileStatus.mockResolvedValue(
      { batch_id: 'b1', state: 'awaiting_toc', toc } as WikiCompileStatus)
    const wrapper = mountPanel()
    await flush()

    expect(state.api.compileStatus).toHaveBeenCalledTimes(1)   // mount 首拉
    expect(wrapper.findComponent(WikiTocEditor).exists()).toBe(true)

    // 该态服务端在等用户输入——轮询已停，advance 15s 不发请求
    await vi.advanceTimersByTimeAsync(15000)
    expect(state.api.compileStatus).toHaveBeenCalledTimes(1)
    // 编辑器仍在（若仍轮询，每轮新 toc 引用会重置 sections 清空编辑）
    expect(wrapper.findComponent(WikiTocEditor).exists()).toBe(true)
  })

  it('制品切换：目标制品在飞（writing）时重启已停定时器（审查 MEDIUM）', async () => {
    // 制品 A：终态 → 定时器停
    state.api.compileStatus.mockResolvedValue(
      { batch_id: 'bA', state: 'completed', stats: {}, conflicts: [] } as WikiCompileStatus)
    const wrapper = mountPanel(true, 'art-1')
    await flush()
    expect(state.api.compileStatus).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(10000)
    expect(state.api.compileStatus).toHaveBeenCalledTimes(1)   // A 终态停轮询

    // 切到制品 B（在飞 writing）→ 重取落态重启轮询
    state.api.compileStatus.mockReset().mockResolvedValue(
      { batch_id: 'bB', state: 'writing', progress: { batch: '1/2', pages_done: 3 } } as WikiCompileStatus)
    await wrapper.setProps({ artifactId: 'art-2' })
    await flush()
    expect(state.api.compileStatus).toHaveBeenLastCalledWith('art-2')

    await vi.advanceTimersByTimeAsync(5000)
    expect(state.api.compileStatus.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect(state.api.compileStatus).toHaveBeenLastCalledWith('art-2')   // 轮询已复活
  })

  it('writing：进度（批次/页数）+ 取消随令牌', async () => {
    state.api.compileStatus.mockResolvedValue({
      batch_id: 'b1', state: 'writing',
      progress: { batch: '2/8', pages_done: 40 },
    } as WikiCompileStatus)
    const wrapper = mountPanel()
    await flush()
    const vm = wrapper.vm as unknown as PanelVm

    const progress = wrapper.find('[data-test="wiki-compile-writing-progress"]')
    expect(progress.exists()).toBe(true)
    expect(progress.text()).toContain('2/8')
    expect(progress.text()).toContain('40')

    await vm.cancelCompile()
    await flush()
    expect(state.api.cancelCompile).toHaveBeenCalledWith('art-1', TOKEN)
    // 取消 → cancelled 终态：emit changed（writing 已推进令牌）
    expect(wrapper.emitted('changed')).toBeTruthy()
    expect(wrapper.find('[data-test="wiki-compile-writing"]').exists()).toBe(false)
  })

  it('终态即停轮询并 emit changed（M4：编译推进乐观锁令牌须刷新）', async () => {
    state.api.compileStatus.mockResolvedValue({
      batch_id: 'b1', state: 'writing', progress: { batch: '1/2', pages_done: 5 },
    } as WikiCompileStatus)
    const wrapper = mountPanel()
    await flush()
    expect(state.api.compileStatus).toHaveBeenCalledTimes(1)   // mount 首拉

    state.api.compileStatus.mockResolvedValue({
      batch_id: 'b1', state: 'completed',
      stats: { pages_written: 50 }, conflicts: [],
    } as WikiCompileStatus)
    await vi.advanceTimersByTimeAsync(5000)   // 轮询观测到终态
    await flush()
    expect(state.api.compileStatus).toHaveBeenCalledTimes(2)
    expect(wrapper.emitted('changed')).toBeTruthy()

    // 终态后 interval 已清——再 advance 不发请求
    await vi.advanceTimersByTimeAsync(20000)
    expect(state.api.compileStatus).toHaveBeenCalledTimes(2)
  })

  it('卸载清定时器：unmount 后 advance 无在飞请求', async () => {
    state.api.compileStatus.mockResolvedValue(
      { batch_id: 'b1', state: 'writing' } as WikiCompileStatus)
    const wrapper = mountPanel()
    await flush()
    expect(state.api.compileStatus).toHaveBeenCalledTimes(1)

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(15000)
    expect(state.api.compileStatus).toHaveBeenCalledTimes(1)
  })

  it('终态视图：上轮报告子组件挂载（stats/conflicts 渲染见 WikiCompileReport 套）+ 可重编译', async () => {
    state.api.compileStatus.mockResolvedValue({
      batch_id: 'b1', state: 'completed',
      stats: { pages_written: 50 }, conflicts: [],
    } as WikiCompileStatus)
    const wrapper = mountPanel()
    await flush()

    const report = wrapper.findComponent(WikiCompileReport)
    expect(report.exists()).toBe(true)
    expect(report.props('status')).toMatchObject({ state: 'completed' })
    expect(wrapper.text()).toContain('上一轮编译已完成')
    // 终态仍可重编译
    expect(wrapper.find('[data-test="wiki-compile-start"]').attributes('disabled')).toBeUndefined()
  })

  it('failed：上轮报告携带失败态 + 提示文案', async () => {
    state.api.compileStatus.mockResolvedValue({
      batch_id: 'b1', state: 'failed', error: 'writing_failed:TimeoutError',
      stats: {}, conflicts: [],
    } as WikiCompileStatus)
    const wrapper = mountPanel()
    await flush()

    expect(wrapper.findComponent(WikiCompileReport).props('status'))
      .toMatchObject({ state: 'failed' })
    expect(wrapper.text()).toContain('上一轮编译失败')
  })

  it('canWrite=false：不拉编译状态（design-only）、无任何写入口', async () => {
    const wrapper = mountPanel(false)
    await flush()

    // 来源列表 viewer 可读；编译状态 design-only 不拉
    expect(state.api.listWikiSources).toHaveBeenCalledWith('art-1')
    expect(state.api.compileStatus).not.toHaveBeenCalled()

    expect(wrapper.find('[data-test="wiki-compile-start"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="wiki-source-form"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-test="wiki-source-delete"]')).toHaveLength(0)
    expect(wrapper.findAll('[data-test="wiki-compile-cancel"]')).toHaveLength(0)

    // 卸载也无请求（轮询 gate 含 canWrite）
    await vi.advanceTimersByTimeAsync(10000)
    expect(state.api.compileStatus).not.toHaveBeenCalled()
  })

  it('切换制品：重置并按新 artifactId 重取', async () => {
    const wrapper = mountPanel()
    await flush()

    await wrapper.setProps({ artifactId: 'art-2' })
    await flush()
    expect(state.api.listWikiSources).toHaveBeenLastCalledWith('art-2')
    expect(state.api.compileStatus).toHaveBeenLastCalledWith('art-2')
  })
})

describe('WikiTocEditor', () => {
  function mountEditor(toc: WikiTocSection[], canWrite = true): VueWrapper {
    return shallowMount(WikiTocEditor, { props: { toc, canWrite } })
  }

  const smallToc: WikiTocSection[] = [
    {
      section_title: '网络',
      page_hints: [
        { title: '组网', cluster_ids: ['c1'], page_type: 'topic' },
        { title: '交换', cluster_ids: ['c2'], page_type: 'topic' },
      ],
    },
    {
      section_title: '运维',
      page_hints: [{ title: '巡检', cluster_ids: ['c3'], page_type: 'topic' }],
    },
  ]

  it('勾选排除 + 改标题 → 提交 payload 只含纳入页（形状对齐后端）', async () => {
    const wrapper = mountEditor(smallToc)
    await flush()
    const vm = wrapper.vm as unknown as TocVm

    expect(vm.pageCount).toBe(3)
    vm.sections[0].section_title = '网络与组网'
    vm.sections[0].page_hints[1].included = false   // 排除「交换」
    vm.sections[1].page_hints[0].title = '例行巡检'
    vm.confirm()

    expect(wrapper.emitted('confirm')).toEqual([[[
      {
        section_title: '网络与组网',
        page_hints: [{ title: '组网', cluster_ids: ['c1'], page_type: 'topic' }],
      },
      {
        section_title: '运维',
        page_hints: [{ title: '例行巡检', cluster_ids: ['c3'], page_type: 'topic' }],
      },
    ]]])
  })

  it('同内容新引用不重置编辑；目录内容真变才重置（审查 HIGH 双保险）', async () => {
    const wrapper = mountEditor(smallToc)
    await flush()
    const vm = wrapper.vm as unknown as TocVm
    vm.sections[0].section_title = '改过的节'
    vm.sections[0].page_hints[1].included = false

    // 父级以同内容新引用回传（重渲染/重复 status 响应）——编辑保留
    await wrapper.setProps({ toc: JSON.parse(JSON.stringify(smallToc)) as WikiTocSection[] })
    await flush()
    expect(vm.sections[0].section_title).toBe('改过的节')
    expect(vm.sections[0].page_hints[1].included).toBe(false)

    // 服务端目录真变（新一轮 awaiting_toc）——以服务端为准重置
    const changed: WikiTocSection[] = [{
      section_title: '新节',
      page_hints: [{ title: '新页', cluster_ids: ['c9'], page_type: 'topic' }],
    }]
    await wrapper.setProps({ toc: changed })
    await flush()
    expect(vm.sections).toHaveLength(1)
    expect(vm.sections[0].section_title).toBe('新节')
    expect(vm.sections[0].page_hints[0].title).toBe('新页')
  })

  it('>200 页：实时计数 + 禁用提交（后端 422 too_many_pages 前移拦截）', async () => {
    const hints = Array.from({ length: 201 }, (_, i) => ({
      title: `页${i}`, cluster_ids: ['c'], page_type: 'topic',
    }))
    const wrapper = mountEditor([{ section_title: '大节', page_hints: hints }])
    await flush()
    const vm = wrapper.vm as unknown as TocVm

    expect(vm.pageCount).toBe(201)
    expect(vm.canConfirm).toBe(false)
    expect(wrapper.find('[data-test="wiki-toc-over"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="wiki-toc-confirm"]').attributes('disabled')).toBeDefined()
    vm.confirm()
    expect(wrapper.emitted('confirm')).toBeFalsy()   // 禁用时 confirm 不发

    // 勾掉一页 → 200 页恢复可提交
    vm.sections[0].page_hints[0].included = false
    await flush()
    expect(vm.canConfirm).toBe(true)
  })

  it('全取消（零页）禁用提交；canWrite=false 无确认/放弃按钮', async () => {
    const wrapper = mountEditor(smallToc)
    await flush()
    const vm = wrapper.vm as unknown as TocVm
    vm.sections.forEach(s => s.page_hints.forEach(h => { h.included = false }))
    await flush()
    expect(vm.canConfirm).toBe(false)
    expect(wrapper.find('[data-test="wiki-toc-confirm"]').attributes('disabled')).toBeDefined()

    const roWrapper = mountEditor(smallToc, false)
    await flush()
    expect(roWrapper.find('[data-test="wiki-toc-confirm"]').exists()).toBe(false)
    expect(roWrapper.find('[data-test="wiki-toc-cancel"]').exists()).toBe(false)
  })
})

describe('WikiCompileReport', () => {
  function mountReport(status: WikiCompileStatus): VueWrapper {
    return shallowMount(WikiCompileReport, { props: { status } })
  }

  it('completed：stats 表格（缺失键不渲染）+ conflicts 明细', async () => {
    const wrapper = mountReport({
      batch_id: 'b1', state: 'completed',
      stats: {
        pages_written: 50, unit_count: 900, cluster_count: 40,
        failed_pages: 2, broken_links: 3, dangling_pages: 1,
        contradiction_pages: 1, open_question_pages: 2, glossary_pages: 6,
        conflicts_n: 1,
      },
      conflicts: [{ page_key: '组网', manual_title: '人工组网', machine_title: '机器组网' }],
    })
    await flush()

    const report = wrapper.find('[data-test="wiki-compile-report"]')
    expect(report.exists()).toBe(true)
    expect(report.text()).toContain('写入页数')
    expect(report.text()).toContain('50')
    expect(report.text()).toContain('取材单元')
    expect(report.text()).toContain('900')
    expect(report.text()).toContain('断链')
    expect(report.text()).toContain('悬空引用页')
    // 端点未带的键（dropped_pages）不渲染该行
    expect(report.text()).not.toContain('超限截断页')

    const conflicts = wrapper.find('[data-test="wiki-compile-conflicts"]')
    expect(conflicts.exists()).toBe(true)
    expect(conflicts.text()).toContain('组网')
    expect(conflicts.text()).toContain('人工组网')
    expect(conflicts.text()).toContain('机器组网')

    // completed 态无失败文案
    expect(wrapper.find('[data-test="wiki-compile-error"]').exists()).toBe(false)
  })

  it('failed：展示失败原因文案；无冲突时不渲染冲突块', async () => {
    const wrapper = mountReport({
      batch_id: 'b1', state: 'failed',
      error: 'writing_failed:TimeoutError', stats: {}, conflicts: [],
    })
    await flush()

    expect(wrapper.find('[data-test="wiki-compile-error"]').text())
      .toContain('writing_failed:TimeoutError')
    expect(wrapper.find('[data-test="wiki-compile-conflicts"]').exists()).toBe(false)
  })
})
