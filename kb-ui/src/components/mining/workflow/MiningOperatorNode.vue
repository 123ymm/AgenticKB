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
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import WorkflowOperatorNodeBase from '@/components/workflow/WorkflowOperatorNodeBase.vue'
import type { MiningOperatorDef } from '@/types/miningWorkflow'

const props = defineProps<{
  id: string
  operatorType: string
  definition?: MiningOperatorDef
  params?: Record<string, unknown>
  selected?: boolean
  disabled?: boolean
  isOutput?: boolean
}>()

const badge = computed(() => {
  if (!props.definition) return undefined
  return ({ fixed: '固定', protected: '受保护', editable: '可编辑' })[props.definition.editPolicy]
})
</script>

