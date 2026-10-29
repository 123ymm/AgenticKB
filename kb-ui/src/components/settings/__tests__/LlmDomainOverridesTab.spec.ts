/**
 * 设置 → LLM 域覆盖（62号表单化管理面）。
 *
 * 钉住的既有行为：候选域排除已覆盖域、保存/删除走对应 API 并按热加载结果回显、
 * 后端 400 的结构化报文透传、删除确认/幂等空删。
 * 62号二期新增：全局基底继承 placeholder、表单字段写回 YAML 单一真相源、
 * dimensions 只读锁全局、embedding 变更确认框两条路径、高级 YAML 即所存原文。
 * 补齐：rerank 段表单同样暴露 模型/端点/密钥 三输入，且表单改一键保留同段其余键。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'

const api = vi.hoisted(() => ({
  listLlmDomainOverrides: vi.fn(),
  getLlmDomainOverrideRaw: vi.fn(),
  saveLlmDomainOverrideRaw: vi.fn(),
  deleteLlmDomainOverride: vi.fn(),
  getDomains: vi.fn(),
  getSystemConfig: vi.fn(),
}))

vi.mock('@/api/controlPlane', () => ({ useControlPlaneApi: () => api }))
vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  ElMessageBox: { confirm: vi.fn() },
}))

import { ElMessage, ElMessageBox } from 'element-plus'
import LlmDomainOverridesTab from '../LlmDomainOverridesTab.vue'

const OVERRIDES = [
  { domain_id: 'odn', active_model: 'odn-chat', rerank_model: null, embedding_model: 'emb-odn' },
  { domain_id: 'generic', active_model: 'glm-4-flash', rerank_model: 'rerank-pro', embedding_model: null },
]
const DOMAINS = [
  { domain_id: 'odn', display_name: 'ODN' },
  { domain_id: 'generic', display_name: 'Generic' },
  { domain_id: 'cloud_core_network', display_name: 'Cloud Core Network' },
].map(d => ({ ...d, enabled: true, default_channel: 'default', scenario_pack_ref: '' }))

/** 全局基底（llm_service.yaml 解析值）：继承 placeholder 与锁定维度的来源。 */
const GLOBAL_LLM = {
  provider: {
    active_model: 'glm-4-flash',
    models: { 'glm-4-flash': { model: 'glm-4-flash' }, 'glm-4.5v': { model: 'glm-4.5v' } },
  },
  rerank: { model: 'rerank-pro' },
  embedding: { model: 'embedding-3', dimensions: 1024 },
}

function mountTab() {
  return shallowMount(LlmDomainOverridesTab)
}

/** 选中 odn（原始 YAML = 只有 chat 覆盖）后的 vm 视图。 */
type TabVm = {
  select: (id: string) => Promise<void>
  handleCreate: () => void
  handleSave: () => Promise<void>
  handleDelete: () => Promise<void>
  createDomainId: string
  selectedDomainId: string
  text: string
  candidates: { domain_id: string }[]
  chatModel: string
  rerankModel: string
  rerankBaseUrl: string
  rerankApiKey: string
  embeddingModel: string
  embeddingApiKey: string
  chatModelOptions: string[]
  lockedDimensions: string
  embeddingTouched: boolean
}

describe('LLM 域覆盖 tab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listLlmDomainOverrides.mockResolvedValue(OVERRIDES)
    api.getDomains.mockResolvedValue(DOMAINS)
    api.getSystemConfig.mockResolvedValue(GLOBAL_LLM)
    api.getLlmDomainOverrideRaw.mockResolvedValue('provider:\n  active_model: odn-chat\n')
    api.saveLlmDomainOverrideRaw.mockResolvedValue({
      ok: true, llm_reload: { ok: true, detail: null },
    })
    api.deleteLlmDomainOverride.mockResolvedValue({
      ok: true, deleted: true, llm_reload: { ok: true, detail: null },
    })
  })

  it('挂载即拉覆盖/域/全局清单，条目显示 chat/rerank/emb 摘要', async () => {
    const w = mountTab()
    await flushPromises()
    expect(api.listLlmDomainOverrides).toHaveBeenCalled()
    expect(api.getDomains).toHaveBeenCalled()
    expect(api.getSystemConfig).toHaveBeenCalledWith('llm_service')
    expect(w.text()).toContain('ODN')
    expect(w.text()).toContain('chat: odn-chat · rerank: — · emb: emb-odn')
    expect(w.text()).toContain('chat: glm-4-flash · rerank: rerank-pro · emb: —')
  })

  it('新增候选排除已覆盖域，创建后预填模板并选中', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    expect(vm.candidates.map(c => c.domain_id)).toEqual(['cloud_core_network'])

    vm.createDomainId = 'cloud_core_network'
    vm.handleCreate()
    await flushPromises()
    expect(w.text()).toContain('cloud_core_network.yaml')
    // 模板含用法说明与三段示例（全注释形态，rerank 示例含可选端点/密钥）
    expect(vm.text).toContain('只写与全局')
    expect(vm.text).toContain('embedding.dimensions 锁全局')
    expect(vm.text).toContain('独立 rerank 端点/密钥')
    expect(vm.text).toContain('embedding:')
  })

  it('全局基底驱动继承 placeholder、chat 候选并集与锁定维度', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    // placeholder 落在 el-select/el-input 的透传属性上（stub 不渲染为文本）
    expect(w.html()).toContain('继承全局（glm-4-flash）')
    expect(w.html()).toContain('继承全局（rerank-pro）')
    expect(w.html()).toContain('继承全局（embedding-3）')
    // 候选 = 全局 models（glm-4-flash / glm-4.5v）∪ 当前值（odn-chat 来自覆盖文件 models 或 active）
    expect(vm.chatModelOptions).toEqual(expect.arrayContaining(['glm-4-flash', 'glm-4.5v', 'odn-chat']))
    // dimensions 只读展示全局 1024
    expect(vm.lockedDimensions).toBe('1024（锁全局）')
    expect(w.text()).toContain('锁全局')
  })

  it('全局拉取失败降级为通用 placeholder，不阻塞页面', async () => {
    api.getSystemConfig.mockRejectedValue(new Error('boom'))
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    expect(w.html()).toContain('placeholder="继承全局"')
    expect(vm.lockedDimensions).toBe('全局配置值（锁全局）')
    expect(ElMessage.error).not.toHaveBeenCalled()
  })

  it('表单字段写回 YAML 单一真相源（写入/清空删键）', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    // chat：改选写回 provider.active_model
    vm.chatModel = 'glm-4.5v'
    expect(vm.text).toContain('active_model: glm-4.5v')
    // rerank：新键新段（模型/端点/密钥三键，端点密钥同段共存）
    vm.rerankModel = 'rerank-odn'
    expect(vm.text).toContain('model: rerank-odn')
    vm.rerankBaseUrl = 'https://rerank-odn.example/'
    vm.rerankApiKey = 'rk-odn'
    expect(vm.text).toContain('base_url: https://rerank-odn.example/')
    expect(vm.text).toContain('api_key: rk-odn')
    // embedding：模型 + 密钥
    vm.embeddingModel = 'emb-odn'
    vm.embeddingApiKey = 'ek-odn'
    expect(vm.text).toContain('model: emb-odn')
    expect(vm.text).toContain('api_key: ek-odn')
    expect(vm.embeddingTouched).toBe(true)
    // 清空 = 删键：rerank 段三键全清即整段消失（只清模型时端点/密钥保留）
    vm.rerankModel = ''
    expect(vm.text).toContain('base_url: https://rerank-odn.example/')
    vm.rerankBaseUrl = ''
    vm.rerankApiKey = ''
    expect(vm.text).not.toContain('rerank')
  })

  it('rerank 段 YAML 先写的端点/密钥，表单改模型时保留', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    vm.text = 'rerank:\n  model: rerank-pro\n  base_url: https://rk.example/\n  api_key: rk-1\n'
    expect(vm.rerankBaseUrl).toBe('https://rk.example/')
    expect(vm.rerankApiKey).toBe('rk-1')
    vm.rerankModel = 'rerank-odn'
    expect(vm.text).toContain('model: rerank-odn')
    expect(vm.rerankBaseUrl).toBe('https://rk.example/')
    expect(vm.rerankApiKey).toBe('rk-1')
  })

  it('embedding 段无差异的保存不弹确认框，直接走 PUT raw 并按热加载成功回显', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    vm.text = 'provider:\n  active_model: glm-4-flash\n'
    await vm.handleSave()
    expect(ElMessageBox.confirm).not.toHaveBeenCalled()
    expect(api.saveLlmDomainOverrideRaw).toHaveBeenCalledWith(
      'odn', 'provider:\n  active_model: glm-4-flash\n')
    expect(ElMessage.success).toHaveBeenCalledWith('已保存，已热加载')
  })

  it('embedding 段变更的保存先弹确认框；确认后保存', async () => {
    ;(ElMessageBox.confirm as ReturnType<typeof vi.fn>).mockResolvedValue(undefined)
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    vm.embeddingModel = 'emb-odn'
    await vm.handleSave()
    expect(ElMessageBox.confirm).toHaveBeenCalledTimes(1)
    expect(api.saveLlmDomainOverrideRaw).toHaveBeenCalledWith(
      'odn', expect.stringContaining('model: emb-odn'))
    expect(ElMessage.success).toHaveBeenCalledWith('已保存，已热加载')
  })

  it('embedding 确认框取消则不保存', async () => {
    ;(ElMessageBox.confirm as ReturnType<typeof vi.fn>).mockRejectedValue('cancel')
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    vm.embeddingModel = 'emb-odn'
    await vm.handleSave()
    expect(api.saveLlmDomainOverrideRaw).not.toHaveBeenCalled()
  })

  it('热加载失败时保存仍算成功，但回显警告（重启后生效）', async () => {
    api.saveLlmDomainOverrideRaw.mockResolvedValue({
      ok: true, llm_reload: { ok: false, detail: 'llm_service 不可达（ConnectError）' },
    })
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as Pick<TabVm, 'select' | 'handleSave'>
    await vm.select('odn')
    await vm.handleSave()
    expect(ElMessage.warning).toHaveBeenCalledWith(
      expect.stringContaining('重启后台服务后生效'))
  })

  it('后端 400 的结构化报文透传（如 dimensions 锁全局）', async () => {
    api.saveLlmDomainOverrideRaw.mockRejectedValue({
      isAxiosError: true,
      response: { data: { detail: 'embedding.dimensions 锁全局：域覆盖合并后为 2048，全局为 1024…' } },
    })
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as Pick<TabVm, 'select' | 'handleSave'>
    await vm.select('odn')
    await vm.handleSave()
    expect(ElMessage.error).toHaveBeenCalledWith(
      expect.stringContaining('embedding.dimensions 锁全局'))
  })

  it('高级 YAML 模式即所存原文：直接编辑 text 保存的就是该字符串', async () => {
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
    await vm.select('odn')
    const edited = 'provider:\n  active_model: glm-4.5v\nrerank:\n  model: rerank-odn\n'
    vm.text = edited
    await vm.handleSave()
    expect(api.saveLlmDomainOverrideRaw).toHaveBeenCalledWith('odn', edited)
  })

  it('删除走确认框 + DELETE，幂等空删（无 llm_reload）不误报热加载', async () => {
    ;(ElMessageBox.confirm as ReturnType<typeof vi.fn>).mockResolvedValue(undefined)
    api.deleteLlmDomainOverride.mockResolvedValue({ ok: true, deleted: false })
    const w = mountTab()
    await flushPromises()
    const vm = w.vm as unknown as TabVm
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
    const vm = w.vm as unknown as Pick<TabVm, 'select' | 'handleDelete'>
    await vm.select('odn')
    await vm.handleDelete()
    expect(api.deleteLlmDomainOverride).not.toHaveBeenCalled()
  })
})
