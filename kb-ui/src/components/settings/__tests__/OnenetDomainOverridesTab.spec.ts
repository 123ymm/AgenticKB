/**
 * 设置 → 一张网域覆盖（62号 LLM 域覆盖的镜像通路）。
 *
 * 钉住的行为：候选域排除已覆盖域、模板预填、全局基底继承 placeholder（token
 * 不回显）、平铺键表单写回 YAML 单一真相源、保存提示 ~10s 生效（无热加载
 * 字段、无 embedding 式确认框）、后端 400 结构化透传、删除确认/幂等。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'

const api = vi.hoisted(() => ({
  listOnenetDomainOverrides: vi.fn(),
  getOnenetDomainOverrideRaw: vi.fn(),
  saveOnenetDomainOverrideRaw: vi.fn(),
  deleteOnenetDomainOverride: vi.fn(),
  getDomains: vi.fn(),
  getSystemConfig: vi.fn(),
}))

vi.mock('@/api/controlPlane', () => ({ useControlPlaneApi: () => api }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  ElMessageBox: { confirm: vi.fn() },
}))

import { ElMessage, ElMessageBox } from 'element-plus'
import OnenetDomainOverridesTab from '../OnenetDomainOverridesTab.vue'

const OVERRIDES = [
  { domain_id: 'odn', app_id: 'odn-app' },
  { domain_id: 'generic', app_id: null },
]
const DOMAINS = [
  { domain_id: 'odn', display_name: 'ODN' },
  { domain_id: 'generic', display_name: 'Generic' },
  { domain_id: 'cloud_core_network', display_name: 'Cloud Core Network' },
].map(d => ({ ...d, enabled: true, default_channel: 'default', scenario_pack_ref: '' }))

/** 全局基底（onenet.yaml 解析值）：继承 placeholder 来源；token 不回显。 */
const GLOBAL_ONENET = {
  app_id: 'global-app',
  static_token: 'global-tok',
  token_url: 'http://global.example/token',
  search_url: 'http://global.example/search',
}

function mountTab() {
  return shallowMount(OnenetDomainOverridesTab)
}

type TabVm = {
  select: (id: string) => Promise<void>
  handleCreate: () => void
  handleSave: () => Promise<void>
  handleDelete: () => Promise<void>
  createDomainId: string
  selectedDomainId: string
  text: string
  candidates: { domain_id: string }[]
  appId: string
  staticToken: string
  tokenUrl: string
  searchUrl: string
}

describe('一张网域覆盖 tab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listOnenetDomainOverrides.mockResolvedValue(OVERRIDES)
    api.getDomains.mockResolvedValue(DOMAINS)
    api.getSystemConfig.mockResolvedValue(GLOBAL_ONENET)
    api.getOnenetDomainOverrideRaw.mockResolvedValue('app_id: odn-app\n')
    api.saveOnenetDomainOverrideRaw.mockResolvedValue({ ok: true })
    api.deleteOnenetDomainOverride.mockResolvedValue({ ok: true, deleted: true })
  })

  it('挂载即拉覆盖/域/全局清单，条目显示 app_id 摘要（未覆盖为 —）', async () => {
    const w = mountTab()
    await flushPromises()
    expect(api.listOnenetDomainOverrides).toHaveBeenCalled()
    expect(api.getDomains).toHaveBeenCalled()
    expect(api.getSystemConfig).toHaveBeenCalledWith('onenet')
    expect(w.text()).toContain('ODN')
    expect(w.text()).toContain('app_id: odn-app')
    expect(w.text()).toContain('app_id: —')
  })

  it('新增候选排除已覆盖域，创建后预填全注释平铺模板并选中', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    expect(vm.candidates.map(c => c.domain_id)).toEqual(['cloud_core_network'])

    vm.createDomainId = 'cloud_core_network'
    vm.handleCreate()
    await flushPromises()
    expect(w.text()).toContain('cloud_core_network.yaml')
    // 模板含 10 键白名单说明与调优参数示例（全注释形态）
    expect(vm.text).toContain('只写与全局 onenet.yaml 的差异')
    expect(vm.text).toContain('throttle_seconds')
    expect(vm.text).toContain('chunk_width')
  })

  it('全局基底驱动继承 placeholder；static_token 永不回显', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    expect(w.html()).toContain('继承全局（global-app）')
    expect(w.html()).toContain('继承全局（http://global.example/token）')
    expect(w.html()).toContain('继承全局（http://global.example/search）')
    expect(w.html()).toContain('继承全局（不回显）')
    expect(w.html()).not.toContain('global-tok')
  })

  it('全局拉取失败降级为通用 placeholder，不阻塞页面', async () => {
    api.getSystemConfig.mockRejectedValue(new Error('boom'))
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    expect(w.html()).toContain('placeholder="继承全局"')
    expect(ElMessage.error).not.toHaveBeenCalled()
  })

  it('表单字段写回平铺 YAML 单一真相源（写入/清空删键/兄弟保留）', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    vm.staticToken = 'tok-odn'
    expect(vm.text).toContain('static_token: tok-odn')
    expect(vm.appId).toBe('odn-app')          // 兄弟键保留
    vm.tokenUrl = 'http://odn.example/token'
    expect(vm.text).toContain('token_url: http://odn.example/token')
    vm.staticToken = ''                        // 清空 = 删键
    expect(vm.text).not.toContain('static_token')
    expect(vm.appId).toBe('odn-app')
  })

  it('YAML 先写的调优键，表单改凭据时保留', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    vm.text = 'app_id: odn-app\nchunk_width: 500\nverify_tls: true\n'
    vm.appId = 'odn-app-2'
    expect(vm.text).toContain('app_id: odn-app-2')
    expect(vm.text).toContain('chunk_width: 500')
    expect(vm.text).toContain('verify_tls: true')
  })

  it('保存直接走 PUT raw（无确认框），成功提示约 10 秒内生效', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    vm.staticToken = 'tok-odn'
    await vm.handleSave()
    expect(ElMessageBox.confirm).not.toHaveBeenCalled()
    expect(api.saveOnenetDomainOverrideRaw).toHaveBeenCalledWith(
      'odn', expect.stringContaining('static_token: tok-odn'))
    expect(ElMessage.success).toHaveBeenCalledWith('已保存（约 10 秒内生效）')
  })

  it('后端 400 的结构化报文透传（如 merge 后仍缺必填凭据）', async () => {
    api.saveOnenetDomainOverrideRaw.mockRejectedValue({
      isAxiosError: true,
      response: { data: { detail: 'onenet merge 后仍缺必填凭据 [static_token]（部分覆盖需保证 merge 完整）' } },
    })
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as Pick<TabVm, 'select' | 'handleSave'>
    await vm.select('odn')
    await vm.handleSave()
    expect(ElMessage.error).toHaveBeenCalledWith(
      expect.stringContaining('merge 后仍缺必填凭据'))
  })

  it('高级 YAML 模式即所存原文：直接编辑 text 保存的就是该字符串', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    const edited = 'app_id: odn-app\nthrottle_seconds: 0.5\n'
    vm.text = edited
    await vm.handleSave()
    expect(api.saveOnenetDomainOverrideRaw).toHaveBeenCalledWith('odn', edited)
  })

  it('删除走确认框 + DELETE，幂等空删不误报', async () => {
    ;(ElMessageBox.confirm as ReturnType<typeof vi.fn>).mockResolvedValue(undefined)
    api.deleteOnenetDomainOverride.mockResolvedValue({ ok: true, deleted: false })
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    await vm.handleDelete()
    expect(api.deleteOnenetDomainOverride).toHaveBeenCalledWith('odn')
    expect(ElMessage.success).toHaveBeenCalledWith('已删除覆盖（约 10 秒内生效）')
    expect(vm.selectedDomainId).toBe('')
  })

  it('取消确认不删除', async () => {
    ;(ElMessageBox.confirm as ReturnType<typeof vi.fn>).mockRejectedValue('cancel')
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as Pick<TabVm, 'select' | 'handleDelete'>
    await vm.select('odn')
    await vm.handleDelete()
    expect(api.deleteOnenetDomainOverride).not.toHaveBeenCalled()
  })
})
