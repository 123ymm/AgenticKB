<template>
  <div class="system-config-tab">
    <div class="system-config-tab__layout">
      <div class="system-config-tab__sidebar">
        <div
          v-for="name in store.systemConfigNames"
          :key="name"
          class="system-config-tab__item"
          :class="{ 'system-config-tab__item--active': name === store.selectedSystemConfigName }"
          @click="store.selectSystemConfig(name)"
        >
          {{ name }}
        </div>
      </div>
      <div class="system-config-tab__main">
        <div v-if="!store.selectedSystemConfigName" class="system-config-tab__empty">
          选择左侧配置文件查看
        </div>
        <template v-else>
          <div class="system-config-tab__header">
            <span class="system-config-tab__filename">{{ store.selectedSystemConfigName }}.yaml</span>
            <el-button
              type="primary"
              size="small"
              :loading="store.saving"
              :disabled="!store.systemConfigDirty"
              @click="handleSave"
            >
              保存
            </el-button>
          </div>
          <div
            v-if="store.selectedSystemConfigName === 'llm_service'"
            class="system-config-tab__llm-hint"
          >
            本文件是 LLM 的<strong>全局基底</strong>；某域需要差异化模型/密钥时，请前往「LLM 域覆盖」页配置（留空字段自动继承这里）。
          </div>
          <div class="system-config-tab__editor-wrap">
            <YamlEditor v-model="store.systemConfigText" />
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useControlPlaneStore } from '@/stores/controlPlane'
import YamlEditor from '@/components/common/YamlEditor.vue'

const store = useControlPlaneStore()

async function handleSave() {
  try {
    const result = await store.saveSystemConfig()
    const reload = result?.llm_reload
    if (reload) {
      // llm_service.yaml 保存即生效：控制面已尽力通知热加载
      if (reload.ok) {
        ElMessage.success('已保存，LLM 配置已热加载')
      } else {
        ElMessage.warning(`已保存，但热加载失败（${reload.detail ?? '未知原因'}）——重启后台服务后生效`)
      }
    } else {
      ElMessage.success('已保存')
    }
  } catch {
    ElMessage.error('保存失败')
  }
}

onMounted(() => store.loadSystemConfigs())
</script>

<style scoped>
.system-config-tab__layout {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  grid-template-rows: 1fr;
  gap: 0;
  height: calc(100vh - 220px);
  min-height: 400px;
}

.system-config-tab__sidebar {
  border-right: 1px solid var(--kb-border-light);
  padding: 8px 0;
  overflow-y: auto;
}

.system-config-tab__item {
  padding: 8px 16px;
  cursor: pointer;
  font-size: 13px;
  color: var(--kb-text-secondary);
  transition: background 0.15s;
}

.system-config-tab__item:hover {
  background: var(--kb-bg-hover, rgba(0,0,0,0.03));
}

.system-config-tab__item--active {
  background: rgba(64, 158, 255, 0.08);
  color: var(--el-color-primary);
  font-weight: 500;
}

.system-config-tab__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.system-config-tab__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  border-bottom: 1px solid var(--kb-border-light);
  flex-shrink: 0;
}

.system-config-tab__filename {
  font-size: 13px;
  font-weight: 500;
  color: var(--kb-text-primary);
}

.system-config-tab__llm-hint {
  margin: 8px 16px 0;
  padding: 6px 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--kb-text-secondary);
  background: rgba(64, 158, 255, 0.05);
  border: 1px solid rgba(64, 158, 255, 0.15);
  border-radius: 6px;
}

.system-config-tab__editor-wrap {
  flex: 1;
  min-height: 0;
  position: relative;
  overflow: hidden;
}

.system-config-tab__empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
  color: var(--kb-text-tertiary);
  font-size: 14px;
}
</style>
