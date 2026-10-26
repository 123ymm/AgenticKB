<template>
  <el-dialog :model-value="visible" title="发布当前步骤定版" width="850px" @update:model-value="emit('close')">
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-form label-position="top" :disabled="busy || !!result?.revision">
      <el-form-item label="发布目标"><el-select v-model="target" @change="resetMapping"><el-option value="" label="新增表格资产" /><el-option v-for="item in targets" :key="item.artifact_id" :value="item.artifact_id" :label="item.title" /></el-select></el-form-item>
      <el-form-item v-if="!target" label="新资产名称"><el-input v-model="title" /></el-form-item>
      <el-form-item label="使用说明"><el-input v-model="usage" type="textarea" /></el-form-item>
      <el-table :data="mapping" size="small">
        <el-table-column label="发布" width="65"><template #default="{ row }"><el-checkbox v-model="row.enabled" /></template></el-table-column>
        <el-table-column prop="source" label="来源字段" />
        <el-table-column label="目标字段"><template #default="{ row }"><el-select v-if="activeColumns.length" v-model="row.target_column_id"><el-option v-for="c in activeColumns" :key="c.column_id" :value="c.column_id" :label="`${c.column_name} (${c.column_type})${c.is_primary_key ? ' · 主键' : ''}`" /></el-select><el-input v-else v-model="row.target_name" /></template></el-table-column>
        <el-table-column v-if="!activeColumns.length" label="类型"><template #default="{ row }"><el-select v-model="row.column_type"><el-option v-for="type in ['text','int','float','bool','date']" :key="type" :value="type" /></el-select></template></el-table-column>
        <el-table-column v-if="!activeColumns.length" label="主键" width="65"><template #default="{ row }"><el-checkbox v-model="row.is_primary_key" /></template></el-table-column>
      </el-table>
      <el-form-item label="目标主键冲突"><el-radio-group v-model="conflict"><el-radio value="block">阻止发布</el-radio><el-radio value="overwrite">覆盖目标同主键行</el-radio></el-radio-group></el-form-item>
      <el-checkbox v-model="jsonText">JSON 字段以完整 JSON 文本发布</el-checkbox>
      <el-checkbox v-if="snapshot.issues.length" v-model="acknowledge">已查看来源处理情况，确认发布现有产物</el-checkbox>
    </el-form>
    <el-alert v-if="result?.preview_token" type="info" :closable="false" :title="`本次 ${result.input_rows} 行：新增 ${result.inserted}，覆盖 ${result.updated}`" />
    <pre v-if="result?.sample">{{ JSON.stringify(result.sample, null, 2) }}</pre>
    <el-alert v-if="result?.revision" type="success" :closable="false" :title="`已发布版本 v${result.revision}，共 ${result.row_count} 行`" />
    <router-link v-if="result?.artifact_id" :to="`/artifacts/${result.artifact_id}`">查看已发布资产</router-link>
    <template #footer><el-button @click="emit('close')">关闭</el-button><el-button :loading="busy" :disabled="!!result?.revision" @click="publish(false)">预览发布</el-button><el-button type="primary" :loading="busy" :disabled="!result?.preview_token || !!result?.revision" @click="publish(true)">确认发布</el-button></template>
  </el-dialog>
</template>
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useArtifactPipelineApi, type PipelineSnapshot } from '@/api/artifactPipeline'
import { apiErrorDetail } from '@/api/proxyClient'
const props = defineProps<{ visible: boolean; artifactId: string; snapshot: PipelineSnapshot }>()
const emit = defineEmits<{ close: []; published: [] }>()
const api = useArtifactPipelineApi()
const targets = ref<Awaited<ReturnType<typeof api.targets>>>([]), target = ref(''), title = ref(''), usage = ref(''), conflict = ref('block')
const jsonText = ref(false), acknowledge = ref(false), busy = ref(false), error = ref(''), result = ref<Record<string, any>>()
const activeColumns = computed(() => targets.value.find(t => t.artifact_id === target.value)?.columns || [])
const mapping = ref<Array<{ source: string; enabled: boolean; target_name: string; target_column_id: string; column_type: string; is_primary_key: boolean }>>([])
function resetMapping() { mapping.value = (props.snapshot.columns || []).map(c => ({ source: c.key, enabled: true, target_name: c.name, target_column_id: activeColumns.value.find(t => t.column_name === c.key || t.column_name === c.name)?.column_id || '', column_type: c.type === 'json' ? 'text' : c.type, is_primary_key: false })); result.value = undefined }
watch(() => props.visible, async visible => { if (visible) { result.value = undefined; error.value = ''; try { targets.value = await api.targets(props.artifactId); target.value = targets.value.some(t => t.artifact_id === props.artifactId) ? props.artifactId : ''; resetMapping() } catch (e) { error.value = await apiErrorDetail(e) } } }, { immediate: true })
watch([mapping, target, title, usage, conflict, jsonText, acknowledge], () => { result.value = undefined }, { deep: true })
async function publish(confirm: boolean) {
  busy.value = true; error.value = ''
  try {
    result.value = await api.publish(props.artifactId, props.snapshot.id, { target_artifact_id: target.value || null, title: title.value, usage_note: usage.value, conflict_policy: conflict.value, json_as_text: jsonText.value, acknowledge_issues: acknowledge.value, preview_token: result.value?.preview_token,
      columns: mapping.value.filter(c => c.enabled).map(c => ({ ...c, action: activeColumns.value.length ? 'map_to' : 'new' })) }, confirm)
    if (result.value.revision) emit('published')
  } catch (e) { error.value = await apiErrorDetail(e); result.value = undefined }
  finally { busy.value = false }
}
</script>
<style scoped>pre { max-height: 220px; overflow: auto; white-space: pre-wrap; } .el-alert { margin: 12px 0; }</style>
