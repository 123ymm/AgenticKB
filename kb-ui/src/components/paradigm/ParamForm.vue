<template>
  <JsonSchemaParamForm
    :schema-json="schemaJson"
    :model-value="modelValue"
    @update:model-value="emit('update:modelValue', $event)"
  />
</template>

<script setup lang="ts">
import JsonSchemaParamForm from '@/components/workflow/JsonSchemaParamForm.vue'

defineProps<{
  schemaJson: string
  modelValue: Record<string, unknown>
}>()
const emit = defineEmits<{ 'update:modelValue': [Record<string, unknown>] }>()
</script>
