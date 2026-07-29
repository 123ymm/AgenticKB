<template>
  <section class="validation-panel">
    <div class="validation-panel__title">
      校验结果
      <span :class="allIssues.length ? 'is-error' : 'is-ok'">{{ allIssues.length ? `${allIssues.length} 项问题` : '通过' }}</span>
    </div>
    <p v-if="!allIssues.length" class="validation-panel__empty">本地规则通过；发布前仍需服务端校验。</p>
    <ul v-else>
      <li v-for="(item, index) in allIssues" :key="`${item.code}-${index}`">
        <code>{{ item.code }}</code>
        <span>{{ item.message }}</span>
        <small v-if="item.nodeId">{{ item.nodeId }}</small>
      </li>
    </ul>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { MiningWorkflowValidationResult, WorkflowValidationIssue } from '@/types/miningWorkflow'

const props = defineProps<{
  localIssues: WorkflowValidationIssue[]
  serverResult?: MiningWorkflowValidationResult | null
}>()

const allIssues = computed(() => [
  ...props.localIssues,
  ...(props.serverResult?.errors ?? []).map(error => ({
    code: error.kind,
    message: error.message,
    nodeId: error.nodeId,
    severity: 'error' as const,
  })),
])
</script>

<style scoped>
.validation-panel { font-size: 12px; }
.validation-panel__title { display: flex; justify-content: space-between; font-weight: 700; }
.validation-panel__title .is-error { color: #dc2626; }
.validation-panel__title .is-ok { color: #16a34a; }
.validation-panel__empty { color: var(--kb-text-tertiary); line-height: 1.5; }
.validation-panel ul { list-style: none; padding: 0; display: flex; flex-direction: column; gap: 7px; }
.validation-panel li { display: grid; grid-template-columns: auto 1fr; gap: 3px 8px; padding: 7px; border-radius: 6px; background: #fef2f2; }
.validation-panel li code { color: #b91c1c; }
.validation-panel li small { grid-column: 2; color: var(--kb-text-tertiary); }
</style>

