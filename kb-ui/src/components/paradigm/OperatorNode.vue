<template>
  <WorkflowOperatorNodeBase
    :id="id"
    :operator-type="data.operatorType"
    :definition="data.def"
    :is-output="data.isOutput"
    :params="data.params"
    :selected="selected"
  />
</template>

<script setup lang="ts">
import WorkflowOperatorNodeBase from '@/components/workflow/WorkflowOperatorNodeBase.vue'
import type { OperatorDef } from '@/types/operator'

defineProps<{
  id: string
  data: { operatorType: string; def?: OperatorDef; isOutput?: boolean; params?: Record<string, unknown> }
  selected?: boolean
}>()
</script>
