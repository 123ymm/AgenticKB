<template>
  <el-dialog
    :model-value="modelValue"
    title="添加算子"
    width="560px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <!-- 算子卡片：名称 + 一句话描述 + 图标 -->
    <div class="ws-nodedlg__ops">
      <button
        v-for="def in OP_CARDS"
        :key="def.op"
        type="button"
        class="ws-nodedlg__card"
        :class="{ 'is-selected': op === def.op }"
        :data-test="`node-card-${def.op}`"
        @click="pick(def.op)"
      >
        <el-icon class="ws-nodedlg__card-icon"><component :is="def.icon" /></el-icon>
        <span class="ws-nodedlg__card-name">{{ def.name }}</span>
        <span class="ws-nodedlg__card-desc">{{ def.desc }}</span>
      </button>
    </div>

    <!-- join：左右两表（左=当前选中端口，右=必选） -->
    <div v-if="op === 'join'" class="ws-nodedlg__join">
      <div class="ws-nodedlg__field">
        <label class="ws-nodedlg__label">左表（当前选中）</label>
        <el-select v-model="joinLeft" data-test="node-join-left" size="small" filterable placeholder="选择左表">
          <el-option v-for="p in portOptions" :key="p.ref" :value="p.ref" :label="p.label" />
        </el-select>
      </div>
      <div class="ws-nodedlg__field">
        <label class="ws-nodedlg__label">右表</label>
        <el-select v-model="joinRight" data-test="node-join-right" size="small" filterable placeholder="选择第二张表">
          <el-option v-for="p in portOptions" :key="p.ref" :value="p.ref" :label="p.label" />
        </el-select>
      </div>
    </div>
    <p v-else-if="op" class="ws-nodedlg__hint">
      输入默认接当前选中输出「{{ defaultLabel ?? '未选中' }}」，可在保存后于配置面板调整
    </p>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :disabled="!canConfirm" data-test="node-submit" @click="confirm">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Connection, Filter, List, MagicStick, Scissor } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import type { Component } from 'vue'

/**
 * 添加算子对话框（67号 Task 12）：5 个算子卡片（名称+一句话+图标）；确定
 * 只 emit confirm({op, inputs})——节点生成（id 命名/默认 config/端口声明/
 * PUT graph）由父级承接。inputs 默认=当前选中 ref；join 需再选第二表
 * （左右各一下拉，右表必选才可确定）。
 */

const props = defineProps<{
  modelValue: boolean
  /** 可作输入的端口全集（ref + 展示名）。 */
  portOptions: Array<{ ref: string; label: string }>
  /** 当前选中的 node:port（新算子输入默认值）。 */
  defaultRef: string | null
}>()

const emit = defineEmits<{
  'update:modelValue': [boolean]
  confirm: [payload: { op: string; inputs: string[] }]
}>()

interface OpCard { op: string; name: string; desc: string; icon: Component }

const OP_CARDS: OpCard[] = [
  { op: 'select', name: '选取列', desc: '挑选并重命名输出列', icon: List },
  { op: 'filter', name: '过滤行', desc: '按字段条件筛出需要的行', icon: Filter },
  { op: 'join', name: '关联', desc: '按键合并两张表（左/内连接）', icon: Connection },
  { op: 'split_sections', name: '拆章节', desc: '把文档按标题级别拆成长表', icon: Scissor },
  { op: 'llm_extract', name: 'LLM 抽取', desc: '逐行调用大模型抽取结构化字段', icon: MagicStick },
]

const op = ref('')
const joinLeft = ref('')
const joinRight = ref('')

watch(() => props.modelValue, (open) => {
  if (!open) return
  op.value = ''
  joinRight.value = ''
  joinLeft.value = props.defaultRef ?? ''
}, { immediate: true })

function pick(next: string): void {
  op.value = next
}

const defaultLabel = computed(() =>
  props.portOptions.find(p => p.ref === props.defaultRef)?.label ?? null)

const canConfirm = computed(() => {
  if (!op.value) return false
  if (op.value === 'join') return !!joinLeft.value && !!joinRight.value
  return true
})

function close(): void {
  emit('update:modelValue', false)
}

function confirm(): void {
  if (!op.value) return
  const inputs = op.value === 'join'
    ? [joinLeft.value, joinRight.value]
    : props.defaultRef
      ? [props.defaultRef]
      : []
  if (!inputs.length) {
    ElMessage.warning('请先在步骤流选中一个输出端口作为输入（或稍后在配置面板选择）')
    return
  }
  emit('update:modelValue', false)
  emit('confirm', { op: op.value, inputs })
}
</script>

<style scoped>
.ws-nodedlg__ops {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}
.ws-nodedlg__card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 12px 14px;
  border: 1px solid var(--kb-border, #e2e8f0);
  border-radius: 10px;
  background: var(--kb-bg-card, #fff);
  cursor: pointer;
  font-family: inherit;
  text-align: left;
  transition: border-color .12s ease, background .12s ease;
}
.ws-nodedlg__card:hover {
  border-color: var(--kb-accent, #0891b2);
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.06));
}
.ws-nodedlg__card.is-selected {
  border-color: var(--kb-accent, #0891b2);
  background: var(--kb-accent-soft, rgba(8, 145, 178, 0.1));
}
.ws-nodedlg__card-icon {
  font-size: 18px;
  color: var(--kb-accent, #0891b2);
}
.ws-nodedlg__card-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary, #0f172a);
}
.ws-nodedlg__card-desc {
  font-size: 11.5px;
  line-height: 1.4;
  color: var(--kb-text-tertiary, #94a3b8);
}
.ws-nodedlg__join {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 14px;
}
.ws-nodedlg__field { min-width: 0; }
.ws-nodedlg__label {
  display: block;
  margin-bottom: 5px;
  font-size: 11.5px;
  color: var(--kb-text-secondary, #64748b);
}
.ws-nodedlg__hint {
  margin: 12px 0 0;
  font-size: 11.5px;
  color: var(--kb-text-tertiary, #94a3b8);
}
</style>
