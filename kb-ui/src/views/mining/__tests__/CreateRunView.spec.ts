import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  domain: { currentDomain: 'plant-a' },
  router: { push: vi.fn() },
  store: { createRun: vi.fn() },
  miningApi: {
    getUploadConfig: vi.fn(), uploadFiles: vi.fn(), getActiveOntology: vi.fn(),
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
  { id: 'custom', name: 'Custom', description: null, current_version: 2, is_system_default: false },
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
      handleCreate: () => Promise<void>
    }
    vm.files = [new File(['a'], 'a.txt')]

    expect(vm.selectedWorkflowId).toBe('system-full-baseline')
    expect(vm.selectedWorkflowVersion).toBe(4)
    await vm.handleCreate()

    expect(state.store.createRun).toHaveBeenCalledWith(expect.objectContaining({
      domain: 'plant-a', upload_batch_id: 'batch-1',
      workflow_id: 'system-full-baseline', workflow_version: 4,
    }))
    expect(state.store.createRun.mock.calls[0][0]).not.toHaveProperty('input_path')
    expect(wrapper.text()).toContain('当前 Domain 未发布本体')
  })

  it('can bind a historical immutable published version', async () => {
    const wrapper = shallowMount(CreateRunView)
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      files: File[]
      selectedWorkflowVersion: number
      handleCreate: () => Promise<void>
    }
    vm.files = [new File(['a'], 'a.txt')]
    vm.selectedWorkflowVersion = 2

    await vm.handleCreate()

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
    const vm = wrapper.vm as unknown as { files: File[]; handleCreate: () => Promise<void> }
    vm.files = [new File(['a'], 'a.txt')]

    await vm.handleCreate()

    expect(state.miningApi.uploadFiles).toHaveBeenCalledWith('plant-a', expect.any(Array), expect.any(Function))
    expect(state.store.createRun).toHaveBeenCalledWith(expect.objectContaining({ domain: 'plant-a' }))
  })
})

