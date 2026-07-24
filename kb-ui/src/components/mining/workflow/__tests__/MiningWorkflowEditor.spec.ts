import { describe, expect, it } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import MiningOperatorPalette from '../MiningOperatorPalette.vue'
import MiningOperatorNode from '../MiningOperatorNode.vue'
import WorkflowValidationPanel from '../WorkflowValidationPanel.vue'
import WorkflowVersionPreview from '../WorkflowVersionPreview.vue'
import WorkflowOperatorNodeBase from '@/components/workflow/WorkflowOperatorNodeBase.vue'

const definitions = [
  { type: 'fixed', displayName: 'Fixed', category: 'input', zone: 'input', editPolicy: 'fixed', inputSlots: [], outputSlots: [], description: '' },
  { type: 'protected', displayName: 'Protected', category: 'review', zone: 'global', editPolicy: 'protected', inputSlots: [], outputSlots: [], description: '' },
  { type: 'editable', displayName: 'Editable', category: 'document', zone: 'document', editPolicy: 'editable', inputSlots: [], outputSlots: [], description: '' },
] as any[]

describe('mining Workflow editor components', () => {
  it('uses Catalog edit policies in palette and node badges', () => {
    const palette = shallowMount(MiningOperatorPalette, { props: { operators: definitions } })
    expect(palette.find('[data-operator="fixed"]').attributes('draggable')).toBe('false')
    expect(palette.find('[data-operator="editable"]').attributes('draggable')).toBe('true')

    const node = shallowMount(MiningOperatorNode, {
      props: { id: 'n', operatorType: 'fixed', definition: definitions[0], params: {}, selected: false },
    })
    expect(node.getComponent(WorkflowOperatorNodeBase).props('badge')).toBe('固定')
  })

  it('renders local and server validation issues together', () => {
    const wrapper = shallowMount(WorkflowValidationPanel, {
      props: {
        localIssues: [{ code: 'workflow_cycle', message: '不能成环', severity: 'error' }],
        serverResult: { valid: false, errors: [{ kind: 'missing_capability', message: '能力缺失' }] },
      },
    })
    expect(wrapper.text()).toContain('不能成环')
    expect(wrapper.text()).toContain('能力缺失')
  })

  it('keeps version preview read-only and emits an explicit restore request', async () => {
    const wrapper = shallowMount(WorkflowVersionPreview, {
      props: { version: { version: 2, graph_hash: 'abc', release_notes: 'stable' } as any },
    })
    expect(wrapper.text()).toContain('只读')
    await wrapper.get('[data-test="restore-version"]').trigger('click')
    expect(wrapper.emitted('restore')?.[0]).toEqual([2])
  })
})
