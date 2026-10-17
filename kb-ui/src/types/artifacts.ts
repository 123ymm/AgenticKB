/**
 * 表格知识制品（61号 §十 REST 契约）。
 *
 * 错误体：`{"detail":{"code":...}}`（stale_artifact / primary_key_conflict）或
 * FastAPI 默认字符串 detail——统一走 apiErrorDetail 取文案。
 */

export type ArtifactKind = 'table' | 'wiki'
export type ArtifactStatus = 'draft' | 'published' | 'deprecated'
export type ArtifactRole = 'owner' | 'editor' | 'viewer'
export type ArtifactColumnType = 'text' | 'enum' | 'number' | 'date' | 'boolean'

/** 制品元信息（列表行 = 同形状 + my_role）。 */
export interface Artifact {
  artifact_id: string
  domain_id: string
  kind: ArtifactKind
  title: string
  description: string
  usage_note: string
  owner_user_id: string
  status: ArtifactStatus
  current_revision: number
  draft_open: boolean
  created_at: string
  updated_at: string
  /** 列表口径（list_artifacts_for_user）附带；详情不含。 */
  my_role?: ArtifactRole
}

/** GET /{id} = 元信息 + schema + sample_rows（同体展开）。 */
export interface ArtifactDetail extends Artifact {
  schema: ArtifactColumn[]
  sample_rows: ArtifactRow[]
}

/** kb_table_columns 行（含 alias——旧列名兼容读取）。 */
export interface ArtifactColumn {
  artifact_id: string
  column_id: string
  column_name: string
  column_type: ArtifactColumnType | string
  enum_values: string[] | null
  is_primary_key: boolean
  is_indexed: boolean
  alias: string[] | null
  status: 'active' | 'dropped' | string
  ordinal: number
}

/** kb_table_rows 行：data 键=列名；provenance/conflicts 由后端记账。 */
export interface ArtifactRow {
  row_key: string
  data: Record<string, unknown>
  provenance: Record<string, unknown> | null
  conflicts: Record<string, unknown> | null
  ordinal: number
}

export interface ArtifactRowsPage {
  rows: ArtifactRow[]
  total: number
  page: number
  size: number
}

/** kb_artifact_revisions 行（路由层补 artifact_id；倒序）。 */
export interface RevisionInfo {
  artifact_id: string
  revision: number
  row_count: number
  change_summary: string
  published_by: string
  published_at: string
}

export interface PublishResult {
  revision: number
  row_count: number
  diff: { added_count: number; removed_count: number; changed_count: number }
}

export interface DiffCellChange {
  row_key: string
  column: string
  old: unknown
  new: unknown
}

/** draft vs current 行级+列级差异（「本次发布将产生的变化」口径）。 */
export interface DiffResult {
  added: string[]
  removed: string[]
  changed: DiffCellChange[]
  added_count: number
  removed_count: number
  changed_count: number
}

export interface MemberInfo {
  user_id: string
  role: ArtifactRole
}

export interface ImportBatch {
  batch_id: string
  artifact_id: string
  channel: string
  file_name: string
  sheet_name: string
  row_count: number
  created_by: string
  created_at: string
}

/** Excel 上传预览列（类型推断 + 枚举候选）。 */
export interface PreviewColumn {
  source_name: string
  inferred_type: string
  enum_candidates: string[]
}

export interface PreviewPayload {
  file_name: string
  sheets: string[]
  default_sheet: string
  columns: PreviewColumn[]
  sample_rows: Array<Record<string, unknown>>
}

/** upload/preview 共同信封：{preview_token, preview}。 */
export interface PreviewEnvelope {
  preview_token: string
  preview: PreviewPayload
}

export interface ImportConflictPolicy {
  type_error?: 'strict' | 'tolerant'
  key_conflict?: 'block' | 'keep_first'
}

export interface ImportReport {
  batch_id?: string
  inserted?: number
  updated?: number
  skipped?: number
  columns?: ArtifactColumn[]
  [key: string]: unknown
}
