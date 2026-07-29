import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  domain: { currentDomain: 'plant-a' },
  router: { push: vi.fn() },
  store: { createRun: vi.fn() },
  miningApi: {
    getUploadConfig: vi.fn(), uploadFiles: vi.fn(), preflightRun: vi.fn(), getActiveOntology: vi.fn(),
  },
  workflowApi: {
    options: vi.fn(), listVersions: vi.fn(), getVersion: vi.fn(),
  },
  ui: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/stores/domain', () => ({ useDomainStore: () => state.domain }))
vi.mock('@/stores/mining', () => ({ useMiningStore: () => state.store }))
vi.mock('@/api/mining', () => ({ useMiningApi: () => state.miningApi }))
vi.mock('@/api/miningWorkflow', () => ({ useMiningWorkflowApi: () => state.workflowApi }))
vi.mock('vue-router', () => ({ useRouter: () => state.router }))
vi.mock('element-plus', () => ({ ElMessage: state.ui }))

import CreateRunView from '../CreateRunView.vue'

const workflowConfig = {
  max_file_size: 1000, max_archive_size: 2000, max_files_per_request: 10,
  max_file_size_mb: 1, max_archive_size_mb: 2,
  accepted_extensions: ['.txt'], archive_extensions: ['.zip'],
  mining_run_submission_engine: 'workflow',
}

const options = [
  { id: 'minimal', name: '基础文档入库', description: null, current_version: 1, is_system_default: false },
  { id: 'fast', name: '快速向量检索', description: null, current_version: 1, is_system_default: false },
  { id: 'discourse', name: '篇章增强检索', description: null, current_version: 1, is_system_default: false },
  { id: 'entity', name: '固定本体图谱构建', description: null, current_version: 1, is_system_default: false },
  { id: 'hybrid', name: '检索与图谱联合构建', description: null, current_version: 1, is_system_default: false },
  { id: 'ontology', name: '本体演化专项', description: null, current_version: 1, is_system_default: false },
  { id: 'system-full-baseline', name: 'FULL', description: null, current_version: 4, is_system_default: true },
]

describe('Create Run upload and exact Workflow binding', () => {
  beforeEach(() => {
    state.domain.currentDomain = 'plant-a'
    state.router.push.mockReset()
    state.store.createRun.mockReset().mockResolvedValue(undefined)
    state.miningApi.getUploadConfig.mockReset().mockResolvedValue(workflowConfig)
    state.miningApi.uploadFiles.mockReset().mockResolvedValue({
      upload_batch_id: 'batch-1', domain: 'plant-a', file_count: 1, files: ['a.txt'],
      storage_path: 'C:/private/upload/path', extracted_archives: [],
    })
    state.miningApi.preflightRun.mockReset().mockResolvedValue({
      preflight_id: 'pf-1', domain: 'plant-a',
      workflow: { id: 'system-full-baseline', version: 4, version_id: 'version-4', graph_hash: 'graph-4' },
      summary: { NEW: 1 },
      items: [{
        relative_path: 'a.txt', file_name: 'a.txt', file_size: 1, raw_content_hash: 'raw-a',
        classification: 'NEW', default_action: 'NEW', allowed_actions: ['NEW'],
        selected_action: 'NEW', state_token: 'state-a', current_snapshot: null, matched_snapshot: null,
      }],
    })
    state.miningApi.getActiveOntology.mockReset().mockRejectedValue(new Error('not found'))
    state.workflowApi.options.mockReset().mockResolvedValue(options)
    state.workflowApi.listVersions.mockReset().mockResolvedValue([
      { workflow_id: 'system-full-baseline', version: 4 },
      { workflow_id: 'system-full-baseline', version: 2 },
    ])
    state.workflowApi.getVersion.mockReset().mockResolvedValue({
      workflow_id: 'system-full-baseline', version: 4,
      graph_json: { nodes: [{ operatorType: 'ontology_induction' }], edges: [], output: {} },
    })
    state.ui.success.mockReset()
    state.ui.error.mockReset()
    state.ui.warning.mockReset()
  })

  it('defaults to FULL and submits upload_batch_id plus the exact current version', async () => {
    const wrapper = shallowMount(CreateRunView)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      files: File[]
      selectedWorkflowId: string
      selectedWorkflowVersion: number
      workflowOptions: typeof options
      handleCreate: () => Promise<void>
      handleConfirmCreate: () => Promise<void>
    }
    vm.files = [new File(['a'], 'a.txt')]

    expect(vm.selectedWorkflowId).toBe('system-full-baseline')
    expect(vm.selectedWorkflowVersion).toBe(4)
    expect(vm.workflowOptions.map(option => option.name)).toEqual([
      '基础文档入库', '快速向量检索', '篇章增强检索', '固定本体图谱构建',
      '检索与图谱联合构建', '本体演化专项', 'FULL',
    ])
    await vm.handleCreate()

    expect(state.miningApi.preflightRun).toHaveBeenCalledWith({
      domain: 'plant-a', upload_batch_id: 'batch-1',
      workflow_id: 'system-full-baseline', workflow_version: 4,
    })
    expect(state.store.createRun).not.toHaveBeenCalled()
    await vm.handleConfirmCreate()

    expect(state.store.createRun).toHaveBeenCalledWith(expect.objectContaining({
      domain: 'plant-a', upload_batch_id: 'batch-1',
      workflow_id: 'system-full-baseline', workflow_version: 4,
      preflight_id: 'pf-1',
      document_decisions: [{
        relative_path: 'a.txt', raw_content_hash: 'raw-a',
        selected_action: 'NEW', state_token: 'state-a',
      }],
    }))
    expect(state.store.createRun.mock.calls[0][0]).not.toHaveProperty('input_path')
    expect(wrapper.text()).toContain('当前 Domain 未发布本体')
  })

  it('can bind a historical immutable published version', async () => {
    state.miningApi.preflightRun.mockResolvedValueOnce({
      preflight_id: 'pf-v2', domain: 'plant-a',
      workflow: { id: 'system-full-baseline', version: 2, version_id: 'version-2', graph_hash: 'graph-2' },
      summary: { NEW: 1 },
      items: [{
        relative_path: 'a.txt', file_name: 'a.txt', file_size: 1, raw_content_hash: 'raw-a',
        classification: 'NEW', default_action: 'NEW', allowed_actions: ['NEW'],
        selected_action: 'NEW', state_token: 'state-a', current_snapshot: null, matched_snapshot: null,
      }],
    })
    const wrapper = shallowMount(CreateRunView)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      files: File[]
      selectedWorkflowVersion: number
      handleCreate: () => Promise<void>
      handleConfirmCreate: () => Promise<void>
    }
    vm.files = [new File(['a'], 'a.txt')]
    vm.selectedWorkflowVersion = 2

    await vm.handleCreate()
    await vm.handleConfirmCreate()

    expect(state.store.createRun).toHaveBeenCalledWith(expect.objectContaining({ workflow_version: 2 }))
  })

  it('keeps legacy mode compatible and hides Workflow selection', async () => {
    state.miningApi.getUploadConfig.mockResolvedValueOnce({ ...workflowConfig, mining_run_submission_engine: 'legacy' })
    const wrapper = shallowMount(CreateRunView)
    await flushPromises()
    const vm = wrapper.vm as unknown as { inputPath: string; handleCreate: () => Promise<void> }
    vm.inputPath = 'C:/incoming'

    await vm.handleCreate()

    expect(state.workflowApi.options).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="workflow-selector"]').exists()).toBe(false)
    expect(state.store.createRun).toHaveBeenCalledWith({
      domain: 'plant-a', input_path: 'C:/incoming', max_workers: 2, phase1_only: false,
    })
  })

  it('keeps Workflow selection enabled when upload config is unavailable', async () => {
    state.miningApi.getUploadConfig.mockRejectedValueOnce(new Error('temporarily unavailable'))

    const wrapper = shallowMount(CreateRunView)
    await flushPromises()

    expect(wrapper.find('[data-test="workflow-selector"]').exists()).toBe(true)
    expect(state.workflowApi.options).toHaveBeenCalled()
  })

  it('captures the Domain before upload so a mid-request switch cannot retarget the Run', async () => {
    state.miningApi.uploadFiles.mockImplementationOnce(async () => {
      state.domain.currentDomain = 'plant-b'
      return {
        upload_batch_id: 'batch-old-domain', domain: 'plant-a', file_count: 1, files: ['a.txt'],
        storage_path: 'C:/private', extracted_archives: [],
      }
    })
    const wrapper = shallowMount(CreateRunView)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      files: File[]; handleCreate: () => Promise<void>; handleConfirmCreate: () => Promise<void>
    }
    vm.files = [new File(['a'], 'a.txt')]

    await vm.handleCreate()
    await vm.handleConfirmCreate()

    expect(state.miningApi.uploadFiles).toHaveBeenCalledWith('plant-a', expect.any(Array), expect.any(Function))
    expect(state.miningApi.preflightRun).toHaveBeenCalledWith(expect.objectContaining({ domain: 'plant-a' }))
    expect(state.store.createRun).toHaveBeenCalledWith(expect.objectContaining({ domain: 'plant-a' }))
  })

  it('shows conflicts with keep-current selected by default', async () => {
    state.miningApi.preflightRun.mockResolvedValueOnce({
      preflight_id: 'pf-conflict', domain: 'plant-a',
      workflow: { id: 'system-full-baseline', version: 4, version_id: 'version-4', graph_hash: 'graph-4' },
      summary: { WORKFLOW_CONFLICT: 1 },
      items: [{
        relative_path: 'a.txt', file_name: 'a.txt', file_size: 1, raw_content_hash: 'raw-a',
        classification: 'WORKFLOW_CONFLICT', default_action: 'KEPT_CURRENT',
        allowed_actions: ['KEPT_CURRENT', 'REMINED'], selected_action: 'KEPT_CURRENT',
        state_token: 'state-conflict',
        current_snapshot: { workflow_id: 'wf-old', workflow_version: 1, snapshot_id: 'snap-old' },
        matched_snapshot: { workflow_id: 'wf-old', workflow_version: 1, snapshot_id: 'snap-old' },
      }],
    })
    const wrapper = shallowMount(CreateRunView)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      files: File[]; handleCreate: () => Promise<void>
      preflightResult: { items: Array<{ selected_action: string }> }
    }
    vm.files = [new File(['a'], 'a.txt')]

    await vm.handleCreate()

    expect(vm.preflightResult.items[0].selected_action).toBe('KEPT_CURRENT')
    expect(wrapper.find('[data-test="preflight-panel"]').exists()).toBe(true)
  })
})

