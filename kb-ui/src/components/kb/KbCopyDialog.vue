<template>
  <el-dialog
    :model-value="modelValue"
    title="拷贝知识库"
    width="480px"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-alert type="info" :closable="false" class="kb-copy__notice">
      <p>拷贝包含全部文件、目录结构与知识血缘（共享源库的解析与向量资产）。</p>
      <p>拷贝完成即可开始挖掘：选择与源库<b>相同的挖掘范式</b>可复用已有解析与向量，几乎零成本；换范式则全量重算。</p>
    </el-alert>
    <el-form :model="form" label-width="72px" @submit.prevent>
      <el-form-item label="源库">
        <el-input :model-value="kb?.name ?? ''" disabled />
      </el-form-item>
      <el-form-item label="名称" required>
        <el-input v-model="form.name" maxlength="80" show-word-limit />
      </el-form-item>
      <el-form-item label="可见性">
        <el-radio-group v-model="form.visibility" :disabled="!canSetPublic && form.visibility === 'public'">
          <el-radio value="private">私有</el-radio>
          <el-radio value="public" :disabled="!canSetPublic">公开</el-radio>
        </el-radio-group>
        <div v-if="!canSetPublic" class="kb-copy__hint">
          仅源库 owner / 站点管理员可将副本设为公开（防止他人私有知识被扩散）。
        </div>
      </el-form-item>
      <el-form-item label="描述">
        <el-input
          v-model="form.description"
          type="textarea"
          :rows="3"
          maxlength="300"
          show-word-limit
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">拷贝</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useKbApi } from '@/api/kb'
import { apiErrorDetail } from '@/api/proxyClient'
import type { KbCopyResult, KbSummary, KbVisibility } from '@/types/kb'

const props = defineProps<{ modelValue: boolean; kb: KbSummary | null }>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  copied: [KbCopyResult]
}>()

const kbApi = useKbApi()
const authStore = useAuthStore()
const submitting = ref(false)

const form = reactive<{ name: string; visibility: KbVisibility; description: string }>({
  name: '',
  visibility: 'private',
  description: '',
})

/** 防放大规则（后端强约束，此处仅 UI 预锁）：public 仅源库 owner / site-admin。 */
const canSetPublic = computed(
  () => props.kb?.my_role === 'owner' || authStore.siteRole === 'admin',
)

/** 缺省名派生（与后端 _derive_copy_name 同规则）：源名超长先截源名保后缀。 */
function deriveName(sourceName: string): string {
  const suffix = `-副本-${new Date().toISOString().slice(0, 10).replace(/-/g, '')}`
  const stem = sourceName.length + suffix.length > 80
    ? sourceName.slice(0, 80 - suffix.length).trimEnd()
    : sourceName
  return stem + suffix
}

// 每次打开按源库预填
watch(
  () => props.modelValue,
  (open) => {
    if (open && props.kb) {
      form.name = deriveName(props.kb.name)
      // 非 owner 继承 public 会被后端强制 private——直接预填 private，不制造假选项
      form.visibility = props.kb.visibility === 'public' && canSetPublic.value
        ? 'public'
        : 'private'
      form.description = props.kb.description ?? ''
    }
  },
)

function close() {
  emit('update:modelValue', false)
}

async function submit() {
  const name = form.name.trim()
  if (!name) {
    ElMessage.warning('请输入新知识库名称')
    return
  }
  submitting.value = true
  try {
    const result = await kbApi.copyKb(props.kb!.id, {
      new_name: name,
      visibility: form.visibility,
      description: form.description.trim() || null,
    })
    ElMessage.success(
      `已拷贝 ${result.copy_stats.documents} 个文档`
      + `（${result.copy_stats.linked_snapshots} 份知识血缘）——选择挖掘范式后即可挖掘`,
    )
    emit('update:modelValue', false)
    emit('copied', result)
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.kb-copy__notice {
  margin-bottom: 14px;
}
.kb-copy__notice p {
  margin: 0 0 4px;
  font-size: 12.5px;
  line-height: 1.6;
}
.kb-copy__notice p:last-child {
  margin-bottom: 0;
}
.kb-copy__hint {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--kb-text-tertiary);
  line-height: 1.5;
}
</style>
