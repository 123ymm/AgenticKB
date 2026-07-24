<template>
  <div class="mining-palette">
    <p class="mining-palette__hint">算子目录由服务端 Catalog 提供</p>
    <section v-for="group in groups" :key="group.key" class="mining-palette__group">
      <h4>{{ group.label }}</h4>
      <button
        v-for="operator in group.items"
        :key="operator.type"
        class="mining-palette__item"
        type="button"
        :data-operator="operator.type"
        :draggable="operator.editPolicy !== 'fixed'"
        :disabled="operator.editPolicy === 'fixed'"
        @dragstart="startDrag($event, operator)"
        @dblclick="emit('add', operator.type)"
      >
        <span>{{ operator.displayName }}</span>
        <code>{{ operator.type }}</code>
        <small>{{ policyLabel(operator.editPolicy) }}</small>
      </button>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { MiningOperatorDef, MiningEditPolicy } from '@/types/miningWorkflow'

const props = defineProps<{ operators: MiningOperatorDef[] }>()
const emit = defineEmits<{ add: [operatorType: string] }>()

const ZONE_ORDER = ['input', 'document', 'global']
const ZONE_LABELS: Record<string, string> = { input: '输入', document: '文档级', global: '全局级' }
const groups = computed(() => ZONE_ORDER.map(key => ({
  key,
  label: ZONE_LABELS[key],
  items: props.operators.filter(operator => operator.zone === key),
})).filter(group => group.items.length))

function policyLabel(policy: MiningEditPolicy): string {
  return ({ fixed: '固定', protected: '受保护', editable: '可编辑' })[policy]
}

function startDrag(event: DragEvent, operator: MiningOperatorDef) {
  if (operator.editPolicy === 'fixed') {
    event.preventDefault()
    return
  }
  event.dataTransfer?.setData('application/mining-operator-type', operator.type)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}
</script>

<style scoped>
.mining-palette { display: flex; flex-direction: column; gap: 14px; padding: 4px; }
.mining-palette__hint { margin: 0; font-size: 11px; color: var(--kb-text-tertiary); }
.mining-palette__group { display: flex; flex-direction: column; gap: 6px; }
.mining-palette__group h4 { margin: 0; font-size: 12px; color: var(--kb-text-secondary); }
.mining-palette__item { display: grid; grid-template-columns: 1fr auto; gap: 2px 8px; text-align: left; padding: 8px 10px; border: 1px solid var(--kb-border, #e5e7eb); border-left: 3px solid #3b82f6; border-radius: 7px; background: #fff; cursor: grab; }
.mining-palette__item:disabled { cursor: not-allowed; opacity: .58; }
.mining-palette__item span { font-weight: 600; color: var(--kb-text-primary); }
.mining-palette__item code { grid-column: 1; font-size: 10px; color: var(--kb-text-tertiary); }
.mining-palette__item small { grid-column: 2; grid-row: 1 / span 2; align-self: center; color: var(--kb-text-tertiary); }
</style>

