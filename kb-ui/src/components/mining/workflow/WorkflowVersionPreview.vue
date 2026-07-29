<template>
  <section class="version-preview">
    <div>
      <strong>v{{ version.version }}</strong>
      <el-tag size="small" type="info">只读</el-tag>
    </div>
    <p v-if="version.release_notes">{{ version.release_notes }}</p>
    <code>{{ version.graph_hash }}</code>
    <el-button data-test="restore-version" size="small" type="warning" @click="emit('restore', version.version)">
      恢复为新草稿
    </el-button>
  </section>
</template>

<script setup lang="ts">
import type { MiningWorkflowVersion } from '@/types/miningWorkflow'

defineProps<{ version: MiningWorkflowVersion }>()
const emit = defineEmits<{ restore: [version: number] }>()
</script>

<style scoped>
.version-preview { display: flex; flex-direction: column; gap: 8px; padding: 10px; border: 1px solid var(--kb-border, #e5e7eb); border-radius: 8px; }
.version-preview > div { display: flex; justify-content: space-between; align-items: center; }
.version-preview p { margin: 0; color: var(--kb-text-secondary); }
.version-preview code { font-size: 10px; color: var(--kb-text-tertiary); overflow-wrap: anywhere; }
</style>

