<template>
  <div
    class="workflow-node"
    :class="[
      `workflow-node--${definition?.category ?? 'default'}`,
      { 'workflow-node--output': isOutput, 'workflow-node--selected': selected, 'workflow-node--disabled': disabled },
    ]"
  >
    <div class="workflow-node__header">
      <span class="workflow-node__title">{{ definition?.displayName ?? operatorType }}</span>
      <span class="workflow-node__type">{{ operatorType }}</span>
      <span v-if="badge" class="workflow-node__badge">{{ badge }}</span>
      <span v-if="paramSummary" class="workflow-node__params">{{ paramSummary }}</span>
    </div>

    <div class="workflow-node__body">
      <div class="workflow-node__col workflow-node__col--in">
        <div v-for="slot in inputs" :key="`in-${slot.name}`" class="workflow-node__slot workflow-node__slot--in">
          <Handle :id="slot.name" type="target" :position="Position.Left" class="workflow-node__handle" />
          <span class="workflow-node__slot-label">{{ slot.name }}</span>
        </div>
      </div>
      <div class="workflow-node__col workflow-node__col--out">
        <div v-for="slot in outputs" :key="`out-${slot.name}`" class="workflow-node__slot workflow-node__slot--out">
          <span class="workflow-node__slot-label">{{ slot.name }}</span>
          <Handle
            :id="slot.name"
            type="source"
            :position="Position.Right"
            class="workflow-node__handle workflow-node__handle--out"
          />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'

interface PresentationSlot { name: string }
interface OperatorPresentation {
  displayName: string
  category: string
  inputSlots: PresentationSlot[]
  outputSlots: PresentationSlot[]
}

const props = defineProps<{
  id: string
  operatorType: string
  definition?: OperatorPresentation
  isOutput?: boolean
  params?: Record<string, unknown>
  selected?: boolean
  disabled?: boolean
  badge?: string
}>()

const inputs = computed(() => props.definition?.inputSlots ?? [])
const outputs = computed(() => props.definition?.outputSlots ?? [])
const paramSummary = computed(() => Object.entries(props.params ?? {})
  .filter(([, value]) => value !== null && value !== undefined && value !== '' && typeof value !== 'object')
  .slice(0, 3)
  .map(([key, value]) => `${key}=${String(value)}`)
  .join(' · '))
</script>

<style scoped>
.workflow-node {
  min-width: 180px; border-radius: 9px; background: #fff;
  border: 1px solid #e5e7eb; border-top: 3px solid var(--cat, #94a3b8);
  box-shadow: 0 1px 4px rgba(0, 0, 0, .08); font-size: 12px;
}
.workflow-node--output, .workflow-node--publish { --cat: #22c55e; }
.workflow-node--input { --cat: #0ea5e9; }
.workflow-node--query { --cat: #6366f1; }
.workflow-node--scope { --cat: #14b8a6; }
.workflow-node--retrieve, .workflow-node--document, .workflow-node--discourse { --cat: #3b82f6; }
.workflow-node--fuse, .workflow-node--storage { --cat: #f59e0b; }
.workflow-node--rerank, .workflow-node--ontology { --cat: #ec4899; }
.workflow-node--review { --cat: #8b5cf6; }
.workflow-node--selected { border-color: #3b82f6; box-shadow: 0 0 0 2px #3b82f6, 0 6px 18px rgba(59, 130, 246, .30); }
.workflow-node--disabled { opacity: .52; filter: grayscale(.4); }
.workflow-node__header { padding: 7px 12px 6px; display: flex; flex-direction: column; gap: 1px; }
.workflow-node__title { font-weight: 700; color: #1e293b; line-height: 1.2; }
.workflow-node__type { font-size: 10px; color: #94a3b8; font-family: monospace; line-height: 1.2; }
.workflow-node__badge { align-self: flex-start; margin-top: 3px; padding: 1px 5px; border-radius: 999px; background: #eef2ff; color: #4f46e5; font-size: 10px; }
.workflow-node__params { font-size: 10px; color: #64748b; line-height: 1.3; margin-top: 2px; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.workflow-node__body { display: flex; justify-content: space-between; gap: 16px; padding: 4px 0 9px; }
.workflow-node__col { display: flex; flex-direction: column; gap: 3px; flex: 1; min-width: 0; }
.workflow-node__slot { position: relative; height: 22px; display: flex; align-items: center; white-space: nowrap; }
.workflow-node__slot--in { justify-content: flex-start; padding-left: 12px; }
.workflow-node__slot--out { justify-content: flex-end; padding-right: 12px; }
.workflow-node__slot-label { font-size: 11px; color: #475569; }
.workflow-node__handle { width: 9px; height: 9px; background: #64748b; border: 2px solid #fff; }
.workflow-node__handle--out { background: #3b82f6; }
</style>

