import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  api: {
    getWikiToc: vi.fn(),
    saveWikiToc: vi.fn(),
  },
  ui: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => 'mock-error' }))
vi.mock('element-plus', () => ({
  ElMessage: {
    success: state.ui.success, error: state.ui.error, warning: state.ui.warning,
  },
}))

import WikiTocDesignPanel from '../WikiTocDesignPanel.vue'
import type { WikiPageMeta, WikiTocSnapshot } from '@/types/artifacts'

/** 页清单样例（ordinal 乱序给——未分区列表按传入序展示，节内按节声明序）。 */
const metaPages: WikiPageMeta[] = [
  {
    page_key: 'home', title: '首页', page_type: 'topic', origin: 'manual',
    ordinal: 1, flags: null, aliases: [],
    updated_at: '2026-10-22T08:00:00+00:00', updated_by: 'u1',
  },
  {
    page_key: 'glossary-mop', title: 'MOP 术语', page_type: 'glossary',
    origin: 'machine', ordinal: 2, flags: null, aliases: [],
    updated_at: '2026-10-22T08:00:00+00:00', updated_by: 'u2',
  },
  {
    page_key: 'conflict-1', title: '口径矛盾', page_type: 'contradiction',
    origin: 'manual', ordinal: 3, flags: null, aliases: [],
    updated_at: '2026-10-22T08:00:00+00:00', updated_by: 'u1',
  },
]

const TOKEN = '2026-10-22T08:00:00+00:00'
const NEW_TOKEN = '2026-10-22T09:00:00+00:00'

function draftToc(sections: WikiTocSnapshot['sections'], extra: Partial<WikiTocSnapshot> = {}): WikiTocSnapshot {
  return { sections, flat: false, pages_total: 3, updated_at: TOKEN, updated_by: 'u1', ...extra }
}

interface PanelVm {
  sections: { sectionTitle: string; pageKeys: string[] }[]
  unassignedPages: WikiPageMeta[]
  updatedAt: string | undefined
  flat: boolean
  dirty: boolean
  load: () => Promise<void>
  save: () => Promise<void>
  addSection: () => void
  removeSection: (index: number) => void
  moveSection: (index: number, delta: -1 | 1) => void
  movePage: (pageKey: string, targetIndex: number) => void
}

function mountPanel(): VueWrapper {
  return shallowMount(WikiTocDesignPanel, { props: { artifactId: 'art-1', pages: metaPages } })
}

beforeEach(() => {
  state.api.getWikiToc.mockReset().mockResolvedValue(draftToc([
    { section_title: '基础', page_keys: ['home'] },
    { section_title: '进阶', page_keys: ['glossary-mop', 'conflict-1'] },
  ]))
  state.api.saveWikiToc.mockReset().mockResolvedValue({
    artifact_id: 'art-1', updated_at: NEW_TOKEN, updated_by: 'u1',
  })
  state.ui.success.mockReset()
  state.ui.error.mockReset()
  state.ui.warning.mockReset()
})

describe('WikiTocDesignPanel（D03 目录编辑）', () => {
  it('加载 draft toc：按节分组渲染节/节内页，未分区空；无 flat 回落提示', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as PanelVm

    // 编辑器读设计态 draft 口径（updated_at=目录编辑乐观锁令牌来源）
    expect(state.api.getWikiToc).toHaveBeenCalledWith('art-1', { draft: true })

    const sections = wrapper.findAll('[data-test="toc-section"]')
    expect(sections).toHaveLength(2)
    // 节名在 el-input（stub 不渲染值）——形态走 vm 断言
    expect(vm.sections.map(s => s.sectionTitle)).toEqual(['基础', '进阶'])
    expect(sections[0].findAll('[data-test="toc-section-page"]')).toHaveLength(1)
    expect(sections[1].findAll('[data-test="toc-section-page"]')).toHaveLength(2)

    // 三页全在节内 → 未分区空
    expect(wrapper.findAll('[data-test="toc-unassigned-page"]')).toHaveLength(0)
    // flat=false 不出现平铺回落提示
    expect(wrapper.find('[data-test="toc-flat-hint"]').exists()).toBe(false)
  })

  it('无 toc 行（flat）→ 平铺回落提示可见，全部页入未分区（Task 1.3 M-4）', async () => {
    state.api.getWikiToc.mockResolvedValueOnce({ sections: [], flat: true, pages_total: 3 })
    const wrapper = mountPanel()
    await flushPromises()

    const hint = wrapper.find('[data-test="toc-flat-hint"]')
    expect(hint.exists()).toBe(true)
    expect(hint.text()).toContain('目录未生成')
    expect(wrapper.findAll('[data-test="toc-unassigned-page"]')).toHaveLength(3)
    expect(wrapper.findAll('[data-test="toc-section"]')).toHaveLength(0)
  })

  it('加载失败 → 错误提示并回落平铺编辑（不白屏）', async () => {
    state.api.getWikiToc.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountPanel()
    await flushPromises()

    expect(state.ui.error).toHaveBeenCalledWith(expect.stringContaining('目录加载失败'))
    expect(wrapper.find('[data-test="toc-flat-hint"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-test="toc-unassigned-page"]')).toHaveLength(3)
  })

  it('改节名保存：全量 sections + 令牌=GET draft 的 updated_at；新令牌回存供下次编辑', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as PanelVm
    expect(vm.updatedAt).toBe(TOKEN)

    vm.sections[0].sectionTitle = '基础概念'
    await vm.save()
    await flushPromises()

    // 全量 PUT + 乐观锁令牌（GET draft 返回的 updated_at）
    expect(state.api.saveWikiToc).toHaveBeenCalledWith('art-1', {
      sections: [
        { section_title: '基础概念', page_keys: ['home'] },
        { section_title: '进阶', page_keys: ['glossary-mop', 'conflict-1'] },
      ],
      expected_updated_at: TOKEN,
    })
    // 新令牌回存：再次编辑保存携带新 updated_at
    expect(vm.updatedAt).toBe(NEW_TOKEN)
    expect(vm.dirty).toBe(false)
    expect(wrapper.emitted('saved')).toBeTruthy()
    expect(state.ui.success).toHaveBeenCalled()

    vm.sections[1].sectionTitle = '进阶专题'
    await vm.save()
    await flushPromises()
    expect(state.api.saveWikiToc).toHaveBeenLastCalledWith('art-1', {
      sections: [
        { section_title: '基础概念', page_keys: ['home'] },
        { section_title: '进阶专题', page_keys: ['glossary-mop', 'conflict-1'] },
      ],
      expected_updated_at: NEW_TOKEN,
    })
  })

  it('首次创建（draft 无 updated_at）→ 保存载荷不带 expected_updated_at', async () => {
    state.api.getWikiToc.mockResolvedValueOnce({ sections: [], flat: true, pages_total: 3 })
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as PanelVm
    expect(vm.updatedAt).toBeUndefined()

    vm.addSection()
    vm.sections[0].sectionTitle = '新节'
    await vm.movePage('home', 0)
    await vm.save()
    await flushPromises()

    expect(state.api.saveWikiToc).toHaveBeenCalledWith('art-1', {
      sections: [{ section_title: '新节', page_keys: ['home'] }],
    })
    expect(vm.updatedAt).toBe(NEW_TOKEN)
  })

  it('移动页到节：从旧节移除并入目标节；-1=移回未分区', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as PanelVm

    await vm.movePage('home', 1)   // 基础 → 进阶
    expect(vm.sections[0].pageKeys).toEqual([])
    expect(vm.sections[1].pageKeys).toEqual(['glossary-mop', 'conflict-1', 'home'])

    await vm.movePage('glossary-mop', -1)   // 进阶 → 未分区
    expect(vm.sections[1].pageKeys).toEqual(['conflict-1', 'home'])
    expect(vm.unassignedPages.map(p => p.page_key)).toEqual(['glossary-mop'])
    // 未分区页渲染可见
    expect(wrapper.findAll('[data-test="toc-unassigned-page"]')).toHaveLength(1)

    await vm.save()
    await flushPromises()
    expect(state.api.saveWikiToc).toHaveBeenCalledWith('art-1', {
      sections: [
        { section_title: '基础', page_keys: [] },
        { section_title: '进阶', page_keys: ['conflict-1', 'home'] },
      ],
      expected_updated_at: TOKEN,
    })
  })

  it('增删节与节排序：删节的页回落未分区；上/下移交换节序', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as PanelVm

    vm.addSection()
    expect(vm.sections).toHaveLength(3)
    expect(vm.sections[2].sectionTitle).not.toBe('')

    vm.moveSection(0, 1)   // 基础下移 ↔ 进阶上移
    expect(vm.sections.map(s => s.sectionTitle)).toEqual(['进阶', '基础', '第 3 节'])
    vm.moveSection(1, -1)  // 移回
    expect(vm.sections[0].sectionTitle).toBe('基础')

    vm.removeSection(1)    // 删「进阶」→ 节内两页回落未分区
    expect(vm.sections).toHaveLength(2)
    expect(vm.unassignedPages.map(p => p.page_key)).toEqual(['glossary-mop', 'conflict-1'])
  })

  it('dirty 流转：编辑触发 dirty=true；保存成功复位 false', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as PanelVm

    expect(vm.dirty).toBe(false)
    await vm.movePage('home', 1)
    expect(vm.dirty).toBe(true)
    // 父级切换守卫依据的 dirty 事件已发出
    const dirtyEvents = wrapper.emitted('dirty')
    expect(dirtyEvents?.at(-1)).toEqual([true])

    await vm.save()
    await flushPromises()
    expect(vm.dirty).toBe(false)
    expect(wrapper.emitted('dirty')?.at(-1)).toEqual([false])
  })

  it('409 stale_artifact → 提示已被他人修改并重新拉取最新目录', async () => {
    state.api.saveWikiToc.mockRejectedValueOnce({
      response: { status: 409, data: { detail: { code: 'stale_artifact' } } },
    })
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as PanelVm

    await vm.movePage('home', 1)
    await vm.save()
    await flushPromises()

    expect(state.ui.error).toHaveBeenCalledWith(expect.stringContaining('已被他人修改'))
    // 重新 GET（服务端最新令牌+目录落地，本地的半程编辑被服务端形态取代）
    expect(state.api.getWikiToc).toHaveBeenCalledTimes(2)
  })

  it('空节名阻断保存（后端 422 前置拦截）', async () => {
    const wrapper = mountPanel()
    await flushPromises()
    const vm = wrapper.vm as unknown as PanelVm

    vm.sections[0].sectionTitle = '   '
    await vm.save()
    await flushPromises()

    expect(state.ui.warning).toHaveBeenCalledWith(expect.stringContaining('节名'))
    expect(state.api.saveWikiToc).not.toHaveBeenCalled()
  })
})
