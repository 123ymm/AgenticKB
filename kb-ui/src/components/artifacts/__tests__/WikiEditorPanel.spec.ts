import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'

const state = vi.hoisted(() => ({
  api: {
    listWikiPages: vi.fn(),
    getWikiPage: vi.fn(),
    upsertWikiPage: vi.fn(),
    deleteWikiPage: vi.fn(),
  },
  ui: { confirm: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => 'mock-error' }))
vi.mock('element-plus', () => ({
  ElMessageBox: { confirm: state.ui.confirm },
  ElMessage: {
    success: state.ui.success, error: state.ui.error, warning: state.ui.warning,
  },
}))

import WikiEditorPanel from '../WikiEditorPanel.vue'
import WikiTocDesignPanel from '../WikiTocDesignPanel.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import type { WikiPage, WikiPageMeta } from '@/types/artifacts'

interface EditorVm {
  sortedPages: WikiPageMeta[]
  selectedKey: string
  isNew: boolean
  preview: boolean
  leftView: 'pages' | 'toc'
  tocDirty: boolean
  switchLeftView: (view: 'pages' | 'toc') => Promise<boolean>
  form: {
    title: string
    pageType: string
    pageKey: string
    aliases: string
    tags: string
    ordinal?: number
    bodyMd: string
  }
  frontmatterJson: string
  frontmatterValid: boolean
  loadPages: () => Promise<void>
  selectPage: (pageKey: string) => Promise<void>
  startNewPage: () => void
  save: () => Promise<void>
  removePage: (meta: WikiPageMeta) => Promise<void>
  buildFrontmatter: () => Record<string, unknown> | null
}

/** 清单乱序给（ordinal 2/1/3）——断言前端 ordinal 排序。 */
const metaPages: WikiPageMeta[] = [
  {
    page_key: 'glossary-mop', title: 'MOP 术语', page_type: 'glossary',
    origin: 'machine', ordinal: 2, flags: null, aliases: [],
    updated_at: '2026-10-20T08:00:00+00:00', updated_by: 'u2',
  },
  {
    page_key: 'home', title: '首页', page_type: 'topic',
    origin: 'manual', ordinal: 1, flags: null, aliases: ['主页'],
    updated_at: '2026-10-20T08:00:00+00:00', updated_by: 'u1',
  },
  {
    page_key: 'conflict-1', title: '口径矛盾', page_type: 'contradiction',
    origin: 'manual', ordinal: 3, flags: null, aliases: [],
    updated_at: '2026-10-20T08:00:00+00:00', updated_by: 'u1',
  },
]

const homePage: WikiPage = {
  page_key: 'home', title: '首页', page_type: 'topic', origin: 'manual',
  ordinal: 1, flags: null, aliases: ['主页'],
  updated_at: '2026-10-20T08:00:00+00:00', updated_by: 'u1',
  frontmatter: { aliases: ['主页'], tags: ['入口'] },
  body_md: '# 首页\n\n欢迎', provenance: [],
}

function mountPanel(canWrite = true): VueWrapper {
  return shallowMount(WikiEditorPanel, {
    props: {
      artifactId: 'art-1',
      canWrite,
      expectedUpdatedAt: '2026-10-16T08:00:00+00:00',
      draftOpen: true,
    },
  })
}

beforeEach(() => {
  // D03.1 信封形态：{revision, pages}（清单只消费 pages）
  state.api.listWikiPages.mockReset().mockResolvedValue({ revision: 0, pages: metaPages })
  state.api.getWikiPage.mockReset().mockResolvedValue(homePage)
  state.api.upsertWikiPage.mockReset().mockResolvedValue(homePage)
  state.api.deleteWikiPage.mockReset().mockResolvedValue(undefined)
  state.ui.confirm.mockReset().mockResolvedValue('confirm')
  state.ui.success.mockReset()
  state.ui.error.mockReset()
  state.ui.warning.mockReset()
})

describe('WikiEditorPanel', () => {
  it('renders the page list ordinal-sorted with type/origin badges', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    // 读取走设计态 draft 视图
    expect(state.api.listWikiPages).toHaveBeenCalledWith('art-1', { draft: true })
    // ordinal 排序：1 首页 → 2 术语 → 3 矛盾
    expect(vm.sortedPages.map(p => p.page_key)).toEqual(['home', 'glossary-mop', 'conflict-1'])

    const rows = wrapper.findAll('[data-test="wiki-page-item"]')
    expect(rows).toHaveLength(3)
    expect(rows[0].text()).toContain('首页')
    expect(rows[0].text()).toContain('主题')     // page_type 徽标
    expect(rows[0].text()).toContain('manual')   // origin 徽标
    expect(rows[1].text()).toContain('术语')
    expect(rows[1].text()).toContain('machine')
    expect(rows[2].text()).toContain('矛盾')
  })

  it('fills the edit form from getWikiPage when a page is selected', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    await vm.selectPage('home')
    await flushPromises()

    expect(state.api.getWikiPage).toHaveBeenCalledWith('art-1', 'home', { draft: true })
    expect(vm.selectedKey).toBe('home')
    expect(vm.form.title).toBe('首页')
    expect(vm.form.pageType).toBe('topic')
    expect(vm.form.pageKey).toBe('home')
    expect(vm.form.aliases).toBe('主页')
    expect(vm.form.bodyMd).toBe('# 首页\n\n欢迎')
    // frontmatter JSON 区展示全量（含专属输入框覆盖不到的键）
    expect(vm.frontmatterJson).toContain('tags')
  })

  it('saves with the lock token, split aliases/tags and the stable page_key', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    await vm.selectPage('home')
    await flushPromises()
    vm.form.aliases = '主页, 别称'
    vm.form.tags = '入口，导航'
    await vm.save()
    await flushPromises()

    // URL 寻址键=原 page_key（title 改名不漂移）；hex 编码在 API 客户端内做
    expect(state.api.upsertWikiPage).toHaveBeenCalledWith('art-1', 'home', {
      title: '首页',
      page_type: 'topic',
      frontmatter: {
        aliases: ['主页', '别称'],   // 中英文逗号均拆分
        tags: ['入口', '导航'],
      },
      body_md: '# 首页\n\n欢迎',
      ordinal: 1,
      page_key: 'home',
      expected_updated_at: '2026-10-16T08:00:00+00:00',
    })
    expect(wrapper.emitted('changed')).toBeTruthy()
  })

  it('creates a new page keyed by title and reselects the backend-normalized key', async () => {
    state.api.upsertWikiPage.mockResolvedValueOnce({
      ...homePage, page_key: 'normalized-new', title: '新建页',
    })
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    vm.startNewPage()
    expect(vm.isNew).toBe(true)
    vm.form.title = '新建页'
    vm.form.aliases = '甲, 乙'
    await vm.save()
    await flushPromises()

    // 新建流：page_key 字段空 → URL 寻址键=title 原文，body 不带 page_key（后端归一）
    expect(state.api.upsertWikiPage).toHaveBeenCalledWith('art-1', '新建页', {
      title: '新建页',
      page_type: 'topic',
      frontmatter: { aliases: ['甲', '乙'], tags: [] },
      body_md: '',
      ordinal: 4,   // 缺省=当前页数+1
      expected_updated_at: '2026-10-16T08:00:00+00:00',
    })
    // 响应 200 后按后端返回 page_key 刷新选中（归一结果可能 ≠ title 原文）
    expect(vm.selectedKey).toBe('normalized-new')
    expect(vm.isNew).toBe(false)
    expect(state.api.getWikiPage).toHaveBeenLastCalledWith('art-1', 'normalized-new', { draft: true })
  })

  it('surfaces stale_artifact 409, emits changed and reloads the list', async () => {
    state.api.upsertWikiPage.mockRejectedValueOnce({
      response: { status: 409, data: { detail: { code: 'stale_artifact' } } },
    })
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    await vm.selectPage('home')
    await flushPromises()
    state.api.listWikiPages.mockClear()
    await vm.save()
    await flushPromises()

    expect(state.ui.error).toHaveBeenCalledWith('页面已被他人修改，已刷新最新状态')
    expect(wrapper.emitted('changed')).toBeTruthy()
    expect(state.api.listWikiPages).toHaveBeenCalledTimes(1)   // 重新 loadPages
  })

  it('blocks saving when the frontmatter JSON area is malformed', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    await vm.selectPage('home')
    await flushPromises()
    vm.frontmatterJson = '{ not json'
    expect(vm.frontmatterValid).toBe(false)
    expect(vm.buildFrontmatter()).toBeNull()

    await vm.save()
    await flushPromises()
    expect(state.ui.warning).toHaveBeenCalled()
    expect(state.api.upsertWikiPage).not.toHaveBeenCalled()
  })

  it('deletes a page after confirmation and collapses the selection', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    await vm.selectPage('home')
    await flushPromises()
    await vm.removePage(vm.sortedPages[0])
    await flushPromises()

    expect(state.api.deleteWikiPage).toHaveBeenCalledWith(
      'art-1', 'home', '2026-10-16T08:00:00+00:00')
    expect(wrapper.emitted('changed')).toBeTruthy()
    expect(vm.selectedKey).toBe('')   // 被删页收起表单
    expect(state.ui.success).toHaveBeenCalled()
  })

  it('hides write controls and reads the runtime view when canWrite=false', async () => {
    const wrapper = mountPanel(false)
    await flushPromises()

    // viewer 读取走 runtime 视图（draft=false 读 current_revision）
    expect(state.api.listWikiPages).toHaveBeenCalledWith('art-1', { draft: false })
    expect(wrapper.find('[data-test="wiki-save"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="wiki-new-page"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-test="wiki-page-delete"]')).toHaveLength(0)
  })

  it('toggles markdown preview rendering the body with v-html', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    await vm.selectPage('home')
    await flushPromises()
    expect(wrapper.find('[data-test="wiki-preview-body"]').exists()).toBe(false)

    vm.preview = true
    await nextTick()
    const previewEl = wrapper.find('[data-test="wiki-preview-body"]')
    expect(previewEl.exists()).toBe(true)
    // 真实 marked+DOMPurify 渲染（utils/markdown.ts）
    expect(previewEl.html()).toContain('<h1')
    expect(previewEl.text()).toContain('首页')
  })

  it('shows the empty state when the artifact has no pages', async () => {
    state.api.listWikiPages.mockResolvedValueOnce({ revision: 0, pages: [] })
    const wrapper = mountPanel()
    await flushPromises()

    const empty = wrapper.findComponent(EmptyState)
    expect(empty.exists()).toBe(true)
    expect(empty.props('text')).toBe('暂无页面，点击新建开始')
  })
})

// ── 目录编辑入口（66号 D03：设计态左栏「页面清单｜目录」切换）──

describe('WikiEditorPanel 目录编辑入口（D03）', () => {
  it('切到目录视图：挂载 WikiTocDesignPanel 并传排序后页清单；页表单让位', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm
    expect(wrapper.findComponent(WikiTocDesignPanel).exists()).toBe(false)

    await wrapper.find('[data-test="wiki-left-toc"]').trigger('click')
    await flushPromises()

    expect(vm.leftView).toBe('toc')
    const panel = wrapper.findComponent(WikiTocDesignPanel)
    expect(panel.exists()).toBe(true)
    expect(panel.props('artifactId')).toBe('art-1')
    // pages=sortedPages（ordinal 排序口径与页清单一致）
    expect((panel.props('pages') as WikiPageMeta[]).map(p => p.page_key))
      .toEqual(['home', 'glossary-mop', 'conflict-1'])
    // 右栏被目录编辑占据——页编辑表单隐藏
    expect(wrapper.find('[data-test="wiki-save"]').exists()).toBe(false)

    // 切回页面清单：目录面板卸载、表单恢复
    await wrapper.find('[data-test="wiki-left-pages"]').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(WikiTocDesignPanel).exists()).toBe(false)
  })

  it('viewer 无目录编辑入口（目录编辑是 editor+ 能力）', async () => {
    const wrapper = mountPanel(false)
    await flushPromises()

    expect(wrapper.find('[data-test="wiki-left-toc"]').exists()).toBe(false)
  })

  it('未保存切换守卫：目录 dirty 时离开需确认——取消留在目录视图，确认后离开（66号 §6）', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    await vm.switchLeftView('toc')
    wrapper.findComponent(WikiTocDesignPanel).vm.$emit('dirty', true)
    await flushPromises()
    expect(vm.tocDirty).toBe(true)

    // 用户取消 → 留在目录视图（编辑不丢）
    state.ui.confirm.mockRejectedValueOnce('cancel')
    await expect(vm.switchLeftView('pages')).resolves.toBe(false)
    expect(vm.leftView).toBe('toc')
    expect(state.ui.confirm).toHaveBeenCalledTimes(1)

    // 用户确认 → 放弃修改离开
    state.ui.confirm.mockResolvedValueOnce('confirm')
    await expect(vm.switchLeftView('pages')).resolves.toBe(true)
    expect(vm.leftView).toBe('pages')
    expect(vm.tocDirty).toBe(false)
  })

  it('目录视图下点击左栏页项：dirty 时取消确认则不选页；确认后选页', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as EditorVm

    await vm.switchLeftView('toc')
    wrapper.findComponent(WikiTocDesignPanel).vm.$emit('dirty', true)
    await flushPromises()

    state.ui.confirm.mockRejectedValueOnce('cancel')
    await vm.selectPage('home')
    await flushPromises()
    expect(vm.leftView).toBe('toc')
    expect(state.api.getWikiPage).not.toHaveBeenCalled()

    state.ui.confirm.mockResolvedValueOnce('confirm')
    await vm.selectPage('home')
    await flushPromises()
    expect(vm.leftView).toBe('pages')
    expect(state.api.getWikiPage).toHaveBeenCalledWith('art-1', 'home', { draft: true })
  })
})
