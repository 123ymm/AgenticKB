import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { VueFlow } from '@vue-flow/core'

const state = vi.hoisted(() => ({
  api: {
    listWikiPages: vi.fn(),
    listWikiLinks: vi.fn(),
  },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => 'mock-error' }))

import WikiGraphPanel from '../WikiGraphPanel.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import {
  buildWikiGraph,
  filterWikiGraph,
  restrictToOneHop,
} from '@/utils/wikiGraph'
import type { Node, Edge } from '@vue-flow/core'
import type {
  WikiLink,
  WikiLinksEnvelope,
  WikiPageMeta,
  WikiPagesEnvelope,
} from '@/types/artifacts'

interface GraphVm {
  searchQuery: string
  oneHopOnly: boolean
  reload: () => Promise<void>
}

/** 三页分层样例：home/solo（ordinal 1/2 → 层0）+ res-pool（ordinal 1001 → 层1）。 */
const pagesFixture: WikiPageMeta[] = [
  {
    page_key: 'home', title: '首页', page_type: 'topic', origin: 'manual',
    ordinal: 1, flags: null, aliases: [],
    updated_at: '2026-11-01T08:00:00+00:00', updated_by: 'u1',
  },
  {
    page_key: 'solo', title: '独立页', page_type: 'glossary', origin: 'machine',
    ordinal: 2, flags: null, aliases: [],
    updated_at: '2026-11-01T08:10:00+00:00', updated_by: 'u2',
  },
  {
    page_key: 'res-pool', title: '资源池管理特性', page_type: 'topic',
    origin: 'machine', ordinal: 1001, flags: null, aliases: [],
    updated_at: '2026-11-01T08:20:00+00:00', updated_by: 'u2',
  },
]

/** 六条全 valid 边（home↔res-pool 两条平行边=不同锚文本；solo 无引用也成节点）。 */
const linksFixture: WikiLink[] = [
  { from_page: 'home', to_page: 'res-pool', anchor_text: '资源池', status: 'valid' },
  { from_page: 'home', to_page: 'solo', anchor_text: '术语', status: 'valid' },
  { from_page: 'res-pool', to_page: 'home', anchor_text: '回到首页', status: 'valid' },
  { from_page: 'solo', to_page: 'res-pool', anchor_text: '相关特性', status: 'valid' },
  { from_page: 'home', to_page: 'res-pool', anchor_text: '再看资源池', status: 'valid' },
  { from_page: 'res-pool', to_page: 'solo', anchor_text: '术语表', status: 'valid' },
]

function mountGraph(draft = false): VueWrapper {
  return shallowMount(WikiGraphPanel, { props: { artifactId: 'art-1', draft } })
}

/** 画布桩上的节点/边（浅渲染：vue-flow 画布本身不打 DOM，断言传入 props）。 */
function flowProps(wrapper: VueWrapper): { nodes: Node[]; edges: Edge[] } {
  const flow = wrapper.getComponent(VueFlow)
  return {
    nodes: flow.props('nodes') as Node[],
    edges: flow.props('edges') as Edge[],
  }
}

function nodeById(nodes: Node[], id: string): Node | undefined {
  return nodes.find(node => node.id === id)
}

function clickNode(wrapper: VueWrapper, id: string): void {
  wrapper.getComponent(VueFlow).vm.$emit('nodeClick', { node: { id } })
}

beforeEach(() => {
  state.api.listWikiPages.mockReset().mockResolvedValue(
    { revision: 3, pages: pagesFixture })
  state.api.listWikiLinks.mockReset().mockResolvedValue(
    { revision: 3, links: linksFixture })
})

// ── 纯函数：图数据计算（分层坐标/断链伪节点/过滤）──

describe('buildWikiGraph 纯函数', () => {
  it('节点=页面、边=引用；独立页（无引用）也有节点；出链/被引用计数', () => {
    const model = buildWikiGraph(pagesFixture, linksFixture)
    expect(model.nodes.map(n => n.id).sort()).toEqual(['home', 'res-pool', 'solo'])
    expect(model.edges).toHaveLength(6)
    const home = model.nodes.find(n => n.id === 'home')
    expect(home?.outCount).toBe(3)   // 出链 3（含平行边）
    expect(home?.inCount).toBe(1)    // 被引用 1
    const solo = model.nodes.find(n => n.id === 'solo')
    expect(solo?.outCount).toBe(1)
    expect(solo?.inCount).toBe(2)
  })

  it('写死 ordinal 分层坐标：ordinal//1000 为层、层内 ordinal 序（层1在层0右侧）', () => {
    const model = buildWikiGraph(pagesFixture, linksFixture)
    const home = model.nodes.find(n => n.id === 'home')
    const solo = model.nodes.find(n => n.id === 'solo')
    const resPool = model.nodes.find(n => n.id === 'res-pool')
    expect(home?.layer).toBe(0)
    expect(solo?.layer).toBe(0)
    expect(resPool?.layer).toBe(1)
    // 层内按 ordinal：home(1) 在 solo(2) 上方
    expect(home!.position.y).toBeLessThan(solo!.position.y)
    expect(resPool!.position.x).toBeGreaterThan(home!.position.x)
  })

  it('断链：目标页不存在 → 伪节点 isBroken（层=页面层之后）+ 边带断链标记', () => {
    const links: WikiLink[] = [
      { from_page: 'home', to_page: 'res-pool', anchor_text: 'a', status: 'valid' },
      { from_page: 'res-pool', to_page: '幽灵页', anchor_text: '断链', status: 'broken' },
    ]
    const model = buildWikiGraph(pagesFixture, links)
    expect(model.nodes.map(n => n.id)).toContain('幽灵页')
    const ghost = model.nodes.find(n => n.id === '幽灵页')
    expect(ghost?.isBroken).toBe(true)
    expect(ghost?.inCount).toBe(1)   // 被一条断链引用
    expect(ghost?.layer).toBe(2)     // 页面层 0/1 之后的断链层
    const brokenEdge = model.edges.find(e => e.target === '幽灵页')
    expect(brokenEdge?.isBroken).toBe(true)
  })
})

describe('filterWikiGraph / restrictToOneHop 纯函数', () => {
  it('搜索按标题过滤节点，边只保留两端都在的', () => {
    const model = buildWikiGraph(pagesFixture, linksFixture)
    const filtered = filterWikiGraph(model, '资源池')
    expect(filtered.nodes.map(n => n.id)).toEqual(['res-pool'])
    expect(filtered.edges).toHaveLength(0)   // 邻居被滤掉 → 边不悬挂
  })

  it('空查询=全量；一跳收窄：中心+直接邻居，二跳外节点剔除', () => {
    const chainPages: WikiPageMeta[] = ['a', 'b', 'c', 'd'].map((key, i) => ({
      page_key: key, title: `页${key}`, page_type: 'topic' as const,
      origin: 'manual' as const, ordinal: i + 1, flags: null, aliases: [],
      updated_at: '2026-11-01T08:00:00+00:00', updated_by: 'u1',
    }))
    const chainLinks: WikiLink[] = [
      { from_page: 'a', to_page: 'b', anchor_text: '', status: 'valid' },
      { from_page: 'b', to_page: 'c', anchor_text: '', status: 'valid' },
      { from_page: 'c', to_page: 'd', anchor_text: '', status: 'valid' },
    ]
    const model = buildWikiGraph(chainPages, chainLinks)
    expect(filterWikiGraph(model, '').nodes).toHaveLength(4)

    const oneHop = restrictToOneHop(model, 'b')
    expect(oneHop.nodes.map(n => n.id).sort()).toEqual(['a', 'b', 'c'])
    expect(oneHop.edges.map(e => `${e.source}->${e.target}`).sort())
      .toEqual(['a->b', 'b->c'])
    // 无选中中心 → 全量（开关空转不收窄）
    expect(restrictToOneHop(model, null).nodes).toHaveLength(4)
  })
})

// ── 组件：加载/渲染/交互（vue-flow 画布以桩断言 props）──

describe('WikiGraphPanel', () => {
  it('渲染：pages(3)+links(6) → 节点 3 边 6；运行态缺省不带 draft 读正式版', async () => {
    const wrapper = mountGraph()
    await flushPromises()

    expect(state.api.listWikiPages).toHaveBeenCalledWith('art-1', { draft: false })
    expect(state.api.listWikiLinks).toHaveBeenCalledWith('art-1', { draft: false })

    const { nodes, edges } = flowProps(wrapper)
    expect(nodes).toHaveLength(3)
    expect(edges).toHaveLength(6)
    // 独立页也有节点
    expect(nodeById(nodes, 'solo')).toBeDefined()
  })

  it('设计态模式：两请求都带 draft=true（页面守卫在入口，组件不重复鉴权）', async () => {
    const wrapper = mountGraph(true)
    await flushPromises()

    expect(state.api.listWikiPages).toHaveBeenCalledWith('art-1', { draft: true })
    expect(state.api.listWikiLinks).toHaveBeenCalledWith('art-1', { draft: true })
    const { nodes } = flowProps(wrapper)
    expect(nodes).toHaveLength(3)
  })

  it('断链：broken 目标渲染断链 class 节点 + 点击不选中不可打开', async () => {
    state.api.listWikiLinks.mockResolvedValue({
      revision: 3,
      links: [
        { from_page: 'home', to_page: 'res-pool', anchor_text: 'a', status: 'valid' },
        { from_page: 'res-pool', to_page: '幽灵页', anchor_text: '断链', status: 'broken' },
      ],
    })
    const wrapper = mountGraph()
    await flushPromises()

    const { nodes } = flowProps(wrapper)
    const ghost = nodeById(nodes, '幽灵页')
    expect(ghost).toBeDefined()
    expect(String(ghost?.class)).toContain('wiki-graph-node--broken')

    // 点击断链节点：不选中（详情区停留提示文案）、无打开页面出口
    clickNode(wrapper, '幽灵页')
    await flushPromises()
    const detailAfterBrokenClick = wrapper.find('[data-test="wiki-graph-detail"]')
    expect(detailAfterBrokenClick.text()).toContain('点击节点查看')
    expect(detailAfterBrokenClick.find('[data-test="wiki-graph-open"]').exists()).toBe(false)
    expect(wrapper.emitted('open-page')).toBeUndefined()
  })

  it('点击节点 → 详情区（标题/页型/origin/出链数/被引用数）+ 打开页面 emit', async () => {
    const wrapper = mountGraph()
    await flushPromises()

    clickNode(wrapper, 'home')
    await flushPromises()

    const detail = wrapper.find('[data-test="wiki-graph-detail"]')
    expect(detail.exists()).toBe(true)
    const text = detail.text()
    expect(text).toContain('首页')
    expect(text).toContain('主题')     // 页型中文标记
    expect(text).toContain('manual')   // origin 标记
    expect(text).toContain('3')        // 出链数
    expect(text).toContain('1')        // 被引用数

    await wrapper.find('[data-test="wiki-graph-open"]').trigger('click')
    expect(wrapper.emitted('open-page')).toEqual([['home']])
  })

  it('搜索过滤节点标题；「仅所选页及一跳」开关收窄子图', async () => {
    const wrapper = mountGraph()
    await flushPromises()
    const vm = wrapper.vm as unknown as GraphVm

    vm.searchQuery = '资源池'
    await flushPromises()
    expect(flowProps(wrapper).nodes.map(n => n.id)).toEqual(['res-pool'])

    vm.searchQuery = ''
    vm.oneHopOnly = true
    clickNode(wrapper, 'home')
    await flushPromises()
    // home 的一跳邻域：home + res-pool + solo（链 a→b→c→d 样例外无二跳页，全收）
    expect(flowProps(wrapper).nodes.map(n => n.id).sort())
      .toEqual(['home', 'res-pool', 'solo'])
  })

  it('混版防护：两响应 revision 不一致 → 自动重拉一次对齐（不报错）', async () => {
    state.api.listWikiPages.mockReset()
      .mockResolvedValueOnce({ revision: 2, pages: pagesFixture })
      .mockResolvedValue({ revision: 3, pages: pagesFixture })
    state.api.listWikiLinks.mockReset()
      .mockResolvedValueOnce({ revision: 1, links: linksFixture })   // 首对不一致（请求间发生发布）
      .mockResolvedValue({ revision: 3, links: linksFixture })       // 重拉对齐
    const wrapper = mountGraph()
    await flushPromises()

    expect(state.api.listWikiPages).toHaveBeenCalledTimes(2)
    expect(state.api.listWikiLinks).toHaveBeenCalledTimes(2)
    expect(flowProps(wrapper).nodes).toHaveLength(3)
    expect(wrapper.text()).not.toContain('版本切换中')
  })

  it('混版防护：重拉后仍不一致 → 错误态「版本切换中，请重试」，不再第三次请求', async () => {
    state.api.listWikiPages.mockResolvedValue({ revision: 2, pages: pagesFixture })
    state.api.listWikiLinks.mockResolvedValue({ revision: 3, links: linksFixture })
    const wrapper = mountGraph()
    await flushPromises()

    expect(state.api.listWikiPages).toHaveBeenCalledTimes(2)   // 首拉+重拉一次，封顶
    // 错误态非空白：EmptyState 文案 + 无画布
    expect(wrapper.findComponent(EmptyState).props('text'))
      .toContain('版本切换中，请重试')
    expect(wrapper.findComponent(VueFlow).exists()).toBe(false)
  })

  it('并发守卫（审查 #2）：artifactId 快切时旧响应晚到被丢，不串台', async () => {
    const newPages: WikiPageMeta[] = [{
      page_key: 'new-page', title: '新制品页', page_type: 'topic',
      origin: 'manual', ordinal: 1, flags: null, aliases: [],
      updated_at: '2026-11-02T08:00:00+00:00', updated_by: 'u9',
    }]
    let resolveOldPages!: (value: WikiPagesEnvelope) => void
    let resolveOldLinks!: (value: WikiLinksEnvelope) => void
    state.api.listWikiPages.mockReset()
      .mockImplementationOnce(() => new Promise<WikiPagesEnvelope>(resolve => {
        resolveOldPages = resolve
      }))   // 旧制品请求挂起（慢）
      .mockResolvedValue({ revision: 5, pages: newPages })
    state.api.listWikiLinks.mockReset()
      .mockImplementationOnce(() => new Promise<WikiLinksEnvelope>(resolve => {
        resolveOldLinks = resolve
      }))
      .mockResolvedValue({ revision: 5, links: [] })

    const wrapper = mountGraph()
    await flushPromises()   // 首次请求在飞（未落地）

    await wrapper.setProps({ artifactId: 'art-2' })   // 快切 → watch 触发第二次 reload
    await flushPromises()
    expect(flowProps(wrapper).nodes.map(n => n.id)).toEqual(['new-page'])

    resolveOldPages({ revision: 1, pages: pagesFixture })   // 旧制品慢响应迟到
    resolveOldLinks({ revision: 1, links: linksFixture })
    await flushPromises()

    expect(flowProps(wrapper).nodes.map(n => n.id)).toEqual(['new-page'])   // 旧数据不落地
    expect(state.api.listWikiPages).toHaveBeenCalledTimes(2)
  })

  it('边界：空 Wiki（0 页）→ 空态提示，不渲染画布', async () => {
    state.api.listWikiPages.mockResolvedValue({ revision: 0, pages: [] })
    state.api.listWikiLinks.mockResolvedValue({ revision: 0, links: [] })
    const wrapper = mountGraph()
    await flushPromises()

    const empty = wrapper.findComponent(EmptyState)
    expect(empty.exists()).toBe(true)
    expect(empty.props('text')).toContain('暂无页面')
    expect(wrapper.findComponent(VueFlow).exists()).toBe(false)
  })

  it('边界：无权/加载失败 → 错误态非空白 + 重试入口重拉', async () => {
    state.api.listWikiPages
      .mockRejectedValueOnce({ response: { status: 403 } })
      .mockResolvedValue({ revision: 3, pages: pagesFixture })
    const wrapper = mountGraph()
    await flushPromises()

    expect(wrapper.findComponent(EmptyState).props('text')).toContain('加载失败')
    expect(wrapper.findComponent(VueFlow).exists()).toBe(false)
    const retry = wrapper.find('[data-test="wiki-graph-retry"]')
    expect(retry.exists()).toBe(true)

    await retry.trigger('click')
    await flushPromises()
    expect(state.api.listWikiPages).toHaveBeenCalledTimes(2)
    expect(flowProps(wrapper).nodes).toHaveLength(3)
  })

  it('只读：无任何写操作入口（无新建/保存/删除按钮）', async () => {
    const wrapper = mountGraph(true)
    await flushPromises()

    const buttonLabels = wrapper.findAll('button').map(b => b.text()).join('|')
    for (const banned of ['新建', '保存', '删除', '发布']) {
      expect(buttonLabels).not.toContain(banned)
    }
  })
})
