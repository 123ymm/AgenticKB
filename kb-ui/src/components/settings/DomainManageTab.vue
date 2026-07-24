<template>
  <div class="domain-manage-tab">
    <div class="domain-manage-tab__layout">
      <div class="domain-manage-tab__sidebar">
        <div class="domain-manage-tab__sidebar-header">
          <span class="domain-manage-tab__sidebar-title">Domains</span>
          <el-button size="small" @click="showCreateDialog = true">新增</el-button>
        </div>
        <div
          v-for="d in store.domains"
          :key="d.domain_id"
          class="domain-manage-tab__item"
          :class="{ 'domain-manage-tab__item--active': d.domain_id === store.selectedDomainId }"
          @click="store.selectDomain(d.domain_id)"
        >
          <span class="domain-manage-tab__item-name">{{ d.display_name }}</span>
          <span class="domain-manage-tab__item-id">{{ d.domain_id }}</span>
        </div>
      </div>
      <div class="domain-manage-tab__main">
        <div v-if="!store.selectedDomainId" class="domain-manage-tab__empty">
          选择左侧域查看配置
        </div>
        <template v-else>
          <div class="domain-manage-tab__header">
            <span class="domain-manage-tab__domain-label">{{ store.selectedDomainId }}</span>
            <div class="domain-manage-tab__actions">
              <el-button
                type="primary"
                size="small"
                :loading="store.saving"
                :disabled="!store.domainYamlDirty"
                @click="handleSave"
              >
                保存
              </el-button>
              <el-button size="small" type="danger" @click="handleDelete">删除</el-button>
            </div>
          </div>
          <div class="domain-manage-tab__editor-wrap">
            <YamlEditor v-model="store.domainYamlText" />
          </div>
        </template>
      </div>
    </div>

    <el-dialog v-model="showCreateDialog" title="新增域" width="400px" @close="newDomainId = ''">
      <el-input v-model="newDomainId" placeholder="domain_id (snake_case)" />
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" :disabled="!newDomainId" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useControlPlaneStore } from '@/stores/controlPlane'
import YamlEditor from '@/components/common/YamlEditor.vue'

const store = useControlPlaneStore()
const showCreateDialog = ref(false)
const newDomainId = ref('')

async function handleSave() {
  try {
    await store.saveDomainYaml()
    ElMessage.success('已保存')
  } catch {
    ElMessage.error('保存失败')
  }
}

async function handleDelete() {
  if (!store.selectedDomainId) return
  try {
    await ElMessageBox.confirm(
      `确认删除域 "${store.selectedDomainId}"？该操作不可恢复。`,
      '删除确认',
      { type: 'warning' },
    )
    await store.deleteDomain(store.selectedDomainId)
    ElMessage.success('已删除')
  } catch {
    // cancelled
  }
}

async function handleCreate() {
  const id = newDomainId.value.trim()
  if (!id) return
  try {
    await store.createDomain(id)
    showCreateDialog.value = false
    newDomainId.value = ''
    ElMessage.success('已创建')
  } catch {
    ElMessage.error('创建失败')
  }
}

onMounted(() => store.loadDomains())
</script>

<style scoped>
.domain-manage-tab__layout {
  display: grid;
  grid-template-columns: 200px minmax(0, 1fr);
  grid-template-rows: 1fr;
  gap: 0;
  height: calc(100vh - 220px);
  min-height: 400px;
}

.domain-manage-tab__sidebar {
  border-right: 1px solid var(--kb-border-light);
  display: flex;
  flex-direction: column;
}

.domain-manage-tab__sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--kb-border-light);
  flex-shrink: 0;
}

.domain-manage-tab__sidebar-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.domain-manage-tab__item {
  padding: 8px 12px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.domain-manage-tab__item:hover {
  background: var(--kb-bg-hover, rgba(0,0,0,0.03));
}

.domain-manage-tab__item--active {
  background: rgba(64, 158, 255, 0.08);
}

.domain-manage-tab__item-name {
  font-size: 13px;
  font-weight: 500;
  color: var(--kb-text-primary);
}

.domain-manage-tab__item-id {
  font-size: 11px;
  color: var(--kb-text-tertiary);
}

.domain-manage-tab__main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.domain-manage-tab__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  border-bottom: 1px solid var(--kb-border-light);
  flex-shrink: 0;
}

.domain-manage-tab__domain-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.domain-manage-tab__actions {
  display: flex;
  gap: 8px;
}

.domain-manage-tab__editor-wrap {
  flex: 1;
  min-height: 0;
  position: relative;
  overflow: hidden;
}

.domain-manage-tab__empty {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
  color: var(--kb-text-tertiary);
  font-size: 14px;
}
</style>
