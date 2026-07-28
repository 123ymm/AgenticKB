<template>
  <el-drawer
    :model-value="modelValue"
    :title="doc ? doc.document_name : '预览'"
    direction="rtl"
    size="55%"
    @update:model-value="emit('update:modelValue', $event)"
    @close="cleanup"
  >
    <div v-loading="loading" class="preview">
      <div v-if="error" class="preview__error">
        <el-icon :size="28"><WarningFilled /></el-icon>
        <p>{{ error }}</p>
        <el-button v-if="doc" size="small" @click="download">下载查看</el-button>
      </div>

      <div v-else-if="tooLarge" class="preview__error">
        <el-icon :size="28"><Document /></el-icon>
        <p>文件较大（{{ (blobSize / 1024 / 1024).toFixed(1) }} MB），未在线渲染。</p>
        <el-button v-if="doc" type="primary" size="small" @click="download">下载</el-button>
      </div>

      <!-- markdown / html -->
      <div v-else-if="html" class="preview__rich" v-html="html" />

      <!-- image -->
      <div v-else-if="kind === 'image' && objectUrl" class="preview__img">
        <img :src="objectUrl" :alt="doc?.document_name" />
      </div>

      <!-- pdf -->
      <div v-else-if="kind === 'pdf' && objectUrl" class="preview__pdf">
        <iframe :src="objectUrl" :title="doc?.document_name" />
      </div>

      <!-- plain text / code -->
      <pre v-else-if="text !== null" class="preview__text">{{ text }}</pre>

      <div v-else-if="kind === 'unsupported'" class="preview__error">
        <el-icon :size="28"><Document /></el-icon>
        <p>该类型暂不支持在线预览（{{ ext || '未知' }}）。</p>
        <el-button v-if="doc" size="small" @click="download">下载查看</el-button>
      </div>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Document, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useKbApi } from '@/api/kb'
import { apiErrorDetail } from '@/api/proxyClient'
import { filenameFromDisposition, saveBlob } from '@/utils/download'
import type { KbDocument } from '@/types/kb'

const PREVIEW_MAX_BYTES = 50 * 1024 * 1024 // 50MB 以上不在线渲染

const props = defineProps<{ modelValue: boolean; kbId: string; doc: KbDocument | null }>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const kbApi = useKbApi()
const loading = ref(false)
const error = ref('')
const text = ref<string | null>(null)
const html = ref('')
const objectUrl = ref<string | null>(null)
const blobSize = ref(0)

const ext = computed(() => {
  const n = props.doc?.document_name || ''
  const i = n.lastIndexOf('.')
  return i >= 0 ? n.slice(i + 1).toLowerCase() : ''
})

const kind = computed<'md' | 'html' | 'image' | 'pdf' | 'text' | 'unsupported'>(() => {
  const e = ext.value
  if (['md', 'markdown'].includes(e)) return 'md'
  if (['htm', 'html'].includes(e)) return 'html'
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico'].includes(e)) return 'image'
  if (e === 'pdf') return 'pdf'
  if ([
    'txt', 'log', 'csv', 'json', 'yaml', 'yml', 'xml', 'js', 'ts', 'py',
    'sh', 'java', 'c', 'cpp', 'h', 'hpp', 'go', 'rs', 'sql', 'ini', 'conf', 'toml',
  ].includes(e)) return 'text'
  return 'unsupported'
})

const tooLarge = computed(() => blobSize.value > PREVIEW_MAX_BYTES &&
  (kind.value === 'pdf' || kind.value === 'image' || kind.value === 'text'))

function cleanup() {
  if (objectUrl.value) {
    URL.revokeObjectURL(objectUrl.value)
    objectUrl.value = null
  }
  text.value = null
  html.value = ''
  error.value = ''
  blobSize.value = 0
}

watch(
  () => [props.modelValue, props.doc?.id] as const,
  async ([open]) => {
    if (!open || !props.doc) return
    cleanup()
    loading.value = true
    try {
      const blob = await kbApi.downloadDocument(props.kbId, props.doc.id)
      blobSize.value = blob.size
      const k = kind.value
      if (k === 'md') {
        html.value = DOMPurify.sanitize(await marked(await blob.text()))
      } else if (k === 'html') {
        html.value = DOMPurify.sanitize(await blob.text())
      } else if (k === 'image' || k === 'pdf') {
        if (blob.size > PREVIEW_MAX_BYTES) {
          // tooLarge computed 会接管
        } else {
          objectUrl.value = URL.createObjectURL(blob)
        }
      } else if (k === 'text') {
        text.value = await blob.text()
      }
    } catch (e) {
      error.value = await apiErrorDetail(e)
    } finally {
      loading.value = false
    }
  },
)

async function download() {
  if (!props.doc) return
  try {
    const blob = await kbApi.downloadDocument(props.kbId, props.doc.id)
    saveBlob(blob, filenameFromDisposition(null, props.doc.document_name))
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  }
}
</script>

<style scoped>
.preview { min-height: 200px; padding: 0 4px; }
.preview__error {
  display: flex; flex-direction: column; align-items: center; gap: 10px;
  padding: 48px 24px; color: var(--kb-text-tertiary); text-align: center;
}
.preview__error p { margin: 0; font-size: 13px; }
.preview__rich { font-size: 14px; line-height: 1.7; color: var(--kb-text-primary); }
.preview__rich :deep(h1), .preview__rich :deep(h2), .preview__rich :deep(h3) { margin: 1em 0 0.4em; }
.preview__rich :deep(pre) {
  background: var(--kb-bg-card); border: 1px solid var(--kb-border-light);
  border-radius: 6px; padding: 12px; overflow-x: auto; font-size: 12.5px;
}
.preview__rich :deep(code) { font-family: 'SF Mono', 'Cascadia Code', monospace; }
.preview__rich :deep(table) { border-collapse: collapse; width: 100%; margin: 0.8em 0; }
.preview__rich :deep(th), .preview__rich :deep(td) { border: 1px solid var(--kb-border); padding: 6px 10px; }
.preview__rich :deep(img) { max-width: 100%; border-radius: 4px; }
.preview__img { display: flex; justify-content: center; padding: 12px; }
.preview__img img { max-width: 100%; border-radius: 6px; box-shadow: var(--kb-shadow-card); }
.preview__pdf { height: calc(100vh - 160px); }
.preview__pdf iframe { width: 100%; height: 100%; border: 0; border-radius: 6px; }
.preview__text {
  font-family: 'SF Mono', 'Cascadia Code', monospace; font-size: 12.5px; line-height: 1.55;
  white-space: pre-wrap; word-break: break-word; color: var(--kb-text-secondary);
  margin: 0; padding: 8px 4px;
}
</style>
