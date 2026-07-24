import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, shallowMount } from '@vue/test-utils'

const state = vi.hoisted(() => ({
  router: { push: vi.fn() },
  leaveGuard: undefined as ((to: unknown, from: unknown, next: (value?: unknown) => void) => void) | undefined,
  api: {
    getCatalog: vi.fn(), get: vi.fn(), saveDraft: vi.fn(), validate: vi.fn(), publish: vi.fn(),
    listVersions: vi.fn(), getVersion: vi.fn(), restoreDraft: vi.fn(),
  },
  ui: { confirm: vi.fn(), success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}))

vi.mock('@/api/miningWorkflow', () => ({ useMiningWorkflowApi: () => state.api }))
vi.mock('vue-router', () => ({
  useRouter: () => state.router,
  onBeforeRouteLeave: (guard: typeof state.leaveGuard) => { state.leaveGuard = guard },
}))
vi.mock('element-plus', () => ({
  ElMessageBox: { confirm: state.ui.confirm },
  ElMessage: { success: state.ui.success, error: state.ui.error, warning: state.ui.warning },
}))
vi.mock('@vue-flow/core', () => ({
  VueFlow: { template: '<div><slot name="node-miningOperator" /></div>' },
  useVueFlow: () => ({ screenToFlowCoordinate: ({ x, y }: { x: number; y: number }) => ({ x, y }) }),
  addEdge: vi.fn(),
}))
vi.mock('@vue-flow/background', () => ({ Background: { template: '<div />' } }))
vi.mock('@vue-flow/controls', () => ({ Controls: { template: '<div />' } }))

import WorkflowEditorView from '../WorkflowEditorView.vue'

const catalog = [
  {
    type: 'input_ingest', version: '1', displayName: 'Input', description: '', category: 'input', zone: 'input',
    editPolicy: 'fixed', inputSlots: [], outputSlots: [{ name: 'out', type: 'DOCUMENT_BATCH', required: true, variadic: false, description: '' }],
    requires: [], provides: [], paramSchemaJson: { type: 'object', properties: {} }, errorPolicy: 'FAIL_FAST', unique: true,
  },
  {
    type: 'editable', version: '1', displayName: 'Editable', description: '', category: 'document', zone: 'document',
    editPolicy: 'editable', inputSlots: [{ name: 'in', type: 'DOCUMENT_BATCH', required: true, variadic: false, description: '' }],
    outputSlots: [{ name: 'out', type: 'DOCUMENT_BATCH', required: true, variadic: false, description: '' }],
    requires: [], provides: [], paramSchemaJson: { type: 'object', properties: { limit: { type: 'integer' } } }, errorPolicy: 'FAIL_FAST', unique: true,
  },
  {
    type: 'extra', version: '1', displayName: 'Extra', description: '', category: 'document', zone: 'document',
    editPolicy: 'editable', inputSlots: [{ name: 'in', type: 'DOCUMENT_BATCH', required: true, variadic: false, description: '' }],
    outputSlots: [{ name: 'out', type: 'DOCUMENT_BATCH', required: true, variadic: false, description: '' }],
    requires: [], provides: [], paramSchemaJson: { type: 'object', properties: {} }, errorPolicy: 'FAIL_FAST', unique: true,
  },
]

const graph = {
  schemaVersion: '1.0',
  nodes: [
    { nodeId: 'input', operatorType: 'input_ingest', operatorVersion: '1', params: {}, ui: { x: 0, y: 0 } },
    { nodeId: 'edit', operatorType: 'editable', operatorVersion: '1', params: { limit: 1 }, ui: { x: 200, y: 0 } },
  ],
  edges: [{ fromNode: 'input', fromSlot: 'out', toNode: 'edit', toSlot: 'in' }],
  output: { nodeId: 'edit', slot: 'out' },
}

const workflow = {
  id: 'wf', name: 'Workflow', description: null, status: 'active', draft_graph_json: graph,
  draft_revision: 3, current_version: 1, is_system: false, is_system_default: false,
  created_by: null, updated_by: null, metadata_json: {},
}

describe('mining Workflow editor', () => {
  beforeEach(() => {
    state.leaveGuard = undefined
    state.router.push.mockReset()
    state.api.getCatalog.mockReset().mockResolvedValue({ catalog_version: '1', items: catalog })
    state.api.get.mockReset().mockResolvedValue(workflow)
    state.api.listVersions.mockReset().mockResolvedValue([])
    state.api.saveDraft.mockReset().mockResolvedValue({ ...workflow, draft_revision: 4 })
    state.api.validate.mockReset().mockResolvedValue({ valid: true, errors: [], executionPlan: {} })
    state.api.publish.mockReset().mockResolvedValue({ workflow_id: 'wf', version: 2 })
    state.api.getVersion.mockReset()
    state.api.restoreDraft.mockReset()
    state.ui.confirm.mockReset().mockResolvedValue('confirm')
    state.ui.success.mockReset()
    state.ui.error.mockReset()
    state.ui.warning.mockReset()
  })

  it('keeps deterministic local JSON available after a draft revision conflict', async () => {
    state.api.saveDraft.mockRejectedValueOnce({
      response: { status: 409, data: { detail: { code: 'draft_revision_conflict' } } },
    })
    const wrapper = shallowMount(WorkflowEditorView, { props: { id: 'wf' } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      updateNodeParams: (nodeId: string, params: Record<string, unknown>) => void
      saveDraft: () => Promise<void>
    }

    vm.updateNodeParams('edit', { limit: 9 })
    await vm.saveDraft()
    await flushPromises()

    expect(state.api.saveDraft).toHaveBeenCalledWith('wf', expect.objectContaining({ expected_revision: 3 }))
    expect(wrapper.find('[data-test="copy-local-json"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="reload-remote"]').exists()).toBe(true)
    expect(wrapper.get('[data-test="local-json"]').text()).toContain('"limit":9')
  })

  it('requires successful server validation before publish', async () => {
    state.api.validate.mockResolvedValueOnce({ valid: false, errors: [{ kind: 'cycle', message: 'cycle' }] })
    const wrapper = shallowMount(WorkflowEditorView, { props: { id: 'wf' } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { publishWorkflow: () => Promise<void> }

    await vm.publishWorkflow()
    expect(state.api.publish).not.toHaveBeenCalled()

    state.api.validate.mockResolvedValueOnce({ valid: true, errors: [], executionPlan: {} })
    await vm.publishWorkflow()
    expect(state.api.publish).toHaveBeenCalledWith('wf', expect.objectContaining({ expected_revision: 3 }))
  })

  it('saves local changes first and publishes the resulting draft revision', async () => {
    const wrapper = shallowMount(WorkflowEditorView, { props: { id: 'wf' } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      updateNodeParams: (nodeId: string, params: Record<string, unknown>) => void
      publishWorkflow: () => Promise<void>
    }

    vm.updateNodeParams('edit', { limit: 7 })
    await vm.publishWorkflow()

    expect(state.api.saveDraft).toHaveBeenCalledWith('wf', expect.objectContaining({ expected_revision: 3 }))
    expect(state.api.publish).toHaveBeenCalledWith('wf', { expected_revision: 4 })
  })

  it('supports undo/redo and protects unsaved navigation', async () => {
    const wrapper = shallowMount(WorkflowEditorView, { props: { id: 'wf' } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      updateNodeParams: (nodeId: string, params: Record<string, unknown>) => void
      undo: () => void
      redo: () => void
      graph: { nodes: Array<{ nodeId: string; params: Record<string, unknown> }> }
      dirty: boolean
    }

    vm.updateNodeParams('edit', { limit: 8 })
    expect(vm.graph.nodes[1].params.limit).toBe(8)
    vm.undo()
    expect(vm.graph.nodes[1].params.limit).toBe(1)
    vm.redo()
    expect(vm.graph.nodes[1].params.limit).toBe(8)
    expect(vm.dirty).toBe(true)

    const next = vi.fn()
    state.leaveGuard?.({}, {}, next)
    await flushPromises()
    expect(state.ui.confirm).toHaveBeenCalled()
    expect(next).toHaveBeenCalled()
  })

  it('opens immutable history and restores it only as a new draft revision', async () => {
    state.api.getVersion.mockResolvedValueOnce({ workflow_id: 'wf', version: 1, graph_json: graph })
    state.api.restoreDraft.mockResolvedValueOnce({ ...workflow, draft_revision: 4 })
    const wrapper = shallowMount(WorkflowEditorView, { props: { id: 'wf' } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      previewVersion: (version: number) => Promise<void>
      restoreVersion: (version: number) => Promise<void>
      readOnly: boolean
    }

    await vm.previewVersion(1)
    expect(vm.readOnly).toBe(true)
    await vm.restoreVersion(1)

    expect(state.api.restoreDraft).toHaveBeenCalledWith('wf', 1, { expected_revision: 3 })
    expect(state.api.publish).not.toHaveBeenCalled()
  })

  it('preserves unsaved draft edits when leaving a read-only version preview', async () => {
    state.api.getVersion.mockResolvedValueOnce({ workflow_id: 'wf', version: 1, graph_json: graph })
    const wrapper = shallowMount(WorkflowEditorView, { props: { id: 'wf' } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      updateNodeParams: (nodeId: string, params: Record<string, unknown>) => void
      previewVersion: (version: number) => Promise<void>
      exitPreview: () => void
      graph: typeof graph
    }

    vm.updateNodeParams('edit', { limit: 8 })
    await vm.previewVersion(1)
    vm.exitPreview()

    expect(vm.graph.nodes[1].params.limit).toBe(8)
  })

  it('adds editable operators and only reconnects endpoints allowed by policy and slot type', async () => {
    const wrapper = shallowMount(WorkflowEditorView, { props: { id: 'wf' } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      addOperator: (type: string, position?: { x: number; y: number }) => void
      onConnect: (connection: Record<string, string>) => void
      graph: typeof graph
    }

    vm.addOperator('extra', { x: 400, y: 0 })
    expect(vm.graph.nodes.some(node => node.operatorType === 'extra')).toBe(true)
    vm.onConnect({ source: 'edit', sourceHandle: 'out', target: 'extra_1', targetHandle: 'in' })
    expect(vm.graph.edges).toContainEqual({ fromNode: 'edit', fromSlot: 'out', toNode: 'extra_1', toSlot: 'in' })

    vm.onConnect({ source: 'input', sourceHandle: 'out', target: 'extra_1', targetHandle: 'in' })
    expect(vm.graph.edges.filter(edge => edge.toNode === 'extra_1')).toHaveLength(1)

    vm.onConnect({ source: 'extra_1', sourceHandle: 'out', target: 'edit', targetHandle: 'in' })
    expect(vm.graph.edges).toContainEqual({ fromNode: 'input', fromSlot: 'out', toNode: 'edit', toSlot: 'in' })
    expect(vm.graph.edges).not.toContainEqual({ fromNode: 'extra_1', fromSlot: 'out', toNode: 'edit', toSlot: 'in' })
  })
})
