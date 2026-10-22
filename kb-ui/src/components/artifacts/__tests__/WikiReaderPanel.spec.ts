import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  api: {
    listWikiPages: vi.fn(),
    getWikiPage: vi.fn(),
    listWikiLinks: vi.fn(),
  },
  ui: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
  clipboard: { writeText: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => 'mock-error' }))
vi.mock('element-plus', () => ({
  ElMessage: {
    success: state.ui.success, error: state.ui.error,
    warning: state.ui.warning, info: state.ui.info,
  },
}))

import WikiReaderPanel from '../WikiReaderPanel.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import type { WikiLink, WikiPage, WikiPageMeta } from '@/types/artifacts'

interface ReaderVm {
  sortedPages: WikiPageMeta[]
  selectedKey: string
  outLinks: WikiLink[]
  inLinks: WikiLink[]
  reload: () => Promise<void>
}

/** ordinal 乱序给（术语=1 / 首页=2）——目录按 ordinal 排序且自动选中第一页=术语页。 */
const metaPages: WikiPageMeta[] = [
  {
    page_key: 'home', title: '首页', page_type: 'topic', origin: 'manual',
    ordinal: 2, flags: null, aliases: ['主页'],
    updated_at: '2026-10-21T08:00:00+00:00', updated_by: 'u1',
  },
  {
    page_key: 'glossary-mop', title: 'MOP 术语', page_type: 'glossary',
    origin: 'machine', ordinal: 1, flags: null, aliases: [],
    updated_at: '2026-10-21T09:00:00+00:00', updated_by: 'u2',
  },
]

const glossaryPage: WikiPage = {
  page_key: 'glossary-mop', title: 'MOP 术语', page_type: 'glossary',
  origin: 'machine', ordinal: 1, flags: null, aliases: [],
  updated_at: '2026-10-21T09:00:00+00:00', updated_by: 'u2',
  frontmatter: {}, body_md: '术语正文。参见 [[home]]、别名 [[主页]]、断链 [[ghost]]，来源 [[src:unit-9]]',
  provenance: [{ kb_id: 'kb-1', snapshot_id: 'snap-1', unit_ids: ['unit-9', 'unit-10'] }],
}

const homePage: WikiPage = {
  page_key: 'home', title: '首页', page_type: 'topic', origin: 'manual',
  ordinal: 2, flags: null, aliases: ['主页'],
  updated_at: '2026-10-21T08:00:00+00:00', updated_by: 'u1',
  frontmatter: {}, body_md: '首页正文', provenance: [],
}

const outLinks: WikiLink[] = [
  { from_page: 'glossary-mop', to_page: 'home', anchor_text: '首页', status: 'valid' },
  { from_page: 'glossary-mop', to_page: 'gone-page', anchor_text: '已删页', status: 'broken' },
]
const inLinks: WikiLink[] = [
  { from_page: 'home', to_page: 'glossary-mop', anchor_text: '术语', status: 'valid' },
]

function mountReader(): VueWrapper {
  return shallowMount(WikiReaderPanel, { props: { artifactId: 'art-1' } })
}

beforeEach(() => {
  state.api.listWikiPages.mockReset().mockResolvedValue(metaPages)
  state.api.getWikiPage.mockReset().mockImplementation(
    (_id: string, key: string) => Promise.resolve(key === 'home' ? homePage : glossaryPage))
  state.api.listWikiLinks.mockReset().mockImplementation(
    (_id: string, params?: { direction?: 'out' | 'in' }) =>
      Promise.resolve(params?.direction === 'out' ? outLinks : inLinks))
  state.ui.success.mockReset()
  state.ui.error.mockReset()
  state.ui.warning.mockReset()
  state.ui.info.mockReset()
  state.clipboard.writeText.mockReset().mockResolvedValue(undefined)
  // clipboard 可用态（jsdom 默认无 navigator.clipboard——逐用例定义/删除）
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: state.clipboard.writeText }, configurable: true,
  })
})

describe('WikiReaderPanel', () => {
  it('目录按 ordinal 排序、自动加载第一页并渲染正文（运行态 draft=false）', async () => {
    const wrapper = mountReader()
    await flushPromises()
    const vm = wrapper.vm as unknown as ReaderVm

    // 阅读器读运行态（current_revision 投影，不带 draft）
    expect(state.api.listWikiPages).toHaveBeenCalledWith('art-1', { draft: false })
    expect(vm.sortedPages.map(p => p.page_key)).toEqual(['glossary-mop', 'home'])

    const items = wrapper.findAll('[data-test="wiki-reader-toc-item"]')
    expect(items).toHaveLength(2)
    expect(items[0].text()).toContain('MOP 术语')
    expect(items[0].text()).toContain('术语')       // page_type 徽标
    expect(items[0].classes()).toContain('is-active')   // 第一页自动选中高亮

    expect(state.api.getWikiPage).toHaveBeenCalledWith('art-1', 'glossary-mop', { draft: false })
    const body = wrapper.find('[data-test="wiki-reader-body"]')
    expect(body.exists()).toBe(true)
    // 真实 renderWikiMarkdown 产物（utils/markdown.ts）
    expect(body.find('[data-wiki-page="home"]').exists()).toBe(true)
    expect(body.find('sup.wiki-src[data-unit-id="unit-9"]').exists()).toBe(true)
  })

  it('双链点击切页：page_key 命中 → 加载目标页并高亮', async () => {
    const wrapper = mountReader()
    await flushPromises()

    await wrapper.find('[data-wiki-page="home"]').trigger('click')
    await flushPromises()
    const vm = wrapper.vm as unknown as ReaderVm

    expect(state.api.getWikiPage).toHaveBeenLastCalledWith('art-1', 'home', { draft: false })
    expect(vm.selectedKey).toBe('home')
    expect(wrapper.text()).toContain('首页正文')
  })

  it('双链点击切页：alias 命中（来自目录 metas.aliases）→ 解析到正主页', async () => {
    const wrapper = mountReader()
    await flushPromises()

    // getWikiPage 返回体无顶层 aliases 可信口径——alias 解析只依赖 listWikiPages 的 metas
    await wrapper.find('[data-wiki-page="主页"]').trigger('click')
    await flushPromises()

    expect(state.api.getWikiPage).toHaveBeenLastCalledWith('art-1', 'home', { draft: false })
    expect(state.ui.warning).not.toHaveBeenCalled()
  })

  it('双链点击未命中 → warning 且不发起加载', async () => {
    const wrapper = mountReader()
    await flushPromises()

    await wrapper.find('[data-wiki-page="ghost"]').trigger('click')
    await flushPromises()

    expect(state.ui.warning).toHaveBeenCalledWith('目标页面未发布')
    expect(state.api.getWikiPage).toHaveBeenCalledTimes(1)   // 仍是初始那一次
  })

  it('溯源角标点击复制 unit_id；clipboard 不可用降级 info 展示', async () => {
    const wrapper = mountReader()
    await flushPromises()

    await wrapper.find('sup.wiki-src[data-unit-id="unit-9"]').trigger('click')
    await flushPromises()
    expect(state.clipboard.writeText).toHaveBeenCalledWith('unit-9')
    expect(state.ui.success).toHaveBeenCalledWith('已复制切片 ID')

    // 降级：无 navigator.clipboard（如非安全上下文）
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    await wrapper.find('sup.wiki-src[data-unit-id="unit-9"]').trigger('click')
    await flushPromises()
    expect(state.ui.info).toHaveBeenCalledWith(expect.stringContaining('unit-9'))
  })

  it('上下文侧栏：链接/被链渲染 + broken 标红 + 溯源 kb/快照/切片计数', async () => {
    const wrapper = mountReader()
    await flushPromises()

    const out = wrapper.findAll('[data-test="wiki-reader-out-link"]')
    expect(out).toHaveLength(2)
    expect(out[1].classes()).toContain('is-broken')
    expect(out[1].text()).toContain('断链')
    const ins = wrapper.findAll('[data-test="wiki-reader-in-link"]')
    expect(ins).toHaveLength(1)
    expect(ins[0].text()).toContain('home')

    const prov = wrapper.find('[data-test="wiki-reader-provenance"]')
    expect(prov.exists()).toBe(true)
    expect(prov.text()).toContain('kb-1')
    expect(prov.text()).toContain('snap-1')
    expect(prov.text()).toContain('2')   // unit_ids 计数
  })

  it('未发布（listWikiPages 空）→ EmptyState 尚未发布版本', async () => {
    state.api.listWikiPages.mockResolvedValueOnce([])
    const wrapper = mountReader()
    await flushPromises()

    const empty = wrapper.findComponent(EmptyState)
    expect(empty.exists()).toBe(true)
    expect(empty.props('text')).toContain('尚未发布版本')
    expect(wrapper.find('[data-test="wiki-reader-body"]').exists()).toBe(false)
  })

  it('清单加载失败 → EmptyState 错误文案，不白屏', async () => {
    state.api.listWikiPages.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountReader()
    await flushPromises()

    const empty = wrapper.findComponent(EmptyState)
    expect(empty.exists()).toBe(true)
    expect(empty.props('text')).toContain('加载失败')
  })

  it('reload：外部命令式刷新（发布后父级可调）重拉清单与当前页', async () => {
    const wrapper = mountReader()
    await flushPromises()
    const vm = wrapper.vm as unknown as ReaderVm

    await vm.reload()
    await flushPromises()

    expect(state.api.listWikiPages).toHaveBeenCalledTimes(2)
    // 当前选中页（glossary-mop）在 reload 后被重新加载
    expect(state.api.getWikiPage).toHaveBeenLastCalledWith('art-1', 'glossary-mop', { draft: false })
  })
})
