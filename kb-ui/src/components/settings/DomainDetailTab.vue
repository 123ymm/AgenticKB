<template>
  <div class="domain-detail-tab">
    <div v-if="!store.selectedDomainId" class="domain-detail-tab__empty">
      请先在"域管理"中选择一个域
    </div>
    <template v-else>
      <div class="domain-detail-tab__header">
        <span class="domain-detail-tab__label">
          {{ store.selectedDomainId }} / domain.yaml
        </span>
        <el-button
          type="primary"
          size="small"
          :loading="store.saving"
          :disabled="!store.scenarioDirty"
          @click="handleSave"
        >
          保存
        </el-button>
      </div>
      <div class="domain-detail-tab__editor-wrap">
        <YamlEditor v-model="store.scenarioYamlText" />
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { useControlPlaneStore } from '@/stores/controlPlane'
import YamlEditor from '@/components/common/YamlEditor.vue'

const store = useControlPlaneStore()

async function handleSave() {
  try {
    await store.saveScenarioYaml()
    ElMessage.success('已保存')
  } catch {
    ElMessage.error('保存失败')
  }
}
</script>

<style scoped>
.domain-detail-tab {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 220px);
  min-height: 400px;
}

.domain-detail-tab__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 0;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--kb-border-light);
  flex-shrink: 0;
}

.domain-detail-tab__label {
  font-size: 13px;
  font-weight: 500;
  color: var(--kb-text-primary);
}

.domain-detail-tab__editor-wrap {
  flex: 1;
  min-height: 0;
  position: relative;
  overflow: hidden;
}

.domain-detail-tab__empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
  color: var(--kb-text-tertiary);
  font-size: 14px;
}
</style>
