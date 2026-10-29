/**
 * 设置 → LLM 域覆盖（62号第一期管理面）。
 *
 * 钉三件事：候选域排除已覆盖域、保存/删除走对应 API 并按热加载结果回显、
 * 后端 400 的结构化报文（如「embedding 锁全局」）透传给用户而不是吞成「保存失败」。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'

const api = vi.hoisted(() => ({
  listLlmDomainOverrides: vi.fn(),
  getLlmDomainOverrideRaw: vi.fn(),
  saveLlmDomainOverrideRaw: vi.fn(),
  deleteLlmDomainOverride: vi.fn(),
  getDomains: vi.fn(),
}))

vi.mock('@/api/controlPlane', () => ({ useControlPlaneApi: () => api }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  ElMessageBox: { confirm: vi.fn() },
}))

import { ElMessage, ElMessageBox } from 'element-plus'
import LlmDomainOverridesTab from '../LlmDomainOverridesTab.vue'

const OVERRIDES = [
  { domain_id: 'odn', active_model: 'odn-chat', rerank_model: null },
  { domain_id: 'generic', active_model: 'glm-4-flash', rerank_model: 'rerank-pro' },
]
const DOMAINS = [
  { domain_id: 'odn', display_name: 'ODN' },
  { domain_id: 'generic', display_name: 'Generic' },
  { domain_id: 'cloud_core_network', display_name: 'Cloud Core Network' },
].map(d => ({ ...d, enabled: true, default_channel: 'default', scenario_pack_ref: '' }))

function mountTab() {
  return shallowMount(LlmDomainOverridesTab)
}

describe('LLM 域覆盖 tab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listLlmDomainOverrides.mockResolvedValue(OVERRIDES)
    api.getDomains.mockResolvedValue(DOMAINS)
    api.getLlmDomainOverrideRaw.mockResolvedValue('provider:\n  active_model: odn-chat\n')
    api.saveLlmDomainOverrideRaw.mockResolvedValue({
      ok: true, llm_reload: { ok: true, detail: null },
    })
    api.deleteLlmDomainOverride.mockResolvedValue({
      ok: true, deleted: true, llm_reload: { ok: true, detail: null },
    })
  })

  it('挂载即拉覆盖清单与域清单，条目显示 chat/rerank 摘要', async () => {
    const w = mountTab()
    await flushPromises()
    expect(api.listLlmDomainOverrides).toHaveBeenCalled()
    expect(api.getDomains).toHaveBeenCalled()
    expect(w.text()).toContain('ODN')
    expect(w.text()).toContain('chat: odn-chat · rerank: —')
    expect(w.text()).toContain('chat: glm-4-flash · rerank: rerank-pro')
  })

  it('新增候选排除已覆盖域，创建后预填模板并选中', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as {
      candidates: { domain_id: string }[]
      handleCreate: () => void
      createDomainId: string
    }
    expect(vm.candidates.map(c => c.domain_id)).toEqual(['cloud_core_network'])

    vm.createDomainId = 'cloud_core_network'
    vm.handleCreate()
    await flushPromises()
    expect(w.text()).toContain('cloud_core_network.yaml')
    // 模板含用法说明与示例（全注释形态）
    expect((w.vm as unknown as { text: string }).text).toContain('只写与全局')
    expect((w.vm as unknown as { text: string }).text).toContain('embedding 锁全局')
  })

  it('保存调用 PUT raw 并按热加载成功回显', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as {
      select: (id: string) => Promise<void>
      text: string
      handleSave: () => Promise<void>
    }
    await vm.select('odn')
    vm.text = 'provider:\n  active_model: glm-4-flash\n'
    await vm.handleSave()
    expect(api.saveLlmDomainOverrideRaw).toHaveBeenCalledWith(
      'odn', 'provider:\n  active_model: glm-4-flash\n')
    expect(ElMessage.success).toHaveBeenCalledWith('已保存，已热加载')
  })

  it('热加载失败时保存仍算成功，但回显警告（重启后生效）', async () => {
    api.saveLlmDomainOverrideRaw.mockResolvedValue({
      ok: true, llm_reload: { ok: false, detail: 'llm_service 不可达（ConnectError）' },
    })
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as { select: (id: string) => Promise<void>; handleSave: () => Promise<void> }
    await vm.select('odn')
    await vm.handleSave()
    expect(ElMessage.warning).toHaveBeenCalledWith(
      expect.stringContaining('重启后台服务后生效'))
  })

  it('后端 400 的结构化报文透传（embedding 锁全局）', async () => {
    api.saveLlmDomainOverrideRaw.mockRejectedValue({
      isAxiosError: true,
      response: { data: { detail: "不允许的顶层键: ['embedding']（embedding 锁全局…）" } },
    })
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as { select: (id: string) => Promise<void>; handleSave: () => Promise<void> }
    await vm.select('odn')
    await vm.handleSave()
    expect(ElMessage.error).toHaveBeenCalledWith(
      expect.stringContaining('embedding 锁全局'))
  })

  it('删除走确认框 + DELETE，幂等空删（无 llm_reload）不误报热加载', async () => {
    ;(ElMessageBox.confirm as ReturnType<typeof vi.fn>).mockResolvedValue(undefined)
    api.deleteLlmDomainOverride.mockResolvedValue({ ok: true, deleted: false })
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as {
      select: (id: string) => Promise<void>
      handleDelete: () => Promise<void>
      selectedDomainId: string
    }
    await vm.select('odn')
    await vm.handleDelete()
    expect(api.deleteLlmDomainOverride).toHaveBeenCalledWith('odn')
    expect(ElMessage.success).toHaveBeenCalledWith('已删除覆盖')
    expect(vm.selectedDomainId).toBe('')
  })

  it('取消确认不删除', async () => {
    ;(ElMessageBox.confirm as ReturnType<typeof vi.fn>).mockRejectedValue('cancel')
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as {
      select: (id: string) => Promise<void>
      handleDelete: () => Promise<void>
    }
    await vm.select('odn')
    await vm.handleDelete()
    expect(api.deleteLlmDomainOverride).not.toHaveBeenCalled()
  })
})
