import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  api: { listMembers: vi.fn(), addMember: vi.fn(), removeMember: vi.fn() },
  auth: { listDomainUserCandidates: vi.fn() },
}))

vi.mock('@/api/artifacts', () => ({ useArtifactsApi: () => state.api }))
vi.mock('@/api/auth', () => ({ useAuthApi: () => state.auth }))
vi.mock('@/api/proxyClient', () => ({ apiErrorDetail: async () => 'mock-error' }))
vi.mock('@/stores/domain', () => ({ useDomainStore: () => ({ currentDomain: 'plant-a' }) }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  ElMessageBox: { confirm: vi.fn() },
}))

import ArtifactMembersPanel from '../ArtifactMembersPanel.vue'
import type { MemberInfo } from '@/types/artifacts'

// 手写桩（与 UsagePanel.spec 同套路）：表格行渲染走 default 作用域插槽固定一行；
// RouterLink 桩把 to 暴露为 href 供 M01 钥匙引导链接断言。
const members: MemberInfo[] = [
  { user_id: 'u1', user_name: '张工', username: 'zhang', role: 'owner' },
  { user_id: 'u2', user_name: '李工', username: 'li', role: 'viewer' },
]

const stubs = {
  ElTable: { template: '<div class="el-table-stub"><slot /><slot name="empty" /></div>' },
  ElTableColumn: {
    props: ['label'],
    template: '<div class="el-table-column-stub">{{ label }}<slot :row="scopeRow" /></div>',
    data: () => ({ scopeRow: members[0] }),
  },
  ElTag: { template: '<span class="el-tag-stub"><slot /></span>' },
  ElButton: { template: '<button class="el-button-stub"><slot /></button>' },
  ElSelect: { template: '<div class="el-select-stub"><slot /></div>' },
  ElOption: { props: ['label'], template: '<div class="el-option-stub">{{ label }}</div>' },
  EmptyState: { props: ['text'], template: '<div class="empty-stub">{{ text }}</div>' },
  RouterLink: {
    props: ['to'],
    template: '<a class="router-link-stub" :href="to"><slot /></a>',
  },
}

function mountPanel(overrides: { canManage?: boolean; visibility?: string } = {}): VueWrapper {
  return mount(ArtifactMembersPanel, {
    props: {
      artifactId: 'art-1',
      canManage: overrides.canManage ?? true,
      ...(overrides.visibility ? { visibility: overrides.visibility } : {}),
    },
    global: { stubs, directives: { loading: {} } },
  })
}

beforeEach(() => {
  state.api.listMembers.mockReset().mockResolvedValue(members)
  state.api.addMember.mockReset()
  state.api.removeMember.mockReset()
  state.auth.listDomainUserCandidates.mockReset().mockResolvedValue([])
})

describe('ArtifactMembersPanel — 成员管理基础行为', () => {
  it('挂载即拉成员列表并渲染角色标签', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(state.api.listMembers).toHaveBeenCalledWith('art-1')
    expect(wrapper.text()).toContain('张工')
    expect(wrapper.text()).toContain('拥有者')
  })

  it('非 owner：隐藏添加入口并提示仅 owner 可管理', async () => {
    const wrapper = mountPanel({ canManage: false })
    await flushPromises()

    expect(wrapper.find('[data-test="member-add"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('仅拥有者（owner）可管理成员')
  })
})

describe('ArtifactMembersPanel — M01 可见性与钥匙边界（66号 §11）', () => {
  it('public 制品：可见性=本域公开（本域成员可见），点明不等于互联网匿名公开、仅 owner 可修改', async () => {
    const wrapper = mountPanel({ visibility: 'public' })
    await flushPromises()

    const hint = wrapper.find('[data-test="visibility-hint"]')
    expect(hint.exists()).toBe(true)
    expect(hint.text()).toContain('本域公开')
    expect(hint.text()).toContain('不等于互联网匿名公开')
    expect(hint.text()).toContain('owner')
  })

  it('private 制品：可见性=私有（仅成员可见）', async () => {
    const wrapper = mountPanel({ visibility: 'private' })
    await flushPromises()

    const hint = wrapper.find('[data-test="visibility-hint"]')
    expect(hint.text()).toContain('私有')
    expect(hint.text()).not.toContain('本域公开')
  })

  it('Agent 引导：制品对 Agent 可用还需钥匙明确开放（三条件），指引 MCP 配置且链接 /mcp', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const hint = wrapper.find('[data-test="agent-key-hint"]')
    expect(hint.exists()).toBe(true)
    // 三条件齐备口径（与运行态 ArtifactUsagePanel 三条件区同源）
    expect(hint.text()).toContain('正式发布')
    expect(hint.text()).toContain('钥匙')
    expect(hint.text()).toContain('请到 MCP 配置检查')
    expect(hint.find('a').attributes('href')).toBe('/mcp')
  })

  it('M01 铁律：不查询/推测钥匙状态，不提供替任意 key 开放的按钮', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    // 不显示推测性的「已全部开放」；也没有一键开放类替 key 管理入口
    expect(wrapper.text()).not.toContain('已全部开放')
    expect(wrapper.text()).not.toContain('一键开放')
    expect(wrapper.findAll('button').map(b => b.text()).join()).not.toContain('开放')
  })
})
