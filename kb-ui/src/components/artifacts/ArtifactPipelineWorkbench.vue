<template>
  <section class="workbench" aria-label="制品加工工作台">
    <div class="toolbar"><div><b>表格加工 Pipeline</b><p>筛选输入 → 选择算子 → 查看产物 → 继续加工或发布</p></div>
      <el-select v-model="selectedRun" placeholder="全部历史 / 分支" :disabled="submitting" @change="selectHistory"><el-option v-for="run in runs" :key="run.id" :value="run.id" :label="`step${run.step_index || 1} · ${label(run.operator)} · ${statusLabel(run.status)} · ${new Date(run.started_at).toLocaleString()}`" /></el-select>
      <el-button :loading="loading" @click="refresh">刷新</el-button>
      <el-button type="primary" :disabled="snapshot?.status !== 'completed' || busy || submitting || snapshot?.preview" @click="publishVisible = true">发布当前步定版</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <div class="panes">
      <section class="pane" aria-label="输入来源"><header><b>左表 · {{ inputId ? '上一步全部产物' : '表格来源' }}</b><el-tag>{{ input?.total ?? sources.length }} {{ inputId ? '行' : '个文件' }}</el-tag></header>
        <div class="body">
          <el-button plain :disabled="submitting" data-test="choose-source" @click="pickerVisible = true">选择知识库目录 / 文件</el-button>
          <template v-if="inputId">
            <div v-for="(filter, i) in filters" :key="i" class="inline">
              <el-select v-model="filter.column" placeholder="字段" :disabled="!editing"><el-option v-for="c in inputColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select>
              <el-select v-model="filter.op" :disabled="!editing"><el-option v-for="op in filterOps" :key="op.value" :value="op.value" :label="op.label" /></el-select>
              <el-input v-model="filter.value" placeholder="值" :disabled="!editing" /><el-button v-if="editing" @click="filters.splice(i, 1)">×</el-button>
            </div>
            <div class="inline"><el-button :disabled="!editing" @click="filters.push({ column: inputColumns[0]?.key || '', op: 'contains', value: '' })">＋ 筛选条件</el-button><el-button :disabled="!editing" @click="inputPage = 1; loadInput()">应用筛选</el-button></div>
            <el-select v-model="keepColumns" multiple collapse-tags placeholder="保留列（为空时全部保留）" :disabled="!editing"><el-option v-for="c in inputColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select>
            <p>筛选作用于完整输入；筛选与保留列随步骤保存。</p>
            <PipelineTable :rows="input?.rows || []" :columns="selectedColumns" />
            <el-pagination v-if="(input?.total || 0) > 20" v-model:current-page="inputPage" :total="input?.total" :page-size="20" layout="total, prev, pager, next" small @current-change="loadInput" />
          </template>
          <template v-else><p>每张已抽取表格成为一行，包含目录、文件名、文件内容、表名、表格内容 JSON。</p>
            <el-table :data="sources.slice((sourcePage - 1) * 20, sourcePage * 20)" size="small" max-height="440"><el-table-column prop="directory_path" label="目录" min-width="100" /><el-table-column prop="document_name" label="文件名" min-width="150" /></el-table>
            <el-pagination v-if="sources.length > 20" v-model:current-page="sourcePage" :total="sources.length" :page-size="20" layout="prev, pager, next" small />
          </template>
        </div>
      </section>
      <section class="pane" aria-label="算子加工"><header><b>中间 · 算子加工</b><el-tag v-if="!editing">历史配置</el-tag></header><div class="body">
        <el-select v-model="operator" :disabled="!editing || !inputId" @change="resetParams"><el-option value="table_extract" label="表格抽取" :disabled="!!inputId" /><el-option v-for="item in operators" :key="item.id" :value="item.id" :label="item.name" /></el-select>
        <el-form label-position="top" :disabled="!editing || submitting">
          <p v-if="operator === 'table_extract'">读取已有抽取结果，不重复解析文档。</p><p v-if="operator === 'select'">使用左表筛选条件和保留列生成新快照。</p>
          <template v-if="operator === 'drop_columns'">
            <el-form-item label="选择要删除的列">
              <el-select v-model="params.drop_columns" multiple placeholder="可选择多列">
                <el-option v-for="c in selectedColumns" :key="c.key" :value="c.key" :label="c.name" />
              </el-select>
            </el-form-item>
            <p>从当前输入中删除所选列，至少保留一列。执行后生成新快照，历史步骤仍可回看。</p>
          </template>
          <el-form-item v-if="operator === 'deduplicate'" label="去重字段（保留首行）"><el-select v-model="params.keys" multiple><el-option v-for="c in selectedColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select></el-form-item>
          <template v-if="operator === 'fields'"><div v-for="(field, i) in params.fields" :key="i" class="field"><el-select v-model="field.source"><el-option v-for="c in selectedColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select><el-input v-model="field.key" placeholder="新字段名" /><el-select v-model="field.type"><el-option v-for="type in types" :key="type" :value="type" /></el-select><div><el-button size="small" :disabled="i === 0" @click="moveField(i)">上移</el-button><el-button size="small" @click="params.fields.splice(i, 1)">移除</el-button></div></div></template>
          <el-form-item v-if="['expand','merge'].includes(operator)" label="表格 JSON 列"><el-select v-model="params.table_column"><el-option v-for="c in selectedColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select></el-form-item>
          <el-form-item v-if="['expand','join'].includes(operator)" label="新增字段前缀"><el-input v-model="params.prefix" /></el-form-item>
          <template v-if="operator === 'classify'"><el-form-item label="表名字段"><el-select v-model="params.name_column"><el-option v-for="c in selectedColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select></el-form-item><el-form-item label="分类字段名"><el-input v-model="params.output_column" /></el-form-item><el-form-item label="名称相似度阈值"><el-slider v-model="params.threshold" :min="0" :max="1" :step="0.05" show-input /></el-form-item><p>归一化表名后按相似度分组。可点击结果中的分类单元格调整，再进行同类合并。</p></template>
          <template v-if="operator === 'merge'"><el-form-item label="分类字段"><el-select v-model="params.group_column"><el-option v-for="c in selectedColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select></el-form-item><p>按字段名并集合并同类表，保留来源。下一步可展开 JSON 成为二维资产表。</p></template>
          <template v-if="operator === 'join'"><label>对照表（.xlsx，首张工作表，首行表头）<input type="file" accept=".xlsx" :disabled="!editing || submitting" @change="uploadLookup" /></label><p>{{ lookupName || params.lookup_id || '尚未上传' }}</p><el-form-item label="左表关联键"><el-select v-model="params.left_key"><el-option v-for="c in selectedColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select></el-form-item><el-form-item label="对照表关联键"><el-select v-model="params.right_key" filterable allow-create><el-option v-for="c in lookupColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select></el-form-item><p>左关联；对照表键重复时阻止执行。</p></template>
          <template v-if="operator === 'llm_columns'"><el-form-item label="送入模型的列"><el-select v-model="params.input_columns" multiple><el-option v-for="c in selectedColumns" :key="c.key" :value="c.key" :label="c.name" /></el-select></el-form-item><el-form-item label="加工要求"><el-input v-model="params.prompt" type="textarea" :rows="5" placeholder="例如：从文件内容提取命令及参数说明，缺失时返回 null。" /></el-form-item><el-form-item label="模型（留空使用默认配置）"><el-input v-model="params.model" /></el-form-item><div v-for="(column, i) in params.columns" :key="i" class="inline"><el-input v-model="column.key" placeholder="新增列名" /><el-select v-model="column.type"><el-option v-for="type in types" :key="type" :value="type" /></el-select><el-button @click="params.columns.splice(i, 1)">×</el-button></div><el-button @click="params.columns.push({ key: '', type: 'text' })">＋ 新增列</el-button><p>先试跑前 5 行；正式执行支持仅重试失败行。</p></template>
          <p v-if="operator === 'edit'">点击右表单元格填写新值，将生成新的调整步骤。</p>
          <p v-if="operator === 'join' && params.lookup_sha256" :title="params.lookup_sha256">对照表内容版本：{{ params.lookup_sha256.slice(0, 12) }}</p>
        </el-form>
        <el-button v-if="editing && operator === 'llm_columns'" :disabled="busy" :loading="submitting" @click="execute(true)">试跑前 5 行</el-button>
        <el-button type="primary" :loading="submitting || busy" :disabled="!editing || (operator === 'table_extract' && !sources.length) || busy" data-test="execute-extraction" @click="execute(false)">{{ operator === 'table_extract' ? '执行表格抽取' : '执行完整输入' }}</el-button>
        <el-button v-if="busy" @click="cancel">取消当前步骤</el-button><el-button v-if="snapshot && ['failed', 'cancelled'].includes(snapshot.status)" @click="prepareRetry">调整后重试</el-button><div v-if="snapshot" role="status"><el-tag>{{ statusLabel(snapshot.status) }}</el-tag><p v-if="snapshot.progress">已处理 {{ snapshot.progress.done }} / {{ snapshot.progress.total }} 行</p></div>
        <el-alert v-if="snapshot?.error_summary" :title="snapshot.error_summary" type="error" :closable="false" />
        <el-button v-if="snapshot?.status === 'completed' && snapshot.operator === 'llm_columns' && snapshot.issues.some(i => i.reason === 'llm_failed')" :loading="submitting" @click="retryFailed">仅重试失败行</el-button><el-button v-if="snapshot?.preview" @click="fullRun">使用原始输入执行全量</el-button>
      </div></section>
      <section class="pane" aria-label="输出表"><header><b>右表 · 当前产物</b><el-tag>{{ snapshot?.total ?? 0 }} 行</el-tag></header><div class="body"><p>{{ editing ? '执行后生成新的快照；当前显示所选历史产物。' : '点击单元格查看内容与来源；修改会创建新步骤。' }}</p><el-alert v-if="snapshot?.preview" title="当前为试跑样本，不能发布" type="warning" :closable="false" />
        <PipelineTable :rows="snapshot?.rows || []" :columns="snapshot?.columns || defaultColumns" :editable="snapshot?.status === 'completed' && !submitting && !busy && !snapshot?.preview" @edit="editCell" />
        <router-link v-for="publication in snapshot?.publications || []" :key="`${publication.artifact_id}:${publication.revision}`" :to="`/artifacts/${publication.artifact_id}`">已定版：{{ publication.artifact_id }} · v{{ publication.revision }} · {{ publication.row_count }} 行</router-link>
        <el-pagination v-if="(snapshot?.total || 0) > 20" v-model:current-page="page" :total="snapshot?.total" :page-size="20" layout="total, prev, pager, next" small @current-change="loadSnapshot" />
        <el-collapse v-if="snapshot?.issues.length"><el-collapse-item :title="`处理情况（${snapshot.issues.length}）`"><p v-for="(issue, i) in snapshot.issues" :key="i">{{ issue.file_name }} {{ issue.message }}</p></el-collapse-item></el-collapse>
      </div></section>
    </div>
    <footer class="steps"><el-button v-for="run in chain" :key="run.id" :type="run.id === selectedRun ? 'primary' : 'default'" plain data-test="step-one" @click="selectedRun = run.id; selectHistory()">step{{ run.step_index || 1 }} · {{ label(run.operator) }}</el-button><el-button :disabled="snapshot?.status !== 'completed' || busy || submitting || snapshot?.preview" @click="addStep">下一步</el-button></footer>
    <el-dialog v-model="pickerVisible" title="选择表格来源" width="85%" destroy-on-close><ArtifactKbSourcePicker :domain="domain" :active="pickerVisible" @confirmed="chooseSource" /></el-dialog>
    <PipelinePublishDialog v-if="snapshot && publishVisible" :visible="publishVisible" :artifact-id="artifactId" :snapshot="snapshot" @close="publishVisible = false" @published="emit('published'); loadSnapshot()" />
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useArtifactPipelineApi, type PipelineColumn, type PipelineRun, type PipelineSnapshot, type TableSource } from '@/api/artifactPipeline'
import { apiErrorDetail } from '@/api/proxyClient'
import type { KbDocument } from '@/types/kb'
import ArtifactKbSourcePicker from './ArtifactKbSourcePicker.vue'
import PipelineTable from './PipelineTable.vue'
import PipelinePublishDialog from './PipelinePublishDialog.vue'
const props = defineProps<{ artifactId: string; domain: string; active: boolean }>()
const emit = defineEmits<{ published: [] }>()
const api = useArtifactPipelineApi()
const sources = ref<TableSource[]>([]), kbId = ref(''), runs = ref<PipelineRun[]>([]), selectedRun = ref('')
const snapshot = ref<PipelineSnapshot>(), input = ref<PipelineSnapshot>(), inputId = ref('')
const operator = ref('table_extract'), params = ref<Record<string, any>>({}), editing = ref(false)
const pickerVisible = ref(false), publishVisible = ref(false), loading = ref(false), submitting = ref(false), error = ref('')
const page = ref(1), inputPage = ref(1), sourcePage = ref(1)
const filters = ref<Array<{ column: string; op: string; value: string }>>([]), keepColumns = ref<string[]>([])
const operators = ref<Array<{ id: string; name: string }>>([]), lookupName = ref(''), lookupColumns = ref<PipelineColumn[]>([])
const types = ['text', 'int', 'float', 'bool', 'date', 'json']
const filterOps = [{ value: 'contains', label: '包含' }, { value: 'eq', label: '等于' }, { value: 'ne', label: '不等于' }, { value: 'empty', label: '为空' }, { value: 'not_empty', label: '非空' }, { value: 'gt', label: '大于' }, { value: 'lt', label: '小于' }]
const defaultColumns = ['directory', 'file_name', 'file_content', 'table_name', 'table_content'].map((key, i) => ({ key, name: ['目录', '文件名', '文件内容', '表名', '表格内容'][i]!, type: i === 4 ? 'json' : 'text' }))
const busy = computed(() => ['queued', 'running', 'interrupted'].includes(snapshot.value?.status || ''))
const inputColumns = computed(() => input.value?.columns || defaultColumns)
const selectedColumns = computed(() => inputColumns.value.filter(c => !keepColumns.value.length || keepColumns.value.includes(c.key)))
// Viewing an earlier snapshot must not shorten the branch displayed in the footer.
const branchTip = ref('')
function pathTo(runId: string) {
  const result: PipelineRun[] = [], seen = new Set<string>()
  let run = runs.value.find(r => r.id === runId)
  while (run && !seen.has(run.id)) {
    result.unshift(run); seen.add(run.id)
    run = runs.value.find(r => r.id === run!.parent_run_id)
  }
  return result
}
const chain = computed(() => pathTo(branchTip.value))
watch(selectedRun, id => {
  if (!id) { branchTip.value = ''; return }
  if (pathTo(branchTip.value).some(run => run.id === id)) return
  // History is newest first. For another branch, display its latest descendant.
  branchTip.value = runs.value.find(run => pathTo(run.id).some(step => step.id === id))?.id || id
}, { flush: 'sync' })
let generation = 0, inputGeneration = 0
let timer: ReturnType<typeof setTimeout> | undefined
function label(op?: string) { return operators.value.find(o => o.id === op)?.name || '表格抽取' }
function statusLabel(status: string) { return ({ queued: '排队中', running: '执行中', interrupted: '恢复中', completed: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[status] || status }
function resetParams() { const first = selectedColumns.value[0]?.key || ''; params.value = { drop_columns: [], keys: [first], fields: selectedColumns.value.map(c => ({ source: c.key, key: c.key, type: c.type })), table_column: 'table_content', name_column: 'table_name', output_column: '表格分类', threshold: .8, group_column: '表格分类', prefix: operator.value === 'join' ? '对照.' : '数据.', input_columns: [first], prompt: '', model: '', columns: [{ key: '', type: 'text' }], changes: [] }; lookupName.value = ''; lookupColumns.value = [] }
function moveField(index: string | number) { const i = Number(index); const fields = params.value.fields; [fields[i - 1], fields[i]] = [fields[i], fields[i - 1]] }
function chooseSource(value: { kbId: string; documents: KbDocument[] }) { generation++; inputGeneration++; clearTimeout(timer); kbId.value = value.kbId; sources.value = value.documents; sourcePage.value = 1; inputId.value = ''; input.value = undefined; operator.value = 'table_extract'; editing.value = true; pickerVisible.value = false; snapshot.value = undefined; selectedRun.value = '' }
async function loadInput() { const token = ++inputGeneration; if (!inputId.value) { input.value = undefined; return } try { const value = await api.snapshot(props.artifactId, inputId.value, inputPage.value, filters.value); if (token === inputGeneration) input.value = value } catch (e) { if (token === inputGeneration) { input.value = undefined; error.value = await apiErrorDetail(e) } } }
async function loadSnapshot() {
  clearTimeout(timer); if (!selectedRun.value) return
  const request = ++generation; loading.value = true; error.value = ''
  try { const value = await api.snapshot(props.artifactId, selectedRun.value, page.value); if (request !== generation) return; snapshot.value = value; runs.value = runs.value.map(r => r.id === value.id ? value : r); if (!editing.value) sources.value = value.sources; if (busy.value && props.active) timer = setTimeout(() => void loadSnapshot(), 2000) }
  catch (e) { if (request === generation) { snapshot.value = undefined; input.value = undefined; sources.value = []; error.value = `读取加工记录失败：${await apiErrorDetail(e)}` } }
  finally { if (request === generation) loading.value = false }
}
async function selectHistory() { editing.value = false; page.value = 1; sourcePage.value = 1; await loadSnapshot(); if (!snapshot.value) return; operator.value = snapshot.value.operator || 'table_extract'; params.value = JSON.parse(JSON.stringify(snapshot.value.parameters || {})); restoreLookup(); filters.value = params.value.filters || []; keepColumns.value = params.value.keep_columns || []; inputId.value = snapshot.value.parent_run_id || ''; inputPage.value = 1; await loadInput() }
async function refresh() { try { runs.value = await api.runs(props.artifactId); if (!selectedRun.value && !editing.value) { selectedRun.value = runs.value[0]?.id || ''; await selectHistory() } else await loadSnapshot() } catch (e) { error.value = `加载工作台失败：${await apiErrorDetail(e)}` } }
async function addStep() { if (!snapshot.value || snapshot.value.preview) return; inputId.value = snapshot.value.id; input.value = snapshot.value; filters.value = []; keepColumns.value = []; inputPage.value = 1; operator.value = 'select'; editing.value = true; resetParams(); await loadInput() }
async function execute(preview = false) {
  if (!editing.value || busy.value || submitting.value) return
  submitting.value = true; error.value = ''
  try { const result = operator.value === 'table_extract' ? await api.extract(props.artifactId, kbId.value, sources.value.map(s => s.id)) : await api.step(props.artifactId, inputId.value, operator.value, { ...params.value, filters: filters.value, keep_columns: keepColumns.value, preview }); selectedRun.value = result.run_id; editing.value = false; page.value = 1; await refresh(); await selectHistory() }
  catch (e) { error.value = `启动加工失败：${await apiErrorDetail(e)}` } finally { submitting.value = false }
}
async function editCell(change: { row_id: string; column: string; value: string }) { await addStep(); operator.value = 'edit'; params.value = { changes: [change] }; await execute() }
async function retryFailed() { const previous = snapshot.value?.parameters; await addStep(); operator.value = 'llm_columns'; params.value = { ...previous, retry_failed: true }; await execute() }
async function fullRun() { if (!snapshot.value?.parent_run_id) return; inputId.value = snapshot.value.parent_run_id; operator.value = 'llm_columns'; params.value = JSON.parse(JSON.stringify(snapshot.value.parameters || {})); params.value.preview = false; filters.value = params.value.filters || []; keepColumns.value = params.value.keep_columns || []; editing.value = true; await loadInput() }
function restoreLookup() { lookupColumns.value = params.value.lookup_columns || []; lookupName.value = params.value.lookup_filename || '' }
async function prepareRetry() { if (!snapshot.value) return; kbId.value = snapshot.value.kb_id || ''; sources.value = snapshot.value.sources; inputId.value = snapshot.value.parent_run_id || ''; operator.value = snapshot.value.operator || 'table_extract'; params.value = JSON.parse(JSON.stringify(snapshot.value.parameters || {})); restoreLookup(); filters.value = params.value.filters || []; keepColumns.value = params.value.keep_columns || []; editing.value = true; await loadInput() }
async function cancel() { try { await api.cancel(props.artifactId, selectedRun.value); await loadSnapshot() } catch (e) { error.value = await apiErrorDetail(e) } }
async function uploadLookup(event: Event) { const file = (event.target as HTMLInputElement).files?.[0]; if (!file) return; submitting.value = true; try { const value = await api.lookup(props.artifactId, file); lookupName.value = `${value.filename} · ${value.row_count} 行`; lookupColumns.value = value.columns; params.value.lookup_id = value.lookup_id; params.value.right_key = value.columns[0]?.key } catch (e) { error.value = await apiErrorDetail(e) } finally { submitting.value = false } }
watch(() => props.artifactId, () => { generation++; inputGeneration++; clearTimeout(timer); snapshot.value = undefined; selectedRun.value = ''; inputId.value = ''; editing.value = false; runs.value = []; void refresh(); if (api.operators) void api.operators(props.artifactId).then(value => { operators.value = value }).catch(async e => { error.value = await apiErrorDetail(e) }) }, { immediate: true })
watch(() => props.active, active => { if (active) void refresh(); else clearTimeout(timer) })
onBeforeUnmount(() => { generation++; inputGeneration++; clearTimeout(timer) })
</script>
<style scoped>
.workbench { display: flex; flex-direction: column; gap: 16px; }.toolbar, .steps, .inline { display: flex; align-items: center; gap: 8px; }.toolbar { flex-wrap: wrap; }.toolbar > div { flex: 1; }.toolbar > .el-select { flex: none; width: 380px; }
.panes { display: grid; grid-template-columns: minmax(300px, 1fr) minmax(260px, .7fr) minmax(320px, 1fr); gap: 12px; }.pane { min-width: 0; border: 1px solid var(--el-border-color-light); border-radius: 10px; background: var(--el-bg-color); overflow: hidden; }header { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 16px; border-bottom: 1px solid var(--el-border-color-light); }.body { display: flex; flex-direction: column; gap: 12px; padding: 14px; }p { font-size: 13px; line-height: 1.7; color: var(--kb-text-secondary); margin: 6px 0; }.inline > .el-select, .inline > .el-input { min-width: 70px; flex: 1; }.field { display: flex; flex-direction: column; gap: 6px; padding: 10px 0; border-bottom: 1px solid var(--el-border-color-light); }.steps { position: sticky; bottom: 0; padding: 14px; background: var(--el-bg-color); border: 1px solid var(--el-border-color-light); border-radius: 10px; flex-wrap: wrap; z-index: 2; }
@media(max-width: 1400px) { .panes { grid-template-columns: minmax(280px, 1fr) minmax(260px, .8fr); }.pane:last-child { grid-column: 1 / -1; } }@media(max-width: 700px) { .panes { grid-template-columns: 1fr; }.toolbar > .el-select { width: 100%; } }
</style>
