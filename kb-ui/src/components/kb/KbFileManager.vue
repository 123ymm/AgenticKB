<template>
  <div class="fm">
    <!-- Toolbar: breadcrumb + actions -->
    <div class="fm__toolbar">
      <el-breadcrumb separator="/" class="fm__crumb">
        <el-breadcrumb-item>
          <span class="fm__crumb-root" @click="navTo(null)">根目录</span>
        </el-breadcrumb-item>
        <el-breadcrumb-item v-for="seg in breadcrumb" :key="seg.id">
          <span class="fm__crumb-seg" @click="navTo(seg.id)">{{ seg.name }}</span>
        </el-breadcrumb-item>
      </el-breadcrumb>
      <div class="fm__actions">
        <el-button v-if="canWrite" size="small" @click="newFolder">
          <el-icon class="el-icon--left"><FolderAdd /></el-icon>新建文件夹
        </el-button>
        <el-upload
          v-if="canWrite"
          :show-file-list="false"
          :multiple="true"
          :auto-upload="true"
          :http-request="handleUpload"
          :disabled="uploading > 0"
        >
          <el-button size="small" type="primary" :loading="uploading > 0">
            <el-icon class="el-icon--left"><UploadFilled /></el-icon>上传到当前文件夹
          </el-button>
        </el-upload>
        <el-button text size="small" :loading="loading" @click="reload">
          <el-icon><Refresh /></el-icon>
        </el-button>
      </div>
    </div>

    <!-- List (details view) -->
    <div class="fm__list" v-loading="loading">
      <!-- column header -->
      <div class="fm__row fm__row--head">
        <div class="fm__col fm__col--name">名称</div>
        <div class="fm__col fm__col--type">类型</div>
        <div class="fm__col fm__col--status">状态</div>
        <div class="fm__col fm__col--time">上传时间</div>
        <div class="fm__col fm__col--ops">操作</div>
      </div>

      <!-- drop-to-root zone (only while dragging inside a subfolder) -->
      <div
        v-if="currentFolderId !== null && dragId"
        class="fm__rootdrop"
        @dragover.prevent
        @drop="dropToRoot"
      >拖到此处 = 移到根目录</div>

      <!-- folders -->
      <div
        v-for="f in childFolders"
        :key="f.id"
        class="fm__row fm__row--folder"
        :class="{ 'fm__row--dragover': dragOverId === f.id }"
        draggable="true"
        @dragstart="onDragStart($event, 'folder', f.id)"
        @dragend="onDragEnd"
        @dragover.prevent="dragOverId = f.id"
        @dragleave="dragOverId = null"
        @drop.stop="onDrop($event, f.id)"
        @dblclick="enterFolder(f.id)"
      >
        <div class="fm__col fm__col--name">
          <el-icon class="fm__icon fm__icon--folder"><Folder /></el-icon>
          <span class="fm__name" :title="f.name">{{ f.name }}</span>
        </div>
        <div class="fm__col fm__col--type">文件夹</div>
        <div class="fm__col fm__col--status fm__col--muted">—</div>
        <div class="fm__col fm__col--time">{{ formatTime(f.created_at) }}</div>
        <div class="fm__col fm__col--ops">
          <el-dropdown v-if="canWrite" trigger="click" @click.stop>
            <el-icon class="fm__more"><MoreFilled /></el-icon>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="renameFolder(f)">改名</el-dropdown-item>
                <el-dropdown-item @click="deleteFolder(f)" divided>删除</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <!-- files -->
      <div
        v-for="file in files"
        :key="file.id"
        class="fm__row fm__row--file"
        draggable="true"
        @dragstart="onDragStart($event, 'file', file.id)"
        @dragend="onDragEnd"
        @click="preview(file)"
      >
        <div class="fm__col fm__col--name">
          <el-icon class="fm__icon" :class="fileIconClass(file)"><component :is="fileIcon(file)" /></el-icon>
          <span class="fm__name" :title="file.document_name">{{ file.document_name }}</span>
        </div>
        <div class="fm__col fm__col--type">{{ extOf(file.document_name) || '文件' }}</div>
        <div class="fm__col fm__col--status">
          <el-tag v-if="file.status" :type="docStatusTagType(file.status)" size="small" effect="light">
            {{ docStatusLabel(file.status) }}
          </el-tag>
          <span v-else class="fm__col--muted">—</span>
        </div>
        <div class="fm__col fm__col--time">{{ formatTime(file.created_at) }}</div>
        <div class="fm__col fm__col--ops">
          <el-dropdown trigger="click" @click.stop>
            <el-icon class="fm__more"><MoreFilled /></el-icon>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="preview(file)">预览</el-dropdown-item>
                <el-dropdown-item @click="download(file)">下载</el-dropdown-item>
                <el-dropdown-item v-if="canWrite" @click="renameFile(file)" divided>改名</el-dropdown-item>
                <el-dropdown-item v-if="canWrite" disabled>撤回(待接)</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>

      <EmptyState
        v-if="!loading && !childFolders.length && !files.length"
        :text="canWrite ? '空文件夹，上传文件或新建子文件夹' : '空文件夹'"
      />
    </div>

    <KbFilePreview v-model="previewOpen" :kb-id="kbId" :doc="previewDoc" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  Folder, FolderAdd, UploadFilled, Refresh, MoreFilled,
  Document, Picture, Tickets,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import { useKbApi } from '@/api/kb'
import { apiErrorDetail } from '@/api/proxyClient'
import { filenameFromDisposition, saveBlob } from '@/utils/download'
import EmptyState from '@/components/common/EmptyState.vue'
import KbFilePreview from '@/components/kb/KbFilePreview.vue'
import { docStatusLabel, docStatusTagType } from '@/views/kb/kbMeta'
import type { Component } from 'vue'
import type { KbDocument, KbFolder } from '@/types/kb'

const props = defineProps<{ kbId: string; canWrite: boolean }>()
const kbApi = useKbApi()

const folders = ref<KbFolder[]>([])
const files = ref<KbDocument[]>([])
const currentFolderId = ref<string | null>(null) // null = 根
const loading = ref(false)
const uploading = ref(0)

const currentFolder = computed(() => folders.value.find((f) => f.id === currentFolderId.value) ?? null)
const currentPath = computed(() => currentFolder.value?.path ?? '')
const childFolders = computed(() =>
  folders.value.filter((f) => f.parent_id === currentFolderId.value),
)

const breadcrumb = computed(() => {
  const segs: { id: string; name: string }[] = []
  const parts = currentPath.value ? currentPath.value.split('/') : []
  let acc = ''
  for (const p of parts) {
    acc = acc ? `${acc}/${p}` : p
    const f = folders.value.find((x) => x.path === acc)
    if (f) segs.push({ id: f.id, name: f.name })
  }
  return segs
})

async function loadFolders() {
  folders.value = await kbApi.listFolders(props.kbId)
}
async function loadFiles() {
  // currentPath 为 "" 时后端按 directory_path="" 精确匹配（根目录文件）
  files.value = await kbApi.listDocuments(props.kbId, currentPath.value)
}
async function reload() {
  loading.value = true
  try {
    await Promise.all([loadFolders(), loadFiles()])
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    loading.value = false
  }
}

function navTo(id: string | null) { currentFolderId.value = id }
function enterFolder(id: string) { currentFolderId.value = id }

// ── 文件夹 CRUD ──
async function newFolder() {
  let name: string
  try {
    const r = await ElMessageBox.prompt('文件夹名称', '新建文件夹', {
      confirmButtonText: '创建', cancelButtonText: '取消',
      inputValidator: (v) => (!!v?.trim() && !/[\\/]/.test(v)) || '名称无效',
    })
    name = r.value.trim()
  } catch { return }
  try {
    await kbApi.createFolder(props.kbId, { parent_id: currentFolderId.value, name })
    ElMessage.success('已创建')
    await loadFolders()
  } catch (e) { ElMessage.error(await apiErrorDetail(e)) }
}

async function renameFolder(f: KbFolder) {
  let name: string
  try {
    const r = await ElMessageBox.prompt('新名称', '重命名文件夹', {
      inputValue: f.name, confirmButtonText: '保存', cancelButtonText: '取消',
      inputValidator: (v) => (!!v?.trim() && !/[\\/]/.test(v)) || '名称无效',
    })
    name = r.value.trim()
  } catch { return }
  try {
    await kbApi.renameFolder(props.kbId, f.id, name)
    await reload()
  } catch (e) { ElMessage.error(await apiErrorDetail(e)) }
}

async function deleteFolder(f: KbFolder) {
  try {
    await ElMessageBox.confirm(`确定删除文件夹「${f.name}」？仅空文件夹可删。`, '删除文件夹', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  try {
    await kbApi.deleteFolder(props.kbId, f.id)
    ElMessage.success('已删除')
    await loadFolders()
  } catch (e) { ElMessage.error(await apiErrorDetail(e)) }
}

// ── 文件操作 ──
async function handleUpload(opts: UploadRequestOptions) {
  const file = opts.file as File
  uploading.value += 1
  try {
    if (file.name.toLowerCase().endsWith('.zip')) {
      const n = (await kbApi.uploadZip(props.kbId, file)).length
      ElMessage.success(`已解压上传 ${n} 个文档`)
    } else {
      await kbApi.uploadDocument(props.kbId, file, { directory: currentPath.value || undefined })
      ElMessage.success(`已上传 ${file.name}`)
    }
    await loadFiles()
  } catch (e) {
    ElMessage.error(await apiErrorDetail(e))
  } finally {
    uploading.value -= 1
  }
}

async function renameFile(file: KbDocument) {
  let name: string
  try {
    const r = await ElMessageBox.prompt('新文件名', '重命名', {
      inputValue: file.document_name, confirmButtonText: '保存', cancelButtonText: '取消',
      inputValidator: (v) => !!v?.trim() || '名称无效',
    })
    name = r.value.trim()
  } catch { return }
  try {
    await kbApi.patchDocument(props.kbId, file.id, { document_name: name })
    await loadFiles()
  } catch (e) { ElMessage.error(await apiErrorDetail(e)) }
}

async function download(file: KbDocument) {
  try {
    const blob = await kbApi.downloadDocument(props.kbId, file.id)
    saveBlob(blob, filenameFromDisposition(null, file.document_name))
  } catch (e) { ElMessage.error(await apiErrorDetail(e)) }
}

// ── 预览 ──
const previewOpen = ref(false)
const previewDoc = ref<KbDocument | null>(null)
function preview(file: KbDocument) {
  previewDoc.value = file
  previewOpen.value = true
}

// ── 拖拽移动 ──
const dragId = ref<string | null>(null)
const dragKind = ref<'file' | 'folder' | null>(null)
const dragOverId = ref<string | null>(null)

function onDragStart(_e: DragEvent, kind: 'file' | 'folder', id: string) {
  dragKind.value = kind
  dragId.value = id
}
function onDragEnd() {
  dragId.value = null
  dragKind.value = null
  dragOverId.value = null
}
async function onDrop(_e: DragEvent, targetFolderId: string) {
  const id = dragId.value
  const kind = dragKind.value
  dragId.value = null
  dragKind.value = null
  dragOverId.value = null
  if (!id || id === targetFolderId) return
  try {
    if (kind === 'file') await kbApi.moveDocument(props.kbId, id, targetFolderId)
    else if (kind === 'folder') await kbApi.moveFolder(props.kbId, id, targetFolderId)
    ElMessage.success('已移动')
    await reload()
  } catch (e) { ElMessage.error(await apiErrorDetail(e)) }
}
async function dropToRoot() {
  const id = dragId.value
  const kind = dragKind.value
  dragId.value = null
  dragKind.value = null
  if (!id) return
  try {
    if (kind === 'file') await kbApi.moveDocument(props.kbId, id, null)
    else if (kind === 'folder') await kbApi.moveFolder(props.kbId, id, null)
    ElMessage.success('已移到根目录')
    await reload()
  } catch (e) { ElMessage.error(await apiErrorDetail(e)) }
}

// ── 辅助 ──
function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toLowerCase() : ''
}
function fileIcon(file: KbDocument): Component {
  const e = extOf(file.document_name)
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'].includes(e)) return Picture
  if (['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'].includes(e)) return Tickets
  return Document
}
function fileIconClass(file: KbDocument): string {
  const e = extOf(file.document_name)
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'].includes(e)) return 'fm__icon--img'
  if (['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'].includes(e)) return 'fm__icon--doc'
  return ''
}
function formatTime(t?: string): string {
  if (!t) return '-'
  return new Date(t).toLocaleDateString('zh-CN')
}

onMounted(reload)
watch(() => currentFolderId.value, loadFiles)
watch(() => props.kbId, reload)
</script>

<style scoped>
.fm { display: flex; flex-direction: column; gap: 12px; }

.fm__toolbar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
}
.fm__crumb { font-size: 13px; }
.fm__crumb-root, .fm__crumb-seg { cursor: pointer; color: var(--kb-accent); }
.fm__crumb-root:hover, .fm__crumb-seg:hover { text-decoration: underline; }
.fm__actions { display: flex; gap: 8px; align-items: center; }

.fm__list {
  background: var(--kb-bg-card); border: 1px solid var(--kb-border-light);
  border-radius: var(--kb-radius); overflow: hidden;
}
.fm__rootdrop {
  border-top: 1px dashed var(--kb-accent); border-bottom: 1px dashed var(--kb-accent);
  background: var(--kb-accent-soft); padding: 6px 14px;
  color: var(--kb-accent); font-size: 12px;
}

/* row = 5-col grid */
.fm__row {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) 90px 110px 120px 56px;
  align-items: center; gap: 10px;
  padding: 9px 14px; border-bottom: 1px solid var(--kb-border-light);
  font-size: 13px; color: var(--kb-text-primary); user-select: none;
}
.fm__row:last-child { border-bottom: none; }
.fm__row--head {
  font-size: 11.5px; font-weight: 600; color: var(--kb-text-tertiary);
  background: transparent; text-transform: uppercase; letter-spacing: 0.3px;
  border-bottom: 1px solid var(--kb-border);
}
.fm__row--folder { cursor: pointer; background: var(--kb-accent-soft); }
.fm__row--file { cursor: pointer; }
.fm__row--folder:hover, .fm__row--file:hover { background: var(--kb-bg-sidebar-hover); }
.fm__row--dragover {
  background: var(--kb-accent-medium) !important;
  box-shadow: inset 0 0 0 2px var(--kb-accent);
}

.fm__col { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fm__col--name { display: flex; align-items: center; gap: 8px; }
.fm__col--type, .fm__col--time { color: var(--kb-text-secondary); font-size: 12px; }
.fm__col--ops { display: flex; justify-content: flex-end; }
.fm__col--muted { color: var(--kb-text-tertiary); }

.fm__icon { font-size: 17px; flex-shrink: 0; color: var(--kb-text-secondary); }
.fm__icon--folder { color: var(--kb-warning); }
.fm__icon--img { color: var(--kb-success); }
.fm__icon--doc { color: var(--kb-danger); }

.fm__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.fm__more { cursor: pointer; color: var(--kb-text-tertiary); padding: 2px 4px; border-radius: 4px; }
.fm__more:hover { color: var(--kb-text-primary); background: var(--kb-border-light); }
</style>
