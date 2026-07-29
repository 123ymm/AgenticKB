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

const businessDefinitions = [
  { type: 'input_ingest', displayName: '输入发现', category: 'input', zone: 'input', editPolicy: 'fixed', inputSlots: [], outputSlots: [], description: '' },
  { type: 'enrich', displayName: '语义增强', category: 'document', zone: 'document', editPolicy: 'editable', inputSlots: [], outputSlots: [], description: '' },
  { type: 'entity_review_gate', displayName: '实体审核', category: 'review', zone: 'global', editPolicy: 'protected', inputSlots: [], outputSlots: [], description: '' },
  { type: 'graph_write', displayName: '图谱写入', category: 'ontology', zone: 'global', editPolicy: 'protected', inputSlots: [], outputSlots: [], description: '' },
  { type: 'ontology_induction', displayName: '本体归纳', category: 'ontology', zone: 'global', editPolicy: 'editable', inputSlots: [], outputSlots: [], description: '' },
  { type: 'asset_persist', displayName: '资产持久化', category: 'storage', zone: 'document', editPolicy: 'fixed', inputSlots: [], outputSlots: [], description: '' },
] as any[]

describe('mining Workflow editor components', () => {
  it('uses the canvas category classes for palette item colors', () => {
    const palette = shallowMount(MiningOperatorPalette, { props: { operators: definitions } })

    expect(palette.get('[data-operator="fixed"]').classes()).toContain('mining-palette__item--input')
    expect(palette.get('[data-operator="editable"]').classes()).toContain('mining-palette__item--document')
    expect(palette.get('[data-operator="protected"]').classes()).toContain('mining-palette__item--review')
  })

  it('groups operators by business capability and shows execution scope', () => {
    const extension = {
      type: 'custom_extension', displayName: '扩展算子', category: 'document', zone: 'document', editPolicy: 'editable',
      inputSlots: [], outputSlots: [], description: '',
    }
    const palette = shallowMount(MiningOperatorPalette, {
      props: { operators: [...businessDefinitions, extension] as any[], nodes: [] },
    })
    const headings = palette.findAll('.mining-palette__group h4').map(item => item.text())

    expect(headings).toEqual(['输入与解析', '篇章与检索', '实体与图谱', '本体演化', '资产与发布', '其他'])
    expect(palette.get('[data-operator="custom_extension"]').text()).toContain('扩展算子')
    expect(palette.get('[data-operator="entity_review_gate"]').text()).toContain('整批次')
    expect(palette.get('[data-operator="asset_persist"]').text()).toContain('逐文档')
  })

  it('uses graph-aware edit states in palette and node badges', async () => {
    const palette = shallowMount(MiningOperatorPalette, {
      props: {
        operators: businessDefinitions,
        nodes: [{ nodeId: 'entity', operatorType: 'entity_extract', params: {} }],
      },
    })
    expect(palette.get('[data-operator="graph_write"]').text()).toContain('当前必需')
    await palette.setProps({ nodes: [] })
    expect(palette.get('[data-operator="graph_write"]').text()).toContain('可选')
    const staticPalette = shallowMount(MiningOperatorPalette, { props: { operators: definitions, nodes: [] } })
    expect(staticPalette.find('[data-operator="fixed"]').attributes('draggable')).toBe('false')
    expect(staticPalette.find('[data-operator="editable"]').attributes('draggable')).toBe('true')

    const node = shallowMount(MiningOperatorNode, {
      props: {
        id: 'n', operatorType: 'graph_write', definition: businessDefinitions[3], params: {}, selected: false,
        editState: 'required', editReason: '当前存在实体能力线，发布前必须写入图谱',
      },
    })
    expect(node.getComponent(WorkflowOperatorNodeBase).props('badge')).toBe('当前必需')
    expect(node.getComponent(WorkflowOperatorNodeBase).props('badgeTitle')).toContain('实体能力线')
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
