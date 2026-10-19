/**
 * 帮助页（平台指南）：默认章、章树、关键词过滤、上一章/下一章、坏章号兜底。
 * vue-router 按 SettingsViewTab.spec 惯例 mock（useRoute/useRouter 注入不到 global.mocks）。
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'

const routerState = vi.hoisted(() => ({
  params: {} as Record<string, string>,
  push: vi.fn(),
}))
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: routerState.params }),
  useRouter: () => ({ push: routerState.push }),
}))

enableAutoUnmount(afterEach)

import HelpView from '@/views/HelpView.vue'

/**
 * 动态 import 的 md 模块在 vitest 里首次转换可能晚于 flushPromises 的微任务队列，
 * 用 vi.waitFor 轮询等正文出现（坏章号场景不等待——本来就期望无正文）。
 */
async function mountAt(params: Record<string, string> = {}, waitForContent = true) {
  routerState.params = params
  const wrapper = mount(HelpView)
  await flushPromises()
  if (waitForContent) {
    await vi.waitFor(() => {
      expect(wrapper.find('.help-view__content').exists()).toBe(true)
    })
  }
  return wrapper
}

describe('HelpView', () => {
  it('无章号路由回落第 01 章，渲染其正文', async () => {
    const wrapper = await mountAt({})
    expect(wrapper.text()).toContain('平台是什么')
    expect(wrapper.find('.help-view__content').exists()).toBe(true)
  })

  it('章树渲染四部分与全部章节', async () => {
    const wrapper = await mountAt({ chapterId: '01' })
    for (const label of ['开篇', '用户篇', '开发篇', '运维篇']) {
      expect(wrapper.text()).toContain(label)
    }
    expect(wrapper.findAll('.help-view__link')).toHaveLength(23)
  })

  it('关键词过滤只留命中的章（标题匹配）', async () => {
    const wrapper = await mountAt({ chapterId: '01' })
    await vi.waitFor(() => {
      // 等全书预取完成（正文也进入过滤源）
      expect(wrapper.vm.chapterCount).toBe(23)
    })
    wrapper.vm.keyword = 'MCP'
    await flushPromises()
    const links = wrapper.findAll('.help-view__link')
    expect(links.length).toBeGreaterThan(0)
    expect(links.map((l) => l.text()).some((t) => t.includes('MCP 接入'))).toBe(true)
    expect(links.some((l) => l.text().includes('架构总览'))).toBe(false)
  })

  it('上一章/下一章：15 章的邻居是 14 与 16，点击走路由跳转', async () => {
    routerState.push.mockClear()
    const wrapper = await mountAt({ chapterId: '15' })
    expect(wrapper.find('[data-test="help-prev"]').text()).toContain('14')
    expect(wrapper.find('[data-test="help-next"]').text()).toContain('16')
    expect(wrapper.text()).toContain('MCP 接入')

    await wrapper.find('[data-test="help-next"]').trigger('click')
    expect(routerState.push).toHaveBeenCalledWith({
      name: 'help-chapter',
      params: { chapterId: '16' },
    })
  })

  it('首章无上一章；坏章号显示兜底提示', async () => {
    const wrapper = await mountAt({ chapterId: '01' })
    expect(wrapper.find('[data-test="help-prev"]').exists()).toBe(false)

    const bad = await mountAt({ chapterId: '99' }, false)
    expect(bad.find('.help-view__notfound').exists()).toBe(true)
    expect(bad.text()).toContain('没有编号为「99」的章节')
  })
})
