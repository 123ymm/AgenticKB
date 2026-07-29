<template>
  <WorkflowOperatorNodeBase
    :id="id"
    :operator-type="operatorType"
    :definition="definition"
    :params="params"
    :selected="selected"
    :disabled="disabled"
    :is-output="isOutput"
    :badge="badge"
    :badge-title="editReason"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import WorkflowOperatorNodeBase from '@/components/workflow/WorkflowOperatorNodeBase.vue'
import type { MiningEffectiveEditState } from '@/utils/miningWorkflowGraph'
import type { MiningOperatorDef } from '@/types/miningWorkflow'

const props = defineProps<{
  id: string
  operatorType: string
  definition?: MiningOperatorDef
  params?: Record<string, unknown>
  selected?: boolean
  disabled?: boolean
  isOutput?: boolean
  editState?: MiningEffectiveEditState
  editReason?: string
}>()

const badge = computed(() => {
  if (!props.definition) return undefined
  const state = props.editState ?? ({ fixed: 'fixed', protected: 'required', editable: 'optional' } as const)[
    props.definition.editPolicy
  ]
  return ({ fixed: '固定骨架', required: '当前必需', optional: '可选' })[state]
})
</script>

