import { describe, expect, it } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import JsonSchemaParamForm from '@/components/workflow/JsonSchemaParamForm.vue'
import WorkflowOperatorNodeBase from '@/components/workflow/WorkflowOperatorNodeBase.vue'
import ParamForm from '@/components/paradigm/ParamForm.vue'
import OperatorNode from '@/components/paradigm/OperatorNode.vue'

describe('shared workflow presentation primitives', () => {
  it('keeps the retrieval ParamForm prop and update event contract', async () => {
    const wrapper = shallowMount(ParamForm, {
      props: { schemaJson: '{"type":"object"}', modelValue: { topK: 10 } },
    })
    const shared = wrapper.getComponent(JsonSchemaParamForm)
    expect(shared.props()).toMatchObject({ schemaJson: '{"type":"object"}', modelValue: { topK: 10 } })

    await shared.vm.$emit('update:modelValue', { topK: 20 })
    expect(wrapper.emitted('update:modelValue')).toEqual([[{ topK: 20 }]])
  })

  it('keeps retrieval OperatorNode data delegated to the shared node frame', () => {
    const def = {
      type: 'retrieve', category: 'retrieve', displayName: 'Retrieve', description: '',
      inputSlots: [], outputSlots: [], paramSchemaJson: '{}', errorPolicy: 'FAIL_FAST',
    }
    const wrapper = shallowMount(OperatorNode, {
      props: { id: 'n1', selected: true, data: { operatorType: 'retrieve', def, params: { topK: 10 } } },
    })
    const shared = wrapper.getComponent(WorkflowOperatorNodeBase)
    expect(shared.props()).toMatchObject({
      id: 'n1', selected: true, operatorType: 'retrieve', definition: def, params: { topK: 10 },
    })
  })

  it('renders generic schema defaults and emits immutable updates', async () => {
    const modelValue = { enabled: true }
    const wrapper = shallowMount(JsonSchemaParamForm, {
      props: {
        schemaJson: JSON.stringify({ type: 'object', properties: { count: { type: 'integer', default: 2 } } }),
        modelValue,
      },
      global: { stubs: { ElInputNumber: { template: '<button class="number" @click="$emit(\'update:modelValue\', 3)" />' } } },
    })

    await wrapper.get('.number').trigger('click')
    expect(modelValue).toEqual({ enabled: true })
    expect(wrapper.emitted('update:modelValue')).toEqual([[{ enabled: true, count: 3 }]])
  })
})
